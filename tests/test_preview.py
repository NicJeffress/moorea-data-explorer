"""Adversarial boundary, durable budget and bundle tests; no paid provider calls."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.testclient import TestClient
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.preview_access import Settings, AccessVerifier, PreviewBoundary
from backend.preview_analyst import Ledger, Denied, MODEL, RESERVE_MICRO_USD, complete, payload, usage_cost
from scripts.build_preview_release import checked_copy, verify
from backend.preview import PreviewFiles, PreviewRoutes
import gzip
import hashlib

class PreviewAccess(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
  self.settings=Settings('https://preview.example.org','https://test.cloudflareaccess.com','aud',frozenset({'tester@example.org'}),root/'release',root/'private',1000000)
  verifier=AccessVerifier(self.settings,SimpleNamespace(get_signing_key_from_jwt=lambda t:SimpleNamespace(key=self.key.public_key())))
  async def app(scope,receive,send):
   h=dict(scope['headers'])
   await JSONResponse({'identity':scope['state']['preview_identity'],'host':h[b'host'].decode(),'email_header':b'cf-access-authenticated-user-email' in h})(scope,receive,send)
  self.client=TestClient(PreviewBoundary(app,self.settings,verifier),base_url=self.settings.origin);self.addCleanup(self.client.close)
 def token(self,**changes):
  c={'iss':self.settings.issuer,'aud':'aud','sub':'tester-id','email':'tester@example.org','iat':int(time.time())-10,'exp':int(time.time())+60};c.update(changes)
  return jwt.encode(c,self.key,algorithm='RS256')
 def test_signed_identity_required_on_every_route(self):
  for p in ['/','/api/health','/config.json','/mcp','/static-data/v1/a.json']:
   self.assertEqual(self.client.get(p,headers={'Cf-Access-Authenticated-User-Email':'tester@example.org'}).status_code,401,p)
  r=self.client.get('/',headers={'Cf-Access-Jwt-Assertion':self.token(),'Cf-Access-Authenticated-User-Email':'forged@example.org'})
  self.assertEqual(r.status_code,200);self.assertFalse(r.json()['email_header']);self.assertEqual(r.json()['host'],'127.0.0.1:8765')
 def test_invalid_claims_signature_and_email_fail(self):
  for t in [self.token(aud='other'),self.token(iss='https://evil.example'),self.token(exp=1),self.token(email='other@example.org'),self.token(sub=''),self.token()[:-10]+'abcdefghij']:
   self.assertEqual(self.client.get('/',headers={'Cf-Access-Jwt-Assertion':t}).status_code,403)
 def test_origin_host_download_and_cache_guards(self):
  h={'Cf-Access-Jwt-Assertion':self.token()}
  for extra in [{'Origin':'https://evil.example'},{'Host':'evil.example'}]:self.assertEqual(self.client.get('/',headers=h|extra).status_code,403)
  for p in ['/api/materials/x/download','/api/coral-multi/x/original','/api/coral-photos/image/v/id/original','/api/gump-records/source/day','/api/ctd-records/source/transects','/api/reef-temperature/download/cache']:
   self.assertEqual(self.client.get(p,headers=h).status_code,403,p)
  for p,cache in [('/','no-cache'),('/api/sst','no-store'),('/static-data/v1/a.json','private, max-age=31536000, immutable')]:self.assertEqual(self.client.get(p,headers=h).headers['cache-control'],cache)
 def test_duplicate_header_fails(self):
  self.assertEqual(self.client.get('/',headers=[('Cf-Access-Jwt-Assertion',self.token()),('Cf-Access-Jwt-Assertion',self.token())]).status_code,400)
 def test_decoded_path_aliases_cannot_cross_the_release_boundary(self):
  h={'Cf-Access-Jwt-Assertion':self.token()}
  for path in ['/api/%2e%2e/checks.html','/api/%2e%2e/index.html','/api/%2e%2e/datasets.html',
               '/api/%2E%2E/site-only.txt','/api/%2e/health','/api/%5c../checks.html','/api/%00health']:
   self.assertEqual(self.client.get(path,headers=h).status_code,400,path)
 def test_bad_configuration_fails(self):
  for origin,state in [('http://preview.example.org',self.settings.state),(self.settings.origin,self.settings.release/'state')]:
   with self.assertRaises(ValueError):Settings(origin,self.settings.issuer,'aud',self.settings.emails,self.settings.release,state)

class ModelLedger(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.now=1794000000.
  self.ledger=Ledger(self.tmp.name,1000000,clock=lambda:self.now)
 def test_restart_keeps_possible_spend(self):
  self.ledger.reserve('person');other=Ledger(self.tmp.name,1000000,clock=lambda:self.now)
  with self.assertRaises(Denied):other.reserve('different')
  self.now+=121
  for _ in range(2):other.finish(other.reserve('different'))
  with self.assertRaises(Denied):other.reserve('different')
  with other.connect() as db:self.assertEqual(db.execute('SELECT SUM(charge) FROM calls').fetchone()[0],3*RESERVE_MICRO_USD)
 def test_concurrent_workers_have_one_global_slot(self):
  def attempt(_):
   try:return self.ledger.reserve('same')
   except Denied:return None
  with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(attempt,range(8)))
  self.assertEqual(sum(x is not None for x in results),1)
 def test_per_user_rate_kill_switch_and_usage(self):
  for _ in range(15):self.ledger.finish(self.ledger.reserve('person'),1)
  with self.assertRaises(Denied):self.ledger.reserve('person')
  self.ledger.finish(self.ledger.reserve('another'),1)
  (Path(self.tmp.name)/'STOP_ANALYST').touch()
  with self.assertRaises(Denied):self.ledger.reserve('another')
  self.assertEqual(usage_cost({'usage':{'prompt_tokens':1000,'completion_tokens':200}}),1650)
  self.assertIsNone(usage_cost({'usage':{'prompt_tokens':True,'completion_tokens':2}}))
 def test_zero_budget_and_new_utc_month(self):
  disabled=Ledger(Path(self.tmp.name)/'disabled',0,clock=lambda:self.now)
  with self.assertRaises(Denied):disabled.reserve('person')
  for _ in range(3):self.ledger.finish(self.ledger.reserve('person'))
  with self.assertRaises(Denied):self.ledger.reserve('person')
  self.now+=32*86400
  self.ledger.finish(self.ledger.reserve('person'),100)
  with self.ledger.connect() as db:self.assertEqual(db.execute('SELECT COUNT(DISTINCT month) FROM calls').fetchone()[0],2)
 def test_fixed_text_only_payload(self):
  p=payload({'messages':[{'role':'user','content':'test'}],'model':'expensive','store':True,'max_completion_tokens':999999})
  self.assertEqual(p['model'],MODEL);self.assertFalse(p['store']);self.assertEqual(p['max_completion_tokens'],2048)
  for body in [{'messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'https://example.org'}}]}]},{'messages':[{'role':'user','content':'hi'}],'tools':[{'type':'web_search'}]}]:
   with self.assertRaises(ValueError):payload(body)

class ProviderFailures(unittest.IsolatedAsyncioTestCase):
 async def test_malformed_usage_keeps_the_full_charge_without_crashing(self):
  with tempfile.TemporaryDirectory() as tmp:
   for index,usage in enumerate([None,[],42,'unknown',{'prompt_tokens':-1,'completion_tokens':2}]):
    ledger=Ledger(Path(tmp)/str(index),10*RESERVE_MICRO_USD)
    async def receive():return {'type':'http.request','body':b'{"messages":[{"role":"user","content":"test"}]}'}
    request=Request({'type':'http','method':'POST','path':'/','headers':[],'state':{'preview_identity':'id'}},receive)
    factory=lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'choices':[],'usage':usage})),**kw)
    response=await complete(request,ledger,'fixture-key',factory)
    self.assertEqual(response.status_code,200)
    with ledger.connect() as db:
     self.assertEqual(db.execute('SELECT charge,active_until FROM calls').fetchall(),[(RESERVE_MICRO_USD,0)])
 async def test_settlement_and_failure_do_not_log_or_refund_prompt(self):
  with tempfile.TemporaryDirectory() as tmp:
   ledger=Ledger(tmp,1000000)
   def request():
    async def receive():return {'type':'http.request','body':json.dumps({'messages':[{'role':'user','content':'private prompt'}]}).encode()}
    return Request({'type':'http','method':'POST','path':'/','headers':[],'state':{'preview_identity':'id'}},receive)
   def handler(r):
    self.assertEqual(json.loads(r.content)['model'],MODEL)
    return httpx.Response(200,json={'choices':[],'usage':{'prompt_tokens':1000,'completion_tokens':200}})
   factory=lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(handler),**kw)
   self.assertEqual((await complete(request(),ledger,'test-key',factory)).status_code,200)
   def failed(r):raise httpx.ReadTimeout('private prompt')
   factory=lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(failed),**kw)
   r=await complete(request(),ledger,'test-key',factory);self.assertEqual(r.status_code,502);self.assertNotIn(b'private prompt',r.body)
   with ledger.connect() as db:self.assertEqual(db.execute('SELECT charge,active_until FROM calls ORDER BY started').fetchall(),[(1650,0),(RESERVE_MICRO_USD,0)])
   self.assertNotIn(b'private prompt',ledger.path.read_bytes())

class ReleaseSafety(unittest.TestCase):
 def test_raw_files_symlinks_and_keys_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   for name,content in [('raw.parquet',b'x'),('.env',b'x'),('unsafe.js',b'sk-proj-'+b'A'*40)]:
    p=root/name;p.write_bytes(content)
    with self.assertRaises(ValueError):checked_copy(p,root/'out'/name)
   (root/'link.js').symlink_to(root/'unsafe.js')
   with self.assertRaises(ValueError):checked_copy(root/'link.js',root/'out/link.js')

class PackagedFiles(unittest.TestCase):
 def test_only_explicit_api_routes_can_reach_the_backend(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);local=root/'local';web=root/'web';local.mkdir();web.mkdir()
   (local/'checks.html').write_text('excluded local page')
   (local/'site-only.txt').write_text('untracked local content')
   (web/'index.html').write_text('verified preview page')
   async def health(request):return JSONResponse({'status':'ok'})
   routes=[Route('/api/health',health),Mount('/',StaticFiles(directory=local,html=True))]
   client=TestClient(Starlette(routes=[Mount('/',PreviewRoutes(routes,PreviewFiles(directory=web,html=True)))]))
   self.addCleanup(client.close)
   self.assertEqual(client.get('/api/health').json(),{'status':'ok'})
   self.assertEqual(client.get('/').text,'verified preview page')
   for path in ['/api/%2e%2e/checks.html','/api/%2e%2e/site-only.txt','/api/%2e%2e/index.html',
                '/api/missing','/api/health/','/checks.html','/site-only.txt']:
    response=client.get(path,follow_redirects=False)
    self.assertEqual(response.status_code,404,path)
    self.assertNotIn('excluded local',response.text)
 def test_compressed_assets_and_tamper_detection(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);web=root/'web';web.mkdir();raw=b'{"release":"v1"}'
   (web/'current.json').write_bytes(raw);(web/'current.json.gz').write_bytes(gzip.compress(raw,mtime=0))
   (root/'release-manifest.json').write_text(json.dumps({'files':[{'path':'current.json','sha256':hashlib.sha256(raw).hexdigest()}]}))
   self.assertEqual(verify(root),2)
   c=TestClient(PreviewFiles(directory=web));self.addCleanup(c.close)
   for encoding,compressed in [('gzip',True),('gzip;q=0',False),('gzip;q=bad',False),('identity',False)]:
    r=c.get('/current.json',headers={'Accept-Encoding':encoding});self.assertEqual(r.content,raw)
    self.assertEqual(r.headers.get('content-encoding')=='gzip',compressed)
    self.assertEqual(r.headers['vary'],'Accept-Encoding')
   (web/'rogue.env').write_text('private')
   with self.assertRaises(ValueError):verify(root)
   (web/'rogue.env').unlink();(web/'current.json').write_text('changed')
   with self.assertRaises(ValueError):verify(root)

if __name__=='__main__':unittest.main()
