"""Explicit, hash-guarded deployment. No payouts or Telegram messages."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


async def backup_finances(target, backup):
    sys.path.insert(0, str(target))
    import config
    import asyncpg
    conn = await asyncpg.connect(config.DATABASE_URL, statement_cache_size=0, timeout=30)
    try:
        async with conn.transaction(isolation='repeatable_read', readonly=True):
            data = {'captured_at': datetime.now(timezone.utc).isoformat(), 'tables': {}}
            for table in ('referral_earnings','referral_withdrawals','partner_earnings','partner_withdrawals','partnerships'):
                data['tables'][table] = [dict(row) for row in await conn.fetch(f'SELECT * FROM {table}')]
            data['referrer_flags'] = [dict(row) for row in await conn.fetch('SELECT tg_id,referrer_id,first_payment FROM users')]
            (backup / 'referral-finances.json').write_text(json.dumps(data, default=str, ensure_ascii=False))
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('target', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    target = args.target.resolve()
    source = Path(__file__).resolve().parents[2]
    manifest = json.loads((source / 'ops/referral-upgrade/manifest.json').read_text())
    for entry in manifest:
        name = entry['path']
        if name.startswith('/') or '..' in Path(name).parts or '.env' in Path(name).parts:
            raise RuntimeError('Unsafe path in manifest')
        if digest(source / name) != entry['new_sha256']:
            raise RuntimeError(f'Package checksum mismatch: {name}')
        if digest(target / name) != entry['base_sha256']:
            raise RuntimeError(f'Production file differs from reviewed baseline: {name}')
        if name.endswith('.py'):
            compile((source / name).read_bytes(), name, 'exec')
    print(f'CHECKED {len(manifest)} files; .env, venv and unrelated files excluded.', flush=True)
    if not args.apply:
        return
    status = subprocess.run(['systemctl','is-active','spn-bot'], capture_output=True, text=True).stdout.strip()
    if status != 'inactive':
        raise RuntimeError('Stop spn-bot gracefully before applying; status='+status)
    backup = Path(tempfile.mkdtemp(prefix='spn-before-referral-', dir='/root'))
    with tarfile.open(backup / 'files.tar.gz', 'w:gz') as archive:
        for entry in manifest:
            if entry['base_sha256']:
                archive.add(target / entry['path'], arcname=entry['path'])
    shutil.copy2(source / 'ops/referral-upgrade/manifest.json', backup / 'manifest.json')
    asyncio.run(backup_finances(target, backup))
    print('BACKUP', backup, flush=True)
    # Validation and backups are complete before touching application files.
    for entry in manifest:
        destination = target / entry['path']
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + '.referral-new')
        shutil.copy2(source / entry['path'], temporary)
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    print('INSTALLED. Database contents and .env unchanged. Start spn-bot to add outbox tables.', flush=True)


if __name__ == '__main__':
    main()
