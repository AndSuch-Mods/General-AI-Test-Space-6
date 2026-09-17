"""Browser regression tests using generated tones and disposable keys, never book audio."""
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
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]

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
            digest = hashlib.sha256(cipher).hexdigest()
            folder = web / 'audio-v2' / tid
            folder.mkdir(parents=True)
            (folder / '0000.bin').write_bytes(cipher)
            part = {'src': f'audio-v2/{tid}/0000.bin', 'bytes': len(cipher), 'sha256': digest}
            record = {'id': tid, 'order': order, 'book': 1, 'chapter': order, 'part': 'A Gift of Jade', 'title': 'Chapter ' + str(order), 'duration': 65, 'bytes': len(cipher), 'sha256': digest, 'src': f'audio-v2/{tid}/joined.bin', 'chunks': [part]}
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
        code = lambda k: base64.urlsafe_b64encode(k).decode().rstrip('=')
        try:
            with sync_playwright() as p:
                for name in ['chromium', 'webkit']:
                    browser = getattr(p, name).launch(headless=True)
                    context = browser.new_context(viewport={'width': 390, 'height': 844}, has_touch=True)
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    page.goto(url + '#k=' + code(current) + '&t=b1-c02')
                    page.wait_for_function("!document.querySelector('#play').disabled", timeout=30000)
                    assert page.locator('#chapterTitle').inner_text() == 'Chapter 2'
                    assert page.evaluate('location.hash') == ''
                    page.locator('#play').click()
                    page.wait_for_function("!document.querySelector('#audio').paused")
                    assert page.locator('#audio').evaluate('(a)=>a.playbackRate') == 1
                    page.locator('#play').click()
                    page.wait_for_function("document.querySelector('#audio').paused")
                    page.locator('#audio').evaluate('(a)=>a.currentTime=30')
                    page.locator('#rewind').click()
                    assert 19 <= page.locator('#audio').evaluate('(a)=>a.currentTime') <= 21
                    button = page.locator('#forward').bounding_box()
                    page.mouse.move(button['x'] + button['width']/2, button['y'] + button['height']/2)
                    page.mouse.down()
                    page.wait_for_timeout(800)
                    page.mouse.up()
                    assert page.locator('#audio').evaluate('(a)=>a.currentTime') >= 33
                    page.locator('#chaptersButton').click()
                    page.locator('.chapter-row').nth(0).click()
                    page.wait_for_function("!document.querySelector('#unlockPanel').hidden")
                    page.locator('#unlockCode').fill(code(legacy))
                    page.locator('#unlockForm').evaluate('(f)=>f.requestSubmit()')
                    page.wait_for_function("!document.querySelector('#play').disabled", timeout=30000)
                    assert page.locator('#chapterTitle').inner_text() == 'Chapter 1'
                    page.locator('#chaptersButton').click()
                    page.locator('.chapter-row').nth(1).click()
                    page.wait_for_function("document.querySelector('#chapterTitle').textContent==='Chapter 2' && !document.querySelector('#play').disabled", timeout=30000)
                    page.locator('#play').click()
                    page.locator('#menuButton').click()
                    page.locator('#saveCurrent').click()
                    page.wait_for_function("document.querySelector('#downloadStatus').textContent.includes('This chapter is saved')", timeout=30000)
                    page.locator('#menuDialog .close').click()
                    page.wait_for_function('!!navigator.serviceWorker.controller', timeout=30000)
                    context.set_offline(True)
                    page.reload()
                    page.wait_for_function("!document.querySelector('#play').disabled", timeout=30000)
                    assert not errors, errors
                    reports.append({'browser': name, 'new_key_unlock': True, 'legacy_key_retained': True, 'chapter_selection': True, 'play_pause': True, 'tap_and_hold_seek': True, 'normal_speed': True, 'offline_reload': True, 'page_errors': errors})
                    context.close()
                    browser.close()
        finally:
            server.shutdown()
    result = {'synthetic_test_audio': True, 'production_keys_used': False, 'results': reports}
    Path('/tmp/player-key-tests.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))

if __name__ == '__main__':
    main()
