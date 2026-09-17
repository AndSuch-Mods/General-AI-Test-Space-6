"""Generated test audio only. No book text, listening keys, or production writes."""
from pathlib import Path
import hashlib, json, os, tempfile
from unittest.mock import patch
import numpy as np
import soundfile as sf
import layered as layer
import layer_store as store


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old_work = layer.e.WORK
        layer.e.WORK = root / 'work'
        layer.e.WORK.mkdir()
        runtime = layer.e.engine()
        layer.e.WORK = old_work
        from mix_audio import mix_chapter
        sr = 22050
        speech = root / 'speech'
        mixed = root / 'mixed'
        speech.mkdir(); mixed.mkdir()
        rows = []
        for i, tempo in enumerate([None, '', 1.0]):
            name = str(i) * 24
            x = (.12 * np.sin(2 * np.pi * 220 * np.arange(sr) / sr)).astype(np.float32)
            sf.write(speech / (name + '.wav'), x, sr)
            rows.append({'id': i, 'book': 1, 'chapter': 99, 'paragraph_id': i,
                         'page': 1, 'actor': 'Narrator', 'audio_key': name,
                         'speaker_id': 0, 'rate': 1.0, 'delivery': 'neutral',
                         'pause_after': .1, 'quoted': False, 'kind': 'narration', 'tempo_adjust': tempo,
                         'tts_text': 'Generated audio test.', 'text': 'Generated audio test.'})
        fixed = layer.normalize(rows)
        assert all(r['tempo_adjust'] == 1 for r in fixed)
        assert rows[0]['tempo_adjust'] is None
        for bad in [0, -1, 3, 'invalid', float('nan'), float('inf')]:
            try:
                layer.normalize([dict(rows[0], tempo_adjust=bad)])
            except (ValueError, TypeError):
                pass
            else:
                raise AssertionError('Invalid tempo was accepted')
        result = mix_chapter((1, 99), fixed, speech, mixed, {}, {})
        assert [r['segment_id'] for r in result['segments']] == [0, 1, 2]
        assert result['peak_after_master'] <= .951
        master = mixed / result['path']
        store.cmd('ffmpeg', '-v', 'error', '-xerror', '-i', str(master), '-f', 'null', '-')
        network = {}
        def fake_cmd(*args, **kwargs):
            if args[0][:3] == ['gh', 'release', 'upload']:
                file = Path(args[0][4])
                url = 'https://github.com/test/repo/releases/download/' + store.TAG + '/' + file.name
                network[url] = file.read_bytes()
            return type('Process', (), {'returncode': 0})()
        with patch.object(store, 'ROOT', root), patch.object(store, 'OUT', root/'recordings'), \
             patch.object(store, 'WORK', root), patch.object(store, 'checkpoint', lambda *a: 'test'), \
             patch.object(store.subprocess, 'run', fake_cmd), patch.object(store, 'fetch', lambda url: network[url]), \
             patch.dict(os.environ, {'GITHUB_REPOSITORY': 'test/repo'}), patch.object(store, 'CHUNK', 5000):
            key = os.urandom(32)
            proof = store.store('b1-c99/mix', [master], key, 'test-fingerprint')
            assert len(proof['chunks']) > 1
            dest = root / 'restored'
            assert store.restore('b1-c99/mix', dest, key, 'test-fingerprint')
            assert (dest / master.name).read_bytes() == master.read_bytes()
            url = proof['chunks'][0]['url']
            network[url] = b'corrupted'
            with patch.object(store.time, 'sleep', lambda _: None):
                try:
                    store.restore('b1-c99/mix', dest, key, 'test-fingerprint')
                except ValueError:
                    pass
                else:
                    raise AssertionError('Corrupt layer was accepted')
        attempts = []
        def flaky():
            attempts.append(True)
            if len(attempts) < 3:
                raise ConnectionError('Generated connection failure')
            return 'recovered'
        with patch.object(store.time, 'sleep', lambda _: None):
            assert store.retry('synthetic_retry_test', flaky) == 'recovered'
        assert len(attempts) == 3
        print(json.dumps({'passed': True, 'synthetic_audio_only': True,
                          'empty_tempo_regression': True, 'reject_invalid_tempo': True,
                          'mix_segment_order': True, 'full_flac_decode': True,
                          'encrypted_layer_roundtrip': True, 'reject_corrupted_layer': True,
                          'bounded_three_attempt_recovery': True}), flush=True)


if __name__ == '__main__':
    main()
