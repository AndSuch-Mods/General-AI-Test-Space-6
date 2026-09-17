"""Resume sequential recording production with durable encrypted master backups.

No plaintext, secret key, or private link is written to GitHub. Original player
recordings remain untouched. A checkpoint is complete only after its encrypted
MP3 is committed and its native master is saved and read back from a release.
"""
from pathlib import Path
import base64
import importlib.util
import json
import os
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('recordings_engine', ROOT / 'audio-engine/recordings-only/render.py')
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)
TAG = 'taliesin-private-native-masters-v1'
MASTER_LIMIT = 8_000_000
_original_push = engine.push_checkpoint
_release_ready = False
_asset_names = set()


def gh(*args):
    last = None
    for attempt in range(4):
        try:
            return subprocess.check_output(['gh', *args], cwd=ROOT, timeout=300, text=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last = exc
            if attempt < 3:
                time.sleep(2 ** (attempt + 1))
    raise RuntimeError('GitHub release operation failed after bounded retries') from last


def release_ready():
    global _release_ready, _asset_names
    if _release_ready:
        return
    repo = os.environ['GITHUB_REPOSITORY']
    result = subprocess.run(['gh', 'api', f'repos/{repo}/releases/tags/{TAG}'], cwd=ROOT, capture_output=True, text=True, timeout=60)
    if result.returncode:
        try:
            status = json.loads(result.stdout).get('status')
        except json.JSONDecodeError:
            status = None
        if str(status) != '404':
            raise RuntimeError('Cannot inspect encrypted-master release; no render started')
        gh('release', 'create', TAG, '--repo', repo, '--target', 'recordings-only', '--title', 'Encrypted native chapter masters', '--notes', 'Lossless chapter masters encrypted for private listening. The decryption key is not included. Each chapter has an ordered checksum manifest on recordings-only.', '--prerelease', '--latest=false')
        meta = json.loads(gh('api', f'repos/{repo}/releases/tags/{TAG}'))
    else:
        meta = json.loads(result.stdout)
    _asset_names = set(gh('api', '--paginate', f'repos/{repo}/releases/{meta["id"]}/assets?per_page=100', '--jq', '.[].name').splitlines())
    _release_ready = True


def preserve_masters():
    release_ready()
    repo = os.environ['GITHUB_REPOSITORY']
    export = Path('/tmp/encrypted-native-masters')
    scratch = engine.WORK / 'encrypted-master-transfer'
    scratch.mkdir(exist_ok=True)
    for path in sorted(export.glob('*.bin')):
        tid = path.stem
        if tid not in engine.ORDER:
            raise ValueError('Unexpected native master name')
        meta = engine.read(path.with_suffix('.json'))
        raw = path.read_bytes()
        if len(raw) != meta['bytes'] or engine.digest(raw) != meta['cipher_sha256']:
            raise ValueError('Native master archive failed its local checksum')
        parts = []
        for number, offset in enumerate(range(0, len(raw), MASTER_LIMIT)):
            data = raw[offset:offset + MASTER_LIMIT]
            name = f'{tid}-{meta["cipher_sha256"][:16]}-{number:04}.bin'
            chunk = scratch / name
            chunk.write_bytes(data)
            if name not in _asset_names:
                gh('release', 'upload', TAG, str(chunk), '--repo', repo)
                _asset_names.add(name)
            chunk.unlink()
            gh('release', 'download', TAG, '--repo', repo, '--pattern', name, '--dir', str(scratch))
            checked = chunk.read_bytes()
            if len(checked) != len(data) or engine.digest(checked) != engine.digest(data):
                raise ValueError('Uploaded native master chunk failed readback')
            chunk.unlink()
            parts.append({'name': name, 'bytes': len(data), 'sha256': engine.digest(data), 'url': f'https://github.com/{repo}/releases/download/{TAG}/{name}'})
        proof = dict(meta, release_tag=TAG, chunks=parts, remote_readback_pass=True, encryption='AES-256-GCM; first 12 reassembled bytes are the nonce', aad='Taliesin:master-archive:v1:' + tid)
        engine.write(engine.OUT / 'masters' / (tid + '.json'), proof)
        path.unlink()
        print(json.dumps({'stage': 'native_master_saved_and_verified', 'track': tid, 'chunks': len(parts), 'bytes': meta['bytes']}), flush=True)


def push_checkpoint(message):
    preserve_masters()
    complete = engine.OUT / 'complete.json'
    if complete.exists():
        proofs = [engine.read(engine.OUT / 'masters' / (tid + '.json')) for tid in engine.ORDER]
        if not all(p.get('remote_readback_pass') is True for p in proofs):
            raise ValueError('One or more native masters have not been preserved')
        record = engine.read(complete)
        record.update(native_masters_saved=49, native_master_release=TAG, all_native_masters_read_back=True)
        engine.write(complete, record)
        engine.write(engine.OUT / 'progress.json', record)
    return _original_push(message)


def make_vault():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = (engine.WORK / 'recording-key.bin').read_bytes()
    path = engine.OUT / 'vault.json'
    if path.exists():
        blob = base64.b64decode(engine.read(path)['proof'], validate=True)
        if AESGCM(key).decrypt(blob[:12], blob[12:], b'Taliesin:v2:unlock') != b'Taliesin private player':
            raise ValueError('Existing archive vault does not match the authenticated key')
        return
    nonce = os.urandom(12)
    proof = nonce + AESGCM(key).encrypt(nonce, b'Taliesin private player', b'Taliesin:v2:unlock')
    engine.write(path, {'version': 2, 'key_id': engine.KEY_ID, 'proof': base64.b64encode(proof).decode()})


engine.push_checkpoint = push_checkpoint
if __name__ == '__main__':
    engine.WORK.mkdir(exist_ok=True, mode=0o700)
    mode = sys.argv[1] if len(sys.argv) == 2 else ''
    if mode == 'plan':
        manifest, data = engine.input_bytes()
        print(json.dumps({'input_verified': True, 'input_bytes': len(data), 'input_chunks': len(manifest['chunks']), 'parallel_chapters': 1, 'player_deployment': False}))
    elif mode == 'key':
        engine.key_step()
    elif mode == 'render':
        release_ready()
        make_vault()
        engine.render_all()
    else:
        raise SystemExit('Expected plan, key or render')
