"""Publish a complete verified collection without replacing earlier recordings."""
from pathlib import Path
import base64, json, os, re, shutil, subprocess, time
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import layer_store as store

ROOT, OUT = store.ROOT, store.OUT
ORDER = ['b0-c00'] + [f'b1-c{i:02}' for i in range(1,17)] + [f'b2-c{i:02}' for i in range(1,15)] + [f'b3-c{i:02}' for i in range(1,19)]


def prepare(player, records):
    existing = store.read(player / 'catalog.json')
    old = {r['id']: r for r in existing['tracks']}
    if len(records) != 49 or [r['id'] for r in records] != ORDER:
        raise ValueError('All 49 ordered tracks are required before publication')
    if sum(r['segments'] for r in records) != 11105:
        raise ValueError('Reading segment coverage is incomplete')
    tracks = []
    for record in records:
        tid = record['id']
        if not all(record.get(k) is True for k in ('verified','full_decode_pass','reassembly_byte_exact')):
            raise ValueError('A chapter lacks audio verification')
        if record.get('origin') != 'preserved_player' and not record.get('remote_readback_pass'):
            raise ValueError('A new chapter has not passed repository readback')
        if tid in old and old[tid]['sha256'] != record['sha256']:
            raise ValueError('Refusing to replace an existing player recording: ' + tid)
        chunks, pieces = [], []
        for part in record['chunks']:
            source = (ROOT / part['src']).resolve()
            if not source.is_relative_to((OUT / 'audio').resolve()):
                raise ValueError('Unsafe recording path')
            data = source.read_bytes()
            if len(data) != part['bytes'] or not 0 < len(data) <= 4_000_000 or store.digest(data) != part['sha256']:
                raise ValueError('Recording piece failed publication checks')
            relative = Path('audio-v2') / source.relative_to((OUT / 'audio').resolve())
            target = player / relative
            if target.exists():
                if target.read_bytes() != data:
                    raise ValueError('Refusing to overwrite differing audio bytes')
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            chunks.append(dict(part, src=relative.as_posix()))
            pieces.append(data)
        cipher = b''.join(pieces)
        if len(cipher) != record['bytes'] or store.digest(cipher) != record['sha256']:
            raise ValueError('Chapter failed publication reassembly check')
        track = dict(record, chunks=chunks,
                     src=(Path(chunks[0]['src']).parent / 'joined.bin').as_posix())
        tracks.append(track)
        rp = player / 'audio-v2/records' / (tid + '.json')
        if not rp.exists():
            store.write(rp, track)
        elif store.read(rp)['sha256'] != track['sha256']:
            raise ValueError('Existing record conflicts with prepared chapter')
    catalog = dict(existing, ready=True, complete=True, expected_tracks=49,
                   published_tracks=49, tracks=tracks,
                   total_duration=sum(t['duration'] for t in tracks),
                   total_bytes=sum(t['bytes'] for t in tracks),
                   encoding_note='96 kbps stereo MP3 encoded from native 22,050 Hz chapter mixes. Encrypted transport pieces reassemble byte-for-byte before playback.',
                   production_version='layered-1.0.0')
    store.write(player / 'catalog.json', catalog)
    return catalog


def main():
    complete = store.read(OUT / 'complete.json')
    if complete.get('full_collection_complete') is not True or complete.get('completed_tracks') != 49:
        raise ValueError('Recordings must be complete before publishing the player')
    key = (store.WORK / 'recording-key.bin').read_bytes()
    records = [store.read(OUT / 'records' / (tid + '.json')) for tid in ORDER]
    stage = Path('/tmp/taliesin-layered-publication')
    if not stage.exists():
        store.retry('fetch_player_branch', lambda: store.cmd('git','fetch','origin','main'))
        store.cmd('git','worktree','add','--detach',str(stage),'origin/main')
    player = stage / 'player'
    catalog = prepare(player, records)
    vault = player / 'vault-archive.json'
    if vault.exists():
        proof = base64.b64decode(store.read(vault)['proof'], validate=True)
        if AESGCM(key).decrypt(proof[:12], proof[12:], b'Taliesin:v2:unlock') != b'Taliesin private player':
            raise ValueError('Existing player archive key does not match')
    else:
        shutil.copy2(OUT / 'vault.json', vault)
    js = (player / 'player.js').read_text()
    if "['private-archive-v1','vault-archive.json']" not in js:
        raise ValueError('Player does not support the archive key; do not publish silently')
    worker = player / 'sw.js'
    sw = worker.read_text()
    if "'vault-archive.json'" not in sw:
        raise ValueError('Player offline shell lacks the archive vault')
    sw, count = re.subn(r"const SHELL='taliesin-shell-[^']+';",
                        "const SHELL='taliesin-shell-complete-layered-v1';", sw)
    if count != 1:
        raise ValueError('Unexpected service-worker version declaration')
    worker.write_text(sw)
    store.cmd('node','--check',str(player / 'player.js'))
    store.cmd('node','--check',str(worker))
    store.write(player / 'production-complete.json', complete)
    def git(*args):
        return store.cmd('git',*args,cwd=stage)
    git('add','-f','--','player/audio-v2','player/catalog.json','player/vault-archive.json',
        'player/sw.js','player/production-complete.json')
    diff = subprocess.run(['git','diff','--cached','--quiet'],cwd=stage).returncode
    if diff == 1:
        git('commit','-m','Publish all 49 verified tracks while retaining earlier recordings')
    elif diff:
        raise RuntimeError('Cannot inspect player publication changes')
    def push():
        git('fetch','origin','main')
        git('rebase','origin/main')
        git('push','origin','HEAD:main')
        revision = git('rev-parse','HEAD').decode().strip()
        git('fetch','origin','main')
        git('merge-base','--is-ancestor',revision,'origin/main')
        return revision
    revision = store.retry('publish_complete_player',push)
    repo = os.environ['GITHUB_REPOSITORY']
    store.retry('request_pages_build', lambda: store.cmd('gh','api','--method','POST',f'repos/{repo}/pages/builds'))
    owner, name = repo.split('/')
    base = f'https://{owner.lower()}.github.io/{name}/player/'
    expected = store.digest((player / 'catalog.json').read_bytes())
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        try:
            remote = store.fetch(base + 'catalog.json?revision=' + revision)
            if store.digest(remote) == expected:
                break
        except Exception:
            pass
        time.sleep(10)
    else:
        raise TimeoutError('Publication committed, but the complete Pages catalog is not live yet')
    verified = 0
    for track in catalog['tracks']:
        for part in track['chunks']:
            def verify():
                data = store.fetch(base + part['src'])
                if len(data) != part['bytes'] or store.digest(data) != part['sha256']:
                    raise ValueError('Live player audio piece failed readback')
            store.retry('verify_live_audio_piece',verify)
            verified += 1
    store.write(OUT / 'publication.json', {'complete':True,'tracks':49,'player_url':base,
                'commit':revision,'catalog_sha256':expected,'live_pieces_verified':verified,
                'earlier_recordings_preserved':True,'private_keys_published':False})
    store.checkpoint('Verify the complete hosted player and every live audio piece')
    print(json.dumps({'stage':'player_published_and_verified','tracks':49,'url':base,
                      'audio_pieces_verified':verified}),flush=True)


if __name__ == '__main__':
    main()
