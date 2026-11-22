"""Reversible TCP-only REALITY target comparison; preserve all other fields."""
import copy
import importlib.util
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('pilot', HERE / 'tcp_pilot.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
spec = importlib.util.spec_from_file_location('transport', HERE.parent / 'direct-node198/panel_tunnel.py')
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)
t.PORT = 18748
p.m.api = t.adapter(p.m.ROOT)
HOST = 'cdf0f08b-e524-45bd-8657-cf3f20b6bfb6'


def prepare():
    profile, node = p.target()
    host = p.one(p.rows('hosts'), HOST)
    assert host['address'] == p.IP and host['port'] == 2443 and host['nodes'] == [p.NODE]
    config = copy.deepcopy(profile['config'])
    inbound = next(i for i in config['inbounds'] if i['tag'] == p.TAG)
    reality = inbound['streamSettings']['realitySettings']
    assert reality['target'] == '127.0.0.1:443' and reality['serverNames'] == ['www.tekst.site']
    assert host['sni'] == 'www.tekst.site'
    reality['target'] = 'login.vk.com:443'
    reality['serverNames'] = ['login.vk.com']
    p.m.save('tcp-vk-target.profile.before.json', profile)
    p.m.save('tcp-vk-target.host.before.json', host)
    p.m.save('tcp-vk-target.candidate.json', config)
    p.m.save('tcp-vk-target.profile.intent.json', {'uuid': p.PROFILE, 'config': config})
    p.m.save('tcp-vk-target.host.intent.json', {'uuid': HOST, 'sni': 'login.vk.com'})
    print('Prepared: only TCP target/serverNames and host SNI differ; keys unchanged')


def apply():
    before = p.m.load('tcp-vk-target.profile.before.json')
    candidate = p.m.load('tcp-vk-target.candidate.json')
    live, node = p.target()
    if live['config'] != candidate:
        assert live['config'] == before['config'], 'Concurrent profile change; stop'
        p.m.api('PATCH', '/config-profiles', p.m.load('tcp-vk-target.profile.intent.json'))
    host = p.one(p.rows('hosts'), HOST)
    original = p.m.load('tcp-vk-target.host.before.json')
    assert all(host.get(k) == original.get(k) for k in ('address', 'port', 'nodes', 'inbound'))
    if host['sni'] != 'login.vk.com':
        assert host['sni'] == original['sni'], 'Concurrent host SNI change; stop'
        p.m.api('PATCH', '/hosts', p.m.load('tcp-vk-target.host.intent.json'))
    check()


def check():
    live, node = p.target()
    host = p.one(p.rows('hosts'), HOST)
    assert live['config'] == p.m.load('tcp-vk-target.candidate.json')
    assert host['sni'] == 'login.vk.com' and not host['isDisabled']
    print('TCP target comparison active; WS/gRPC configurations and all keys retained')


def rollback():
    before = p.m.load('tcp-vk-target.profile.before.json')
    live, node = p.target()
    config = copy.deepcopy(live['config'])
    old = next(i for i in before['config']['inbounds'] if i['tag'] == p.TAG)['streamSettings']['realitySettings']
    current = next(i for i in config['inbounds'] if i['tag'] == p.TAG)['streamSettings']['realitySettings']
    assert current['privateKey'] == old['privateKey'] and current['shortIds'] == old['shortIds']
    assert current['target'] in {'login.vk.com:443', old['target']}
    assert current['serverNames'] in [['login.vk.com'], old['serverNames']]
    current['target'], current['serverNames'] = old['target'], old['serverNames']
    if config != live['config']:
        body = {'uuid': p.PROFILE, 'config': config}
        p.m.save('tcp-vk-target.rollback.profile.intent.json', body)
        p.m.api('PATCH', '/config-profiles', body)
    host = p.one(p.rows('hosts'), HOST)
    original = p.m.load('tcp-vk-target.host.before.json')
    assert all(host.get(k) == original.get(k) for k in ('address', 'port', 'nodes', 'inbound'))
    if host['sni'] != original['sni']:
        assert host['sni'] == 'login.vk.com'
        body = {'uuid': HOST, 'sni': original['sni']}
        p.m.save('tcp-vk-target.rollback.host.intent.json', body)
        p.m.api('PATCH', '/hosts', body)
    after, node = p.target()
    host = p.one(p.rows('hosts'), HOST)
    assert after['config'] == config and host['sni'] == original['sni']
    print('Rolled back only TCP target/SNI; concurrent unrelated fields preserved')


if __name__ == '__main__':
    try:
        assert len(sys.argv) == 2 and sys.argv[1] in {'prepare', 'apply', 'check', 'rollback'}
        globals()[sys.argv[1]]()
    except Exception as error:
        print('Target comparison stopped:', type(error).__name__, '; credentials suppressed', file=sys.stderr)
        raise SystemExit(2)
