"""One chapter at a time: authenticate, render, publish, verify, checkpoint.

The private key remains only in this job's temporary directory. A failed chapter
stops the queue. A resumed job skips checkpoints already verified on the live site.
"""
from pathlib import Path
import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / '.transfer/serial/request.json'
KEY_ID = 'private-archive-v1'
spec = importlib.util.spec_from_file_location('single_chapter_engine', ROOT / 'audio-engine/v5/one_chapter.py')
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)
engine.KEY_ID = KEY_ID
CURRENT = engine.WORK / 'current-track.json'
EXPORT = Path('/tmp/cipher-export')


def config():
    item = engine.read_json(CONFIG)
    if item.get('key_id') != KEY_ID or item.get('parallel_chapters') != 1:
        raise ValueError('Only single-chapter production is permitted')
    if item.get('start_track') not in engine.ORDER or item.get('end_track') not in engine.ORDER:
        raise ValueError('Invalid chapter range')
    if engine.ORDER.index(item['start_track']) > engine.ORDER.index(item['end_track']):
        raise ValueError('Chapter range is reversed')
    return item


def next_track(existing):
    for tid in engine.ORDER:
        if tid not in existing:
            return tid
        record = existing[tid]
        if not all(record.get(flag) is True for flag in ('verified', 'full_decode_pass', 'reassembly_byte_exact')):
            raise ValueError('An earlier recording has not passed verification: ' + tid)
        if record.get('key_id'):
            path = ROOT / 'player/checkpoints' / (tid + '.json')
            if not path.exists():
                return tid
            proof = engine.read_json(path)
            if proof.get('remote_readback_pass') is not True or proof.get('cipher_sha256') != record['sha256']:
                raise ValueError('Earlier chapter checkpoint mismatch: ' + tid)
    return None


def choose():
    limits = config()
    tid = next_track(engine.records())
    if tid is None or engine.ORDER.index(tid) > engine.ORDER.index(limits['end_track']):
        return None
    if engine.ORDER.index(tid) < engine.ORDER.index(limits['start_track']):
        raise ValueError('An earlier chapter must be verified first: ' + tid)
    engine.private_write(CURRENT, json.dumps({'track': tid}).encode())
    return tid


def request():
    tid = engine.read_json(CURRENT)['track']
    if tid != next_track(engine.records()):
        raise ValueError('This is not the next unverified chapter')
    manifest = engine.read_json(ROOT / '.transfer/archive/inputs' / (tid + '.json'))
    if manifest.get('track') != tid or manifest.get('source_sha256') != engine.SOURCE or manifest.get('key_id') != KEY_ID:
        raise ValueError('Input manifest identity mismatch')
    raw = base64.b64decode((ROOT / '.transfer/archive/inputs' / (tid + '.b64')).read_text(), validate=True)
    if not 28 < len(raw) < 500000 or len(raw) != manifest['cipher_bytes'] or engine.digest(raw) != manifest['cipher_sha256']:
        raise ValueError('Input transfer checksum failed; rendering stopped')
    return tid, manifest, raw


engine.next_track = next_track
engine.request = request


def command(*args):
    return subprocess.run(args, cwd=ROOT, check=True, timeout=300)


def synchronize():
    for attempt in range(4):
        try:
            command('git', 'pull', '--rebase', 'origin', 'main')
            return
        except subprocess.CalledProcessError:
            if (ROOT / '.git/rebase-merge').exists() or (ROOT / '.git/rebase-apply').exists():
                raise RuntimeError('Repository conflict; stopped without overwriting changes')
            if attempt == 3:
                raise
            time.sleep(2 ** (attempt + 1))


def push_files(paths, message):
    command('git', 'add', '-f', '--', *paths)
    changed = subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=ROOT).returncode
    if changed not in (0, 1):
        raise RuntimeError('Unable to inspect staged changes')
    if changed:
        command('git', 'commit', '-m', message)
    for attempt in range(4):
        synchronize()
        if subprocess.run(['git', 'push', 'origin', 'HEAD:main'], cwd=ROOT, timeout=180).returncode == 0:
            return
        if attempt == 3:
            raise RuntimeError('Publication push failed; no next chapter will start')
        time.sleep(2 ** (attempt + 1))


def progress(stage, tid=None, completed=None, error=None):
    path = ROOT / 'player/production-progress.json'
    value = {'stage': stage, 'track': tid, 'run_id': os.environ['GITHUB_RUN_ID'],
             'parallel_chapters': 1, 'completed_in_run': completed or [],
             'published_tracks': len(engine.records()), 'expected_tracks': len(engine.ORDER)}
    if error:
        value['error_type'] = error
    path.write_text(json.dumps(value, indent=2))
    push_files([str(path.relative_to(ROOT))], 'Audiobook chapter checkpoint: ' + stage + (' ' + tid if tid else ''))
    print(json.dumps(value), flush=True)


def archive_chapter(tid):
    record = engine.read_json(ROOT / 'player/audio-v2/records' / (tid + '.json'))
    folder = Path(record['chunks'][0]['src']).parent
    target = EXPORT / folder
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / 'player' / folder, target, dirs_exist_ok=True)
    dest = EXPORT / 'audio-v2/records'
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'player/audio-v2/records' / (tid + '.json'), dest / (tid + '.json'))
    return record, str(folder)


def publish_pages():
    for attempt in range(4):
        result = subprocess.run(['gh', 'api', '--method', 'POST', 'repos/' + os.environ['GITHUB_REPOSITORY'] + '/pages/builds'], cwd=ROOT, timeout=90)
        if result.returncode == 0:
            return
        if attempt == 3:
            raise RuntimeError('Pages publication failed after bounded retries')
        time.sleep(15 * (attempt + 1))


def main():
    completed = []
    # Leave margin for artifact export and cleanup before the hosted-job limit.
    deadline = time.monotonic() + 300 * 60
    command('git', 'config', 'user.name', 'github-actions[bot]')
    command('git', 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    key = (engine.WORK / 'v5-key.bin').read_bytes()
    if len(key) != 32:
        raise ValueError('Private key was not received')
    old_vaults = {name: (ROOT / 'player' / name).read_bytes() for name in ('vault.json', 'vault-v5.json', 'vault-v6.json')}
    patch_spec = importlib.util.spec_from_file_location('continuation_player_patch', ROOT / 'audio-engine/v6/continue_chapter.py')
    patch = importlib.util.module_from_spec(patch_spec)
    patch_spec.loader.exec_module(patch)
    patch.patch_player()
    command('node', '--check', str(ROOT / 'player/player.js'))
    command('node', '--check', str(ROOT / 'player/sw.js'))
    push_files(['player/player.js', 'player/sw.js'], 'Keep all existing unlock keys in the sequential chapter player')
    tid = None
    try:
        while time.monotonic() < deadline:
            synchronize()
            tid = choose()
            if tid is None:
                progress('complete', completed=completed)
                return
            input_deadline = min(deadline, time.monotonic() + 900)
            while not all((ROOT / '.transfer/archive/inputs' / (tid + ext)).is_file() for ext in ('.json', '.b64')):
                if time.monotonic() >= input_deadline:
                    progress('paused_missing_input', tid, completed)
                    return
                time.sleep(15)
                synchronize()
            engine.recipe(key)
            progress('rendering_one_chapter', tid, completed)
            engine.render()
            record, folder = archive_chapter(tid)
            for name, raw in old_vaults.items():
                if (ROOT / 'player' / name).read_bytes() != raw:
                    raise ValueError('An earlier unlock proof changed; publication stopped')
            push_files(['player/' + folder, 'player/audio-v2/records/' + tid + '.json',
                        'player/catalog.json', 'player/verification.json'], 'Publish verified chapter ' + tid)
            publish_pages()
            engine.verify_remote()
            checkpoint = 'player/checkpoints/' + tid + '.json'
            push_files([checkpoint], 'Checkpoint ' + tid + ' after live byte-for-byte verification')
            proof = engine.read_json(ROOT / checkpoint)
            if proof.get('remote_readback_pass') is not True or proof['cipher_sha256'] != record['sha256']:
                raise ValueError('Live verification checkpoint does not match')
            completed.append(tid)
            progress('chapter_complete', tid, completed)
            dest = EXPORT / 'checkpoints'
            dest.mkdir(exist_ok=True)
            shutil.copy2(ROOT / checkpoint, dest / (tid + '.json'))
            for name in ('speech', 'mix', 'foley'):
                shutil.rmtree(engine.WORK / name, ignore_errors=True)
        progress('paused_job_limit', tid, completed)
    except Exception as exc:
        try:
            if subprocess.run(['git', 'diff', '--quiet'], cwd=ROOT).returncode == 0:
                progress('stopped_on_failure', tid, completed, type(exc).__name__)
        except Exception:
            pass
        raise


if __name__ == '__main__':
    engine.WORK.mkdir(exist_ok=True, mode=0o700)
    mode = sys.argv[1] if len(sys.argv) == 2 else ''
    if mode in ('plan', 'key'):
        tid = choose()
        if tid is None:
            raise SystemExit('No remaining chapters in the requested range')
        _, manifest, _ = request()
        if mode == 'key':
            engine.acquire_key()
        else:
            print(json.dumps({'next_chapter': tid, 'segments': manifest['segments'], 'parallel_chapters': 1}))
    elif mode == 'run':
        main()
    else:
        raise SystemExit('Expected plan, key or run')
