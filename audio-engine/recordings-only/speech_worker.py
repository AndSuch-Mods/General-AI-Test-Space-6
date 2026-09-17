"""Offline chapter speech worker using the checksum-pinned native phonemizer.

Preserves the existing VCTK speaker IDs, synthesis scales, and sentence pauses.
Only segment counts are logged; production text is never printed.
"""
from pathlib import Path
import json
import os
import subprocess
import sys
import numpy as np
import soundfile as sf
import onnxruntime as ort

SR = 22050


def valid(path):
    try:
        info = sf.info(path)
        return info.samplerate == SR and info.frames > 1000 and info.channels == 1
    except Exception:
        return False


def main():
    source = Path(sys.argv[1])
    root = source.parent
    rows = json.loads(source.read_text())['segments']
    dest = root / 'speech'
    dest.mkdir(exist_ok=True)
    tools = root / 'tools/piper'
    binary = tools / 'piper_phonemize'
    if not binary.is_file():
        raise RuntimeError('The pinned archive does not contain the native phonemizer')
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.add_session_config_entry('session.intra_op.allow_spinning', '0')
    options.add_session_config_entry('session.inter_op.allow_spinning', '0')
    options.enable_cpu_mem_arena = False
    session = ort.InferenceSession(str(root / 'tools/models/en_GB-vctk-medium.onnx'), options, providers=['CPUExecutionProvider'])
    env = dict(os.environ)
    env['LD_LIBRARY_PATH'] = str(tools) + (':' + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
    process = subprocess.Popen([str(binary), '-l', 'en-gb-x-rp', '--espeak_data', str(tools / 'espeak-ng-data'), '-j', '--allow_missing_phonemes'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
    try:
        for number, row in enumerate(rows):
            target = dest / (row['audio_key'] + '.wav')
            if not valid(target):
                process.stdin.write(json.dumps({'text': row['tts_text']}, ensure_ascii=False) + '\n')
                process.stdin.flush()
                line = process.stdout.readline()
                if not line:
                    raise RuntimeError('The native phonemizer ended without a result')
                ids = json.loads(line)['phoneme_ids']
                start = 0
                pieces = []
                for index, value in enumerate(ids):
                    if value != 2:
                        continue
                    sentence = ids[start:index + 1]
                    start = index + 1
                    if len(sentence) < 5:
                        continue
                    samples = session.run(None, {'input': np.array([sentence], np.int64), 'input_lengths': np.array([len(sentence)], np.int64), 'scales': np.array([.30, 1.45, .30], np.float32), 'sid': np.array([row['speaker_id']], np.int64)})[0].flatten()
                    if not np.isfinite(samples).all():
                        raise ValueError('Speech synthesis produced nonfinite samples')
                    samples = (samples * (32767 / max(.01, float(np.max(np.abs(samples)))))).clip(-32768, 32767).astype(np.int16)
                    pieces.extend([samples, np.zeros(int(.17 * SR), np.int16)])
                if start != len(ids) or not pieces:
                    raise RuntimeError('Incomplete phoneme sequence')
                temp = target.with_suffix('.tmp.wav')
                sf.write(temp, np.concatenate(pieces), SR, subtype='PCM_16', format='WAV')
                if not valid(temp):
                    raise RuntimeError('Incomplete speech file')
                temp.replace(target)
            if (number + 1) % 100 == 0 or number + 1 == len(rows):
                print(f'Speech segments verified: {number + 1}/{len(rows)}', flush=True)
    finally:
        if process.poll() is None:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    main()
