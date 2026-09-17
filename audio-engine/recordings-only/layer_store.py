"""Durable encrypted checkpoints, bounded retries, and read-after-write checks."""
from pathlib import Path
import base64, hashlib, io, json, os, subprocess, time, urllib.request, zipfile
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'recordings'
WORK = Path('/tmp/private-audio')
BRANCH = 'recordings-only'
TAG = 'taliesin-layer-checkpoints-v1'
CHUNK = 8_000_000


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2))
    tmp.replace(path)


def retry(label, action):
    for attempt in range(1, 4):
        try:
            return action()
        except Exception as exc:
            print(json.dumps({'operation': label, 'attempt': attempt,
                              'result': type(exc).__name__, 'attempt_limit': 3}), flush=True)
            if attempt == 3:
                raise
            time.sleep(3 * attempt)


def cmd(*args, cwd=ROOT, timeout=240):
    # Do not echo arguments or stderr; they can contain private content.
    result = subprocess.run(list(args), cwd=cwd, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{args[0]} operation failed (exit {result.returncode})')
    return result.stdout


def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={
            'User-Agent': 'Taliesin-layered-production'}), timeout=120) as response:
        return response.read()


def checkpoint(message):
    cmd('git', 'config', 'user.name', 'github-actions[bot]')
    cmd('git', 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    cmd('git', 'add', '-f', '--', 'recordings')
    result = subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=ROOT)
    if result.returncode == 1:
        cmd('git', 'commit', '-m', message)
    elif result.returncode:
        raise RuntimeError('Cannot inspect checkpoint changes')

    def send():
        cmd('git', 'fetch', 'origin', BRANCH)
        cmd('git', 'rebase', 'origin/' + BRANCH)
        cmd('git', 'push', 'origin', 'HEAD:' + BRANCH)
        revision = cmd('git', 'rev-parse', 'HEAD').decode().strip()
        cmd('git', 'fetch', 'origin', BRANCH)
        cmd('git', 'merge-base', '--is-ancestor', revision, 'origin/' + BRANCH)
        return revision
    return retry('save_repository_checkpoint', send)


def release_ready():
    repo = os.environ['GITHUB_REPOSITORY']
    result = subprocess.run(['gh', 'release', 'view', TAG, '--repo', repo],
                            cwd=ROOT, capture_output=True, timeout=60)
    if result.returncode:
        def create():
            # An uncertain earlier write may already have created the release.
            test = subprocess.run(['gh', 'release', 'view', TAG, '--repo', repo],
                                  cwd=ROOT, capture_output=True, timeout=60)
            if not test.returncode:
                return
            cmd('gh', 'release', 'create', TAG, '--repo', repo, '--target', BRANCH,
                '--title', 'Encrypted production layers', '--prerelease', '--latest=false',
                '--notes', 'Encrypted voice batches and native mixes. No listening keys or plaintext book content are included.')
        retry('prepare_layer_storage', create)


def store(label, files, key, fingerprint):
    path = OUT / 'layers' / (label + '.json')
    if path.exists():
        proof = read(path)
        if proof['fingerprint'] != fingerprint:
            raise ValueError('Conflicting layer fingerprint: ' + label)
        return proof
    buffer = io.BytesIO()
    checks = []
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for file in files:
            file = Path(file)
            data = file.read_bytes()
            archive.writestr(file.name, data)
            checks.append({'name': file.name, 'bytes': len(data), 'sha256': digest(data)})
    plain = buffer.getvalue()
    aad = ('Taliesin:layer:v1:' + label).encode()
    nonce = os.urandom(12)
    cipher = nonce + AESGCM(key).encrypt(nonce, plain, aad)
    sha = digest(cipher)
    staging = WORK / 'encrypted-staging'
    staging.mkdir(exist_ok=True)
    repo = os.environ['GITHUB_REPOSITORY']
    parts = []
    for number, offset in enumerate(range(0, len(cipher), CHUNK)):
        piece = cipher[offset:offset + CHUNK]
        name = label.replace('/', '-') + '-' + sha[:20] + f'-{number:04}.bin'
        local = staging / name
        local.write_bytes(piece)
        url = f'https://github.com/{repo}/releases/download/{TAG}/{name}'
        def upload():
            # Each retry creates a fresh authenticated CLI connection. Never clobber.
            result = subprocess.run(['gh', 'release', 'upload', TAG, str(local), '--repo', repo],
                                    cwd=ROOT, capture_output=True, timeout=240)
            # Also read after an uncertain/duplicate upload response.
            remote = fetch(url)
            if len(remote) != len(piece) or digest(remote) != digest(piece):
                raise ValueError('Layer readback checksum mismatch')
        retry('upload_and_readback_layer', upload)
        local.unlink()
        parts.append({'name': name, 'url': url, 'bytes': len(piece), 'sha256': digest(piece)})
    proof = {'version': 1, 'label': label, 'fingerprint': fingerprint,
             'key_id': 'private-archive-v1', 'release': TAG, 'bytes': len(cipher),
             'sha256': sha, 'plain_sha256': digest(plain), 'aad': aad.decode(),
             'files': checks, 'chunks': parts, 'remote_readback_pass': True}
    write(path, proof)
    checkpoint('Save verified production layer ' + label)
    return proof


def restore(label, target, key, fingerprint):
    path = OUT / 'layers' / (label + '.json')
    if not path.exists():
        return False
    proof = read(path)
    if proof['fingerprint'] != fingerprint or not proof['remote_readback_pass']:
        raise ValueError('Layer identity does not match requested production')
    parts = []
    for part in proof['chunks']:
        def load():
            raw = fetch(part['url'])
            if len(raw) != part['bytes'] or digest(raw) != part['sha256']:
                raise ValueError('Damaged layer chunk')
            return raw
        parts.append(retry('restore_layer_chunk', load))
    cipher = b''.join(parts)
    if len(cipher) != proof['bytes'] or digest(cipher) != proof['sha256']:
        raise ValueError('Layer reassembly failed')
    plain = AESGCM(key).decrypt(cipher[:12], cipher[12:], proof['aad'].encode())
    if digest(plain) != proof['plain_sha256']:
        raise ValueError('Layer plaintext integrity check failed')
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(plain)) as archive:
        if sorted(archive.namelist()) != sorted(p['name'] for p in proof['files']):
            raise ValueError('Layer file inventory mismatch')
        for item in proof['files']:
            name = item['name']
            if Path(name).name != name:
                raise ValueError('Unsafe layer filename')
            data = archive.read(name)
            if len(data) != item['bytes'] or digest(data) != item['sha256']:
                raise ValueError('Layer file integrity failed')
            (target / name).write_bytes(data)
    return True
