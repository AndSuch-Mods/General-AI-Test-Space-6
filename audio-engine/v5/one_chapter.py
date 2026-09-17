"""One chapter per job: authenticated input, bounded speech batches, verified output.

Private keys and plaintext exist only in /tmp/private-audio. Only encrypted audio,
public metadata and an explicit completed checkpoint are committed to the site.
"""
from pathlib import Path
import base64
import hashlib
import importlib.util
import json
import lzma
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

SOURCE = '707254842a0c590201adb29f0f07cc39501f001c031a58f3d850728a4265bcc5'
KEY_ID = 'sequential-v5'
ROOT = Path(__file__).resolve().parents[2]
WORK = Path('/tmp/private-audio')
LIMIT = 4_000_000
ORDER = ['b0-c00'] + [f'b1-c{i:02}' for i in range(1, 17)] + [f'b2-c{i:02}' for i in range(1, 15)] + [f'b3-c{i:02}' for i in range(1, 19)]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
    path.chmod(0o600)


def records():
    result = {}
    for path in (ROOT / 'player/audio-v2/records').glob('*.json'):
        record = read_json(path)
        tid = record['id']
        if tid not in ORDER or tid in result or path.stem != tid:
            raise ValueError('Unexpected or duplicate chapter record')
        result[tid] = record
    return result


def next_track(existing):
    for tid in ORDER:
        if tid not in existing:
            return tid
        record = existing[tid]
        if not (record.get('verified') and record.get('full_decode_pass') and record.get('reassembly_byte_exact')):
            raise ValueError('An earlier chapter has not passed verification: ' + tid)
        if record.get('key_id') == KEY_ID:
            checkpoint = ROOT / 'player/checkpoints' / (tid + '.json')
            if not checkpoint.exists():
                return tid
            proof = read_json(checkpoint)
            if proof.get('cipher_sha256') != record['sha256'] or proof.get('remote_readback_pass') is not True:
                raise ValueError('Previous chapter checkpoint does not match: ' + tid)
    return None


def request():
    item = read_json(ROOT / '.transfer/v5/request.json')
    tid = item.get('track')
    if item.get('version') != 5 or tid not in ORDER or item.get('max_chapters') != 1:
        raise ValueError('A request must name exactly one chapter')
    candidate = next_track(records())
    if tid != candidate:
        raise ValueError('Request is not the next unverified chapter; expected ' + str(candidate))
    manifest = read_json(ROOT / '.transfer/v5/inputs' / (tid + '.json'))
    if manifest.get('track') != tid or manifest.get('source_sha256') != SOURCE or manifest.get('key_id') != KEY_ID:
        raise ValueError('Wrong input manifest identity')
    ciphertext = base64.b64decode((ROOT / '.transfer/v5/inputs' / (tid + '.b64')).read_text(), validate=True)
    if not 28 < len(ciphertext) < 500_000 or len(ciphertext) != manifest['cipher_bytes'] or digest(ciphertext) != manifest['cipher_sha256']:
        raise ValueError('Encrypted chapter input is incomplete or damaged')
    return tid, manifest, ciphertext


def recipe(key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    tid, manifest, ciphertext = request()
    compressed = AESGCM(key).decrypt(ciphertext[:12], ciphertext[12:], ('Taliesin:v5:recipe:' + tid).encode())
    raw = lzma.decompress(compressed, memlimit=128 * 1024 * 1024)
    if len(raw) > 2_000_000 or digest(raw) != manifest['plain_sha256']:
        raise ValueError('Decrypted chapter input does not match')
    item = json.loads(raw)
    if item['source_sha256'] != SOURCE or item['track'] != tid or item.get('coverage_pass') is not True:
        raise ValueError('Source edition or reading coverage is unverified')
    rows = [dict(zip(item['fields'], values, strict=True)) for values in item['segments']]
    if len(rows) != manifest['segments'] or len({r['id'] for r in rows}) != len(rows):
        raise ValueError('Missing or duplicate reading segments')
    for row in rows:
        if f"b{row['book']}-c{row['chapter']:02}" != tid or not row['tts_text'].strip():
            raise ValueError('Invalid chapter speech segment')
        row['text'] = row['tts_text']
        row['audio_key'] = digest((str(row['speaker_id']) + '|' + row['tts_text']).encode())[:24]
    cues = {v[0]: {'paragraph_id': v[0], 'ambience': v[1], 'events': [dict(zip(('sound', 'fraction', 'gain'), e, strict=True)) for e in v[2]]} for v in item['cues']}
    return tid, manifest, rows, cues


def api(path, data=None):
    url = 'https://api.github.com/repos/' + os.environ['GITHUB_REPOSITORY'] + path
    for attempt in range(4):
        req = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(), method='GET' if data is None else 'PUT', headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json', 'User-Agent': 'taliesin-single-chapter-v5'})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise
        except (TimeoutError, urllib.error.URLError):
            if attempt == 3:
                raise
        time.sleep(2 ** attempt)
    raise RuntimeError('GitHub request failed after bounded retries')


def remote(path):
    try:
        return base64.b64decode(api('/contents/' + path + '?ref=main')['content'])
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def acquire_key():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    tid, _, _ = request()
    secret_value = os.environ.get('TALIESIN_V5_KEY_B64', '').strip()
    if secret_value:
        key = base64.b64decode(secret_value, validate=True)
    else:
        epoch = os.environ['GITHUB_RUN_ID'] + '-a' + os.environ.get('GITHUB_RUN_ATTEMPT', '1')
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        path = '.transfer/v5/public-' + epoch + '.json'
        record = {'run': epoch, 'track': tid, 'key_id': KEY_ID, 'public_key': public}
        api('/contents/' + path, {'branch': 'main', 'message': 'Register public key for one audiobook chapter', 'content': base64.b64encode(json.dumps(record).encode()).decode()})
        print(json.dumps({'stage': 'awaiting_sealed_key', 'run': epoch, 'track': tid, 'public_key_path': path}), flush=True)
        deadline = time.monotonic() + 300
        key = None
        while time.monotonic() < deadline:
            raw = remote('.transfer/v5/sealed-' + epoch + '.json')
            if raw:
                envelope = json.loads(raw)
                if envelope['run'] != epoch or envelope['track'] != tid or envelope['key_id'] != KEY_ID:
                    raise ValueError('Sealed key is for another chapter or job')
                key = private.decrypt(base64.b64decode(envelope['sealed_key'], validate=True), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
                break
            time.sleep(4)
        if key is None:
            raise TimeoutError('No sealed key arrived. Stopped before model installation or speech generation.')
    if len(key) != 32:
        raise ValueError('Invalid private key length')
    recipe(key)
    private_write(WORK / 'v5-key.bin', key)
    print(json.dumps({'stage': 'input_authenticated', 'track': tid}), flush=True)


def base_engine():
    spec = importlib.util.spec_from_file_location('chapter_transfer_v5_base', ROOT / 'audio-engine/v3/chapter_transfer.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_record(record, key=None):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    chunks = []
    for part in record['chunks']:
        path = (ROOT / 'player' / part['src']).resolve()
        if not path.is_relative_to((ROOT / 'player/audio-v2').resolve()):
            raise ValueError('Unsafe chapter path')
        raw = path.read_bytes()
        if not 0 < len(raw) <= LIMIT or len(raw) != part['bytes'] or digest(raw) != part['sha256']:
            raise ValueError('Damaged audio transport piece')
        chunks.append(raw)
    ciphertext = b''.join(chunks)
    if len(ciphertext) != record['bytes'] or digest(ciphertext) != record['sha256']:
        raise ValueError('Chapter reassembly check failed')
    if key is not None:
        plain = AESGCM(key).decrypt(ciphertext[:12], ciphertext[12:], ('Taliesin:v2:' + record['id']).encode())
        if digest(plain) != record['mp3_sha256']:
            raise ValueError('Decrypted MP3 checksum failed')
        path = WORK / 'verification.mp3'
        private_write(path, plain)
        try:
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(path), '-f', 'null', '-'], check=True, timeout=180)
            probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=codec_name,sample_rate,channels', '-of', 'json', str(path)], timeout=30))
            stream = probe['streams'][0]
            if stream['codec_name'] != 'mp3' or int(stream['sample_rate']) != 22050 or stream['channels'] != 2 or abs(float(probe['format']['duration']) - record['duration']) > .25:
                raise ValueError('MP3 format or duration changed')
        finally:
            path.unlink(missing_ok=True)
    return ciphertext


def render():
    import numpy as np
    import soundfile as sf
    key = (WORK / 'v5-key.bin').read_bytes()
    tid, manifest, rows, cues = recipe(key)
    existing = records()
    for record in existing.values():
        verify_record(record, key if record.get('key_id') == KEY_ID else None)
    base = base_engine()
    if tid not in existing:
        runtime = base.engine()
        for offset in range(0, len(rows), 24):
            part = rows[offset:offset + 24]
            batch = WORK / 'speech-batch.json'
            private_write(batch, json.dumps({'segments': part}, ensure_ascii=False).encode())
            subprocess.run([sys.executable, str(runtime / 'render_one_thread.py'), str(batch)], check=True, timeout=240)
            for row in part:
                samples, rate = sf.read(WORK / 'speech' / (row['audio_key'] + '.wav'))
                if rate != 22050 or samples.ndim != 1 or len(samples) < 1000 or not np.isfinite(samples).all() or float(np.max(np.abs(samples))) < .0001:
                    raise ValueError('Invalid speech output; chapter stopped')
            batch.unlink()
            print(json.dumps({'stage': 'speech_batch_verified', 'track': tid, 'done': min(offset + 24, len(rows)), 'total': len(rows)}), flush=True)
        vault = ROOT / 'player/vault.json'
        old_vault = vault.read_bytes()
        try:
            base.render(tid, rows, cues, key, runtime)
        finally:
            vault.write_bytes(old_vault)
        path = ROOT / 'player/audio-v2/records' / (tid + '.json')
        record = read_json(path)
        record.update(key_id=KEY_ID, source_sha256=SOURCE, recipe_sha256=manifest['plain_sha256'], speech_segments_verified=len(rows))
        path.write_text(json.dumps(record, indent=2))
    else:
        record = existing[tid]
        if record.get('key_id') != KEY_ID or record.get('recipe_sha256') != manifest['plain_sha256']:
            raise ValueError('Existing chapter belongs to another production; refusing to replace it')
        print('Reusing completed chapter; verifying publication only', flush=True)
    if record['segments'] != len(rows):
        raise ValueError('Chapter reading segment coverage is incomplete')
    verify_record(record, key)
    base.catalog()
    summary = {'stage': 'chapter_verified_locally', 'track': tid, 'segments': len(rows), 'duration': record['duration'], 'chunks': len(record['chunks']), 'full_decode_pass': True, 'reassembly_byte_exact': True}
    private_write(WORK / 'chapter-result.json', json.dumps(summary, indent=2).encode())
    output = os.environ.get('GITHUB_OUTPUT')
    if output:
        with open(output, 'a') as stream:
            stream.write('track=' + tid + '\n')
            stream.write('audio_dir=player/' + str(Path(record['chunks'][0]['src']).parent) + '\n')
            stream.write('record_file=player/audio-v2/records/' + tid + '.json\n')
    print(json.dumps(summary), flush=True)


def verify_remote():
    tid = read_json(WORK / 'chapter-result.json')['track']
    record = read_json(ROOT / 'player/audio-v2/records' / (tid + '.json'))
    owner, name = os.environ['GITHUB_REPOSITORY'].split('/')
    root_url = f'https://{owner.lower()}.github.io/{name}/player/'
    deadline = time.monotonic() + 240
    last_error = None
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(root_url + 'catalog.json?verify=' + str(time.time_ns()), headers={'Cache-Control': 'no-cache'})
            with urllib.request.urlopen(req, timeout=20) as response:
                catalog = json.load(response)
            deployed = next((t for t in catalog['tracks'] if t['id'] == tid), None)
            if deployed != record:
                raise ValueError('Chapter catalog has not reached the live player')
            chunks = []
            for part in record['chunks']:
                with urllib.request.urlopen(root_url + part['src'], timeout=30) as response:
                    raw = response.read(part['bytes'] + 1)
                if len(raw) != part['bytes'] or digest(raw) != part['sha256']:
                    raise ValueError('Live audio chunk failed its checksum')
                chunks.append(raw)
            if digest(b''.join(chunks)) != record['sha256']:
                raise ValueError('Live chapter reassembly failed')
            break
        except (ValueError, urllib.error.URLError, TimeoutError) as exc:
            last_error = type(exc).__name__
            time.sleep(8)
    else:
        raise RuntimeError('Live publication verification failed: ' + str(last_error))
    verify_record(record, (WORK / 'v5-key.bin').read_bytes())
    proof = {'version': 5, 'track': tid, 'key_id': KEY_ID, 'cipher_sha256': record['sha256'], 'mp3_sha256': record['mp3_sha256'], 'segments': record['segments'], 'duration': record['duration'], 'full_decode_pass': True, 'remote_readback_pass': True, 'reassembly_byte_exact': True, 'run_id': os.environ['GITHUB_RUN_ID']}
    directory = ROOT / 'player/checkpoints'
    directory.mkdir(exist_ok=True)
    (directory / (tid + '.json')).write_text(json.dumps(proof, indent=2))
    print(json.dumps(proof), flush=True)


if __name__ == '__main__':
    WORK.mkdir(exist_ok=True, mode=0o700)
    mode = sys.argv[1] if len(sys.argv) == 2 else ''
    if mode == 'plan':
        tid, manifest, _ = request()
        print(json.dumps({'next_chapter': tid, 'segments': manifest['segments'], 'max_chapters': 1, 'speech_batch_size': 24}))
    elif mode == 'key':
        acquire_key()
    elif mode == 'render':
        render()
    elif mode == 'verify-remote':
        verify_remote()
    else:
        raise SystemExit('Expected plan, key, render or verify-remote')
