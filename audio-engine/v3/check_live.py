"""Verify the published ciphertext and actual chapter audio in real browsers."""
from pathlib import Path
import base64, hashlib, json, os, time, urllib.request
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'qa-results';OUT.mkdir(exist_ok=True)
owner,repo=os.environ['GITHUB_REPOSITORY'].split('/')
url=f'https://{owner.lower()}.github.io/{repo}/player/'
key=Path('/tmp/private-audio/browser-key.bin').read_bytes()
code=base64.urlsafe_b64encode(key).decode().rstrip('=')
expected=json.loads((ROOT/'player/catalog.json').read_text())
def get(path):
    req=urllib.request.Request(url+path,headers={'User-Agent':'chapter-publication-check','Cache-Control':'no-cache'})
    with urllib.request.urlopen(req,timeout=45) as response:return response.read()
def sha(data):return hashlib.sha256(data).hexdigest()
end=time.monotonic()+240
while True:
    try:
        catalog=json.loads(get('catalog.json?verification='+str(int(time.time()))))
        if [t['sha256'] for t in catalog['tracks']]==[t['sha256'] for t in expected['tracks']]:break
    except Exception:pass
    if time.monotonic()>end:raise RuntimeError('Pages has not published this chapter catalog')
    time.sleep(8)
checks=[]
for track in catalog['tracks']:
    pieces=[]
    for part in track['chunks']:
        raw=get(part['src'])
        assert len(raw)==part['bytes'] and sha(raw)==part['sha256'], 'Published piece mismatch'
        pieces.append(raw)
    data=b''.join(pieces)
    assert sha(data)==track['sha256'], 'Published chapter mismatch'
    mp3=AESGCM(key).decrypt(data[:12],data[12:],('Taliesin:v2:'+track['id']).encode())
    assert sha(mp3)==track['mp3_sha256'], 'Published MP3 differs from verified master'
    checks.append({'id':track['id'],'parts':len(pieces),'bytes':len(data),'duration':track['duration'],'remote_byte_exact':True})

def wait(page,expr,seconds=90):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if page.evaluate('() => Boolean('+expr+')'):return
        page.wait_for_timeout(100)
    raise RuntimeError('Browser check timed out: '+expr)
def ready(page):wait(page,'!document.getElementById("play").disabled')
def pos(page):return page.eval_on_selector('#audio','a => a.currentTime')
results=[]
with sync_playwright() as p:
    for name in ('chromium','webkit'):
        browser=getattr(p,name).launch(headless=True)
        context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
        page=context.new_page();errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        try:
            page.goto(url,wait_until='domcontentloaded')
            page.locator('#unlockCode').fill(code)
            page.locator('#unlockForm').evaluate('form => form.requestSubmit()')
            ready(page)
            page.locator('#unlockCode').evaluate("input => input.value=''")
            page.locator('#play').click()
            wait(page,'!document.getElementById("audio").paused && document.getElementById("audio").currentTime>0.25')
            start=pos(page);page.locator('#forward').tap();page.wait_for_timeout(150)
            advanced=pos(page);assert advanced-start>9,'Tap forward failed'
            page.locator('#rewind').tap();page.wait_for_timeout(150);assert pos(page)<advanced-8
            box=page.locator('#forward').bounding_box();start=pos(page)
            page.mouse.move(box['x']+box['width']/2,box['y']+box['height']/2);page.mouse.down();page.wait_for_timeout(900);page.mouse.up()
            assert pos(page)-start>20,'Hold forward failed'
            page.locator('#play').click();paused=pos(page);page.wait_for_timeout(300);assert abs(pos(page)-paused)<.5
            if len(catalog['tracks'])>1:
                page.eval_on_selector('#audio','a=>{a.currentTime=a.duration-0.4;a.play();}')
                wait(page,'document.getElementById("chapterTitle").textContent === "Chapter 1"')
                ready(page);wait(page,'!document.getElementById("audio").paused && document.getElementById("audio").currentTime>0.25')
            page.eval_on_selector('#audio','a=>{a.pause();a.currentTime=33;a.dispatchEvent(new Event("timeupdate"));a.dispatchEvent(new Event("pause"));}')
            page.reload();ready(page);assert 32<pos(page)<34,'Resume failed'
            page.locator('#chaptersButton').click();assert page.locator('#chapterList button').count()==len(catalog['tracks'])
            page.locator('#chapterList button').last.click();ready(page)
            assert abs(page.eval_on_selector('#audio','a=>a.duration')-catalog['tracks'][-1]['duration'])<.2
            assert page.eval_on_selector('#audio','a=>a.playbackRate')==1
            assert page.evaluate('document.documentElement.scrollWidth')<=390
            page.screenshot(path=str(OUT/(name+'-live-player.png')),full_page=True)
            page.locator('#saveCurrent').click();wait(page,'document.getElementById("saveCurrent").textContent.includes("Saved offline")')
            page.evaluate('navigator.serviceWorker.ready')
            result={'browser':name,'actual_recording_played':True,'tap_and_hold_seek':True,'resume':True,'chapter_selection':True,
                    'auto_advance':len(catalog['tracks'])>1,'offline_cache_saved':True,'normal_speed':True,'mobile_overflow':False}
            context.set_offline(True)
            try:
                page.reload();ready(page);page.locator('#play').click();wait(page,'!document.getElementById("audio").paused')
                result['offline_reload']='passed'
            except Exception as error:
                if name!='webkit' or 'WebKit encountered an internal error' not in str(error):raise
                result['offline_reload']='unverified: Linux WebKit offline navigation internal error'
            finally:context.set_offline(False)
            assert not errors, 'Browser JavaScript errors'
            results.append(result)
        finally:
            browser.close()
report={'url':url,'published_tracks':len(checks),'expected_tracks':49,'complete':catalog.get('complete',False),'remote_files':checks,'browsers':results}
(OUT/'live-results.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
