"""
Modbus register values: data types, byte/word order, block planning.

Registers are 16-bit words sent big-endian. Multi-register values are put
together in the order named after the bytes of a 32-bit value:
ABCD big-endian (Modicon), CDAB word-swapped, BADC byte-swapped,
DCBA little-endian. 16-bit values only use the byte order (BADC / DCBA swap).
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any, Optional

AREAS = {
    # area: (read function code, bit area, label)
    "coil": (1, True, "Coil"),
    "discrete": (2, True, "Discrete input"),
    "input": (4, False, "Input register"),
    "holding": (3, False, "Holding register"),
}
AREA_SHORT = {"coil": "co", "discrete": "di", "input": "ir", "holding": "hr"}

TYPES = {
    # type: (registers, struct format)
    "bool": (1, None),
    "int16": (1, ">h"), "uint16": (1, ">H"),
    "int32": (2, ">i"), "uint32": (2, ">I"), "float32": (2, ">f"),
    "int64": (4, ">q"), "uint64": (4, ">Q"), "float64": (4, ">d"),
}
ORDERS = ("ABCD", "CDAB", "BADC", "DCBA")

MAX_REGISTERS = 125      # protocol limit per read (FC 3/4)
MAX_BITS = 2000          # protocol limit per read (FC 1/2)


def register_count(area: str, data_type: str) -> int:
    if AREAS[area][1]:
        return 1                                 # one bit
    return TYPES[data_type][0]


def decode(words: list[int], data_type: str, order: str = "ABCD", bit: Optional[int] = None) -> Any:
    """Value from register words (each 0..65535)."""
    if bit is not None or data_type == "bool":
        return bool((words[0] >> (bit or 0)) & 1)
    n, fmt = TYPES[data_type]
    ws = list(words[:n])
    if order in ("CDAB", "DCBA") and n > 1:
        ws.reverse()                             # word swap
    raw = b"".join(w.to_bytes(2, "big") for w in ws)
    if order in ("BADC", "DCBA"):
        raw = b"".join(raw[i + 1:i + 2] + raw[i:i + 1] for i in range(0, len(raw), 2))   # byte swap
    return struct.unpack(fmt, raw)[0]


def encode(value: Any, data_type: str, order: str = "ABCD") -> list[int]:
    """Register words for a value (inverse of decode, used by tests and the server)."""
    if data_type == "bool":
        return [1 if value else 0]
    n, fmt = TYPES[data_type]
    if fmt[1] in "fd":
        raw = struct.pack(fmt, float(value))
    else:
        raw = struct.pack(fmt, int(value))
    if order in ("BADC", "DCBA"):
        raw = b"".join(raw[i + 1:i + 2] + raw[i:i + 1] for i in range(0, len(raw), 2))
    ws = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]
    if order in ("CDAB", "DCBA") and n > 1:
        ws.reverse()
    return ws


def scaled(value: Any, scale: float, offset: float) -> Any:
    """value * scale + offset; integers stay integers when nothing is scaled.
    NaN / infinite floats give None (InfluxDB cannot store them)."""
    if isinstance(value, bool):
        return value
    if scale != 1 or offset != 0:
        value = value * scale + offset
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, float) and (scale != 1 or offset != 0):
        value = round(value, 9)
    return value


@dataclass(frozen=True)
class Point:
    """What to read for one subscription."""
    unit: int
    area: str
    address: int
    data_type: str = "uint16"
    order: str = "ABCD"
    bit: Optional[int] = None
    scale: float = 1.0
    offset: float = 0.0

    @property
    def count(self) -> int:
        return register_count(self.area, self.data_type)

    @property
    def end(self) -> int:
        return self.address + self.count


@dataclass
class Block:
    unit: int
    area: str
    start: int
    count: int
    points: list[int]            # indexes into the point list


def plan_blocks(points: list[Point], max_registers: int = MAX_REGISTERS, max_gap: int = 10) -> list[Block]:
    """Group points into as few reads as possible: same unit and area, ranges
    closer than max_gap apart, at most max_registers (or MAX_BITS bits) long."""
    order = sorted(range(len(points)), key=lambda i: (points[i].unit, points[i].area, points[i].address))
    blocks: list[Block] = []
    for i in order:
        p = points[i]
        limit = MAX_BITS if AREAS[p.area][1] else max(1, min(max_registers, MAX_REGISTERS))
        gap = max_gap * 16 if AREAS[p.area][1] else max_gap
        b = blocks[-1] if blocks else None
        if (b and b.unit == p.unit and b.area == p.area and p.address - (b.start + b.count) <= gap
                and max(b.start + b.count, p.end) - b.start <= limit):
            b.count = max(b.start + b.count, p.end) - b.start
            b.points.append(i)
        else:
            blocks.append(Block(p.unit, p.area, p.address, p.count, [i]))
    return blocks


def point_value(p: Point, block_start: int, data: list) -> Any:
    """Decoded, scaled value of a point from the data of its block."""
    k = p.address - block_start
    if AREAS[p.area][1]:
        return bool(data[k])
    return scaled(decode(data[k:k + p.count], p.data_type, p.order, p.bit), p.scale, p.offset)


def interpretations(words: list[int]) -> list[tuple[str, object]]:
    """The first registers read as each type and order, to find the right one."""
    out: list[tuple[str, object]] = []
    for dtype in ("uint16", "int16", "uint32", "int32", "float32", "float64"):
        n = TYPES[dtype][0]
        if len(words) < n:
            continue
        for order in (("ABCD", "BADC") if n == 1 else ORDERS):
            v = decode(words[:n], dtype, order)
            if isinstance(v, float):
                if not math.isfinite(v) or (v != 0 and not 1e-6 < abs(v) < 1e12):
                    continue                     # implausible: a wrong type / order
                v = float(f"{v:.7g}")
            out.append((f"{dtype} {order}" if n > 1 or order != "ABCD" else dtype, v))
    return out
