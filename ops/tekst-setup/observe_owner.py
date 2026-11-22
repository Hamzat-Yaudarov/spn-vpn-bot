"""Bounded read-only observer for one already-authorized TEKST owner.

Default is a no-I/O plan. --run uses only an existing pinned SSH master.
Only account id/username cross stdin; no token, UUID, destination or peer IP
is printed or persisted. Remote code reads kernel TCP metadata and NEW log
records, never captures packets and never writes remote files.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('owner_observer_validation', HERE.parent / 'host-health-audit/e2e.py')
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)
ROOT = Path('/private/tmp/wayspn-tekst-auth-20261106')
SOCKET = '/private/tmp/spn-tcp39-ssh'
KNOWN = '/private/tmp/spn-tcp39-known-hosts'
TARGET = 'root@64.188.80.39'

REMOTE = r'''
import collections, hashlib, ipaddress, json, os, re, signal, subprocess, sys, time
class Finished(Exception): pass
def expired(*unused): raise Finished()
def emit(value): print(json.dumps(value, separators=(',', ':')), flush=True)
cfg=json.loads(sys.stdin.readline(4097))
assert set(cfg)=={'emails','duration'} and type(cfg['duration']) is int and 1<=cfg['duration']<=180
emails=set(cfg['emails']); assert 1<=len(emails)<=2 and all(isinstance(x,str) and 0<len(x)<=128 and not re.search(r'\s',x) for x in emails)
duration=cfg['duration']; started=time.monotonic(); wall_started=time.time(); salt=os.urandom(32)
signal.signal(signal.SIGALRM,expired); signal.setitimer(signal.ITIMER_REAL,duration)
tags={'ordinary-tcp-reality-tekst':'tcp','ordinary-grpc-reality-tekst':'grpc','ordinary-ws-tekst':'ws'}
states={'ESTAB','SYN-SENT','SYN-RECV','FIN-WAIT-1','FIN-WAIT-2','TIME-WAIT','CLOSE','CLOSE-WAIT','LAST-ACK','LISTEN','CLOSING','NEW-SYN-RECV','UNCONN'}
owners=set(); totals=dict.fromkeys(tags.values(),0); log_state={'identity':None,'position':0,'buffer':b'','rotations':0,'skipped':0}; previous={}

def command(args,limit):
    result=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=2,check=False)
    if result.returncode or len(result.stdout)>limit: raise ValueError()
    return result.stdout.decode('utf8','replace')

def peer(value):
    value=re.sub(r'^(?:tcp:|udp:)','',value)
    address,port=value.rsplit(':',1); parsed=ipaddress.ip_address(address.strip('[]'))
    parsed=getattr(parsed,'ipv4_mapped',None) or parsed
    port=int(port); assert 0<port<65536
    return str(parsed),port

def log_read(initial=False):
    events=dict.fromkeys(tags.values(),0)
    try:
        pid=command(['docker','inspect','-f','{{.State.Pid}}','remnanode'],32).strip()
        assert pid.isdigit() and int(pid)>0
        path='/proc/'+pid+'/root/var/log/xray/current'
        with open(path,'rb') as stream:
            drop_first=False
            st=os.fstat(stream.fileno()); identity=(st.st_dev,st.st_ino)
            if log_state['identity']!=identity:
                if log_state['identity'] is not None: log_state['rotations']+=1
                log_state.update(identity=identity,position=st.st_size if initial or st.st_mtime<wall_started else 0,buffer=b'')
            if st.st_size<log_state['position']:
                log_state.update(position=0,buffer=b''); log_state['rotations']+=1
            if st.st_size-log_state['position']>1048576:
                log_state['skipped']+=st.st_size-log_state['position']-1048576
                log_state.update(position=st.st_size-1048576,buffer=b'')
                drop_first=True
            stream.seek(log_state['position']); data=stream.read(1048576); log_state['position']=stream.tell()
        pieces=(log_state['buffer']+data).split(b'\n'); tail=pieces.pop()
        if drop_first and pieces: pieces.pop(0)
        log_state['buffer']=tail if len(tail)<=4096 else b''
        for raw in pieces:
            if len(raw)>4096: continue
            line=raw.decode('utf8','replace'); account=re.search(r'\bemail:\s*(\S+)',line)
            if account is None or account.group(1) not in emails: continue
            tag=next((t for t in tags if re.search(r'\['+re.escape(t)+r'(?:\s|>|\])',line)),None)
            if tag is None: continue
            events[tags[tag]]+=1; totals[tags[tag]]+=1
            if tags[tag]=='tcp' and ' accepted ' in line:
                source=re.search(r'\bfrom\s+(\S+)',line)
                if source:
                    try:
                        if len(owners)<4096: owners.add(peer(source.group(1)))
                    except (ValueError,AssertionError): pass
        return True,events
    except (OSError,ValueError,AssertionError,subprocess.TimeoutExpired): return False,events

def metrics(line):
    result={}
    for name in ('bytes_acked','bytes_received'):
        match=re.search(r'(?:^|\s)'+name+r':(\d+)',line); result[name]=int(match.group(1)) if match else 0
    match=re.search(r'(?:^|\s)retrans:\d+/(\d+)',line); result['retrans']=int(match.group(1)) if match else 0
    return result

def snapshot():
    rows={}
    for line in command(['ss','-HntioO','( sport = :2443 )'],4194304).splitlines():
        fields=line.split()
        if len(fields)<5: continue
        try: key=peer(fields[4])
        except (ValueError,AssertionError): continue
        state=fields[0] if fields[0] in states else 'OTHER'
        rows[key]=(state,metrics(line))
    return rows

def sample():
    global previous
    available,events=log_read(); ss_available=True
    try: rows=snapshot()
    except (OSError,ValueError,subprocess.TimeoutExpired): rows={}; ss_available=False
    all_states=collections.Counter(); other_states=collections.Counter(); own_rows=[]
    own_delta=dict.fromkeys(('acked','received','retrans'),0); other_delta=own_delta.copy(); own_count=0
    for key,(state,values) in rows.items():
        all_states[state]+=1; before=previous.get(key,{})
        delta={short:max(0,values[long]-before.get(long,0)) for short,long in [('acked','bytes_acked'),('received','bytes_received'),('retrans','retrans')]}
        if key in owners:
            own_count+=1
            for name in delta: own_delta[name]+=delta[name]
            if len(own_rows)<64:
                label='peer-'+hashlib.blake2s((key[0]+':'+str(key[1])).encode(),key=salt,digest_size=6).hexdigest()
                own_rows.append({'label':label,'state':state,'ackedDelta':delta['acked'],'receivedDelta':delta['received'],'retransDelta':delta['retrans']})
        else:
            other_states[state]+=1
            for name in delta: other_delta[name]+=delta[name]
    if ss_available: previous={key:value[1] for key,value in rows.items()}
    emit({'event':'SAMPLE','elapsedSeconds':round(time.monotonic()-started,1),'logIdentityConfigured':bool(emails),
          'logAvailable':available,'ssAvailable':ss_available,'logRotations':log_state['rotations'],'logBytesSkipped':log_state['skipped'],
          'ownerAccessEvents':events,'ownerAccessTotal':totals.copy(),'ownerTcpAuthenticated':bool(owners),
          'ownerTcpConnections':own_count,'ownerTcpRows':own_rows,'ownerRowsLimited':own_count>64,
          'allTcpStates':dict(all_states),'otherTcpStates':dict(other_states),
          'ownerTrafficDelta':own_delta,'otherTrafficDelta':other_delta})

try:
    available,_=log_read(initial=True)
    try: previous={key:value[1] for key,value in snapshot().items()}
    except (OSError,ValueError,subprocess.TimeoutExpired): previous={}
    emit({'event':'READY','durationSeconds':duration,'sampleSeconds':5,'logIdentityConfigured':bool(emails),'logAvailable':available})
    due=time.monotonic()+5
    while True:
        time.sleep(max(0,due-time.monotonic())); sample(); due+=5
except Finished: pass
except BaseException:
    emit({'event':'ERROR','logIdentityConfigured':bool(emails)})
finally:
    signal.setitimer(signal.ITIMER_REAL,0)
    emit({'event':'DONE','elapsedSeconds':round(time.monotonic()-started,1),'logIdentityConfigured':bool(emails)})
'''


def safe_output(line):
    value = json.loads(line)
    assert isinstance(value, dict) and value.get('event') in {'READY', 'SAMPLE', 'DONE', 'ERROR'}
    allowed = {'event', 'elapsedSeconds', 'durationSeconds', 'sampleSeconds', 'logIdentityConfigured',
               'logAvailable', 'ssAvailable', 'logRotations', 'logBytesSkipped', 'ownerAccessEvents',
               'ownerAccessTotal', 'ownerTcpAuthenticated', 'ownerTcpConnections', 'ownerTcpRows',
               'ownerRowsLimited', 'allTcpStates', 'otherTcpStates', 'ownerTrafficDelta', 'otherTrafficDelta'}
    assert set(value) <= allowed
    for row in value.get('ownerTcpRows', []):
        assert set(row) == {'label', 'state', 'ackedDelta', 'receivedDelta', 'retransDelta'}
        assert re.fullmatch(r'peer-[0-9a-f]{12}', row['label']) and re.fullmatch(r'[A-Z0-9_-]{1,16}', row['state'])
    def clean(item):
        if isinstance(item, dict): return all(isinstance(k, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,40}', k) and clean(v) for k, v in item.items())
        if isinstance(item, list): return len(item) <= 64 and all(clean(v) for v in item)
        if isinstance(item, str): return bool(re.fullmatch(r'(?:READY|SAMPLE|DONE|ERROR|[A-Z0-9_-]{1,16}|peer-[0-9a-f]{12})', item))
        return item is None or type(item) in (int, float, bool)
    assert clean(value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--duration', type=int, default=180)
    args = parser.parse_args()
    assert 1 <= args.duration <= 180
    if not args.run:
        print(json.dumps({'planOnly': True, 'maxDurationSeconds': args.duration, 'network': False}))
        return
    record = validation.protected_json(ROOT / 'owner-selected.private.json')
    assert type(record.get('id')) is int and record['id'] > 0
    username = record.get('username')
    assert isinstance(username, str) and re.fullmatch(r'[A-Za-z0-9_.@+-]{1,128}', username)
    assert not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', username)
    payload = {'emails': sorted({str(record['id']), username}), 'duration': args.duration}
    check = subprocess.run(['/usr/bin/ssh', '-S', SOCKET, '-O', 'check', TARGET],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    assert check.returncode == 0, 'Existing SSH master required'
    argv = ['/usr/bin/ssh', '-S', SOCKET, '-o', 'ControlMaster=no', '-o', 'ProxyCommand=/usr/bin/false',
            '-o', 'BatchMode=yes', '-o', 'RequestTTY=no', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile=' + KNOWN, '-T', TARGET, 'python3 -u -c ' + shlex.quote(REMOTE)]
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
    timer = threading.Timer(args.duration + 15, child.kill)
    timer.daemon = True
    timer.start()
    try:
        child.stdin.write(json.dumps(payload) + '\n'); child.stdin.close()
        while True:
            line = child.stdout.readline(65537)
            if not line: break
            try:
                assert len(line) <= 65536 and line.endswith('\n')
                print(json.dumps(safe_output(line)), flush=True)
            except (ValueError, AssertionError, TypeError):
                print(json.dumps({'event': 'ERROR', 'logIdentityConfigured': True}), flush=True)
        child.wait(timeout=3)
        if child.returncode: raise RuntimeError('Read-only observer ended')
    finally:
        timer.cancel()
        if child.poll() is None:
            child.terminate()
            try: child.wait(timeout=3)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=3)


if __name__ == '__main__':
    try: main()
    except KeyboardInterrupt: raise SystemExit(130)
    except Exception as error:
        print('Observer stopped:', type(error).__name__, '; private identities suppressed', file=sys.stderr)
        raise SystemExit(2)
