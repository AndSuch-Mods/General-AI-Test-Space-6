"""Save verified recordings on a non-deploying branch, one chapter at a time."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import base64, hashlib, importlib.util, io, json, lzma, os, shutil
import subprocess, sys, time, urllib.error, urllib.parse, urllib.request, zipfile

ROOT=Path(__file__).resolve().parents[2]
WORK=Path('/tmp/private-audio')
OUT=ROOT/'recordings'
BRANCH='recordings-only'
KEY_ID='private-archive-v1'
SOURCE='707254842a0c590201adb29f0f07cc39501f001c031a58f3d850728a4265bcc5'
ORDER=['b0-c00']+[f'b1-c{i:02}' for i in range(1,17)]+[f'b2-c{i:02}' for i in range(1,15)]+[f'b3-c{i:02}' for i in range(1,19)]
LIMIT=4_000_000


def digest(data):return hashlib.sha256(data).hexdigest()


def read(path):return json.loads(Path(path).read_text())


def write(path,value):
 path.parent.mkdir(parents=True,exist_ok=True)
 temp=path.with_suffix(path.suffix+'.tmp');temp.write_text(json.dumps(value,indent=2));temp.replace(path)


def run(*args,**kw):return subprocess.run(args,check=True,cwd=ROOT,**kw)


def api(path,body=None):
 url='https://api.github.com/repos/'+os.environ['GITHUB_REPOSITORY']+path
 for attempt in range(4):
  req=urllib.request.Request(url,data=None if body is None else json.dumps(body).encode(),method='GET' if body is None else 'PUT',headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json','Content-Type':'application/json','User-Agent':'verified-recordings-only'})
  try:
   with urllib.request.urlopen(req,timeout=30) as response:return json.load(response)
  except urllib.error.HTTPError as exc:
   if exc.code not in (409,429,500,502,503,504) or attempt==3:raise
  except (urllib.error.URLError,TimeoutError):
   if attempt==3:raise
  time.sleep(2**attempt)
 raise RuntimeError('GitHub request failed')


def input_bytes():
 manifest=read(ROOT/'.recording-input/manifest.json');parts=[]
 if manifest.get('source_sha256')!=SOURCE or manifest.get('key_id')!=KEY_ID:raise ValueError('Wrong source or key identity')
 for number,part in enumerate(manifest['chunks']):
  if part['path']!=f'{number:03}.bin':raise ValueError('Unexpected input order')
  data=(ROOT/'.recording-input'/part['path']).read_bytes()
  if len(data)!=part['bytes'] or digest(data)!=part['sha256']:raise ValueError('Damaged input chunk '+str(number))
  parts.append(data)
 blob=b''.join(parts)
 if len(blob)!=manifest['cipher_bytes'] or digest(blob)!=manifest['cipher_sha256']:raise ValueError('Input reassembly failed')
 return manifest,blob


def recipe(key):
 from cryptography.hazmat.primitives.ciphers.aead import AESGCM
 manifest,blob=input_bytes()
 compressed=AESGCM(key).decrypt(blob[:12],blob[12:],b'Taliesin:recordings-only:full-recipe:v1')
 raw=lzma.decompress(compressed,memlimit=128*1024*1024)
 if len(raw)>10_000_000 or digest(raw)!=manifest['plain_sha256']:raise ValueError('Decrypted recipe mismatch')
 item=json.loads(raw)
 if item['source_sha256']!=SOURCE or item.get('coverage_pass') is not True:raise ValueError('Unverified source edition')
 if len(item['fields'])!=len(item['segments']) or any(len(c)!=11105 for c in item['segments']):raise ValueError('Incomplete reading columns')
 rows=[]
 for values in zip(*item['segments'],strict=True):
  row=dict(zip(item['fields'],values,strict=True));row['text']=row['tts_text']
  row['audio_key']=digest((str(row['speaker_id'])+'|'+row['tts_text']).encode())[:24]
  if not row['tts_text'].strip():raise ValueError('Empty speech segment')
  rows.append(row)
 if len(rows)!=11105 or len({r['id'] for r in rows})!=11105:raise ValueError('Duplicate or missing reading segments')
 cues={v[0]:{'paragraph_id':v[0],'ambience':v[1],'events':[dict(zip(('sound','fraction','gain'),e,strict=True)) for e in v[2]]} for v in item['cues']}
 return rows,cues,item['source_script_sha256']


def key_step():
 from cryptography.hazmat.primitives import serialization,hashes
 from cryptography.hazmat.primitives.asymmetric import rsa,padding
 epoch=os.environ['GITHUB_RUN_ID']+'-a'+os.environ.get('GITHUB_RUN_ATTEMPT','1')
 private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
 public=private.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
 name='.recording-key/public-'+epoch+'.json'
 record={'run':epoch,'branch':BRANCH,'key_id':KEY_ID,'public_key':public}
 api('/contents/'+name,{'branch':BRANCH,'message':'Register public key for recording-only job','content':base64.b64encode(json.dumps(record).encode()).decode()})
 print(json.dumps({'stage':'awaiting_sealed_key','public_key_path':name,'run':epoch}),flush=True)
 deadline=time.monotonic()+600
 while time.monotonic()<deadline:
  try:
   response=api('/contents/.recording-key/sealed-'+epoch+'.json?ref='+BRANCH)
  except urllib.error.HTTPError as exc:
   if exc.code!=404:raise
   time.sleep(5);continue
  envelope=json.loads(base64.b64decode(response['content']))
  if envelope.get('run')!=epoch or envelope.get('key_id')!=KEY_ID or envelope.get('branch')!=BRANCH:raise ValueError('Key envelope identity mismatch')
  key=private.decrypt(base64.b64decode(envelope['sealed_key'],validate=True),padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))
  if len(key)!=32:raise ValueError('Invalid key length')
  recipe(key)
  target=WORK/'recording-key.bin';target.write_bytes(key);target.chmod(0o600)
  print('Complete recipe authenticated; no private keys were published',flush=True)
  return
 raise TimeoutError('No sealed key arrived; stopped before speech rendering')


def engine():
 bundle=base64.b64decode((ROOT/'audio-engine/v2/engine.b64').read_text())
 if digest(bundle)!=(ROOT/'audio-engine/v2/engine.sha256').read_text().strip():raise ValueError('Audio-engine checksum mismatch')
 runtime=WORK/'runtime';runtime.mkdir(exist_ok=True)
 with zipfile.ZipFile(io.BytesIO(bundle)) as z:
  if any(Path(n).name!=n or not n.endswith('.py') for n in z.namelist()):raise ValueError('Unsafe engine archive')
  z.extractall(runtime)
 sys.path.insert(0,str(runtime));return runtime


def push_checkpoint(message):
 run('git','add','-f','--','recordings')
 code=subprocess.run(['git','diff','--cached','--quiet'],cwd=ROOT).returncode
 if code not in (0,1):raise RuntimeError('Cannot inspect staged files')
 if code:run('git','commit','-m',message,stdout=subprocess.DEVNULL)
 for attempt in range(4):
  run('git','pull','--rebase','origin',BRANCH,timeout=240)
  if subprocess.run(['git','push','origin','HEAD:'+BRANCH],cwd=ROOT,timeout=240).returncode==0:break
  if attempt==3:raise RuntimeError('Recording checkpoint push failed')
  time.sleep(2**(attempt+1))
 expected=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
 actual=subprocess.check_output(['git','ls-remote','origin','refs/heads/'+BRANCH],cwd=ROOT,text=True,timeout=30).split()[0]
 if actual!=expected:raise ValueError('Remote branch did not retain the checkpoint')
 return actual


def validate_record(record,key):
 from cryptography.hazmat.primitives.ciphers.aead import AESGCM
 parts=[]
 for entry in record['chunks']:
  p=(ROOT/entry['src']).resolve()
  if not p.is_relative_to((OUT/'audio').resolve()):raise ValueError('Unsafe recording path')
  data=p.read_bytes()
  if not 0<len(data)<=LIMIT or len(data)!=entry['bytes'] or digest(data)!=entry['sha256']:raise ValueError('Saved audio chunk failed')
  parts.append(data)
 cipher=b''.join(parts)
 if len(cipher)!=record['bytes'] or digest(cipher)!=record['sha256']:raise ValueError('Saved chapter reassembly failed')
 plain=AESGCM(key).decrypt(cipher[:12],cipher[12:],('Taliesin:v2:'+record['id']).encode())
 if digest(plain)!=record['mp3_sha256']:raise ValueError('Saved MP3 differs')
 temp=WORK/'verify.mp3';temp.write_bytes(plain)
 try:run('ffmpeg','-v','error','-nostdin','-xerror','-i',str(temp),'-f','null','-',timeout=240)
 finally:temp.unlink(missing_ok=True)
 return True


def render_all():
 import numpy as np
 import soundfile as sf
 from cryptography.hazmat.primitives.ciphers.aead import AESGCM
 key=(WORK/'recording-key.bin').read_bytes();rows,cues,source_script_sha=recipe(key)
 runtime=engine()
 from mix_audio import grouped_segments,mix_chapter,load_bank,PART_NAMES
 from sound_design import sound_bank
 groups=grouped_segments(rows)
 if [f'b{k[0]}-c{k[1]:02}' for k,_ in groups]!=ORDER:raise ValueError('Unexpected chapter order')
 run('git','config','user.name','github-actions[bot]');run('git','config','user.email','41898282+github-actions[bot]@users.noreply.github.com')
 sound_bank(WORK/'foley');bank=load_bank(WORK/'foley')
 (OUT/'records').mkdir(parents=True,exist_ok=True)
 master_export=Path('/tmp/encrypted-native-masters');master_export.mkdir(exist_ok=True)
 completed=[];start=time.monotonic()
 for pair,chapter in groups:
  tid=f'b{pair[0]}-c{pair[1]:02}';record_path=OUT/'records'/(tid+'.json')
  if record_path.exists():
   record=read(record_path)
   if record['segments']!=len(chapter) or record.get('key_id')!=KEY_ID:raise ValueError('Conflicting saved chapter')
   validate_record(record,key);completed.append(tid);continue
  if time.monotonic()-start>280*60:raise TimeoutError('Safe job limit reached; completed chapters remain committed')
  print(json.dumps({'stage':'rendering_one_chapter','track':tid,'completed':len(completed),'expected':49}),flush=True)
  speech=WORK/'speech';speech.mkdir(exist_ok=True)
  unique={r['audio_key']:r for r in chapter}
  workers=min(4,max(1,os.cpu_count() or 1));bins=[[] for _ in range(workers)];weights=[0]*workers
  for row in sorted(unique.values(),key=lambda r:len(r['tts_text']),reverse=True):
   i=min(range(workers),key=lambda j:weights[j]);bins[i].append(row);weights[i]+=len(row['tts_text'])
  processes=[];logs=[]
  try:
   for i,batch in enumerate(bins):
    if not batch:continue
    path=WORK/f'batch-{i}.json';path.write_text(json.dumps({'segments':batch},ensure_ascii=False));path.chmod(0o600)
    log=(WORK/f'batch-{i}.log').open('w');logs.append(log)
    processes.append(subprocess.Popen([sys.executable,str(runtime/'render_one_thread.py'),str(path)],stdout=log,stderr=subprocess.STDOUT))
   end=time.monotonic()+1200
   while any(p.poll() is None for p in processes):
    if any(p.poll() not in (None,0) for p in processes):raise RuntimeError('Speech segment worker failed')
    if time.monotonic()>end:raise TimeoutError('One chapter exceeded its speech limit')
    time.sleep(1)
   if any(p.returncode!=0 for p in processes):raise RuntimeError('Speech segment worker failed')
  finally:
   for p in processes:
    if p.poll() is None:p.terminate()
   for log in logs:log.close()
  for row in chapter:
   data,rate=sf.read(speech/(row['audio_key']+'.wav'),dtype='float32')
   if rate!=22050 or data.ndim!=1 or len(data)<1000 or not np.isfinite(data).all() or float(np.max(np.abs(data)))<.0001:raise ValueError('Invalid speech segment')
  mixed=WORK/'mix';mixed.mkdir(exist_ok=True)
  result=mix_chapter(pair,chapter,speech,mixed,bank,cues)
  if [v['segment_id'] for v in result['segments']]!=[v['id'] for v in chapter]:raise ValueError('Reading segment order changed')
  if result['peak_after_master']>.951:raise ValueError('Unsafe master peak')
  master=mixed/result['path'];mp3=WORK/'chapter.mp3'
  run('ffmpeg','-v','error','-nostdin','-y','-i',str(master),'-map_metadata','-1','-c:a','libmp3lame','-b:a','96k','-ar','22050','-ac','2',str(mp3),timeout=240)
  run('ffmpeg','-v','error','-nostdin','-xerror','-i',str(mp3),'-f','null','-',timeout=240)
  probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_name,sample_rate,channels,bit_rate','-of','json',str(mp3)],timeout=30))
  stream=probe['streams'][0];duration=float(probe['format']['duration'])
  if stream['codec_name']!='mp3' or int(stream['sample_rate'])!=22050 or stream['channels']!=2 or int(stream['bit_rate'])!=96000 or abs(duration-result['duration'])>.25:raise ValueError('MP3 format or duration mismatch')
  plain=mp3.read_bytes();nonce=os.urandom(12);aad=('Taliesin:v2:'+tid).encode();cipher=nonce+AESGCM(key).encrypt(nonce,plain,aad)
  hash_=digest(cipher);folder=OUT/'audio'/(tid+'-'+hash_[:16]);folder.mkdir(parents=True,exist_ok=True);chunks=[]
  for index,offset in enumerate(range(0,len(cipher),LIMIT)):
   part=cipher[offset:offset+LIMIT];target=folder/f'{index:04}.bin';target.write_bytes(part)
   chunks.append({'src':str(target.relative_to(ROOT)),'bytes':len(part),'sha256':digest(part)})
  record={'id':tid,'order':ORDER.index(tid),'book':pair[0],'chapter':pair[1],'part':PART_NAMES.get(pair[0],'Opening'),'title':f'Chapter {pair[1]}' if pair[0] else 'Title and opening verse',
   'duration':duration,'bytes':len(cipher),'sha256':hash_,'mp3_sha256':digest(plain),'chunks':chunks,'segments':len(chapter),
   'sample_rate':22050,'channels':2,'bit_rate':96000,'key_id':KEY_ID,'source_sha256':SOURCE,'source_script_sha256':source_script_sha,
   'verified':True,'full_decode_pass':True,'reassembly_byte_exact':True,'ordered_segment_coverage':True,
   'sound_events':len(result['events']),'ambience_intervals':len(result['ambience']),'master_peak':result['peak_after_master'],
   'duration_anomaly_count':len(result['duration_anomalies']),'render_run_id':os.environ['GITHUB_RUN_ID']}
  validate_record(record,key)
  buffer=io.BytesIO()
  with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_STORED) as z:
   z.write(master,master.name);z.writestr('mix-verification.json',json.dumps(result))
  raw=buffer.getvalue();nonce=os.urandom(12);master_cipher=nonce+AESGCM(key).encrypt(nonce,raw,('Taliesin:master-archive:v1:'+tid).encode())
  (master_export/(tid+'.bin')).write_bytes(master_cipher)
  write(master_export/(tid+'.json'),{'id':tid,'key_id':KEY_ID,'cipher_sha256':digest(master_cipher),'plain_sha256':digest(raw),'bytes':len(master_cipher)})
  write(record_path,record);completed.append(tid)
  write(OUT/'progress.json',{'stage':'chapter_verified','track':tid,'completed_tracks':len(completed),'expected_tracks':49,'parallel_chapters':1,'player_deployed':False,'completed':completed})
  commit=push_checkpoint('Save verified recording '+tid+' without deploying the player')
  print(json.dumps({'stage':'recording_saved_to_repository','track':tid,'commit':commit,'completed':len(completed),'duration':duration,'chunks':len(chunks)}),flush=True)
  shutil.rmtree(speech);shutil.rmtree(mixed);mp3.unlink()
 records=[read(OUT/'records'/(tid+'.json')) for tid in ORDER]
 if sum(r['segments'] for r in records)!=11105:raise ValueError('Final source segment coverage differs')
 final={'stage':'all_recordings_complete','tracks':49,'numbered_chapters':48,'opening_tracks':1,'reading_segments':11105,
  'duration_seconds':sum(r['duration'] for r in records),'audio_bytes':sum(r['bytes'] for r in records),'maximum_chunk_bytes':max(c['bytes'] for r in records for c in r['chunks']),
  'all_mp3s_fully_decoded':True,'all_reassemblies_byte_exact':True,'all_ordered_segments_covered':True,'key_id':KEY_ID,
  'source_sha256':SOURCE,'player_deployed':False,'parallel_chapters':1,'run_id':os.environ['GITHUB_RUN_ID']}
 write(OUT/'complete.json',final);write(OUT/'progress.json',final);push_checkpoint('Finish all 49 verified recordings on the recordings-only branch')
 print(json.dumps(final),flush=True)


if __name__=='__main__':
 WORK.mkdir(exist_ok=True,mode=0o700)
 mode=sys.argv[1] if len(sys.argv)==2 else ''
 if mode=='plan':
  manifest,blob=input_bytes();print(json.dumps({'input_verified':True,'input_bytes':len(blob),'parallel_chapters':1,'player_deployment':False}))
 elif mode=='key':key_step()
 elif mode=='render':render_all()
 else:raise SystemExit('Expected plan, key or render')
