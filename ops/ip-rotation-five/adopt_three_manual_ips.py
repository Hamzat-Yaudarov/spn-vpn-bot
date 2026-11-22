#!/usr/bin/env python3
"""Operator for three user-confirmed manual additions. No billing/panel writes."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import sys

import deploy_managed_access as setup

SCOPE = {
    '9b0316d5-fa83-4bf8-b6dc-8202b66622c9': ('87.251.16.245', '87.251.16.37', 23003),
    '626633f5-3bf3-48c7-aaba-9bbf43f8ea9d': ('87.251.16.241', '191.217.252.35', 23004),
    '4d6f358a-e75f-4dd8-a635-c6ad320f2732': ('177.3.248.30', '87.121.47.143', 23005),
}
OUTPUT = setup.STATE / 'manual-three-20261116'
READ = r'''
import importlib.util,json,pathlib,stat,sys
p=json.loads(sys.stdin.buffer.read(4096));sid=p['service_id'];root=pathlib.Path('/opt/spn-ip-failover-guest')
for path in [root,root/'enroll_netplan.py']:
 m=path.lstat();assert m.st_uid==0 and not m.st_mode&0o022 and (stat.S_ISDIR(m.st_mode)if path==root else stat.S_ISREG(m.st_mode))
assert (root/'.service-id').read_text().strip()==sid
spec=importlib.util.spec_from_file_location('manual_enroll',root/'enroll_netplan.py');e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e)
h,b,n=e.load_modules(root)
cfg=h.record(pathlib.Path('/etc/spn-ip-failover-guest.json'));v=json.loads(h.body(cfg,mode=0o600));state=pathlib.Path(v['state_directory'])
assert v['server_id']==sid
value={'format':1,'service_id':sid,'proof':p,'config':cfg,'state':h.record(state/'state.json'),
 'history':h.record(state/'netplan-generated-history.json',absent=True),
 'backup':h.record(state/'bootstrap-original.interfaces'),'current':e.capture(sid,p['new_ip'],h,n),
 'networkctl':json.loads(h.run('/usr/bin/networkctl','--json=short','status',v['interface'])),
 'actual_mac':pathlib.Path('/sys/class/net/'+v['interface']+'/address').read_text().strip(),
 'egress':h.run('/usr/bin/curl','--ipv4','--noproxy','*','--interface',p['new_ip'],'--max-time','15','--silent','--show-error','--fail','https://api.ipify.org')}
print(json.dumps(value,sort_keys=True))
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--service', choices=SCOPE)
    p.add_argument('--capture', action='store_true')
    args = p.parse_args()
    if not args.capture:
        print(json.dumps({'plan_only':True,'network_used':False,'services':list(SCOPE)}));return
    OUTPUT.mkdir(mode=0o700,exist_ok=True)
    assert OUTPUT.stat().st_mode&0o777==0o700
    with setup.browser() as page:
        page.route('**/*',lambda r:r.continue_()if r.request.method in ('GET','HEAD','OPTIONS')else r.abort())
        for row in setup.access.scope.SELECTED:
            sid=row['service_id']
            if sid not in SCOPE or args.service and sid!=args.service:continue
            old,new,port=SCOPE[sid]
            print(json.dumps({'capture':row['key']}),flush=True)
            assigned = {new:True} if sid=='4d6f358a-e75f-4dd8-a635-c6ad320f2732' else {old:True,new:False}
            with setup.Guest(page,row,expected_addresses=assigned,reverse_port=port) as guest:
                proof={'service_id':sid,'old_ip':old,'new_ip':new,'provider':{r.ip:r.primary for r in guest.observed_addresses},
                       'observed_at':datetime.now(timezone.utc).isoformat(),'user_confirmed_manual_change':True,
                       'old_addresses_preserved':True,'no_orders_or_deletes':True}
                raw=guest.run(shlex.join(['python3','-I','-c',READ]),setup.encoded(proof),timeout=180)
                target=OUTPUT/(row['key']+'.capture-'+hashlib.sha256(raw).hexdigest()[:16]+'.json')
                setup.access.private_write(target,raw)
                v=json.loads(raw)
                print(json.dumps({'captured':row['key'],'file':str(target),'egress':v['egress'],
                                  'live_ipv4':[{x['ifname']:[a['local']for a in x.get('addr_info',[])if a.get('family')=='inet']}for x in v['current']['diagnostic']['network']['addresses']],
                                  'state':v['networkctl'].get('AdministrativeState')}),flush=True)


if __name__=='__main__':
    try:main()
    except Exception as e:
        print('Manual setup stopped: '+(str(e)if isinstance(e,RuntimeError)else type(e).__name__),file=sys.stderr)
        raise SystemExit(1)
