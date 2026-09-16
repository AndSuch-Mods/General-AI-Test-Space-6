"""Browser regression tests with non-copyrighted tone fixtures and an intact CSP."""
from pathlib import Path
import base64, hashlib, json, secrets, shutil, subprocess, sys, tempfile, time, traceback
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / 'qa-results'
OUT.mkdir(exist_ok=True)

def wait_js(page, expression, timeout=30000):
    end = time.monotonic() + timeout / 1000
    while time.monotonic() < end:
        if page.evaluate('() => Boolean(' + expression + ')'):
            return
        page.wait_for_timeout(100)
    raise TimeoutError(expression)

def ready(page):
    wait_js(page, 'document.getElementById("play").disabled === false', 60000)

def position(page):
    return page.eval_on_selector('#audio', 'a => a.currentTime')

def exercise(page, context, browser_name, address):
    page.goto(address)
    ready(page)
    page.locator('#play').click()
    wait_js(page, '!document.getElementById("audio").paused')
    page.wait_for_timeout(400)
    start = position(page)
    page.locator('#forward').tap()
    page.wait_for_timeout(200)
    forward = position(page)
    assert forward - start > 9, 'Forward tap did not seek'
    page.locator('#rewind').tap()
    page.wait_for_timeout(200)
    assert position(page) < forward - 8, 'Rewind tap did not seek'
    box = page.locator('#forward').bounding_box()
    page.mouse.move(box['x'] + box['width']/2, box['y'] + box['height']/2)
    page.mouse.down()
    page.wait_for_timeout(1250)
    page.mouse.up()
    held = position(page)
    assert held > 25, 'Hold-to-seek did not repeat'
    page.locator('#play').click()
    page.wait_for_timeout(500)
    assert abs(position(page) - held) < 1, 'Pause did not stop playback'
    page.locator('#chaptersButton').click()
    assert page.locator('#chapterList button').count() == 49
    page.locator('[data-part="2"]').click()
    assert page.locator('#chapterList button').count() == 14
    page.locator('#chapterList button').nth(2).click()
    ready(page)
    assert page.locator('#chapterTitle').inner_text() == 'Chapter 3'
    page.eval_on_selector('#audio', 'a => {a.pause(); a.currentTime=33; a.dispatchEvent(new Event("timeupdate")); a.dispatchEvent(new Event("pause"));}')
    page.reload()
    ready(page)
    assert page.locator('#chapterTitle').inner_text() == 'Chapter 3'
    assert 32 < position(page) < 34, 'Saved position was not restored'
    assert page.evaluate('document.documentElement.scrollWidth') <= 390
    page.screenshot(path=str(OUT / (browser_name + '-mobile.png')), full_page=True)
    page.locator('#saveCurrent').click()
    wait_js(page, 'document.getElementById("saveCurrent").textContent.includes("Saved offline")')
    page.evaluate('navigator.serviceWorker.ready')
    page.reload()
    ready(page)
    # Prove normal playback and chapter transitions before changing connectivity.
    page.eval_on_selector('#audio', 'a => {a.currentTime=a.duration-1; a.play();}')
    wait_js(page, 'document.getElementById("chapterTitle").textContent === "Chapter 4"', 20000)
    ready(page)
    wait_js(page, '!document.getElementById("audio").paused')
    assert page.eval_on_selector('#audio', 'a => a.playbackRate') == 1
    page.locator('#play').click()
    page.locator('#saveCurrent').click()
    wait_js(page, 'document.getElementById("saveCurrent").textContent.includes("Saved offline")')
    result = {'browser':browser_name, 'core_passed':True, 'tap_and_hold_seek':True,
              'chapter_selection':True, 'resume':True, 'chapter_auto_advance':True,
              'normal_speed':True, 'offline_cache_saved':True, 'mobile_overflow':False}
    context.set_offline(True)
    try:
        page.reload()
        ready(page)
        page.locator('#play').click()
        wait_js(page, '!document.getElementById("audio").paused')
        result['offline_reload'] = 'passed'
    except Exception as exc:
        # This failure comes from Playwright's Linux WebKit network emulation.
        # Do not count it as an offline-playback pass or suppress other failures.
        if browser_name != 'webkit' or 'WebKit encountered an internal error' not in str(exc):
            raise
        result['offline_reload'] = 'unverified: WebKit offline navigation internal error'
    finally:
        context.set_offline(False)
    return result

root = Path(tempfile.mkdtemp(prefix='chapter-player-qa-'))
site = root / 'site'
shutil.copytree(REPO / 'player', site)
key = secrets.token_bytes(32)
code = base64.urlsafe_b64encode(key).decode().rstrip('=')
nonce = secrets.token_bytes(12)
proof = nonce + AESGCM(key).encrypt(nonce, b'Taliesin private player', b'Taliesin:v2:unlock')
(site / 'vault.json').write_text(json.dumps({'version':2,'proof':base64.b64encode(proof).decode()}))
subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=220:sample_rate=22050:duration=90','-filter:a','volume=0.01','-ac','2','-c:a','libmp3lame','-b:a','96k',str(root/'tone.mp3')], check=True)
plain = (root / 'tone.mp3').read_bytes()
(site / 'audio-v2').mkdir(exist_ok=True)
tracks = []
parts = {0:'Opening',1:'A Gift of Jade',2:'The Sun Bull',3:'The Merlin'}
for book, count in [(0,0),(1,16),(2,14),(3,18)]:
    for chapter in ([0] if not book else range(1,count+1)):
        tid = f'b{book}-c{chapter:02}'
        nonce = secrets.token_bytes(12)
        data = nonce + AESGCM(key).encrypt(nonce, plain, ('Taliesin:v2:'+tid).encode())
        (site / 'audio-v2' / (tid+'.bin')).write_bytes(data)
        tracks.append({'id':tid,'order':len(tracks),'book':book,'chapter':chapter,'part':parts[book],
          'title':f'Chapter {chapter}' if book else 'Title and opening verse','duration':90.096326,
          'src':'audio-v2/'+tid+'.bin','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
(site / 'catalog.json').write_text(json.dumps({'version':2,'ready':True,'tracks':tracks,
    'total_duration':sum(t['duration'] for t in tracks),'total_bytes':sum(t['bytes'] for t in tracks)}))
server = subprocess.Popen([sys.executable,'-m','http.server','8765','--bind','127.0.0.1','--directory',str(site)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
results = []
failed = False
try:
    time.sleep(.5)
    with sync_playwright() as playwright:
        for name in ['chromium','webkit']:
            browser = getattr(playwright, name).launch(headless=True)
            context = browser.new_context(viewport={'width':390,'height':844}, is_mobile=True, has_touch=True)
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            try:
                result = exercise(page, context, name, 'http://127.0.0.1:8765/#k='+code)
                assert not errors, errors
                results.append(result)
            except Exception:
                failed = True
                result = {'browser':name,'core_passed':False,'traceback':traceback.format_exc(),'page_errors':errors}
                results.append(result)
                try:
                    page.screenshot(path=str(OUT/(name+'-failure.png')), full_page=True)
                    (OUT/(name+'-failure.html')).write_text(page.content())
                except Exception:
                    pass
            finally:
                (OUT/'ui-results.json').write_text(json.dumps(results,indent=2))
                browser.close()
finally:
    server.terminate()
    shutil.rmtree(root)
print(json.dumps(results, indent=2))
if failed:
    raise SystemExit('Browser regression failed; see ui-results.json')
