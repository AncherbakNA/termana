"""Public RDP profile preferences and optional login hints; never secrets/trust."""

from dataclasses import dataclass, replace

from termana.rdp.endpoint import validate_display_size

RDP_ZOOMS = (None, 50, 75, 100, 125, 150, 200)
RDP_KEYBOARD_MODES = ("remote", "local", "fullscreen")


@dataclass(frozen=True, slots=True)
class RdpOptions:
    width: int = 1280
    height: int = 720
    zoom: int | None = None
    view_only: bool = False
    full_screen: bool = False
    username: str = ""
    domain: str = ""
    clipboard_to_remote: bool = False
    clipboard_to_local: bool = False
    dynamic_resolution: bool = False
    keyboard_mode: str = "remote"
    admin_session: bool = False

    def __post_init__(self):
        validate_display_size((self.width, self.height))
        if (self.zoom is not None and type(self.zoom) is not int) or self.zoom not in RDP_ZOOMS:
            raise ValueError("Choose a supported RDP zoom.")
        if type(self.view_only) is not bool or type(self.full_screen) is not bool:
            raise ValueError("RDP view flags must be boolean")
        if any(type(value) is not bool for value in (self.clipboard_to_remote, self.clipboard_to_local,
                                                    self.dynamic_resolution, self.admin_session)):
            raise ValueError("RDP connection flags must be boolean")
        if type(self.keyboard_mode) is not str or self.keyboard_mode not in RDP_KEYBOARD_MODES:
            raise ValueError("Choose where keyboard shortcuts are handled.")
        for value in (self.username, self.domain):
            if (type(value) is not str or value != value.strip()
                    or len(value.encode("utf-8")) > 512
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise ValueError("RDP login hints must be plain text up to 512 bytes.")

    @property
    def is_default(self):
        return self == RdpOptions()

    def without_login(self):
        """A fresh copy carries neither identity nor local clipboard permissions."""
        return replace(self, username="", domain="", clipboard_to_remote=False, clipboard_to_local=False)

    @property
    def native_flags(self):
        return (int(self.admin_session) | (int(self.dynamic_resolution) << 1)
                | (int(self.clipboard_to_remote) << 2) | (int(self.clipboard_to_local) << 3))


DEFAULT_RDP_OPTIONS = RdpOptions()
