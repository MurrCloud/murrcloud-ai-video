"""Isolated, non-network image decoder. Input and output are bounded binary streams."""

import io
import json
import resource
import sys
import warnings


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (6, 6))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (192 * 1024**2, 192 * 1024**2))
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 8_388_608
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    data = sys.stdin.buffer.read(5 * 1024**2 + 1)
    if len(data) > 5 * 1024**2:
        raise ValueError
    with Image.open(io.BytesIO(data), formats=["JPEG", "PNG", "WEBP"]) as source:
        expected = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
        if source.format != expected.get(sys.argv[1]):
            raise ValueError
        if getattr(source, "n_frames", 1) != 1:
            return fail("animated_image_rejected")
        if (
            min(source.size) < 300
            or max(source.size) > 8192
            or source.width * source.height > Image.MAX_IMAGE_PIXELS
        ):
            return fail("image_dimensions_rejected")
        source.verify()
    with Image.open(io.BytesIO(data), formats=["JPEG", "PNG", "WEBP"]) as source:
        source.load()
        with ImageOps.exif_transpose(source) as oriented:
            alpha = "A" in oriented.getbands() or "transparency" in oriented.info
            with oriented.convert("RGBA" if alpha else "RGB") as canonical:
                canonical.info.clear()
                destination = io.BytesIO()
                if alpha:
                    canonical.save(destination, "PNG", compress_level=1)
                    mime = "image/png"
                else:
                    canonical.save(destination, "JPEG", quality=92, optimize=False)
                    mime = "image/jpeg"
                body = destination.getvalue()
                if len(body) > 20 * 1024**2:
                    return fail("canonical_image_too_large")
                metadata = {"mime_type": mime, "width": canonical.width, "height": canonical.height}
    sys.stdout.buffer.write(json.dumps(metadata).encode() + b"\n" + body)
    return 0


def fail(code):
    sys.stdout.write(json.dumps({"error": code}) + "\n")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(fail("image_decode_rejected"))
