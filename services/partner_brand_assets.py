"""Bounded, owner-scoped logo uploads; no SVG, remote URLs, paths or startup I/O.

Only decoded pixels enter a fresh 256x256 PNG. One current image is retained per
project. Pillow is imported lazily so a disabled platform has no new import
dependency. Explicit versioned ownership is rechecked after the threaded decode.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from datetime import datetime, timezone
import hashlib
import inspect
from io import BytesIO
import json
import re
from uuid import UUID

from services.partner_catalog import validate_slug
from services.partner_store import _PROJECT_COLUMNS, _public

MAX_LOGO_BYTES = 512 * 1024
MAX_LOGO_BASE64 = ((MAX_LOGO_BYTES + 2) // 3) * 4
MAX_LOGO_EDGE = 1024
LOGO_EDGE = 256
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class InvalidBrandAsset(ValueError):
    pass


class BrandAssetConflict(RuntimeError):
    pass


class BrandAssetNotFound(LookupError):
    pass


class BrandAssetUnavailable(RuntimeError):
    pass


def _uuid(value):
    try:
        if not isinstance(value, (str, UUID)):
            raise ValueError()
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise InvalidBrandAsset("Некорректный проект.") from None


def _owner(value):
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise InvalidBrandAsset("Некорректный пользователь.")
    return value


def _version(value):
    if type(value) is not int or not 1 <= value <= 2_147_483_646:
        raise InvalidBrandAsset("Обновите настройки перед загрузкой логотипа.")
    return value


def _png_without_metadata(raw):
    """Drop PNG text/ICC/EXIF *before* Pillow can decompress ancillary chunks."""
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return raw
    output = bytearray(raw[:8])
    position, count, seen_ihdr = 8, 0, False
    while position < len(raw):
        count += 1
        if count > 2048 or position + 12 > len(raw):
            raise InvalidBrandAsset("Повреждённый PNG-файл.")
        length = int.from_bytes(raw[position : position + 4], "big")
        end = position + 12 + length
        if end > len(raw):
            raise InvalidBrandAsset("Повреждённый PNG-файл.")
        kind = raw[position + 4 : position + 8]
        if kind == b"IHDR":
            if seen_ihdr or position != 8 or length != 13:
                raise InvalidBrandAsset("Повреждённый PNG-файл.")
            seen_ihdr = True
            width = int.from_bytes(raw[position + 8 : position + 12], "big")
            height = int.from_bytes(raw[position + 12 : position + 16], "big")
            if not 1 <= width <= MAX_LOGO_EDGE or not 1 <= height <= MAX_LOGO_EDGE:
                raise InvalidBrandAsset(
                    "Логотип должен быть не больше 1024×1024 пикселей."
                )
        elif not seen_ihdr:
            raise InvalidBrandAsset("Повреждённый PNG-файл.")
        if kind in {b"acTL", b"fcTL", b"fdAT"}:
            raise InvalidBrandAsset("Анимированные изображения не поддерживаются.")
        if kind in {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS"}:
            output.extend(raw[position:end])
        elif kind and kind[0] & 32 == 0:
            raise InvalidBrandAsset("Неизвестный формат PNG.")
        position = end
        if kind == b"IEND":
            return bytes(output)
    raise InvalidBrandAsset("Незавершённый PNG-файл.")


def normalize_logo(image_base64):
    if (
        not isinstance(image_base64, str)
        or not 1 <= len(image_base64) <= MAX_LOGO_BASE64
    ):
        raise InvalidBrandAsset("Нужен PNG или JPEG размером до 512 КБ.")
    try:
        raw = base64.b64decode(image_base64, validate=True)
    except (ValueError, binascii.Error):
        raise InvalidBrandAsset("Некорректный файл логотипа.") from None
    if not 1 <= len(raw) <= MAX_LOGO_BYTES:
        raise InvalidBrandAsset("Нужен PNG или JPEG размером до 512 КБ.")
    if not (raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff")):
        raise InvalidBrandAsset("Поддерживаются только PNG и JPEG, без SVG и анимации.")
    raw = _png_without_metadata(raw)
    try:
        import PIL
        from PIL import Image, ImageOps
    except ImportError:
        raise BrandAssetUnavailable(
            "Обработка логотипов не установлена оператором."
        ) from None
    try:
        # The supported major release includes current decoder security fixes.
        if int(PIL.__version__.split(".", 1)[0]) < 13:
            raise BrandAssetUnavailable(
                "Нужно обновить защищённую обработку изображений."
            )
        with Image.open(BytesIO(raw), formats=["PNG", "JPEG"]) as image:
            if (
                image.format not in {"PNG", "JPEG"}
                or getattr(image, "n_frames", 1) != 1
            ):
                raise InvalidBrandAsset("Поддерживаются только неподвижные PNG и JPEG.")
            if (
                not 1 <= image.width <= MAX_LOGO_EDGE
                or not 1 <= image.height <= MAX_LOGO_EDGE
            ):
                raise InvalidBrandAsset(
                    "Логотип должен быть не больше 1024×1024 пикселей."
                )
            image.verify()
        with Image.open(BytesIO(raw), formats=["PNG", "JPEG"]) as image:
            oriented = ImageOps.exif_transpose(image)
            pixels = oriented.convert("RGBA")
            pixels = ImageOps.contain(
                pixels, (LOGO_EDGE, LOGO_EDGE), method=Image.Resampling.LANCZOS
            )
            fresh = Image.new("RGBA", (LOGO_EDGE, LOGO_EDGE), (0, 0, 0, 0))
            fresh.paste(
                pixels,
                ((LOGO_EDGE - pixels.width) // 2, (LOGO_EDGE - pixels.height) // 2),
            )
            output = BytesIO()
            fresh.save(output, format="PNG", compress_level=6)
            result = output.getvalue()
            if not 32 <= len(result) <= 270000:
                raise InvalidBrandAsset("Не удалось подготовить логотип.")
            return result
    except (BrandAssetUnavailable, InvalidBrandAsset):
        raise
    except Exception:
        raise InvalidBrandAsset(
            "Не удалось прочитать PNG или JPEG. Попробуйте другой файл."
        ) from None


class PartnerBrandAssetStore:
    def __init__(self, pool_getter, *, enabled=False):
        if not callable(pool_getter) or type(enabled) is not bool:
            raise TypeError("Logo store requires explicit configuration")
        self._pool_getter, self.enabled = pool_getter, enabled
        self._decode_slots = asyncio.Semaphore(2)

    def _enabled(self):
        if not self.enabled:
            raise BrandAssetUnavailable("Загрузка логотипа пока недоступна.")

    async def _pool(self):
        value = self._pool_getter()
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    async def _project(conn, pid, owner, *, lock=False):
        row = await conn.fetchrow(
            f"SELECT {_PROJECT_COLUMNS} FROM wl_projects WHERE id=$1 AND owner_tg_id=$2"
            + (" FOR UPDATE" if lock else ""),
            pid,
            owner,
        )
        if row is None:
            raise BrandAssetNotFound("Проект не найден.")
        return row

    @staticmethod
    def _result(project, asset=None):
        return {
            "project": _public(project),
            "logo_id": asset["asset_id"] if asset else None,
            "png_base64": (
                base64.b64encode(bytes.fromhex(asset["png_hex"])).decode("ascii")
                if asset
                else None
            ),
        }

    async def get_logo(self, project_id, owner_id):
        self._enabled()
        pid, owner = _uuid(project_id), _owner(owner_id)
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                project = await self._project(conn, pid, owner, lock=True)
                asset = await conn.fetchrow(
                    "SELECT asset_id,encode(png,'hex') AS png_hex FROM wl_brand_assets WHERE project_id=$1",
                    pid,
                )
                return self._result(project, asset)

    async def upload_logo(self, project_id, owner_id, expected_version, image_base64):
        self._enabled()
        pid, owner, version = (
            _uuid(project_id),
            _owner(owner_id),
            _version(expected_version),
        )
        pool = await self._pool()
        # Authorization before decoding; release the DB connection during CPU work.
        async with pool.acquire() as conn:
            project = await self._project(conn, pid, owner)
            if project["version"] != version:
                raise BrandAssetConflict(
                    "Настройки изменились. Обновите проект и загрузите файл ещё раз."
                )
        try:
            await asyncio.wait_for(self._decode_slots.acquire(), timeout=1)
        except TimeoutError:
            raise BrandAssetUnavailable(
                "Обработка логотипов занята. Повторите чуть позже."
            ) from None
        task = asyncio.create_task(asyncio.to_thread(normalize_logo, image_base64))

        # Cancellation/timeout cannot release a slot while its thread still runs.
        def release(completed):
            self._decode_slots.release()
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(release)
        try:
            png = await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except TimeoutError:
            raise BrandAssetUnavailable(
                "Обработка файла заняла слишком много времени."
            ) from None
        digest = hashlib.sha256(png).hexdigest()
        async with pool.acquire() as conn:
            async with conn.transaction():
                project = await self._project(conn, pid, owner, lock=True)
                if project["version"] != version:
                    raise BrandAssetConflict(
                        "Настройки изменились. Обновите проект и загрузите файл ещё раз."
                    )
                brand = project["brand"]
                brand = json.loads(brand) if isinstance(brand, str) else dict(brand)
                brand.pop("logo_url", None)
                brand["logo_asset_id"] = digest
                now = datetime.now(timezone.utc)
                await conn.execute(
                    """INSERT INTO wl_brand_assets(project_id,asset_id,png,created_at,updated_at)
                    VALUES($1,$2,decode($3,'hex'),$4,$4) ON CONFLICT(project_id) DO UPDATE
                    SET asset_id=EXCLUDED.asset_id,png=EXCLUDED.png,updated_at=EXCLUDED.updated_at""",
                    pid,
                    digest,
                    png.hex(),
                    now,
                )
                project = await conn.fetchrow(
                    f"""UPDATE wl_projects SET brand=$3::jsonb,version=version+1,updated_at=$4
                    WHERE id=$1 AND owner_tg_id=$2 RETURNING {_PROJECT_COLUMNS}""",
                    pid,
                    owner,
                    json.dumps(brand),
                    now,
                )
                await conn.execute(
                    """INSERT INTO wl_brand_asset_audit(project_id,owner_tg_id,action,asset_id,project_version,created_at)
                    VALUES($1,$2,'uploaded',$3,$4,$5)""",
                    pid,
                    owner,
                    digest,
                    project["version"],
                    now,
                )
                return self._result(project, {"asset_id": digest, "png_hex": png.hex()})

    async def delete_logo(self, project_id, owner_id, expected_version):
        self._enabled()
        pid, owner, version = (
            _uuid(project_id),
            _owner(owner_id),
            _version(expected_version),
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                project = await self._project(conn, pid, owner, lock=True)
                if project["version"] != version:
                    raise BrandAssetConflict("Настройки изменились. Обновите проект.")
                brand = project["brand"]
                brand = json.loads(brand) if isinstance(brand, str) else dict(brand)
                digest = brand.pop("logo_asset_id", None)
                brand.pop("logo_url", None)
                now = datetime.now(timezone.utc)
                await conn.execute(
                    "DELETE FROM wl_brand_assets WHERE project_id=$1", pid
                )
                project = await conn.fetchrow(
                    f"""UPDATE wl_projects SET brand=$3::jsonb,version=version+1,updated_at=$4
                    WHERE id=$1 AND owner_tg_id=$2 RETURNING {_PROJECT_COLUMNS}""",
                    pid,
                    owner,
                    json.dumps(brand),
                    now,
                )
                await conn.execute(
                    """INSERT INTO wl_brand_asset_audit(project_id,owner_tg_id,action,asset_id,project_version,created_at)
                    VALUES($1,$2,'removed',$3,$4,$5)""",
                    pid,
                    owner,
                    digest,
                    project["version"],
                    now,
                )
                return self._result(project)

    async def public_logo(self, slug, digest):
        self._enabled()
        try:
            slug = validate_slug(slug)
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise ValueError()
        except ValueError:
            raise BrandAssetNotFound("Логотип не найден.") from None
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT encode(a.png,'hex') AS png_hex FROM wl_brand_assets a
                JOIN wl_projects p ON p.id=a.project_id WHERE p.slug=$1 AND p.status='active'
                AND a.asset_id=$2 AND p.brand->>'logo_asset_id'=a.asset_id""",
                slug,
                digest,
            )
        if row is None:
            raise BrandAssetNotFound("Логотип не найден.")
        png = bytes.fromhex(row["png_hex"])
        if hashlib.sha256(png).hexdigest() != digest:
            raise BrandAssetUnavailable("Не удалось проверить логотип.")
        return png
