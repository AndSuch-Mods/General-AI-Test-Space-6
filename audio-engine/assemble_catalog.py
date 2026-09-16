"""Fail closed unless every expected chapter is present and hash-verified."""
import hashlib,json
from pathlib import Path
folder=Path('player/audio');tracks=[]
for p in sorted(folder.glob('b*-c*.json')):
 t=json.loads(p.read_text());path=Path('player')/t['src']
 if not path.is_file() or path.stat().st_size!=t['bytes']:raise SystemExit('Missing or truncated encrypted audio')
 if hashlib.sha256(path.read_bytes()).hexdigest()!=t['sha256']:raise SystemExit('Audio checksum mismatch')
 if not t.get('ready') or t['duration']<=0:raise SystemExit('Chapter was not verified')
 tracks.append(t)
tracks.sort(key=lambda t:t['order'])
expected={(0,0)}|{(1,c) for c in range(1,17)}|{(2,c) for c in range(1,15)}|{(3,c) for c in range(1,19)}
if len(tracks)!=49 or {(t['book'],t['chapter']) for t in tracks}!=expected:raise SystemExit('The full 49-track inventory is not present')
if len({t['id'] for t in tracks})!=49 or sum(t['segments'] for t in tracks)!=11105:raise SystemExit('Reading segment coverage mismatch')
catalog={'version':1,'title':'Taliesin','author':'Stephen Lawhead','storyChapters':48,'totalDuration':sum(t['duration'] for t in tracks),'totalBytes':sum(t['bytes'] for t in tracks),'tracks':tracks}
Path('player/catalog.json').write_text(json.dumps(catalog,indent=2))
print(json.dumps({'tracks':len(tracks),'segments':sum(t['segments'] for t in tracks),'hours':catalog['totalDuration']/3600,'bytes':catalog['totalBytes']}))
