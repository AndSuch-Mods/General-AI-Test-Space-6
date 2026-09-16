'use strict';
const SHELL='taliesin-shell-v3.0.0';
const ROOT=new URL('./',self.location.href).href;
const ASSETS=['./','index.html','styles.css','player.js','manifest.webmanifest','icon.svg','icon-192.png','icon-512.png','vault.json','catalog.json'];
self.addEventListener('install',e=>e.waitUntil(caches.open(SHELL).then(c=>c.addAll(ASSETS.map(p=>new URL(p,ROOT).href))).then(()=>self.skipWaiting())));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k.startsWith('taliesin-shell-')&&k!==SHELL).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{
 const u=new URL(e.request.url);if(e.request.method!=='GET'||u.origin!==self.location.origin||!u.href.startsWith(ROOT))return;
 if(u.pathname.includes('/audio-v2/'))return;
 if(e.request.mode==='navigate'){e.respondWith(fetch(e.request).then(async r=>{if(r.ok){const c=await caches.open(SHELL);await c.put(new URL('index.html',ROOT),r.clone());}return r;}).catch(()=>caches.match(new URL('index.html',ROOT).href)));return;}
 if(ASSETS.some(p=>new URL(p,ROOT).pathname===u.pathname))e.respondWith(fetch(e.request).then(async r=>{if(r.ok){const c=await caches.open(SHELL);await c.put(e.request,r.clone());}return r;}).catch(()=>caches.match(e.request)));
});
