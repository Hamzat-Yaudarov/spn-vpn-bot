"""Client-side RU service bypass; no users, servers, DNS records or payments.

Default is a no-I/O plan. Prepare saves private preimages. Apply/rollback are
explicit, compare-before-write and durable-intent operations; no automatic
write retries. Existing transports, selectors, balancers and non-RU rules stay.
"""
from __future__ import annotations
import argparse,base64,copy,hashlib,importlib.util,json,os,re,ssl,stat,sys,time,urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
import certifi

ROOT=Path(__file__).resolve().parents[2]
STATE=Path('/private/tmp/spn-ru-direct-20261101-r2')
SERVICE_DOMAINS='''
vk.com vk.ru vk.me vk.cc vk-portal.net vk-cdn.net vk-cdn.me userapi.com vkuseraudio.net vkuserlive.net vkvideo.ru
max.ru oneme.ru
ozon.ru ozon.com ozone.ru ozonusercontent.com
wildberries.ru wildberries.by wb.ru wbbasket.ru wbstatic.net wbcontent.net wb-basket.ru wbpay.ru
avito.ru avito.st
5ka.ru pyaterochka.ru x5.ru perekrestok.ru chizhik.ru vprok.ru
magnit.ru magnit.com magnit-market.ru
gosuslugi.ru gu-st.ru
samokat.ru samokat.tech kuper.ru sbermarket.ru instamart.ru
yandex.ru yandex.net yandex.com yandex.by yandex.kz ya.ru yastatic.net yandexcdn.net kinopoisk.ru
dzen.ru dzeninfra.ru dzeninfra.net
mail.ru imgsmail.ru appsmail.ru attachmail.ru cdnmail.ru mrgcdn.ru smailru.net vmailru.net mradx.net mycdn.me my.com
rustore.ru rustore.team rustore.tv
ok.ru okcdn.ru odnoklassniki.ru
2gis.ru 2gis.com rzd.ru pochta.ru mos.ru mosreg.ru kremlin.ru government.ru
sber.ru sberbank.ru sberbank.com tbank.ru tinkoff.ru tinkoffbank.ru cdn-tinkoff.ru alfabank.ru vtb.ru
nspk.ru mironline.ru mirpayonline.ru yoomoney.ru yoo.money yookassa.ru
'''.split()
BANK_NON_RU=set('alfa.me alfabank.st bank131.com gazprombank.com gazprombank.investments moex.com pvubank.com hva.im tochka.com tochka-tech.com vtb-direct.com vtb.com best2pay.net koronapay.com rbsgate.com rbsuat.com alefbank.com energotransbank.com maritimebank.com priovtb.com pskb.com'.split())
BANK_FILES=('category-bank-ru','sberbank','tbank-ru')
MAJOR_BANK_DOMAINS='''gazprombank.ru gazprombank.com gpb.ru raiffeisen.ru rshb.ru psbank.ru psb.ru mkb.ru otpbank.ru pochtabank.ru sovcombank.ru rosbank.ru domrfbank.ru akbars.ru uralsib.ru rncb.ru bspb.ru mtsbank.ru sbpvtb.ru tochka.com modulbank.ru'''.split()
LAN_IPV4=['10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','169.254.0.0/16','224.0.0.0/4','255.255.255.255']
YAML_TYPES={'MIHOMO','STASH','CLASH'}
def require(v):
    if not v:raise ValueError('RU routing prerequisites changed; details suppressed')
def api(*args):
    s=importlib.util.spec_from_file_location('_ru_client_api',Path(__file__).with_name('client_api.py'));m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m.api(*args)
def sha(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
def save(name,v):
    require(STATE.is_dir() and not STATE.is_symlink() and stat.S_IMODE(STATE.stat().st_mode)==0o700)
    with os.fdopen(os.open(STATE/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'w') as f:
        json.dump(v,f,ensure_ascii=False,separators=(',',':'));f.flush();os.fsync(f.fileno())
    fd=os.open(STATE,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
def read(name):
    p=STATE/name;require(not p.is_symlink() and stat.S_IMODE(p.stat().st_mode)==0o600);return json.loads(p.read_text())
def compact(domains):
    roots=set()
    for d in domains:
        d=d.lower().encode('idna').decode('ascii')
        require(re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+',d) and len(d)<254)
        roots.add(d)
    return sorted(d for d in roots if not any('.'.join(d.split('.')[i:]) in roots for i in range(1,len(d.split('.')))))
def bank_domains():
    found=[];sources=[]
    for name in BANK_FILES:
        url='https://raw.githubusercontent.com/v2fly/domain-list-community/master/data/'+name
        with urllib.request.urlopen(url,timeout=20,context=ssl.create_default_context(cafile=certifi.where())) as f:
            raw=f.read(200001);require(len(raw)<=200000)
        sources.append({'url':url,'sha256':hashlib.sha256(raw).hexdigest()})
        for line in raw.decode().splitlines():
            word=line.split('#',1)[0].strip().split()
            if not word:continue
            d=word[0]
            if d.startswith(('domain:','full:')):d=d.split(':',1)[1]
            if ':' in d or not re.fullmatch(r'[a-zA-Z0-9.-]+',d):continue
            if d.endswith(('.ru','.su','.xn--p1ai','.bank')) or d in BANK_NON_RU:found.append(d)
    return found,sources
def pattern(domains):
    groups={}
    for d in domains:
        name,tld=d.rsplit('.',1);groups.setdefault(tld,[]).append(re.escape(name))
    alternatives=[]
    for tld,names in sorted(groups.items()):
        alternatives.append(('(?:'+'|'.join(sorted(names))+')' if len(names)>1 else names[0])+r'\.'+re.escape(tld))
    return 'regexp:(^|\\.)(?:'+'|'.join(alternatives)+')$'
def app_profile(domains,updated):
    return {'Name':'SPN — российские сервисы напрямую','GlobalProxy':'true','LastUpdated':str(updated),
        'RemoteDNSType':'DoH','RemoteDNSDomain':'https://cloudflare-dns.com/dns-query','RemoteDNSIP':'1.1.1.1',
        'DomesticDNSType':'DoU','DomesticDNSDomain':'','DomesticDNSIP':'77.88.8.8',
        'DirectSites':[pattern(domains)],'DirectIp':list(LAN_IPV4),'ProxySites':[],'ProxyIp':[],'BlockSites':[],'BlockIp':[],
        'DomainStrategy':'AsIs','FakeDNS':'false'}
def header_profile(domains,updated):
    # The deployed subscription proxy rejects ~6KiB routing headers (502).
    # Keep RAW/URI metadata under 2300B, including ALL named services and major
    # banks first. Full JSON/YAML configs retain the complete 399-domain policy.
    wanted=compact(SERVICE_DOMAINS+MAJOR_BANK_DOMAINS)
    selected=compact([d for d in wanted if d in domains])
    def make(ds):
        full=app_profile(ds,updated);full['Name']='SPN RU Direct'
        return {k:full[k] for k in ['Name','GlobalProxy','LastUpdated','DomesticDNSType','DomesticDNSIP','DirectSites','DirectIp','DomainStrategy']}
    def link(p):return 'happ://routing/onadd/'+base64.b64encode(json.dumps(p,separators=(',',':')).encode()).decode()
    require(len(link(make(selected)))<=2300)
    for d in domains:
        if d in selected:continue
        trial=compact(selected+[d])
        if len(link(make(trial)))<=2300:selected=trial
    profile=make(selected)
    regex=re.compile(profile['DirectSites'][0][7:])
    require(all(regex.search(d) and regex.search('api.'+d) for d in SERVICE_DOMAINS))
    return profile,selected,link(profile)
def build_template(original,domains):
    t=copy.deepcopy(original);kind=t['templateType'];d=['domain:'+x for x in domains]
    if kind=='XRAY_JSON':
        j=t['templateJson'];require(isinstance(j,dict) and any(o.get('tag')=='direct' and o.get('protocol')=='freedom' for o in j.get('outbounds',[])))
        rules=j.setdefault('routing',{}).setdefault('rules',[])
        # Keep existing protocol blocks/security rules ahead of the new bypass.
        pos=0
        while pos<len(rules) and (rules[pos].get('outboundTag')=='block' or rules[pos].get('inboundTag')==['dns-in']):pos+=1
        rules.insert(pos,{'type':'field','domain':d,'outboundTag':'direct'})
        dns=j.setdefault('dns',{});servers=dns.setdefault('servers',[]);require(isinstance(servers,list))
        servers.insert(0,{'address':'77.88.8.8','port':53,'domains':d,'skipFallback':True})
        rules.insert(pos,{'type':'field','ip':['77.88.8.8'],'port':53,'network':'udp','outboundTag':'direct'})
        return t,'templateJson'
    if kind=='SINGBOX':
        j=t['templateJson'];require(any(o.get('tag')=='direct' and o.get('type')=='direct' for o in j.get('outbounds',[])))
        rules=j.setdefault('route',{}).setdefault('rules',[]);pos=0
        while pos<len(rules) and rules[pos].get('action') in {'sniff','hijack-dns'}:pos+=1
        rules.insert(pos,{'domain_suffix':domains,'outbound':'direct'})
        dns=j.get('dns',{});require(any(s.get('tag')=='local' for s in dns.get('servers',[])))
        dns.setdefault('rules',[]).insert(0,{'domain_suffix':domains,'server':'local'})
        return t,'templateJson'
    require(kind in YAML_TYPES)
    y=base64.b64decode(t['encodedTemplateYaml'],validate=True).decode();require(y.count('\nrules:\n')==1)
    block=''.join('  - DOMAIN-SUFFIX,'+x+',DIRECT\n' for x in domains)
    t['encodedTemplateYaml']=base64.b64encode(y.replace('\nrules:\n','\nrules:\n'+block,1).encode()).decode()
    return t,'encodedTemplateYaml'
def content(t):return {k:t.get(k) for k in ('uuid','name','templateType','templateJson','encodedTemplateYaml','tags')}
def prepare():
    require(not STATE.exists());os.umask(0o077)
    banks,sources=bank_domains();domains=compact(SERVICE_DOMAINS+banks)
    settings=api('GET','/subscription-settings');headers=settings.get('customResponseHeaders') or {}
    require(isinstance(headers,dict) and not any(k.lower() in {'routing','autorouting'} for k in headers))
    profile,header_domains,routing=header_profile(domains,int(time.time()))
    allmeta=api('GET','/subscription-templates')['templates']
    with ThreadPoolExecutor(max_workers=3) as ex:templates=list(ex.map(lambda t:api('GET','/subscription-templates/'+t['uuid']),allmeta))
    operations=[]
    for before in templates:
        after,field=build_template(before,domains)
        operations.append({'before':content(before),'after':content(after),'body':{'uuid':before['uuid'],field:after[field]}})
    newheaders={**headers,'routing':routing}
    STATE.mkdir(mode=0o700)
    save('plan.json',{'format':1,'domains':domains,'sources':sources,'operations':operations,'settingsBefore':settings,
        'headersAfter':newheaders,'routingProfile':profile,'headerDomains':header_domains,'templateIds':[t['uuid'] for t in templates]})
    for op in operations:
        if op['before']['templateType']=='XRAY_JSON':
            fixture=copy.deepcopy(op['after']['templateJson']);fixture.pop('remnawave',None);fixture['inbounds']=[]
            # Keyless parse-only leaves for injected host/balancer selectors.
            selectors=[b.get('selector',[]) for b in fixture.get('routing',{}).get('balancers',[])]
            for group in selectors:
                for selector in group:
                    tag=selector+'keyless'
                    if not any(o.get('tag')==tag for o in fixture['outbounds']):fixture['outbounds'].append({'tag':tag,'protocol':'freedom'})
            save('validation-'+op['before']['uuid']+'.json',fixture)
    return {'prepared':True,'templates':len(operations),'domains':len(domains),'routingHeaderBytes':len(routing),'state':str(STATE),'networkWrites':0}
def settings_projection(s):return {k:v for k,v in s.items() if k not in {'updatedAt','createdAt','customResponseHeaders'}}
def audit(plan):
    meta=api('GET','/subscription-templates')['templates'];require({t['uuid'] for t in meta}==set(plan['templateIds']))
    results=[]
    for op in plan['operations']:
        t=content(api('GET','/subscription-templates/'+op['before']['uuid']));require(t in (op['before'],op['after']))
        results.append({'uuid':t['uuid'],'applied':t==op['after']})
    s=api('GET','/subscription-settings');require(settings_projection(s)==settings_projection(plan['settingsBefore']))
    require(s.get('customResponseHeaders') in (plan['settingsBefore'].get('customResponseHeaders'),plan['headersAfter']))
    return results,s
def smoke(url,routing):
    u=urlsplit(url);require(u.scheme=='https' and u.hostname=='sub.wayspn.online' and not u.username and not u.password and not u.query and not u.fragment and re.fullmatch(r'/[A-Za-z0-9_-]{12,80}',u.path))
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args):raise ValueError('Subscription redirect refused')
    opener=urllib.request.build_opener(NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))
    req=urllib.request.Request(url,method='HEAD',headers={'User-Agent':'Happ/4.0','Cache-Control':'no-cache'})
    with opener.open(req,timeout=15) as response:
        require(response.status==200 and response.headers.get('routing','')==routing)
        return {'status':response.status,'routingHeaderBytes':len(routing),'privateBodyRead':False}
def apply(smoke_url,resume=False):
    plan=read('plan.json');validation=read('validation.json');require(validation.get('allPassed') is True and validation.get('planHash')==sha(plan))
    results,settings=audit(plan)
    for op,result in zip(plan['operations'],results):
        if result['applied']:continue
        name='intent-template-'+op['before']['uuid']+'.json'
        require(content(api('GET','/subscription-templates/'+op['before']['uuid']))==op['before'])
        if (STATE/name).exists():
            require(resume is True and read(name)==op['body'])
            save('reconciled-retry-'+op['before']['uuid']+'.json',{'bodyHash':sha(op['body']),'reason':'Explicit operator resume after full scope audit and fresh baseline confirmation; idempotent PATCH only'})
        else:save(name,op['body'])
        api('PATCH','/subscription-templates',op['body'])
        require(content(api('GET','/subscription-templates/'+op['before']['uuid']))==op['after'])
        print(json.dumps({'template':op['before']['name'],'type':op['before']['templateType'],'applied':True}),flush=True)
    # Headers are last: no app receives new metadata before all config formats are ready.
    s=api('GET','/subscription-settings');require(s==settings)
    if s.get('customResponseHeaders')!=plan['headersAfter']:
        body={'uuid':s['uuid'],'customResponseHeaders':plan['headersAfter']};require(not (STATE/'intent-headers.json').exists())
        save('intent-headers.json',body);api('PATCH','/subscription-settings',body)
    try:
        delivery=smoke(smoke_url,plan['headersAfter']['routing'])
    except Exception:
        # Never leave an oversized/broken response header on all subscriptions.
        current=api('GET','/subscription-settings');require(current['customResponseHeaders']==plan['headersAfter'])
        body={'uuid':current['uuid'],'customResponseHeaders':plan['settingsBefore'].get('customResponseHeaders') or {}}
        save('header-delivery-rollback.json',body);api('PATCH','/subscription-settings',body)
        raise ValueError('Header delivery failed; prior headers restored, template rules retained') from None
    results,s=audit(plan);require(all(r['applied'] for r in results) and s['customResponseHeaders']==plan['headersAfter'])
    save('applied.json',{'templates':len(results),'domains':len(plan['domains']),'completedAt':int(time.time())})
    return {'applied':True,'templates':len(results),'domains':len(plan['domains']),'headersVerified':True,'delivery':delivery}
def rollback():
    plan=read('plan.json');results,s=audit(plan)
    if s.get('customResponseHeaders')==plan['headersAfter']:
        body={'uuid':s['uuid'],'customResponseHeaders':plan['settingsBefore'].get('customResponseHeaders') or {}}
        save('rollback-headers.json',body);api('PATCH','/subscription-settings',body)
    for op,result in reversed(list(zip(plan['operations'],results))):
        if not result['applied']:continue
        field='encodedTemplateYaml' if op['before']['templateType'] in YAML_TYPES else 'templateJson'
        require(content(api('GET','/subscription-templates/'+op['before']['uuid']))==op['after'])
        body={'uuid':op['before']['uuid'],field:op['before'][field]};save('rollback-'+op['before']['uuid']+'.json',body);api('PATCH','/subscription-templates',body)
    results,s=audit(plan);require(not any(r['applied'] for r in results) and s.get('customResponseHeaders')==plan['settingsBefore'].get('customResponseHeaders'))
    return {'rolledBack':True}
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',nargs='?',choices=['prepare','audit','apply','rollback']);p.add_argument('--confirm',action='store_true');p.add_argument('--resume',action='store_true');p.add_argument('--smoke-url-file',type=Path);a=p.parse_args()
    try:
        if a.action is None:r={'planOnly':True,'network':False,'userAccess':False,'serverChanges':False}
        elif a.action=='prepare':r=prepare()
        elif a.action=='audit':
            q,s=audit(read('plan.json'));r={'templatesApplied':sum(x['applied'] for x in q),'templates':len(q),'headersApplied':s.get('customResponseHeaders')==read('plan.json')['headersAfter']}
        else:
            require(a.confirm)
            if a.action=='apply':
                require(a.smoke_url_file is not None and a.smoke_url_file.parent==STATE and not a.smoke_url_file.is_symlink() and stat.S_IMODE(a.smoke_url_file.stat().st_mode)==0o600)
                r=apply(json.loads(a.smoke_url_file.read_text())['url'],resume=a.resume)
            else:r=rollback()
        print(json.dumps(r,ensure_ascii=False))
    except Exception as e:
        print('RU routing stopped: '+type(e).__name__+'; private response suppressed',file=sys.stderr);raise SystemExit(2)
