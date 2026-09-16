"""Exercise the player with generated audio, never with a private manuscript."""
import base64,hashlib,json,os,secrets,shutil,subprocess,tempfile,time
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from playwright.sync_api import sync_playwright
repo=Path(__file__).resolve().parents[2]
root=Path(tempfile.mkdtemp(prefix='player-qa-'));site=root/'site';shutil.copytree(repo/'player',site)
key=secrets.token_bytes(32);code=base64.urlsafe_b64encode(key).decode().rstrip('=')
nonce=secrets.token_bytes(12)
(site/'vault.json').write_text(json.dumps({'version':2,'proof':base64.b64encode(nonce+AESGCM(key).encrypt(nonce,b'Taliesin private player',b'Taliesin:v2:unlock')).decode()}))
tone=root/'tone.mp3'
subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=220:sample_rate=22050:duration=90','-filter:a','volume=0.01','-ac','2','-c:a','libmp3lame','-b:a','96k',str(tone)],check=True)
plain=tone.read_bytes();audio=site/'audio-v2';audio.mkdir(exist_ok=True);tracks=[]
for book,count in [(0,0),(1,16),(2,14),(3,18)]:
 for chapter in ([0] if book==0 else range(1,count+1)):
  tid=f'b{book}-c{chapter:02}';nonce=secrets.token_bytes(12);data=nonce+AESGCM(key).encrypt(nonce,plain,('Taliesin:v2:'+tid).encode());name=tid+'.bin';(audio/name).write_bytes(data)
  tracks.append({'id':tid,'order':len(tracks),'book':book,'chapter':chapter,'part':{0:'Opening',1:'A Gift of Jade',2:'The Sun Bull',3:'The Merlin'}[book],'title':f'Chapter {chapter}' if book else 'Title and opening verse','duration':90.096326,'src':'audio-v2/'+name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
(site/'catalog.json').write_text(json.dumps({'version':2,'ready':True,'tracks':tracks,'total_duration':sum(t['duration'] for t in tracks),'total_bytes':sum(t['bytes'] for t in tracks)}))
server=subprocess.Popen(['python','-m','http.server','8765','--bind','127.0.0.1','--directory',str(site)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(.5)
results=[];out=repo/'qa-results';out.mkdir(exist_ok=True)
try:
 with sync_playwright() as p:
  for name in ['chromium','webkit']:
   browser=getattr(p,name).launch(headless=True);ctx=browser.new_context(viewport={'width':390,'height':844},device_scale_factor=1,is_mobile=True,has_touch=True)
   page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   page.goto('http://127.0.0.1:8765/#k='+code)
   page.wait_for_function("document.getElementById('play').disabled === false",timeout=60000)
   page.locator('#play').click();page.wait_for_function("!document.getElementById('audio').paused",timeout=10000);page.wait_for_timeout(300)
   initial=page.eval_on_selector('#audio','a=>a.currentTime');page.locator('#forward').tap();page.wait_for_timeout(200);after=page.eval_on_selector('#audio','a=>a.currentTime');assert after-initial>9
   page.locator('#rewind').tap();page.wait_for_timeout(100);assert page.eval_on_selector('#audio','a=>a.currentTime')<after-8
   box=page.locator('#forward').bounding_box();page.mouse.move(box['x']+box['width']/2,box['y']+box['height']/2);page.mouse.down();page.wait_for_timeout(1250);page.mouse.up()
   held=page.eval_on_selector('#audio','a=>a.currentTime');assert held>25
   page.locator('#play').click();page.wait_for_timeout(500);assert abs(page.eval_on_selector('#audio','a=>a.currentTime')-held)<1
   page.locator('#chaptersButton').click();assert page.locator('#chapterList button').count()==49;page.locator('[data-part="2"]').click();assert page.locator('#chapterList button').count()==14;page.locator('#chapterList button').nth(2).click()
   page.wait_for_function("document.getElementById('play').disabled === false");assert page.locator('#chapterTitle').inner_text()=='Chapter 3'
   page.eval_on_selector('#audio','a=>{a.pause();a.currentTime=33;a.dispatchEvent(new Event("timeupdate"));a.dispatchEvent(new Event("pause"))}')
   page.screenshot(path=str(out/f'{name}-mobile.png'),full_page=True);assert page.evaluate('document.documentElement.scrollWidth')<=390
   page.reload();page.wait_for_function("document.getElementById('play').disabled === false");assert page.locator('#chapterTitle').inner_text()=='Chapter 3';assert 32<page.eval_on_selector('#audio','a=>a.currentTime')<34
   page.locator('#saveCurrent').click();page.wait_for_function("document.getElementById('saveCurrent').textContent.includes('Saved offline')",timeout=30000)
   page.evaluate('navigator.serviceWorker.ready');page.reload();page.wait_for_function("document.getElementById('play').disabled === false")
   ctx.set_offline(True);page.reload();page.wait_for_function("document.getElementById('play').disabled === false",timeout=30000);page.locator('#play').click();page.wait_for_function("!document.getElementById('audio').paused");assert page.eval_on_selector('#audio','a=>a.playbackRate')==1
   assert not errors,errors
   results.append({'browser':name,'passed':True,'tap_and_hold_seek':True,'offline_reload':True,'resume':True,'chapter_selection':True,'overflow':False});browser.close()
finally:
 server.terminate();shutil.rmtree(root)
(out/'ui-results.json').write_text(json.dumps(results,indent=2));print(json.dumps(results))
