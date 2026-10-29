"""Opt-in bounded output recording; never terminal input or AskPass responses."""
import codecs
import os
import stat
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from uuid import uuid4

from PySide6.QtCore import QObject, Signal

from termana.terminal.private_recovery_files import PrivateDirectory, PrivateFile

PART_BYTES = 5 * 1024 * 1024
MAX_PARTS = 3
ROOT_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
QUEUE_CHUNKS = 16
_budget_lock = Lock()
_reservations = {}


class SessionLogWriter(QObject):
    failed = Signal(str)
    finished = Signal()

    def __init__(self, root, encoding='utf-8', parent=None):
        super().__init__(parent)
        self.root, self.encoding = root, encoding
        self.recording_id = uuid4().hex
        self._queue = Queue(QUEUE_CHUNKS)
        self._stop = Event()
        self._done = Event()
        self._failed = Event()
        self._thread = None

    @property
    def busy(self):
        return self._thread is not None and not self._done.is_set()

    @property
    def accepting(self):
        return self.busy and not self._stop.is_set()

    def start(self):
        if self._thread is not None:
            raise RuntimeError('A recording cannot be restarted')
        self._thread = Thread(target=self._run, name='Termana-session-log', daemon=False)
        try:
            self._thread.start()
        except Exception:
            self._done.set()
            raise

    def append(self, data):
        if not self.accepting:
            return
        if type(data) is not bytes:
            raise TypeError('Session log input must be output bytes')
        try:
            for offset in range(0, len(data), CHUNK_BYTES):
                self._queue.put_nowait(data[offset:offset + CHUNK_BYTES])
        except Full:
            self._fail('Session logging stopped: the disk writer could not keep up. The recording is incomplete.')

    def stop(self):
        self._stop.set()

    def _fail(self, message):
        if not self._failed.is_set():
            self._failed.set()
            self._stop.set()
            self.failed.emit(message)

    def _run(self):
        reservation = (str(self.root), self.recording_id)
        target = None
        try:
            with PrivateDirectory(self.root.parent) as parent:
                with PrivateDirectory(self.root, parent=parent, create=not self.root.exists(), writable=True) as directory:
                    with _budget_lock:
                        used = 0
                        for name in directory.names(maximum=512):
                            info = (self.root / name).lstat()
                            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                                raise ValueError('Nonregular log entry')
                            used += info.st_size
                        reserved = sum(value for (root, _), value in _reservations.items() if root == str(self.root))
                        if used + reserved + PART_BYTES * MAX_PARTS > ROOT_BYTES:
                            self._fail('Session logging stopped: the logs folder reached its size limit. Review or remove old recordings before starting another.')
                            return
                        _reservations[reservation] = PART_BYTES * MAX_PARTS
                    decoder = codecs.getincrementaldecoder(self.encoding)(errors='replace')
                    part, size = 0, 0

                    def write(data):
                        nonlocal target, part, size
                        while data:
                            if target is None or size == PART_BYTES:
                                if target is not None:
                                    os.fsync(target.fd)
                                    target.close()
                                    target = None
                                if part == MAX_PARTS:
                                    self._fail('Session logging stopped at 15 MiB. Existing log parts are retained; start another recording if needed.')
                                    return False
                                part += 1
                                target = PrivateFile(directory, f'session-{self.recording_id}-{part:02d}.log', create=True)
                                size = 0
                            count = min(len(data), PART_BYTES - size)
                            # Every part must remain individually valid UTF-8.
                            while count < len(data) and count > 0 and data[count] & 0xC0 == 0x80:
                                count -= 1
                            if count == 0:
                                size = PART_BYTES
                                continue
                            chunk, data = data[:count], data[count:]
                            target.validate()
                            view = memoryview(chunk)
                            while view:
                                count = os.write(target.fd, view)
                                if count <= 0:
                                    raise OSError('Incomplete log write')
                                view = view[count:]
                                size += count
                        return True

                    while not self._stop.is_set() or not self._queue.empty():
                        try:
                            raw = self._queue.get(timeout=0.1)
                        except Empty:
                            continue
                        if not write(decoder.decode(raw).encode('utf-8')):
                            break
                    else:
                        write(decoder.decode(b'', final=True).encode('utf-8'))
                    if target is not None:
                        os.fsync(target.fd)
        except Exception:
            self._fail('Session logging stopped: the private log file could not be written safely. Check permissions and free space.')
        finally:
            if target is not None:
                target.close()
            with _budget_lock:
                _reservations.pop(reservation, None)
            self._done.set()
            self.finished.emit()
