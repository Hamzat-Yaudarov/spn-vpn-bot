"""Unpublished TEKST TCP/TLS trial with the existing panel-managed account.

Keep current hosts, inbounds and account membership. Only the new inbound is
assigned to the node and one of the owner's already-authorized squads. The
listener's IPv4 firewall rule is managed separately after validation.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('pilot', HERE / 'tcp_pilot.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
if os.environ.get('TEKST_PANEL_TUNNEL') == '1':
    spec = importlib.util.spec_from_file_location('tunnel', HERE.parent / 'direct-node198/panel_tunnel.py')
    t = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t)
    t.PORT = 18748
    p.m.api = t.adapter(p.m.ROOT)

TAG = 'ordinary-tcp-tls-tekst-trial'
PORT = 2444
CERT = '/etc/letsencrypt/live/tekst.site/'
OWNER = Path('/private/tmp/wayspn-tekst-auth-20261106/owner-selected.private.json')


def inbound():
    return {
        'tag': TAG, 'port': PORT, 'listen': p.IP, 'protocol': 'vless',
        'settings': {'clients': [], 'decryption': 'none', 'flow': 'xtls-rprx-vision'},
        'sniffing': {'enabled': True, 'destOverride': ['http', 'tls'], 'routeOnly': True},
        'streamSettings': {'network': 'raw', 'security': 'tls', 'tlsSettings': {
            'minVersion': '1.3', 'rejectUnknownSni': True,
            'alpn': ['http/1.1'],
            'certificates': [{'certificateFile': CERT + 'fullchain.pem', 'keyFile': CERT + 'privkey.pem'}],
        }},
    }


def prepare():
    if (p.m.STATE / 'tls-trial-plan.json').exists():
        print('Private TLS trial plan already exists')
        return
    assert not OWNER.is_symlink() and OWNER.stat().st_mode & 0o777 == 0o600
    user = json.loads(OWNER.read_bytes())
    assert user['status'] == 'ACTIVE'
    profile, node = p.target()
    original = profile['config']
    assert not any(i['port'] == PORT or i['tag'] == TAG for i in original['inbounds'])
    assert not any(h['address'] == p.IP and h['port'] == PORT for h in p.rows('hosts'))
    existing = next(i['uuid'] for i in profile['inbounds'] if i['tag'] == p.TAG)
    owner_squads = set(p.ids(user['activeInternalSquads']))
    matching = sorted(s['uuid'] for s in p.rows('internal-squads')
                      if s['uuid'] in owner_squads and existing in p.ids(s['inbounds']))
    assert matching, 'Existing owner squad not found'
    candidate = copy.deepcopy(original)
    candidate['inbounds'].append(inbound())
    p.m.save('tls-trial-candidate.json', candidate)
    p.m.save('tls-trial-plan.json', {'before': original, 'squad': matching[0],
                                     'active': p.ids(node['configProfile']['activeInbounds'])})
    print('Prepared unpublished TLS trial and private rollback; no server writes')


def update_node(active):
    p.m.api('PATCH', '/nodes', {'uuid': p.NODE, 'configProfile': {
        'activeConfigProfileUuid': p.PROFILE, 'activeInbounds': active}})


def update_squad(squad, active):
    p.m.api('PATCH', '/internal-squads', {'uuid': squad, 'inbounds': active})


def apply():
    plan = p.m.load('tls-trial-plan.json')
    candidate = p.m.load('tls-trial-candidate.json')
    assert candidate['inbounds'][-1] == inbound()
    assert {**candidate, 'inbounds': candidate['inbounds'][:-1]} == plan['before']
    profile, node = p.target()
    if profile['config'] != candidate:
        assert profile['config'] == plan['before'], 'Concurrent profile change'
        p.m.api('PATCH', '/config-profiles', {'uuid': p.PROFILE, 'config': candidate})
    profile, node = p.target()
    assert profile['config'] == candidate
    uid = next(i['uuid'] for i in profile['inbounds'] if i['tag'] == TAG)
    p.m.save('tls-trial-inbound.json', {'uuid': uid})
    active = p.ids(node['configProfile']['activeInbounds'])
    assert set(plan['active']).issubset(active)
    if uid not in active:
        update_node(active + [uid])
    squad = p.one(p.rows('internal-squads'), plan['squad'])
    members = p.ids(squad['inbounds'])
    if uid not in members:
        update_squad(squad['uuid'], members + [uid])
    check()


def check():
    plan = p.m.load('tls-trial-plan.json')
    uid = p.m.load('tls-trial-inbound.json')['uuid']
    profile, node = p.target()
    active = p.ids(node['configProfile']['activeInbounds'])
    assert profile['config'] == p.m.load('tls-trial-candidate.json')
    assert {uid, *plan['active']}.issubset(active)
    assert uid in p.ids(p.one(p.rows('internal-squads'), plan['squad'])['inbounds'])
    assert not any(h.get('inbound', {}).get('configProfileInboundUuid') == uid for h in p.rows('hosts'))
    print('TCP/TLS 2444 staged, unpublished; original inbounds retained')


def remove():
    plan = p.m.load('tls-trial-plan.json')
    profile, node = p.target()
    matches = [i for i in profile['config']['inbounds'] if i['tag'] == TAG]
    if not matches:
        print('No trial inbound present')
        return
    assert matches == [inbound()], 'Trial inbound edited; do not remove'
    uid = next(i['uuid'] for i in profile['inbounds'] if i['tag'] == TAG)
    assert not any(h.get('inbound', {}).get('configProfileInboundUuid') == uid for h in p.rows('hosts'))
    attached = [s for s in p.rows('internal-squads') if uid in p.ids(s['inbounds'])]
    assert all(s['uuid'] == plan['squad'] for s in attached), 'Unexpected trial audience'
    for s in attached:
        update_squad(s['uuid'], [i for i in p.ids(s['inbounds']) if i != uid])
    active = p.ids(node['configProfile']['activeInbounds'])
    if uid in active:
        update_node([i for i in active if i != uid])
    profile, _ = p.target()
    assert [i for i in profile['config']['inbounds'] if i['tag'] == TAG] == [inbound()]
    cleaned = copy.deepcopy(profile['config'])
    cleaned['inbounds'] = [i for i in cleaned['inbounds'] if i['tag'] != TAG]
    p.m.api('PATCH', '/config-profiles', {'uuid': p.PROFILE, 'config': cleaned})
    assert p.target()[0]['config'] == cleaned
    print('Removed only unpublished trial inbound; firewall cleanup is separate')


if __name__ == '__main__':
    try:
        assert len(sys.argv) == 2 and sys.argv[1] in {'prepare', 'apply', 'check', 'remove'}
        globals()[sys.argv[1]]()
    except Exception as error:
        print('TCP/TLS trial stopped:', type(error).__name__, '; private data suppressed', file=sys.stderr)
        raise SystemExit(2)
