"""Continue one verified chapter at a time; never replace an existing recording.

Version 6 keeps its own private key, backed up outside the public repository.
The existing version-5 rendering and MP3 verification engine is reused.
"""
from pathlib import Path
import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
KEY_ID = 'sequential-v6'
spec = importlib.util.spec_from_file_location('chapter_v5', ROOT / 'audio-engine/v5/one_chapter.py')
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)
engine.KEY_ID = KEY_ID


def next_track(existing):
    for tid in engine.ORDER:
        if tid not in existing:
            return tid
        record = existing[tid]
        if not all(record.get(k) is True for k in ('verified', 'full_decode_pass', 'reassembly_byte_exact')):
            raise ValueError('Earlier chapter has not passed verification: ' + tid)
        if record.get('key_id'):
            checkpoint = ROOT / 'player/checkpoints' / (tid + '.json')
            if not checkpoint.exists():
                return tid
            proof = engine.read_json(checkpoint)
            if proof.get('cipher_sha256') != record['sha256'] or proof.get('remote_readback_pass') is not True:
                raise ValueError('Earlier checkpoint does not match its recording: ' + tid)
    return None


def request():
    item = engine.read_json(ROOT / '.transfer/v6/request.json')
    tid = item.get('track')
    if item.get('version') != 6 or tid not in engine.ORDER or item.get('max_chapters') != 1:
        raise ValueError('Exactly one chapter must be requested')
    candidate = next_track(engine.records())
    if tid != candidate:
        raise ValueError('Request is not the next unverified chapter: ' + str(candidate))
    manifest = engine.read_json(ROOT / '.transfer/v6/inputs' / (tid + '.json'))
    if manifest.get('track') != tid or manifest.get('source_sha256') != engine.SOURCE or manifest.get('key_id') != KEY_ID:
        raise ValueError('Wrong chapter input identity')
    data = base64.b64decode((ROOT / '.transfer/v6/inputs' / (tid + '.b64')).read_text(), validate=True)
    if not 28 < len(data) < 500000 or len(data) != manifest['cipher_bytes'] or engine.digest(data) != manifest['cipher_sha256']:
        raise ValueError('Encrypted chapter input failed its size or checksum check')
    return tid, manifest, data


engine.request = request
engine.next_track = next_track


def patch_player():
    subprocess.run([sys.executable, str(ROOT / 'audio-engine/v5/patch_player.py')], check=True)
    path = ROOT / 'player/player.js'
    text = path.read_text()
    old = "[['legacy','vault.json'],['sequential-v5','vault-v5.json']]"
    new = "[['legacy','vault.json'],['sequential-v5','vault-v5.json'],['sequential-v6','vault-v6.json']]"
    if new not in text:
        if text.count(old) != 1:
            raise ValueError('Player key registry changed; refusing a blind patch')
        text = text.replace(old, new)
    text = text.replace('if(!t||!state.key||state.next?.id===t.id||state.prefetching===t.id)return;',
                        'if(!t||!keyFor(t)||state.next?.id===t.id||state.prefetching===t.id)return;')
    text = text.replace('keyFor(track())&&track()&&state.current&&!state.loading',
                        'keyFor(track())&&track()&&state.current?.id===track().id&&!state.loading')
    text = text.replace('if(!track()||!state.current||state.loading)return;',
                        'if(!track()||!state.current||state.current.id!==track().id||state.loading)return;')
    path.write_text(text)
    sw = ROOT / 'player/sw.js'
    text = sw.read_text()
    if "'vault-v6.json'" not in text:
        old = "'vault-v5.json','catalog.json'"
        if text.count(old) != 1:
            raise ValueError('Service-worker asset list changed')
        text = text.replace(old, "'vault-v5.json','vault-v6.json','catalog.json'")
    import re
    text = re.sub(r"const SHELL='taliesin-shell-[^']+';", "const SHELL='taliesin-shell-v6.0.0';", text)
    sw.write_text(text)
    print('Earlier keys and audio preserved; recoverable chapter key enabled', flush=True)


def export_verified():
    result = engine.read_json(engine.WORK / 'chapter-result.json')
    tid = result['track']
    record = engine.read_json(ROOT / 'player/audio-v2/records' / (tid + '.json'))
    dest = Path('/tmp/cipher-export')
    folder = Path(record['chunks'][0]['src']).parent
    target = dest / folder
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / 'player' / folder, target, dirs_exist_ok=True)
    records = dest / 'audio-v2/records'
    records.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'player/audio-v2/records' / (tid + '.json'), records / (tid + '.json'))
    shutil.copy2(engine.WORK / 'chapter-result.json', dest / 'chapter-result.json')


if __name__ == '__main__':
    engine.WORK.mkdir(exist_ok=True, mode=0o700)
    mode = sys.argv[1] if len(sys.argv) == 2 else ''
    if mode == 'plan':
        tid, manifest, _ = request()
        print(json.dumps({'next_chapter': tid, 'segments': manifest['segments'], 'max_chapters': 1, 'speech_batch_size': 24}))
    elif mode == 'key':
        if os.environ.get('TALIESIN_V6_KEY_B64'):
            os.environ['TALIESIN_V5_KEY_B64'] = os.environ['TALIESIN_V6_KEY_B64']
        engine.acquire_key()
    elif mode == 'render':
        engine.render()
        export_verified()
    elif mode == 'patch-player':
        patch_player()
    elif mode == 'verify-remote':
        engine.verify_remote()
    else:
        raise SystemExit('Expected plan, key, render, patch-player or verify-remote')
