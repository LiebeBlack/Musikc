"""Metadata readers for the containers True HQ Audio handles natively.

Only tag *reading* happens here; no tag ever influences the audio path.  The
ID3v2 parser is used by DSF (at the offset declared in the DSD header) and by
DFF (dedicated ``ID3 `` chunk).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from .decoders.base import normalise_tags

ID3_TEXT_FRAMES = {
    "TIT2": "title",
    "TT2": "title",
    "TPE1": "artist",
    "TP1": "artist",
    "TPE2": "album_artist",
    "TP2": "album_artist",
    "TALB": "album",
    "TAL": "album",
    "TRCK": "track",
    "TRK": "track",
    "TPOS": "disc",
    "TDRC": "date",
    "TYER": "date",
    "TYE": "date",
    "TCON": "genre",
    "TCO": "genre",
    "TCOM": "composer",
    "TCM": "composer",
    "TENC": "encoder",
    "TEN": "encoder",
    "TSSE": "encoder_settings",
    "COMM": "comment",
    "COM": "comment",
    "TCOP": "copyright",
    "TCR": "copyright",
}

MAX_TAG_BYTES = 4 * 1024 * 1024


def _decode_text(payload: bytes) -> str:
    if not payload:
        return ""
    encoding = payload[0]
    body = payload[1:]
    if encoding == 0:
        text = body.decode("latin-1", "replace")
    elif encoding == 1:
        text = body.decode("utf-16", "replace")
    elif encoding == 2:
        text = body.decode("utf-16-be", "replace")
    else:
        text = body.decode("utf-8", "replace")
    return text.replace("\x00", "").strip()


def _decode_comment(payload: bytes) -> str:
    if len(payload) < 5:
        return ""
    encoding = payload[0]
    body = payload[4:]
    if encoding in (1, 2):
        try:
            return body.decode("utf-16" if encoding == 1 else "utf-16-be", "replace").strip("\x00").strip()
        except UnicodeDecodeError:
            return ""
    return body.decode("latin-1", "replace").strip("\x00").strip()


def _syncsafe(value: bytes) -> int:
    result = 0
    for byte in value:
        result = (result << 7) | (byte & 0x7F)
    return result


def read_id3v2(payload: bytes, limit: int = MAX_TAG_BYTES) -> Dict[str, str]:
    """Parse an ID3v2.2/2.3/2.4 tag from a raw payload."""

    if len(payload) < 10 or payload[0:3] != b"ID3":
        return {}
    version = payload[3]
    if version not in (2, 3, 4):
        return {}
    flags = payload[5]
    size = _syncsafe(payload[6:10])
    end = min(len(payload), 10 + size, limit)
    offset = 10
    if flags & 0x40 and version >= 3:
        offset += 4
        if offset + 4 <= end:
            extended_size = int.from_bytes(payload[offset : offset + 4], "big")
            offset += 4 + extended_size
    tags: Dict[str, str] = {}
    identifier_size = 3 if version == 2 else 4
    header_size = 6 if version == 2 else 10
    while offset + header_size <= end:
        frame_id_bytes = payload[offset : offset + identifier_size]
        if not frame_id_bytes.strip(b"\x00"):
            break
        try:
            frame_id = frame_id_bytes.decode("latin-1")
        except UnicodeDecodeError:
            break
        if version == 2:
            frame_size = int.from_bytes(payload[offset + 3 : offset + 6], "big")
        elif version == 4:
            frame_size = _syncsafe(payload[offset + 4 : offset + 8])
        else:
            frame_size = int.from_bytes(payload[offset + 4 : offset + 8], "big")
        if frame_size <= 0 or offset + header_size + frame_size > end:
            break
        body = payload[offset + header_size : offset + header_size + frame_size]
        offset += header_size + frame_size
        key = ID3_TEXT_FRAMES.get(frame_id)
        if key is None:
            continue
        if frame_id in ("COMM", "COM"):
            text = _decode_comment(body)
        else:
            text = _decode_text(body)
        if text:
            tags[key] = text
    return tags


def read_id3v2_from_file(path: Path, offset: int) -> Dict[str, str]:
    """Read an ID3v2 tag located at an absolute file offset."""

    if offset <= 0:
        return {}
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            header = handle.read(10)
            if len(header) < 10 or header[0:3] != b"ID3":
                return {}
            size = _syncsafe(header[6:10])
            if size <= 0 or size > MAX_TAG_BYTES:
                return {}
            payload = header + handle.read(size)
    except OSError:
        return {}
    return read_id3v2(payload)


def gui_metadata_block(tags: Dict[str, str]) -> Dict[str, str]:
    """Normalise a tag dictionary for presentation."""

    cleaned = normalise_tags(tags)
    title = cleaned.get("title", "")
    if title.startswith("\ufeff"):
        cleaned["title"] = title.lstrip("\ufeff")
    return cleaned


def format_tag_line(tags: Dict[str, str]) -> str:
    parts = []
    if tags.get("album"):
        parts.append(tags["album"])
    if tags.get("artist"):
        parts.append(tags["artist"])
    if tags.get("date"):
        parts.append(tags["date"][:4])
    return " · ".join(parts)


def safe_int(value: Optional[str], default: int = 0) -> int:
    if not value:
        return default
    head = value.strip().split("/")[0]
    try:
        return int(head)
    except ValueError:
        return default
