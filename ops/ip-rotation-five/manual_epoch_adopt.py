#!/usr/bin/env python3
"""Three fixed user-confirmed manual IP adoptions. No billing or VPN edits."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import types

SCOPE = {
 '9b0316d5-fa83-4bf8-b6dc-8202b66622c9': ('87.251.16.245','87.251.16.37'),
 '626633f5-3bf3-48c7-aaba-9bbf43f8ea9d': ('87.251.16.241','191.217.252.35'),
 '4d6f358a-e75f-4dd8-a635-c6ad320f2732': ('177.3.248.30','87.121.47.143'),
}
PREFIX = Path('/opt/spn-ip-failover-guest')
CONFIG = Path('/etc/spn-ip-failover-guest.json')
ARCHIVES = Path('/var/lib/spn-ip-failover-manual-adopt')
EPOCHS = Path('/var/lib/spn-ip-failover-guest-epochs')
ENV = {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','HOME':'/nonexistent','LC_ALL':'C'}

def need(ok,message):
 if not ok:raise ValueError(message)

def encoded(v):return (json.dumps(v,sort_keys=True,separators=(',',':'))+'\n').encode()
def sha(v):return hashlib.sha256(v).hexdigest()

def run(argv,timeout=30):
 r=subprocess.run(argv,stdin=subprocess.DEVNULL,capture_output=True,env=ENV,timeout=timeout,check=False)
 need(r.returncode==0 and len(r.stdout)<262144,'Fixed adoption command failed')
 return r.stdout.decode().strip()

def runtime():
 path=PREFIX/'enroll_netplan.py'
 for parent in path.parents:
  m=parent.lstat();need(stat.S_ISDIR(m.st_mode)and m.st_uid==0 and not m.st_mode&0o022,'Untrusted source parent')
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 with os.fdopen(fd,'rb')as f:
  m=os.fstat(f.fileno());need(stat.S_ISREG(m.st_mode)and m.st_uid==0 and m.st_nlink==1 and not m.st_mode&0o022,'Untrusted enrollment loader')
  raw=f.read(262145);need(0<len(raw)<=262144,'Source exceeds limit')
 e=types.ModuleType('_manual_adopt_enroll');e.__file__=str(path)
 exec(compile(raw,str(path),'exec'),e.__dict__)
 h,b,n=e.load_modules(PREFIX)
 return e,h,b,n

def directories(h,path):
 for parent in reversed([path,*path.parents]):
  if parent==Path('/'):continue
  if not parent.exists():parent.mkdir(mode=0o700)
  m=parent.lstat();need(stat.S_ISDIR(m.st_mode)and m.st_uid==0 and not m.st_mode&0o022,'Untrusted archive parent')
 h.safe_parents(path/'child')

def proof(v,service):
 need(service in SCOPE and isinstance(v,dict),'Outside manual adoption scope')
 old,new=SCOPE[service]
 assigned={new:True}if service=='4d6f358a-e75f-4dd8-a635-c6ad320f2732'else{old:True,new:False}
 need(set(v)=={'service_id','old_ip','new_ip','provider','observed_at','user_confirmed_manual_change',
               'old_addresses_preserved','no_orders_or_deletes'} and v['service_id']==service
      and v['old_ip']==old and v['new_ip']==new and v['provider']==assigned
      and v['user_confirmed_manual_change']is True and v['old_addresses_preserved']is True
      and v['no_orders_or_deletes']is True,'Fresh provider/user authorization differs')
 observed=datetime.fromisoformat(v['observed_at'])
 need(observed.tzinfo is not None and observed.utcoffset().total_seconds()==0
      and -30<=(datetime.now(timezone.utc)-observed).total_seconds()<=600,'Provider proof is stale')
 return old,new,set(assigned)

def prepare(s,rt):
 e,h,b,n=rt;service=s['service_id'];old,new,owned=proof(s['proof'],service)
 need(set(s)=={'format','service_id','proof','config','state','history','backup','current','networkctl','actual_mac','egress'}
      and s['format']==1 and s['egress']==new,'Unexpected capture/egress')
 cfg=json.loads(h.body(s['config'],mode=0o600));oldsettings=n.Settings.from_value(cfg)
 state=json.loads(h.body(s['state'],mode=0o600));n.ManagedGuest(oldsettings,None,None).validate_state(state)
 oldallocation={'id':old,'ip':old}
 need(oldsettings.server_id==service and oldsettings.state_directory==Path('/var/lib/spn-ip-failover-guest')
      and state['active']==oldallocation and state['allocations']==[oldallocation]
      and state['pending']is None and state['retired']==[], 'Original guest epoch has unreviewed operations')
 c=s['current'];guard=c['netplan_guard'];interface=oldsettings.interface;gateway=oldsettings.gateway
 need(interface=='eth0'and gateway=='10.0.0.1'and c['service_id']==service and c['ip']==new
      and s['actual_mac']==guard['macaddress']
      and all(guard[k]==oldsettings.netplan_guard[k]for k in ('macaddress','networkd_inputs','cloud_init_file_sha256'))
      and h.body(c['cloud_init'],mode=0o600)==n.CLOUD_DISABLED,'Network ownership changed beyond manual IPv4')
 data=c['diagnostic'];net=data['network'];managers=data['managers']
 need(data['interfaces']is None and managers['systemd-networkd']=='active'
      and managers['networking']in ('inactive','failed','unknown')
      and managers['NetworkManager']in ('inactive','failed','unknown'),'Wrong network manager')
 links=[x for x in net['addresses']if x.get('ifname')==interface]
 need(len(links)==1 and 'UP'in links[0].get('flags',[]),'Interface is not uniquely UP')
 ips=[x for x in links[0].get('addr_info',[])if x.get('family')=='inet']
 need(len(ips)==len(owned)and {x.get('local')for x in ips}==owned
      and all(x.get('prefixlen')==32 for x in ips),'Live IPv4 differs from exact current provider set')
 need(not any(a.get('family')=='inet'and a.get('local')in owned for x in net['addresses']
              if x.get('ifname')!=interface for a in x.get('addr_info',[])),'Owned IPv4 duplicated on another interface')
 need(len(net['default'])==1 and len(net['selected'])==1,'Ambiguous route')
 route=net['default'][0];selected=net['selected'][0]
 need(route.get('dev')==interface and route.get('gateway')==gateway and route.get('flags')==['onlink']
      and route.get('prefsrc',new)in owned and route.get('protocol','boot')in ('boot',3,'static',4)
      and route.keys()<={'dst','dev','gateway','flags','prefsrc','protocol','scope','type','table'}
      and route.get('dst')=='default'and route.get('table','main')in ('main',254)
      and route.get('type','unicast')=='unicast'and route.get('scope','global')in ('global',0)
      and selected.get('dev')==interface and selected.get('gateway')==gateway
      and (selected.get('prefsrc')or selected.get('src'))in owned,'Current route is not owned')
 need([(x.get('priority'),x.get('table'),x.get('src'))for x in net['rules']]
      ==[(0,'local','all'),(32766,'main','all'),(32767,'default','all')],'Unexpected policy rules')
 raw=h.body(c['netplan'],mode=0o600);generated=h.body(c['generated'])
 value=n.parse_yaml(raw);eth=value['network']['ethernets'][interface]
 need(eth.get('dhcp4')is False and {a for a in eth['addresses']if ':'not in a}=={ip+'/32'for ip in owned},'Saved IP set differs')
 defaults=[r for r in eth['routes']if ':'not in r['via']]
 need(len(defaults)==1 and defaults[0].get('from')in (old,new,None),'Foreign preferred source')
 expected=deepcopy(value);v4=[new]+sorted(owned-{new},key=IPv4Address)
 expected['network']['ethernets'][interface]['addresses']=[ip+'/32'for ip in v4]+[a for a in eth['addresses']if ':'in a]
 [r for r in expected['network']['ethernets'][interface]['routes']if ':'not in r['via']][0]['from']=new
 candidate=n.yaml_module().safe_dump(expected,sort_keys=False,allow_unicode=True).encode()
 epoch=sha(encoded(s));fresh={**cfg,'state_directory':str(EPOCHS/service/epoch),'netplan_guard':guard,
   'bootstrap':{'expected_config_sha256':sha(raw),'expected_live_ipv4':v4,
                'active':{'id':new,'ip':new},'allocations':[{'id':ip,'ip':ip}for ip in v4]}}
 settings=n.Settings.from_value(fresh);after=n.generate_candidate(candidate,settings)
 status=s['networkctl'];reload_required=status.get('AdministrativeState')!='configured'
 need(status.get('Name')==interface and status.get('NetworkFile')==str(Path('/run/systemd/network')/('10-netplan-'+interface+'.network'))
      and status.get('NetworkFileDropins')==[] and status.get('IPv4AddressState')=='routable','Unexpected networkd identity')
 if reload_required:
  pending=[r for r in status.get('Routes',[])if r.get('Family')==2 and r.get('ConfigState')!='configured']
  need(service=='4d6f358a-e75f-4dd8-a635-c6ad320f2732'and status.get('AdministrativeState')=='configuring'
       and len(pending)==1 and pending[0].get('ConfigState')=='requesting'
       and pending[0].get('ConfigSource')=='static'and pending[0].get('Destination')==[0,0,0,0]
       and pending[0].get('Table')==254 and pending[0].get('Type')==1
       and pending[0].get('PreferredSource')==list(IPv4Address(old).packed)
       and pending[0].get('Gateway')==[10,0,0,1]and pending[0].get('DestinationPrefixLength')==0,
       'Unreviewed pending IPv4 request')
  fresh['bootstrap']['expected_config_sha256']=sha(candidate)
  fresh['netplan_guard']={**guard,'generated_network_sha256':sha(after)}
  settings=n.Settings.from_value(fresh)
 else:
  need(n.generate_candidate(raw,settings)==generated,'Running generation differs from captured current YAML')
 need(n.canonical_netplan(candidate,settings,fresh['bootstrap']['allocations'],fresh['bootstrap']['active'])==candidate,
      'Candidate is not the native canonical render')
 return {'epoch':epoch,'service':service,'old':old,'new':new,'owned':owned,'oldsettings':oldsettings,
         'settings':settings,'fresh':fresh,'yaml':candidate,'generated':after,'reload_required':reload_required}

def unchanged(s,rt,allow_fresh=None):
 _e,h,_b,_n=rt;cfg=json.loads(h.body(s['config'],mode=0o600));state=Path(cfg['state_directory'])
 need(h.body(h.record(CONFIG),mode=0o600)in (h.body(s['config'],mode=0o600),allow_fresh),'Guest config pointer changed')
 for path,record in ((state/'state.json',s['state']),(state/'bootstrap-original.interfaces',s['backup'])):
  need(h.record(path)==record,'Old guest journal/backup changed')
 need(h.record(state/'netplan-generated-history.json',absent=True)==s['history'],'Old generation history changed')

def watchdog(s,archive,rt):
 e,h,b,n=rt;sid=s['service_id'];old,new=SCOPE[sid]
 if (archive/'complete.json').exists():return {'already_complete':True}
 fresh=h.body(h.record(archive/'fresh-config.json'),mode=0o600)
 unchanged(s,rt,fresh)
 c=s['current'];cfg=json.loads(fresh);settings=n.Settings.from_value(cfg)
 guard=n.capture_guard(settings.interface,settings.interfaces_file)
 need(all(guard[k]==c['netplan_guard'][k]for k in ('macaddress','networkd_inputs','cloud_init_file_sha256'))
      and Path('/sys/class/net/eth0/address').read_text().strip()==guard['macaddress'],'Rollback identity drift')
 raw=h.body(h.record(settings.interfaces_file),mode=0o600)
 need(raw in (h.body(c['netplan'],mode=0o600),h.body(h.record(archive/'candidate.yaml'),mode=0o600)),'Rollback YAML is foreign')
 links=json.loads(run(['/usr/sbin/ip','-j','-4','address','show','dev',settings.interface]))
 assigned={r['local']for x in links for r in x.get('addr_info',[])if r.get('family')=='inet'}
 need(assigned<=set(s['proof']['provider']),'Rollback refuses foreign added IPs')
 for ip in s['proof']['provider']:
  if ip not in assigned:run(['/usr/sbin/ip','-4','address','replace',ip+'/32','dev',settings.interface])
 run(['/usr/sbin/ip','-4','route','replace','default','via',settings.gateway,'dev',settings.interface,'proto','static','onlink','src',new])
 h.write_once(archive/'watchdog-ran.json',encoded({'service_id':sid,'restored_source':new,'old_journals_preserved':True}),b)
 return {'restored_source':new}

def apply(s,p,rt):
 e,h,b,n=rt;sid=p['service'];archive=ARCHIVES/sid/p['epoch'];directories(h,archive)
 fresh=encoded(p['fresh']);oldraw=h.body(s['config'],mode=0o600);current=s['current']
 oldstore=n.FileStore(p['oldsettings'])
 with oldstore.locked():
  unchanged(s,rt,fresh)
  h.write_once(archive/'snapshot.json',encoded(s),b);h.write_once(archive/'fresh-config.json',fresh,b)
  h.write_once(archive/'candidate.yaml',p['yaml'],b);h.write_once(archive/'candidate.network',p['generated'],b)
  h.write_once(archive/'intent.json',encoded({'service_id':sid,'epoch':p['epoch'],'ip':p['new'],
               'retained_provider_ips':sorted(p['owned']),'billing_journals_untouched':True}),b)
  unit='spn-manual-adopt-'+p['epoch'][:16]+'-rollback'
  run(['/usr/bin/systemd-run','--quiet','--unit='+unit,'--on-active=120s','--timer-property=AccuracySec=1s',
       '--property=Type=oneshot','/usr/bin/python3','-I',str(PREFIX/'manual_epoch_adopt.py'),'watchdog','--service',sid,'--epoch',p['epoch']])
  try:
   need(h.body(h.record(p['oldsettings'].interfaces_file),mode=0o600)==h.body(current['netplan'],mode=0o600),'Netplan changed since freeze')
   need(n.capture_guard('eth0',p['oldsettings'].interfaces_file)==current['netplan_guard'],'Network ownership changed since freeze')
   if p['reload_required']:
    b.atomic_write(p['oldsettings'].interfaces_file,p['yaml'],0o600)
    network=Path('/run/systemd/network/10-netplan-eth0.network')
    b.atomic_write(network,p['generated'],current['generated']['mode'],group=current['generated']['gid'])
    run(['/usr/bin/networkctl','reload']);run(['/usr/bin/networkctl','reconfigure','eth0'])
   kernel=n.SystemKernel(p['settings']);deadline=time.monotonic()+40
   while True:
    try:
     live=kernel.snapshot();need(set(live['ips'])==p['owned'],'Live IP set changed');break
    except Exception:
     if time.monotonic()>deadline:raise
     time.sleep(2)
   if live['source']!=p['new']:kernel.route(live['route'],p['new'])
   need(h.run('/usr/bin/curl','--ipv4','--noproxy','*','--max-time','15','--silent','--show-error','--fail','https://api.ipify.org')==p['new'],'Default egress source did not change')
   directories(h,p['settings'].state_directory)
   store=n.FileStore(p['settings']);guest=n.ManagedGuest(p['settings'],store,kernel)
   with store.locked():
    guest.run({'operation':'bootstrap','server_id':sid,'apply':True})
    need(guest.run({'operation':'check','server_id':sid})=={'ok':True},'New epoch failed strict readiness')
   unchanged(s,rt,fresh)
   def cas():need(h.body(h.record(CONFIG),mode=0o600)==oldraw,'Config pointer changed before commit')
   b.atomic_write(CONFIG,fresh,0o600,before_replace=cas)
   need(h.body(h.record(CONFIG),mode=0o600)==fresh,'Config readback differs')
   need(guest.run({'operation':'check','server_id':sid})=={'ok':True},'Published guest failed readiness')
   result={'ok':True,'service_id':sid,'active_ip':p['new'],'retained_provider_ips':sorted(p['owned']),
           'epoch':p['epoch'],'state_directory':str(p['settings'].state_directory),'old_journals_preserved':True,
           'network_reconfigured':p['reload_required'],'vpn_restarted':False,'billing_journals_untouched':True}
   h.write_once(archive/'complete.json',encoded(result),b);run(['/usr/bin/systemctl','stop',unit+'.timer'])
   return result
  except Exception:
   watchdog(s,archive,rt);raise

def main():
 a=argparse.ArgumentParser(description=__doc__);a.add_argument('action',nargs='?',choices=('plan','apply','watchdog'))
 a.add_argument('--service',choices=SCOPE);a.add_argument('--epoch');args=a.parse_args()
 if args.action is None:print(json.dumps({'plan_only':True,'network_used':False}));return
 need(sys.platform.startswith('linux')and os.geteuid()==0,'Apply only as Linux root on selected guest')
 rt=runtime();e,h,b,n=rt
 need(args.service in SCOPE and (PREFIX/'.service-id').read_text().strip()==args.service,'Wrong helper service')
 if args.action=='watchdog':
  need(args.epoch is not None and re.fullmatch('[0-9a-f]{64}',args.epoch),'Invalid rollback epoch')
  archive=ARCHIVES/args.service/args.epoch;s=json.loads(h.body(h.record(archive/'snapshot.json'),mode=0o600))
  need(sha(encoded(s))==args.epoch,'Frozen snapshot changed');print(json.dumps(watchdog(s,archive,rt)));return
 raw=sys.stdin.buffer.read(1000001);need(0<len(raw)<=1000000,'Invalid frozen snapshot')
 s=json.loads(raw);need(s['service_id']==args.service,'CLI/snapshot service mismatch');p=prepare(s,rt)
 if args.action=='plan':print(json.dumps({'plan_only':True,'service_id':args.service,'active_ip':p['new'],
  'retained_provider_ips':sorted(p['owned']),'epoch':p['epoch'],'network_reconfigure':p['reload_required']}));return
 print(json.dumps(apply(s,p,rt)))

if __name__=='__main__':
 try:main()
 except Exception as exc:
  print('Manual epoch adoption stopped: '+str(exc),file=sys.stderr);raise SystemExit(1)
