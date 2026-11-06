"""Native file transport: bounded bytes, safe names and explicit failure stages.

This module never interprets a document as an image or executes generated code.
"""
from __future__ import annotations

import ast
import base64
import binascii
import codecs
import json
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlsplit

from utils.file_names import is_safe_public_filename

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_FILES = 6
MAX_TOTAL_BYTES = 64 * 1024 * 1024
EXPORT_FILE_RE = re.compile(r"(?:sandbox:)?(/mnt/data/[^\s\"'\)\]<>]+)", re.IGNORECASE)
SANDBOX_MARKDOWN_LINK_RE = re.compile(
    r"!?\[([^\]\n]*)\]\(\s*<?(?:sandbox:)?/mnt/data/(?:[^()\n]|\([^()\n]*\))*\)",
    re.IGNORECASE,
)
SANDBOX_LINK_RE = re.compile(r"<sandbox:[^>\n]*>|sandbox:/mnt/data/[^\s\"'\)\]<>]*", re.IGNORECASE)
LIST_MARKER_ONLY_RE = re.compile(r"(?:[-*+•]|\d+[.)])")
CURLY_QUOTES = {
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
}
CODE_QUOTE_REPAIR_SUFFIXES = {".py", ".json"}


class NativeFileError(RuntimeError):
    def __init__(self, stage: str):
        self.stage = stage
        super().__init__("native_file:" + stage)


def file_name(value: object) -> str:
    name = str(value or "")
    if not is_safe_public_filename(name):
        raise NativeFileError("invalid_filename")
    return name


def decode_file_part(part: dict) -> dict:
    item = part.get("file") if isinstance(part.get("file"), dict) else part
    name = file_name(item.get("filename") or item.get("name"))
    raw = item.get("data")
    mime = str(item.get("mime") or mimetypes.guess_type(name)[0] or "application/octet-stream")
    if not isinstance(raw, (bytes, bytearray)):
        encoded = item.get("file_data")
        if not isinstance(encoded, str) or len(encoded) > MAX_FILE_BYTES * 4 // 3 + 1024:
            raise NativeFileError("invalid_or_oversize_input")
        match = re.fullmatch(r"data:([^;,]+);base64,([A-Za-z0-9+/=\r\n]+)", encoded)
        if not match:
            raise NativeFileError("file_data_requires_base64_data_url")
        mime = match.group(1).lower()
        try:
            raw = base64.b64decode(match.group(2).replace("\r", "").replace("\n", ""), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise NativeFileError("invalid_base64") from exc
    data = bytes(raw)
    if not data or len(data) > MAX_FILE_BYTES:
        raise NativeFileError("empty_or_oversize_input")
    if not re.fullmatch(r"[\w.+-]+/[\w.+-]+", mime):
        raise NativeFileError("invalid_mime")
    return {"type": "file", "filename": name, "mime": mime, "data": data}


def signed_asset_url(value: str) -> str:
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        allowed = ("oaiusercontent.com", "blob.core.windows.net", "openai.com", "chatgpt.com")
        if (
            url.scheme != "https"
            or url.port not in (None, 443)
            or url.username
            or url.password
            or url.fragment
            or not any(host == item or host.endswith("." + item) for item in allowed)
        ):
            raise ValueError()
    except ValueError as exc:
        raise NativeFileError("untrusted_asset_url") from exc
    return value


def validate_download(data: bytes, name: str, mime: str) -> None:
    if not data or len(data) > MAX_FILE_BYTES:
        raise NativeFileError("empty_or_oversize_output")
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if mime in {"application/json", "text/html"} and suffix not in {"json", "html", "htm"}:
        raise NativeFileError("unexpected_output_type")
    if suffix in {"txt", "md", "csv", "json", "html", "htm", "xml", "yaml", "yml", "py", "js", "ts", "css", "sql"}:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise NativeFileError("output_text_decode_failed") from exc
        if "\x00" in text or (
            suffix not in {"html", "htm"} and text.lstrip().lower().startswith(("<!doctype html", "<html"))
        ):
            raise NativeFileError("invalid_text_output")
    if suffix == "pdf" and not data.startswith(b"%PDF-"):
        raise NativeFileError("invalid_pdf_output")
    if suffix in {"zip", "pptx", "docx", "xlsx"} and not data.startswith(b"PK\x03\x04"):
        raise NativeFileError("invalid_archive_output")


def _decode_text(data: bytes) -> str:
    return data.decode("utf-8-sig")


def _repair_curly_quotes(text: str, suffix: str) -> str | None:
    repaired = "".join(CURLY_QUOTES.get(char, char) for char in text)
    if repaired == text:
        return None
    try:
        if suffix == ".py":
            ast.parse(repaired)
        elif suffix == ".json":
            json.loads(repaired)
    except (SyntaxError, ValueError, TypeError):
        return None
    return repaired


def repair_generated_code(data: bytes, name: str) -> bytes:
    """Repair typographic quotes only when the repaired code parses and the original does not."""
    suffix = Path(name).suffix.lower()
    if suffix not in CODE_QUOTE_REPAIR_SUFFIXES:
        return data
    try:
        text = _decode_text(data)
    except UnicodeDecodeError:
        return data
    try:
        if suffix == ".py":
            ast.parse(text)
        else:
            json.loads(text)
        return data
    except (SyntaxError, ValueError, TypeError):
        repaired = _repair_curly_quotes(text, suffix)
    if repaired is None:
        return data
    return codecs.encode(repaired, "utf-8")


def clean_generated_text(text: object) -> str:
    """Remove sandbox links while keeping readable link labels and ordinary text."""
    value = str(text or "")
    value = SANDBOX_MARKDOWN_LINK_RE.sub(lambda match: match.group(1), value)
    value = SANDBOX_LINK_RE.sub("", value)
    lines = [line for line in value.splitlines() if not LIST_MARKER_ONLY_RE.fullmatch(line.strip())]
    return "\n".join(lines).strip()
