"""Versioned attempt-owned binary protocol; no secret or frame repr/logging."""

import struct
from dataclasses import dataclass, field
from enum import IntEnum

from termana.rdp.identity import RdpAttempt

HEADER = struct.Struct("!4sIIII48s")
CONTROL_MAX = 16384
FRAME_MAX = 1920 * 1080 * 4
IPC_VERSION = 3
CLIPBOARD_MAX = 16000


class Kind(IntEnum):
    START = 1
    TRUST = 2
    SECRET = 3
    ACK = 4
    KEY = 5
    MOUSE = 6
    RELEASE = 7
    STOP = 8
    RESIZE = 9
    CLIP_POLICY = 10
    CLIP_ANNOUNCE = 11
    CLIP_READ = 12
    CLIP_REPLY = 13
    HELLO = 101
    CERT = 102
    AUTH = 103
    ACTIVE = 104
    FRAME = 105
    CURSOR = 106
    ENDED = 107
    DIAGNOSTIC = 108
    LOGON = 109
    CHANNELS = 110
    CLIP_OFFER = 111
    CLIP_REQUEST = 112
    CLIP_TEXT = 113
    CLIP_ACK = 114


@dataclass(frozen=True, slots=True)
class Packet:
    kind: Kind
    request: int
    data: bytes = field(repr=False)


def owner_bytes(owner: RdpAttempt, nonce: bytes) -> bytes:
    if type(owner) is not RdpAttempt or type(nonce) is not bytes or len(nonce) != 16:
        raise ValueError("Invalid RDP IPC owner")
    return nonce + owner.session_id.bytes + owner.attempt_id.bytes


def encode(kind: Kind, request: int, data: bytes, owner: bytes) -> bytes:
    if (type(kind) is not Kind or type(request) is not int or not 0 <= request <= 0xFFFFFFFF
            or type(data) is not bytes or len(data) > CONTROL_MAX or len(owner) != 48):
        raise ValueError("Invalid RDP control message")
    return HEADER.pack(b"TRDP", IPC_VERSION, kind, len(data), request, owner) + data


def fields(*values: str) -> bytes:
    result = bytearray()
    for value in values:
        if type(value) is not str or "\0" in value:
            raise ValueError("Invalid RDP text field")
        data = value.encode("utf-8")
        if len(data) > 4096:
            raise ValueError("RDP field is too long")
        result.extend(struct.pack("!I", len(data)))
        result.extend(data)
    if len(result) > CONTROL_MAX:
        raise ValueError("RDP fields exceed the control limit")
    return bytes(result)


class Decoder:
    def __init__(self, owner: bytes):
        self.owner = owner
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[Packet]:
        if len(self.buffer) + len(data) > 2 * (FRAME_MAX + HEADER.size + 20):
            raise ValueError("RDP receive budget exceeded")
        self.buffer.extend(data)
        result = []
        while len(self.buffer) >= HEADER.size:
            magic, version, kind, length, request, owner = HEADER.unpack_from(self.buffer)
            if magic != b"TRDP" or version != IPC_VERSION or owner != self.owner:
                raise ValueError("RDP IPC identity mismatch")
            try:
                kind = Kind(kind)
            except ValueError:
                raise ValueError("Unknown RDP message") from None
            maximum = FRAME_MAX + 20 if kind is Kind.FRAME else (
                96 * 96 * 4 + 20 if kind is Kind.CURSOR else CONTROL_MAX
            )
            if kind < Kind.HELLO or length > maximum:
                raise ValueError("Invalid RDP event length or direction")
            end = HEADER.size + length
            if len(self.buffer) < end:
                break
            result.append(Packet(kind, request, bytes(self.buffer[HEADER.size:end])))
            del self.buffer[:end]
        return result
