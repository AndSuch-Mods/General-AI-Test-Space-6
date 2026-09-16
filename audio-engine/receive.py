"""One-use encrypted transfer to an Actions render worker.
Only public keys are committed. Private keys remain in worker memory.
Decrypted reading scripts and speech caches are never uploaded.
"""
from __future__ import annotations
import base64,hashlib,json,os,random,time,urllib.request,urllib.error,lzma
from pathlib import Path
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa,padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
REPO=os.environ['GITHUB_REPOSITORY'];RUN=os.environ['GITHUB_RUN_ID']
INDEX=int(os.environ['RENDER_INDEX']);TOTAL=int(os.environ.get('RENDER_TOTAL','12'))
BASE=f'https://api.github.com/repos/{REPO}';TOKEN=os.environ['GH_TOKEN']
def api(path,payload=None):
 body=None if payload is None else json.dumps(payload).encode()
 req=urllib.request.Request(BASE+path,data=body,method='GET' if payload is None else 'PUT',headers={'Authorization':'Bearer '+TOKEN,'Accept':'application/vnd.github+json','Content-Type':'application/json','User-Agent':'private-audio-build'})
 with urllib.request.urlopen(req,timeout=45) as r:return json.load(r)
def get_file(path):
 try:
  r=api('/contents/'+path+'?ref=main');return base64.b64decode(r['content'])
 except urllib.error.HTTPError as e:
  if e.code==404:return None
  raise
def publish_public(path,data):
 for attempt in range(15):
  try:
   api('/contents/'+path,{'message':'Register one-use public audio-build key','content':base64.b64encode(data).decode(),'branch':'main'});return
  except urllib.error.HTTPError as e:
   if e.code not in (409,422):raise
   if get_file(path)==data:return
   time.sleep(1+random.random()*3)
 raise RuntimeError('Public-key registration failed')
private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
public=private.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
publish_public(f'.render-keys/{RUN}/{INDEX}.json',json.dumps({'run':RUN,'worker':INDEX,'public_key':public}).encode())
print(f'Worker {INDEX}: public key registered; waiting for sealed input.',flush=True)
start=time.monotonic()
if INDEX==0:
 while time.monotonic()-start<2100:
  keys=[]
  for i in range(TOTAL):
   item=get_file(f'.render-keys/{RUN}/{i}.json')
   if item:keys.append(json.loads(item))
  if len(keys)==TOTAL:
   publish_public(f'.render-keys/{RUN}/all.json',json.dumps({'run':RUN,'keys':keys}).encode());print('All public keys registered.',flush=True);break
  time.sleep(12)
while time.monotonic()-start<2400:
 value=get_file(f'.sealed/{RUN}.json')
 if value:
  envelope=json.loads(value)
  if envelope.get('run')==RUN:break
 time.sleep(18)
else:raise TimeoutError('Sealed recipe was not delivered within this run')
key=private.decrypt(base64.b64decode(envelope['recipients'][str(INDEX)]),padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))
del private
chunks=[]
for sha in envelope['blobs']:
 b=api('/git/blobs/'+sha);raw=base64.b64decode(b['content'])
 if hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()!=sha:raise RuntimeError('Transfer chunk validation failed')
 chunks.append(raw)
cipher=b''.join(chunks);del chunks
if hashlib.sha256(cipher).hexdigest()!=envelope['cipher_sha256']:raise RuntimeError('Sealed recipe checksum failed')
compressed=AESGCM(key).decrypt(base64.b64decode(envelope['nonce']),cipher,('audio-recipe:'+RUN).encode());del key,cipher
plain=lzma.decompress(compressed);del compressed
if hashlib.sha256(plain).hexdigest()!=envelope['plain_sha256']:raise RuntimeError('Recipe checksum failed')
recipe=json.loads(plain);del plain
if recipe['source_sha256']!='707254842a0c590201adb29f0f07cc39501f001c031a58f3d850728a4265bcc5':raise RuntimeError('Source identity mismatch')
work=Path(os.environ.get('AUDIO_WORK','/tmp/private-audio'));work.mkdir(parents=True,exist_ok=True)
(work/'recipe.json').write_text(json.dumps(recipe,ensure_ascii=False));os.chmod(work/'recipe.json',0o600)
print(f'Worker {INDEX}: sealed reading recipe authenticated and received.',flush=True)
