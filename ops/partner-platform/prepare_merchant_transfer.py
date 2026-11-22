"""Explicit, one-time transfer of the already authorized SPN merchant settings.

Not a runtime adapter and never shipped. Reads ONLY the two selected settings
from the explicitly named local .env, validates the account by GET /me, and
creates one new private transfer file. Does not create payments or webhooks.
"""
import argparse
import asyncio
import io
import json
import os
import base64
import ssl
from pathlib import Path
import stat
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
DESTINATION = Path('/private/tmp/spn-owner-pilot-merchant-20261022.json')


async def prepare(source, merchant_file=None):
    from dotenv import dotenv_values
    from services.partner_yookassa import PartnerYooKassaProvider, PartnerYooKassaSettings

    if merchant_file is not None:
        source = Path(merchant_file).absolute()
        if source != Path('/private/tmp/spn-owner-pilot-merchant-from-live-20261022.json') or source.is_symlink():
            raise ValueError()
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r') as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid() or info.st_size > 4096:
                raise ValueError()
            merchant = json.load(f)
        if set(merchant) != {'shop_id', 'secret_key'}:
            raise ValueError()
    else:
        source = Path(source).absolute()
        if source != ROOT / '.env' or source.is_symlink():
            raise ValueError()
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r') as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1_000_000:
                raise ValueError()
            selected = [line for line in f if line.split('=', 1)[0].strip() in
                        {'YOOKASSA_SHOP_ID', 'YOOKASSA_SECRET_KEY'}]
        values = dotenv_values(stream=io.StringIO(''.join(selected)), interpolate=False)
        merchant = {'shop_id': values.get('YOOKASSA_SHOP_ID'), 'secret_key': values.get('YOOKASSA_SECRET_KEY')}
    provider = PartnerYooKassaProvider(PartnerYooKassaSettings(
        merchant['shop_id'], merchant['secret_key'], 'https://partners.wayspn.online/partners',
        test_mode=False, owner_entry_pilot=True))
    import certifi
    request = urllib.request.Request('https://api.yookassa.ru/v3/me', headers={
        'Authorization': 'Basic ' + base64.b64encode((merchant['shop_id'] + ':' + merchant['secret_key']).encode()).decode(),
        'Accept': 'application/json',
    })
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            raise ValueError()
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(
        context=ssl.create_default_context(cafile=certifi.where())))
    with opener.open(request, timeout=20) as response:
        raw = response.read(131_073)
        if len(raw) > 131_072:
            raise ValueError()
        account = json.loads(raw)
    if (account.get('account_id') != merchant['shop_id'] or account.get('test') is not False
            or account.get('status') != 'enabled' or account.get('fiscalization_enabled') is not False):
        print(json.dumps({'merchant_verified': False, 'account_matches': account.get('account_id') == merchant['shop_id'],
                          'live': account.get('test') is False, 'enabled': account.get('status') == 'enabled',
                          'receipt_required': account.get('fiscalization_enabled') is True,
                          'status_present': 'status' in account,
                          'status': account.get('status') if account.get('status') in {'enabled', 'disabled', 'blocked', 'pending', 'registered'} else None,
                          'bank_card_supported': 'bank_card' in account.get('payment_methods', [])}))
        raise ValueError()
    blob = (json.dumps(merchant) + '\n').encode()
    fd = os.open(DESTINATION, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    print('{"merchant_verified":true,"live":true,"invoice_created":false,"transfer_ready":true}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--source-env')
    parser.add_argument('--merchant-file')
    args = parser.parse_args()
    if not args.prepare:
        print('{"plan_only":true,"network":false,"source_read":false}')
    else:
        try:
            if bool(args.source_env) == bool(args.merchant_file):
                raise ValueError()
            asyncio.run(prepare(args.source_env, args.merchant_file))
        except Exception:
            print('{"merchant_verified":false,"transfer_ready":false,"error":"merchant_check_failed"}')
            sys.exit(1)
