"""Explicit private data backup before the approved owner-entry pilot migration.

Run on the named VPS from its existing release. No SQL writes or service changes.
Only partner public tables are exported; secrets/rows never go to stdout.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

RELEASE = Path('/opt/spn-franchise-staging/releases/20261021-5fab5be5cb97')
CONFIG = Path('/etc/spn-partner-private/staging.json')
BACKUP = Path('/etc/spn-partner-private/before-owner-entry-pilot-20261022.json')


async def backup():
    if os.geteuid() != 0 or sys.platform != 'linux':
        raise ValueError()
    if CONFIG.parent.is_symlink() or stat.S_IMODE(CONFIG.parent.stat().st_mode) != 0o700:
        raise ValueError()
    sys.path.insert(0, str(RELEASE))
    from scripts.partner_runtime import load_config, open_pool
    settings, _ = load_config(CONFIG)
    if (settings.database_project_ref != 'crrmaqiakrqpfvhiwtkq'
            or settings.manager_allowed_user_ids != (6910097562,)
            or settings.enable_billing or settings.enable_launches or settings.enable_worker):
        raise ValueError()
    data = {'format': 1, 'project_ref': settings.database_project_ref, 'tables': {}}
    async with open_pool(settings) as pool:
        async with pool.acquire() as conn:
            async with conn.transaction(isolation='repeatable_read', readonly=True):
                tables = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
                if len(tables) != 31 or any(not re.fullmatch(r'wl_[a-z_]+', row['tablename']) for row in tables):
                    raise ValueError()
                for row in tables:
                    name = row['tablename']
                    data['tables'][name] = [dict(item) for item in await conn.fetch('SELECT * FROM public."' + name + '"')]
    blob = (json.dumps(data, default=str, sort_keys=True) + '\n').encode()
    if len(blob) > 20_000_000:
        raise ValueError()
    fd = os.open(BACKUP, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    print(json.dumps({'backup_saved': True, 'sha256': hashlib.sha256(blob).hexdigest(),
                      'table_counts': {key: len(value) for key, value in data['tables'].items()}}))


if __name__ == '__main__':
    try:
        asyncio.run(backup())
    except Exception:
        print('{"backup_saved": false, "error": "private_backup_failed"}')
        sys.exit(1)
