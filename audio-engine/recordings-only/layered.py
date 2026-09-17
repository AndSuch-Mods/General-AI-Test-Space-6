"""Resume the book in durable voice, mix, and verified-MP3 layers.

Existing published recordings are retained byte-for-byte. Native mixes and voice
batches are encrypted before leaving the runner. Never publish a key or source.
"""
from pathlib import Path
import base64, importlib.util, json, math, os, shutil, subprocess, sys, time
import numpy as np
import soundfile as sf
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import layer_store as store

ROOT, OUT, WORK = store.ROOT, store.OUT, store.WORK
spec = importlib.util.spec_from_file_location('base_renderer', Path(__file__).with_name('render.py'))
e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e)
BASELINE = '673e7c0a2f012e176f7ac3226b1c0e76bbc3d09f'
VERSION = 'layered-1.0.0'
VOICE_BATCH = 48


def normalize(rows):
    fixed = []
    for row in rows:
        item = dict(row)
        value = item.get('tempo_adjust')
        if value is None or isinstance(value, str) and not value.strip():
            value = 1.0
        value = float(value)
        if not math.isfinite(value) or not .5 <= value <= 2.0:
            raise ValueError('Invalid tempo for segment ' + str(item['id']))
        item['tempo_adjust'] = value
        for field in ('rate', 'pause_after'):
            if not math.isfinite(float(item[field])):
                raise ValueError('Invalid numeric setting: ' + field)
        fixed.append(item)
    return fixed


def identity(chapter, cues):
    return store.digest(json.dumps({'version': VERSION, 'segments': chapter,
                                  'cues': cues}, sort_keys=True).encode())


def status(stage, tid=None, **extra):
    records = [store.read(p) for p in sorted((OUT / 'records').glob('*.json'))]
    saved = [r for r in records if r.get('verified')]
    value = {'version': VERSION, 'stage': stage, 'current_track': tid,
             'completed_tracks': len(saved), 'expected_tracks': 49,
             'preserved_tracks': sum(r.get('origin') == 'preserved_player' for r in saved),
             'new_tracks': sum(r.get('origin') != 'preserved_player' for r in saved),
             'completed': [r['id'] for r in saved], 'parallel_chapters': 1,
             'voice_batch_size': VOICE_BATCH, 'attempt_limit': 3,
             'player_deployed': False, 'run_id': os.environ.get('GITHUB_RUN_ID'), **extra}
    store.write(OUT / 'progress.json', value)
    print(json.dumps(value), flush=True)
    return value


def check_cipher(record):
    parts = []
    for part in record['chunks']:
        path = (ROOT / part['src']).resolve()
        if not path.is_relative_to((OUT / 'audio').resolve()):
            raise ValueError('Unsafe audio path')
        data = path.read_bytes()
        if len(data) != part['bytes'] or store.digest(data) != part['sha256']:
            raise ValueError('Saved audio chunk is damaged')
        parts.append(data)
    data = b''.join(parts)
    if len(data) != record['bytes'] or store.digest(data) != record['sha256']:
        raise ValueError('Saved audio reassembly failed')
    return data


def preserve_existing(groups, key):
    folder = OUT / 'records'
    folder.mkdir(parents=True, exist_ok=True)
    if (OUT / 'preserved-player.json').exists():
        return
    store.retry('fetch_previous_recordings', lambda: store.cmd('git', 'fetch', '--depth=1', 'origin', BASELINE))
    cat = json.loads(store.cmd('git', 'show', BASELINE + ':player/catalog.json'))
    expected = {f'b{pair[0]}-c{pair[1]:02}': len(ch) for pair, ch in groups}
    preserved = []
    for original in cat['tracks']:
        tid = original['id']
        if tid not in expected or original['segments'] != expected[tid]:
            raise ValueError('Prior recording has different chapter coverage')
        if not all(original.get(k) is True for k in ('verified', 'full_decode_pass', 'reassembly_byte_exact')):
            raise ValueError('Prior chapter lacks verification')
        rp = folder / (tid + '.json')
        if rp.exists():
            if store.read(rp)['sha256'] != original['sha256']:
                raise ValueError('Refusing to overwrite an existing recording')
            preserved.append(tid)
            continue
        record = dict(original)
        chunks = []
        for number, part in enumerate(original['chunks']):
            source = 'player/' + part['src']
            if '..' in Path(source).parts:
                raise ValueError('Unsafe previous recording path')
            data = store.cmd('git', 'show', BASELINE + ':' + source)
            if len(data) != part['bytes'] or store.digest(data) != part['sha256']:
                raise ValueError('Prior recording failed readback')
            target = OUT / 'audio' / (tid + '-' + original['sha256'][:16]) / f'{number:04}.bin'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            chunks.append(dict(part, src=target.relative_to(ROOT).as_posix()))
        record.update(chunks=chunks, origin='preserved_player', preserved_from_commit=BASELINE,
                      cipher_readback_pass=True, source_segment_count_matches=True,
                      decode_verification_origin='existing published record',
                      native_master_status='not required to preserve existing MP3')
        record.pop('src', None)
        check_cipher(record)
        if record.get('key_id') == e.KEY_ID:
            e.validate_record(record, key)
            record['decode_verification_origin'] = 'rechecked in this run'
        store.write(rp, record)
        preserved.append(tid)
    store.write(OUT / 'preserved-player.json', {'source_commit': BASELINE, 'tracks': preserved,
                'count': len(preserved), 'cipher_readback_pass': True, 'original_player_unchanged': True})
    status('earlier_recordings_preserved')
    store.checkpoint('Preserve existing opening and chapters without rerendering')


def validate_speech(rows):
    for row in rows:
        data, rate = sf.read(WORK / 'speech' / (row['audio_key'] + '.wav'), dtype='float32')
        if rate != 22050 or data.ndim != 1 or len(data) < 1000 or not np.isfinite(data).all() or float(np.max(np.abs(data))) < .0001:
            raise ValueError('Invalid speech segment ' + str(row['id']))


def synthesize(rows, runtime):
    # Two workers within the current batch; never two chapters at once.
    buckets = [rows[::2], rows[1::2]]
    processes, logs = [], []
    try:
        for index, batch in enumerate(buckets):
            if not batch:
                continue
            path = WORK / f'batch-{index}.json'
            path.write_text(json.dumps({'segments': batch}, ensure_ascii=False))
            path.chmod(0o600)
            log = (WORK / f'voice-{index}.log').open('wb')
            logs.append(log)
            processes.append(subprocess.Popen([sys.executable, str(runtime / 'render_one_thread.py'), str(path)],
                                              stdout=log, stderr=subprocess.STDOUT))
        deadline = time.monotonic() + 1200
        while any(p.poll() is None for p in processes):
            if any(p.poll() not in (None, 0) for p in processes):
                raise RuntimeError('Voice worker failed; completed cached segments retained')
            if time.monotonic() > deadline:
                raise TimeoutError('Voice batch timed out; completed cached segments retained')
            time.sleep(1)
        if any(p.returncode for p in processes):
            raise RuntimeError('Voice worker failed')
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for log in logs:
            log.close()
    validate_speech(rows)


def chapter(pair, rows, cues, runtime, bank, key, script_sha):
    from mix_audio import mix_chapter, PART_NAMES
    tid = f'b{pair[0]}-c{pair[1]:02}'
    record_path = OUT / 'records' / (tid + '.json')
    if record_path.exists():
        record = store.read(record_path)
        check_cipher(record)
        if record.get('key_id') == e.KEY_ID:
            e.validate_record(record, key)
        return
    fingerprint = identity(rows, cues)
    mixed = WORK / 'mix'
    speech = WORK / 'speech'
    mixed.mkdir(exist_ok=True)
    speech.mkdir(exist_ok=True)
    slug = f'part-{pair[0]:02}-chapter-{pair[1]:02}'
    master_path = mixed / (slug + '.flac')
    mix_label = tid + '/mix'
    if not store.restore(mix_label, mixed, key, fingerprint):
        unique = list({r['audio_key']: r for r in rows}.values())
        for offset in range(0, len(unique), VOICE_BATCH):
            batch = unique[offset:offset + VOICE_BATCH]
            number = offset // VOICE_BATCH
            label = tid + f'/voice-{number:04}'
            voice_id = store.digest(json.dumps({'version': VERSION,
                       'voices': [(r['speaker_id'], r['audio_key']) for r in batch]}).encode())
            status('voice_batch', tid, batch=number + 1,
                   batches=(len(unique) + VOICE_BATCH - 1) // VOICE_BATCH)
            if not store.restore(label, speech, key, voice_id):
                store.retry('render_current_voice_batch', lambda: synthesize(batch, runtime))
                store.store(label, [speech / (r['audio_key'] + '.wav') for r in batch], key, voice_id)
            validate_speech(batch)
        status('mixing', tid)
        result = mix_chapter(pair, rows, speech, mixed, bank, cues)
        if [v['segment_id'] for v in result['segments']] != [v['id'] for v in rows]:
            raise ValueError('Mixed reading segment order changed')
        if not math.isfinite(result['peak_after_master']) or result['peak_after_master'] > .951:
            raise ValueError('Invalid mix peak')
        store.cmd('ffmpeg', '-v', 'error', '-nostdin', '-xerror', '-i', str(master_path), '-f', 'null', '-')
        store.store(mix_label, [master_path, mixed / (slug + '.json')], key, fingerprint)
    result = store.read(mixed / (slug + '.json'))
    if [v['segment_id'] for v in result['segments']] != [v['id'] for v in rows]:
        raise ValueError('Restored mix coverage differs')
    status('encoding_and_verifying', tid)
    mp3 = WORK / 'chapter.mp3'
    store.cmd('ffmpeg', '-v', 'error', '-nostdin', '-y', '-i', str(master_path), '-map_metadata', '-1',
              '-c:a', 'libmp3lame', '-b:a', '96k', '-ar', '22050', '-ac', '2', str(mp3))
    store.cmd('ffmpeg', '-v', 'error', '-nostdin', '-xerror', '-i', str(mp3), '-f', 'null', '-')
    probe = json.loads(store.cmd('ffprobe', '-v', 'error', '-show_entries',
            'format=duration:stream=codec_name,sample_rate,channels,bit_rate', '-of', 'json', str(mp3)))
    stream = probe['streams'][0]
    duration = float(probe['format']['duration'])
    if stream['codec_name'] != 'mp3' or int(stream['sample_rate']) != 22050 or stream['channels'] != 2 or int(stream['bit_rate']) != 96000 or abs(duration - result['duration']) > .25:
        raise ValueError('MP3 format or duration mismatch')
    plain = mp3.read_bytes()
    nonce = os.urandom(12)
    cipher = nonce + AESGCM(key).encrypt(nonce, plain, ('Taliesin:v2:' + tid).encode())
    sha = store.digest(cipher)
    folder = OUT / 'audio' / (tid + '-' + sha[:16])
    folder.mkdir(parents=True, exist_ok=True)
    chunks = []
    for number, offset in enumerate(range(0, len(cipher), e.LIMIT)):
        part = cipher[offset:offset + e.LIMIT]
        target = folder / f'{number:04}.bin'
        target.write_bytes(part)
        chunks.append({'src': target.relative_to(ROOT).as_posix(), 'bytes': len(part), 'sha256': store.digest(part)})
    record = {'id': tid, 'order': e.ORDER.index(tid), 'book': pair[0], 'chapter': pair[1],
              'part': PART_NAMES.get(pair[0], 'Opening'), 'title': f'Chapter {pair[1]}' if pair[0] else 'Title and opening verse',
              'origin': VERSION, 'duration': duration, 'bytes': len(cipher), 'sha256': sha,
              'mp3_sha256': store.digest(plain), 'chunks': chunks, 'segments': len(rows),
              'sample_rate': 22050, 'channels': 2, 'bit_rate': 96000, 'key_id': e.KEY_ID,
              'source_sha256': e.SOURCE, 'source_script_sha256': script_sha,
              'verified': True, 'full_decode_pass': True, 'reassembly_byte_exact': True,
              'ordered_segment_coverage': True, 'sound_events': len(result['events']),
              'ambience_intervals': len(result['ambience']), 'master_peak': result['peak_after_master'],
              'duration_anomaly_count': len(result['duration_anomalies']),
              'native_master_layer': 'recordings/layers/' + mix_label + '.json',
              'render_run_id': os.environ.get('GITHUB_RUN_ID')}
    e.validate_record(record, key)
    store.write(record_path, record)
    status('chapter_verified', tid)
    revision = store.checkpoint('Save verified chapter ' + tid + ' with its preserved production layers')
    for part in chunks:
        url = f'https://raw.githubusercontent.com/{os.environ["GITHUB_REPOSITORY"]}/{revision}/{part["src"]}'
        def verify():
            data = store.fetch(url)
            if len(data) != part['bytes'] or store.digest(data) != part['sha256']:
                raise ValueError('Published checkpoint audio failed readback')
        store.retry('readback_saved_mp3_chunk', verify)
    record['remote_readback_pass'] = True
    store.write(record_path, record)
    status('chapter_saved', tid)
    store.checkpoint('Confirm repository readback of ' + tid)
    shutil.rmtree(speech)
    shutil.rmtree(mixed)
    mp3.unlink(missing_ok=True)


def vault(key):
    path = OUT / 'vault.json'
    if path.exists():
        blob = base64.b64decode(store.read(path)['proof'])
        if AESGCM(key).decrypt(blob[:12], blob[12:], b'Taliesin:v2:unlock') != b'Taliesin private player':
            raise ValueError('Stored vault does not match archive key')
    else:
        nonce = os.urandom(12)
        proof = nonce + AESGCM(key).encrypt(nonce, b'Taliesin private player', b'Taliesin:v2:unlock')
        store.write(path, {'version': 2, 'key_id': e.KEY_ID, 'proof': base64.b64encode(proof).decode()})


def main():
    WORK.mkdir(exist_ok=True, mode=0o700)
    OUT.mkdir(exist_ok=True)
    mode = sys.argv[1]
    if mode == 'key':
        secret = os.environ.get('TALIESIN_ARCHIVE_KEY_B64', '')
        if secret:
            key = base64.b64decode(secret, validate=True)
            if len(key) != 32:
                raise ValueError('Invalid archive key size')
            e.recipe(key)
            target = WORK / 'recording-key.bin'
            target.write_bytes(key)
            target.chmod(0o600)
        else:
            e.key_step()
        return
    if mode != 'run':
        raise ValueError('Expected key or run')
    key = (WORK / 'recording-key.bin').read_bytes()
    rows, cue_map, script_sha = e.recipe(key)
    rows = normalize(rows)
    runtime = e.engine()
    from mix_audio import grouped_segments, load_bank
    from sound_design import sound_bank
    groups = grouped_segments(rows)
    if [f'b{p[0]}-c{p[1]:02}' for p, _ in groups] != e.ORDER:
        raise ValueError('Unexpected chapter order')
    preserve_existing(groups, key)
    vault(key)
    store.release_ready()
    sound_bank(WORK / 'foley')
    bank = load_bank(WORK / 'foley')
    started = time.monotonic()
    for pair, ch in groups:
        if time.monotonic() - started > 240 * 60:
            status('continuation_needed')
            store.checkpoint('Save layered continuation point')
            return
        pids = {r['paragraph_id'] for r in ch}
        cues = {p: v for p, v in cue_map.items() if p in pids}
        chapter(pair, ch, cues, runtime, bank, key, script_sha)
    records = [store.read(OUT / 'records' / (tid + '.json')) for tid in e.ORDER]
    if sum(r['segments'] for r in records) != 11105:
        raise ValueError('Final source segment coverage differs')
    for record in records:
        check_cipher(record)
        if record.get('key_id') == e.KEY_ID:
            e.validate_record(record, key)
        if record.get('origin') != 'preserved_player' and not record.get('remote_readback_pass'):
            # A previous connection failure may have happened after its durable commit.
            for part in record['chunks']:
                remote = store.retry('final_readback', lambda p=part: store.fetch(
                    f'https://raw.githubusercontent.com/{os.environ["GITHUB_REPOSITORY"]}/{store.BRANCH}/{p["src"]}'))
                if store.digest(remote) != part['sha256']:
                    raise ValueError('Final repository readback failed')
            record['remote_readback_pass'] = True
            store.write(OUT / 'records' / (record['id'] + '.json'), record)
    final = status('all_recordings_complete')
    final.update(numbered_chapters=48, opening_tracks=1, reading_segments=11105,
                 duration_seconds=sum(r['duration'] for r in records),
                 source_sha256=e.SOURCE, full_collection_complete=True,
                 native_masters_saved=sum(r.get('origin') != 'preserved_player' for r in records))
    store.write(OUT / 'complete.json', final)
    store.checkpoint('Complete all 49 recordings without replacing earlier chapters')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Safe diagnostic only; no source text, key, or private input in CI logs.
        import traceback
        traceback.print_exc()
        try:
            status('blocked', error_type=type(exc).__name__)
        except Exception:
            pass
        raise SystemExit(1)
