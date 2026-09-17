"""Add an independent key slot without replacing the earlier recordings or key."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
path = ROOT / 'player/player.js'
s = path.read_text()
marker = 'Independent chapter keys v5'
if marker not in s:
    def replace(old, new, count=1):
        global s
        if s.count(old) != count:
            raise ValueError('Player source changed; refusing an unverified patch')
        s = s.replace(old, new)
    replace('index:0,key:null,loading:false', 'index:0,key:null,keys:{},loading:false')
    replace("const track=()=>state.tracks[state.index];", """const track=()=>state.tracks[state.index];
 // Independent chapter keys v5. The earlier key remains in its original slot.
 const keyFor=t=>t?state.keys[t.key_id||'legacy']:null;
 async function restoreKeys(){try{state.keys=(await setting('keys-v5'))||{};const old=await setting('key');if(old)state.keys.legacy=old;}catch{}state.key=Object.values(state.keys)[0]||null;}
 function chooseUnlocked(preferred){const at=state.tracks.findIndex(t=>t.id===preferred&&keyFor(t));if(at>=0)state.index=at;else if(!keyFor(track())){const first=state.tracks.findIndex(t=>keyFor(t));if(first>=0)state.index=first;}}
""")
    replace('const usable=!!(state.key&&track()&&', 'const usable=!!(keyFor(track())&&track()&&')
    replace("if(!state.key)throw new Error('Player is locked.');let plain;", "const chapterKey=keyFor(t);if(!chapterKey){$('unlockPanel').hidden=false;throw new Error('This chapter needs its private listening link. Earlier recordings keep their earlier unlock code.');}let plain;")
    replace('},state.key,raw.slice(12))', '},chapterKey,raw.slice(12))')
    start = s.index(' async function unlock(value)')
    end = s.index(' async function init()', start)
    s = s[:start] + r''' async function unlock(value){
  let code=value.trim(),preferred=new URLSearchParams(location.hash.slice(1)).get('t');
  if(code.includes('#')){const params=new URLSearchParams(code.slice(code.indexOf('#')+1));preferred=params.get('t')||preferred;code=params.get('k')||code;}
  if(!/^[A-Za-z0-9_-]{43}$/.test(code))throw new Error('Paste the complete 43-character unlock code or private link.');
  const key=await crypto.subtle.importKey('raw',b64(code),'AES-GCM',false,['decrypt']);let matched=null;
  for(const [id,file]of [['legacy','vault.json'],['sequential-v5','vault-v5.json']]){
   try{const response=await fetch(file);if(!response.ok)continue;const vault=await response.json(),proof=b64(vault.proof);const text=await crypto.subtle.decrypt({name:'AES-GCM',iv:proof.slice(0,12),additionalData:encode(AAD+'unlock')},key,proof.slice(12));if(new TextDecoder().decode(text)==='Taliesin private player'){matched=id;break;}}catch{}
  }
  if(!matched)throw new Error('That code does not match the available recordings. Keep both private links if you use the earlier and new chapters.');
  state.keys[matched]=key;state.key=key;
  try{await setting('keys-v5',state.keys);if(matched==='legacy')await setting('key',key);}catch{toast('Unlocked for this session. Keep your private link.');}
  $('unlockPanel').hidden=true;$('unlockCode').value='';chooseUnlocked(preferred);controlState();renderList();if(state.tracks.length)await load(state.index,false);
 }
''' + s[end:]
    replace('await scanCache();let code=', 'await scanCache();await restoreKeys();let code=')
    replace("try{state.key=await setting('key');}catch{}if(state.key){$('unlockPanel').hidden=true;await load(state.index,false);}", "if(state.key){chooseUnlocked(null);$('unlockPanel').hidden=true;await load(state.index,false);}")
    replace("meta.textContent=fmt(t.duration);", "meta.textContent=fmt(t.duration)+(state.key&&!keyFor(t)?' · unlock':'');")
    replace('state.key=null;state.loading=false;', 'state.key=null;state.keys={};state.loading=false;')
    replace("await setting('key',undefined,true);", "await setting('key',undefined,true);await setting('keys-v5',undefined,true);")
    path.write_text(s)
sw = ROOT / 'player/sw.js'
s = sw.read_text()
s = re.sub(r"const SHELL='taliesin-shell-[^']+';", "const SHELL='taliesin-shell-v5.0.0';", s)
if "'vault-v5.json'" not in s:
    if "'vault.json','catalog.json'" not in s:
        raise ValueError('Service-worker shell changed; refusing to patch blindly')
    s = s.replace("'vault.json','catalog.json'", "'vault.json','vault-v5.json','catalog.json'")
sw.write_text(s)
print('Player supports both private keys; the original vault and audio remain unchanged')
