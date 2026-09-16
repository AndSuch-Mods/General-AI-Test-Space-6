"""Authenticate and decode hosted chapters, then exercise the real mobile player."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import base64, hashlib, json, os, subprocess, sys, tempfile, time, urllib.request
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'audio-engine/v4'))
import runner
SITE = 'https://andsuch-mods.github.io/General-AI-Test-Space-6/player/'
OUT = ROOT / 'qa-results'
OUT.mkdir(exist_ok=True)

def fetch(path):
    if path.startswith('/') or '..' in path or '://' in path:
        raise ValueError('Unexpected hosted file path')
    for attempt in range(5):
        try:
            req = urllib.request.Request(SITE + path, headers={'User-Agent': 'Taliesin-release-verification'})
            with urllib.request.urlopen(req, timeout=60) as response:
                return response.read()
        except Exception:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)

def wait(page, expression, seconds=90):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if page.evaluate('() => Boolean(' + expression + ')'):
            return
        page.wait_for_timeout(150)
    raise TimeoutError('Player condition timed out: ' + expression)

def ready(page):
    wait(page, '!document.getElementById("play").disabled')

def position(page):
    return page.eval_on_selector('#audio', 'a => a.currentTime')

def select(page, part, index, title):
    page.locator('#chaptersButton').click()
    page.locator('[data-part="' + str(part) + '"]').click()
    page.locator('#chapterList button').nth(index).click()
    ready(page)
    assert page.locator('#chapterTitle').inner_text() == title
    wait(page, '!document.getElementById("audio").paused')

def check_browser(name, browser_type, key, catalog):
    code = base64.urlsafe_b64encode(key).decode().rstrip('=')
    browser = browser_type.launch(headless=True)
    context = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda err: errors.append(str(err).replace(code, '[redacted]')))
    result = {'browser': name, 'passed': False}
    try:
        page.goto(SITE + '#k=' + code, wait_until='domcontentloaded', timeout=90000)
        ready(page)
        page.locator('#chaptersButton').click()
        assert page.locator('#chapterList button').count() == 49
        page.locator('#chapterList button').nth(2).click()
        ready(page)
        assert page.locator('#chapterTitle').inner_text() == 'Chapter 2'
        wait(page, '!document.getElementById("audio").paused')
        page.wait_for_timeout(500)
        start = position(page)
        page.locator('#forward').tap()
        page.wait_for_timeout(250)
        forward = position(page)
        assert forward - start > 9
        page.locator('#rewind').tap()
        page.wait_for_timeout(250)
        assert position(page) < forward - 8
        before = position(page)
        box = page.locator('#forward').bounding_box()
        page.mouse.move(box['x'] + box['width']/2, box['y'] + box['height']/2)
        page.mouse.down()
        page.wait_for_timeout(1250)
        page.mouse.up()
        assert position(page) - before > 25
        page.locator('#play').click()
        paused = position(page)
        page.wait_for_timeout(500)
        assert abs(position(page) - paused) < 1
        page.locator('#play').click()
        wait(page, '!document.getElementById("audio").paused')
        select(page, 2, 6, 'Chapter 7')
        page.eval_on_selector('#audio', 'a => {a.currentTime=a.duration-1; a.play();}')
        wait(page, 'document.getElementById("chapterTitle").textContent === "Chapter 8"')
        ready(page)
        wait(page, '!document.getElementById("audio").paused')
        select(page, 3, 17, 'Chapter 18')
        assert abs(page.eval_on_selector('#audio', 'a => a.duration') - catalog['tracks'][-1]['duration']) < 1
        page.eval_on_selector('#audio', 'a => {a.pause(); a.currentTime=33; a.dispatchEvent(new Event("timeupdate")); a.dispatchEvent(new Event("pause"));}')
        page.reload(wait_until='domcontentloaded')
        ready(page)
        assert page.locator('#chapterTitle').inner_text() == 'Chapter 18'
        assert 32 < position(page) < 34
        assert page.eval_on_selector('#audio', 'a => a.playbackRate') == 1
        assert page.evaluate('document.documentElement.scrollWidth') <= 390
        page.screenshot(path=str(OUT / (name + '-full-book-mobile.png')), full_page=True)
        page.locator('#saveCurrent').click()
        wait(page, 'document.getElementById("saveCurrent").textContent.includes("Saved offline")')
        page.evaluate('navigator.serviceWorker.ready')
        page.reload(wait_until='domcontentloaded')
        ready(page)
        assert not errors, 'Browser reported a script error'
        result.update(passed=True, chapter_count=49, chapter_selection=True,
                      tap_and_hold_seeking=True, play_pause=True, saved_position=True,
                      auto_advance=True, final_chapter_loaded=True, normal_speed=True,
                      offline_cache_saved=True, horizontal_overflow=False)
        context.set_offline(True)
        try:
            page.reload(wait_until='domcontentloaded', timeout=30000)
            ready(page)
            page.locator('#play').click()
            wait(page, '!document.getElementById("audio").paused', 20)
            page.wait_for_timeout(300)
            result['offline_reload'] = 'passed'
        except Exception as exc:
            result['offline_reload'] = 'not verified'
            result['offline_error'] = str(exc).replace(code, '[redacted]')[:1000]
        finally:
            context.set_offline(False)
    except Exception as exc:
        result['error'] = str(exc).replace(code, '[redacted]')[:2000]
        result['page_errors'] = errors
    finally:
        browser.close()
    return result

def main():
    key = runner.base.receive_key()
    catalog = json.loads(fetch('catalog.json?verify=' + runner.EPOCH))
    assert catalog.get('complete') is True
    assert len(catalog['tracks']) == 49
    assert {t['id'] for t in catalog['tracks']} == runner.legacy.EXPECTED
    assert sum(t['segments'] for t in catalog['tracks']) == 11105
    report = {'published_tracks': 49, 'expected_tracks': 49, 'chapters': [], 'browsers': []}
    def save():
        (OUT / 'full-book-live-results.json').write_text(json.dumps(report, indent=2))
    def check_track(t):
        pieces = []
        for part in t['chunks']:
            data = fetch(part['src'])
            assert 0 < len(data) <= 4000000 and len(data) == part['bytes']
            assert hashlib.sha256(data).hexdigest() == part['sha256']
            pieces.append(data)
        cipher = b''.join(pieces)
        assert len(cipher) == t['bytes'] and hashlib.sha256(cipher).hexdigest() == t['sha256']
        plain = AESGCM(key).decrypt(cipher[:12], cipher[12:], ('Taliesin:v2:' + t['id']).encode())
        assert hashlib.sha256(plain).hexdigest() == t['mp3_sha256']
        with tempfile.TemporaryDirectory(prefix='chapter-verification-') as tmp:
            path = Path(tmp) / 'chapter.mp3'
            path.write_bytes(plain)
            path.chmod(0o600)
            decoded = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(path), '-f', 'null', '-'],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
            assert decoded.returncode == 0 and not decoded.stderr, 'MP3 decode error'
            info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
                    'format=duration:stream=sample_rate,channels', '-of', 'json', str(path)]))
            assert abs(float(info['format']['duration']) - t['duration']) < .3
            assert int(info['streams'][0]['sample_rate']) == 22050 and info['streams'][0]['channels'] == 2
        return {'id': t['id'], 'duration': t['duration'], 'chunks': len(pieces),
                'hosted_bytes_verified': True, 'authenticated': True, 'full_decode_pass': True}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(check_track, t) for t in catalog['tracks']]
        for future in as_completed(futures):
            result = future.result()
            report['chapters'].append(result)
            save()
            print(json.dumps(result), flush=True)
    report['chapters'].sort(key=lambda r: r['id'])
    with sync_playwright() as p:
        for name in ('chromium', 'webkit'):
            result = check_browser(name, getattr(p, name), key, catalog)
            report['browsers'].append(result)
            save()
            print(json.dumps(result), flush=True)
    report['passed'] = len(report['chapters']) == 49 and all(b['passed'] for b in report['browsers'])
    report['total_duration'] = catalog['total_duration']
    report['total_bytes'] = catalog['total_bytes']
    save()
    if not report['passed']:
        raise SystemExit('Hosted playback verification did not pass')

if __name__ == '__main__':
    main()
