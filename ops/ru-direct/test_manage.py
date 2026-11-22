import base64,copy,importlib.util,json,re,sys,unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('ru_manage',Path(__file__).with_name('manage.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class RoutingTests(unittest.TestCase):
    def setUp(self):self.domains=m.compact(m.SERVICE_DOMAINS)
    def test_boundaries(self):
        regex=re.compile(m.pattern(self.domains)[7:])
        for d in self.domains:
            self.assertTrue(regex.search(d));self.assertTrue(regex.search('api.'+d));self.assertFalse(regex.search(d+'.evil.org'));self.assertFalse(regex.search('not'+d))
    def test_named_apps(self):
        regex=re.compile(m.pattern(self.domains)[7:])
        for d in ['api.vk.com','ws-api.oneme.ru','api.max.ru','id.tbank.ru','online.sberbank.ru','api.ozon.ru','basket-01.wbbasket.ru','api.avito.ru','api.5ka.ru','api.magnit.ru','esia.gosuslugi.ru','api.samokat.ru','api.kuper.ru','api-maps.yandex.ru','music.yandex.ru','market.yandex.ru','hd.kinopoisk.ru','lavka.yandex.ru','eda.yandex.ru','dzen.ru','mail.ru','static.rustore.ru','st.okcdn.ru']:
            self.assertTrue(regex.search(d),d)
    def test_non_ru_stays_proxy(self):
        regex=re.compile(m.pattern(self.domains)[7:])
        for d in ['youtube.com','google.com','gemini.google.com','gstatic.com','telegram.org','t.me','openai.com','instagram.com','whatsapp.net','cloudflare.com','example.com','ubs.com']:
            self.assertFalse(regex.search(d),d)
    def test_no_countrywide_ip_or_tld_bypass(self):
        p=m.app_profile(self.domains,1);self.assertEqual(p['GlobalProxy'],'true');self.assertEqual(p['DirectIp'],m.LAN_IPV4)
        for x in ['geoip:ru','0.0.0.0/0','198.18.0.0/15','240.0.0.0/4']:self.assertNotIn(x,p['DirectIp'])
        self.assertNotIn('ru',self.domains);self.assertNotIn('com',self.domains);self.assertFalse(re.search(p['DirectSites'][0][7:],'random-unlisted.ru'))
    def test_compact_suffixes(self):self.assertEqual(m.compact(['vk.com','api.vk.com','VK.COM']),['vk.com'])
    def test_domain_injection_refused(self):
        for d in ['.ru','*','evil.com\nMATCH,DIRECT','127.0.0.1/32','https://vk.com']:
            with self.assertRaises(ValueError):m.compact([d])
    def fixture(self):return {'uuid':'x','templateType':'XRAY_JSON','templateJson':{'dns':{'servers':['1.1.1.1']},'routing':{'rules':[{'type':'field','network':'tcp,udp','balancerTag':'b'}],'balancers':[{'tag':'b','selector':['auto-']}]},'remnawave':{'injectHosts':[{'foo':'bar'}]},'outbounds':[{'tag':'direct','protocol':'freedom'},{'tag':'block','protocol':'blackhole'}],'inbounds':[{'protocol':'socks','sniffing':{'enabled':True}}]}}
    def test_xray_preserves_transports_balancer_injection(self):
        old=self.fixture();before=copy.deepcopy(old);new,field=m.build_template(old,self.domains);self.assertEqual(old,before);self.assertEqual(field,'templateJson')
        for k in ['outbounds','inbounds','remnawave']:self.assertEqual(new['templateJson'][k],old['templateJson'][k])
        self.assertEqual(new['templateJson']['routing']['balancers'],old['templateJson']['routing']['balancers']);self.assertEqual(new['templateJson']['routing']['rules'][2:],old['templateJson']['routing']['rules'])
    def test_xray_dns_is_scoped(self):
        new,_=m.build_template(self.fixture(),self.domains);j=new['templateJson'];self.assertEqual(j['dns']['servers'][1:],['1.1.1.1']);self.assertTrue(j['dns']['servers'][0]['skipFallback']);self.assertEqual(j['routing']['rules'][0]['ip'],['77.88.8.8']);self.assertEqual(j['routing']['rules'][0]['port'],53)
    def test_existing_block_retained_first(self):
        x=self.fixture();block={'type':'field','protocol':['bittorrent'],'outboundTag':'block'};x['templateJson']['routing']['rules'].insert(0,block);y,_=m.build_template(x,self.domains);self.assertEqual(y['templateJson']['routing']['rules'][0],block)
    def test_singbox_dns_sniff_preserved(self):
        x={'templateType':'SINGBOX','templateJson':{'outbounds':[{'type':'direct','tag':'direct'}],'route':{'rules':[{'action':'sniff'},{'action':'hijack-dns'}]},'dns':{'servers':[{'tag':'local'}],'rules':[{'query_type':['A'],'server':'remote'}]}}};y,_=m.build_template(x,self.domains)
        self.assertEqual(y['templateJson']['route']['rules'][:2],x['templateJson']['route']['rules']);self.assertEqual(y['templateJson']['dns']['rules'][1:],x['templateJson']['dns']['rules'])
    def test_yaml_preserves_original_bytes(self):
        raw='proxies: []\nrules:\n  - MATCH,Proxy\n';x={'templateType':'MIHOMO','encodedTemplateYaml':base64.b64encode(raw.encode()).decode()};y,field=m.build_template(x,['vk.com']);self.assertEqual(field,'encodedTemplateYaml');s=base64.b64decode(y[field]).decode();self.assertEqual(s.replace('  - DOMAIN-SUFFIX,vk.com,DIRECT\n',''),raw)
    def test_yaml_missing_anchor_refused(self):
        with self.assertRaises(ValueError):m.build_template({'templateType':'STASH','encodedTemplateYaml':base64.b64encode(b'no: rules').decode()},self.domains)
    def test_no_unknown_format(self):
        with self.assertRaises(ValueError):m.build_template({'templateType':'UNKNOWN'},self.domains)
    def test_smoke_url_rejects_other_sites_before_network(self):
        with patch.object(m.urllib.request,'build_opener',side_effect=AssertionError('network')):
            for u in ['http://sub.wayspn.online/123456789012','https://evil.com/123456789012','https://sub.wayspn.online/123456789012?x=1']:
                with self.assertRaises(ValueError):m.smoke(u,'x')
    def test_small_header_covers_all_named_services(self):
        p,ds,link=m.header_profile(m.compact(m.SERVICE_DOMAINS+m.MAJOR_BANK_DOMAINS),1)
        self.assertLessEqual(len(link),2300);self.assertEqual(p['GlobalProxy'],'true')
        regex=re.compile(p['DirectSites'][0][7:])
        for d in m.SERVICE_DOMAINS:self.assertTrue(regex.search(d),d)
        self.assertEqual(p['DirectIp'],m.LAN_IPV4)
if __name__=='__main__':unittest.main()
