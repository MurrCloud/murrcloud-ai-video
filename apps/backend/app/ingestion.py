"""Bounded DOCX parsing and reference-image preprocessing; no network fetches."""

import hashlib
import io
import json
import os
import stat
import struct
import subprocess
import sys
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from defusedxml import ElementTree as SafeET

MAX_UPLOAD_BYTES = 5 * 1024**2
MAX_SCRIPT_CHARS = 100_000
MAX_CANONICAL_BYTES = 20 * 1024**2
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
CONTENT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"


class IngestionError(Exception):
    def __init__(self, code: str, status: int = 422):
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True)
class ParsedScript:
    text: str
    source_name: str
    sha256: str
    warnings: list[str]


@dataclass(frozen=True)
class CanonicalImage:
    data: bytes
    extension: str
    mime_type: str
    width: int
    height: int
    sha256: str


def safe_source_name(filename: str | None, fallback: str) -> str:
    name = PurePosixPath((filename or fallback).replace("\\", "/")).name
    name = "".join(c for c in name if not unicodedata.category(c).startswith("C"))
    return (name.strip(" .") or fallback)[-160:]


def _xml(data: bytes):
    try:
        root = SafeET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        count, pending = 0, [(root, 0)]
        while pending:
            node, depth = pending.pop()
            count += 1
            if count > 50_000 or depth > 64:
                raise IngestionError("document_xml_too_complex")
            pending.extend((child, depth + 1) for child in node)
        return root
    except IngestionError:
        raise
    except Exception:
        raise IngestionError("document_xml_rejected") from None


def parse_docx(data: bytes, filename: str | None, mime_type: str | None) -> ParsedScript:
    if len(data) > MAX_UPLOAD_BYTES:
        raise IngestionError("document_too_large", 413)
    name = safe_source_name(filename, "script.docx")
    if not name.lower().endswith(".docx") or (mime_type or "application/octet-stream") not in {
        DOCX_MIME,
        "application/octet-stream",
    }:
        raise IngestionError("docx_file_required", 415)
    if not data.startswith(b"PK\x03\x04"):
        raise IngestionError("invalid_docx_signature", 415)
    # Inspect the small EOCD header before ZipFile constructs unbounded ZipInfo
    # objects. Small DOCX uploads have no need for multi-disk/ZIP64 directories.
    footer = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if footer < 0 or len(data) - footer < 22:
        raise IngestionError("invalid_docx_archive")
    _, disk, directory_disk, local_count, count, directory_bytes, directory_offset, comment = (
        struct.unpack_from("<4s4H2LH", data, footer)
    )
    if (
        disk != 0
        or directory_disk != 0
        or local_count != count
        or not 1 <= count <= 256
        or directory_bytes > 128 * 1024
        or directory_offset + directory_bytes > footer
        or data[max(0, footer - 20) : footer - 16] == b"PK\x06\x07"
        or footer + 22 + comment != len(data)
    ):
        raise IngestionError("document_archive_too_complex")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) != count:
                raise IngestionError("document_archive_too_complex")
            seen, total = set(), 0
            for info in members:
                path = info.filename
                folded = path.casefold()
                parts = PurePosixPath(path).parts
                file_mode = info.external_attr >> 16
                if (
                    folded in seen
                    or len(path) > 256
                    or path.startswith("/")
                    or "\\" in path
                    or ":" in path
                    or ".." in parts
                    or stat.S_ISLNK(file_mode)
                    or any(ord(c) < 32 for c in path)
                ):
                    raise IngestionError("unsafe_document_archive_path")
                seen.add(folded)
                if info.flag_bits & 1 or info.compress_type not in {
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                }:
                    raise IngestionError("document_archive_encoding_rejected")
                if any(
                    piece in folded
                    for piece in ("vbaproject", "activex", "embeddings/", "customui/")
                ) or PurePosixPath(folded).suffix in {
                    ".exe",
                    ".dll",
                    ".js",
                    ".vbs",
                    ".bat",
                    ".cmd",
                    ".ps1",
                    ".com",
                    ".scr",
                    ".hta",
                    ".jar",
                    ".ole",
                    ".docm",
                    ".bin",
                    ".zip",
                }:
                    raise IngestionError("active_document_content_rejected")
                total += info.file_size
                if (
                    info.file_size > 8 * 1024**2
                    or total > 16 * 1024**2
                    or info.file_size > max(1, info.compress_size) * 1000
                ):
                    raise IngestionError("document_decompression_limit")
            for required in ("[Content_Types].xml", "word/document.xml"):
                if required not in archive.namelist():
                    raise IngestionError("invalid_docx_package")

            def read_xml(path, limit=1024 * 1024):
                if archive.getinfo(path).file_size > limit:
                    raise IngestionError("document_xml_too_large")
                with archive.open(path) as source:
                    content = source.read(limit + 1)
                if len(content) > limit:
                    raise IngestionError("document_xml_too_large")
                return _xml(content)

            types = read_xml("[Content_Types].xml", 64 * 1024)
            if types.tag != CONTENT_NS + "Types" or not any(
                item.get("PartName") == "/word/document.xml"
                and item.get("ContentType")
                == "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
                for item in types
            ):
                raise IngestionError("invalid_docx_content_type")
            if any(
                any(
                    token in str(item.get("ContentType", "")).lower()
                    for token in ("macroenabled", "vbaproject", "oleobject", "activex")
                )
                for item in types
            ):
                raise IngestionError("active_document_content_rejected")
            warnings = []
            for info in members:
                if info.filename.endswith(".rels"):
                    relationships = read_xml(info.filename, 64 * 1024)
                    if any(
                        item.get("TargetMode", "").lower() == "external" for item in relationships
                    ):
                        warnings = ["external_document_links_ignored"]
            document = read_xml("word/document.xml")
            if document.tag != WORD_NS + "document":
                raise IngestionError("invalid_docx_document")
            text_parts, count, pending = [], 0, [(document, False)]
            while pending:
                node, leaving = pending.pop()
                value = ""
                if leaving and node.tag == WORD_NS + "p":
                    value = "\n"
                elif not leaving:
                    if node.tag == WORD_NS + "t":
                        value = node.text or ""
                    elif node.tag in {WORD_NS + "br", WORD_NS + "cr"}:
                        value = "\n"
                    elif node.tag == WORD_NS + "tab":
                        value = "\t"
                    if node.tag == WORD_NS + "p":
                        pending.append((node, True))
                    pending.extend((child, False) for child in reversed(list(node)))
                count += len(value)
                if count > MAX_SCRIPT_CHARS:
                    raise IngestionError("script_text_too_long", 413)
                text_parts.append(value)
            extracted = "".join(text_parts).strip()
            if not extracted:
                raise IngestionError("document_has_no_script_text")
            return ParsedScript(extracted, name, hashlib.sha256(data).hexdigest(), warnings)
    except IngestionError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError, NotImplementedError):
        raise IngestionError("invalid_docx_archive") from None


def canonicalize_reference(
    data: bytes, filename: str | None, mime_type: str | None
) -> CanonicalImage:
    if len(data) > MAX_UPLOAD_BYTES:
        raise IngestionError("reference_image_too_large", 413)
    name = safe_source_name(filename, "reference")
    expected = {"image/jpeg": {".jpg", ".jpeg"}, "image/png": {".png"}, "image/webp": {".webp"}}
    if mime_type not in expected or PurePosixPath(name.lower()).suffix not in expected[mime_type]:
        raise IngestionError("reference_image_type_rejected", 415)
    signature_ok = (
        (mime_type == "image/jpeg" and data.startswith(b"\xff\xd8\xff"))
        or (mime_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
        or (mime_type == "image/webp" and data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )
    if not signature_ok:
        raise IngestionError("reference_image_signature_rejected", 415)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.image_worker", mime_type],
            input=data,
            capture_output=True,
            timeout=15,
            check=False,
            env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", "OMP_NUM_THREADS": "1"},
        )
        header, separator, canonical = result.stdout.partition(b"\n")
        metadata = json.loads(header) if len(header) <= 1024 else {}
        if result.returncode != 0 or not separator or metadata.get("error"):
            code = metadata.get("error")
            if code not in {
                "image_dimensions_rejected",
                "animated_image_rejected",
                "image_decode_rejected",
                "canonical_image_too_large",
            }:
                code = "image_decode_rejected"
            raise IngestionError(code)
        if (
            metadata.get("mime_type") not in {"image/jpeg", "image/png"}
            or type(metadata.get("width")) is not int
            or type(metadata.get("height")) is not int
            or min(metadata["width"], metadata["height"]) < 300
            or metadata["width"] * metadata["height"] > 8_388_608
            or not 1 <= len(canonical) <= MAX_CANONICAL_BYTES
        ):
            raise IngestionError("image_decode_rejected")
        extension = "jpg" if metadata["mime_type"] == "image/jpeg" else "png"
        return CanonicalImage(
            canonical,
            extension,
            metadata["mime_type"],
            metadata["width"],
            metadata["height"],
            hashlib.sha256(canonical).hexdigest(),
        )
    except subprocess.TimeoutExpired:
        raise IngestionError("reference_processing_timeout", 408) from None
    except IngestionError:
        raise
    except (OSError, ValueError, TypeError):
        raise IngestionError("image_decode_rejected") from None
