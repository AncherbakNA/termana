"""GUI-owned, opt-in Unicode text bridge. Native code never reads the OS clipboard."""

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication

from termana.rdp.ipc import CLIPBOARD_MAX


class RdpClipboardBridge(QObject):
    def __init__(self, page, *, clipboard=None):
        super().__init__(page)
        self.page = page
        self.controller = page.controller
        self.clipboard = clipboard if clipboard is not None else QApplication.clipboard()
        self._owner = None
        self._policy = 0
        self._revision = 0
        self._acknowledged = 0
        self._text = None
        self._remote_text = None
        self._writing = False
        self._offer = 0
        self._read = 0
        self.clipboard.dataChanged.connect(self.local_changed)
        self.controller.state_changed.connect(self.sync)
        self.controller.channels_changed.connect(self.sync)
        self.controller.clipboard_offered.connect(self.offered)
        self.controller.clipboard_requested.connect(self.requested)
        self.controller.clipboard_received.connect(self.received)
        self.controller.clipboard_acknowledged.connect(self.acknowledged)
        page.surface.eligibility_changed.connect(self.sync)
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.sync)
        self.timer.start()

    def eligible(self):
        return (not self.page._shutdown and self.page._focus_guard()
                and self.controller.state == "Connected"
                and self.controller.channel_capabilities & 1
                and self.page.surface.actions_allowed
                and not self.page.surface.capture_suspended
                and QApplication.activePopupWidget() is None)

    def sync(self, *_):
        owner = self.controller.owner
        if owner != self._owner:
            self._owner = owner
            self._policy = self._offer = self._read = 0
            self._acknowledged = 0
            self._text = self._remote_text = None
        options = self.controller.options
        policy = ((int(options.clipboard_to_remote) | (int(options.clipboard_to_local) << 1))
                  if self.eligible() else 0)
        if policy != self._policy:
            self._policy = policy
            self.controller.set_clipboard_policy(policy)
            if not policy & 1:
                self._text = None
            else:
                self.local_changed()
        if policy & 2 and self._offer and not self._read:
            self._read_offer()

    def local_changed(self):
        if self._writing or not self._policy & 1 or not self.eligible():
            return  # Do not even inspect OS MIME data without a current grant.
        mime = self.clipboard.mimeData()
        text = mime.text() if mime is not None and mime.hasText() and not mime.hasUrls() else None
        if text is not None:
            try:
                if "\0" in text or len(text.encode("utf-8")) > CLIPBOARD_MAX:
                    text = None
            except UnicodeError:
                text = None
        if text == self._remote_text:
            text = None  # Never echo our own remote clipboard write back to Windows.
        self._text = text
        self._revision = self._revision % 0xffffffff + 1
        self._acknowledged = 0
        self.controller.announce_clipboard(self._revision, text is not None)

    def acknowledged(self, result):
        revision, success = result
        if success and revision == self._revision and self._policy & 1:
            self._acknowledged = revision

    def requested(self, request):
        serial, revision = request
        text = (self._text if self.eligible() and self._policy & 1
                and revision == self._revision else None)
        self.controller.reply_clipboard(serial, text)

    def offered(self, offer):
        self._offer = offer
        self.sync()

    def _read_offer(self):
        offer = self._offer
        self._read = offer
        if not self.controller.request_clipboard(offer):
            self._read = 0

    def received(self, result):
        if result.request != self._read:
            return
        self._read = 0
        if result.request == self._offer:
            self._offer = 0
        if (result.text is not None and result.request == self.controller.clipboard_offer
                and self._policy & 2 and self.eligible()):
            self._writing = True
            self._remote_text = result.text
            self._text = None
            try:
                self.clipboard.setText(result.text)  # Plain text only, never remote MIME/URLs.
            finally:
                self._writing = False
        self.sync()

    def shutdown(self):
        self.timer.stop()
        self._policy = self._offer = self._read = 0
        self._text = self._remote_text = None
        self.controller.set_clipboard_policy(0)
