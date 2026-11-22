#!/usr/bin/env python3
"""Read provider/panel/our financial state and freeze the confirmed manual move."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

import capture_guests as access


SERVICE = "f5c0287a-636e-4981-95c7-80b24c9559c0"
OLD, NEW = "177.3.250.133", "177.3.250.179"
FINANCIAL_SOURCE = r'''
import base64,copy,hashlib,json,re
from decimal import Decimal
from types import SimpleNamespace
from spn_ip_failover.budget import AccountBudget, money
from spn_ip_failover.onecent import OneCentBilling
from spn_ip_failover.provider import Allocation
from spn_ip_failover.five_scope import SERVERS, DEPLOYMENT

SWEDEN='f5c0287a-636e-4981-95c7-80b24c9559c0'
PILOT_REQUEST='five-vps-pilot-20261116-001'

def need(ok,message):
 if not ok:raise ValueError(message)

def digest(raw):return hashlib.sha256(raw).hexdigest()

def financial_summary(settings,orders_record,budget_record,controllers,pilot):
 # This is deliberately one settled reviewed pilot, not a generic exemption
 # for historical orders. No Journal context is opened and no state is saved.
 rows={row['service_id']:row for row in SERVERS}
 orders=orders_record['value'] if orders_record else {'version':1,'servers':{}}
 need(isinstance(orders,dict) and set(orders)=={'version','servers'} and type(orders['version'])is int
      and orders['version']==1 and isinstance(orders['servers'],dict) and set(orders['servers'])<=set(rows),
      'Unknown order journal schema/scope')
 entries=[]
 for sid,server in orders['servers'].items():
  need(isinstance(server,dict) and set(server)=={'orders','releases'}
       and isinstance(server['orders'],dict) and isinstance(server['releases'],dict),'Unknown order service state')
  for request,item in server['orders'].items():
   need(sid!=SWEDEN and request==PILOT_REQUEST and isinstance(item,dict)
        and item.get('request_id')==request and item.get('phase')=='released',
        'Sweden/unknown/unsettled order prevents reconciliation')
   entries.append((sid,request,item,server['releases']))
  need(all(value=='released' for value in server['releases'].values()),'A release is unresolved')
  need(set(server['releases'])=={item.get('ip') for item in server['orders'].values()},
       'Release journal is not exactly the reviewed pilot')
 need(len(entries)<=1,'Only one globally approved pilot may precede this manual move')
 controller_evidence=[]
 for path,record in sorted(controllers.items()):
  if record:
   value=record['value']
   need(isinstance(value,dict) and set(value)=={'version','servers'} and type(value['version'])is int
        and value['version']==1 and isinstance(value['servers'],dict)
        and set(value['servers'])<={row['key'] for row in SERVERS},'Unknown controller journal schema/scope')
   # No rotation has been authorized yet. Monitoring rows are acceptable only
   # with an untouched attempt counter and no pending/retired allocations.
   for row in value['servers'].values():
    need(isinstance(row,dict) and type(row.get('tries'))is int and row['tries']==0
         and 'pending'in row and row['pending']is None and row.get('retired')==[]
         and row.get('rejected_history',[])==[],'Controller attempts/operations require separate review')
  controller_evidence.append({'path':path,'exists':record is not None,
                              'sha256':record['sha256'] if record else None})
 ledger=None
 if budget_record:
  need(settings.budget is not None,'Shared budget configuration missing')
  document=budget_record['value']
  need(isinstance(document,dict) and set(document)=={'version','servers','budget'}
       and type(document['version'])is int and document['version']==1 and document['servers']=={},
       'Unknown shared-budget journal schema')
  ledger=AccountBudget(settings.budget,settings.profile.resolve())._ledger(SimpleNamespace(data=copy.deepcopy(document)))
  need(all(row['phase']=='confirmed' for row in ledger['orders'].values()),'Unsettled/unaccounted gross reservation')
 need(len((ledger or {}).get('orders',{}))==len(entries),'Budget and order counts differ')
 settled=[];pilot_evidence=None
 if entries:
  need(pilot is not None and set(pilot)=={'intent','dispatch','response','proof'},'Reviewed pilot artifacts are missing/ambiguous')
  sid,request,item,releases=entries[0]
  intent,dispatch,response,proof=(pilot[name]['value'] for name in ('intent','dispatch','response','proof'))
  need(type(proof.get('version'))is int and proof=={'version':1,'deployment':DEPLOYMENT,'service_id':sid,'request_id':request,'ip':item.get('ip'),
               'response_sha256':pilot['response']['sha256'],'intent_sha256':pilot['intent']['sha256'],
               'dispatch_sha256':pilot['dispatch']['sha256']},'Reviewed pilot proof does not bind the immutable captures')
  need(type(intent.get('format'))is int and intent=={'format':1,'request_key':request,'service_id':sid,'expected_primary':intent.get('expected_primary'),
                'upfront_cap_rub':'100.00','monthly_cap_rub':'300.00','keep_current_primary':True},'Pilot intent differs')
  Allocation(intent['expected_primary'],intent['expected_primary'])
  need(type(dispatch.get('format'))is int and dispatch=={'format':1,'request_key':request,'service_id':sid,'expected_primary':intent['expected_primary'],
                  'upfront_rub':item.get('upfront_rub'),'monthly_rub':item.get('monthly_rub'),
                  'variant_id':item.get('variant_id'),'ownership_confirmed':False},'Pilot dispatch differs')
  need(item.get('before')=={intent['expected_primary']:True} and item['before'][intent['expected_primary']]is True
       and Decimal('0')<=money(item['upfront_rub'])<=Decimal('100.00')
       and Decimal('0')<=money(item['monthly_rub'])<=Decimal('300.00'),'Pilot primary or price differs')
  normalized=OneCentBilling(settings,sid,None).entry_receipt(item)
  need(releases.get(normalized['ip'])=='released' and item.get('receipt_sha256')==pilot['response']['sha256'],
       'Order/release does not bind the full captured artifact')
  need(type(response.get('http_status'))is int and 200<=response['http_status']<=299
       and type(response.get('format'))is int and response.get('format')==1 and response.get('request_key')==request and response.get('service_id')==sid
       and response.get('expected_primary')==intent['expected_primary']
       and response.get('request')=={'method':'POST','path':'/api/services/'+sid+'/ipv4/add',
                                    'json':{'variantId':item['variant_id']}}
       and response.get('ownership_confirmed')is False and response.get('requires_receipt_schema_review')is True
       and response.get('idempotency_header_captured')is False and 'body_omitted'not in response,
       'Captured pilot response does not describe the exact paid request')
  raw=base64.b64decode(response['body_base64'],validate=True)
  need(0<len(raw)<=262144 and type(response.get('body_bytes'))is int and response['body_bytes']==len(raw)
       and digest(raw)==response.get('body_sha256')==normalized['response_sha256'],
       'Normalized receipt must bind the HTTP BODY, not the full capture artifact')
  budget_key=AccountBudget.key(sid,request);payment=ledger['orders'].get(budget_key)
  need(payment is not None and payment['phase']=='confirmed' and payment['server_id']==sid
       and payment['request_id']==request and payment['amount_rub']==item['upfront_rub']
       and item.get('budget')=={'journal':str(settings.budget.journal.resolve()),'key':budget_key},
       'Pilot gross charge is not bound to the shared ledger')
  settled=[{'service_id':sid,'request_id':request,'ip':normalized['ip'],'phase':'released','release_phase':'released',
            'receipt_sha256':pilot['response']['sha256'],'response_body_sha256':normalized['response_sha256'],
            'budget_key':budget_key,'budget_phase':'confirmed','amount_rub':payment['amount_rub']}]
  pilot_evidence={'service_id':sid,'request_id':request,'ip':normalized['ip'],
                  **{name+'_sha256':pilot[name]['sha256'] for name in ('intent','dispatch','response','proof')}}
 else:need(pilot is None,'A pilot fence without the settled matching order requires review')
 return {'rotation_running':False,'scope_service_id':SWEDEN,'controller_orders':len(entries),
         'pending_orders':0,'global_pending_orders':0,'sweden_attempts':0,
         'orders_journal_exists':orders_record is not None,'orders_journal_sha256':orders_record['sha256'] if orders_record else None,
         'budget_journal_exists':budget_record is not None,'budget_journal_sha256':budget_record['sha256'] if budget_record else None,
         'pilot_fence_exists':pilot is not None,'controller_journals':controller_evidence,
         'settled_other_orders':settled,'pilot_evidence':pilot_evidence}
'''
exec(compile(FINANCIAL_SOURCE, '<read-only-sweden-financial-proof>', 'exec'))

RF_READ = r'''
import hashlib,json,os,pathlib,pwd,stat,subprocess,sys
sys.path.insert(0,'/opt/wayspn')
from spn_ip_failover.remna import Remnawave
from spn_ip_failover.onecent import Settings
from spn_ip_failover.provider import _unique_object
''' + FINANCIAL_SOURCE + r'''
root=pathlib.Path('/opt/wayspn')
assert (root/'.controller-deployment').read_text().strip()=='276abbcb4f334b58ab25d9b2adc9d55c'
settings=Settings.load('/etc/spn-ip-failover/onecent.json')
owner=pwd.getpwnam('spn-failover').pw_uid
observed={}
def record(path):
 path=pathlib.Path(path)
 for parent in reversed(path.parents):
  try:info=parent.lstat()
  except FileNotFoundError:return None
  need(stat.S_ISDIR(info.st_mode) and info.st_uid in (0,owner) and not info.st_mode&0o022,'Untrusted financial parent')
 try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 except FileNotFoundError:return None
 with os.fdopen(fd,'rb')as stream:
  info=os.fstat(stream.fileno())
  need(stat.S_ISREG(info.st_mode) and info.st_uid in (0,owner) and info.st_nlink==1
       and stat.S_IMODE(info.st_mode)==0o600,'Untrusted financial file')
  raw=stream.read(1048577)
 need(0<len(raw)<=1048576,'Financial input exceeds limit')
 return {'sha256':digest(raw),'value':json.loads(raw,object_pairs_hook=_unique_object)}
def read(path):
 value=record(path);observed[pathlib.Path(path)]=value;return value
def inactive():
 for unit in ('spn-ip-failover.service','spn-ip-failover-five.service'):
  result=subprocess.run(['/usr/bin/systemctl','is-active',unit],capture_output=True,text=True,timeout=15,check=False)
  need(result.returncode in (3,4) and result.stdout.strip()in ('inactive','failed','unknown'),'Financial rotation is active/unknown')
inactive()
orders=read(settings.journal/'state.json');budget=read(settings.budget.journal/'state.json')
controllers={str(path):read(path) for path in (pathlib.Path('/var/lib/spn-ip-failover/state.json'),
                                            pathlib.Path('/var/lib/spn-ip-failover/rotation/state.json'))}
pilot_path=pathlib.Path('/var/lib/spn-ip-failover/onecent-pilot-20261116')
pilot=None
pilot_present=pilot_path.exists() or pilot_path.is_symlink()
if pilot_present:
 info=pilot_path.lstat()
 need(stat.S_ISDIR(info.st_mode) and info.st_uid==owner and stat.S_IMODE(info.st_mode)==0o700,'Untrusted pilot fence')
 need(not (pilot_path/'capture-error.json').exists() and not (pilot_path/'capture-error.json').is_symlink(),'Pilot capture is unresolved')
 pilot={name:read(pilot_path/(name+'.json')) for name in ('intent','dispatch','response')}
 pilot['proof']=read(pathlib.Path('/var/lib/spn-ip-failover/priced-pilot-proof.json'))
 need(all(pilot.values()),'Reviewed settled-pilot proof is incomplete')
else:need(read(pathlib.Path('/var/lib/spn-ip-failover/priced-pilot-proof.json'))is None,'Pilot proof has no fence')
financial=financial_summary(settings,orders,budget,controllers,pilot)
inactive()
need(all(record(path)==value for path,value in observed.items()),'Financial state changed during the read-only snapshot')
need((pilot_path.exists() or pilot_path.is_symlink())==pilot_present,'Pilot fence changed during the read-only snapshot')
refresh=json.loads(pathlib.Path('/var/lib/spn-ip-failover/refresh.json').read_text())
token=pathlib.Path('/etc/spn-ip-failover/credentials/remnawave').read_text().strip()
panel=Remnawave(refresh['panel_url'],token)
node=panel.read_node('5c472650-1f93-470a-abdb-0caadef059b7')
host=panel.read_host('7737ba07-6d54-435c-8c01-aea4fe1369b9')
print(json.dumps({'remnawave':{'host_uuid':host['uuid'],'node_uuid':node['uuid'],
 'host_address':host['address'],'node_address':node['address'],'host_node_uuids':host['nodes'],
 'node_connected':node['isConnected'],'host_disabled':host['isDisabled'],'node_disabled':node['isDisabled']},
 'financial':financial}))
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--user-confirmed-manual-change", action="store_true")
    p.add_argument("--billing-config", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    if not args.user_confirmed_manual_change:
        print(json.dumps({"plan_only": True, "network_used": False}))
        return
    output = args.output.resolve(strict=True)
    assert output.stat().st_uid == access.os.getuid() and output.stat().st_mode & 0o777 == 0o700
    row = next(r for r in access.scope.SELECTED if r["service_id"] == SERVICE)
    with access.browser(access.Settings.load(args.billing_config)) as page:
        page.route("**/*", lambda r: r.continue_() if r.request.method in ("GET", "HEAD", "OPTIONS") else r.abort())
        ui = access.OneCentPage(page, SERVICE, "srv-mwfa434s")
        ui.open()
        assigned = ui.inventory()
        assert len(assigned) == 1 and assigned[0].primary and assigned[0].ip == NEW
    known, _alias = access.trust(row, output)
    keys = {p[2] for line in known.read_text().splitlines()
            if len(p := line.split()) == 3 and p[1] == "ssh-ed25519"}
    assert len(keys) == 1
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(next(iter(keys)), validate=True)).digest()).decode().rstrip("=")
    proxy = shlex.join([sys.executable, str(Path(__file__).with_name("physical_connect.py")),
                        "--ip", "185.9.24.53", "--interface", "en0"])
    command = ["/usr/bin/ssh", *access.controller_socket_args(), "-i",
               str(access.ROOT / ".local-state/ip-rotation-20261115/controller_setup_ed25519"),
               "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=15", "-o", "ProxyCommand=" + proxy,
               "root@185.9.24.53", shlex.join(["python3", "-c", RF_READ])]
    result = subprocess.run(command, capture_output=True, check=False, timeout=100)
    if result.returncode:
        raise RuntimeError("Current panel/financial zero-state proof was not confirmed")
    proof = json.loads(result.stdout)
    assert proof["remnawave"]["host_address"] == proof["remnawave"]["node_address"] == NEW
    document = {"format": 1, "service_id": SERVICE, "old_ip": OLD, "new_ip": NEW,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "user_confirmed_manual_change": True,
                "provider": {"service_id": SERVICE, "name": "srv-mwfa434s", "primary_ip": NEW,
                             "assigned_ipv4": [NEW]},
                "ssh": {"trusted_alias": "177.3.250.75", "fingerprint": fingerprint}, **proof}
    raw = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    sha = hashlib.sha256(raw).hexdigest()
    target = output / ("agreement-" + sha[:16] + ".json")
    access.private_write(target, raw)
    print(json.dumps({"agreement": str(target), "sha256": sha, "financial_writes": False}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Manual-move proof not confirmed: " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
