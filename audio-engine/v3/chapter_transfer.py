"""Receive an authenticated recipe and publish independent, byte-exact audio chunks.

Only ciphertext, public keys, and non-sensitive manifests leave the worker.
The owner provides the transfer key sealed to this run's ephemeral public key.
"""
from pathlib import Path
import base64, hashlib, io, json, lzma, os, subprocess, sys, time, urllib.request, urllib.error, zipfile
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[2]
REPO = os.environ['GITHUB_REPOSITORY']
RUN = os.environ['GITHUB_RUN_ID']
BASE = 'https://api.github.com/repos/' + REPO
WORK = Path('/tmp/private-audio')
LIMIT = 4_000_000

def digest(data):
    return hashlib.sha256(data).hexdigest()

def api(path, data=None):
    request = urllib.request.Request(BASE + path,
        data=None if data is None else json.dumps(data).encode(),
        method='GET' if data is None else 'PUT',
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                 'User-Agent': 'private-chapter-transfer-v3'})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)

def read_remote(path):
    try:
        return base64.b64decode(api('/contents/' + path + '?ref=main')['content'])
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise

def receive_key():
    secret = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = secret.public_key().public_bytes(serialization.Encoding.PEM,
                                             serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    name = '.transfer/v3/public-key-' + RUN + '.json'
    public_record = json.dumps({'run': RUN, 'public_key': public}).encode()
    api('/contents/' + name, {'message': 'Register ephemeral public key for chapter transfer',
                             'branch': 'main', 'content': base64.b64encode(public_record).decode()})
    print('Public transfer key registered for run ' + RUN, flush=True)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        data = read_remote('.transfer/v3/sealed-key-' + RUN + '.json')
        if data:
            envelope = json.loads(data)
            if str(envelope['run']) != RUN:
                raise ValueError('Key envelope belongs to another run')
            return secret.decrypt(base64.b64decode(envelope['sealed_key']),
                padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        time.sleep(8)
    raise TimeoutError('No sealed key arrived; no private material was published')

def receive_recipe(track_id, transfer_key):
    manifest = json.loads((ROOT / '.transfer/v3' / (track_id + '.manifest.json')).read_text())
    if manifest['track'] != track_id:
        raise ValueError('Wrong chapter manifest')
    chunks = []
    for entry in manifest['parts']:
        data = base64.b64decode(api('/git/blobs/' + entry['blob'])['content'])
        if len(data) != entry['bytes'] or digest(data) != entry['sha256']:
            raise ValueError('Input chunk checksum mismatch: ' + entry['blob'])
        chunks.append(data)
    ciphertext = b''.join(chunks)
    if len(ciphertext) != manifest['cipher_bytes'] or digest(ciphertext) != manifest['cipher_sha256']:
        raise ValueError('Reassembled input checksum mismatch')
    raw = lzma.decompress(AESGCM(transfer_key).decrypt(ciphertext[:12], ciphertext[12:],
                                                   ('Taliesin:v3:recipe:' + track_id).encode()))
    if digest(raw) != manifest['plain_sha256']:
        raise ValueError('Recipe checksum mismatch')
    recipe = json.loads(raw)
    if recipe['source_sha256'] != '707254842a0c590201adb29f0f07cc39501f001c031a58f3d850728a4265bcc5':
        raise ValueError('Different source edition')
    rows = []
    for values in recipe['segments']:
        row = dict(zip(recipe['fields'], values))
        row['text'] = row['tts_text']
        row['audio_key'] = digest((str(row['speaker_id']) + '|' + row['tts_text']).encode())[:24]
        rows.append(row)
    if len(rows) != manifest['segments'] or not rows:
        raise ValueError('Incomplete chapter recipe')
    cues = {v[0]: {'paragraph_id': v[0], 'ambience': v[1],
                  'events': [{'sound': e[0], 'fraction': e[1], 'gain': e[2]} for e in v[2]]}
            for v in recipe['cues']}
    return rows, cues, base64.b64decode(recipe['player_key'])

def engine():
    parent = ROOT / 'audio-engine/v2'
    bundle = base64.b64decode((parent / 'engine.b64').read_text())
    if digest(bundle) != (parent / 'engine.sha256').read_text().strip():
        raise ValueError('Speech engine checksum mismatch')
    runtime = WORK / 'runtime'
    runtime.mkdir(exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        for name in archive.namelist():
            if Path(name).name != name or not name.endswith('.py'):
                raise ValueError('Unsafe engine archive')
        archive.extractall(runtime)
    sys.path.insert(0, str(runtime))
    return runtime

def render(track_id, rows, cues, key, runtime):
    from mix_audio import grouped_segments, mix_chapter, load_bank, PART_NAMES
    from sound_design import sound_bank
    groups = grouped_segments(rows)
    if len(groups) != 1:
        raise ValueError('Each recipe must contain exactly one chapter')
    pair, rows = groups[0]
    if track_id != f'b{pair[0]}-c{pair[1]:02}':
        raise ValueError('Chapter identity mismatch')
    shard = WORK / 'shard.json'
    shard.write_text(json.dumps({'segments': rows}, ensure_ascii=False))
    shard.chmod(0o600)
    subprocess.run([sys.executable, str(runtime / 'render_one_thread.py'), str(shard)], check=True)
    sound_bank(WORK / 'foley')
    bank = load_bank(WORK / 'foley')
    mixed = WORK / 'mix'
    mixed.mkdir(exist_ok=True)
    report = mix_chapter(pair, rows, WORK / 'speech', mixed, bank, cues)
    mp3 = mixed / 'chapter.mp3'
    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-i', str(mixed / report['path']),
                    '-map_metadata', '-1', '-c:a', 'libmp3lame', '-b:a', '96k',
                    '-ar', '22050', '-ac', '2', str(mp3)], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(mp3), '-f', 'null', '-'], check=True)
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
                         'format=duration:stream=codec_name,sample_rate,channels,bit_rate', '-of', 'json', str(mp3)]))
    duration = float(probe['format']['duration'])
    if abs(duration - report['duration']) > .25:
        raise ValueError('Encoded chapter duration mismatch')
    plain = mp3.read_bytes()
    nonce = os.urandom(12)
    aad = ('Taliesin:v2:' + track_id).encode()
    cipher = nonce + AESGCM(key).encrypt(nonce, plain, aad)
    cipher_hash = digest(cipher)
    folder = ROOT / 'player/audio-v2' / (track_id + '-' + cipher_hash[:16])
    folder.mkdir(parents=True, exist_ok=True)
    pieces = []
    for number, offset in enumerate(range(0, len(cipher), LIMIT)):
        part = cipher[offset:offset + LIMIT]
        target = folder / f'{number:04}.bin'
        target.write_bytes(part)
        pieces.append({'src': str(target.relative_to(ROOT / 'player')), 'bytes': len(part),
                       'sha256': digest(part)})
    joined = b''.join((ROOT / 'player' / p['src']).read_bytes() for p in pieces)
    if digest(joined) != cipher_hash or AESGCM(key).decrypt(joined[:12], joined[12:], aad) != plain:
        raise ValueError('Reassembly or decryption did not preserve the original MP3')
    order = 0 if pair[0] == 0 else pair[1] + {1: 0, 2: 16, 3: 30}[pair[0]]
    record = {'id': track_id, 'order': order, 'book': pair[0], 'chapter': pair[1],
              'part': PART_NAMES.get(pair[0], 'Opening'),
              'title': 'Title and opening verse' if not pair[0] else f'Chapter {pair[1]}',
              'duration': duration, 'src': str((folder / 'joined.bin').relative_to(ROOT / 'player')),
              'bytes': len(cipher), 'sha256': cipher_hash, 'chunks': pieces, 'segments': len(rows),
              'sample_rate': 22050, 'bit_rate': 96000, 'channels': 2, 'verified': True,
              'mp3_sha256': digest(plain), 'full_decode_pass': True,
              'reassembly_byte_exact': True, 'sound_events': len(report['events']),
              'ambience_intervals': len(report['ambience'])}
    records = ROOT / 'player/audio-v2/records'
    records.mkdir(exist_ok=True)
    (records / (track_id + '.json')).write_text(json.dumps(record, indent=2))
    proof_nonce = os.urandom(12)
    proof = proof_nonce + AESGCM(key).encrypt(proof_nonce, b'Taliesin private player', b'Taliesin:v2:unlock')
    (ROOT / 'player/vault.json').write_text(json.dumps({'version': 2, 'proof': base64.b64encode(proof).decode()}))
    print(json.dumps({'track': track_id, 'segments': len(rows), 'duration': duration,
                      'audio_bytes': len(plain), 'chunks': len(pieces), 'largest_chunk': max(p['bytes'] for p in pieces),
                      'reassembly_byte_exact': True, 'full_decode_pass': True}), flush=True)
    mp3.unlink()
    (mixed / report['path']).unlink()
    shard.unlink()

def catalog():
    tracks = sorted((json.loads(p.read_text()) for p in (ROOT / 'player/audio-v2/records').glob('*.json')),
                    key=lambda t: t['order'])
    expected = {'b0-c00'} | {f'b1-c{i:02}' for i in range(1, 17)} | {f'b2-c{i:02}' for i in range(1, 15)} | {f'b3-c{i:02}' for i in range(1, 19)}
    complete = len(tracks) == 49 and {t['id'] for t in tracks} == expected
    if sum(t['bytes'] for t in tracks) > 950_000_000:
        raise ValueError('Published site would exceed the audio budget')
    data = {'version': 2, 'title': 'Taliesin', 'author': 'Stephen Lawhead',
            'ready': bool(tracks), 'complete': complete, 'expected_tracks': 49,
            'published_tracks': len(tracks), 'total_duration': sum(t['duration'] for t in tracks),
            'total_bytes': sum(t['bytes'] for t in tracks),
            'encoding_note': '96 kbps stereo MP3 encoded from native 22,050 Hz chapter masters. Encrypted transport pieces are reassembled byte-for-byte before playback.',
            'tracks': tracks}
    (ROOT / 'player/catalog.json').write_text(json.dumps(data, indent=2))
    (ROOT / 'player/verification.json').write_text(json.dumps({'complete': complete, 'published_tracks': len(tracks),
        'expected_tracks': 49, 'max_transport_chunk_bytes': LIMIT, 'all_published_chapters_decoded': True,
        'all_reassemblies_byte_exact': True}, indent=2))

if __name__ == '__main__':
    WORK.mkdir(exist_ok=True, mode=0o700)
    queue = json.loads((ROOT / '.transfer/v3/queue.json').read_text())['tracks']
    if not queue or any(not isinstance(t, str) or not __import__('re').fullmatch(r'b[0-3]-c\d{2}', t) for t in queue):
        raise ValueError('Invalid chapter queue')
    transfer_key = receive_key()
    runtime = engine()
    for tid in queue:
        rows, cues, key = receive_recipe(tid, transfer_key)
        render(tid, rows, cues, key, runtime)
    catalog()
