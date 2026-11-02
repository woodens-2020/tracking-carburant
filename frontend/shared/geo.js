/* Carte des stations (Geospatial Station Intelligence Center) — page « geo ».
   Données : /api/geospatial/* (backend/geospatial.py). Carte : Leaflet +
   regroupement des marqueurs, servis par l'application (/vendor/leaflet),
   fond OpenStreetMap. Utilise les aides globales GET, POST, esc, notify,
   goTo, showConfirm et le changement de station (ouvrirChoixStation). */
(function(){
'use strict';
const GEO = window.GEO = {stations:[], vue:null, sel:null, carte:null, groupe:null, marqueurs:{}, f:{q:'', departement:'', commune:'', statut:'', gps:''},
  dep:null, mode:null, edition:null};
const HAITI = [[17.95, -74.55], [20.15, -71.62]];
const COUL = {verifie:'#22c55e', non_verifie:'#f59e0b', absent:'#94a3b8'};
const LIB_GPS = {verifie:'Position vérifiée', non_verifie:'Position non vérifiée', absent:'Sans position GPS'};
const FONDS = {
  'Plan (OpenStreetMap)': ['https://tile.openstreetmap.org/{z}/{x}/{y}.png', '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'],
  'Humanitaire (OSM France / HOT)': ['https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png', '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>, tuiles HOT par OSM France'],
};
const api = (m, u, b) => m === 'GET' ? GET('/geospatial' + u) : (m === 'PUT' ? PUT('/geospatial' + u, b) : POST('/geospatial' + u, b));
const n6 = v => v == null ? '—' : Number(v).toFixed(6);
const qs = o => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== '' && v != null)).toString();

// ── Chargement de Leaflet (une seule fois, à la première ouverture) ───
let _leaflet = null;
GEO.leaflet = function(){
  if(window.L && L.markerClusterGroup) return Promise.resolve();
  if(_leaflet) return _leaflet;
  const css = h => new Promise(r => { const l = document.createElement('link'); l.rel = 'stylesheet'; l.href = h; l.onload = r; l.onerror = r; document.head.appendChild(l); });
  const js = s => new Promise((ok, ko) => { const e = document.createElement('script'); e.src = s; e.onload = ok; e.onerror = () => ko(new Error('Carte indisponible')); document.head.appendChild(e); });
  _leaflet = Promise.all([css('/vendor/leaflet/leaflet.css'), css('/vendor/leaflet/MarkerCluster.css'), css('/vendor/leaflet/MarkerCluster.Default.css')])
    .then(() => js('/vendor/leaflet/leaflet.js')).then(() => js('/vendor/leaflet/leaflet.markercluster.js'))
    .then(() => { L.Icon.Default.imagePath = '/vendor/leaflet/images/'; });
  return _leaflet;
};
function fonds(){
  const out = {};
  for(const [nom, [url, attr]] of Object.entries(FONDS)) out[nom] = L.tileLayer(url, {maxZoom:19, attribution:attr});
  return out;
}
function icone(couleur, choisi){
  return L.divIcon({className:'', iconSize:[26, 26], iconAnchor:[13, 26], popupAnchor:[0, -24],
    html:`<div class="geo-mark ${choisi ? 'sel' : ''}" style="background:${couleur}"></div>`});
}

// ── Page ────────────────────────────────────────────────────────────
window.initGeo = async function(){
  const page = document.getElementById('page-geo');
  if(!page) return;
  if(!page.dataset.pret){
    page.innerHTML = `<div class="geo-wrap">
      <div class="geo-kpis" id="geo-kpis" aria-live="polite"></div>
      <div class="geo-corps">
        <aside class="geo-side" id="geo-side" aria-label="Stations">
          <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 12px;border-bottom:1px solid var(--border)">
            <b>Stations <span id="geo-nb" class="geo-note"></span></b>
            <button class="geo-btn geo-toggle" onclick="document.getElementById('geo-side').classList.toggle('ferme')" aria-label="Afficher ou masquer la liste">☰</button></div>
          <div class="f">
            <label>Rechercher<input class="geo-in" id="geo-q" type="search" placeholder="Nom, code, commune…" oninput="GEO.chercher(this.value)"></label>
            <label>Département<select class="geo-in" id="geo-dep" onchange="GEO.filtre('departement', this.value)"></select></label>
            <label>Commune<select class="geo-in" id="geo-com" onchange="GEO.filtre('commune', this.value)"></select></label>
            <label>Statut<select class="geo-in" id="geo-statut" onchange="GEO.filtre('statut', this.value)">
              <option value="">Toutes</option><option value="active">Actives</option><option value="inactive">Inactives (retirées)</option></select></label>
            <label>Position GPS<select class="geo-in" id="geo-gps" onchange="GEO.filtre('gps', this.value)">
              <option value="">Toutes</option><option value="verifie">Vérifiée</option><option value="non_verifie">Non vérifiée</option><option value="absent">Sans position</option></select></label>
            <button class="geo-btn" onclick="GEO.reinitialiser()">Effacer les filtres</button>
          </div>
          <div class="geo-liste" id="geo-liste" role="list"></div>
        </aside>
        <div class="geo-carte" id="geo-carte-box">
          <div id="geo-map" role="application" aria-label="Carte d'Haïti avec les stations"></div>
          <div class="geo-ctrl">
            <button class="geo-btn" onclick="GEO.voirHaiti()" title="Revenir à la vue d'Haïti">🇭🇹 Haïti</button>
            <button class="geo-btn" onclick="GEO.ajuster()" title="Afficher toutes les stations">⤢ Mes stations</button>
            <button class="geo-btn" onclick="GEO.pleinEcran()" title="Plein écran">⛶ Plein écran</button>
          </div>
        </div>
        <aside class="geo-panel" aria-live="polite"><div class="corps" id="geo-panel"></div></aside>
      </div></div>`;
    page.dataset.pret = '1';
  }
  try{ await GEO.leaflet(); }
  catch(e){ document.getElementById('geo-map').innerHTML = `<div class="geo-err" style="margin:20px">${esc(e.message)}</div>`; return; }
  if(!GEO.carte){
    const f = fonds();
    GEO.carte = L.map('geo-map', {zoomControl:true, maxBounds:[[16.5, -76], [21.5, -70]], minZoom:7}).fitBounds(HAITI);
    f['Plan (OpenStreetMap)'].addTo(GEO.carte);
    L.control.layers(f, null, {position:'bottomleft'}).addTo(GEO.carte);
    L.control.scale({imperial:false}).addTo(GEO.carte);
    GEO.groupe = L.markerClusterGroup({showCoverageOnHover:false, maxClusterRadius:45}).addTo(GEO.carte);
    GEO.carte.on('click', e => { if(GEO.mode) GEO.placer(e.latlng.lat, e.latlng.lng, 'carte'); });
  }
  setTimeout(() => GEO.carte.invalidateSize(), 50);
  await GEO.charger(true);
};

GEO.charger = async function(ajuster){
  try{
    const [o, s, d] = await Promise.all([api('GET', '/overview'), api('GET', '/stations?' + qs(GEO.f)), GEO.dep ? GEO.dep : api('GET', '/departments')]);
    GEO.vue = o; GEO.stations = s.stations; GEO.dep = d;
    kpis(o); listes(); marqueurs();
    if(ajuster) GEO.ajuster();
    if(GEO.sel != null && GEO.stations.some(x => x.id === GEO.sel)) GEO.choisir(GEO.sel, false); else if(GEO.sel == null) apercu();
  }catch(e){ document.getElementById('geo-panel').innerHTML = `<div class="geo-err">${esc(e.message)} <button class="geo-btn" onclick="GEO.charger(true)">Réessayer</button></div>`; }
};

function kpis(o){
  const k = (t, v, c) => `<div class="geo-kpi"><div class="t">${t}</div><div class="v">${v}</div><div class="c">${c}</div></div>`;
  document.getElementById('geo-kpis').innerHTML =
    k('Stations', o.total, o.proprietaire ? 'Propriétaire : ' + esc(o.proprietaire) : 'Votre station') +
    k('Actives', o.actives, 'en service') +
    k('Inactives', o.inactives, 'retirées') +
    k('Départements', o.departements_couverts, 'couverts sur 10') +
    k('GPS vérifié', o.gps_verifies, `${o.gps_non_verifies} non vérifiée(s) · ${o.sans_gps} sans position`);
}
function listes(){
  const d = GEO.dep, dep = document.getElementById('geo-dep'), com = document.getElementById('geo-com');
  dep.innerHTML = '<option value="">Tous</option>' + d.departements.map(x => `<option ${GEO.f.departement === x ? 'selected' : ''}>${esc(x)}</option>`).join('');
  const communes = GEO.f.departement ? (d.communes[GEO.f.departement] || []) : [...new Set(Object.values(d.communes).flat())].sort();
  com.innerHTML = '<option value="">Toutes</option>' + communes.map(x => `<option ${GEO.f.commune === x ? 'selected' : ''}>${esc(x)}</option>`).join('');
  document.getElementById('geo-nb').textContent = `(${GEO.stations.length})`;
  document.getElementById('geo-liste').innerHTML = GEO.stations.length ? GEO.stations.map(s => `<button class="geo-item ${GEO.sel === s.id ? 'on' : ''}" role="listitem" onclick="GEO.choisir(${s.id}, true)">
      <span class="geo-pt" style="background:${s.statut === 'inactive' ? 'var(--red2)' : COUL[s.verification]}"></span>
      <span><b>${esc(s.nom)}${s.ici ? ' · <span class="geo-note">vous êtes ici</span>' : ''}</b>
      <small>${esc([s.code, s.commune, s.departement].filter(Boolean).join(' · ') || 'Informations à compléter')}</small>
      <small>${LIB_GPS[s.verification]}${s.statut === 'inactive' ? ' · inactive' : ''}</small></span></button>`).join('')
    : '<div class="geo-note" style="padding:12px">Aucune station ne correspond aux filtres.</div>';
}
function marqueurs(){
  GEO.groupe.clearLayers(); GEO.marqueurs = {};
  for(const s of GEO.stations){
    if(s.latitude == null) continue;
    const m = L.marker([s.latitude, s.longitude], {icon:icone(s.statut === 'inactive' ? '#ef4444' : COUL[s.verification], GEO.sel === s.id), title:s.nom, keyboard:true});
    m.bindTooltip(`${esc(s.nom)}${s.commune ? ' — ' + esc(s.commune) : ''}`);
    m.on('click', () => GEO.choisir(s.id, false));
    GEO.groupe.addLayer(m); GEO.marqueurs[s.id] = m;
  }
}
GEO.ajuster = function(){
  const pts = GEO.stations.filter(s => s.latitude != null).map(s => [s.latitude, s.longitude]);
  if(!pts.length) return GEO.voirHaiti();
  GEO.carte.fitBounds(L.latLngBounds(pts).pad(0.25), {maxZoom:14});
};
GEO.voirHaiti = () => GEO.carte.fitBounds(HAITI);
GEO.pleinEcran = function(){
  const b = document.getElementById('geo-carte-box');
  if(document.fullscreenElement) document.exitFullscreen(); else b.requestFullscreen?.().catch(() => notify('Plein écran non disponible sur cet appareil.', 'err'));
  setTimeout(() => GEO.carte.invalidateSize(), 300);
};
let _tq = null;
GEO.chercher = v => { clearTimeout(_tq); _tq = setTimeout(() => { GEO.f.q = v.trim(); GEO.charger(true); }, 250); };
GEO.filtre = (k, v) => { GEO.f[k] = v; if(k === 'departement') GEO.f.commune = ''; GEO.charger(true); };
GEO.reinitialiser = () => { GEO.f = {q:'', departement:'', commune:'', statut:'', gps:''}; document.getElementById('geo-q').value = '';
  ['geo-statut', 'geo-gps'].forEach(i => document.getElementById(i).value = ''); GEO.charger(true); };

function apercu(){
  const o = GEO.vue, sans = GEO.stations.filter(s => s.latitude == null);
  document.getElementById('geo-panel').innerHTML = `<h3>Votre réseau</h3>
    <p class="geo-note">Cliquez sur une station (carte ou liste) pour voir sa fiche technique et ouvrir son tableau de bord.</p>
    <dl class="geo-dl"><dt>Stations</dt><dd>${o.total} (${o.actives} active(s))</dd><dt>Départements</dt><dd>${o.departements_couverts} sur 10</dd>
      <dt>GPS vérifié</dt><dd>${o.gps_verifies}</dd><dt>À localiser</dt><dd>${o.sans_gps}</dd></dl>
    ${sans.length ? `<h4>À localiser</h4>${sans.map(s => `<button class="geo-btn" onclick="GEO.choisir(${s.id}, true);GEO.editerPosition()">📍 ${esc(s.nom)}</button>`).join('')}` : ''}
    <h4>Légende</h4><div class="geo-note">
      <span class="geo-badge ok">● vérifiée</span> <span class="geo-badge attention">● non vérifiée</span> <span class="geo-badge off">● inactive</span></div>
    <p class="geo-note">Fond de carte © contributeurs OpenStreetMap. Une nouvelle station se localise lors de son enregistrement (Saisie › Gérer › Enregistrer une station).</p>`;
}

// ── Fiche technique ─────────────────────────────────────────────────
GEO.choisir = async function(id, centrer){
  if(GEO._pin){ GEO.carte.removeLayer(GEO._pin); GEO._pin = null; }
  GEO.sel = id; GEO.mode = null; GEO.edition = null;
  listes(); marqueurs();
  const s = GEO.stations.find(x => x.id === id);
  if(centrer && s && s.latitude != null) GEO.carte.setView([s.latitude, s.longitude], Math.max(GEO.carte.getZoom(), 14));
  const p = document.getElementById('geo-panel');
  p.innerHTML = '<div class="ic-sq" style="height:240px;border-radius:8px;background:var(--panel2)"></div>';
  try{ GEO.detail = await api('GET', '/stations/' + id); fiche(); }
  catch(e){ p.innerHTML = `<div class="geo-err">${esc(e.message)}</div>`; }
};
function fiche(){
  const d = GEO.detail, f = d.fiche, a = d.activite, p = document.getElementById('geo-panel');
  const gpsBadge = {verifie:'<span class="geo-badge ok">✓ Position vérifiée</span>', non_verifie:'<span class="geo-badge attention">Position non vérifiée</span>',
    absent:'<span class="geo-badge neutre">Sans position GPS</span>'}[d.verification];
  p.innerHTML = `<div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
      <div><h3>${esc(d.nom)}</h3><div class="geo-note">${esc(f.code || 'Sans code')} · ${d.statut === 'active' ? '<span class="geo-badge ok">Active</span>' : '<span class="geo-badge off">Inactive</span>'} ${gpsBadge}</div></div>
      <button class="geo-btn" onclick="GEO.annulerPosition();GEO.sel=null;GEO.charger(false)" aria-label="Fermer la fiche">✕</button></div>
    <div class="geo-actions">
      ${d.statut === 'active' ? `<button class="geo-btn prim" onclick="GEO.ouvrirTableau(${d.id})">📊 Tableau de bord</button>` : ''}
      <button class="geo-btn" onclick="GEO.section('details')">🔎 Détails techniques</button>
      <button class="geo-btn" onclick="GEO.section('infos')">✏️ Informations</button>
      <button class="geo-btn" onclick="GEO.editerPosition()">📍 Position GPS</button>
      ${d.latitude != null ? `<a class="geo-btn" target="_blank" rel="noopener" href="https://www.google.com/maps/dir/?api=1&destination=${d.latitude},${d.longitude}">🧭 Itinéraire</a>` : ''}
      <button class="geo-btn" onclick="GEO.section('journal')">🕘 Historique</button>
    </div>
    <h4>Identification</h4><dl class="geo-dl"><dt>Propriétaire</dt><dd>${esc(d.proprietaire || '—')}</dd>
      <dt>Enregistrée le</dt><dd>${d.enregistree_le ? new Date(d.enregistree_le).toLocaleDateString('fr-FR') : (d.id === 0 ? 'Station d’origine' : '—')}</dd></dl>
    <h4>Localisation</h4><dl class="geo-dl"><dt>Pays</dt><dd>${esc(f.pays)}</dd><dt>Département</dt><dd>${esc(f.departement || '—')}</dd>
      <dt>Commune</dt><dd>${esc(f.commune || '—')}</dd><dt>Adresse</dt><dd>${esc(d.adresse || '—')}</dd>
      <dt>Latitude</dt><dd>${n6(f.latitude)}</dd><dt>Longitude</dt><dd>${n6(f.longitude)}</dd>
      <dt>Précision</dt><dd>${f.precision_m != null ? '± ' + Number(f.precision_m).toLocaleString('fr-FR') + ' m' : '—'}</dd></dl>
    <h4>Activité (30 derniers jours, relevés validés)</h4>
    ${a ? `<dl class="geo-dl"><dt>Ventes</dt><dd>${fmtG(a.ventes)}</dd><dt>Relevés</dt><dd>${a.nb_releves} · ${a.jours_actifs} jour(s) actif(s)</dd>
      <dt>Gallons</dt><dd>${Object.entries(a.gallons_par_produit).map(([n, g]) => esc(n) + ' ' + fmtGal(g)).join('<br>') || '—'}</dd>
      <dt>Dernier relevé</dt><dd>${a.dernier_releve ? frDate(a.dernier_releve) : 'aucun'}</dd>
      <dt>État</dt><dd>${!a.dernier_releve ? 'Aucune activité enregistrée' : (a.jours_actifs ? 'En activité' : 'Pas d’activité depuis 30 jours')}</dd></dl>`
      : '<div class="geo-note">Station inactive : pas de chiffres.</div>'}
    <div id="geo-section"></div>`;
  if(GEO.edition) GEO.section(GEO.edition);
}
GEO.apercu = apercu;

GEO.section = function(quoi){
  GEO.edition = quoi;
  const d = GEO.detail, f = d.fiche, z = document.getElementById('geo-section');
  if(quoi === 'details'){
    z.innerHTML = `<h4>Détails techniques</h4><dl class="geo-dl"><dt>Identifiant</dt><dd>${d.id}</dd><dt>Source</dt><dd>${{gps:'GPS de l’appareil', carte:'Placée sur la carte'}[f.source] || '—'}</dd>
      <dt>Relevée par</dt><dd>${esc(f.capture_par || '—')}</dd><dt>Relevée le</dt><dd>${f.capture_le ? new Date(f.capture_le).toLocaleString('fr-FR') : '—'}</dd>
      <dt>Système</dt><dd>WGS84 (EPSG:4326)</dd></dl>`;
  }else if(quoi === 'journal'){
    const lib = {position:'Position', informations:'Informations'};
    z.innerHTML = `<h4>Historique des modifications</h4>${d.journal.length ? `<div class="geo-journal">${d.journal.map(j => `<div><b>${lib[j.action] || esc(j.action)}</b> · ${new Date(j.date).toLocaleString('fr-FR')} · ${esc(j.utilisateur || '')}
      <br>${Object.keys(j.nouvelles || {}).filter(k => JSON.stringify((j.anciennes || {})[k]) !== JSON.stringify(j.nouvelles[k])).map(k => `${esc(k)} : ${esc(String((j.anciennes || {})[k] ?? '—'))} → ${esc(String(j.nouvelles[k] ?? '—'))}`).join('<br>')}</div>`).join('')}</div>`
      : '<div class="geo-note">Aucune modification enregistrée.</div>'}`;
  }else if(quoi === 'infos'){
    z.innerHTML = `<h4>Modifier les informations</h4><div class="geo-form">
      <label>Code de la station<input class="geo-in" id="gi-code" maxlength="30" value="${esc(f.code || '')}" placeholder="ex. CPV-01"></label>
      <label>Département<select class="geo-in" id="gi-dep"><option value="">—</option>${GEO.dep.departements.map(x => `<option ${f.departement === x ? 'selected' : ''}>${esc(x)}</option>`).join('')}</select></label>
      <label>Commune<input class="geo-in" id="gi-com" maxlength="100" value="${esc(f.commune || '')}"></label>
      <label>Adresse<input class="geo-in" id="gi-adr" maxlength="255" value="${esc(f.adresse || d.adresse || '')}"></label>
      <div id="gi-msg"></div><div class="ligne"><button class="geo-btn prim" onclick="GEO.enregistrerInfos()">Enregistrer</button>
      <button class="geo-btn" onclick="GEO.edition=null;GEO.fiche()">Annuler</button></div></div>`;
  }
  z.scrollIntoView({block:'nearest'});
};
GEO.fiche = fiche;
GEO.enregistrerInfos = async function(){
  const v = id => document.getElementById(id).value.trim();
  try{
    GEO.detail = await api('PUT', `/stations/${GEO.sel}/infos`, {code:v('gi-code') || null, departement:v('gi-dep') || null, commune:v('gi-com') || null, adresse:v('gi-adr') || null});
    GEO.edition = null; GEO.dep = null; notify('Informations enregistrées.', 'ok'); await GEO.charger(false);
  }catch(e){ document.getElementById('gi-msg').innerHTML = `<div class="geo-err">${esc(e.message)}</div>`; }
};

// ── Position : GPS de l'appareil ou placement sur la carte ──────────
GEO.editerPosition = function(){
  GEO.mode = {lat:GEO.detail?.latitude ?? null, lon:GEO.detail?.longitude ?? null, precision:null, source:'carte'};
  GEO.edition = 'position';
  const z = document.getElementById('geo-section');
  z.innerHTML = `<h4>Position de la station</h4><div class="geo-form">
    <div class="geo-note">Touchez la carte pour placer la station, puis faites glisser le marqueur pour l’ajuster. Ou utilisez le GPS de cet appareil (sur place).</div>
    <div class="ligne"><button class="geo-btn prim" onclick="GEO.gps()">📡 Ma position GPS actuelle</button></div>
    <label>Chercher un lieu (ville, quartier, route)<input class="geo-in" id="gp-q" placeholder="ex. Limbé, Champin…" onkeydown="if(event.key==='Enter')GEO.rechercher()"></label>
    <div class="ligne"><button class="geo-btn" onclick="GEO.rechercher()">Chercher</button></div><div id="gp-res"></div>
    <div class="ligne"><label style="flex:1">Latitude<input class="geo-in" id="gp-lat" inputmode="decimal" value="${GEO.mode.lat ?? ''}" onchange="GEO.saisie()"></label>
      <label style="flex:1">Longitude<input class="geo-in" id="gp-lon" inputmode="decimal" value="${GEO.mode.lon ?? ''}" onchange="GEO.saisie()"></label></div>
    <div class="geo-note" id="gp-prec"></div>
    <label style="flex-direction:row;align-items:center;gap:8px;color:var(--text)"><input type="checkbox" id="gp-ok"> Position vérifiée (je confirme que c’est l’emplacement exact)</label>
    <div id="gp-msg"></div>
    <div class="ligne"><button class="geo-btn prim" onclick="GEO.enregistrerPosition()">Enregistrer la position</button>
      <button class="geo-btn" onclick="GEO.annulerPosition()">Annuler</button></div></div>`;
  if(GEO.mode.lat != null) GEO.placer(GEO.mode.lat, GEO.mode.lon, GEO.detail.fiche.source || 'carte', true);
  else GEO.carte.getContainer().style.cursor = 'crosshair';
  z.scrollIntoView({block:'nearest'});
};
GEO.annulerPosition = function(){
  if(GEO._pin){ GEO.carte.removeLayer(GEO._pin); GEO._pin = null; }
  GEO.mode = null; GEO.edition = null; GEO.carte.getContainer().style.cursor = '';
  if(GEO.sel != null && GEO.detail) fiche();
};
GEO.placer = function(lat, lon, source, sansCentrer){
  if(!GEO.mode) return;
  Object.assign(GEO.mode, {lat:+(+lat).toFixed(6), lon:+(+lon).toFixed(6), source});
  if(source === 'carte') GEO.mode.precision = null;
  if(!GEO._pin){
    GEO._pin = L.marker([lat, lon], {draggable:true, icon:icone('#3b82f6', true), zIndexOffset:1000}).addTo(GEO.carte);
    GEO._pin.on('dragend', e => { const p = e.target.getLatLng(); GEO.placer(p.lat, p.lng, 'carte', true); });
  }else GEO._pin.setLatLng([lat, lon]);
  if(!sansCentrer) GEO.carte.setView([lat, lon], Math.max(GEO.carte.getZoom(), 16));
  const a = document.getElementById('gp-lat'), b = document.getElementById('gp-lon');
  if(a){ a.value = GEO.mode.lat; b.value = GEO.mode.lon; }
  const pr = document.getElementById('gp-prec');
  if(pr) pr.textContent = source === 'gps' ? `Source : GPS de l’appareil${GEO.mode.precision != null ? ` · précision ± ${Math.round(GEO.mode.precision)} m` : ''}` : 'Source : placée sur la carte';
};
GEO.saisie = function(){
  const lat = parseFloat(String(document.getElementById('gp-lat').value).replace(',', '.')), lon = parseFloat(String(document.getElementById('gp-lon').value).replace(',', '.'));
  if(isFinite(lat) && isFinite(lon)) GEO.placer(lat, lon, 'carte');
};
GEO.gps = function(){
  const msg = m => { const z = document.getElementById('gp-msg'); if(z) z.innerHTML = `<div class="geo-err">${m}</div>`; };
  if(!window.isSecureContext) return msg('Le GPS du navigateur demande une connexion sécurisée (https). Placez la station sur la carte.');
  if(!navigator.geolocation) return msg('Cet appareil ne fournit pas de GPS au navigateur. Placez la station sur la carte.');
  document.getElementById('gp-msg').innerHTML = '<div class="geo-note">Recherche de la position…</div>';
  navigator.geolocation.getCurrentPosition(p => {
    GEO.mode.precision = p.coords.accuracy;
    GEO.placer(p.coords.latitude, p.coords.longitude, 'gps');
    document.getElementById('gp-msg').innerHTML = p.coords.accuracy > 100
      ? `<div class="geo-err">Position imprécise (± ${Math.round(p.coords.accuracy)} m). Déplacez le marqueur si besoin, ou réessayez à l’extérieur.</div>` : '';
  }, err => msg({1:'Vous avez refusé l’accès à la position. Autorisez-la dans le navigateur, ou placez la station sur la carte.',
                2:'Position introuvable (GPS indisponible). Placez la station sur la carte.', 3:'Le GPS a mis trop de temps à répondre. Réessayez ou placez la station sur la carte.'}[err.code] || err.message),
     {enableHighAccuracy:true, timeout:20000, maximumAge:0});
};
GEO.rechercher = async function(){
  const q = document.getElementById('gp-q').value.trim(), z = document.getElementById('gp-res');
  if(q.length < 2) return;
  z.innerHTML = '<div class="geo-note">Recherche…</div>';
  try{
    const r = await api('GET', '/recherche?' + qs({q}));
    z.innerHTML = r.resultats.length ? `<div class="geo-res">${r.resultats.map(x => `<button onclick="GEO.carte.setView([${x.latitude},${x.longitude}],15)">${esc(x.nom)}</button>`).join('')}</div><div class="geo-note">${esc(r.attribution)}</div>`
      : '<div class="geo-note">Aucun lieu trouvé en Haïti.</div>';
  }catch(e){ z.innerHTML = `<div class="geo-err">${esc(e.message)}</div>`; }
};
GEO.enregistrerPosition = async function(extra){
  const m = GEO.mode, z = document.getElementById('gp-msg');
  if(!m || m.lat == null) return z.innerHTML = '<div class="geo-err">Placez d’abord la station : touchez la carte ou utilisez le GPS.</div>';
  const corps = Object.assign({latitude:m.lat, longitude:m.lon, precision_m:m.precision, source:m.source, verifie:document.getElementById('gp-ok').checked}, extra || {});
  try{
    GEO.detail = await api('POST', `/stations/${GEO.sel}/location`, corps);
    if(GEO._pin){ GEO.carte.removeLayer(GEO._pin); GEO._pin = null; }
    GEO.mode = null; GEO.edition = null; GEO.carte.getContainer().style.cursor = '';
    notify('Position enregistrée.', 'ok'); await GEO.charger(false);
  }catch(e){
    const code = e.message.includes('position vérifiée') ? 'confirmer_remplacement' : (e.message.includes('Une autre station') ? 'forcer' : null);
    if(code && await showConfirm(e.message.replace(/^\[\d+\]\s*/, ''), {title:'Confirmer la position', type:'warn', okLabel:'Oui, enregistrer'}))
      return GEO.enregistrerPosition(Object.assign({}, extra, {[code]:true}));
    z.innerHTML = `<div class="geo-err">${esc(e.message)}</div>`;
  }
};

// ── Tableau de bord de la station ──────────────────────────────────
GEO.ouvrirTableau = function(id){
  const s = GEO.stations.find(x => x.id === id) || GEO.detail;
  if(s && s.ici) return goTo('dashboard');
  // _STATIONS est déclaré par « let » dans index.html : visible par son nom, pas sur window
  const st = typeof _STATIONS !== 'undefined' ? _STATIONS : null;
  if(typeof ouvrirChoixStation !== 'function' || !st || !st.peut_changer)
    return notify('Changement de station réservé au propriétaire.', 'err');
  window._ST_PAGE_APRES = 'dashboard';          // après le PIN : tableau de bord de cette station
  ouvrirChoixStation(); choisirStation(id, false);
};

// ── Sélecteur de position réutilisable (enregistrement d'une station) ──
GEO.selecteur = async function(boite, init, change){
  await GEO.leaflet();
  const c = L.map(boite, {maxBounds:[[16.5, -76], [21.5, -70]], minZoom:7}).fitBounds(HAITI);
  fonds()['Plan (OpenStreetMap)'].addTo(c);
  let pin = null;
  const poser = (lat, lon, src, prec) => {
    lat = +(+lat).toFixed(6); lon = +(+lon).toFixed(6);
    if(!pin){ pin = L.marker([lat, lon], {draggable:true, icon:icone('#3b82f6', true)}).addTo(c);
      pin.on('dragend', e => { const p = e.target.getLatLng(); poser(p.lat, p.lng, 'carte'); }); }
    else pin.setLatLng([lat, lon]);
    change({latitude:lat, longitude:lon, source:src, precision_m:src === 'gps' ? prec : null});
  };
  c.on('click', e => poser(e.latlng.lat, e.latlng.lng, 'carte'));
  if(init && init.latitude != null){ poser(init.latitude, init.longitude, init.source || 'carte', init.precision_m); c.setView([init.latitude, init.longitude], 16); }
  setTimeout(() => c.invalidateSize(), 80);
  return {carte:c, poser:(lat, lon, src, prec) => { poser(lat, lon, src, prec); c.setView([lat, lon], 16); }};
};
})();
