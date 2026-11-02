/* Centre des stations (Geospatial Station Intelligence Center) — fenêtre
   plein écran ouverte après la connexion du propriétaire et par « Changer
   de station ». Données : /api/geospatial/* (backend/geospatial.py).
   Carte : Leaflet + regroupement des marqueurs (/vendor/leaflet), fond
   OpenStreetMap. Utilise les aides globales GET, POST, PUT, esc, notify,
   goTo, showConfirm, fmtG, fmtGal, frDate. */
(function(){
'use strict';
const GEO = window.GEO = {stations:[], vue:null, sel:null, detail:null, carte:null, groupe:null, marqueurs:{},
  f:{q:'', departement:'', commune:'', statut:'', gps:''}, dep:null, mode:null, edition:null, fond:'sombre', _pin:null};
const HAITI = [[17.95, -74.55], [20.15, -71.62]];
const COUL = {verifie:'#22c55e', non_verifie:'#f59e0b', absent:'#94a3b8'};
const LIB_GPS = {verifie:'Position vérifiée', non_verifie:'Position non vérifiée', absent:'Sans position GPS'};
const OSM = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
const TUILES = {osm:['https://tile.openstreetmap.org/{z}/{x}/{y}.png', OSM],
  hot:['https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png', OSM + ', tuiles HOT par OSM France']};
const DOUX = !matchMedia('(prefers-reduced-motion: reduce)').matches;
const api = (m, u, b) => m === 'GET' ? GET('/geospatial' + u) : (m === 'PUT' ? PUT('/geospatial' + u, b) : POST('/geospatial' + u, b));
const n6 = v => v == null ? '—' : Number(v).toFixed(6);
const qs = o => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== '' && v != null)).toString();
const $ = id => document.getElementById(id);
const sansCode = m => String(m || '').replace(/^\[\d+\]\s*/, '');

// ── Leaflet (chargé à la première ouverture) ────────────────────────
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
function icone(s, choisi){
  const c = s.statut === 'inactive' ? '#ef4444' : COUL[s.verification];
  return L.divIcon({className:'', iconSize:[30, 30], iconAnchor:[15, 30], popupAnchor:[0, -28],
    html:`<div class="gm-mk ${choisi ? 'sel' : ''} ${s.ici ? 'ici' : ''}"><span class="halo"></span><span class="pin" style="background:${c}"></span></div>`});
}

// ── Ouverture / fermeture de la fenêtre ─────────────────────────────
GEO.ouvrir = async function(){
  let m = $('geo-modal');
  if(!m){
    m = document.createElement('div');
    m.id = 'geo-modal'; m.className = 'sombre'; m.hidden = true;
    m.setAttribute('role', 'dialog'); m.setAttribute('aria-modal', 'true'); m.setAttribute('aria-label', 'Centre des stations');
    m.innerHTML = `<div id="geo-map" role="application" aria-label="Carte d’Haïti avec vos stations"></div>
      <header class="gm-top gm-verre">
        <div class="gm-marque"><div class="gm-logo" aria-hidden="true">⛽</div>
          <div class="gm-titre"><b>Centre des stations</b><span id="gm-proprio">KONEKTA · Haïti</span></div></div>
        <div class="gm-kpis" id="geo-kpis" aria-live="polite"></div>
        <div class="gm-actions">
          <div class="gm-fonds" role="group" aria-label="Fond de carte">
            <button data-f="sombre" class="on" onclick="GEO.changerFond('sombre')">Nuit</button>
            <button data-f="osm" onclick="GEO.changerFond('osm')">Plan</button>
            <button data-f="hot" onclick="GEO.changerFond('hot')">Humanitaire</button></div>
          <button class="gm-btn" onclick="GEO.voirHaiti()" title="Toute l’île">🗺️ <span class="txt">Haïti</span></button>
          <button class="gm-btn" onclick="GEO.ajuster()" title="Toutes mes stations">◎ <span class="txt">Mes stations</span></button>
          <button class="gm-btn prim" onclick="GEO.fermer()" title="Fermer (Échap)">✕ <span class="txt">Fermer</span></button>
        </div>
      </header>
      <aside class="gm-side gm-verre" id="geo-side" aria-label="Stations">
        <div class="tete">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <b style="font-size:15px">Mes stations <span id="geo-nb" class="gm-note"></span></b>
            <button class="gm-btn gm-replier" style="height:30px" onclick="document.getElementById('geo-side').classList.toggle('replie')" aria-label="Replier la liste">⌄</button></div>
          <div class="gm-rech"><input id="geo-q" type="search" placeholder="Nom, code, commune…" aria-label="Rechercher une station" oninput="GEO.chercher(this.value)"></div>
          <div class="gm-filtres">
            <select id="geo-dep" aria-label="Département" onchange="GEO.filtre('departement', this.value)"></select>
            <select id="geo-com" aria-label="Commune" onchange="GEO.filtre('commune', this.value)"></select></div>
          <div class="gm-puces" role="group" aria-label="Filtres rapides">
            <button class="gm-puce" data-k="statut" data-v="active" onclick="GEO.puce(this)">Actives</button>
            <button class="gm-puce" data-k="statut" data-v="inactive" onclick="GEO.puce(this)">Inactives</button>
            <button class="gm-puce" data-k="gps" data-v="verifie" onclick="GEO.puce(this)">GPS vérifié</button>
            <button class="gm-puce" data-k="gps" data-v="absent" onclick="GEO.puce(this)">À localiser</button>
            <button class="gm-puce" onclick="GEO.reinitialiser()">Tout</button></div>
        </div>
        <div class="gm-liste" id="geo-liste" role="list"></div>
        <div class="gm-pied"><span>© OpenStreetMap</span><span id="gm-heure"></span></div>
      </aside>
      <aside class="gm-panel gm-verre" id="geo-panel-box" aria-live="polite"><div class="corps" id="geo-panel"></div></aside>
      <div class="gm-legende gm-verre">
        <span><i style="background:#22c55e"></i>Vérifiée</span><span><i style="background:#f59e0b"></i>Non vérifiée</span>
        <span><i style="background:#ef4444"></i>Inactive</span><span><i style="background:#facc15"></i>Vous êtes ici</span></div>`;
    document.body.appendChild(m);
    document.addEventListener('keydown', e => {
      if(e.key !== 'Escape' || $('geo-modal').hidden) return;
      if(document.querySelector('#gi-confirm-overlay.show, .cs-overlay.show')) return;
      if(GEO.sel != null) GEO.fermerFiche(); else GEO.fermer();
    });
  }
  m.hidden = false;
  document.body.style.overflow = 'hidden';
  if(!GEO.stations.length){                      // emplacements animés pendant le chargement
    $('geo-liste').innerHTML = '<div class="gm-sq" style="height:68px;margin:6px 4px"></div>'.repeat(4);
    $('geo-kpis').innerHTML = '<div class="gm-kpi gm-sq" style="height:44px"></div>'.repeat(4);
  }
  requestAnimationFrame(() => requestAnimationFrame(() => m.classList.add('ouvert')));
  try{ await GEO.leaflet(); }
  catch(e){ $('geo-panel').innerHTML = `<div class="gm-err">${esc(e.message)}</div>`; $('geo-panel-box').classList.add('ouvert'); return; }
  if(!GEO.carte){
    GEO.carte = L.map('geo-map', {zoomControl:false, maxBounds:[[16.5, -76.5], [21.5, -69.5]], minZoom:7, zoomSnap:0.25}).fitBounds(HAITI);
    L.control.zoom({position:'bottomright'}).addTo(GEO.carte);
    L.control.scale({imperial:false, position:'bottomright'}).addTo(GEO.carte);
    GEO.couche = L.tileLayer(TUILES.osm[0], {maxZoom:19, attribution:TUILES.osm[1]}).addTo(GEO.carte);
    GEO.groupe = L.markerClusterGroup({showCoverageOnHover:false, maxClusterRadius:45, spiderfyOnMaxZoom:true}).addTo(GEO.carte);
    GEO.carte.on('click', e => { if(GEO.mode) GEO.placer(e.latlng.lat, e.latlng.lng, 'carte'); });
  }
  setTimeout(() => GEO.carte.invalidateSize(), 60);
  await GEO.charger(true);
};
GEO.fermer = function(){
  const m = $('geo-modal');
  if(!m || m.hidden) return;
  GEO.annulerPosition(true);
  m.classList.remove('ouvert');
  document.body.style.overflow = '';
  setTimeout(() => { m.hidden = true; }, DOUX ? 450 : 0);
};
GEO.changerFond = function(f){
  GEO.fond = f;
  document.querySelectorAll('.gm-fonds button').forEach(b => b.classList.toggle('on', b.dataset.f === f));
  $('geo-modal').classList.toggle('sombre', f === 'sombre');
  const [url, attr] = TUILES[f === 'hot' ? 'hot' : 'osm'];
  GEO.carte.removeLayer(GEO.couche);
  GEO.couche = L.tileLayer(url, {maxZoom:19, attribution:attr}).addTo(GEO.carte);
};
// Compatibilité : l'ancienne page « geo » ouvre la fenêtre
window.initGeo = () => GEO.ouvrir();

// ── Données ─────────────────────────────────────────────────────────
GEO.charger = async function(ajuster){
  try{
    const [o, s, d] = await Promise.all([api('GET', '/overview'), api('GET', '/stations?' + qs(GEO.f)), GEO.dep ? GEO.dep : api('GET', '/departments')]);
    GEO.vue = o; GEO.stations = s.stations; GEO.dep = d;
    kpis(o); listes(); marqueurs();
    $('gm-heure').textContent = 'Mis à jour ' + new Date().toLocaleTimeString('fr-FR', {hour:'2-digit', minute:'2-digit'});
    if(ajuster) setTimeout(GEO.ajuster, DOUX ? 250 : 0);
    if(GEO.sel != null && GEO.stations.some(x => x.id === GEO.sel)) GEO.choisir(GEO.sel, false);
  }catch(e){
    $('geo-panel').innerHTML = `<div class="gm-err">${esc(sansCode(e.message))}</div><button class="gm-btn" onclick="GEO.charger(true)">Réessayer</button>`;
    $('geo-panel-box').classList.add('ouvert');
  }
};
function kpis(o){
  $('gm-proprio').textContent = o.proprietaire ? `Propriétaire : ${o.proprietaire}` : 'Votre station';
  const k = (t, v, cls, i) => `<div class="gm-kpi ${cls || ''}" style="animation-delay:${0.12 + i * 0.06}s"><div class="t">${t}</div><div class="v">${v}</div></div>`;
  $('geo-kpis').innerHTML = k('Stations', o.total, '', 0) + k('Actives', o.actives, 'ok', 1) + k('Inactives', o.inactives, o.inactives ? 'off' : '', 2)
    + k('Départements', `${o.departements_couverts}<small style="font-size:12px;color:var(--gm-muted)">/10</small>`, '', 3)
    + k('GPS vérifié', o.gps_verifies, 'ok', 4) + k('À localiser', o.sans_gps, o.sans_gps ? 'warn' : '', 5);
}
function listes(){
  const d = GEO.dep, dep = $('geo-dep'), com = $('geo-com');
  dep.innerHTML = '<option value="">Tous les départements</option>' + d.departements.map(x => `<option ${GEO.f.departement === x ? 'selected' : ''}>${esc(x)}</option>`).join('');
  const communes = GEO.f.departement ? (d.communes[GEO.f.departement] || []) : [...new Set(Object.values(d.communes).flat())].sort();
  com.innerHTML = '<option value="">Toutes les communes</option>' + communes.map(x => `<option ${GEO.f.commune === x ? 'selected' : ''}>${esc(x)}</option>`).join('');
  document.querySelectorAll('.gm-puce[data-k]').forEach(b => b.classList.toggle('on', GEO.f[b.dataset.k] === b.dataset.v));
  $('geo-nb').textContent = `· ${GEO.stations.length}`;
  const peut = GEO.vue && GEO.vue.peut_tout_voir;
  $('geo-liste').innerHTML = GEO.stations.length ? GEO.stations.map((s, i) => `<button class="gm-item ${GEO.sel === s.id ? 'on' : ''}" role="listitem"
      style="animation-delay:${Math.min(i, 12) * 0.04}s" onclick="GEO.choisir(${s.id}, true)">
      <span class="gm-ico">⛽<i style="background:${s.statut === 'inactive' ? '#ef4444' : COUL[s.verification]}"></i></span>
      <span style="min-width:0"><b>${esc(s.nom)}</b>
        <small>${esc([s.code, s.commune, s.departement].filter(Boolean).join(' · ') || 'Informations à compléter')}</small>
        <small>${s.ici ? '<span style="color:#facc15">● Vous êtes ici</span> · ' : ''}${LIB_GPS[s.verification]}${s.statut === 'inactive' ? ' · inactive' : ''}</small>
        ${!s.ici && s.statut === 'active' && peut ? `<span class="gm-aller" onclick="event.stopPropagation();GEO.allerVers(${s.id})">➜ Aller à cette station</span>` : ''}</span></button>`).join('')
    : '<div class="gm-vide">Aucune station ne correspond.<br><button class="gm-btn" style="margin-top:10px" onclick="GEO.reinitialiser()">Effacer les filtres</button></div>';
}
function marqueurs(){
  GEO.groupe.clearLayers(); GEO.marqueurs = {};
  const peut = GEO.vue && GEO.vue.peut_tout_voir;
  for(const s of GEO.stations){
    if(s.latitude == null) continue;
    const m = L.marker([s.latitude, s.longitude], {icon:icone(s, GEO.sel === s.id), title:s.nom, keyboard:true, riseOnHover:true});
    m.bindTooltip(`${esc(s.nom)}${s.commune ? ' — ' + esc(s.commune) : ''}`, {direction:'top', offset:[0, -26]});
    m.bindPopup(`<b style="font-size:14px">${esc(s.nom)}</b>${s.commune ? '<br><span style="color:var(--gm-muted)">' + esc(s.commune) + '</span>' : ''}<br>`
      + (s.ici ? '<span style="color:#facc15">● Vous êtes ici</span>' : s.statut === 'active' && peut ? `<a href="#" onclick="GEO.allerVers(${s.id});return false">➜ Aller à cette station</a>` : '<i>Station retirée</i>'));
    m.on('click', () => GEO.choisir(s.id, false));
    GEO.groupe.addLayer(m); GEO.marqueurs[s.id] = m;
  }
}
const decalage = () => {                       // laisser la place aux panneaux
  const large = innerWidth > 820;
  return {paddingTopLeft:[large ? 400 : 30, large ? 130 : 170], paddingBottomRight:[large && GEO.sel != null ? 470 : 70, large ? 70 : 80]};
};
GEO.ajuster = function(){
  const pts = GEO.stations.filter(s => s.latitude != null).map(s => [s.latitude, s.longitude]);
  if(!pts.length) return GEO.voirHaiti();
  GEO.carte.flyToBounds(L.latLngBounds(pts), Object.assign({maxZoom:13, duration:DOUX ? 1.2 : 0}, decalage()));
};
GEO.voirHaiti = () => GEO.carte.flyToBounds(HAITI, Object.assign({duration:DOUX ? 1.1 : 0}, decalage()));
let _tq = null;
GEO.chercher = v => { clearTimeout(_tq); _tq = setTimeout(() => { GEO.f.q = v.trim(); GEO.charger(true); }, 250); };
GEO.filtre = (k, v) => { GEO.f[k] = v; if(k === 'departement') GEO.f.commune = ''; GEO.charger(true); };
GEO.puce = b => { const k = b.dataset.k; GEO.f[k] = GEO.f[k] === b.dataset.v ? '' : b.dataset.v; GEO.charger(true); };
GEO.reinitialiser = () => { GEO.f = {q:'', departement:'', commune:'', statut:'', gps:''}; $('geo-q').value = ''; GEO.charger(true); };

// ── Fiche de la station ─────────────────────────────────────────────
GEO.fermerFiche = function(){
  GEO.annulerPosition(true);
  const avant = GEO.sel; GEO.sel = null; GEO.detail = null;
  $('geo-panel-box').classList.remove('ouvert'); $('geo-modal').classList.remove('fiche');
  const m = GEO.marqueurs[avant], s = GEO.stations.find(x => x.id === avant);
  if(m && s) m.setIcon(icone(s, false));
  listes();
};
GEO.choisir = async function(id, voler){
  if(GEO._pin){ GEO.carte.removeLayer(GEO._pin); GEO._pin = null; }
  const avant = GEO.sel;
  GEO.sel = id; GEO.mode = null; GEO.edition = null;
  listes();
  for(const sid of [avant, id]){                 // seuls les deux marqueurs concernés changent
    const m = GEO.marqueurs[sid], st = GEO.stations.find(x => x.id === sid);
    if(m && st) m.setIcon(icone(st, sid === id));
  }
  const s = GEO.stations.find(x => x.id === id);
  if(voler && s && s.latitude != null){
    const d = decalage(), z = Math.max(GEO.carte.getZoom(), 15);
    const p = GEO.carte.project([s.latitude, s.longitude], z).add([(d.paddingBottomRight[0] - d.paddingTopLeft[0]) / 2, 0]);
    GEO.carte.flyTo(GEO.carte.unproject(p, z), z, {duration:DOUX ? 1.2 : 0});
  }
  $('geo-panel-box').classList.add('ouvert'); $('geo-modal').classList.add('fiche');
  $('geo-panel').innerHTML = '<div class="gm-sq" style="height:120px"></div><div class="gm-sq" style="height:80px"></div><div class="gm-sq" style="height:200px"></div>';
  try{ const d = await api('GET', '/stations/' + id); if(GEO.sel === id){ GEO.detail = d; fiche(); } }
  catch(e){ $('geo-panel').innerHTML = `<div class="gm-err">${esc(sansCode(e.message))}</div>`; }
};
function fiche(){
  const d = GEO.detail, f = d.fiche, a = d.activite;
  const gps = {verifie:'<span class="gm-badge ok">✓ GPS vérifié</span>', non_verifie:'<span class="gm-badge warn">GPS non vérifié</span>',
    absent:'<span class="gm-badge">Sans position</span>'}[d.verification];
  const peut = GEO.vue && GEO.vue.peut_tout_voir;
  $('geo-panel').innerHTML = `
    <div class="gm-hero"><div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start">
        <div><h3>${esc(d.nom)}</h3><div class="sous">${esc([f.code, f.commune, f.departement].filter(Boolean).join(' · ') || 'Informations à compléter')}</div></div>
        <button class="gm-btn" style="height:32px;padding:0 10px" onclick="GEO.fermerFiche()" aria-label="Fermer la fiche">✕</button></div>
      <div class="gm-badges">${d.statut === 'active' ? '<span class="gm-badge ok">● Active</span>' : '<span class="gm-badge off">● Inactive</span>'}${gps}
        ${d.ici ? '<span class="gm-badge" style="color:#facc15">Vous êtes ici</span>' : ''}</div></div>
    ${d.statut === 'active' && !d.ici && peut ? `<div><button class="gm-btn prim grand" onclick="GEO.allerVers(${d.id})">➜ Aller à cette station</button><div id="geo-aller"></div></div>`
      : (d.ici ? `<button class="gm-btn prim grand" onclick="GEO.fermer();goTo('dashboard')">📊 Ouvrir le tableau de bord</button>` : '')}
    ${a ? `<div class="gm-chiffres">
      <div class="gm-chiffre"><div class="t">Ventes · 30 jours</div><div class="v">${fmtG(a.ventes)}</div></div>
      <div class="gm-chiffre"><div class="t">Relevés validés</div><div class="v">${a.nb_releves}<small style="font-size:12px;color:var(--gm-muted)"> · ${a.jours_actifs} j</small></div></div>
      <div class="gm-chiffre" style="grid-column:1 / -1"><div class="t">Gallons vendus · par produit</div><div class="v" style="font-size:14px">${Object.entries(a.gallons_par_produit).map(([n, g]) => esc(n) + ' : ' + fmtGal(g)).join('<br>') || '—'}</div></div>
      <div class="gm-chiffre" style="grid-column:1 / -1"><div class="t">Dernier relevé</div><div class="v" style="font-size:14px">${a.dernier_releve ? frDate(a.dernier_releve) : 'aucun'} · ${!a.dernier_releve ? 'aucune activité enregistrée' : (a.jours_actifs ? 'en activité' : 'pas d’activité depuis 30 jours')}</div></div>
    </div>` : '<div class="gm-note">Station inactive : pas de chiffres.</div>'}
    <div class="gm-grille">
      <button class="gm-btn" onclick="GEO.section('infos')">✏️ Informations</button>
      <button class="gm-btn" onclick="GEO.editerPosition()">📍 Position GPS</button>
      <button class="gm-btn" onclick="GEO.section('details')">🔎 Détails</button>
      <button class="gm-btn" onclick="GEO.section('journal')">🕘 Historique</button>
      ${d.latitude != null ? `<a class="gm-btn" style="grid-column:1 / -1" target="_blank" rel="noopener" href="https://www.google.com/maps/dir/?api=1&destination=${d.latitude},${d.longitude}">🧭 Itinéraire vers la station</a>` : ''}
    </div>
    <div class="gm-bloc"><h4>Localisation</h4><dl class="gm-dl"><dt>Pays</dt><dd>${esc(f.pays)}</dd><dt>Département</dt><dd>${esc(f.departement || '—')}</dd>
      <dt>Commune</dt><dd>${esc(f.commune || '—')}</dd><dt>Adresse</dt><dd>${esc(d.adresse || '—')}</dd>
      <dt>Coordonnées</dt><dd style="font-family:var(--mono);font-size:12.5px">${n6(f.latitude)}, ${n6(f.longitude)}</dd>
      <dt>Précision</dt><dd>${f.precision_m != null ? '± ' + Number(f.precision_m).toLocaleString('fr-FR') + ' m' : '—'}</dd></dl></div>
    <div class="gm-bloc"><h4>Identification</h4><dl class="gm-dl"><dt>Propriétaire</dt><dd>${esc(d.proprietaire || '—')}</dd>
      <dt>Enregistrée le</dt><dd>${d.enregistree_le ? new Date(d.enregistree_le).toLocaleDateString('fr-FR') : (d.id === 0 ? 'Station d’origine' : '—')}</dd></dl></div>
    <div id="geo-section"></div>`;
  if(GEO.edition && GEO.edition !== 'position') GEO.section(GEO.edition);
}
GEO.fiche = fiche;
GEO.section = function(quoi){
  GEO.edition = quoi;
  const d = GEO.detail, f = d.fiche, z = $('geo-section');
  if(quoi === 'details'){
    z.innerHTML = `<div class="gm-bloc"><h4>Détails techniques</h4><dl class="gm-dl"><dt>Identifiant</dt><dd>${d.id}</dd>
      <dt>Source</dt><dd>${{gps:'GPS de l’appareil', carte:'Placée sur la carte'}[f.source] || '—'}</dd>
      <dt>Relevée par</dt><dd>${esc(f.capture_par || '—')}</dd><dt>Relevée le</dt><dd>${f.capture_le ? new Date(f.capture_le).toLocaleString('fr-FR') : '—'}</dd>
      <dt>Système</dt><dd>WGS84 (EPSG:4326)</dd></dl></div>`;
  }else if(quoi === 'journal'){
    const lib = {position:'Position', informations:'Informations'};
    z.innerHTML = `<div class="gm-bloc"><h4>Historique des modifications</h4>${d.journal.length ? `<div class="gm-journal">${d.journal.map(j => `<div><b>${lib[j.action] || esc(j.action)}</b> · ${new Date(j.date).toLocaleString('fr-FR')} · ${esc(j.utilisateur || '')}
      <br>${Object.keys(j.nouvelles || {}).filter(k => JSON.stringify((j.anciennes || {})[k]) !== JSON.stringify(j.nouvelles[k])).map(k => `${esc(k)} : ${esc(String((j.anciennes || {})[k] ?? '—'))} → ${esc(String(j.nouvelles[k] ?? '—'))}`).join('<br>')}</div>`).join('')}</div>`
      : '<div class="gm-note">Aucune modification enregistrée.</div>'}</div>`;
  }else if(quoi === 'infos'){
    z.innerHTML = `<div class="gm-bloc"><h4>Modifier les informations</h4><div class="gm-form">
      <label>Code de la station<input class="gm-in" id="gi-code" maxlength="30" value="${esc(f.code || '')}" placeholder="ex. CPV-01"></label>
      <label>Département<select class="gm-in" id="gi-dep"><option value="">—</option>${GEO.dep.departements.map(x => `<option ${f.departement === x ? 'selected' : ''}>${esc(x)}</option>`).join('')}</select></label>
      <label>Commune<input class="gm-in" id="gi-com" maxlength="100" value="${esc(f.commune || '')}"></label>
      <label>Adresse<input class="gm-in" id="gi-adr" maxlength="255" value="${esc(f.adresse || d.adresse || '')}"></label>
      <div id="gi-msg"></div><div class="gm-ligne"><button class="gm-btn prim" onclick="GEO.enregistrerInfos()">Enregistrer</button>
      <button class="gm-btn" onclick="GEO.edition=null;document.getElementById('geo-section').innerHTML=''">Annuler</button></div></div></div>`;
  }
  z.scrollIntoView({block:'nearest', behavior:DOUX ? 'smooth' : 'auto'});
};
GEO.enregistrerInfos = async function(){
  const v = id => $(id).value.trim();
  try{
    GEO.detail = await api('PUT', `/stations/${GEO.sel}/infos`, {code:v('gi-code') || null, departement:v('gi-dep') || null, commune:v('gi-com') || null, adresse:v('gi-adr') || null});
    GEO.edition = null; GEO.dep = null; notify('Informations enregistrées.', 'ok'); await GEO.charger(false);
  }catch(e){ $('gi-msg').innerHTML = `<div class="gm-err">${esc(sansCode(e.message))}</div>`; }
};

// ── Position : GPS de l'appareil ou placement sur la carte ──────────
GEO.editerPosition = function(){
  GEO.mode = {lat:GEO.detail?.latitude ?? null, lon:GEO.detail?.longitude ?? null, precision:null, source:'carte'};
  GEO.edition = 'position';
  const z = $('geo-section');
  z.innerHTML = `<div class="gm-bloc"><h4>Position de la station</h4><div class="gm-form">
    <div class="gm-note">Touchez la carte pour placer la station, puis faites glisser le marqueur. Sur place, utilisez le GPS de l’appareil.</div>
    <button class="gm-btn prim" onclick="GEO.gps()">📡 Ma position GPS actuelle</button>
    <label>Chercher un lieu (ville, quartier, route)<input class="gm-in" id="gp-q" placeholder="ex. Limbé, Champin…" onkeydown="if(event.key==='Enter')GEO.rechercher()"></label>
    <button class="gm-btn" onclick="GEO.rechercher()">Chercher</button><div id="gp-res"></div>
    <div class="gm-grille"><label>Latitude<input class="gm-in" id="gp-lat" inputmode="decimal" value="${GEO.mode.lat ?? ''}" onchange="GEO.saisie()"></label>
      <label>Longitude<input class="gm-in" id="gp-lon" inputmode="decimal" value="${GEO.mode.lon ?? ''}" onchange="GEO.saisie()"></label></div>
    <div class="gm-note" id="gp-prec"></div>
    <label style="flex-direction:row;align-items:center;gap:8px;color:var(--gm-text)"><input type="checkbox" id="gp-ok"> Position vérifiée (emplacement exact)</label>
    <div id="gp-msg"></div>
    <div class="gm-ligne"><button class="gm-btn prim" onclick="GEO.enregistrerPosition()">Enregistrer la position</button>
      <button class="gm-btn" onclick="GEO.annulerPosition()">Annuler</button></div></div></div>`;
  if(GEO.mode.lat != null) GEO.placer(GEO.mode.lat, GEO.mode.lon, GEO.detail.fiche.source || 'carte', true);
  GEO.carte.getContainer().style.cursor = 'crosshair';
  z.scrollIntoView({block:'nearest', behavior:DOUX ? 'smooth' : 'auto'});
};
GEO.annulerPosition = function(silencieux){
  if(GEO._pin && GEO.carte){ GEO.carte.removeLayer(GEO._pin); GEO._pin = null; }
  GEO.mode = null; if(GEO.edition === 'position') GEO.edition = null;
  if(GEO.carte) GEO.carte.getContainer().style.cursor = '';
  if(!silencieux && $('geo-section')) $('geo-section').innerHTML = '';
};
GEO.placer = function(lat, lon, source, sansVol){
  if(!GEO.mode) return;
  Object.assign(GEO.mode, {lat:+(+lat).toFixed(6), lon:+(+lon).toFixed(6), source});
  if(source === 'carte') GEO.mode.precision = null;
  if(!GEO._pin){
    GEO._pin = L.marker([lat, lon], {draggable:true, zIndexOffset:1000,
      icon:icone({statut:'active', verification:'verifie', ici:false}, true)}).addTo(GEO.carte);
    GEO._pin.on('dragend', e => { const p = e.target.getLatLng(); GEO.placer(p.lat, p.lng, 'carte', true); });
  }else GEO._pin.setLatLng([lat, lon]);
  if(!sansVol) GEO.carte.flyTo([lat, lon], Math.max(GEO.carte.getZoom(), 16), {duration:DOUX ? 0.9 : 0});
  if($('gp-lat')){ $('gp-lat').value = GEO.mode.lat; $('gp-lon').value = GEO.mode.lon; }
  if($('gp-prec')) $('gp-prec').textContent = source === 'gps' ? `Source : GPS de l’appareil${GEO.mode.precision != null ? ` · précision ± ${Math.round(GEO.mode.precision)} m` : ''}` : 'Source : placée sur la carte';
};
GEO.saisie = function(){
  const lat = parseFloat(String($('gp-lat').value).replace(',', '.')), lon = parseFloat(String($('gp-lon').value).replace(',', '.'));
  if(isFinite(lat) && isFinite(lon)) GEO.placer(lat, lon, 'carte');
};
GEO.gps = function(){
  const msg = m => { if($('gp-msg')) $('gp-msg').innerHTML = `<div class="gm-err">${m}</div>`; };
  if(!window.isSecureContext) return msg('Le GPS du navigateur demande une connexion sécurisée (https). Placez la station sur la carte.');
  if(!navigator.geolocation) return msg('Cet appareil ne fournit pas de GPS au navigateur. Placez la station sur la carte.');
  $('gp-msg').innerHTML = '<div class="gm-note">Recherche de la position…</div>';
  navigator.geolocation.getCurrentPosition(p => {
    GEO.mode.precision = p.coords.accuracy;
    GEO.placer(p.coords.latitude, p.coords.longitude, 'gps');
    $('gp-msg').innerHTML = p.coords.accuracy > 100
      ? `<div class="gm-err">Position imprécise (± ${Math.round(p.coords.accuracy)} m). Ajustez le marqueur, ou réessayez à l’extérieur.</div>` : '';
  }, err => msg({1:'Accès à la position refusé. Autorisez-le dans le navigateur, ou placez la station sur la carte.',
                 2:'Position introuvable (GPS indisponible). Placez la station sur la carte.', 3:'Le GPS a mis trop de temps à répondre. Réessayez ou placez la station sur la carte.'}[err.code] || err.message),
     {enableHighAccuracy:true, timeout:20000, maximumAge:0});
};
GEO.rechercher = async function(){
  const q = $('gp-q').value.trim(), z = $('gp-res');
  if(q.length < 2) return;
  z.innerHTML = '<div class="gm-note">Recherche…</div>';
  try{
    const r = await api('GET', '/recherche?' + qs({q}));
    z.innerHTML = r.resultats.length ? `<div class="gm-res">${r.resultats.map(x => `<button onclick="GEO.carte.flyTo([${x.latitude},${x.longitude}],15,{duration:1})">${esc(x.nom)}</button>`).join('')}</div><div class="gm-note">${esc(r.attribution)}</div>`
      : '<div class="gm-note">Aucun lieu trouvé en Haïti.</div>';
  }catch(e){ z.innerHTML = `<div class="gm-err">${esc(sansCode(e.message))}</div>`; }
};
GEO.enregistrerPosition = async function(extra){
  const m = GEO.mode, z = $('gp-msg');
  if(!m || m.lat == null) return z.innerHTML = '<div class="gm-err">Placez d’abord la station : touchez la carte ou utilisez le GPS.</div>';
  const corps = Object.assign({latitude:m.lat, longitude:m.lon, precision_m:m.precision, source:m.source, verifie:$('gp-ok').checked}, extra || {});
  try{
    GEO.detail = await api('POST', `/stations/${GEO.sel}/location`, corps);
    GEO.annulerPosition(true);
    notify('Position enregistrée.', 'ok'); await GEO.charger(false);
  }catch(e){
    const code = e.message.includes('position vérifiée') ? 'confirmer_remplacement' : (e.message.includes('Une autre station') ? 'forcer' : null);
    if(code && await showConfirm(sansCode(e.message), {title:'Confirmer la position', type:'warn', okLabel:'Oui, enregistrer'}))
      return GEO.enregistrerPosition(Object.assign({}, extra, {[code]:true}));
    z.innerHTML = `<div class="gm-err">${esc(sansCode(e.message))}</div>`;
  }
};

// ── Aller dans une autre station (PIN), depuis la carte ─────────────
GEO.allerVers = async function(id, page){
  if(GEO.sel !== id || !GEO.detail || GEO.detail.id !== id) await GEO.choisir(id, true);
  GEO.carte && GEO.carte.closePopup();
  const z = $('geo-aller');
  if(!z) return;
  z.innerHTML = `<div class="gm-form" style="margin-top:10px"><label for="geo-pin">Votre PIN pour entrer dans ${esc(GEO.detail.nom)}</label>
    <div class="gm-pin"><input id="geo-pin" type="password" inputmode="numeric" maxlength="9" autocomplete="off" placeholder="•••••••••"
      onkeydown="if(event.key==='Enter')GEO.entrer(${id}, '${page || 'dashboard'}')">
      <button class="gm-btn prim" style="height:46px" onclick="GEO.entrer(${id}, '${page || 'dashboard'}')">Entrer</button></div>
    <div class="gm-note">Vous quitterez la station actuelle et arriverez sur le tableau de bord de ${esc(GEO.detail.nom)}.</div><div id="geo-pin-err"></div></div>`;
  $('geo-pin').focus();
};
GEO.entrer = async function(id, page){
  const pin = ($('geo-pin').value || '').trim(), err = m => $('geo-pin-err').innerHTML = `<div class="gm-err">${esc(m)}</div>`;
  if(!/^[0-9]{9}$/.test(pin)) return err('Le PIN contient 9 chiffres.');
  try{
    await POST('/espaces/entrer', {cible_id:id, pin});
    try{ sessionStorage.setItem('konekta-station-choisie', '1'); sessionStorage.setItem('konekta_current_page', page || 'dashboard'); }catch(e){}
    $('geo-modal').classList.remove('ouvert');
    setTimeout(() => { location.href = '/'; }, DOUX ? 350 : 0);
  }catch(e){ err(sansCode(e.message)); $('geo-pin').select(); }
};
GEO.ouvrirTableau = function(id){
  const s = GEO.stations.find(x => x.id === id) || GEO.detail;
  if(s && s.ici){ GEO.fermer(); return goTo('dashboard'); }
  GEO.allerVers(id, 'dashboard');
};

// ── Sélecteur de position réutilisable (enregistrement d'une station) ──
GEO.selecteur = async function(boite, init, change){
  await GEO.leaflet();
  const c = L.map(boite, {maxBounds:[[16.5, -76], [21.5, -70]], minZoom:7}).fitBounds(HAITI);
  L.tileLayer(TUILES.osm[0], {maxZoom:19, attribution:TUILES.osm[1]}).addTo(c);
  let pin = null;
  const poser = (lat, lon, src, prec) => {
    lat = +(+lat).toFixed(6); lon = +(+lon).toFixed(6);
    if(!pin){ pin = L.marker([lat, lon], {draggable:true}).addTo(c);
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
