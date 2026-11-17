"""
Register lists from pasted text: what a spreadsheet, a manual or an AI gives.

Accepts Markdown tables, tab / semicolon / comma separated text with a header
line, and JSON (a list of objects). Column names and values are matched
loosely (German and English, PLC type names, Modicon addresses such as 40001,
hex addresses, "1/10" scales), and every row becomes the fields of the
register form: name, area, address, type, order, bit, scale, offset, unit_id,
interval_ms, enabled, measurement.
"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Optional

PROMPT = """Extract the Modbus register map from the document below as a Markdown table with exactly these columns:

| name | area | address | type | order | bit | scale | offset | unit_id |

- name: short unique signal name, letters, digits, dots and underscores (e.g. Boiler.Temperature)
- area: holding, input, coil or discrete
- address: 0-based protocol address as a decimal number. If the document uses Modicon numbers (40001, 30001, 10001, 00001), write those as they are.
- type: uint16, int16, uint32, int32, float32, uint64, int64, float64 or bool
- order: byte/word order for 32/64-bit values: ABCD (big-endian), CDAB (word swap), BADC or DCBA; leave empty if not stated
- bit: 0-15 if the signal is a single bit of a register (type bool), else empty
- scale: factor to get the engineering value from the raw value (e.g. 0.1), empty = 1
- offset: added after scaling, empty = 0
- unit_id: only if the document names a different unit / slave id, else empty

One row per signal, no other text."""

# column name → field (lower case, without spaces / punctuation)
_ALIASES = {
    "name": "name", "bezeichnung": "name", "signal": "name", "signalname": "name", "tag": "name",
    "tagname": "name", "variable": "name", "variablenname": "name", "parameter": "name", "parametername": "name",
    "kurzname": "name", "datenpunkt": "name",
    "description": "description", "beschreibung": "description", "text": "description", "kommentar": "description",
    "comment": "description",
    "area": "area", "bereich": "area", "registertype": "area", "registertyp": "area", "objecttype": "area",
    "objekttyp": "area", "table": "area", "tabelle": "area", "function": "area", "functioncode": "area",
    "fc": "area", "funktion": "area", "funktionscode": "area", "registerart": "area", "art": "area",
    "address": "address", "adresse": "address", "addr": "address", "register": "address",
    "registeraddress": "address", "registeradresse": "address", "reg": "address", "startaddress": "address",
    "startadresse": "address", "modbusaddress": "address", "modbusadresse": "address", "modicon": "address",
    "type": "type", "datatype": "type", "datentyp": "type", "typ": "type", "format": "type", "dtype": "type",
    "datenformat": "type", "dataformat": "type",
    "order": "order", "byteorder": "order", "wordorder": "order", "reihenfolge": "order", "endianness": "order",
    "byteorderwordorder": "order", "bytereihenfolge": "order",
    "bit": "bit", "bitnr": "bit", "bitnummer": "bit", "bitnumber": "bit", "bitno": "bit",
    "scale": "scale", "scaling": "scale", "faktor": "scale", "factor": "scale", "skalierung": "scale",
    "multiplier": "scale", "gain": "scale", "resolution": "scale", "auflosung": "scale", "aufloesung": "scale",
    "offset": "offset",
    "unitid": "unit_id", "slave": "unit_id", "slaveid": "unit_id", "deviceid": "unit_id", "geratadresse": "unit_id",
    "slaveaddress": "unit_id", "slaveadresse": "unit_id",
    "intervalms": "interval_ms", "interval": "interval_ms",
    "enabled": "enabled", "aktiv": "enabled", "active": "enabled", "on": "enabled",
    "measurement": "measurement",
}

_TYPES = {
    "bool": "bool", "boolean": "bool", "bit": "bool", "coil": "bool",
    "uint16": "uint16", "u16": "uint16", "uint": "uint16", "word": "uint16", "unsigned16": "uint16",
    "unsignedint": "uint16", "ushort": "uint16", "unsigned": "uint16",
    "int16": "int16", "s16": "int16", "i16": "int16", "int": "int16", "short": "int16", "signed16": "int16",
    "integer": "int16", "sint16": "int16",
    "uint32": "uint32", "u32": "uint32", "udint": "uint32", "dword": "uint32", "unsigned32": "uint32",
    "ulong": "uint32",
    "int32": "int32", "s32": "int32", "i32": "int32", "dint": "int32", "long": "int32", "signed32": "int32",
    "float32": "float32", "f32": "float32", "float": "float32", "real": "float32", "single": "float32",
    "ieee754": "float32", "ieee754float": "float32", "fp32": "float32",
    "uint64": "uint64", "u64": "uint64", "ulint": "uint64", "lword": "uint64",
    "int64": "int64", "s64": "int64", "i64": "int64", "lint": "int64",
    "float64": "float64", "f64": "float64", "double": "float64", "lreal": "float64", "fp64": "float64",
}

_AREAS = [
    (r"^(fc ?0?3|0?3|4x|hr|holding.*|.*holding ?reg.*|haltereg.*|read holding.*)$", "holding"),
    (r"^(fc ?0?4|0?4|3x|ir|inputs?|input ?reg.*|.*input register.*|eingangsreg.*|read input reg.*)$", "input"),
    (r"^(fc ?0?1|0?1|0x|co|coils?|.*coil.*|spule.*)$", "coil"),
    (r"^(fc ?0?2|0?2|1x|di|discrete.*|.*discrete input.*|digitaleingang.*|read discrete.*)$", "discrete"),
]


def _key(header: str) -> str:
    h = re.sub(r"\(.*?\)|\[.*?\]", "", header.lower())
    h = h.replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]", "", h)


def _split_markdown(text: str) -> Optional[list[list[str]]]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return None
    rows = []
    for ln in lines:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue                                     # |---|---| separator
        rows.append([c.strip("`*_ ") for c in cells])
    return rows


def split_table(text: str) -> list[list[str]]:
    """Rows of cells; the first row is the header."""
    text = text.lstrip("﻿").strip()
    text = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", text).strip()   # a code block from a chat answer
    md = _split_markdown(text)
    if md:
        return md
    first = text.splitlines()[0] if text else ""
    delim = max(("\t", ";", ","), key=first.count)
    if first.count(delim) == 0:
        delim = ","
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), delimiter=delim) if any(c.strip() for c in row)]


def parse_rows(text: str) -> list[dict]:
    """Raw rows (field → text) from pasted text; unknown columns are dropped."""
    stripped = text.strip()
    stripped = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", stripped).strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                data = next((v for v in data.values() if isinstance(v, list)), [data])
            table = [list(data[0].keys())] + [[("" if r.get(k) is None else str(r.get(k))) for k in data[0]]
                                              for r in data if isinstance(r, dict)]
        except (ValueError, IndexError, AttributeError):
            table = split_table(text)
    else:
        table = split_table(text)
    if len(table) < 2:
        return []
    fields = [_ALIASES.get(_key(h), "") for h in table[0]]
    # "Register" next to an "Address" column is the Modicon number: keep the address
    out = []
    for cells in table[1:]:
        row: dict[str, str] = {}
        for f, c in zip(fields, cells):
            if f and c and not row.get(f):
                row[f] = c
        if row:
            out.append(row)
    return out


def _number(text: str) -> Optional[float]:
    t = text.strip().lower().replace("×", "").replace(" ", "")
    t = t.lstrip("x*")
    if not t:
        return None
    m = re.fullmatch(r"(-?[\d.,]+)/([\d.,]+)", t)
    if m:
        return float(m.group(1).replace(",", ".")) / float(m.group(2).replace(",", "."))
    t = t.replace(",", ".")
    if re.fullmatch(r"-?0x[0-9a-f]+", t):
        return float(int(t, 16))
    return float(t)


def normalize(row: dict, modicon: bool = True) -> dict:
    """Fields of the register form from a raw row (raises ValueError with a reason).
    modicon: 5/6-digit addresses like 40001 are Modicon numbers (else 0-based as they are)."""
    out: dict = {}
    name = (row.get("name") or row.get("description") or "").strip()
    out["name"] = re.sub(r"\s+", "_", name)[:200] if name else ""

    # type (may carry the order, "bit 3", "float32 (CDAB)")
    tcell = (row.get("type") or "").strip()
    order = (row.get("order") or "").strip().upper()
    m = re.search(r"\b(ABCD|CDAB|BADC|DCBA)\b", tcell.upper())
    if m and not order:
        order = m.group(1)
    bit = (row.get("bit") or "").strip()
    mb = re.search(r"bit\s*(\d{1,2})", tcell.lower())
    if mb and not bit:
        bit = mb.group(1)
    tkey = re.sub(r"[^a-z0-9]", "", re.sub(r"\(.*?\)|abcd|cdab|badc|dcba|bit\s*\d+", "", tcell.lower()))
    dtype = _TYPES.get(tkey, "") if tkey else ""
    if tkey and not dtype:
        m = re.search(r"(u?int|float)(16|32|64)", tkey)
        dtype = m.group(0) if m else ""
        if not dtype:
            raise ValueError(f"type: unknown ({tcell})")
    order_map = {"BIGENDIAN": "ABCD", "BIG": "ABCD", "LITTLEENDIAN": "DCBA", "LITTLE": "DCBA",
                 "WORDSWAP": "CDAB", "BYTESWAP": "BADC", "MSWFIRST": "ABCD", "LSWFIRST": "CDAB"}
    order = order_map.get(re.sub(r"[^A-Z]", "", order), order)
    if order and order not in ("ABCD", "CDAB", "BADC", "DCBA"):
        order = ""

    # area: from its column, else from a Modicon address
    acell = (row.get("area") or "").strip().lower()
    area = ""
    if acell:
        for pattern, a in _AREAS:
            if re.match(pattern, acell):
                area = a
                break
        if not area:
            raise ValueError(f"area: unknown ({row.get('area')})")

    addr_text = (row.get("address") or "").strip().lower().replace(" ", "")
    if not addr_text:
        raise ValueError("address: missing")
    mbit = re.fullmatch(r"(\d+)[.:](\d{1,2})", addr_text)       # 40001.3 → bit 3
    if mbit:
        addr_text, bit = mbit.group(1), bit or mbit.group(2)
    try:
        address = int(addr_text, 16) if addr_text.startswith("0x") else int(float(addr_text.replace(",", ".")))
    except ValueError:
        raise ValueError(f"address: not a number ({row.get('address')})") from None
    modicon_area = {4: "holding", 3: "input", 1: "discrete", 0: "coil"}
    if modicon and len(addr_text) in (5, 6) and not addr_text.startswith("0x") and addr_text[0] in "0134" \
            and address % (10 ** (len(addr_text) - 1)) >= 1:
        m_area = modicon_area[int(addr_text[0])]
        if not area or area == m_area:
            area = m_area
            address = address % (10 ** (len(addr_text) - 1)) - 1        # 40001 → 0
    if not area:
        area = "coil" if dtype == "bool" and not bit else "holding"
    out["area"] = area
    out["address"] = address

    if area in ("coil", "discrete"):
        out["type"] = "bool"
    elif bit != "" or dtype == "bool":
        if bit == "":
            raise ValueError("type bool in a register needs a bit (0 … 15)")
        out["type"], out["bit"] = "uint16", bit
    else:
        out["type"] = dtype or "uint16"
    if order and out["type"] not in ("bool",) and "bit" not in out:
        out["order"] = order

    for field in ("scale", "offset"):
        cell = (row.get(field) or "").strip()
        if cell:
            try:
                out[field] = _number(cell)
            except (ValueError, ZeroDivisionError):
                raise ValueError(f"{field}: not a number ({cell})") from None
    if (row.get("unit_id") or "").strip():
        out["unit_id"] = row["unit_id"].strip()
    for field in ("interval_ms", "enabled", "measurement"):
        if (row.get(field) or "").strip():
            out[field] = row[field].strip()
    if not out["name"]:
        short = {"holding": "HR", "input": "IR", "coil": "CO", "discrete": "DI"}[area]
        out["name"] = f"{short}{address}" + (f".{out['bit']}" if "bit" in out else "")
    return out
