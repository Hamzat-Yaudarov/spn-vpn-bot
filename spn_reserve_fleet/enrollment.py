"""Discover completed owned direct installations, without contacting any VPS."""
from __future__ import annotations

import importlib.util
from contextlib import nullcontext
from pathlib import Path
from functools import lru_cache

from .store import DEFAULT_ROOT, Store


REPO_ROOT = Path(__file__).resolve().parents[1]


class InstallationBusy(ValueError):
    pass


@lru_cache(maxsize=1)
def replacement_module():
    spec = importlib.util.spec_from_file_location('spn_reserve_replacement',
                                                REPO_ROOT / 'ops/universal-reality/replace.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=1)
def cdn_replacement_module():
    spec = importlib.util.spec_from_file_location('spn_reserve_cdn_replacement',
                                                REPO_ROOT / 'ops/universal-yandex/replace.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def installation_snapshot(state_dir, *, locked=False, allow_delegated=False):
    r = replacement_module()
    original = r.u.Store(Path(state_dir).expanduser().absolute())
    def read():
        if not allow_delegated:
            from .remote import require_local_control
            require_local_control(original)
        plan = original.load('plan.json')
        if not isinstance(plan, dict) or not plan.get('domain') or not plan.get('panel'):
            raise ValueError('Нужна завершённая прямая установка с доменом')
        if plan.get('cdnDomain'):
            cdn = cdn_replacement_module()
            try:
                cdn.validate_original(original, plan['domain'], plan['panel'])
                snapshot = cdn.installation_snapshot(state_dir)
                if snapshot['status_proof'] not in ('traffic', 'pending'):
                    raise ValueError('Сначала завершите проверку CDN')
                return {**snapshot, 'name': plan['name'], 'country': plan.get('country', ''),
                        'transport': 'yandex-xhttp-cdn-v1', 'state_dir': str(original.path), 'panel_url': plan['panel']}
            except cdn.u.DeployError:
                raise ValueError('Журнал CDN не подтверждён. Продолжите его установку.') from None
        r.validate_original(original, plan['domain'], plan['panel'])
        commits, pending = r.chain(original, plan)
        identity = r.original_machine(original, commits)
        return {'domain': plan['domain'], 'name': plan['name'], 'country': plan.get('country', ''),
                'transport': plan['transport'], 'current_ip': commits[-1]['toIp'] if commits else plan['ip'],
                'ssh_port': plan['sshPort'], 'state_dir': str(original.path), 'installation_id': plan['id'],
                'panel_url': plan['panel'], 'ssh_fingerprint': identity['sshFingerprint'],
                'generation': len(commits),
                'pending_ip': pending[1]['toIp'] if pending and pending[1] else None}
    try:
        if locked:
            return read()
        with original.lock():
            return read()
    except r.u.DeployError as error:
        if str(error) == 'Это развёртывание уже выполняется.':
            raise InstallationBusy('Установка занята; проверка отложена') from None
        raise ValueError(str(error)) from None


def synchronize(store, state_dir, *, locked=False):
    r = replacement_module()
    original = r.u.Store(Path(state_dir).expanduser().absolute())
    try:
        with nullcontext() if locked else original.lock():
            return store.register(installation_snapshot(state_dir, locked=True))
    except r.u.DeployError as error:
        if str(error) == 'Это развёртывание уже выполняется.':
            raise InstallationBusy('Установка занята; проверка отложена') from None
        raise ValueError(str(error)) from None


def register_installation(state_dir, data_dir=None, *, locked=False):
    return synchronize(Store(data_dir or DEFAULT_ROOT), state_dir, locked=locked)


def discover(store, home=None):
    home = Path.home() if home is None else Path(home)
    root = home / '.spn-reality-deploy'
    known_rows = store.list_servers(include_archived=True)
    known_paths = {Path(value['state_dir']): value for value in known_rows}
    paths = {path for path, value in known_paths.items() if not value.get('archived')}
    if root.is_dir() and not root.is_symlink():
        for namespace in root.iterdir():
            if not namespace.is_dir() or namespace.is_symlink():
                continue
            paths.update(path for path in namespace.iterdir()
                         if path.is_dir() and not path.is_symlink() and (path / 'plan.json').is_file())
    imported, errors = [], []
    for path in sorted(paths):
        known = known_paths.get(path)
        if known and known.get('archived'):
            continue
        try:
            with store.server_lock(known['id']) if known else nullcontext():
                # An archive can be requested while the namespace was being
                # enumerated. Recheck under the operational server lock.
                if known and (store.get_server(known['id']) or {}).get('archived'):
                    continue
                imported.append(synchronize(store, path))
        except InstallationBusy:
            continue
        except (ValueError, RuntimeError, OSError) as error:
            if str(error) == 'Этот сервер уже проверяется или переключается':
                continue
            # Incomplete deployments and legacy VK/IP-only plans are expected.
            # Known broken installations must be visible and stop automation.
            if known:
                store.update(known['id'], auto_enabled=False, status='error',
                             last_error='Журнал установки занят или не подтверждён; автосмена остановлена')
                errors.append(known['id'])
    return {'imported': len(imported), 'errors': errors}
