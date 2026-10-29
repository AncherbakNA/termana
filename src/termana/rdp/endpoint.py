"""Validated session-only RDP destination, not a saved connection profile."""

from dataclasses import dataclass

RDP_RESOLUTIONS = ((1024, 768), (1280, 720), (1600, 900), (1920, 1080))
MAX_DESKTOP_WIDTH, MAX_DESKTOP_HEIGHT = 1920, 1080


def bounded_desktop_size(width, height):
    """Display-control bounds, independent of fixed initial presets; width is even."""
    if type(width) is not int or type(height) is not int:
        raise ValueError("RDP dimensions must be integers")
    return (max(200, min(MAX_DESKTOP_WIDTH, width)) // 2 * 2,
            max(200, min(MAX_DESKTOP_HEIGHT, height)))


def validate_display_size(size):
    if (type(size) is not tuple or len(size) != 2
            or any(type(edge) is not int for edge in size) or size not in RDP_RESOLUTIONS):
        raise ValueError("Choose a supported RDP desktop size.")
    return size


@dataclass(frozen=True)
class RdpEndpoint:
    host: str
    port: int = 3389
    width: int = 1280
    height: int = 720

    def __post_init__(self):
        if (type(self.host) is not str or not self.host
                or len(self.host.encode("utf-8")) > 255
                or any(ord(c) <= 32 for c in self.host)):
            raise ValueError("Enter a server name or IP address without spaces (up to 255 bytes).")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Enter a port between 1 and 65535.")
        validate_display_size((self.width, self.height))
