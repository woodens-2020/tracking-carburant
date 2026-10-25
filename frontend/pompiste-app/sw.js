/* Service worker de l'application pompiste (PWA « Konekta Ponpis »).
   Rend l'espace /pompiste installable sur l'appareil (icône, plein écran).
   Toujours le réseau d'abord : les données (compteurs, argent) viennent du
   serveur en direct, rien n'est servi depuis un cache périmé. Le cache ne
   sert qu'à afficher l'écran et un message clair quand il n'y a pas
   d'internet. Les appels /api/ ne passent jamais par le cache. */
const VERSION = 'ponpis-v1';
const COQUILLE = ['/pompiste', '/pompiste-app/icone-192.png', '/pompiste-app/icone-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(COQUILLE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(cles => Promise.all(cles.filter(k => k !== VERSION).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

const HORS_LIGNE = `<!DOCTYPE html><html lang="ht"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Konekta Ponpis</title>
<style>body{margin:0;font:18px system-ui,sans-serif;background:#eef2f7;color:#0f172a;display:grid;place-items:center;min-height:100vh;text-align:center}
.c{max-width:420px;padding:24px}.i{font-size:64px}button{font:inherit;font-weight:800;border:0;border-radius:14px;padding:16px 28px;background:#ea580c;color:#fff;margin-top:18px}</style></head>
<body><div class="c"><div class="i">📡</div><h1>Pa gen entènèt</h1><p>Aparèy la pa konekte. Tcheke Wi-Fi a oswa done mobil yo, epi eseye ankò.</p>
<p style="color:#64748b">Pas de connexion internet. Vérifiez le Wi-Fi ou les données mobiles, puis réessayez.</p>
<button onclick="location.reload()">Eseye ankò / Réessayer</button></div></body></html>`;

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  if (e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request).then(r => {
      const copie = r.clone();
      if (r.ok && url.pathname === '/pompiste') caches.open(VERSION).then(c => c.put('/pompiste', copie));
      return r;
    }).catch(() => new Response(HORS_LIGNE, {headers: {'Content-Type': 'text/html; charset=utf-8'}})));
    return;
  }
  if (url.pathname.startsWith('/pompiste-app/')) {
    e.respondWith(caches.match(e.request).then(m => m || fetch(e.request)));
  }
});
