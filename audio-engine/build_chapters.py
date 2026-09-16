"""Render reviewed chapter segments and publish only authenticated ciphertext."""
from __future__ import annotations
import base64,hashlib,json,os,subprocess,sys,time
from pathlib import Path
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
ENGINE=Path(__file__).resolve().parent
sys.path.insert(0,str(ENGINE/'runtime'))
from mix_audio import grouped_segments,mix_chapter,load_bank,PART_NAMES
from sound_design import sound_bank
SOURCE='707254842a0c590201adb29f0f07cc39501f001c031a58f3d850728a4265bcc5'
def b64(b):return base64.urlsafe_b64encode(b).decode().rstrip('=')
def unb64(s):return base64.urlsafe_b64decode(s+'='*(-len(s)%4))
def reconstruct(recipe):
 if recipe['source_sha256']!=SOURCE or not recipe['stats']['coverage_pass']:raise ValueError('Wrong or unverified source')
 segments=[]
 for r in zip(*recipe['segments']):
  sid=r[8];text=recipe['texts'][r[9]]
  segments.append({'id':r[0],'paragraph_id':r[1],'book':r[2],'chapter':r[3],'page':r[4],
   'kind':recipe['kinds'][r[5]],'quoted':bool(r[6]),'actor':recipe['actors'][r[7]],'speaker_id':sid,
   'text':text,'tts_text':text,'rate':r[10],'delivery':recipe['deliveries'][r[11]],'pause_after':r[12],
   'tempo_adjust':r[13],'audio_key':hashlib.sha256((str(sid)+'|'+text).encode()).hexdigest()[:24]})
 cues={r[0]:{'paragraph_id':r[0],'ambience':r[1],'events':[{'sound':e[0],'fraction':e[1],'gain':e[2]} for e in r[2]]} for r in zip(*recipe['cues'])}
 if len(segments)!=11105 or len(grouped_segments(segments))!=49:raise ValueError('Incomplete reading script')
 return segments,cues

def encrypt_mp3(mp3,owner,track_id,destination):
 pub=ec.EllipticCurvePublicNumbers(int.from_bytes(unb64(owner['x']),'big'),int.from_bytes(unb64(owner['y']),'big'),ec.SECP256R1()).public_key()
 secret=ec.generate_private_key(ec.SECP256R1());salt=os.urandom(16)
 shared=secret.exchange(ec.ECDH(),pub)
 key=HKDF(algorithm=hashes.SHA256(),length=32,salt=salt,info=b'Taliesin audio v1').derive(shared)
 nonce=os.urandom(12);plain=mp3.read_bytes()
 cipher=nonce+AESGCM(key).encrypt(nonce,plain,track_id.encode());digest=hashlib.sha256(cipher).hexdigest()
 name=track_id+'-'+digest[:16]+'.bin';destination.mkdir(parents=True,exist_ok=True);(destination/name).write_bytes(cipher)
 if AESGCM(key).decrypt(cipher[:12],cipher[12:],track_id.encode())!=plain:raise RuntimeError('Encryption round-trip failed')
 seal={'ephemeral':b64(secret.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)),'salt':b64(salt)}
 return name,len(cipher),digest,seal

def main():
 work=Path(os.environ.get('AUDIO_WORK','/tmp/private-audio'));index=int(os.environ['RENDER_INDEX']);total=int(os.environ.get('RENDER_TOTAL','12'))
 recipe=json.loads((work/'recipe.json').read_text());ss,cues=reconstruct(recipe);del recipe
 allgroups=grouped_segments(ss);groups=[(i,k,rows) for i,(k,rows) in enumerate(allgroups) if i%total==index]
 subset=[row for _,_,rows in groups for row in rows];(work/'speech').mkdir(exist_ok=True)
 shard=work/f'worker{index}.json';shard.write_text(json.dumps({'segments':subset},ensure_ascii=False));os.chmod(shard,0o600)
 subprocess.run([sys.executable,str(ENGINE/'runtime/render_singlethread.py'),str(shard)],check=True)
 sound_bank(work/'foley');bank=load_bank(work/'foley');out=work/'mix';out.mkdir(exist_ok=True)
 publish=work/'publish';publish.mkdir(exist_ok=True)
 owner=json.loads((ENGINE.parent/'player/owner-public.json').read_text())
 for order,key,rows in groups:
  report=mix_chapter(key,rows,work/'speech',out,bank,cues)
  mp3=out/(f'b{key[0]}-c{key[1]:02}.mp3')
  subprocess.run(['ffmpeg','-hide_banner','-nostdin','-loglevel','error','-y','-i',str(out/report['path']),'-map_metadata','-1','-codec:a','libmp3lame','-b:a','64k','-ar','22050','-ac','2',str(mp3)],check=True)
  subprocess.run(['ffmpeg','-hide_banner','-nostdin','-v','error','-i',str(mp3),'-f','null','-'],check=True)
  probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(mp3)]))
  duration=float(probe['format']['duration'])
  if abs(duration-report['duration'])>.25:raise RuntimeError('Encoded chapter duration mismatch')
  track_id=f'b{key[0]}-c{key[1]:02}'
  name,size,digest,seal=encrypt_mp3(mp3,owner,track_id,publish)
  record={'id':track_id,'order':order,'book':key[0],'chapter':key[1],'part':PART_NAMES.get(key[0],'Opening'),
   'title':'Title and opening verse' if not key[0] else f'Chapter {key[1]}','duration':duration,'ready':True,
   'src':'audio/'+name,'bytes':size,'sha256':digest,'seal':seal,'segments':len(rows),'source_sha256':SOURCE}
  (publish/(track_id+'.json')).write_text(json.dumps(record,indent=2))
  print(json.dumps({'chapter':track_id,'duration':round(duration,3),'encrypted_bytes':size,'verified':True}),flush=True)
  mp3.unlink()
if __name__=='__main__':main()
