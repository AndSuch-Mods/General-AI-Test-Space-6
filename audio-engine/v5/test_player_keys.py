"""Browser regression tests with generated tones and disposable keys only."""
from pathlib import Path
import base64
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
REPORT = Path('/tmp/player-key-tests.json')


def main():
    reports = []
    with tempfile.TemporaryDirectory() as temp:
        web = Path(temp)
        for source in (ROOT / 'player').iterdir():
            if source.is_file():
                shutil.copy2(source, web / source.name)
        legacy, current = os.urandom(32), os.urandom(32)
        for name, key in [('vault.json', legacy), ('vault-v5.json', current)]:
            nonce = os.urandom(12)
            proof = nonce + AESGCM(key).encrypt(nonce, b'Taliesin private player', b'Taliesin:v2:unlock')
            (web / name).write_text(json.dumps({'version': 2, 'proof': base64.b64encode(proof).decode()}))
        audio = web / 'tone.mp3'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=65', '-c:a', 'libmp3lame', '-ar', '22050', '-ac', '2', str(audio)], check=True)
        plain = audio.read_bytes()
        tracks = []
        for tid, key, kid, order in [('b1-c01', legacy, None, 1), ('b1-c02', current, 'sequential-v5', 2)]:
            nonce = os.urandom(12)
            cipher = nonce + AESGCM(key).encrypt(nonce, plain, ('Taliesin:v2:' + tid).encode())
            folder = web / 'audio-v2' / tid
            folder.mkdir(parents=True)
            parts = []
            for number, offset in enumerate(range(0, len(cipher), 64128)):
                raw = cipher[offset:offset + 64128]
                filename = f'{number:04}.bin'
                (folder / filename).write_bytes(raw)
                parts.append({'src': f'audio-v2/{tid}/{filename}', 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
            record = {'id': tid, 'order': order, 'book': 1, 'chapter': order, 'part': 'A Gift of Jade', 'title': 'Chapter ' + str(order), 'duration': 65, 'bytes': len(cipher), 'sha256': hashlib.sha256(cipher).hexdigest(), 'src': f'audio-v2/{tid}/joined.bin', 'chunks': parts}
            if kid:
                record['key_id'] = kid
            tracks.append(record)
        (web / 'catalog.json').write_text(json.dumps({'version': 2, 'ready': True, 'complete': False, 'expected_tracks': 49, 'published_tracks': 2, 'tracks': tracks, 'total_duration': 130, 'total_bytes': sum(t['bytes'] for t in tracks)}))

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(web), **kwargs)
            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}/'
        code = lambda key: base64.urlsafe_b64encode(key).decode().rstrip('=')
        try:
            with sync_playwright() as p:
                for name in ['chromium', 'webkit']:
                    browser = getattr(p, name).launch(headless=True)
                    context = browser.new_context(viewport={'width': 390, 'height': 844}, has_touch=True)
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    result = {'browser': name, 'passed': False}
                    stage = 'new_key_unlock'
                    try:
                        page.goto(url + '#k=' + code(current) + '&t=b1-c02')
                        expect(page.locator('#play')).to_be_enabled(timeout=30000)
                        expect(page.locator('#chapterTitle')).to_have_text('Chapter 2')
                        expect(page).to_have_url(url)
                        result[stage] = True
                        stage = 'play_pause_and_normal_speed'
                        page.locator('#play').click()
                        expect(page.locator('#audio')).to_have_js_property('paused', False)
                        expect(page.locator('#audio')).to_have_js_property('playbackRate', 1)
                        page.locator('#play').click()
                        expect(page.locator('#audio')).to_have_js_property('paused', True)
                        result[stage] = True
                        stage = 'tap_and_hold_seek'
                        page.locator('#audio').evaluate('(a)=>a.currentTime=30')
                        page.locator('#rewind').click()
                        assert 19 <= page.locator('#audio').evaluate('(a)=>a.currentTime') <= 21
                        page.locator('#forward').scroll_into_view_if_needed()
                        button = page.locator('#forward').bounding_box()
                        page.mouse.move(button['x'] + button['width']/2, button['y'] + button['height']/2)
                        page.mouse.down()
                        page.wait_for_timeout(800)
                        page.mouse.up()
                        assert page.locator('#audio').evaluate('(a)=>a.currentTime') >= 33
                        result[stage] = True
                        stage = 'legacy_key_retained'
                        page.locator('#chaptersButton').click()
                        page.locator('.chapter-row').nth(0).click()
                        expect(page.locator('#unlockPanel')).to_be_visible(timeout=30000)
                        page.locator('#unlockCode').fill(code(legacy))
                        page.locator('#unlockForm button[type=submit]').click()
                        expect(page.locator('#play')).to_be_enabled(timeout=30000)
                        expect(page.locator('#chapterTitle')).to_have_text('Chapter 1')
                        result[stage] = True
                        stage = 'chapter_selection'
                        page.locator('#chaptersButton').click()
                        page.locator('.chapter-row').nth(1).click()
                        expect(page.locator('#chapterTitle')).to_have_text('Chapter 2')
                        expect(page.locator('#play')).to_be_enabled(timeout=30000)
                        result[stage] = True
                        stage = 'offline_save'
                        page.locator('#saveCurrent').click()
                        expect(page.locator('#downloadStatus')).to_contain_text('This chapter is saved', timeout=30000)
                        deadline = time.monotonic() + 30
                        while not page.evaluate('()=>Boolean(navigator.serviceWorker.controller)'):
                            if time.monotonic() >= deadline:
                                raise AssertionError('Service worker did not take control')
                            page.wait_for_timeout(100)
                        result[stage] = True
                        stage = 'offline_reload'
                        context.set_offline(True)
                        page.reload()
                        expect(page.locator('#play')).to_be_enabled(timeout=30000)
                        expect(page.locator('#chapterTitle')).to_have_text('Chapter 2')
                        page.locator('#play').click()
                        expect(page.locator('#audio')).to_have_js_property('paused', False)
                        result[stage] = True
                        assert not errors, errors
                        result['passed'] = True
                    except Exception as exc:
                        result['failed_stage'] = stage
                        result['error'] = str(exc)
                        result['page_errors'] = errors
                        try:
                            result['status_text'] = page.locator('#status').inner_text()
                        except Exception:
                            pass
                    finally:
                        reports.append(result)
                        print(json.dumps(result), flush=True)
                        REPORT.write_text(json.dumps({'synthetic_test_audio': True, 'production_keys_used': False, 'results': reports}, indent=2))
                        context.close()
                        browser.close()
        finally:
            server.shutdown()
    if len(reports) != 2 or not all(result['passed'] for result in reports):
        raise SystemExit('Browser regression test failed; see the saved verification report')


if __name__ == '__main__':
    main()
