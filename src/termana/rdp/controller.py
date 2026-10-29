"""Direct RDP attempt lifecycle, one-shot trust/auth and validated display/input."""

import struct
from dataclasses import dataclass, field
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QCursor, QImage, QPixmap

from termana.models.rdp_options import RdpOptions
from termana.rdp.backend import RdpBackend
from termana.rdp.certificate import decode_certificate
from termana.rdp.endpoint import bounded_desktop_size, validate_display_size
from termana.rdp.frame import RdpFrame
from termana.rdp.identity import RdpAttempt
from termana.rdp.input import RdpKeyInput, RdpPointerInput, RdpReleaseAll
from termana.rdp.ipc import CLIPBOARD_MAX, Kind, fields
from termana.rdp.keymap import modifier_keys, pointer_flags, virtual_key


@dataclass(frozen=True)
class ClipboardText:
    request: int
    text: str | None = field(repr=False)


class RdpController(QObject):
    state_changed = Signal(str)
    certificate_requested = Signal(object)
    credentials_requested = Signal(object)
    stopped = Signal()
    channels_changed = Signal()
    clipboard_offered = Signal(int)
    clipboard_requested = Signal(object)
    clipboard_received = Signal(object)
    clipboard_acknowledged = Signal(object)

    def __init__(self, surface, host, port, parent=None, *, backend_factory=RdpBackend):
        super().__init__(parent)
        self.surface = surface
        self.host, self.port = host, port
        self._factory = backend_factory
        self.backend = None
        self.owner = None
        self.state = "Ended"
        self._pending = None
        self._trust_decided = False
        self._auth_used = False
        self._active = False
        self._logged_on = False
        self._has_frame = False
        self._last_request = 0
        self._key_vks = {}
        self._synthetic_modifiers = set()
        self.diagnostic = ()
        self.failure_reason = ""
        self.end_code = None
        self.metrics = {}
        self.display_size = (1280, 720)
        self.options = RdpOptions()
        self.channel_capabilities = 0
        self.clipboard_offer = 0
        self._clip_read = None
        self._clip_reply = None
        self.certificate_fingerprint = None
        self.certificate_details = None
        self.certificate_exception = False
        self.selected_protocol = None
        surface.remote_input.connect(self.send_input)

    @property
    def running(self):
        return self.backend is not None

    def _state(self, value):
        self.state = value
        self.state_changed.emit(value)

    def start(self):
        if self.backend is not None:
            return False
        validate_display_size(self.display_size)
        self.owner = RdpAttempt(self.surface.session_id, uuid4())
        self._pending = None
        self._trust_decided = self._auth_used = self._active = False
        self._logged_on = self._has_frame = False
        self._last_request = 0
        self._key_vks.clear()
        self._synthetic_modifiers.clear()
        self.diagnostic = ()
        self.failure_reason = ""
        self.end_code = None
        self.metrics = {}
        self.channel_capabilities = 0
        self.clipboard_offer = 0
        self._clip_read = self._clip_reply = None
        self.certificate_fingerprint = None
        self.certificate_details = None
        self.certificate_exception = False
        self.selected_protocol = None
        self.surface.begin_attempt(self.owner)
        self.surface.unsetCursor()
        backend = self._factory(self.owner, self.host, self.port, self)
        backend.display_size = self.display_size
        backend.rdp_flags = self.options.native_flags
        self.backend = backend
        owner = self.owner
        backend.event_received.connect(lambda packet: self._receive(owner, backend, packet))
        backend.failed.connect(lambda message: self._failed(owner, backend, message))
        backend.finished.connect(lambda: self._finished(owner, backend))
        self._state("Connecting")
        if not self._current(owner, backend) or self.state != "Connecting":
            return False
        backend.start()
        return True

    def _current(self, owner, backend):
        return self.owner == owner and self.backend is backend

    def _receive(self, owner, backend, packet):
        if not self._current(owner, backend) or self.state in {"Disconnecting", "Failed"}:
            return
        try:
            if packet.kind is Kind.HELLO:
                return
            if packet.kind in (Kind.CERT, Kind.AUTH):
                if self._pending is not None or self._active or packet.request <= self._last_request:
                    raise ValueError("Unexpected RDP prompt")
                self._last_request = packet.request
            if packet.kind is Kind.CERT:
                if self._trust_decided or self._auth_used:
                    raise ValueError("Unexpected certificate")
                details = decode_certificate(packet.data)
                fingerprint = details.fingerprint
                self.certificate_details = details
                self.certificate_fingerprint = fingerprint
                self._pending = (Kind.CERT, packet.request)
                self._state("Certificate review")
                if self._current(owner, backend) and self._pending == (Kind.CERT, packet.request):
                    self.certificate_requested.emit((packet.request, fingerprint,
                                                     struct.unpack_from("!I", packet.data)[0]))
            elif packet.kind is Kind.AUTH:
                if (len(packet.data) != 8 or struct.unpack_from("!I", packet.data)[0] != 0
                        or struct.unpack_from("!I", packet.data, 4)[0] not in (2, 8)
                        or self._auth_used):
                    raise ValueError("Non-NLA authentication refused")
                self._pending = (Kind.AUTH, packet.request)
                self._auth_used = True
                self._state("Sign in")
                if self._current(owner, backend) and self._pending == (Kind.AUTH, packet.request):
                    self.credentials_requested.emit(packet.request)
            elif packet.kind is Kind.ACTIVE:
                if (self._pending is not None or not self._auth_used or self._active
                        or len(packet.data) != 4 or struct.unpack("!I", packet.data)[0] not in (2, 8)):
                    raise ValueError("Invalid active state")
                self._active = True
                self.selected_protocol = struct.unpack("!I", packet.data)[0]
                self.surface.set_connected(owner, True)
                if not self._current(owner, backend) or self.state in {"Disconnecting", "Failed"}:
                    return
                self._state("Starting desktop")
            elif packet.kind is Kind.LOGON:
                if (not self._auth_used or self._pending is not None or len(packet.data) != 4
                        or struct.unpack("!I", packet.data)[0] not in (0, 1, 2)):
                    raise ValueError("Invalid Windows logon notification")
                self._logged_on = True
                self._desktop_ready()
            elif packet.kind is Kind.FRAME:
                if not self._active or len(packet.data) < 20:
                    raise ValueError("Frame before authentication")
                generation, sequence, width, height, stride = struct.unpack_from("!5I", packet.data)
                if not 0 < width <= 1920 or not 0 < height <= 1080 or stride != width * 4:
                    raise ValueError("RDP desktop exceeds prototype limits")
                frame = RdpFrame(width, height, stride, packet.data[20:])
                if packet.request != sequence or not self.surface.submit_frame(
                    owner, generation, sequence, frame
                ):
                    raise ValueError("Stale or invalid RDP frame")
                if not self._current(owner, backend) or self.state in {"Disconnecting", "Failed"}:
                    return
                backend.send(Kind.ACK, sequence)
                self._has_frame = True
                self._desktop_ready()
            elif packet.kind is Kind.CURSOR:
                self._cursor(packet.data)
            elif packet.kind is Kind.CHANNELS:
                if len(packet.data) != 4 or packet.request:
                    raise ValueError("Invalid channel capabilities")
                mask = struct.unpack("!I", packet.data)[0]
                if mask > 3:
                    raise ValueError("Invalid channel mask")
                self.channel_capabilities = mask
                self.channels_changed.emit()
            elif packet.kind in {Kind.CLIP_OFFER, Kind.CLIP_REQUEST}:
                if len(packet.data) != 4 or not packet.request or not self._active:
                    raise ValueError("Invalid clipboard request")
                value = struct.unpack("!I", packet.data)[0]
                if packet.kind is Kind.CLIP_OFFER:
                    if value != 13:
                        raise ValueError("Unsupported clipboard format")
                    self.clipboard_offer = packet.request
                    self.clipboard_offered.emit(packet.request)
                else:
                    if self._clip_reply is not None:
                        raise ValueError("Overlapping clipboard request")
                    self._clip_reply = packet.request
                    self.clipboard_requested.emit((packet.request, value))
            elif packet.kind is Kind.CLIP_ACK:
                if len(packet.data) != 4 or not packet.request or struct.unpack("!I", packet.data)[0] > 1:
                    raise ValueError("Invalid clipboard acknowledgement")
                self.clipboard_acknowledged.emit((packet.request, bool(struct.unpack("!I", packet.data)[0])))
            elif packet.kind is Kind.CLIP_TEXT:
                if not 4 <= len(packet.data) <= CLIPBOARD_MAX + 4 or not packet.request:
                    raise ValueError("Invalid clipboard data length")
                success = struct.unpack_from("!I", packet.data)[0]
                if success not in (0, 1) or (not success and len(packet.data) != 4):
                    raise ValueError("Invalid clipboard response")
                text = packet.data[4:].decode("utf-8") if success else None
                if text is not None and "\0" in text:
                    raise ValueError("Invalid clipboard text")
                if packet.request == self._clip_read:
                    self._clip_read = None
                    self.clipboard_received.emit(ClipboardText(packet.request, text))
            elif packet.kind is Kind.ENDED:
                if len(packet.data) != 8:
                    raise ValueError("Invalid end event")
                code, _connected = struct.unpack("!II", packet.data)
                if _connected not in (0, 1):
                    raise ValueError("Invalid end state")
                self.end_code = code
                self.surface.end_attempt(owner)
                self._pending = None
                self._state(f"Session ended · code {code}")
            elif packet.kind is Kind.DIAGNOSTIC:
                if len(packet.data) > 192 or len(packet.data) % 8:
                    raise ValueError("Invalid diagnostics")
                self.diagnostic = tuple(struct.iter_unpack("!II", packet.data))
                self.metrics.update({key: value for key, value in self.diagnostic if 100 <= key <= 102})
            else:
                raise ValueError("Unexpected event")
        except (ValueError, UnicodeError, struct.error) as error:
            self._failed(owner, backend, str(error) if type(error) is ValueError
                         else "Invalid RDP event encoding")
            backend.stop()

    def _desktop_ready(self):
        if self._active and self._logged_on and self._has_frame and self.state != "Connected":
            self.surface.set_input_enabled(self.owner, True)
            self._state("Connected")

    def answer_certificate(self, request: int, accepted: bool):
        if self._pending != (Kind.CERT, request) or self.backend is None:
            return False
        self._pending = None
        self._trust_decided = True
        self.certificate_exception = bool(accepted)
        backend, owner = self.backend, self.owner
        self._state("Connecting" if accepted else "Disconnecting")
        if not self._current(owner, backend):
            return False
        return backend.send(Kind.TRUST, request, struct.pack("!I", bool(accepted)))

    def submit_credentials(self, request: int, username: str, domain: str, password: str):
        if self._pending != (Kind.AUTH, request) or self.backend is None:
            return False
        try:
            valid = (all(type(value) is str and "\0" not in value
                         for value in (username, domain, password))
                     and username and password and len(username.encode("utf-8")) <= 512
                     and len(domain.encode("utf-8")) <= 512 and len(password.encode("utf-8")) <= 4096)
        except UnicodeError:
            valid = False
        if not valid:
            return False
        data = fields(username, domain, password)
        self._pending = None
        backend, owner = self.backend, self.owner
        self._state("Authenticating")
        try:
            if not self._current(owner, backend) or self.state != "Authenticating":
                return False
            return backend.send(Kind.SECRET, request, data)
        finally:
            data = b""
            password = ""

    def send_input(self, event):
        if (self.backend is None or not self._active or event.owner != self.owner
                or self.state not in {"Connected", "Starting desktop"}):
            return
        if isinstance(event, RdpReleaseAll):
            self._key_vks.clear()
            self._synthetic_modifiers.clear()
            self.backend.send(Kind.RELEASE)
        elif isinstance(event, RdpKeyInput):
            if self.surface.view_only or self.surface.private:
                return
            identity = (event.key, event.native_scan_code, event.native_virtual_key)
            if event.pressed:
                virtual = virtual_key(event.key)
                if virtual is None:
                    return
                if virtual not in {0x10, 0x11, 0x12, 0x5B}:
                    self._sync_modifiers(event.modifiers)
                already_held = virtual in self._key_vks.values() or virtual in self._synthetic_modifiers
                self._synthetic_modifiers.discard(virtual)
                self._key_vks[identity] = virtual
                if already_held and not event.auto_repeat:
                    return
            else:
                virtual = self._key_vks.pop(identity, None)
                if virtual is None:
                    return
                if virtual in self._key_vks.values():
                    return
            self.backend.send(Kind.KEY, data=struct.pack("!III", virtual,
                                                       event.pressed, event.auto_repeat))
            if not event.pressed and virtual not in {0x10, 0x11, 0x12, 0x5B}:
                self._sync_modifiers(event.modifiers)
        elif isinstance(event, RdpPointerInput):
            if self.surface.view_only or self.surface.private:
                return
            self.backend.send(Kind.MOUSE, data=struct.pack("!4I", event.desktop_generation,
                              pointer_flags(event), event.x, event.y))

    def resize_remote(self, width, height):
        if (self.state != "Connected" or self.backend is None or not self.options.dynamic_resolution
                or not self.channel_capabilities & 2
                or bounded_desktop_size(width, height) != (width, height)):
            return False
        self.surface.release_input()
        return self.backend.send(Kind.RESIZE, data=struct.pack("!II", width, height))

    def set_clipboard_policy(self, mask):
        if type(mask) is not int or not 0 <= mask <= 3 or self.backend is None:
            return False
        if self.state != "Connected":
            mask = 0
        granted = int(self.options.clipboard_to_remote) | (int(self.options.clipboard_to_local) << 1)
        return self.backend.send(Kind.CLIP_POLICY, data=struct.pack("!I", mask & granted))

    def announce_clipboard(self, revision, present):
        if (self.backend is None or self.state != "Connected" or not self.options.clipboard_to_remote
                or type(revision) is not int or not 0 < revision <= 0xffffffff
                or type(present) is not bool):
            return False
        return self.backend.send(Kind.CLIP_ANNOUNCE, revision, struct.pack("!I", present))

    def request_clipboard(self, offer):
        if (self.backend is None or self.state != "Connected" or not self.options.clipboard_to_local
                or not self.channel_capabilities & 1 or self._clip_read is not None
                or not offer or offer != self.clipboard_offer):
            return False
        self._clip_read = offer
        sent = self.backend.send(Kind.CLIP_READ, offer)
        if not sent:
            self._clip_read = None
        return sent

    def reply_clipboard(self, request, text=None):
        if self.backend is None or self._clip_reply != request:
            return False
        self._clip_reply = None
        data = b""
        if (self.state == "Connected" and self.options.clipboard_to_remote and type(text) is str
                and "\0" not in text):
            try:
                encoded = text.encode("utf-8")
                if len(encoded) <= CLIPBOARD_MAX:
                    data = struct.pack("!I", 1) + encoded
            except UnicodeError:
                pass
        return self.backend.send(Kind.CLIP_REPLY, request, data or struct.pack("!I", 0))

    def _sync_modifiers(self, modifiers):
        desired = modifier_keys(modifiers)
        physical = set(self._key_vks.values())
        for virtual in self._synthetic_modifiers - desired:
            if virtual not in physical:
                self.backend.send(Kind.KEY, data=struct.pack("!III", virtual, 0, 0))
        self._synthetic_modifiers.intersection_update(desired)
        for virtual in desired - physical - self._synthetic_modifiers:
            self.backend.send(Kind.KEY, data=struct.pack("!III", virtual, 1, 0))
            self._synthetic_modifiers.add(virtual)

    def send_shortcut(self, keys):
        """Explicit local menu action; never bypass view-only/modal/owner guards."""
        if (type(keys) is not tuple or not 1 <= len(keys) <= 4
                or any(type(key) is not int or not 1 <= key <= 255 for key in keys)
                or len(set(keys)) != len(keys)):
            raise ValueError("Invalid remote key combination")
        if self.state != "Connected" or not self.surface.actions_allowed:
            return False
        backend, owner = self.backend, self.owner
        self.surface.release_input()
        self._key_vks.clear()
        self._synthetic_modifiers.clear()
        if not self._current(owner, backend) or backend is None:
            return False
        if not backend.send(Kind.RELEASE):
            return False
        for down, sequence in ((1, keys), (0, reversed(keys))):
            for key in sequence:
                if not self._current(owner, backend) or self.state != "Connected":
                    return False
                if not backend.send(Kind.KEY, data=struct.pack("!III", key, down, 0)):
                    backend.send(Kind.RELEASE)
                    return False
        return True

    def _cursor(self, data):
        if not self._active or len(data) < 4:
            raise ValueError("Invalid cursor")
        mode = struct.unpack_from("!I", data)[0]
        if mode in (0, 1) and len(data) == 4:
            if not self.surface.private:
                self.surface.setCursor(Qt.CursorShape.BlankCursor if not mode else Qt.CursorShape.ArrowCursor)
            return
        if mode != 2 or len(data) < 20:
            raise ValueError("Invalid cursor format")
        width, height, x, y = struct.unpack_from("!4I", data, 4)
        if (not 0 < width <= 96 or not 0 < height <= 96 or x >= width or y >= height
                or len(data) != 20 + width * height * 4):
            raise ValueError("Invalid cursor geometry")
        if self.surface.private:
            return
        image = QImage(data[20:], width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
        self.surface.setCursor(QCursor(QPixmap.fromImage(image), x, y))

    def stop(self):
        self._pending = None
        if self.backend is None:
            return
        self.surface.end_attempt(self.owner)
        self._state("Disconnecting")
        self.backend.stop()

    def _failed(self, owner, backend, reason="RDP connection failed"):
        if self._current(owner, backend):
            self.failure_reason = reason
            self._pending = None
            self.surface.end_attempt(owner)
            self._state("Failed")

    def _finished(self, owner, backend):
        if not self._current(owner, backend):
            return
        self.surface.end_attempt(owner)
        self.backend = None
        self._pending = None
        self._key_vks.clear()
        self._synthetic_modifiers.clear()
        backend.deleteLater()
        if self.state in {"Disconnecting", "Connecting", "Authenticating", "Starting desktop", "Connected"}:
            self._state("Ended")
        if self.backend is None:
            self.stopped.emit()
