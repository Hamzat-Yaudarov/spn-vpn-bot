"""Opt-in real Linux nftables round trips and isolated packet tests.

Run as Linux root with CAP_SYS_ADMIN and CAP_NET_ADMIN:
  SPN_RUN_NETNS_TESTS=1 python3 -m unittest discover -s ops/universal-reality -p test_node_guard_netns.py

All network and nft mutations happen in a fresh outer network namespace. A
second nested namespace provides the client. The host namespace is never used
for firewall, route, interface or address changes. No systemd units are changed.
"""
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import textwrap
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import node_guard as g

ID = 'a1b2c3d4' * 4
ACTIVE, NEW = '217.60.30.79', '217.60.30.86'
CONTROL, PANEL = '147.45.164.28', '193.23.216.11'


def cmd(*args, input=None):
    result = subprocess.run(args, input=input, text=True, capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError(repr(args) + ': ' + result.stderr)
    return result.stdout


def run_validation():
    assert sys.platform.startswith('linux') and os.geteuid() == 0
    assert os.readlink('/proc/self/ns/net') != os.readlink('/proc/1/ns/net'), 'Refusing the host network namespace'
    policy={'version':1,'deployment':ID,'activeIp':ACTIVE,'panelIp':PANEL,'controllerIp':CONTROL,'scanners':g.scanners.load_snapshot()}
    def apply(policy, old=False):
        objects=g.policy_objects(policy)
        ops=([{'delete':{'table':{'family':'inet','name':g.TABLE}}}] if old else [])
        ops += [{'create' if 'table' in item else 'add':item} for item in objects]
        batch=json.dumps({'nftables':ops})
        cmd('nft','-j','-c','-f','-',input=batch)
        cmd('nft','-j','-f','-',input=batch)
        actual=json.loads(cmd('nft','-j','list','table','inet',g.TABLE))['nftables']
        expected=g.normalize(objects); normalized=g.normalize(actual)
        if normalized != expected:
            print(json.dumps({'roundtrip':False,'actual':normalized,'expected':expected}),flush=True)
            raise AssertionError('nft JSON readback differs')
        print(json.dumps({'roundtrip':True,'retarget':old}),flush=True)
    apply(policy)
    cmd('ip','link','set','lo','up')
    CHILD=r'''
    import json, socket, sys, time
    print('READY',flush=True)
    for line in sys.stdin:
        d=json.loads(line); s=socket.socket(socket.AF_INET6 if ':' in d['dst'] else socket.AF_INET,socket.SOCK_STREAM);s.settimeout(.7)
        try:
            if d.get('src'):s.bind((d['src'],0))
            s.connect((d['dst'],d['port'])); s.sendall(b'probe'); outcome=s.recv(16)==b'OK'
        except OSError: outcome=False
        finally:s.close()
        print(json.dumps({'ok':outcome}),flush=True)
    '''
    child=subprocess.Popen(['unshare','--net','python3','-u','-c',textwrap.dedent(CHILD)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    assert child.stdout.readline().strip()=='READY'
    try:
        cmd('ip','link','add','ngs','type','veth','peer','name','ngc')
        cmd('ip','link','set','ngc','netns',str(child.pid))
        cmd('ip','addr','add','10.77.0.1/24','dev','ngs')
        cmd('ip','addr','add',ACTIVE+'/32','dev','ngs')
        cmd('ip','addr','add',NEW+'/32','dev','ngs')
        cmd('ip','-6','addr','add','fd00:77::1/64','dev','ngs','nodad')
        cmd('ip','link','set','ngs','up')
        def client(*args):return cmd('nsenter','--net=/proc/'+str(child.pid)+'/ns/net','--',*args)
        client('ip','link','set','lo','up');client('ip','link','set','ngc','up')
        client('ip','addr','add','10.77.0.2/24','dev','ngc')
        client('ip','-6','addr','add','fd00:77::2/64','dev','ngc','nodad')
        for ip in ('8.8.8.8','185.224.228.10',CONTROL,PANEL):
            client('ip','addr','add',ip+'/32','dev','ngc');cmd('ip','route','add',ip+'/32','dev','ngs')
        client('ip','route','add',ACTIVE+'/32','dev','ngc');client('ip','route','add',NEW+'/32','dev','ngc')
        listeners=[]
        def serve(family,port):
            s=socket.socket(family,socket.SOCK_STREAM);s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            if family==socket.AF_INET6:s.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
            s.bind(('::' if family==socket.AF_INET6 else '0.0.0.0',port));s.listen();listeners.append(s)
            def loop():
                while True:
                    try:c,_=s.accept();c.settimeout(1);c.recv(16);c.sendall(b'OK');c.close()
                    except OSError:pass
            threading.Thread(target=loop,daemon=True).start()
        for family in (socket.AF_INET,socket.AF_INET6):
            for port in (22,443):serve(family,port)
        def probe(label,dst,port,expected,src='8.8.8.8'):
            child.stdin.write(json.dumps({'dst':dst,'port':port,'src':src})+'\n');child.stdin.flush()
            actual=json.loads(child.stdout.readline())['ok'];assert actual==expected,(label,actual,expected)
            print(json.dumps({'check':label,'passed':True}),flush=True)
        probe('active443',ACTIVE,443,True)
        probe('reserve443',NEW,443,False)
        probe('reserveSSH22',NEW,22,True)
        probe('ipv6VPN443','fd00:77::1',443,False,src='fd00:77::2')
        probe('ipv6SSH22','fd00:77::1',22,True,src='fd00:77::2')
        probe('controllerReserve443',NEW,443,False,src=CONTROL)
        probe('scannerSSH22',ACTIVE,22,False,src='185.224.228.10')
        override=copy.deepcopy(policy); override['controllerIp']='185.224.228.10';apply(override,True)
        probe('trustedScannerOverrideSSH22',ACTIVE,22,True,src='185.224.228.10')
        probe('trustedScannerStillReserve443',NEW,443,False,src='185.224.228.10')
        moved=copy.deepcopy(override);moved['activeIp']=NEW;apply(moved,True)
        probe('retargetNewActive443',NEW,443,True)
        probe('retargetOldBlocked443',ACTIVE,443,False)
        # No output hook exists; a server-initiated connection to a client HTTP listener must pass.
        client('python3','-c','import socket,threading,os; s=socket.socket();s.bind(("10.77.0.2",18080));s.listen();p=os.fork();(os._exit(0) if p else None);null=os.open("/dev/null",os.O_RDWR);os.dup2(null,0);os.dup2(null,1);os.dup2(null,2);c,a=s.accept();c.sendall(b"OUTBOUND_OK");c.close()')
        s=socket.create_connection(('10.77.0.2',18080),timeout=2);assert s.recv(32)==b'OUTBOUND_OK';s.close()
        print(json.dumps({'check':'outboundPreserved','passed':True}),flush=True)
        print(json.dumps({'functionalPacketChecks':'PASS'}),flush=True)
    finally:
        child.terminate()
        try:child.wait(timeout=3)
        except subprocess.TimeoutExpired:child.kill();child.wait()


@unittest.skipUnless(sys.platform.startswith('linux') and os.geteuid() == 0
                     and os.environ.get('SPN_RUN_NETNS_TESTS') == '1',
                     'Opt-in Linux root network namespace integration test')
class RealNodeGuardNamespaceTests(unittest.TestCase):
    def test_nft_json_and_real_packets_in_two_isolated_namespaces(self):
        completed = subprocess.run(['unshare', '--net', sys.executable, '-u', str(Path(__file__).resolve()),
                                    '--inside-netns'], text=True, capture_output=True, timeout=45)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        observations = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(sum(value.get('roundtrip') is True for value in observations), 3)
        self.assertEqual(sum(value.get('passed') is True for value in observations), 12)
        self.assertIn({'functionalPacketChecks': 'PASS'}, observations)


if __name__ == '__main__':
    if sys.argv[1:] == ['--inside-netns']:
        run_validation()
    else:
        unittest.main()
