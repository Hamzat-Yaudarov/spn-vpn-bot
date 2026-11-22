"""Narrow subscription-routing API adapter; never accesses users or server profiles."""
from __future__ import annotations
import io,json,re,ssl,time,urllib.request,urllib.error
from pathlib import Path
from urllib.parse import urlsplit
import certifi
from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[2]
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):raise ValueError('Panel redirect refused')

def api(method,path,body=None):
    template=re.fullmatch(r'/subscription-templates/[0-9a-f-]{36}',path)
    allowed=(method=='GET' and (path in {'/subscription-templates','/hosts','/subscription-settings'} or template) and body is None)
    allowed=allowed or (method=='PATCH' and path=='/subscription-templates' and isinstance(body,dict)
        and set(body)<= {'uuid','templateJson','encodedTemplateYaml'} and 'uuid' in body)
    allowed=allowed or (method=='PATCH' and path=='/subscription-settings' and isinstance(body,dict)
        and set(body)=={'uuid','customResponseHeaders'})
    if not allowed:raise ValueError('Outside subscription-routing scope')
    lines=[line for line in (ROOT/'.env').read_text().splitlines() if line.split('=',1)[0].strip() in {'REMNAWAVE_BASE_URL','REMNAWAVE_API_TOKEN'}]
    values=dotenv_values(stream=io.StringIO('\n'.join(lines)),interpolate=False)
    base=values['REMNAWAVE_BASE_URL'].rstrip('/');token=values['REMNAWAVE_API_TOKEN'];parsed=urlsplit(base)
    if base!='https://panel.wayspn.online/api' or not token or any(c.isspace() for c in token):raise ValueError('Unexpected panel configuration')
    headers={'Authorization':'Bearer '+token,'Accept':'application/json'};data=None
    if body is not None:data=json.dumps(body,separators=(',',':')).encode();headers['Content-Type']='application/json'
    req=urllib.request.Request(base+path,method=method,headers=headers,data=data)
    opener=urllib.request.build_opener(NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))
    for attempt in range(3 if method=='GET' else 1):
        try:
            with opener.open(req,timeout=25) as response:
                raw=response.read(8_000_001)
                if len(raw)>8_000_000:raise ValueError('Oversized panel response')
                return json.loads(raw)['response']
        except urllib.error.HTTPError as e:raise RuntimeError('Panel HTTP '+str(e.code)+'; body suppressed') from None
        except (urllib.error.URLError,TimeoutError):
            if method!='GET' or attempt==2:raise
            time.sleep(.5*(attempt+1))
