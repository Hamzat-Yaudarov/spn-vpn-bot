"""Offline bounded pixel processing; Pillow loaded only for opted-in tests."""

import base64
import importlib.util
from io import BytesIO
import struct
import unittest
from uuid import uuid4
import zlib

from fastapi import FastAPI, HTTPException
import httpx

from partner_brand_web import create_router
from services.partner_brand_assets import (
    InvalidBrandAsset,
    MAX_LOGO_BYTES,
    PartnerBrandAssetStore,
    BrandAssetUnavailable,
    normalize_logo,
)


HAS_PIL = importlib.util.find_spec("PIL") is not None


def logo_image(*, fmt="PNG", size=(128, 64), color=(255, 10, 0), **kwargs):
    from PIL import Image

    output = BytesIO()
    Image.new("RGB", size, color).save(output, format=fmt, **kwargs)
    return base64.b64encode(output.getvalue()).decode()


class BrandValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_never_reads_or_decodes(self):
        def forbidden():
            raise AssertionError("no IO")

        store = PartnerBrandAssetStore(forbidden)
        with self.assertRaises(BrandAssetUnavailable):
            await store.upload_logo(str(uuid4()), 42, 1, "a")

    async def test_nonimage_svg_gif_and_oversized_rejected_before_decoder(self):
        for raw in (
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>',
            b"GIF89a" + b"x" * 40,
            b"<html>fakePNG</html>",
            b"x" * (MAX_LOGO_BYTES + 1),
        ):
            with self.assertRaises(InvalidBrandAsset):
                normalize_logo(base64.b64encode(raw).decode())
        for value in (None, True, "data:image/png;base64,AAAA", "not base64 !!"):
            with self.assertRaises(InvalidBrandAsset):
                normalize_logo(value)

    async def test_huge_png_dimensions_rejected_before_decoder(self):
        data = struct.pack(">IIBBBBB", 100000, 100000, 8, 6, 0, 0, 0)
        raw = (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + data
            + struct.pack(">I", zlib.crc32(b"IHDR" + data))
        )
        with self.assertRaises(InvalidBrandAsset):
            normalize_logo(base64.b64encode(raw).decode())


@unittest.skipUnless(HAS_PIL, "Optional Pillow test runtime is not configured")
class BrandPixelTests(unittest.TestCase):
    def test_png_jpeg_reencoded_fixed_canvas_and_only_pixels_retained(self):
        from PIL import Image

        for fmt in ("PNG", "JPEG"):
            result = normalize_logo(logo_image(fmt=fmt))
            with Image.open(BytesIO(result)) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.size, (256, 256))
                self.assertEqual(image.mode, "RGBA")
                self.assertEqual(image.info, {})
                self.assertEqual(image.getpixel((0, 0))[3], 0)
                self.assertGreater(image.getpixel((128, 128))[3], 0)

    def test_png_metadata_is_stripped_before_decompression(self):
        from PIL import Image
        from PIL.PngImagePlugin import PngInfo

        info = PngInfo()
        info.add_text("private", "LOCATION +79991234567" * 10000, zip=True)
        raw = logo_image(pnginfo=info)
        result = normalize_logo(raw)
        self.assertNotIn(b"LOCATION", result)
        self.assertNotIn(b"zTXt", result)
        with Image.open(BytesIO(result)) as image:
            self.assertEqual(image.info, {})

    def test_animation_large_dimension_and_broken_crc_rejected(self):
        from PIL import Image

        output = BytesIO()
        Image.new("RGB", (10, 10), "red").save(
            output,
            format="PNG",
            save_all=True,
            append_images=[Image.new("RGB", (10, 10), "blue")],
            duration=100,
            loop=0,
        )
        for data in (
            base64.b64encode(output.getvalue()).decode(),
            logo_image(size=(1025, 1)),
            logo_image(fmt="JPEG", size=(1, 1025)),
        ):
            with self.assertRaises(InvalidBrandAsset):
                normalize_logo(data)
        raw = bytearray(base64.b64decode(logo_image()))
        raw[32] ^= 0xFF
        with self.assertRaises(InvalidBrandAsset):
            normalize_logo(base64.b64encode(raw).decode())

    def test_jpeg_exif_and_trailing_html_not_in_output(self):
        from PIL import Image

        exif = Image.Exif()
        exif[270] = "private location"
        exif[274] = 6
        encoded = logo_image(fmt="JPEG", exif=exif)
        raw = base64.b64decode(encoded) + b"<script>private location</script>"
        result = normalize_logo(base64.b64encode(raw).decode())
        self.assertNotIn(b"script", result)
        self.assertNotIn(b"private", result)
        with Image.open(BytesIO(result)) as image:
            self.assertFalse(image.getexif())


class FakeStore:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def call(*args):
            self.calls.append((name, args))
            return {
                "project": {"id": args[0], "version": 2},
                "logo_id": None,
                "png_base64": None,
            }

        return call


class BrandHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = FakeStore()

        async def auth(request):
            if request.headers.get("x-test-auth") != "owner":
                raise HTTPException(401, "manager auth only")
            return 42

        app = FastAPI()
        app.include_router(create_router(self.store, authenticate=auth, enabled=True))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://manager.test"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.path = f"/partners/api/projects/{uuid4()}/logo"

    async def test_auth_safe_headers_and_unknown_fields(self):
        result = await self.client.get(self.path)
        self.assertEqual(result.status_code, 401)
        self.assertIn("no-store", result.headers["cache-control"])
        self.assertEqual(self.store.calls, [])
        body = {"expected_version": 1, "image_base64": "AAAA"}
        result = await self.client.post(
            self.path, headers={"x-test-auth": "owner"}, json=body
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.store.calls[0][1][1], 42)
        result = await self.client.post(
            self.path, headers={"x-test-auth": "owner"}, json=body | {"owner_id": 99}
        )
        self.assertEqual(result.status_code, 400)

    async def test_upload_limit_and_duplicate_fields(self):
        headers = {"x-test-auth": "owner", "content-type": "application/json"}
        result = await self.client.post(
            self.path,
            headers=headers,
            content=b'{"image_base64":"' + b"A" * 710000 + b'","expected_version":1}',
        )
        self.assertEqual(result.status_code, 413)
        result = await self.client.post(
            self.path,
            headers=headers,
            content=b'{"expected_version":1,"expected_version":2,"image_base64":"x"}',
        )
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.store.calls, [])
