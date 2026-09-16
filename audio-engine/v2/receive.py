"""Receive the owner's encrypted manuscript without publishing its key or text."""
import base64,hashlib,json,lzma,os,time,random,urllib.request,urllib.error
from pathlib import Path
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa,padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
REPO=os.environ['GITHUB_REPOSITORY'];RUN=os.environ['GITHUB_RUN_ID'];INDEX=int(os.environ['RENDER_INDEX']);TOTAL=int(os.environ['RENDER_TOTAL']);BASE='https://api.github.com/repos/'+REPO

def api(path,data=None):
 req=urllib.request.Request(BASE+path,data=None if data is None else json.dumps(data).encode(),method='GET' if data is None else 'PUT',headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json','Content-Type':'application/json','User-Agent':'taliesin-private-render-v2'})
 with urllib.request.urlopen(req,timeout=45) as response:return json.load(response)
def read(path):
 try:return base64.b64decode(api('/contents/'+path+'?ref=main')['content'])
 except urllib.error.HTTPError as e:
  if e.code==404:return None
  raise
def put(path,data):
 for _ in range(20):
  try:api('/contents/'+path,{'branch':'main','message':'Register public render key v2','content':base64.b64encode(data).decode()});return
  except urllib.error.HTTPError as e:
   if e.code not in (409,422):raise
   if read(path)==data:return
   time.sleep(1+random.random()*3)
 raise RuntimeError('Key registration failed')
secret=rsa.generate_private_key(public_exponent=65537,key_size=2048)
pub=secret.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
put(f'.render-v2/{RUN}/{INDEX}.json',json.dumps({'index':INDEX,'public_key':pub}).encode())
start=time.monotonic()
if INDEX==0:
 while time.monotonic()-start<2700:
  keys=[read(f'.render-v2/{RUN}/{i}.json') for i in range(TOTAL)]
  if all(keys):put(f'.render-v2/{RUN}/all.json',json.dumps({'run':RUN,'keys':[json.loads(k) for k in keys]}).encode());break
  time.sleep(10)
while time.monotonic()-start<3600:
 content=read(f'.sealed-v2/{RUN}.json')
 if content:
  envelope=json.loads(content)
  if envelope.get('run')==RUN:break
 time.sleep(12)
else:raise TimeoutError('Encrypted manuscript not delivered; no audio was published')
key=secret.decrypt(base64.b64decode(envelope['recipients'][str(INDEX)]),padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None));del secret
parts=[]
for sha in envelope['blobs']:
 raw=base64.b64decode(api('/git/blobs/'+sha)['content'])
 if hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()!=sha:raise ValueError('Damaged transfer chunk')
 parts.append(raw)
cipher=b''.join(parts);del parts
if hashlib.sha256(cipher).hexdigest()!=envelope['cipher_sha256']:raise ValueError('Wrong ciphertext checksum')
plain=lzma.decompress(AESGCM(key).decrypt(base64.b64decode(envelope['nonce']),cipher,b'Taliesin render payload v2'));del key,cipher
if hashlib.sha256(plain).hexdigest()!=envelope['plain_sha256']:raise ValueError('Wrong manuscript checksum')
work=Path('/tmp/private-audio');work.mkdir(exist_ok=True);(work/'recipe.json').write_bytes(plain);os.chmod(work/'recipe.json',0o600)
print('Encrypted manuscript authenticated. Private keys and text remain on this worker.',flush=True)
