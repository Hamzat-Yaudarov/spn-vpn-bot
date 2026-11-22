import base64
from contextlib import nullcontext
import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from spn_ip_failover import managed_guest as b, netplan_guest as n

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v);return v
m=load('manual_adopt_tests',ROOT/'ops/ip-rotation-five/manual_epoch_adopt.py')
h=load('manual_adopt_helper_tests',ROOT/'ops/ip-rotation-five/guest_enrollment.py')
SID=next(iter(m.SCOPE));MAC='52:54:00:ea:40:61'

def record(raw):return {'uid':0,'gid':0,'mode':0o600,'size':len(raw),'sha256':m.sha(raw),'base64':base64.b64encode(raw).decode()}

def snapshot(sid=SID):
 old,new=m.SCOPE[sid];owned=[new]if sid.startswith('4d6f')else[new,old]
 yaml={'network':{'version':2,'ethernets':{'eth0':{'addresses':[ip+'/32'for ip in owned]+['2001:4860::2/64'],
  'dhcp4':False,'match':{'macaddress':MAC},'set-name':'eth0','nameservers':{'addresses':['1.1.1.1','2606:4700::1111']},
  'routes':[{'to':'0.0.0.0/0','via':'10.0.0.1','on-link':True,'from':old},{'to':'::/0','via':'2001:4860::1','on-link':True}]}}}}
 raw=n.yaml_module().safe_dump(yaml,sort_keys=False).encode();guard={'macaddress':MAC,'networkd_inputs':{},
  'generated_network_sha256':m.sha(b'generated'),'cloud_init_file_sha256':m.sha(n.CLOUD_DISABLED)}
 cfg={'version':1,'server_id':sid,'interface':'eth0','gateway':'10.0.0.1','method':'netplan',
  'interfaces_file':'/etc/netplan/50-cloud-init.yaml','state_directory':'/var/lib/spn-ip-failover-guest',
  'bootstrap':{'expected_config_sha256':'a'*64,'expected_live_ipv4':[old],'active':{'id':old,'ip':old},'allocations':[{'id':old,'ip':old}]},'netplan_guard':guard}
 state={'version':1,'server_id':sid,'interface':'eth0','gateway':'10.0.0.1','active':{'id':old,'ip':old},
  'allocations':[{'id':old,'ip':old}],'retired':[],'pending':None,'config_sha256':'b'*64,
  'bootstrap_original_sha256':'a'*64,'route':{'dst':'default','dev':'eth0','gateway':'10.0.0.1','flags':['onlink'],'protocol':'static','prefsrc':old}}
 route={'dst':'default','dev':'eth0','gateway':'10.0.0.1','flags':['onlink']}
 if old in owned:route.update(protocol='static',prefsrc=old)
 diagnostic={'interface':'eth0','interfaces':None,'managers':{'systemd-networkd':'active','networking':'inactive','NetworkManager':'inactive'},
  'network':{'addresses':[{'ifname':'eth0','flags':['UP'],'addr_info':[{'family':'inet','local':ip,'prefixlen':32}for ip in owned]}],
   'default':[route],'selected':[{'dev':'eth0','gateway':'10.0.0.1','prefsrc':old if old in owned else new}],
   'rules':[{'priority':p,'table':t,'src':'all'}for p,t in ((0,'local'),(32766,'main'),(32767,'default'))]}}
 status={'Name':'eth0','NetworkFile':'/run/systemd/network/10-netplan-eth0.network','NetworkFileDropins':[],
  'IPv4AddressState':'routable','AdministrativeState':'configuring'if sid.startswith('4d6f')else'configured','Routes':[]}
 if sid.startswith('4d6f'):
  status['Routes']=[{'Family':2,'ConfigState':'requesting','ConfigSource':'static','Destination':[0,0,0,0],
   'Gateway':[10,0,0,1],'DestinationPrefixLength':0,'PreferredSource':list(m.IPv4Address(old).packed),'Table':254,'Type':1}]
 return {'format':1,'service_id':sid,'proof':{'service_id':sid,'old_ip':old,'new_ip':new,
  'provider':{new:True}if sid.startswith('4d6f')else{old:True,new:False},'observed_at':datetime.now(timezone.utc).isoformat(),
  'user_confirmed_manual_change':True,'old_addresses_preserved':True,'no_orders_or_deletes':True},
  'config':record(m.encoded(cfg)),'state':record(m.encoded(state)),'history':None,'backup':record(b'old-backup'),
  'current':{'service_id':sid,'ip':new,'netplan_guard':guard,'cloud_init':record(n.CLOUD_DISABLED),
   'diagnostic':diagnostic,'netplan':record(raw),'generated':record(b'generated')},'networkctl':status,'actual_mac':MAC,'egress':new}

class ManualEpochTests(unittest.TestCase):
 def plan(self,s):
  with patch.object(n,'generate_candidate',return_value=b'generated'):
   return m.prepare(s,(None,h,b,n))

 def test_two_assigned_ips_are_both_retained_active_is_new_old_journal_unchanged(self):
  for sid in list(m.SCOPE)[:2]:
   s=snapshot(sid);before=copy.deepcopy(s);p=self.plan(s);old,new=m.SCOPE[sid]
   self.assertEqual(s,before);self.assertEqual(p['owned'],{old,new});self.assertFalse(p['reload_required'])
   self.assertEqual(p['fresh']['bootstrap']['active'],{'id':new,'ip':new})
   self.assertEqual({r['ip']for r in p['fresh']['bootstrap']['allocations']},{old,new})
   self.assertNotEqual(p['fresh']['state_directory'],json.loads(h.body(s['config']))['state_directory'])
   result=n.parse_yaml(p['yaml']);original=n.parse_yaml(h.body(s['current']['netplan']))
   original['network']['ethernets']['eth0']['routes'][0]['from']=new
   self.assertEqual(result,original)

 def test_germany_singleton_never_recreates_deleted_old_and_requires_reload(self):
  sid=list(m.SCOPE)[2];s=snapshot(sid);p=self.plan(s);old,new=m.SCOPE[sid]
  self.assertEqual(p['owned'],{new});self.assertTrue(p['reload_required'])
  self.assertEqual(p['fresh']['bootstrap']['expected_live_ipv4'],[new])
  self.assertNotIn(old+'/32',p['yaml'].decode())
  self.assertEqual(p['fresh']['bootstrap']['expected_config_sha256'],m.sha(p['yaml']))

 def test_wrong_service_provider_primary_extra_ips_or_old_pending_is_rejected(self):
  changes=[lambda s:s['proof']['provider'].update({'8.8.8.8':False}),
   lambda s:s['proof']['provider'].update({m.SCOPE[SID][0]:False}),
   lambda s:s.update(egress=m.SCOPE[SID][0]),lambda s:s.update(actual_mac='00:00:00:00:00:00')]
  for change in changes:
   s=snapshot();change(s)
   with self.assertRaises(ValueError):self.plan(s)
  s=snapshot();state=json.loads(h.body(s['state']));state['pending']={'foreign':True};s['state']=record(m.encoded(state))
  with self.assertRaises(ValueError):self.plan(s)

 def test_foreign_generated_or_nonstatic_extra_live_ip_is_rejected(self):
  s=snapshot();s['current']['generated']=record(b'foreign')
  with self.assertRaises(ValueError):self.plan(s)
  s=snapshot();s['current']['diagnostic']['network']['addresses'][0]['addr_info'].append({'family':'inet','local':'8.8.8.8','prefixlen':32})
  with self.assertRaises(ValueError):self.plan(s)

 def test_de_pending_request_must_be_exactly_stale_old_source(self):
  for field,value in [('PreferredSource',[8,8,8,8]),('Gateway',[10,0,0,2]),('ConfigState','configuring'),('ConfigSource','dhcp4')]:
   s=snapshot(list(m.SCOPE)[2]);s['networkctl']['Routes'][0][field]=value
   with self.assertRaises(ValueError):self.plan(s)

 def test_default_cli_is_local_and_network_free(self):
  with patch.object(m.sys,'argv',['manual_epoch_adopt.py']),patch.object(m,'runtime')as runtime,patch('builtins.print')as emit:m.main()
  runtime.assert_not_called();self.assertFalse(json.loads(emit.call_args.args[0])['network_used'])

 def test_apply_arms_watchdog_before_route_and_commits_only_after_new_guest_checks(self):
  s=snapshot();p=self.plan(s);events=[]
  helper=SimpleNamespace(body=h.body,record=Mock(side_effect=lambda path:s['current']['netplan']if path==p['oldsettings'].interfaces_file else s['config']),
   write_once=lambda path,*_:events.append('freeze:'+path.name),run=Mock(return_value=p['new']))
  oldstore=SimpleNamespace(locked=lambda:nullcontext())
  newstore=SimpleNamespace(locked=lambda:nullcontext())
  guest=SimpleNamespace(run=lambda request:events.append(request['operation'])or{'ok':True})
  kernel=SimpleNamespace(snapshot=lambda:{'ips':sorted(p['owned']),'source':p['old'],'route':{'old':True}},
   route=lambda *_:events.append('route-new'))
  network=SimpleNamespace(FileStore=Mock(side_effect=[oldstore,newstore]),SystemKernel=Mock(return_value=kernel),
    capture_guard=Mock(return_value=s['current']['netplan_guard']),ManagedGuest=Mock(return_value=guest))
  fresh=m.encoded(p['fresh'])
  def atomic(path,raw,*a,**kw):events.append('commit-config');helper.record.return_value=record(fresh);helper.record.side_effect=None
  managed=SimpleNamespace(atomic_write=atomic)
  def fixedrun(argv,**_):events.append('timer'if argv[0].endswith('systemd-run')else'stop-timer');return''
  with patch.object(m,'directories'),patch.object(m,'unchanged'),patch.object(m,'run',side_effect=fixedrun):
   result=m.apply(s,p,(None,helper,managed,network))
  self.assertLess(events.index('freeze:snapshot.json'),events.index('timer'))
  self.assertLess(events.index('timer'),events.index('route-new'))
  self.assertLess(events.index('check'),events.index('commit-config'))
  self.assertTrue(result['old_journals_preserved']);self.assertFalse(result['network_reconfigured'])

if __name__=='__main__':unittest.main()
