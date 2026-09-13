"""Decode a compressed Chromaprint fingerprint into its raw sub-fingerprints (pure).

``fpcalc`` without ``-raw`` prints the *compressed* form that AcoustID's lookup
wants. The dedup scan also needs the *raw* 32-bit sub-fingerprints to compare
files with :func:`crate.domain.fingerprint.similarity`. Decoding the compressed
string here means one fpcalc run per file instead of two.

The format follows chromaprint's ``fingerprint_decompressor.cpp``: URL-safe
base64 without padding, holding:

1. A 4-byte header: the algorithm byte, then the sub-fingerprint count
   (24-bit big-endian).
2. A little-endian bit stream of 3-bit "normal" values. Each value is the gap to
   the next set bit of that sub-fingerprint; ``0`` ends a sub-fingerprint and
   ``7`` means "7 + the next exceptional value".
3. After the normal values, byte-aligned, a stream of 5-bit exceptional values.

Each sub-fingerprint after the first is stored XOR-ed with its predecessor.
The decoder is verified against golden fixtures from real ``fpcalc`` output.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass

_HEADER = 4
_MAX_NORMAL = 7
_BITS = 32  # a sub-fingerprint is one 32-bit word


@dataclass(frozen=True)
class Fingerprint:
    algorithm: int
    raw: list[int]


def _unpack(data: bytes, width: int, count: int | None = None) -> list[int]:
    """``width``-bit values from a little-endian bit stream (LSB first). Reads
    ``count`` values, or as many as fit."""
    stream = int.from_bytes(data, "little")
    n = (len(data) * 8) // width if count is None else count
    mask = (1 << width) - 1
    return [(stream >> (i * width)) & mask for i in range(n)]


def _b64decode(text: str) -> bytes:
    cleaned = text.strip()
    return base64.urlsafe_b64decode(cleaned + "=" * (-len(cleaned) % 4))


def decompress(text: str) -> Fingerprint:
    """The algorithm id and raw sub-fingerprints of a compressed fingerprint.

    Raises ``ValueError`` on anything that isn't a well-formed compressed
    fingerprint."""
    try:
        data = _b64decode(text)
    except (ValueError, TypeError) as exc:
        raise ValueError("not a base64 fingerprint") from exc
    if len(data) < _HEADER:
        raise ValueError("fingerprint header too short")
    algorithm = data[0]
    count = int.from_bytes(data[1:4], "big")
    body = data[_HEADER:]

    # Normal values up to the count-th terminator (0).
    normals = _unpack(body, 3)
    seen = used = 0
    for i, value in enumerate(normals):
        if value == 0:
            seen += 1
            if seen == count:
                used = i + 1
                break
    if seen != count:
        raise ValueError("fingerprint body ended early")
    normals = normals[:used]

    # Exceptional values start at the first byte after the normal values.
    exc_needed = sum(1 for v in normals if v == _MAX_NORMAL)
    exc_start = (used * 3 + 7) // 8
    exceptional = _unpack(body[exc_start:], 5)
    if len(exceptional) < exc_needed:
        raise ValueError("fingerprint exceptional bits missing")

    raw: list[int] = []
    value = last_bit = j = 0
    for gap in normals:
        if gap == 0:
            raw.append(value ^ raw[-1] if raw else value)
            value = last_bit = 0
            continue
        step = gap
        if gap == _MAX_NORMAL:
            step += exceptional[j]
            j += 1
        last_bit += step
        if last_bit > _BITS:
            raise ValueError("fingerprint bit position out of range")
        value |= 1 << (last_bit - 1)
    return Fingerprint(algorithm, raw)


def pack_raw(values: list[int]) -> bytes:
    """Raw sub-fingerprints as a compact BLOB (little-endian uint32s)."""
    return struct.pack(f"<{len(values)}I", *values)


def unpack_raw(blob: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(blob) // 4}I", blob[: len(blob) // 4 * 4]))


def to_signed32(values: list[int]) -> list[int]:
    """Raw sub-fingerprints as signed 32-bit ints (``fpcalc -raw -signed``'s form)."""
    return [v - (1 << 32) if v >= 1 << 31 else v for v in values]
