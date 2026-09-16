"""Parallel chapter rendering with sealed keys and byte-exact publication checks."""
from pathlib import Path
import base64, hashlib, importlib.util, json, lzma, os, shutil, sys, time, urllib.error
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

spec=importlib.util.spec_from_file_location('chapter_transfer',Path(__file__).with_name('chapter_transfer.py'))
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
ROOT=base.ROOT;WORK=base.WORK;RUN=os.environ['GITHUB_RUN_ID']
TOTAL=12
EXPECTED={'b0-c00'}|{f'b1-c{i:02}' for i in range(1,17)}|{f'b2-c{i:02}' for i in range(1,15)}|{f'b3-c{i:02}' for i in range(1,19)}

def write_public(path,record):
    body={'message':'Register public verification material for independent chapters',
          'branch':'main','content':base64.b64encode(json.dumps(record).encode()).decode()}
    for attempt in range(10):
        try:
            base.api('/contents/'+path,body)
            return
        except urllib.error.HTTPError as error:
            if error.code not in (409,422,500,502,503) or attempt==9:raise
            time.sleep(min(12,1+attempt*2))
    raise RuntimeError('Public key registration failed')

def worker_key(index):
    secret=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    public=secret.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    prefix='.transfer/v3/full-public-'+RUN
    write_public(prefix+'-'+str(index)+'.json',{'run':RUN,'index':index,'public_key':public})
    print('Public key registered for chapter worker '+str(index),flush=True)
    deadline=time.monotonic()+1200
    if index==0:
        collected={}
        while len(collected)<TOTAL and time.monotonic()<deadline:
            for i in range(TOTAL):
                if str(i) in collected:continue
                raw=base.read_remote(prefix+'-'+str(i)+'.json')
                if raw:
                    record=json.loads(raw)
                    if str(record['run'])!=RUN or record['index']!=i:raise ValueError('Incorrect public key identity')
                    collected[str(i)]=record['public_key']
            if len(collected)<TOTAL:time.sleep(5)
        if len(collected)!=TOTAL:raise TimeoutError('Not all workers registered their public keys')
        write_public(prefix+'.json',{'run':RUN,'keys':collected})
        print('All chapter worker public keys collected',flush=True)
    while time.monotonic()<deadline:
        raw=base.read_remote('.transfer/v3/full-sealed-'+RUN+'.json')
        if raw:
            envelope=json.loads(raw)
            if str(envelope['run'])!=RUN:raise ValueError('Wrong key envelope run')
            encrypted=base64.b64decode(envelope['recipients'][str(index)])
            key=secret.decrypt(encrypted,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))
            if len(key)!=32:raise ValueError('Invalid transfer key')
            return key
        time.sleep(8)
    raise TimeoutError('No sealed transfer key arrived')

def input_cipher():
    directory=ROOT/'.transfer/v3'
    manifest=json.loads((directory/'all.manifest.json').read_text())
    parts=[]
    for i,blob in enumerate(manifest['blobs']):
        raw=(directory/'all'/f'{i:03}.bin').read_bytes()
        expected=min(manifest['chunk_bytes'],manifest['cipher_bytes']-i*manifest['chunk_bytes'])
        if len(raw)!=expected:raise ValueError('Incomplete input piece '+str(i))
        actual=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        if actual!=blob:raise ValueError('Corrupted input piece '+str(i))
        parts.append(raw)
    data=b''.join(parts)
    if len(data)!=manifest['cipher_bytes'] or base.digest(data)!=manifest['cipher_sha256']:
        raise ValueError('Input reassembly failed')
    return manifest,data

def read_recipe(transfer_key):
    manifest,data=input_cipher()
    raw=lzma.decompress(AESGCM(transfer_key).decrypt(data[:12],data[12:],b'Taliesin:v3:recipe:all'))
    if base.digest(raw)!=manifest['plain_sha256']:raise ValueError('Decrypted recipe identity mismatch')
    recipe=json.loads(raw)
    if recipe['source_sha256']!='707254842a0c590201adb29f0f07cc39501f001c031a58f3d850728a4265bcc5' or recipe.get('coverage_pass') is not True:
        raise ValueError('Unverified source edition or incomplete coverage')
    if not recipe['columnar'] or len(recipe['fields'])!=len(recipe['segments']):raise ValueError('Invalid recipe columns')
    if any(len(column)!=11105 for column in recipe['segments']):raise ValueError('Incomplete recipe column')
    rows=[]
    for values in zip(*recipe['segments']):
        row=dict(zip(recipe['fields'],values));row['text']=row['tts_text']
        row['audio_key']=base.digest((str(row['speaker_id'])+'|'+row['tts_text']).encode())[:24]
        rows.append(row)
    if len(rows)!=11105 or len({r['id'] for r in rows})!=11105:raise ValueError('Missing or duplicate reading segments')
    cues={v[0]:{'paragraph_id':v[0],'ambience':v[1],
               'events':[{'sound':e[0],'fraction':e[1],'gain':e[2]} for e in v[2]]} for v in recipe['cues']}
    return rows,cues,base64.b64decode(recipe['player_key'])

def verify_record(record,key=None):
    if record['id'] not in EXPECTED or record.get('verified') is not True or record.get('full_decode_pass') is not True:
        raise ValueError('Unverified chapter record')
    parts=[]
    for part in record['chunks']:
        path=(ROOT/'player'/part['src']).resolve()
        if not path.is_relative_to((ROOT/'player/audio-v2').resolve()):raise ValueError('Unsafe chapter path')
        raw=path.read_bytes()
        if not 0<len(raw)<=base.LIMIT or len(raw)!=part['bytes'] or base.digest(raw)!=part['sha256']:
            raise ValueError('Corrupted chapter piece')
        parts.append(raw)
    data=b''.join(parts)
    if len(data)!=record['bytes'] or base.digest(data)!=record['sha256']:raise ValueError('Corrupted chapter')
    if key is not None:
        raw=AESGCM(key).decrypt(data[:12],data[12:],('Taliesin:v2:'+record['id']).encode())
        if base.digest(raw)!=record['mp3_sha256']:raise ValueError('Incorrect decrypted MP3')
    return True

def render_worker():
    index=int(os.environ['RENDER_INDEX'])
    if not 0<=index<TOTAL:raise ValueError('Invalid worker index')
    input_cipher()
    transfer_key=worker_key(index)
    runtime=base.engine()
    from mix_audio import grouped_segments
    rows,cues,player_key=read_recipe(transfer_key)
    groups=grouped_segments(rows)
    if len(groups)!=49 or {f'b{k[0]}-c{k[1]:02}' for k,_ in groups}!=EXPECTED:
        raise ValueError('Missing chapter groups')
    remaining=[]
    for order,(pair,chapter) in enumerate(groups):
        tid=f'b{pair[0]}-c{pair[1]:02}'
        path=ROOT/'player/audio-v2/records'/(tid+'.json')
        if path.exists():
            verify_record(json.loads(path.read_text()),player_key)
            continue
        remaining.append((order,pair,chapter))
    bins=[[] for _ in range(TOTAL)];weights=[0]*TOTAL
    for item in sorted(remaining,key=lambda item:sum(len(r['tts_text']) for r in item[2]),reverse=True):
        slot=min(range(TOTAL),key=lambda i:weights[i])
        bins[slot].append(item);weights[slot]+=sum(len(r['tts_text']) for r in item[2])
    export=Path('/tmp/cipher-export');export.mkdir(exist_ok=True)
    (export/'records').mkdir(exist_ok=True)
    summary=[]
    for order,pair,chapter in sorted(bins[index]):
        tid=f'b{pair[0]}-c{pair[1]:02}'
        base.render(tid,chapter,cues,player_key,runtime)
        path=ROOT/'player/audio-v2/records'/(tid+'.json')
        record=json.loads(path.read_text());verify_record(record,player_key)
        folder=(ROOT/'player'/record['chunks'][0]['src']).parent
        shutil.copytree(folder,export/folder.name,dirs_exist_ok=True)
        shutil.copy2(path,export/'records'/path.name)
        summary.append({'id':tid,'duration':record['duration'],'segments':record['segments'],'chunks':len(record['chunks'])})
        print('Verified and exported '+tid,flush=True)
    (export/('worker-'+str(index)+'.json')).write_text(json.dumps({'worker':index,'tracks':summary}))
    print(json.dumps({'worker':index,'chapters':len(summary),'segments':sum(r['segments'] for r in summary)}),flush=True)

def publish():
    key=base.receive_key()
    if len(key)!=32:raise ValueError('Invalid publication key')
    path=WORK/'browser-key.bin';path.write_bytes(key);path.chmod(0o600)
    records=[json.loads(p.read_text()) for p in (ROOT/'player/audio-v2/records').glob('*.json')]
    if len(records)!=49 or {r['id'] for r in records}!=EXPECTED:raise ValueError('Not all 49 tracks are present')
    if sum(r['segments'] for r in records)!=11105:raise ValueError('Total reading segment count is incomplete')
    for record in records:verify_record(record,key)
    base.catalog()
    catalog=json.loads((ROOT/'player/catalog.json').read_text())
    if catalog.get('complete') is not True:raise ValueError('Catalog is incomplete')
    print(json.dumps({'complete':True,'tracks':49,'segments':11105,'duration':catalog['total_duration'],
                      'bytes':catalog['total_bytes'],'all_chapters_authenticated':True,'largest_piece_bytes':base.LIMIT}),flush=True)

if __name__=='__main__':
    WORK.mkdir(exist_ok=True,mode=0o700)
    if len(sys.argv)!=2 or sys.argv[1] not in ('render','publish','verify-input'):
        raise SystemExit('Expected render, publish or verify-input')
    if sys.argv[1]=='render':render_worker()
    elif sys.argv[1]=='publish':publish()
    else:
        manifest,data=input_cipher();print(json.dumps({'pieces':len(manifest['blobs']),'bytes':len(data),'verified':True}))
