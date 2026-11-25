"""Hostile document/image fixtures never contact external services."""

import asyncio
import io
import struct
import subprocess
import zipfile
from uuid import UUID, uuid4

import pytest
from app.ingestion import (
    DOCX_MIME,
    MAX_UPLOAD_BYTES,
    IngestionError,
    canonicalize_reference,
    parse_docx,
)
from app.middleware import BoundedBodyMiddleware
from app.models import MediaAsset, ProjectInput
from app.reference_storage import reference_path, store_reference
from app.storage import StorageError
from PIL import Image, PngImagePlugin
from sqlalchemy import select

ORIGIN = {"Origin": "http://localhost:3000"}
TYPES = b"""<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>"""


def docx(text="Hello world", *, document=None, extras=None):
    document = (
        document
        or (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            + text
            + "</w:t></w:r></w:p></w:body></w:document>"
        ).encode()
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", TYPES)
        archive.writestr("word/document.xml", document)
        for name, data in (extras or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def image_bytes(size=(320, 300), *, mode="RGB", fmt="PNG", metadata=False):
    image = Image.new(mode, size, (120, 40, 10, 100) if mode == "RGBA" else (120, 40, 10))
    buffer = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "private metadata")
    image.save(buffer, fmt, **({"pnginfo": info} if metadata else {}))
    return buffer.getvalue()


def test_docx_extracts_chinese_paragraphs_tabs_breaks_tables_and_ignores_links():
    document = (
        b"""<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>English</w:t><w:tab/><w:t>text</w:t><w:br/><w:t>line</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>"""
        + "中文文案".encode()
        + b"""</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>"""
    )
    content = docx(
        document=document,
        extras={
            "word/_rels/document.xml.rels": b'<Relationships><Relationship TargetMode="External" Target="http://169.254.169.254/latest/meta-data"/></Relationships>'
        },
    )
    result = parse_docx(content, r"C:\private\script.docx", DOCX_MIME)
    assert result.text == "English\ttext\nline\n中文文案"
    assert result.source_name == "script.docx"
    assert len(result.sha256) == 64
    assert result.warnings == ["external_document_links_ignored"]


@pytest.mark.parametrize(
    ("content", "filename", "mime", "code"),
    [
        (b"not a zip", "script.docx", DOCX_MIME, "invalid_docx_signature"),
        (docx(), "script.exe", DOCX_MIME, "docx_file_required"),
        (docx(), "script.docx", "application/msword", "docx_file_required"),
        (
            docx(extras={"../outside.xml": b"x"}),
            "script.docx",
            DOCX_MIME,
            "unsafe_document_archive_path",
        ),
        (
            docx(extras={"word/vbaProject.bin": b"x"}),
            "script.docx",
            DOCX_MIME,
            "active_document_content_rejected",
        ),
        (
            docx(extras={"word/embeddings/object.ole": b"x"}),
            "script.docx",
            DOCX_MIME,
            "active_document_content_rejected",
        ),
        (docx(""), "script.docx", DOCX_MIME, "document_has_no_script_text"),
        (docx("x" * 100_001), "script.docx", DOCX_MIME, "script_text_too_long"),
        (
            docx(
                document=b'<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><x>&secret;</x>'
            ),
            "script.docx",
            DOCX_MIME,
            "document_xml_rejected",
        ),
        (
            docx(extras={"huge.txt": b"x" * (8 * 1024**2 + 1)}),
            "script.docx",
            DOCX_MIME,
            "document_decompression_limit",
        ),
    ],
)
def test_docx_rejects_unsafe_packages(content, filename, mime, code):
    with pytest.raises(IngestionError) as error:
        parse_docx(content, filename, mime)
    assert error.value.code == code


def test_docx_rejects_zip64_before_constructing_members(monkeypatch):
    content = docx()
    footer = content.rfind(b"PK\x05\x06")
    # A forged locator overrides the ordinary EOCD when ZipFile opens it.
    hostile = content[:footer] + b"PK\x06\x07" + bytes(16) + content[footer:]
    monkeypatch.setattr(zipfile, "ZipFile", lambda *_a, **_kw: pytest.fail("ZIP64 reached parser"))
    with pytest.raises(IngestionError, match="document_archive_too_complex"):
        parse_docx(hostile, "script.docx", DOCX_MIME)


def test_docx_preflights_directory_and_upload_size(monkeypatch):
    content = bytearray(docx())
    footer = content.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", content, footer + 8, 257, 257)
    monkeypatch.setattr(
        zipfile, "ZipFile", lambda *_a, **_kw: pytest.fail("Entry bomb reached parser")
    )
    with pytest.raises(IngestionError, match="document_archive_too_complex"):
        parse_docx(bytes(content), "script.docx", DOCX_MIME)
    with pytest.raises(IngestionError) as error:
        parse_docx(b"x" * (MAX_UPLOAD_BYTES + 1), "script.docx", DOCX_MIME)
    assert error.value.status == 413


def test_reference_subprocess_canonicalizes_and_strips_metadata():
    source = image_bytes(metadata=True)
    canonical = canonicalize_reference(source, "ref.png", "image/png")
    assert canonical.mime_type == "image/jpeg"
    assert (canonical.width, canonical.height) == (320, 300)
    assert b"private metadata" not in canonical.data
    with Image.open(io.BytesIO(canonical.data)) as decoded:
        assert decoded.size == (320, 300) and not decoded.getexif()
    alpha = canonicalize_reference(image_bytes(mode="RGBA"), "ref.png", "image/png")
    assert alpha.mime_type == "image/png" and alpha.extension == "png"


@pytest.mark.parametrize(
    ("data", "name", "mime", "code"),
    [
        (image_bytes(), "ref.svg", "image/svg+xml", "reference_image_type_rejected"),
        (b"not an image", "ref.png", "image/png", "reference_image_signature_rejected"),
        (image_bytes((10, 10)), "ref.png", "image/png", "image_dimensions_rejected"),
        (b"\x89PNG\r\n\x1a\ninvalid", "ref.png", "image/png", "image_decode_rejected"),
    ],
)
def test_reference_rejects_invalid_inputs(data, name, mime, code):
    with pytest.raises(IngestionError, match=code):
        canonicalize_reference(data, name, mime)


def test_reference_rejects_animation():
    buffer = io.BytesIO()
    a = Image.new("RGB", (300, 300), "red")
    b = Image.new("RGB", (300, 300), "blue")
    a.save(buffer, "PNG", save_all=True, append_images=[b], duration=100, loop=0)
    with pytest.raises(IngestionError, match="animated_image_rejected"):
        canonicalize_reference(buffer.getvalue(), "ref.png", "image/png")


def test_reference_timeout_has_no_secret_environment(monkeypatch):
    monkeypatch.setenv("VIDEO_OPENAI_API_KEY", "must-not-reach-decoder")

    def timeout(args, **kwargs):
        assert "VIDEO_OPENAI_API_KEY" not in kwargs["env"]
        assert kwargs["timeout"] == 15 and "shell" not in kwargs
        raise subprocess.TimeoutExpired(args, 15)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(IngestionError, match="reference_processing_timeout"):
        canonicalize_reference(image_bytes(), "ref.png", "image/png")


@pytest.fixture
def imports(api, login, tmp_path):
    login()
    api.app.state.settings = api.settings.model_copy(
        update={
            "storage_dir": tmp_path / "media",
            "storage_min_free_bytes": 0,
            "storage_max_bytes": 1024 * 1024,
        }
    )
    response = api.client.post("/api/v1/projects", json={"name": "Imports"}, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return f"/api/v1/projects/{response.json()['id']}"


def test_docx_api_retains_only_script_and_revision(api, imports, tmp_path):
    response = api.client.post(
        imports + "/imports/docx",
        files={"file": ("script.docx", docx("中文脚本"), DOCX_MIME)},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["text"] == "中文脚本" and body["kind"] == "docx"
    board = api.client.get(imports + "/storyboard").json()
    assert board["input_id"] == body["id"] and board["revision"] == 1
    with api.sessions() as db:
        assert db.scalar(select(ProjectInput)).sha256 == body["sha256"]
        assert db.scalar(select(MediaAsset)) is None
    assert not list(tmp_path.rglob("*.docx"))


def test_reference_api_is_private_and_canonical(api, imports, login):
    response = api.client.post(
        imports + "/assets/reference-images",
        files={"file": ("ref.png", image_bytes(metadata=True), "image/png")},
        data={"caption": "蓝色天空"},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    asset = response.json()
    assert asset["metadata_json"] == {"width": 320, "height": 300, "caption": "蓝色天空"}
    assert "object_key" not in asset and "original_sha256" not in asset["metadata_json"]
    preview = api.client.get(asset["preview_url"])
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"
    assert b"private metadata" not in preview.content
    assert api.client.get(imports + "/assets/reference-images").json() == [asset]
    login("other")
    assert api.client.get(asset["preview_url"]).status_code == 404
    assert api.client.get(imports + "/assets/reference-images").status_code == 404
    login("viewer")
    assert (
        api.client.post(
            imports + "/assets/reference-images",
            files={"file": ("ref.png", image_bytes(), "image/png")},
            headers=ORIGIN,
        ).status_code
        == 403
    )


def test_import_multipart_bounds_and_unicode_script(api, imports):
    assert (
        api.client.post(
            imports + "/imports/docx",
            files={"file": ("x.docx", docx(), DOCX_MIME)},
            data={"unexpected": "x"},
            headers=ORIGIN,
        ).status_code
        == 400
    )
    assert (
        api.client.post(
            imports + "/imports/docx",
            files=[
                ("file", ("x.docx", docx(), DOCX_MIME)),
                ("file", ("y.docx", docx(), DOCX_MIME)),
            ],
            headers=ORIGIN,
        ).status_code
        == 400
    )
    text = "中文" * 50_000
    response = api.client.post(
        imports + "/inputs", json={"kind": "script", "text": text}, headers=ORIGIN
    )
    assert response.status_code == 201, response.text
    assert len(response.json()["text"]) == 100_000
    response = api.client.post(
        imports + "/inputs",
        content=b" " * (1024**2 + 1),
        headers={**ORIGIN, "Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_reference_quota_and_symlinks(api, imports, tmp_path):
    settings = api.app.state.settings
    canonical = canonicalize_reference(image_bytes(), "x.png", "image/png")
    project_id = UUID(imports.split("/")[-1])
    key = store_reference(canonical, project_id, uuid4(), settings)
    assert reference_path(settings, key).is_file()
    limited = settings.model_copy(update={"storage_max_bytes": 1})
    with pytest.raises(StorageError, match="storage_quota_exceeded"):
        store_reference(canonical, project_id, uuid4(), limited)
    outside = tmp_path / "outside"
    outside.mkdir()
    other_id = uuid4()
    (settings.storage_dir / str(other_id)).symlink_to(outside, target_is_directory=True)
    with pytest.raises(StorageError, match="unsafe_reference_path"):
        store_reference(canonical, other_id, uuid4(), settings)
    assert not (outside / "references").exists()
    with pytest.raises(StorageError, match="invalid_reference_key"):
        reference_path(settings, "../../etc/passwd")


def test_large_request_concurrency_fails_closed():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def app(scope, receive, send):
            entered.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = BoundedBodyMiddleware(app)
        scope = {
            "type": "http",
            "method": "POST",
            "path": f"/api/v1/projects/{uuid4()}/inputs",
            "headers": [],
        }

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def discard(message):
            pass

        first = asyncio.create_task(middleware(scope, receive, discard))
        await entered.wait()
        entered.clear()
        second = asyncio.create_task(middleware(scope, receive, discard))
        await entered.wait()
        messages = []

        async def capture(message):
            messages.append(message)

        await middleware(scope, receive, capture)
        assert messages[0]["status"] == 429
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())


def test_director_configuration_is_safe_and_disabled(api, login):
    assert api.client.get("/api/v1/director-config").status_code == 401
    login()
    response = api.client.get("/api/v1/director-config")
    assert response.json() == {
        "director_enabled": False,
        "provider_configured": False,
        "requires_operator_approval": True,
        "model": None,
    }
    assert "key" not in response.text.lower()
