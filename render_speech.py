"""Render cached neural speech locally using one persistent Piper process.

Interrupted jobs resume by content hash. No text is sent to a network service.
"""
from __future__ import annotations
import argparse, hashlib, json, os, subprocess, time
from pathlib import Path
import soundfile as sf
import numpy as np

def valid(path:Path)->bool:
 try:
  info=sf.info(path)
  return info.frames>1000 and info.samplerate==22050 and info.channels==1
 except Exception:return False

def render(script:Path,model:Path,binary:Path,out:Path,mode:str='all',limit:int=0)->dict:
 out.mkdir(parents=True,exist_ok=True)
 data=json.loads(script.read_text());ss=data['segments']
 if mode=='narration':ss=[s for s in ss if not s['quoted']]
 elif mode=='dialogue':ss=[s for s in ss if s['quoted']]
 if limit:ss=ss[:limit]
 done=skipped=0;seconds=0.;start=time.time();errors=[]
 logpath=out.parent/f'piper-{mode}.log'
 with logpath.open('a') as err:
  cmd=[str(binary),'-m',str(model),'--json-input','--length_scale','1.45','--sentence_silence','0.17','--noise_scale','.30','--noise_w','.30','-q']
  p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,text=True,bufsize=1)
  try:
   for s in ss:
    path=out/(s['audio_key']+'.wav')
    if valid(path):skipped+=1
    else:
     payload={'text':s['tts_text'],'speaker_id':s['speaker_id'],'output_file':str(path)}
     p.stdin.write(json.dumps(payload,ensure_ascii=False)+'\n');p.stdin.flush()
     reply=p.stdout.readline().strip()
     if reply!=str(path) or not valid(path):
      raise RuntimeError(f'Render failed for segment {s["id"]}: {reply!r}')
     done+=1
    seconds+=sf.info(path).duration
    if (done+skipped)%25==0 or done+skipped==len(ss):
     status={'mode':mode,'processed':done+skipped,'total':len(ss),'new':done,'cached':skipped,
       'audio_hours':round(seconds/3600,3),'elapsed_seconds':round(time.time()-start,1),
       'last_segment':s['id'],'last_page':s['page'],'book':s['book'],'chapter':s['chapter']}
     (out.parent/f'status-{mode}.json').write_text(json.dumps(status,indent=2))
     print(json.dumps(status),flush=True)
   p.stdin.close();code=p.wait(timeout=30)
   if code:raise RuntimeError(f'Piper exited {code}')
  finally:
   if p.poll() is None:p.terminate()
 result={'complete':True,'mode':mode,'processed':len(ss),'audio_seconds':seconds,'new':done,'cached':skipped,'elapsed':time.time()-start}
 (out.parent/f'complete-{mode}.json').write_text(json.dumps(result,indent=2))
 print(json.dumps(result),flush=True)
 return result

if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('script',type=Path);a.add_argument('--model',type=Path,required=True);a.add_argument('--binary',type=Path,required=True);a.add_argument('--output',type=Path,required=True);a.add_argument('--mode',choices=['all','narration','dialogue'],default='all');a.add_argument('--limit',type=int,default=0)
 v=a.parse_args();render(v.script,v.model,v.binary,v.output,v.mode,v.limit)
