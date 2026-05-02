#!/usr/bin/env python3
"""Extract metadata from FL Studio .flp project files.

The FLP format is not officially documented as a stable public interchange
format. This parser is intentionally read-only and defensive: it preserves raw
event details when a field is unknown or context-dependent.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import struct
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


WORD = 64
DWORD = 128
TEXT = 192
DATA = 208

DELPHI_EPOCH = dt.datetime(1899, 12, 30)
MIN_PROJECT_DATE = dt.datetime(1997, 1, 1)
MAX_TIME_SPENT_DAYS = 3650

EVENT_NAMES: dict[int, str] = {
    0: "ChannelIsEnabled",
    2: "ChannelVolumeByte",
    3: "ChannelPanByte",
    9: "ProjectLoopActive",
    10: "ProjectShowInfo",
    12: "ProjectMainVolumeByte",
    15: "ChannelZipped",
    20: "ChannelPingPongLoop",
    21: "ChannelType",
    22: "ChannelRoutedTo",
    23: "ProjectPanLaw",
    28: "ProjectLicensed",
    29: "MixerAPDC",
    30: "PatternsPlayTruncatedNotes",
    32: "ChannelIsLocked",
    64: "ChannelNew",
    65: "PatternNew",
    66: "ProjectTempoCoarse",
    67: "PatternsCurrentlySelected",
    69: "ChannelFreqTilt",
    70: "ChannelFXFlags",
    71: "ChannelCutoff",
    72: "ChannelVolumeWord",
    73: "ChannelPanWord",
    74: "ChannelPreamp",
    75: "ChannelFadeOut",
    76: "ChannelFadeIn",
    80: "ProjectPitch",
    83: "ChannelResonance",
    85: "ChannelStereoDelay",
    86: "ChannelPogo",
    89: "ChannelTimeShift",
    93: "ProjectTempoFine",
    94: "ChannelChildren",
    95: "InsertIcon",
    97: "ChannelSwing",
    98: "SlotIndex",
    128: "PluginColor",
    131: "ChannelRingMod",
    132: "ChannelCutGroup",
    133: "RackWindowHeight",
    135: "ChannelRootNote",
    138: "ChannelDelayModXY",
    139: "ChannelReverb",
    140: "ChannelStretchTime",
    142: "ChannelFineTune",
    143: "ChannelSamplerFlags",
    144: "ChannelLayerFlags",
    145: "ChannelGroupNum",
    146: "ProjectCurrentGroupId",
    147: "InsertOutput",
    149: "InsertColor",
    153: "ChannelAUSampleRate",
    154: "InsertInput",
    155: "PluginIcon",
    156: "ProjectTempo",
    157: "PatternColorOrUnknown157",
    158: "PatternUnknown158",
    159: "ProjectFLBuild",
    160: "PatternChannelIID",
    161: "PatternUnknown161",
    162: "PatternUnknown162",
    164: "PatternLength",
    172: "ProjectFLVersionInfo",
    192: "ChannelNameOrText",
    193: "PatternName",
    194: "ProjectTitle",
    195: "ProjectComments",
    196: "ChannelSamplePath",
    197: "ProjectUrl",
    198: "ProjectRTFComments",
    199: "ProjectFLVersion",
    200: "ProjectLicensee",
    201: "PluginInternalName",
    202: "ProjectDataPath",
    203: "PluginName",
    204: "MixerInsertName",
    206: "ProjectGenre",
    207: "ProjectArtists",
    216: "ArrangementName",
    223: "PlaylistTrackName",
    209: "ChannelDelay",
    212: "PluginWrapper",
    213: "PluginData",
    215: "ChannelParameters",
    218: "ChannelEnvelopeLFO",
    219: "ChannelLevels",
    221: "ChannelPolyphony",
    225: "MixerParams",
    228: "ChannelTracking",
    229: "ChannelLevelAdjusts",
    231: "DisplayGroupNameOrChannelParameters",
    234: "ChannelAutomation",
    235: "PatternControllers",
    236: "PatternNotes",
    237: "ProjectTimestampOrChannelPolyphony",
}

PROJECT_TEXT_IDS = {
    194: "title",
    195: "comments",
    197: "url",
    198: "rtf_comments",
    199: "fl_version",
    200: "licensee_encoded",
    202: "data_path",
    206: "genre",
    207: "artists",
}

BOOL_EVENT_IDS = {0, 9, 10, 15, 20, 28, 29, 30, 32}
SIGNED_16_EVENT_IDS = {80, 95}
SIGNED_32_EVENT_IDS = {142, 145, 146, 147, 154, 161}
TEXT_LIKE_IDS = {
    192,
    193,
    194,
    195,
    196,
    197,
    198,
    199,
    200,
    201,
    202,
    203,
    204,
    206,
    207,
    216,
    223,
    231,
}

VST_SUBEVENT_NAMES = {
    1: "midi",
    2: "flags",
    30: "io",
    31: "inputs",
    32: "outputs",
    50: "plugin_info",
    51: "fourcc",
    52: "guid",
    53: "state",
    54: "name",
    55: "plugin_path",
    56: "vendor",
    57: "unknown_57",
}

PRINTABLE_RE = re.compile(rb"[\x09\x0a\x0d\x20-\x7e]{4,}")
WINDOWS_PATH_RE = re.compile(r"^[a-zA-Z]:\\|\\\\")


@dataclass
class FLPEvent:
    index: int
    offset: int
    event_id: int
    name: str
    kind: str
    length: int
    raw: bytes
    value: Any
    text: str | None = None
    parsed: dict[str, Any] | None = None


class FLPParseError(ValueError):
    """Raised when an FLP cannot be parsed as an event stream."""


def read_u8(data: bytes) -> int:
    return data[0] if data else 0


def read_u16(data: bytes, signed: bool = False) -> int:
    fmt = "<h" if signed else "<H"
    return struct.unpack(fmt, data[:2].ljust(2, b"\x00"))[0]


def read_u32(data: bytes, signed: bool = False) -> int:
    fmt = "<i" if signed else "<I"
    return struct.unpack(fmt, data[:4].ljust(4, b"\x00"))[0]


def read_f32(data: bytes) -> float:
    return struct.unpack("<f", data[:4].ljust(4, b"\x00"))[0]


def read_varint(buf: bytes, pos: int, end: int) -> tuple[int, int, bytes]:
    value = 0
    shift = 0
    start = pos
    while pos < end:
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos, buf[start:pos]
        shift += 7
        if shift > 63:
            raise FLPParseError(f"varint too large at offset 0x{start:x}")
    raise FLPParseError(f"unterminated varint at offset 0x{start:x}")


def strip_nuls(text: str) -> str:
    return text.strip("\ufeff").rstrip("\x00").strip()


def decode_text(raw: bytes, force_ascii: bool = False) -> str:
    if not raw:
        return ""
    if force_ascii:
        return strip_nuls(raw.decode("ascii", errors="replace"))

    odd_bytes = raw[1::2]
    even_bytes = raw[0::2]
    odd_nuls = odd_bytes.count(0) / max(1, len(odd_bytes))
    even_printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in even_bytes)
    even_ratio = even_printable / max(1, len(even_bytes))

    if len(raw) >= 2 and odd_nuls > 0.35 and even_ratio > 0.45:
        sized = raw if len(raw) % 2 == 0 else raw[:-1]
        return strip_nuls(sized.decode("utf-16le", errors="replace"))

    for encoding in ("utf-8", "cp1252", "latin1"):
        try:
            return strip_nuls(raw.decode(encoding))
        except UnicodeDecodeError:
            continue
    return strip_nuls(raw.decode("utf-8", errors="replace"))


def looks_like_text(text: str) -> bool:
    if not text:
        return False
    if len(text) < 2:
        return False
    useful = [c for c in text if c.isprintable() or c in "\r\n\t"]
    if len(useful) / max(1, len(text)) < 0.9:
        return False
    return any(c.isalpha() for c in text)


def short_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def truncate_text(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"... <truncated {len(text) - limit} chars>"


def decode_licensee(encoded: str) -> str:
    decoded = bytearray()
    for idx, char in enumerate(encoded):
        for num in (ord(char) - 26 + idx, ord(char) + 49 + idx):
            if 0 <= num <= 0x10FFFF and chr(num).isalnum():
                if num > 255:
                    continue
                decoded.append(num)
                break
    return decoded.decode("ascii", errors="replace")


def delphi_days_to_iso(days: float) -> str:
    return (DELPHI_EPOCH + dt.timedelta(days=days)).isoformat(sep=" ")


def seconds_to_hms(seconds: float) -> str:
    whole = int(round(seconds))
    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_timestamp_pair(raw: bytes) -> dict[str, Any] | None:
    if len(raw) < 16:
        return None
    created_days, spent_days = struct.unpack("<dd", raw[:16])
    if not 1 <= created_days <= 100000:
        return None
    created_on = DELPHI_EPOCH + dt.timedelta(days=created_days)
    if created_on < MIN_PROJECT_DATE or created_on > dt.datetime.now() + dt.timedelta(days=366):
        return None
    if spent_days < 0 or spent_days > MAX_TIME_SPENT_DAYS:
        return None
    spent_seconds = spent_days * 86400
    return {
        "created_on": created_on.isoformat(sep=" "),
        "created_on_delphi_days": created_days,
        "time_spent_seconds": spent_seconds,
        "time_spent": seconds_to_hms(spent_seconds),
    }


def parse_timestamp(raw: bytes) -> dict[str, Any] | None:
    if len(raw) < 16:
        return None
    for offset in range(0, len(raw) - 15):
        parsed = parse_timestamp_pair(raw[offset : offset + 16])
        if parsed is not None:
            if offset:
                parsed["timestamp_offset"] = offset
            return parsed
    return None


def parse_color(raw: bytes) -> dict[str, int] | None:
    if len(raw) < 4:
        return None
    value = read_u32(raw)
    return {
        "raw": value,
        "r": value & 0xFF,
        "g": (value >> 8) & 0xFF,
        "b": (value >> 16) & 0xFF,
        "a": (value >> 24) & 0xFF,
    }


def parse_wrapper(raw: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {
        "length": len(raw),
        "sha256": short_hash(raw),
    }
    if len(raw) >= 18:
        flags = read_u16(raw[16:18])
        result["flags_raw"] = flags
        result["flags"] = {
            "visible": bool(flags & (1 << 0)),
            "disabled_legacy": bool(flags & (1 << 1)),
            "detached": bool(flags & (1 << 2)),
            "generator": bool(flags & (1 << 4)),
            "smart_disable": bool(flags & (1 << 5)),
            "threaded_processing": bool(flags & (1 << 6)),
            "demo_mode": bool(flags & (1 << 7)),
            "hide_settings": bool(flags & (1 << 8)),
            "minimized": bool(flags & (1 << 9)),
        }
    if len(raw) >= 21:
        result["page"] = raw[20]
    if len(raw) >= 52:
        result["editor_width"] = read_u32(raw[44:48])
        result["editor_height"] = read_u32(raw[48:52])
    return result


def extract_printable_strings(raw: bytes, min_len: int = 4, max_count: int = 50) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = strip_nuls(value)
        if len(value) < min_len or value in seen or not looks_like_text(value):
            return
        if len(set(value)) <= 3 and len(value) > 12:
            return
        seen.add(value)
        found.append(value)

    for match in PRINTABLE_RE.finditer(raw):
        add(match.group(0).decode("utf-8", errors="replace"))
        if len(found) >= max_count:
            return found

    if len(raw) >= min_len * 2:
        utf16_chars = []
        for i in range(0, len(raw) - 1, 2):
            lo, hi = raw[i], raw[i + 1]
            if hi == 0 and (32 <= lo < 127 or lo in (9, 10, 13)):
                utf16_chars.append(chr(lo))
            else:
                if len(utf16_chars) >= min_len:
                    add("".join(utf16_chars))
                utf16_chars = []
        if len(utf16_chars) >= min_len:
            add("".join(utf16_chars))

    return found[:max_count]


def parse_xml_text(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text.startswith("<"):
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    return {
        "root": root.tag,
        "attributes": dict(root.attrib),
    }


def clean_vst_string(value: str, field: str) -> tuple[str, str | None]:
    cleaned = value.rstrip("\x00")
    raw_value = cleaned
    if field == "plugin_path":
        lower = cleaned.lower()
        for suffix in (".component", ".vst3", ".dll", ".vst"):
            marker = lower.find(suffix)
            if marker != -1:
                cleaned = cleaned[: marker + len(suffix)]
                break
    return cleaned, raw_value if raw_value != cleaned else None


def parse_vst_plugin_data(raw: bytes, text_limit: int) -> dict[str, Any] | None:
    if len(raw) < 4:
        return None
    plugin_type = read_u32(raw[:4])
    if plugin_type not in (8, 10, 12):
        return None

    pos = 4
    subevents: list[dict[str, Any]] = []
    fields: dict[str, Any] = {"type": plugin_type}

    while pos < len(raw):
        if pos + 12 > len(raw):
            return None
        sub_id = read_u32(raw[pos : pos + 4])
        pos += 4
        size = struct.unpack("<Q", raw[pos : pos + 8])[0]
        pos += 8
        if size > len(raw) - pos:
            return None
        chunk = raw[pos : pos + size]
        pos += size

        name = VST_SUBEVENT_NAMES.get(sub_id, f"unknown_{sub_id}")
        subevent: dict[str, Any] = {
            "id": sub_id,
            "name": name,
            "length": size,
            "sha256": short_hash(chunk),
        }

        if name in {"fourcc", "name", "plugin_path", "vendor"}:
            value = decode_text(chunk)
            if name in {"name", "plugin_path", "vendor"}:
                value, raw_value = clean_vst_string(value, name)
                if raw_value is not None:
                    subevent["raw_value"] = raw_value
            subevent["value"] = value
            fields[name] = value
        elif name == "guid":
            subevent["hex"] = chunk.hex()
            fields["guid_hex"] = chunk.hex()
        elif name == "state":
            strings = extract_printable_strings(chunk, max_count=25)
            subevent["embedded_strings"] = [truncate_text(s, text_limit) for s in strings]
            fields["state_length"] = size
            fields["state_sha256"] = short_hash(chunk)
            if strings:
                fields["state_strings"] = [truncate_text(s, text_limit) for s in strings]
                for item in strings:
                    xml = parse_xml_text(item)
                    if xml:
                        fields["state_xml"] = xml
                        break
        elif name == "midi":
            if len(chunk) >= 12:
                subevent["value"] = {
                    "input": read_u32(chunk[0:4], signed=True),
                    "output": read_u32(chunk[4:8], signed=True),
                    "pitch_bend_range": read_u32(chunk[8:12]),
                }
        elif name == "flags":
            if len(chunk) >= 17:
                subevent["flags_raw"] = read_u32(chunk[9:13])
                subevent["flags2_raw"] = read_u32(chunk[13:17])

        subevents.append(subevent)

    fields["subevents"] = subevents
    return fields


def event_kind(event_id: int) -> str:
    if event_id < WORD:
        return "byte"
    if event_id < DWORD:
        return "word"
    if event_id < TEXT:
        return "dword"
    if event_id < DATA:
        return "text"
    if event_id in TEXT_LIKE_IDS:
        return "text_or_data"
    return "data"


def decode_event_value(event_id: int, raw: bytes, text_limit: int) -> tuple[Any, str | None, dict[str, Any] | None]:
    text: str | None = None
    parsed: dict[str, Any] | None = None

    if event_id < WORD:
        value: Any = read_u8(raw)
        if event_id in BOOL_EVENT_IDS:
            value = bool(value)
        return value, text, parsed

    if event_id < DWORD:
        value = read_u16(raw, signed=event_id in SIGNED_16_EVENT_IDS)
        return value, text, parsed

    if event_id < TEXT:
        if event_id == 140:
            value = read_f32(raw)
        elif event_id == 128 or event_id == 149 or event_id == 157:
            value = parse_color(raw) or read_u32(raw)
        elif event_id == 172:
            value = read_u32(raw)
            parsed = parse_version_info_tail(raw)
            if parsed is not None:
                value = {"flags_raw": value, **parsed}
                text = parsed["version_info"]
        else:
            value = read_u32(raw, signed=event_id in SIGNED_32_EVENT_IDS)
        return value, text, parsed

    if event_id == 212:
        parsed = parse_wrapper(raw)
        return parsed, text, parsed

    if event_id == 213:
        parsed = parse_vst_plugin_data(raw, text_limit)
        if parsed is not None:
            return parsed, text, parsed
        embedded = extract_printable_strings(raw)
        return {
            "length": len(raw),
            "sha256": short_hash(raw),
            "embedded_strings": [truncate_text(s, text_limit) for s in embedded],
        }, text, parsed

    if event_id in TEXT_LIKE_IDS:
        text = decode_text(raw, force_ascii=event_id == 199)
        value = text
    else:
        maybe_text = decode_text(raw)
        if looks_like_text(maybe_text):
            text = maybe_text
            value = text
        else:
            value = {
                "length": len(raw),
                "sha256": short_hash(raw),
                "embedded_strings": [
                    truncate_text(s, text_limit) for s in extract_printable_strings(raw)
                ],
            }

    if event_id == 237:
        parsed = parse_timestamp(raw)
        if parsed is not None:
            value = parsed

    return value, text, parsed


def parse_version_info_tail(raw: bytes) -> dict[str, Any] | None:
    if len(raw) <= 5:
        return None
    try:
        length, pos, length_bytes = read_varint(raw, 4, len(raw))
    except FLPParseError:
        return None
    if length <= 0 or pos + length > len(raw):
        return None
    payload = raw[pos : pos + length]
    version_info = decode_text(payload)
    if not version_info.startswith("FL Studio "):
        return None
    return {
        "version_info": version_info,
        "version_info_length": length,
        "version_info_length_bytes": length_bytes.hex(),
    }


def consume_version_info_tail(blob: bytes, pos: int, end: int) -> bytes:
    try:
        length, tail_pos, length_bytes = read_varint(blob, pos, end)
    except FLPParseError:
        return b""
    if length <= 0 or length > end - tail_pos or length > 512:
        return b""
    payload = blob[tail_pos : tail_pos + length]
    if len(payload) % 2:
        return b""
    version_info = decode_text(payload)
    if not version_info.startswith("FL Studio "):
        return b""
    return length_bytes + payload


def parse_flp(path: Path, text_limit: int) -> tuple[dict[str, Any], list[FLPEvent]]:
    blob = path.read_bytes()
    warnings: list[str] = []
    if len(blob) < 22:
        raise FLPParseError("file is too small to be an FLP")
    if blob[:4] != b"FLhd":
        raise FLPParseError("missing FLhd header")

    header_size = struct.unpack("<I", blob[4:8])[0]
    header_start = 8
    header_end = header_start + header_size
    if header_size < 6 or header_end + 8 > len(blob):
        raise FLPParseError(f"unexpected FLhd size {header_size}")

    file_format, channel_count, ppq = struct.unpack("<hHH", blob[header_start : header_start + 6])
    data_magic = blob[header_end : header_end + 4]
    if data_magic != b"FLdt":
        raise FLPParseError("missing FLdt data chunk")

    data_size = struct.unpack("<I", blob[header_end + 4 : header_end + 8])[0]
    data_start = header_end + 8
    data_end = data_start + data_size
    if data_end > len(blob):
        warnings.append(
            f"FLdt chunk declares {data_size} bytes, but file ends {data_end - len(blob)} bytes early; parsed available bytes"
        )
        data_end = len(blob)

    events: list[FLPEvent] = []
    pos = data_start
    while pos < data_end:
        event_offset = pos
        event_id = blob[pos]
        pos += 1

        if event_id < WORD:
            length = 1
            if pos + length > data_end:
                warnings.append(f"event at 0x{event_offset:x} overruns FLdt chunk; stopped before incomplete event")
                break
            raw = blob[pos : pos + length]
            pos += length
        elif event_id < DWORD:
            length = 2
            if pos + length > data_end:
                warnings.append(f"event at 0x{event_offset:x} overruns FLdt chunk; stopped before incomplete event")
                break
            raw = blob[pos : pos + length]
            pos += length
        elif event_id < TEXT:
            length = 4
            if pos + length > data_end:
                warnings.append(f"event at 0x{event_offset:x} overruns FLdt chunk; stopped before incomplete event")
                break
            raw = blob[pos : pos + length]
            pos += length
            if event_id == 172:
                tail = consume_version_info_tail(blob, pos, data_end)
                if tail:
                    raw += tail
                    length += len(tail)
                    pos += len(tail)
        else:
            try:
                length, pos, _length_bytes = read_varint(blob, pos, data_end)
            except FLPParseError as exc:
                warnings.append(str(exc))
                break
            available = data_end - pos
            if length > available:
                warnings.append(
                    f"event at 0x{event_offset:x} declares {length} bytes, only {available} available; parsed partial event"
                )
                raw = blob[pos:data_end]
                pos = data_end
            else:
                raw = blob[pos : pos + length]
                pos += length

        try:
            value, text, parsed = decode_event_value(event_id, raw, text_limit)
        except Exception as exc:  # noqa: BLE001 - one malformed event should not hide earlier metadata.
            warnings.append(f"event at 0x{event_offset:x} could not be decoded: {exc}")
            value = {"length": len(raw), "sha256": short_hash(raw), "decode_error": str(exc)}
            text = None
            parsed = None
        events.append(
            FLPEvent(
                index=len(events),
                offset=event_offset,
                event_id=event_id,
                name=EVENT_NAMES.get(event_id, f"Event{event_id}"),
                kind=event_kind(event_id),
                length=length,
                raw=raw,
                value=value,
                text=text,
                parsed=parsed,
            )
        )

    stat = path.stat()
    header = {
        "magic": "FLhd",
        "header_size": header_size,
        "format": file_format,
        "format_name": {
            0: "Project",
            16: "Score",
            24: "Automation",
            32: "ChannelState",
            48: "PluginState",
            49: "GeneratorState",
            50: "FXState",
            64: "InsertState",
        }.get(file_format, "Unknown"),
        "channel_count": channel_count,
        "ppq": ppq,
    }
    file_info = {
        "path": str(path),
        "size": len(blob),
        "modified": dt.datetime.fromtimestamp(stat.st_mtime).isoformat(sep=" "),
        "created": dt.datetime.fromtimestamp(stat.st_ctime).isoformat(sep=" "),
        "sha256": short_hash(blob),
    }
    data_chunk = {
        "magic": "FLdt",
        "offset": data_start,
        "declared_size": data_size,
        "parsed_size": pos - data_start,
        "event_count": len(events),
    }
    if warnings:
        data_chunk["warnings"] = warnings
    return {"file": file_info, "header": header, "data_chunk": data_chunk}, events


def event_to_json(event: FLPEvent, text_limit: int, include_raw_preview: bool = False) -> dict[str, Any]:
    item = {
        "index": event.index,
        "offset": event.offset,
        "offset_hex": f"0x{event.offset:x}",
        "id": event.event_id,
        "name": event.name,
        "kind": event.kind,
        "length": event.length,
        "value": event.value,
    }
    if event.text is not None:
        item["text"] = truncate_text(event.text, text_limit)
    if include_raw_preview:
        item["raw_preview_hex"] = event.raw[:64].hex()
        item["raw_sha256"] = short_hash(event.raw)
    return item


def summarize_events(events: list[FLPEvent]) -> dict[str, Any]:
    counts = Counter(event.event_id for event in events)
    by_id = []
    for event_id, count in sorted(counts.items()):
        lengths = [event.length for event in events if event.event_id == event_id]
        by_id.append(
            {
                "id": event_id,
                "name": EVENT_NAMES.get(event_id, f"Event{event_id}"),
                "count": count,
                "total_bytes": sum(lengths),
                "min_length": min(lengths),
                "max_length": max(lengths),
            }
        )
    return {
        "by_id": by_id,
        "total_events": len(events),
        "unique_event_ids": len(counts),
    }


def reasonable_tempo(value: Any) -> float | None:
    try:
        tempo = float(value)
    except (TypeError, ValueError):
        return None
    if 10 <= tempo <= 999:
        return tempo
    return None


def tempo_candidates(events: list[FLPEvent]) -> list[float]:
    candidates: list[float] = []
    for event in events:
        if event.event_id != 156:
            continue
        if isinstance(event.value, (int, float)):
            candidates.extend([float(event.value) / 1000, float(event.value)])
        if len(event.raw) >= 4:
            candidates.append(read_f32(event.raw))

    coarse_events = [event for event in events if event.event_id == 66 and isinstance(event.value, (int, float))]
    fine_events = [event for event in events if event.event_id == 93 and isinstance(event.value, (int, float))]
    if coarse_events:
        coarse = float(coarse_events[0].value)
        fine = float(fine_events[0].value) / 1000 if fine_events else 0
        candidates.extend([coarse + fine, coarse])

    result: list[float] = []
    for candidate in candidates:
        tempo = reasonable_tempo(candidate)
        if tempo is not None and tempo not in result:
            result.append(tempo)
    return result


def extract_project(events: list[FLPEvent]) -> dict[str, Any]:
    project: dict[str, Any] = {}

    first_by_id: dict[int, FLPEvent] = {}
    for event in events:
        first_by_id.setdefault(event.event_id, event)

    for event_id, key in PROJECT_TEXT_IDS.items():
        event = first_by_id.get(event_id)
        if event and isinstance(event.value, str):
            project[key] = event.value

    if "licensee_encoded" in project:
        project["licensee_decoded"] = decode_licensee(project["licensee_encoded"])

    if 9 in first_by_id:
        project["loop_active"] = first_by_id[9].value
    if 10 in first_by_id:
        project["show_info_on_open"] = first_by_id[10].value
    if 12 in first_by_id:
        project["main_volume_raw"] = first_by_id[12].value
    if 23 in first_by_id:
        pan_law = first_by_id[23].value
        project["pan_law"] = {0: "circular", 2: "triangular"}.get(pan_law, pan_law)
    if 28 in first_by_id:
        project["licensed"] = first_by_id[28].value
    if 80 in first_by_id:
        project["main_pitch_cents"] = first_by_id[80].value
    if 146 in first_by_id:
        project["current_group_id"] = first_by_id[146].value
    if 159 in first_by_id:
        project["fl_build"] = first_by_id[159].value

    tempos = tempo_candidates(events)
    if tempos:
        project["tempo_bpm"] = tempos[0]

    for event in events:
        if event.event_id == 237 and isinstance(event.value, dict) and "created_on" in event.value:
            project.update(event.value)
            break

    return project


def collect_text_events(events: list[FLPEvent], text_limit: int) -> list[dict[str, Any]]:
    result = []
    for event in events:
        if event.text is None or not looks_like_text(event.text):
            continue
        result.append(
            {
                "index": event.index,
                "offset_hex": f"0x{event.offset:x}",
                "id": event.event_id,
                "name": event.name,
                "text": truncate_text(event.text, text_limit),
            }
        )
    return result


def collect_named_values(events: list[FLPEvent]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = defaultdict(list)
    mapping = {
        192: "channel_or_generic_names",
        193: "pattern_names",
        196: "sample_paths",
        203: "plugin_display_names",
        204: "mixer_insert_names",
        216: "arrangement_names",
        223: "playlist_track_names",
        231: "display_group_names_or_parameter_blobs",
    }
    for event in events:
        bucket = mapping.get(event.event_id)
        if bucket and isinstance(event.value, str) and looks_like_text(event.value):
            buckets[bucket].append(event.value)

    return {key: unique_preserve(values) for key, values in sorted(buckets.items())}


def unique_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def looks_like_path(text: str) -> bool:
    return bool(WINDOWS_PATH_RE.search(text) or "/" in text or "\\" in text)


def collect_paths(events: list[FLPEvent], plugins: list[dict[str, Any]]) -> dict[str, list[str]]:
    samples = []
    data_paths = []
    plugin_paths = []

    for event in events:
        if isinstance(event.value, str):
            if event.event_id == 196 or looks_like_path(event.value):
                if event.event_id == 202:
                    data_paths.append(event.value)
                elif event.event_id == 196:
                    samples.append(event.value)

    for plugin in plugins:
        path = plugin.get("plugin_path")
        if isinstance(path, str) and path:
            plugin_paths.append(path)

    return {
        "sample_paths": unique_preserve(samples),
        "plugin_paths": unique_preserve(plugin_paths),
        "project_data_paths": unique_preserve(data_paths),
    }


def collect_plugins(events: list[FLPEvent], text_limit: int) -> list[dict[str, Any]]:
    plugins: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def ensure_current(event: FLPEvent) -> dict[str, Any]:
        nonlocal current
        if current is None:
            current = {
                "event_index": event.index,
                "offset_hex": f"0x{event.offset:x}",
            }
            plugins.append(current)
        return current

    for event in events:
        if event.event_id == 201:
            current = {
                "event_index": event.index,
                "offset_hex": f"0x{event.offset:x}",
                "internal_name": event.value if isinstance(event.value, str) else event.text,
            }
            plugins.append(current)
            continue

        if event.event_id == 203:
            if current is None:
                continue
            plugin = current
            if isinstance(event.value, str):
                plugin["display_name"] = event.value
            continue

        if event.event_id in {212, 213}:
            plugin = ensure_current(event)
            if event.event_id == 212 and isinstance(event.value, dict):
                plugin["wrapper"] = event.value
            elif event.event_id == 213 and isinstance(event.value, dict):
                plugin["data_event_index"] = event.index
                plugin["data_length"] = event.length
                for key in (
                    "type",
                    "name",
                    "plugin_path",
                    "vendor",
                    "fourcc",
                    "guid_hex",
                    "state_length",
                    "state_sha256",
                    "state_strings",
                    "state_xml",
                ):
                    if key in event.value:
                        plugin[key] = event.value[key]
                if "subevents" in event.value:
                    plugin["subevents"] = event.value["subevents"]
                elif "embedded_strings" in event.value:
                    plugin["embedded_strings"] = [
                        truncate_text(s, text_limit) for s in event.value["embedded_strings"]
                    ]

    useful_plugins = []
    for plugin in plugins:
        internal_name = plugin.get("internal_name")
        has_vst_identity = any(key in plugin for key in ("name", "plugin_path", "vendor", "fourcc"))
        has_native_identity = isinstance(internal_name, str) and bool(internal_name.strip())
        if has_vst_identity or has_native_identity:
            useful_plugins.append(plugin)
    return useful_plugins


def collect_embedded_strings(events: list[FLPEvent], text_limit: int) -> list[dict[str, Any]]:
    result = []
    for event in events:
        if event.kind not in {"data", "text_or_data"}:
            continue
        strings = extract_printable_strings(event.raw, max_count=20)
        for text in strings:
            result.append(
                {
                    "event_index": event.index,
                    "offset_hex": f"0x{event.offset:x}",
                    "id": event.event_id,
                    "name": event.name,
                    "text": truncate_text(text, text_limit),
                }
            )
    return result


def build_metadata(
    base: dict[str, Any],
    events: list[FLPEvent],
    text_limit: int,
    include_events: bool,
    include_embedded_strings: bool,
) -> dict[str, Any]:
    plugins = collect_plugins(events, text_limit)
    metadata: dict[str, Any] = {
        **base,
        "project": extract_project(events),
        "names": collect_named_values(events),
        "paths": collect_paths(events, plugins),
        "plugins": plugins,
        "text_events": collect_text_events(events, text_limit),
        "event_summary": summarize_events(events),
    }
    if include_embedded_strings:
        metadata["embedded_strings"] = collect_embedded_strings(events, text_limit)
    if include_events:
        metadata["events"] = [
            event_to_json(event, text_limit, include_raw_preview=True) for event in events
        ]
    return metadata


def print_summary(metadata: dict[str, Any]) -> None:
    project = metadata.get("project", {})
    header = metadata.get("header", {})
    paths = metadata.get("paths", {})
    names = metadata.get("names", {})
    plugins = metadata.get("plugins", [])

    print(f"File: {metadata['file']['path']}")
    print(
        "FLP: "
        f"{project.get('fl_version', 'unknown version')} "
        f"(build {project.get('fl_build', 'unknown')}); "
        f"{header.get('channel_count')} channels; PPQ {header.get('ppq')}"
    )
    if "title" in project:
        print(f"Title: {project['title']}")
    if "comments" in project:
        print(f"Comments: {project['comments']}")
    if "tempo_bpm" in project:
        print(f"Tempo: {project['tempo_bpm']} BPM")
    if "created_on" in project:
        print(f"Created: {project['created_on']} | Time spent: {project.get('time_spent')}")

    if plugins:
        print(f"Plugins: {len(plugins)}")
        for plugin in plugins:
            label = plugin.get("name") or plugin.get("display_name") or plugin.get("internal_name")
            vendor = f" ({plugin['vendor']})" if plugin.get("vendor") else ""
            print(f"  - {label}{vendor}")

    if paths.get("sample_paths"):
        print(f"Sample paths: {len(paths['sample_paths'])}")
        for sample in paths["sample_paths"]:
            print(f"  - {sample}")

    for key, values in names.items():
        if values:
            print(f"{key}: {len(values)}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flp", type=Path, help="Path to the .flp file")
    parser.add_argument("-o", "--output", type=Path, help="Write JSON metadata to this path")
    parser.add_argument("--summary", action="store_true", help="Print a human-readable summary")
    parser.add_argument("--include-events", action="store_true", help="Include every parsed event in JSON")
    parser.add_argument(
        "--include-embedded-strings",
        action="store_true",
        help="Include printable strings extracted from binary event payloads",
    )
    parser.add_argument(
        "--text-limit",
        type=int,
        default=1000,
        help="Maximum characters to keep for long text fields; 0 disables truncation",
    )
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        base, events = parse_flp(args.flp, args.text_limit)
        metadata = build_metadata(
            base,
            events,
            text_limit=args.text_limit,
            include_events=args.include_events,
            include_embedded_strings=args.include_embedded_strings,
        )
    except (OSError, FLPParseError, struct.error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.summary:
        print_summary(metadata)

    if args.output or not args.summary:
        json_text = json.dumps(
            metadata,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            sort_keys=False,
        )
        if args.output:
            args.output.write_text(json_text + "\n", encoding="utf-8")
        else:
            print(json_text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
