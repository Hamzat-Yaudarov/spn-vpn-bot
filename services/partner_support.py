"""Explicit platform support, never a tenant owner impersonation port.

Only central-manager authenticated operators may call these methods. Operator
membership is repeated at the store boundary. No credentials, service grants,
payments, team changes or outbound customer messages are exposed here.
"""

import json

from services.partner_manager_access import normalize_manager_allowed_user_ids
from services.partner_store import (
    InvalidProject, ProjectConflict, ProjectNotFound, _PROJECT_COLUMNS,
    _PROJECT_SELECT, _positive_int, _project_id, _public, validate_changes,
)


class PartnerSupportStore:
    def __init__(self, pool_getter, *, operator_ids=()):
        if not callable(pool_getter):
            raise TypeError("pool_getter must be callable")
        self._pool_getter = pool_getter
        self.operator_ids = normalize_manager_allowed_user_ids(operator_ids) or ()

    def require_operator(self, actor_id):
        actor = _positive_int(actor_id, "actor_id")
        if actor not in self.operator_ids:
            raise ProjectNotFound("Раздел недоступен.")
        return actor

    async def _pool(self):
        import inspect
        value = self._pool_getter()
        return await value if inspect.isawaitable(value) else value

    async def list_projects(self, actor_id, *, limit=50, cursor=0):
        self.require_operator(actor_id)
        if (type(limit) is not int or not 1 <= limit <= 100
                or type(cursor) is not int or not 0 <= cursor <= 10000):
            raise InvalidProject("Некорректная страница.")
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                _PROJECT_SELECT + " ORDER BY created_at DESC,id LIMIT $1 OFFSET $2",
                limit + 1, cursor,
            )
        return {"items": [_public(row) for row in rows[:limit]],
                "truncated": len(rows) > limit, "cursor": cursor}

    async def get_project(self, project_id, actor_id):
        self.require_operator(actor_id)
        pid = _project_id(project_id)
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_PROJECT_SELECT + " WHERE id=$1", pid)
        if row is None:
            raise ProjectNotFound("Проект не найден.")
        return _public(row)

    async def update_project(self, project_id, actor_id, expected_version, changes):
        actor = self.require_operator(actor_id)
        pid = _project_id(project_id)
        version = _positive_int(expected_version, "version", maximum=2**31 - 2)
        changes = validate_changes(changes)
        fields = sorted(changes)
        values = [json.dumps(changes[key], ensure_ascii=False) if key == "brand"
                  else changes[key] for key in fields]
        # Column names come only from validate_changes' fixed allowlist.
        assignments = ", ".join(f"{key}=${index + 3}" + ("::jsonb" if key == "brand" else "")
                                for index, key in enumerate(fields))
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"UPDATE wl_projects SET {assignments}, version=version+1, updated_at=now() "
                    f"WHERE id=$1 AND version=$2 RETURNING {_PROJECT_COLUMNS}",
                    pid, version, *values,
                )
                if row is None:
                    exists = await conn.fetchval("SELECT 1 FROM wl_projects WHERE id=$1", pid)
                    if not exists:
                        raise ProjectNotFound("Проект не найден.")
                    raise ProjectConflict("Настройки изменились. Обновите страницу.")
                # Keep the real owner FK; record the REAL support actor explicitly.
                # Audit and settings commit together, or neither commits.
                await conn.execute(
                    """INSERT INTO wl_project_audit
                    (project_id,owner_tg_id,action,project_version,details)
                    VALUES($1,$2,'settings_updated',$3,$4::jsonb)""",
                    pid, row["owner_tg_id"], row["version"],
                    json.dumps({"fields": fields, "actor_tg_id": actor,
                                "context": "platform_support"}, separators=(",", ":")),
                )
        return _public(row)
