"""Optional isolated PostgreSQL/WASM logo ownership + atomic upload tests."""

import base64
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from test_partner_store_postgres import RegistryBridge
from test_partner_brand_assets import logo_image
from services.partner_store import PartnerStore
from services.partner_brand_assets import (
    BrandAssetConflict,
    BrandAssetNotFound,
    PartnerBrandAssetStore,
)


@unittest.skipUnless(
    os.environ.get("PGLITE_MODULE") and importlib.util.find_spec("PIL"),
    "Optional isolated PostgreSQL/WASM + Pillow are not configured",
)
class BrandPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        self.schema = (
            Path(__file__).resolve().parents[1] / "ops/partner-platform/branding.sql"
        ).read_text()
        await self.sql.query(self.schema, exec=True)
        self.registry = PartnerStore(lambda: self.sql)
        self.project = await self.registry.create_project(
            42, "brand-creation-123", "brand-test", "Бренд"
        )
        self.pid = self.project["id"]
        self.store = PartnerBrandAssetStore(lambda: self.sql, enabled=True)

    async def test_atomic_versioned_upload_roundtrip_and_current_tenant_public_only(
        self,
    ):
        result = await self.store.upload_logo(self.pid, 42, 1, logo_image())
        self.assertEqual(result["project"]["version"], 2)
        self.assertEqual(result["project"]["brand"]["logo_asset_id"], result["logo_id"])
        self.assertEqual(result, await self.store.get_logo(self.pid, 42))
        with self.assertRaises(BrandAssetNotFound):
            await self.store.public_logo("brand-test", result["logo_id"])
        await self.sql.execute(
            "UPDATE wl_projects SET status='active' WHERE id=$1", self.pid
        )
        png = await self.store.public_logo("brand-test", result["logo_id"])
        self.assertEqual(png, base64.b64decode(result["png_base64"]))
        with self.assertRaises(BrandAssetNotFound):
            await self.store.public_logo("other-tenant", result["logo_id"])
        with self.assertRaises(BrandAssetNotFound):
            await self.store.get_logo(self.pid, 99)
        self.assertNotIn("encrypted_token", str(result))

    async def test_replace_keeps_one_current_asset_and_stale_version_does_not_decode(
        self,
    ):
        first = await self.store.upload_logo(self.pid, 42, 1, logo_image())
        with patch(
            "services.partner_brand_assets.normalize_logo",
            side_effect=AssertionError("mustnotdecode"),
        ):
            with self.assertRaises(BrandAssetConflict):
                await self.store.upload_logo(self.pid, 42, 1, "bogus")
            with self.assertRaises(BrandAssetNotFound):
                await self.store.upload_logo(self.pid, 99, 2, "bogus")
        second = await self.store.upload_logo(
            self.pid, 42, 2, logo_image(color=(0, 0, 255))
        )
        self.assertNotEqual(first["logo_id"], second["logo_id"])
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_brand_assets"), 1
        )
        await self.sql.execute(
            "UPDATE wl_projects SET status='active' WHERE id=$1", self.pid
        )
        with self.assertRaises(BrandAssetNotFound):
            await self.store.public_logo("brand-test", first["logo_id"])

    async def test_concurrent_setting_change_after_decode_rechecked_under_project_lock(
        self,
    ):
        import services.partner_brand_assets as module

        actual = module.normalize_logo

        # The decoder itself runs in a thread; an already decoded logo is not
        # enough to authorize overriding a later settings version.
        async def mutate_and_decode(*_args, **_kwargs):
            await self.sql.execute(
                "UPDATE wl_projects SET version=version+1 WHERE id=$1", self.pid
            )
            return actual(logo_image())

        with patch(
            "services.partner_brand_assets.asyncio.to_thread",
            side_effect=mutate_and_decode,
        ):
            with self.assertRaises(BrandAssetConflict):
                await self.store.upload_logo(self.pid, 42, 1, logo_image())
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_brand_assets"), 0
        )

    async def test_audit_failure_rolls_back_pixels_brand_and_version(self):
        original = self.sql.execute

        async def fail(sql, *args):
            if "INSERT INTO wl_brand_asset_audit" in sql:
                raise RuntimeError("injected audit failure")
            return await original(sql, *args)

        self.sql.execute = fail
        with self.assertRaisesRegex(RuntimeError, "injected"):
            await self.store.upload_logo(self.pid, 42, 1, logo_image())
        self.sql.execute = original
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_brand_assets"), 0
        )
        result = await self.store.get_logo(self.pid, 42)
        self.assertEqual(result["project"]["version"], 1)
        self.assertIsNone(result["logo_id"])

    async def test_delete_versioned_and_migration_repeat_preserves_brand(self):
        first = await self.store.upload_logo(self.pid, 42, 1, logo_image())
        await self.sql.query(self.schema, exec=True)
        self.assertEqual(first, await self.store.get_logo(self.pid, 42))
        result = await self.store.delete_logo(self.pid, 42, 2)
        self.assertEqual(result["project"]["version"], 3)
        self.assertIsNone(result["logo_id"])
        self.assertNotIn("logo_asset_id", result["project"]["brand"])
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_brand_assets"), 0
        )
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_brand_asset_audit"), 2
        )
