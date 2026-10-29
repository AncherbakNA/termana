"""Bounded asynchronous QProcess owner. Secrets exist only in inherited control pipe."""

import secrets
import struct
import sys
from contextlib import ExitStack
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from termana.paths import application_config_directory
from termana.rdp.component import WINDOWS_CLIENT_SHA256, verified_component, windows_component_lease
from termana.rdp.endpoint import RdpEndpoint, validate_display_size
from termana.rdp.identity import RdpAttempt
from termana.rdp.ipc import Decoder, Kind, encode, fields, owner_bytes
from termana.rdp.runtime import runtime_directory


class RdpBackend(QObject):
    event_received = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, owner: RdpAttempt, host: str, port: int, parent=None,
                 *, component: Path | None = None, runtime_root: Path | None = None):
        super().__init__(parent)
        RdpEndpoint(host, port)
        self.owner = owner
        self._host, self._port = host, port
        self.display_size = (1280, 720)
        self.rdp_flags = 0
        self._component = component
        self._component_leases = ExitStack()
        self._root = runtime_root or application_config_directory() / "rdp"
        self._runtime = None
        self.cleanup_failed = False
        self._identity = owner_bytes(owner, secrets.token_bytes(16))
        self._decoder = Decoder(self._identity)
        self._closing = False
        self._done = False
        self._hello = False
        self._reading = False
        self._process_ended = False
        self._ended_event = False
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.started.connect(self._started)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.readyReadStandardError.connect(self._discard_stderr)
        self.process.errorOccurred.connect(self._process_error)
        self.process.finished.connect(self._finished)
        self._kill = QTimer(self)
        self._kill.setSingleShot(True)
        self._kill.timeout.connect(self.process.kill)
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(lambda: self._fail("RDP connection timed out"))

    @property
    def running(self):
        return not self._done and self.process.state() != QProcess.ProcessState.NotRunning

    def start(self):
        try:
            executable = self._component or verified_component()
            if self._component is None and sys.platform == "win32":
                self._component_leases.enter_context(windows_component_lease(executable, WINDOWS_CLIENT_SHA256))
            self._runtime = runtime_directory(self._root)
            environment = QProcessEnvironment.systemEnvironment()
            for key in environment.keys():
                if key.startswith(("DYLD_", "LD_", "WLOG_", "WINPR_", "FREERDP_")) or key in {
                    "SSLKEYLOGFILE", "OPENSSL_CONF", "OPENSSL_MODULES", "KRB5CCNAME",
                }:
                    environment.remove(key)
            self.process.setProcessEnvironment(environment)
            self.process.setWorkingDirectory(self._runtime.name)
            self.process.start(str(executable), ["--ipc-v3"])
            self._deadline.start(120_000)
        except (OSError, ValueError, RuntimeError):
            self._fail("RDP component is unavailable or could not start")
            self._finish_once()

    def _started(self):
        width, height = validate_display_size(self.display_size)
        self.send(Kind.START, 0, struct.pack("!I", self._port)
                  + fields(self._runtime.name, self._host)
                  + struct.pack("!III", width, height, self.rdp_flags))

    def send(self, kind: Kind, request=0, data=b""):
        if self._done or self._closing or self.process.state() != QProcess.ProcessState.Running:
            return False
        if self.process.bytesToWrite() > 65536:
            self._fail("RDP input queue exceeded its limit")
            return False
        packet = encode(kind, request, data, self._identity)
        if self.process.write(packet) < 0:
            self._fail("RDP control channel closed")
            return False
        return True

    def _read(self):
        if self._reading:
            return
        self._reading = True
        try:
            # Finite work per GUI turn even under a busy remote desktop.
            for _ in range(16):
                if not self.process.bytesAvailable():
                    break
                for packet in self._decoder.feed(bytes(self.process.read(65536))):
                    if self._closing:
                        continue
                    if not self._hello:
                        if (packet.kind is not Kind.HELLO or len(packet.data) != 12
                                or struct.unpack_from("!I", packet.data)[0] != self.process.processId()
                                or packet.data[4:] != b"3.32.2\0\0"):
                            raise ValueError("Invalid component handshake")
                        self._hello = True
                    elif packet.kind is Kind.HELLO:
                        raise ValueError("Repeated component handshake")
                    if packet.kind is Kind.ACTIVE:
                        self._deadline.stop()
                    if packet.kind is Kind.ENDED:
                        self._ended_event = True
                    self.event_received.emit(packet)
        except (ValueError, struct.error):
            self._fail("RDP component sent an invalid message")
        finally:
            self._reading = False
        if not self._done and self.process.bytesAvailable():
            QTimer.singleShot(0, self, self._read)
        elif self._process_ended and not self._done:
            if not self._closing and (self._decoder.buffer or not self._ended_event):
                self._fail("RDP component exited without a complete end event")
            self._finish_once()

    def _discard_stderr(self):
        # Never forward raw engine/authentication messages to an application log.
        self.process.readAllStandardError()

    def _process_error(self, _error):
        if not self._closing:
            self._fail("RDP component stopped unexpectedly")
        if self.process.state() == QProcess.ProcessState.NotRunning:
            self._finish_once()

    def _fail(self, message):
        if not self._closing:
            self.failed.emit(message)
        self.stop()

    def stop(self):
        if self._done or self._closing:
            return
        self.send(Kind.STOP)
        self._closing = True
        self._deadline.stop()
        self.process.closeWriteChannel()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self._kill.start(3000)
        else:
            self._finish_once()

    def _finished(self, _code=0, _status=None):
        self._process_ended = True
        self._read()

    def _finish_once(self):
        if self._done:
            return
        self._done = True
        self._deadline.stop()
        self._kill.stop()
        self._decoder.buffer.clear()
        runtime, self._runtime = self._runtime, None
        try:
            if runtime is not None:
                runtime.cleanup()
        except (OSError, ValueError):
            # The process is already finished. A denied cleanup must not strand
            # its page or expose an OS exception containing a private path.
            # Preserve the private directory; never broaden permissions/retry.
            self.cleanup_failed = True
            self.failed.emit("RDP stopped, but temporary files could not be removed")
        finally:
            try:
                self._component_leases.close()
            except (OSError, ValueError, RuntimeError):
                self.cleanup_failed = True
                self.failed.emit("RDP stopped, but component ownership could not be confirmed")
            finally:
                self.finished.emit()
