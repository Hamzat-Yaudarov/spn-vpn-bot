"""Offline project-bound tenant Mini App identity; no Telegram or database I/O."""

import base64
from copy import deepcopy
import time
import unittest
from unittest.mock import AsyncMock

from fastapi import HTTPException, Request

from services.partner_management import ManagementDenied
from services.partner_security import PartnerSettings, TokenVault
from services.partner_store import InvalidProject, PartnerStore, ProjectNotFound
from services.partner_web_auth import create_authenticate, create_tenant_authenticate
from test_partner_web import MANAGER, TENANT, authorization
from test_partner_store import PROJECT_ID as STORE_PROJECT_ID, RecordingConnection, project_row


PROJECT_ID = "7df2b77c-fefd-4ce4-a49d-228eec3a2607"
OTHER_PROJECT_ID = "ad2c8d31-d4d2-4056-9bb2-9eab24f6d68e"
KEY = base64.urlsafe_b64encode(b"w" * 32).decode("ascii")
OWNER = 123
MEMBER = 456
TENANT_BOT_ID = int(TENANT.split(":", 1)[0])
OTHER_TENANT = "300003:" + "C" * 35


def tenant_project(**changes):
    return {
        "id": PROJECT_ID, "owner_tg_id": OWNER, "status": "active",
        "title": "Tenant workspace", "slug": "tenant-workspace",
        "bot": {"id": TENANT_BOT_ID, "username": "TenantBot"},
        "encrypted_token": TokenVault((KEY,)).encrypt(PROJECT_ID, TENANT_BOT_ID, TENANT),
        **changes,
    }


def request(*, actor=OWNER, token=TENANT, path_project=PROJECT_ID,
            workspace_project=PROJECT_ID, query=b"", headers=None, **signed_fields):
    if headers is None:
        headers = [(b"authorization", authorization(actor, token=token, **signed_fields).encode())]
    params = {"workspace_project_id": workspace_project}
    if path_project is not None:
        params["project_id"] = path_project
    return Request({
        "type": "http", "method": "GET", "path": "/workspace",
        "query_string": query, "headers": headers, "path_params": params,
    })


class TenantWorkspaceAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.project = tenant_project()
        self.loader = AsyncMock(return_value=self.project)
        self.management = AsyncMock()
        self.management.authorize.return_value = {
            "project_id": PROJECT_ID, "role": "viewer",
            "permissions": ["analytics", "customers", "orders", "referrals"],
        }
        self.settings = PartnerSettings(True, "partners.example.com", MANAGER, (KEY,))

    def authenticate(self, **changes):
        values = {
            "project_loader": self.loader,
            "management_store": self.management,
        }
        values.update(changes)
        return create_tenant_authenticate(self.settings, **values)

    async def denied(self, req, *, authenticate=None, statuses=(401, 403, 404, 503)):
        with self.assertRaises(HTTPException) as raised:
            await (authenticate or self.authenticate())(req)
        self.assertIn(raised.exception.status_code, statuses)
        for secret in (MANAGER, TENANT, self.project.get("encrypted_token", ""), "PRIVATE-FAILURE"):
            if secret:
                self.assertNotIn(secret, str(raised.exception.detail))
        self.assertIsNone(getattr(req.state, "partner_workspace", None))

    async def test_owner_uses_one_exact_server_loader_and_safe_project_context(self):
        self.project["future_private_credential"] = "PRIVATE-FAILURE"
        req = request()
        original = deepcopy(self.project)
        self.assertEqual(await self.authenticate()(req), OWNER)
        self.loader.assert_awaited_once_with(PROJECT_ID)
        self.management.authorize.assert_not_awaited()
        self.management.record_manager_identity.assert_not_awaited()
        context = req.state.partner_workspace
        self.assertEqual(context["project_id"], PROJECT_ID)
        self.assertEqual(context["actor_id"], OWNER)
        self.assertEqual(context["project"]["role"], "owner")
        self.assertNotIn("encrypted_token", context["project"])
        self.assertNotIn(TENANT, str(context))
        self.assertNotIn(original["encrypted_token"], str(context))
        self.assertNotIn("future_private_credential", context["project"])
        self.assertNotIn("PRIVATE-FAILURE", str(context))
        self.assertEqual(self.project, original)

    async def test_none_default_preserves_owner_access_and_owner_needs_no_member_store(self):
        for kwargs in ({}, {"manager_allowed_user_ids": None}):
            req = request()
            authenticate = self.authenticate(management_store=None, **kwargs)
            self.assertEqual(await authenticate(req), OWNER)

    async def test_member_has_only_current_exact_project_membership_permissions(self):
        req = request(actor=MEMBER)
        self.assertEqual(await self.authenticate()(req), MEMBER)
        self.management.authorize.assert_awaited_once_with(PROJECT_ID, MEMBER, "referrals")
        self.management.record_manager_identity.assert_not_awaited()
        project = req.state.partner_workspace["project"]
        self.assertEqual(project["role"], "viewer")
        self.assertEqual(set(project["permissions"]), {"analytics", "customers", "orders", "referrals"})
        self.assertNotIn("team", project["permissions"])

    async def test_revoked_unknown_member_and_missing_membership_store_fail_closed(self):
        self.management.authorize.side_effect = ManagementDenied("PRIVATE-FAILURE")
        await self.denied(request(actor=MEMBER), statuses=(403, 404))
        self.management.record_manager_identity.assert_not_awaited()
        await self.denied(
            request(actor=MEMBER), authenticate=self.authenticate(management_store=None),
            statuses=(403, 404),
        )

    async def test_membership_is_rechecked_for_every_new_request(self):
        authenticate = self.authenticate()
        self.assertEqual(await authenticate(request(actor=MEMBER)), MEMBER)
        self.management.authorize.side_effect = ManagementDenied("revoked")
        await self.denied(request(actor=MEMBER), authenticate=authenticate, statuses=(403, 404))
        self.assertEqual(self.management.authorize.await_count, 2)

    async def test_membership_response_cannot_select_another_project_or_owner_role(self):
        for changes in ({"project_id": OTHER_PROJECT_ID}, {"role": "owner"}, {"role": "operator"}):
            self.management.authorize.return_value = {
                "project_id": PROJECT_ID, "role": "viewer", "permissions": ["referrals"],
                **changes,
            }
            await self.denied(request(actor=MEMBER), statuses=(403,))
        self.management.record_manager_identity.assert_not_awaited()

    async def test_other_tenant_or_central_token_cannot_sign_this_workspace(self):
        for token in (OTHER_TENANT, MANAGER):
            with self.subTest(token_kind="central" if token == MANAGER else "other-tenant"):
                await self.denied(request(token=token), statuses=(401,))
        self.management.authorize.assert_not_awaited()
        self.management.record_manager_identity.assert_not_awaited()

    async def test_tenant_token_cannot_authorize_central_onboarding(self):
        central = create_authenticate(self.settings, manager_allowed_user_ids=[OWNER])
        await self.denied(request(), authenticate=central, statuses=(401,))
        self.loader.assert_not_awaited()

    async def test_path_and_query_project_cannot_escape_verified_workspace(self):
        for req in (
            request(path_project=OTHER_PROJECT_ID),
            request(query=("project_id=" + OTHER_PROJECT_ID).encode()),
        ):
            with self.subTest(scope=req.scope["path_params"], query=req.scope["query_string"]):
                await self.denied(req, statuses=(403,))
        self.management.authorize.assert_not_awaited()
        self.management.record_manager_identity.assert_not_awaited()

    async def test_matching_project_query_and_path_are_lookup_only(self):
        req = request(query=("project_id=" + PROJECT_ID).encode())
        self.assertEqual(await self.authenticate()(req), OWNER)
        self.loader.assert_awaited_once_with(PROJECT_ID)

    async def test_duplicate_project_selector_is_not_accepted(self):
        query = ("project_id=" + PROJECT_ID + "&project_id=" + PROJECT_ID).encode()
        await self.denied(request(query=query), statuses=(403,))
        self.loader.assert_not_awaited()

    async def test_request_cached_context_cannot_bypass_later_scope_check(self):
        req = request()
        authenticate = self.authenticate()
        self.assertEqual(await authenticate(req), OWNER)
        req.scope["path_params"]["project_id"] = OTHER_PROJECT_ID
        with self.assertRaises(HTTPException) as raised:
            await authenticate(req)
        self.assertEqual(raised.exception.status_code, 403)
        self.assertIsNone(req.state.partner_workspace)

    async def test_loader_cannot_return_another_project_or_inactive_bot(self):
        for changes in (
            {"id": OTHER_PROJECT_ID}, {"status": "draft"}, {"status": "suspended"},
            {"bot": {"id": None, "username": "TenantBot"}},
            {"bot": {"id": True, "username": "TenantBot"}},
            {"bot": {"id": int(MANAGER.split(":", 1)[0]), "username": "ManagerBot"}},
            {"owner_tg_id": True}, {"owner_tg_id": 0}, {"owner_tg_id": "123"},
        ):
            self.loader.return_value = tenant_project(**changes)
            with self.subTest(changes=changes):
                await self.denied(request())
        self.management.authorize.assert_not_awaited()

    async def test_missing_tampered_or_wrong_project_cipher_is_never_public(self):
        other_cipher = TokenVault((KEY,)).encrypt(OTHER_PROJECT_ID, TENANT_BOT_ID, TENANT)
        for cipher in (None, "", "PRIVATE-FAILURE", other_cipher):
            self.loader.return_value = tenant_project(encrypted_token=cipher)
            with self.subTest(cipher_kind=type(cipher).__name__):
                await self.denied(request())
        self.management.authorize.assert_not_awaited()

    async def test_missing_keys_disabled_feature_and_loader_failure_fail_closed(self):
        self.settings = PartnerSettings(True, "partners.example.com", MANAGER)
        await self.denied(request())
        self.settings = PartnerSettings()
        await self.denied(request(), statuses=(404,))
        self.settings = PartnerSettings(True, "partners.example.com", MANAGER, (KEY,))
        for error in (ProjectNotFound("PRIVATE-FAILURE"), RuntimeError("PRIVATE-FAILURE")):
            self.loader.side_effect = error
            await self.denied(request())

    async def test_pilot_allowlist_requires_both_loaded_owner_and_signed_actor(self):
        self.assertEqual(
            await self.authenticate(manager_allowed_user_ids=[OWNER])(request()), OWNER
        )
        await self.denied(
            request(), authenticate=self.authenticate(manager_allowed_user_ids=[]),
            statuses=(403,),
        )
        await self.denied(
            request(actor=MEMBER),
            authenticate=self.authenticate(manager_allowed_user_ids=[OWNER]), statuses=(403,),
        )
        self.loader.return_value = tenant_project(owner_tg_id=999)
        await self.denied(
            request(actor=OWNER),
            authenticate=self.authenticate(manager_allowed_user_ids=[OWNER]), statuses=(403,),
        )
        self.management.authorize.assert_not_awaited()

    async def test_pilot_allows_explicitly_listed_owner_and_member_only(self):
        req = request(actor=MEMBER)
        authenticate = self.authenticate(manager_allowed_user_ids=[OWNER, MEMBER])
        self.assertEqual(await authenticate(req), MEMBER)
        self.management.authorize.assert_awaited_once_with(PROJECT_ID, MEMBER, "referrals")

    async def test_signed_actor_is_strict_integer_and_init_data_is_fresh(self):
        for actor in (True, "123", 0, -1, 2**63):
            with self.subTest(actor=actor):
                await self.denied(request(actor=actor), statuses=(401,))
        for auth_date in (str(int(time.time()) - 7200), str(int(time.time()) + 120)):
            await self.denied(request(auth_date=auth_date), statuses=(401,))
        self.management.authorize.assert_not_awaited()

    async def test_duplicate_oversized_or_missing_authorization_is_not_identity(self):
        signed = authorization(OWNER, token=TENANT).encode()
        for headers in (
            [], [(b"authorization", b"tma invalid")],
            [(b"authorization", signed), (b"authorization", signed)],
            [(b"authorization", signed + b"&auth_date=1")],
            [(b"authorization", b"tma " + b"a" * 16385)],
        ):
            await self.denied(request(headers=headers), statuses=(401,))
        self.management.authorize.assert_not_awaited()

    async def test_unscoped_or_malformed_workspace_never_enumerates_projects(self):
        for workspace in (None, "", "not-a-project"):
            req = request(workspace_project=workspace)
            if workspace is None:
                req.scope["path_params"].pop("workspace_project_id")
            await self.denied(req)
        self.loader.assert_not_awaited()
        self.management.authorize.assert_not_awaited()
        self.management.visible_projects.assert_not_awaited()
        self.management.record_manager_identity.assert_not_awaited()


class WorkspaceCredentialStoreContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_loader_selects_only_exact_active_connected_project(self):
        conn = RecordingConnection(rows=[project_row(status="active", bot_id=TENANT_BOT_ID)])
        result = await PartnerStore(lambda: conn).get_workspace_auth_project(str(STORE_PROJECT_ID))
        self.assertEqual(result["encrypted_token"], "secret-must-never-leak")
        self.assertNotIn("future_private_column", result)
        self.assertEqual(len(conn.calls), 1)
        operation, sql, args = conn.calls[0]
        self.assertEqual(operation, "fetchrow")
        self.assertEqual(args, (STORE_PROJECT_ID,))
        self.assertIn("WHERE id=$1 AND status='active'", sql)
        self.assertIn("bot_id IS NOT NULL", sql)
        self.assertIn("encrypted_token IS NOT NULL", sql)

    async def test_missing_project_has_no_credential_fallback(self):
        conn = RecordingConnection(rows=[None])
        with self.assertRaises(ProjectNotFound):
            await PartnerStore(lambda: conn).get_workspace_auth_project(STORE_PROJECT_ID)
        self.assertEqual(len(conn.calls), 1)

    async def test_invalid_locator_does_not_open_store(self):
        getter = AsyncMock()
        with self.assertRaises(InvalidProject):
            await PartnerStore(getter).get_workspace_auth_project("invalid-project")
        getter.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
