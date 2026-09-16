"""Add resumable byte-exact transport without changing the audio timeline."""
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
p = ROOT / 'player/player.js'
s = p.read_text()
marker = '// Verified transport chunks v3'
if marker not in s:
    needle = '  const res=await fetch(url,{signal});'
    assert s.count(needle) == 1, 'Player source changed; review before patching'
    insert = '''  // Verified transport chunks v3
  if(Array.isArray(t.chunks)){
   if(!Number.isSafeInteger(t.bytes)||t.bytes<28||t.bytes>100000000||!t.chunks.length||t.chunks.length>100)throw new Error('Invalid chapter chunk manifest.');
   if(t.chunks.reduce((n,p)=>n+p.bytes,0)!==t.bytes)throw new Error('Chapter chunk sizes do not add up.');
   const joined=new Uint8Array(t.bytes);let offset=0,pieceCache;
   try{pieceCache=await caches.open(CACHE+'-pieces');}catch{}
   for(const part of t.chunks){
    if(signal?.aborted)throw new DOMException('Cancelled','AbortError');
    if(!Number.isSafeInteger(part.bytes)||part.bytes<1||part.bytes>4000000||!/^[a-f0-9]{64}$/.test(part.sha256))throw new Error('Invalid audio piece.');
    const pieceURL=urlFor(part);let raw;
    if(pieceCache){const hit=await pieceCache.match(pieceURL);if(hit){raw=new Uint8Array(await hit.arrayBuffer());if(raw.length!==part.bytes||hex(await crypto.subtle.digest('SHA-256',raw))!==part.sha256){await pieceCache.delete(pieceURL);raw=null;}}}
    if(!raw){
     const response=await fetch(pieceURL,{signal});if(!response.ok)throw new Error(`Audio piece could not be loaded (${response.status}). Retry to resume.`);
     // HTTP compression can make Content-Length exceed the decoded piece size. The bounded reader and SHA-256 below validate the actual bytes.
     if(response.body?.getReader){const reader=response.body.getReader();raw=new Uint8Array(part.bytes);let used=0;try{for(;;){const {done,value}=await reader.read();if(done)break;if(used+value.length>part.bytes){await reader.cancel();throw new Error('Audio piece is larger than expected.');}raw.set(value,used);used+=value.length;if(progress)progress(offset+used,t.bytes);}if(used!==part.bytes)throw new Error('Incomplete audio piece. Retry to resume.');}finally{reader.releaseLock();}}
     else raw=new Uint8Array(await response.arrayBuffer());
     if(raw.length!==part.bytes||hex(await crypto.subtle.digest('SHA-256',raw))!==part.sha256)throw new Error('Audio piece failed its integrity check. Retry to resume.');
     try{if(pieceCache)await pieceCache.put(pieceURL,new Response(raw,{headers:{'Content-Type':'application/octet-stream'}}));}catch{}
    }
    joined.set(raw,offset);offset+=raw.length;if(progress)progress(offset,t.bytes);
   }
   if(hex(await crypto.subtle.digest('SHA-256',joined))!==t.sha256)throw new Error('Reassembled chapter failed its integrity check.');
   return joined;
  }
'''
    s = s.replace(needle, insert + needle)
    changes = {
      "const NS='taliesin-v2'": "const NS='taliesin-v3'",
      'state.tracks.length!==49': 'state.tracks.length===0',
      '48 chapters + opening · ${fmt(state.catalog.total_duration)}': "${state.tracks.length} of ${state.catalog.expected_tracks||49} tracks published · ${fmt(state.catalog.total_duration)}",
      '% through the book`': "% through ${state.catalog.complete===false?'available chapters':'the book'}`",
      "status('End of the book.');": "status(state.catalog.complete===false?'End of the available chapters. The remaining chapters have not been published yet.':'End of the book.');",
      'The full book is saved on this device.': 'All available chapters are saved on this device.',
      'All chapters saved for offline listening.': 'Available chapters saved for offline listening.',
      'await caches.delete(CACHE);await scanCache();': "await caches.delete(CACHE);await caches.delete(CACHE+'-pieces');await scanCache();",
    }
    for old,new in changes.items():
        assert old in s, 'Expected player text was not found: '+old
        s=s.replace(old,new)
    p.write_text(s)
    p=ROOT/'player/sw.js'
    p.write_text(p.read_text().replace('taliesin-shell-v2.0.0','taliesin-shell-v3.0.0'))
    p=ROOT/'player/index.html'
    p.write_text(p.read_text().replace('Save the whole book offline','Save available chapters offline'))
