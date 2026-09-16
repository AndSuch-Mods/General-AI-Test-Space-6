# Offline audio workspace

This repository holds public tool-download workflows and a resumable local speech renderer. It does not contain a book, manuscript, transcript or generated audiobook. Keep private material outside this public repository.

## Public tools

- `.github/workflows/bootstrap-audio.yml` downloads the Piper 2023.11.14-2 Linux x86-64 executable and the English VCTK medium multi-speaker model. The workflow exports a tool-only ZIP artifact with a one-day retention period. It can be run again manually.
- `.github/workflows/kokoro-tools.yml` downloads an alternative Kokoro engine for evaluation. It is not required for the Piper pipeline.
- `render_speech.py` is the local batch renderer used by the production pipeline. It resumes existing speech chunks and fails on missing or malformed output rather than silently skipping text.

Neither workflow reads or processes private book text. GitHub is used only to obtain public tools. Speech generation and audio mixing run locally, without a third-party audio-generation account or API key.

## Run the renderer

Tested with Python 3.13 on Linux x86-64. Install NumPy and SoundFile, extract the Piper artifact, and provide a reviewed script manifest:

```sh
python -m pip install numpy soundfile
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
python render_speech.py private/script.json \
  --model tools/models/en_GB-vctk-medium.onnx \
  --binary tools/piper/piper \
  --output private/speech \
  --mode all
```

Each item in `script.json`'s `segments` array contains `id`, `book`, `chapter`, `page`, `quoted`, `speaker_id`, `tts_text`, and `audio_key`. The key is the first 24 hex characters of SHA256(`str(speaker_id) + "|" + tts_text`). Use only a manifest you created or reviewed.

This script is the speech stage, not an automatic fiction editor. Source extraction, OCR/conversion repair, pronunciation preparation and dialogue attribution must be completed and checked first. The full production package supplied privately with the finished recording also includes the reviewed manifest, procedural sound generator, cue plan, chapter mixer and verification reports. Those private files are intentionally not committed here.

## Audio production

The local mixer writes chapter FLAC masters and one MP3, preserves all supplied reading segments, embeds chapter markers, checks duration, and decodes the complete output for errors. Ambient beds and foley are made with noise, filters, oscillators and envelopes. They are synthesized approximations, not a library of recorded effects. Stock neural voices are not a human cast, and spoken poetry is not converted into singing.

The generic engine source archive is supplied separately in the conversation. The book-specific production package contains copyrighted text and should remain private.

## Credits

Piper: https://github.com/rhasspy/piper, release 2023.11.14-2, MIT license. VCTK model: https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_GB/vctk/medium. Its model card identifies the VCTK dataset as CC BY 4.0; dataset reference: https://datashare.ed.ac.uk/handle/10283/3443. The model card and license notices remain with the downloaded tools. FFmpeg and Python dependencies retain their own licenses.
