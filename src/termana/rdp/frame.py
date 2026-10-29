"""Bounded, owned RGBX frames and device-independent remote pointer coordinates."""

import math
from dataclasses import dataclass, field

MAX_FRAME_EDGE = 8192
MAX_FRAME_PIXELS = 3840 * 2160
MAX_FRAME_BYTES = MAX_FRAME_PIXELS * 4


def _positive_int(value: int, maximum: int, name: str) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"Invalid RDP {name}")


@dataclass(frozen=True, slots=True)
class RdpFrame:
    """Top-down RGBX8888, with explicit stride; never a borrowed native buffer."""

    width: int
    height: int
    stride: int
    pixels: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _positive_int(self.width, MAX_FRAME_EDGE, "frame width")
        _positive_int(self.height, MAX_FRAME_EDGE, "frame height")
        if self.width * self.height > MAX_FRAME_PIXELS:
            raise ValueError("RDP frame exceeds the pixel limit")
        if (type(self.stride) is not int or self.stride < self.width * 4
                or self.stride % 4 or self.stride * self.height > MAX_FRAME_BYTES):
            raise ValueError("Invalid RDP frame stride")
        if type(self.pixels) is not bytes or len(self.pixels) != self.stride * self.height:
            raise ValueError("Invalid RDP frame buffer")


@dataclass(frozen=True, slots=True)
class ViewportTransform:
    """Fit a remote frame into logical Qt coordinates without stretching it."""

    remote_width: int
    remote_height: int
    viewport_width: int
    viewport_height: int

    def __post_init__(self) -> None:
        _positive_int(self.remote_width, MAX_FRAME_EDGE, "remote width")
        _positive_int(self.remote_height, MAX_FRAME_EDGE, "remote height")
        for value in (self.viewport_width, self.viewport_height):
            if type(value) is not int or not 0 <= value <= 1_000_000:
                raise ValueError("Invalid RDP viewport extent")

    @property
    def rectangle(self) -> tuple[float, float, float, float]:
        # Keep the constrained edge exact: scale-then-multiply can make the
        # right edge slightly wider than the viewport and admit letterbox input.
        if self.viewport_width * self.remote_height <= self.viewport_height * self.remote_width:
            width = float(self.viewport_width)
            height = self.viewport_width * self.remote_height / self.remote_width
        else:
            height = float(self.viewport_height)
            width = self.viewport_height * self.remote_width / self.remote_height
        return ((self.viewport_width - width) / 2, (self.viewport_height - height) / 2,
                width, height)

    def remote_point(self, x: float, y: float, *, clamp: bool = False) -> tuple[int, int] | None:
        if type(clamp) is not bool:
            raise TypeError("RDP clamp must be a bool")
        if type(x) not in (int, float) or type(y) not in (int, float):
            return None
        try:
            if not math.isfinite(x) or not math.isfinite(y):
                return None
        except OverflowError:
            return None
        left, top, width, height = self.rectangle
        if not width or not height:
            return None
        if not clamp and not (left <= x < left + width and top <= y < top + height):
            return None
        # Clamp before multiplying: even finite offscreen drag coordinates may
        # overflow a float when scaled by the remote width or height.
        x = min(left + width, max(left, x))
        y = min(top + height, max(top, y))
        return (min(self.remote_width - 1, max(0, int((x - left) * self.remote_width / width))),
                min(self.remote_height - 1, max(0, int((y - top) * self.remote_height / height))))
