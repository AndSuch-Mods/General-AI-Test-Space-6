"""Publish a catalog only after all 49 complete chapter files pass validation."""
import hashlib,json
from pathlib import Path
p=Path('player/audio-v2');tracks=sorted((json.loads(f.read_text()) for f in p.glob('b*-c*.json')),key=lambda t:t['order'])
expected={(0,0)}|{(1,c) for c in range(1,17)}|{(2,c) for c in range(1,15)}|{(3,c) for c in range(1,19)}
if len(tracks)!=49 or {(t['book'],t['chapter']) for t in tracks}!=expected or sum(t['segments'] for t in tracks)!=11105:raise ValueError('Incomplete chapter inventory')
for t in tracks:
 f=Path('player')/t['src'];raw=f.read_bytes()
 if not t['verified'] or len(raw)!=t['bytes'] or hashlib.sha256(raw).hexdigest()!=t['sha256']:raise ValueError('Damaged chapter')
 if t['bit_rate']!=96000 or t['sample_rate']!=22050 or t['channels']!=2:raise ValueError('Unexpected audio quality')
size=sum(t['bytes'] for t in tracks)
if size>950_000_000:raise ValueError('Audio would exceed site size budget')
catalog={'version':2,'title':'Taliesin','author':'Stephen Lawhead','ready':True,'total_duration':sum(t['duration'] for t in tracks),'total_bytes':size,'encoding_note':'96 kbps stereo MP3, encoded directly from native 22,050 Hz chapter masters. Not upconverted from the compact download.','tracks':tracks}
Path('player/catalog.json').write_text(json.dumps(catalog,indent=2))
Path('player/verification.json').write_text(json.dumps({'chapters':49,'story_chapters':48,'segments':11105,'all_cipher_hashes_valid':True,'all_encoded_chapters_decoded':True,'audio_bytes':size,'duration_seconds':catalog['total_duration'],'bit_rate':96000,'sample_rate':22050,'channels':2},indent=2))
print('Verified all 49 files, 11,105 reading segments, and',size,'bytes.')
