import json,os,sys,tempfile,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import argparse
p=argparse.ArgumentParser(description="Check an assembled preview locally with signed test tokens and no paid API calls.")
p.add_argument("--release",type=Path,required=True)
a=p.parse_args()
from backend.preview import create_app
from backend.preview_access import AccessVerifier
with tempfile.TemporaryDirectory() as state:
 env={'GLEN_PREVIEW_ORIGIN':'https://preview.example.org','GLEN_ACCESS_ISSUER':'https://test.cloudflareaccess.com','GLEN_ACCESS_AUD':'aud','GLEN_PREVIEW_EMAILS':'tester@example.org','GLEN_PREVIEW_RELEASE':str(a.release.resolve()),'GLEN_PREVIEW_STATE':state,'GLEN_PREVIEW_BUDGET_MICRO_USD':'0','OPENAI_API_KEY':'','ANTHROPIC_API_KEY':'','GLEN_ANALYST_MODEL':'','OPENAI_MODEL':'','GLEN_ANALYST_EFFORT':''}
 with patch.dict(os.environ,env):app=create_app()
 key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
 app.verifier=AccessVerifier(app.settings,SimpleNamespace(get_signing_key_from_jwt=lambda t:SimpleNamespace(key=key.public_key())))
 token=jwt.encode({'iss':env['GLEN_ACCESS_ISSUER'],'aud':'aud','sub':'test','email':'tester@example.org','iat':int(time.time())-5,'exp':int(time.time())+600},key,algorithm='RS256')
 headers={'Cf-Access-Jwt-Assertion':token,'Origin':env['GLEN_PREVIEW_ORIGIN']}
 with TestClient(app,base_url=env['GLEN_PREVIEW_ORIGIN']) as c:
  for p in ['/','/config.json','/api/health','/glen/main.js','/static-data/current.json','/.env','/data/waves.parquet','/datasets.html','/glen/index.html','/comparison/index.html','/api/materials/x/download','/api/%2e%2e/checks.html','/api/%2e%2e/datasets.html','/api/missing','/api/health/']:
   unauth=c.get(p);r=c.get(p,headers=headers);print(p,unauth.status_code,r.status_code,r.headers.get('cache-control'))
   assert unauth.status_code==401
   if p in ['/.env','/data/waves.parquet','/glen/index.html','/comparison/index.html','/api/missing','/api/health/']:assert r.status_code==404
   elif '%2e' in p:assert r.status_code==400
   elif p.endswith('/download'):assert r.status_code==403
   else:assert r.status_code==200
  schema_page=c.get('/datasets.html?dataset=moorea_waves',headers=headers);assert schema_page.status_code==200 and 'GLEN_PREVIEW=true' in schema_page.text
  compressed=c.get('/workspace.js',headers=headers|{'Accept-Encoding':'gzip'});assert compressed.headers.get('content-encoding')=='gzip'
  cfg=c.get('/config.json',headers=headers).json();assert cfg['llm_models'][0]['endpoint']=='https://preview.example.org/api/llm/v1'
  assert cfg['llm_model']=='gpt-5.4-mini-2026-03-17'
  health=c.get('/api/health',headers=headers).json();assert health['public_release'] is False and health['access']=='private-preview'
  r=c.post('/mcp',headers=headers|{'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'preview-test','version':'1'}}});print('MCP initialize',r.status_code);assert r.status_code==200
  r=c.post('/api/llm/v1/chat/completions',headers=headers,json={'messages':[{'role':'user','content':'test'}]});print('Analyst disabled',r.status_code);assert r.status_code==503
  r=c.post('/api/llm/v1/chat/completions',headers=headers,content=b'x'*256001);assert r.status_code==413
print('PASS authenticated package, config, health, MCP, original/raw blocks, body bound and disabled analyst')
