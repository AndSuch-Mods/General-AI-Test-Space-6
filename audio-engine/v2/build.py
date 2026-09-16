"""Render native-rate chapter masters; export verified 96 kbps encrypted MP3s."""
import base64,hashlib,json,os,subprocess,sys,zipfile,io
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
HERE=Path(__file__).resolve().parent
bundle=base64.b64decode((HERE/'engine.b64').read_text())
if hashlib.sha256(bundle).hexdigest()!=(HERE/'engine.sha256').read_text().strip():raise RuntimeError('Engine checksum failed')
with zipfile.ZipFile(io.BytesIO(bundle)) as z:
 for name in z.namelist():
  if Path(name).name!=name or not name.endswith('.py'):raise ValueError('Unsafe engine archive')
 z.extractall(HERE/'runtime')
sys.path.insert(0,str(HERE/'runtime'))
from mix_audio import grouped_segments,mix_chapter,load_bank,PART_NAMES
from sound_design import sound_bank
work=Path('/tmp/private-audio');r=json.loads((work/'recipe.json').read_text())
if r['source_sha256']!='707254842a0c590201adb29f0f07cc39501f001c031a58f3d850728a4265bcc5' or not r['stats']['coverage_pass']:raise ValueError('Unverified source')
key=base64.b64decode(r['player_key']);segments=[]
for v in zip(*r['segments']):
 text=r['texts'][v[9]];sid=v[8]
 segments.append({'id':v[0],'paragraph_id':v[1],'book':v[2],'chapter':v[3],'page':v[4],'kind':r['kinds'][v[5]],'quoted':bool(v[6]),'actor':r['actors'][v[7]],'speaker_id':sid,'text':text,'tts_text':text,'rate':v[10],'delivery':r['deliveries'][v[11]],'pause_after':v[12],'tempo_adjust':v[13],'audio_key':hashlib.sha256((str(sid)+'|'+text).encode()).hexdigest()[:24]})
cues={v[0]:{'paragraph_id':v[0],'ambience':v[1],'events':[{'sound':e[0],'fraction':e[1],'gain':e[2]} for e in v[2]]} for v in zip(*r['cues'])};del r
groups=grouped_segments(segments)
if len(segments)!=11105 or len(groups)!=49:raise ValueError('Incomplete reading')
index=int(os.environ['RENDER_INDEX']);total=int(os.environ['RENDER_TOTAL']);mine=[(i,k,s) for i,(k,s) in enumerate(groups) if i%total==index]
rows=[s for _,_,ss in mine for s in ss];shard=work/'shard.json';shard.write_text(json.dumps({'segments':rows},ensure_ascii=False));os.chmod(shard,0o600)
subprocess.run([sys.executable,str(HERE/'runtime/render_one_thread.py'),str(shard)],check=True)
sound_bank(work/'foley');bank=load_bank(work/'foley');out=work/'mix';out.mkdir(exist_ok=True);publish=work/'publish';publish.mkdir(exist_ok=True)
for order,pair,ss in mine:
 report=mix_chapter(pair,ss,work/'speech',out,bank,cues);mp3=out/'chapter.mp3'
 subprocess.run(['ffmpeg','-hide_banner','-nostdin','-v','error','-y','-i',str(out/report['path']),'-map_metadata','-1','-c:a','libmp3lame','-b:a','96k','-ar','22050','-ac','2',str(mp3)],check=True)
 subprocess.run(['ffmpeg','-hide_banner','-nostdin','-v','error','-i',str(mp3),'-f','null','-'],check=True)
 duration=float(json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(mp3)]))['format']['duration'])
 if abs(duration-report['duration'])>.25:raise RuntimeError('Chapter was truncated')
 tid=f'b{pair[0]}-c{pair[1]:02}';nonce=os.urandom(12);plain=mp3.read_bytes();cipher=nonce+AESGCM(key).encrypt(nonce,plain,('Taliesin:v2:'+tid).encode())
 if AESGCM(key).decrypt(nonce,cipher[12:],('Taliesin:v2:'+tid).encode())!=plain:raise RuntimeError('Encryption failed')
 digest=hashlib.sha256(cipher).hexdigest();name=tid+'-'+digest[:16]+'.bin';(publish/name).write_bytes(cipher)
 record={'id':tid,'order':order,'book':pair[0],'chapter':pair[1],'part':PART_NAMES.get(pair[0],'Opening'),'title':f'Chapter {pair[1]}' if pair[0] else 'Title and opening verse','duration':duration,'src':'audio-v2/'+name,'bytes':len(cipher),'sha256':digest,'segments':len(ss),'sample_rate':22050,'bit_rate':96000,'channels':2,'verified':True}
 (publish/(tid+'.json')).write_text(json.dumps(record,indent=2));print(json.dumps(record),flush=True);mp3.unlink();(out/report['path']).unlink()
