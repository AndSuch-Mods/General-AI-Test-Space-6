"""Unpack public engine code, then authenticate the private render recipe."""
from __future__ import annotations
import base64,hashlib,io,json,lzma,os,time,urllib.request,urllib.error,zipfile
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
root=Path(__file__).resolve().parent
bundle=base64.b64decode((root/'engine-bundle.b64').read_text(),validate=False)
if hashlib.sha256(bundle).hexdigest()!=(root/'engine-bundle.sha256').read_text().strip():
 raise RuntimeError('Public engine bundle checksum mismatch')
with zipfile.ZipFile(io.BytesIO(bundle)) as z:
 for name in z.namelist():
  if Path(name).name!=name or not name.endswith('.py'):raise ValueError('Unsafe engine member')
 z.extractall(root/'runtime')
work=Path(os.environ.get('AUDIO_WORK','/tmp/private-audio'))
p=work/'recipe.json';value=json.loads(p.read_text())
if 'transfer' in value:
 transfer=value['transfer'];run=os.environ['GITHUB_RUN_ID'];repo=os.environ['GITHUB_REPOSITORY']
 def get(path):
  req=urllib.request.Request('https://api.github.com/repos/'+repo+path,headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json','User-Agent':'private-audio-render'})
  with urllib.request.urlopen(req,timeout=45) as response:return json.load(response)
 deadline=time.monotonic()+2700
 while time.monotonic()<deadline:
  try:
   item=get('/contents/.sealed-input/'+run+'.json?ref=main');manifest=json.loads(base64.b64decode(item['content']));break
  except urllib.error.HTTPError as e:
   if e.code!=404:raise
  time.sleep(15)
 else:raise TimeoutError('Encrypted input did not arrive')
 chunks=[]
 for sha in manifest['blobs']:
  blob=get('/git/blobs/'+sha);raw=base64.b64decode(blob['content'])
  if hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()!=sha:raise ValueError('Damaged input chunk')
  chunks.append(raw)
 cipher=b''.join(chunks);del chunks
 if hashlib.sha256(cipher).hexdigest()!=transfer['cipher_sha256']:raise ValueError('Encrypted input checksum mismatch')
 plain=lzma.decompress(AESGCM(base64.b64decode(transfer['key'])).decrypt(base64.b64decode(transfer['nonce']),cipher,('full-recipe:'+run).encode()))
 if hashlib.sha256(plain).hexdigest()!=transfer['plain_sha256']:raise ValueError('Recipe checksum mismatch')
 p.write_bytes(plain);os.chmod(p,0o600)
 print('Complete encrypted input received and authenticated.',flush=True)
