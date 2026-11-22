"""One scoped direct REALITY node. No subscribers, DNS, CDN or existing-node edits."""
import argparse
import base64
import copy
import importlib.util
import io
import json
from pathlib import Path
import secrets
import socket
import subprocess
import tarfile
import time
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location('universal_infra', HERE.parent / 'universal-direct/deploy.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)
IP, SNI, TAG = '177.3.250.75', 'login.vk.com', 'node75-single-reality'
SOURCE = '3ea9e8d5-89a2-4c8b-8234-5aa462c8ee15'
NAME, NODE, REMARK = 'REALITY_SINGLE_25075', 'single-reality-25075', '⚡ TCP REALITY • Тест 75'
STATE = Path('/private/tmp/spn-node75-panel')
SOCKET = '/private/tmp/spn-node75-physical.sock'


def ssh(command, data=None):
    args = ['ssh', '-S', SOCKET, '-o', 'ControlMaster=no', '-o', 'BatchMode=yes',
            '-o', 'ProxyCommand=/usr/bin/false', 'root@' + IP, command]
    result = subprocess.run(args, input=data, check=False)
    u.need(result.returncode == 0, 'Scoped SSH step failed; state preserved.')


def config(private, short):
    return {'log': {'loglevel': 'warning', 'access': 'none'},
            'dns': {'servers': ['localhost'], 'queryStrategy': 'UseIPv4'},
            'inbounds': [{'tag': TAG, 'listen': '0.0.0.0', 'port': 443, 'protocol': 'vless',
                'settings': {'clients': [], 'decryption': 'none'},
                'sniffing': {'enabled': True, 'destOverride': ['http', 'tls'], 'routeOnly': True},
                'streamSettings': {'network': 'raw', 'security': 'reality', 'realitySettings': {
                    'show': False, 'target': SNI + ':443', 'xver': 0, 'serverNames': [SNI],
                    'privateKey': private, 'shortIds': [short]}}}],
            'outbounds': [{'tag': 'direct', 'protocol': 'freedom', 'settings': {'domainStrategy': 'UseIPv4'}},
                          {'tag': 'block', 'protocol': 'blackhole'}],
            'routing': {'domainStrategy': 'IPIfNonMatch', 'rules': [
                {'type': 'field', 'ip': ['geoip:private', '169.254.0.0/16', '168.63.129.16/32', '100.64.0.0/10'],
                 'outboundTag': 'block'}]}}


def active_sni(store):
    update = store.load('target-update.intent.json')
    if update:
        u.need(store.load('target-update.receipt.json') is not None, 'Target update not confirmed.')
        return update['sni']
    return SNI


def active_fingerprint(store):
    receipt = store.load('fingerprint-update.receipt.json')
    if receipt:
        u.need(receipt['newFingerprint'] == 'safari', 'Unknown fingerprint update.')
        return receipt['newFingerprint']
    return 'chrome'


def connection():
    settings = u.read_settings(ROOT / '.env')
    api = u.API(settings['REMNAWAVE_BASE_URL'], settings['REMNAWAVE_API_TOKEN'])
    store = u.Store(STATE)
    return api, store


def get_rows(api, kind):
    # GETs may be retried. Mutations never are blindly retried.
    for attempt in range(4):
        try:
            return api.rows(kind)
        except u.DeployError:
            if attempt == 3:
                raise
            time.sleep(.3)


def prepare(api, store):
    if store.load('plan.json'):
        return store.load('plan.json')
    nodes, profiles, hosts = (get_rows(api, kind) for kind in ('nodes', 'config-profiles', 'hosts'))
    u.need(not any(n['address'] == IP or n['name'] == NODE for n in nodes), 'Target node already exists.')
    u.need(not any(p['name'] == NAME or any(i['tag'] == TAG for i in p['inbounds']) for p in profiles), 'Target profile/tag exists.')
    source = u.one(hosts, SOURCE)
    u.need(source['address'] == 'fi104.wayspn.com' and source['isDisabled'] is False, 'Reference host changed.')
    squads = get_rows(api, 'internal-squads')
    audience = sorted(s['uuid'] for s in squads if source['inbound']['configProfileInboundUuid'] in u.ids(s['inbounds']))
    u.need(audience, 'Reference audience empty.')
    key = X25519PrivateKey.generate()
    encode = lambda b: base64.urlsafe_b64encode(b).decode().rstrip('=')
    private = encode(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()))
    public = encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    short = secrets.token_hex(8)
    store.save('reality.private.json', {'private': private, 'public': public, 'short': short})
    plan = {'id': uuid.uuid4().hex, 'ip': IP, 'domain': '', 'name': REMARK, 'country': '', 'squads': audience,
            'panelIp': socket.gethostbyname(u.urlsplit(api.base).hostname), 'scope': copy.deepcopy(source['internalSquads'])}
    store.save('plan.json', plan)
    return plan


def provision(api, store, plan):
    keys = store.load('reality.private.json')
    helper = u.Deployment(api, store, plan, {})
    baseline = {'name': NAME, 'config': config(keys['private'], keys['short'])}
    update = store.load('target-update.intent.json')
    if update:
        u.need(update['sni'] == 'www.cloudflare.com' and update['before'] == baseline['config']
               and store.load('profile.intent.json') == baseline and store.load('target-update.receipt.json'), 'Target update proof differs.')
        profile = u.one(get_rows(api, 'config-profiles'), store.load('profile.receipt.json')['uuid'])
        u.need(profile['name'] == NAME and profile['config'] == update['after'], 'Updated profile changed.')
    else:
        profile = helper.ensure('profile', 'config-profiles', baseline, lambda row: row['name'] == NAME)
    u.need(len(profile['inbounds']) == 1 and profile['inbounds'][0]['tag'] == TAG, 'Expected one inbound.')
    inbound = profile['inbounds'][0]['uuid']
    node = helper.ensure('node', 'nodes', {'name': NODE, 'address': IP, 'port': 2222,
        'tags': ['SINGLE_REALITY75'], 'note': 'One direct raw TCP REALITY443; no Nginx, WS, gRPC or CDN.',
        'configProfile': {'activeConfigProfileUuid': profile['uuid'], 'activeInbounds': [inbound]}},
        lambda row: row['address'] == IP or row['name'] == NODE)
    store.save('ids.json', {'profile': profile['uuid'], 'node': node['uuid'], 'inbound': inbound})
    return helper, profile, node, inbound


def install(api, store, plan):
    key = store.load('node-secret.json')
    if key is None:
        key = {'value': api('GET', '/keygen')['secretKey']}
        u.need(len(key['value']) > 20 and not any(c.isspace() for c in key['value']), 'Invalid node key.')
        store.save('node-secret.json', key)
    compose = f'''services:
  remnanode:
    container_name: remnanode
    hostname: remnanode
    image: {u.IMAGE}
    restart: unless-stopped
    network_mode: host
    labels:
      spn.deploy.id: "{plan['id']}"
    env_file:
      - node.env
    ulimits:
      nofile:
        soft: 65536
        hard: 65536
    logging:
      driver: json-file
      options:
        max-size: 20m
        max-file: '3'
'''
    files = {'docker-compose.yml': compose.encode(), 'node.env': ('NODE_PORT=2222\nSECRET_KEY=' + key['value'] + '\n').encode(),
             '.spn-deployment': (plan['id'] + '\n').encode()}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.mode, info.size = 0o600, len(data)
            archive.addfile(info, io.BytesIO(data))
    stage = '/root/spn-single75-' + plan['id']
    ssh(f'test ! -L {stage} && install -d -m 700 {stage} && tar -xmf - --no-same-owner -C {stage}', buffer.getvalue())
    command = f'''set -eu
umask 077
NODE=/opt/remnanode-single75
STAGE={stage}
if [ -e "$NODE" ]; then
 test ! -L "$NODE"
 test "$(cat "$NODE/.spn-deployment")" = "{plan['id']}"
 cmp "$STAGE/node.env" "$NODE/node.env"
 cmp "$STAGE/docker-compose.yml" "$NODE/docker-compose.yml"
else
 install -d -m 700 "$NODE"
 install -m 600 "$STAGE/.spn-deployment" "$STAGE/node.env" "$STAGE/docker-compose.yml" "$NODE/"
fi
if docker inspect remnanode >/dev/null 2>&1; then
 test "$(docker inspect -f '{{{{index .Config.Labels "spn.deploy.id"}}}}' remnanode)" = "{plan['id']}"
fi
if ! test -f "$NODE/firewall-ready"; then
 ufw allow 22/tcp comment SPN-SSH
 ufw allow 443/tcp comment SPN-REALITY
 ufw allow from {plan['panelIp']} to any port 2222 proto tcp comment SPN-PANEL
 ufw default deny incoming
 ufw default allow outgoing
 ufw --force enable
 touch "$NODE/firewall-ready"
fi
docker compose -f "$NODE/docker-compose.yml" up -d
'''
    ssh('bash -s', command.encode())


def bind_and_stage(api, store, plan, helper, profile, node, inbound):
    helper.wait_ready(node, 120)
    for uid in plan['squads']:
        current = u.one(get_rows(api, 'internal-squads'), uid)
        members = u.ids(current['inbounds'])
        if inbound not in members:
            body = {'uuid': uid, 'inbounds': members + [inbound]}
            store.save('squad-' + uid + '.intent.json', body)
            result = api('PATCH', '/internal-squads', body)
            u.need(set(u.ids(result['inbounds'])) == set(body['inbounds']), 'Squad readback differs.')
    body = {'remark': REMARK, 'address': IP, 'port': 443, 'nodes': [node['uuid']],
        'inbound': {'configProfileUuid': profile['uuid'], 'configProfileInboundUuid': inbound},
        'sni': active_sni(store), 'alpn': 'h2', 'fingerprint': active_fingerprint(store), 'securityLayer': 'DEFAULT',
        'isDisabled': True, 'isHidden': False, 'internalSquads': plan['scope'],
        'mihomoX25519': False, 'overrideSniFromAddress': False, 'keepSniBlank': False,
        'tags': ['SINGLE_REALITY75']}
    if store.load('target-update.intent.json') or store.load('fingerprint-update.receipt.json'):
        host = u.one(get_rows(api, 'hosts'), store.load('tcp.receipt.json')['uuid'])
        original = {**body, 'sni': SNI, 'fingerprint': 'chrome'}
        u.need(store.load('tcp.intent.json') == original, 'Original host ownership differs.')
        observed = store.load('tcp.external-enabled.receipt.json')
        if observed:
            u.need(observed == {'uuid': host['uuid'], 'observedDisabled': False}, 'External enable receipt differs.')
        expected = {**body, 'isDisabled': host['isDisabled']} if store.load('tcp.enable.intent.json') or observed else body
        u.need(u.same(host, expected), 'Updated host differs.')
    else:
        host = helper.ensure('tcp', 'hosts', body,
                             lambda row: (row.get('inbound') or {}).get('configProfileInboundUuid') == inbound)
    return host


def retarget(api, store):
    ids = store.load('ids.json')
    keys = store.load('reality.private.json')
    before = config(keys['private'], keys['short'])
    after = copy.deepcopy(before)
    after['inbounds'][0]['streamSettings']['realitySettings'].update(
        target='www.cloudflare.com:443', serverNames=['www.cloudflare.com'])
    intent = {'sni': 'www.cloudflare.com', 'profile': ids['profile'], 'before': before, 'after': after,
              'host': store.load('tcp.receipt.json')['uuid']}
    store.save('target-update.intent.json', intent)
    node = u.one(get_rows(api, 'nodes'), ids['node'])
    u.need(node['address'] == IP and node['configProfile']['activeConfigProfileUuid'] == ids['profile'], 'Wrong node.')
    u.need([n['uuid'] for n in get_rows(api, 'nodes') if n['configProfile']['activeConfigProfileUuid'] == ids['profile']] == [ids['node']], 'Shared profile.')
    profile = u.one(get_rows(api, 'config-profiles'), ids['profile'])
    u.need(profile['config'] in (before, after), 'Concurrent profile change.')
    host = u.one(get_rows(api, 'hosts'), intent['host'])
    u.need(host['isDisabled'] and host['nodes'] == [ids['node']], 'Host must still be staged disabled.')
    if profile['config'] == before:
        api('PATCH', '/config-profiles', {'uuid': ids['profile'], 'config': after})
    u.need(u.one(get_rows(api, 'config-profiles'), ids['profile'])['config'] == after, 'Profile update not verified.')
    u.need(host['sni'] in (SNI, intent['sni']), 'Unexpected SNI.')
    if host['sni'] != intent['sni']:
        api('PATCH', '/hosts', {'uuid': host['uuid'], 'sni': intent['sni']})
    u.need(u.one(get_rows(api, 'hosts'), host['uuid'])['sni'] == intent['sni'], 'Host update not verified.')
    store.save('target-update.receipt.json', {'profile': ids['profile'], 'host': host['uuid'], 'afterHash': u.digest(after)})
    print('Updated only the staged new node REALITY target; still one inbound, host disabled.')


def set_safari(api, store):
    ids = store.load('ids.json')
    proof = store.load('probe-proof.json')
    u.need(proof and proof.get('passed') is True and proof.get('ip') == IP
           and proof.get('fingerprint') == 'safari', 'Direct Safari traffic proof required.')
    host_id = store.load('tcp.receipt.json')['uuid']
    host = u.one(get_rows(api, 'hosts'), host_id)
    original = store.load('tcp.intent.json')
    expected = {**original, 'sni': active_sni(store), 'fingerprint': host['fingerprint'],
                'isDisabled': host['isDisabled']}
    u.need(isinstance(host['isDisabled'], bool) and host['nodes'] == [ids['node']]
           and host['fingerprint'] in {'chrome', 'safari'} and u.same(host, expected), 'Staged host changed.')
    if host['isDisabled'] is False:
        # The owner may enable a staged host manually. Record that observation;
        # do not claim that this script enabled it or toggle its availability.
        store.save('tcp.external-enabled.receipt.json', {'uuid': host_id, 'observedDisabled': False})
    body = {'uuid': host_id, 'fingerprint': 'safari'}
    store.save('fingerprint-update.intent.json', {**body, 'profile': ids['profile'], 'proofHash': u.digest(proof)})
    if host['fingerprint'] != 'safari':
        api('PATCH', '/hosts', body)
    actual = u.one(get_rows(api, 'hosts'), host_id)
    u.need(u.same(actual, {**expected, 'fingerprint': 'safari'}), 'Fingerprint update not verified.')
    store.save('fingerprint-update.receipt.json', {'uuid': host_id, 'newFingerprint': 'safari'})
    print('Only the new owned host now exports Safari; availability preserved, other hosts unchanged.')


def main(action):
    api, store = connection()
    with store.lock():
        if action == 'retarget':
            retarget(api, store)
            return
        if action == 'set-safari':
            set_safari(api, store)
            return
        plan = prepare(api, store)
        helper, profile, node, inbound = provision(api, store, plan)
        if action == 'deploy':
            install(api, store, plan)
        host = bind_and_stage(api, store, plan, helper, profile, node, inbound)
        if action == 'enable':
            proof = store.load('probe-proof.json')
            u.need(proof and proof.get('passed') is True and proof.get('ip') == IP
                   and proof.get('fingerprint') == host['fingerprint'], 'Matching authenticated probe proof required.')
            helper.publish({'tcp': host})
            host = u.one(get_rows(api, 'hosts'), host['uuid'])
        print(json.dumps({'host': host['uuid'], 'remark': REMARK, 'disabled': host['isDisabled'],
                          'node': node['uuid'], 'oneInbound': True, 'ip': IP}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('deploy', 'stage', 'enable', 'retarget', 'set-safari'))
    try:
        main(parser.parse_args().action)
    except Exception as error:
        print('Scoped deployment stopped:', type(error).__name__, '; private details suppressed.')
        raise SystemExit(2)
