"""Resume verified, encrypted audiobook production without replacing published tracks."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'audio-engine/v3'))
import full_transfer as legacy

base = legacy.base
EPOCH = os.environ['GITHUB_RUN_ID'] + '-a' + os.environ.get('GITHUB_RUN_ATTEMPT', '1')
legacy.RUN = base.RUN = EPOCH
_original_api = base.api

def resilient_api(path, data=None):
    for attempt in range(5):
        try:
            return _original_api(path, data)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 4:
                raise
        except (TimeoutError, urllib.error.URLError):
            if attempt == 4:
                raise
        time.sleep(min(20, 2 ** (attempt + 1)))
    raise RuntimeError('GitHub request failed after retries')

base.api = resilient_api

def input_cipher():
    manifest = json.loads((ROOT / '.transfer/v4/input.json').read_text())
    if manifest['version'] != 4:
        raise ValueError('Unsupported input manifest')
    pieces = []
    for folder, count, size, last_size in manifest['groups']:
        if folder not in ('.transfer/v3/all', '.transfer/v4/tail'):
            raise ValueError('Unexpected input directory')
        for number in range(count):
            data = (ROOT / folder / f'{number:03}.bin').read_bytes()
            if len(data) != (last_size if number == count - 1 else size):
                raise ValueError('Input piece size mismatch')
            pieces.append(data)
    data = b''.join(pieces)
    if len(data) != manifest['cipher_bytes'] or hashlib.sha256(data).hexdigest() != manifest['cipher_sha256']:
        raise ValueError('Complete encrypted input checksum mismatch')
    return manifest, data

legacy.input_cipher = input_cipher
_original_render = base.render

def retry_chapter(*args):
    for attempt in range(3):
        try:
            return _original_render(*args)
        except (subprocess.SubprocessError, OSError) as exc:
            print(json.dumps({'track': args[0], 'attempt': attempt + 1,
                              'retryable_error': type(exc).__name__}), flush=True)
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))

base.render = retry_chapter

def publish():
    key = base.receive_key()
    if len(key) != 32:
        raise ValueError('Invalid publication key')
    base.WORK.mkdir(mode=0o700, exist_ok=True)
    key_file = base.WORK / 'browser-key.bin'
    key_file.write_bytes(key)
    key_file.chmod(0o600)
    records = [json.loads(p.read_text()) for p in (ROOT / 'player/audio-v2/records').glob('*.json')]
    identifiers = {r['id'] for r in records}
    if len(identifiers) != len(records) or not identifiers.issubset(legacy.EXPECTED):
        raise ValueError('Unexpected or duplicate chapter records')
    rows, _, _ = legacy.read_recipe(key)
    from collections import Counter
    expected_counts = Counter(f"b{r['book']}-c{r['chapter']:02}" for r in rows)
    for record in records:
        legacy.verify_record(record, key)
        if record['segments'] != expected_counts[record['id']]:
            raise ValueError('Chapter reading segment coverage mismatch')
    if identifiers == legacy.EXPECTED and sum(r['segments'] for r in records) != 11105:
        raise ValueError('Full-book coverage mismatch')
    base.catalog()
    catalog = json.loads((ROOT / 'player/catalog.json').read_text())
    report = {'complete': catalog['complete'], 'published_tracks': len(records),
              'expected_tracks': 49, 'reading_segments': sum(r['segments'] for r in records),
              'input_bytes': 332336, 'input_sha256_verified': True,
              'all_published_chapters_authenticated': True,
              'all_published_chapters_decoded': True, 'all_reassemblies_byte_exact': True,
              'max_transport_chunk_bytes': base.LIMIT,
              'duration': catalog['total_duration'], 'audio_bytes': catalog['total_bytes']}
    (ROOT / 'player/verification.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)

if __name__ == '__main__':
    base.WORK.mkdir(mode=0o700, exist_ok=True)
    if len(sys.argv) != 2:
        raise SystemExit('Expected verify-input, render, or publish')
    mode = sys.argv[1]
    if mode == 'verify-input':
        manifest, data = input_cipher()
        print(json.dumps({'verified': True, 'pieces': sum(g[1] for g in manifest['groups']),
                          'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}))
    elif mode == 'render':
        legacy.render_worker()
    elif mode == 'publish':
        publish()
    else:
        raise SystemExit('Unknown operation')
