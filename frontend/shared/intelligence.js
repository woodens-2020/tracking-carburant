/* KONEKTA Intelligence Center — page « intelligence » de l'administration.
   Données : /api/intelligence/* (backend/intelligence.py), calculées à partir
   du journal de caisse et des relevés validés. Graphiques : Chart.js (déjà
   servi par l'application). Utilise les aides globales GET, esc, fmtG, goTo. */
(function(){
'use strict';
const IC = window.IC = {
  onglet:'apercu', f:null, opts:null, data:null, cle:null, charts:{}, enCours:null, auto:null,
  exp:{page:1, taille:50, tri:'date', ordre:'desc', q:'', sens:'', cols:null, data:null},
};
const ONGLETS = [['apercu','Vue d\'ensemble'],['finances','Finances'],['operations','Opérations'],
  ['departements','Départements'],['explorateur','Explorateur'],['analyses','Analyses'],['rapports','Rapports & exports']];
const PRESETS = [['aujourdhui','Aujourd\'hui'],['hier','Hier'],['7j','7 derniers jours'],['30j','30 derniers jours'],
  ['90j','90 derniers jours'],['semaine','Cette semaine'],['semaine-1','Semaine dernière'],['mois','Ce mois'],
  ['mois-1','Mois dernier'],['trimestre','Ce trimestre'],['trimestre-1','Trimestre dernier'],['annee','Cette année'],
  ['annee-1','Année dernière'],['perso','Période personnalisée']];
const COMPS = [['precedent','Période précédente'],['annee','Même période l\'an dernier'],['perso','Période personnalisée'],['aucune','Aucune comparaison']];
const MULTI = [['stations','Stations'],['modules','Départements'],['types','Types d\'opération'],['comptes','Catégories de dépenses'],
  ['produits','Produits'],['employes','Pompistes / saisi par']];
const COLS = [['date','Date'],['station','Station'],['module','Département'],['type_lib','Type'],['compte','Catégorie'],
  ['description','Description'],['tiers','Tiers'],['saisi_par','Saisi par'],['entree','Entrée'],['sortie','Sortie']];
const STOCKAGE = 'ic-filtres-v1';

// ── Dates ───────────────────────────────────────────────────────────
const iso = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
const jour = s => { const [y,m,d] = s.split('-').map(Number); return new Date(y, m-1, d); };
const plus = (d, n) => { const x = new Date(d); x.setDate(x.getDate()+n); return x; };
const fr = s => s ? jour(s).toLocaleDateString('fr-FR', {day:'2-digit', month:'2-digit', year:'numeric'}) : '';
const frCourt = s => jour(s).toLocaleDateString('fr-FR', {day:'2-digit', month:'short'});
function bornes(p){
  const a = new Date(); a.setHours(0,0,0,0);
  const lundi = plus(a, -((a.getDay()+6)%7));
  const tq = Math.floor(a.getMonth()/3)*3;
  switch(p){
    case 'aujourdhui': return [a, a];
    case 'hier': return [plus(a,-1), plus(a,-1)];
    case '7j': return [plus(a,-6), a];
    case '90j': return [plus(a,-89), a];
    case 'semaine': return [lundi, a];
    case 'semaine-1': return [plus(lundi,-7), plus(lundi,-1)];
    case 'mois': return [new Date(a.getFullYear(), a.getMonth(), 1), a];
    case 'mois-1': return [new Date(a.getFullYear(), a.getMonth()-1, 1), new Date(a.getFullYear(), a.getMonth(), 0)];
    case 'trimestre': return [new Date(a.getFullYear(), tq, 1), a];
    case 'trimestre-1': return [new Date(a.getFullYear(), tq-3, 1), new Date(a.getFullYear(), tq, 0)];
    case 'annee': return [new Date(a.getFullYear(), 0, 1), a];
    case 'annee-1': return [new Date(a.getFullYear()-1, 0, 1), new Date(a.getFullYear()-1, 11, 31)];
    default: return [plus(a,-29), a];
  }
}

// ── Filtres (gardés pendant la session) ─────────────────────────────
function filtresDefaut(){
  const [d, f] = bornes('30j');
  return {preset:'30j', debut:iso(d), fin:iso(f), comparer:'precedent', cdebut:'', cfin:'',
          stations:[], modules:[], types:[], comptes:[], produits:[], employes:[]};
}
function charger(){
  try{ const s = JSON.parse(sessionStorage.getItem(STOCKAGE) || 'null'); if(s && s.debut) return Object.assign(filtresDefaut(), s); }catch(e){}
  return filtresDefaut();
}
function garder(){ try{ sessionStorage.setItem(STOCKAGE, JSON.stringify(IC.f)); }catch(e){} }
function requete(extra){
  const f = IC.f, p = new URLSearchParams({debut:f.debut, fin:f.fin, comparer:f.comparer});
  if(f.comparer === 'perso'){ if(f.cdebut) p.set('cdebut', f.cdebut); if(f.cfin) p.set('cfin', f.cfin); }
  if(f.stations.length) p.set('stations', f.stations.join(','));
  for(const k of ['modules','types','comptes','produits','employes']) if(f[k].length) p.set(k, f[k].join('|'));
  for(const [k, v] of Object.entries(extra || {})) if(v !== '' && v != null) p.set(k, v);
  return p.toString();
}

// ── Formats ─────────────────────────────────────────────────────────
const G = v => v == null ? '—' : fmtG(v);
const GAL = v => v == null ? '—' : Number(v).toLocaleString('fr-FR', {maximumFractionDigits:1}) + ' gal';
const N = v => v == null ? '—' : Number(v).toLocaleString('fr-FR');
const PCT = v => v == null ? '—' : `${Number(v).toLocaleString('fr-FR', {maximumFractionDigits:1})} %`;
function badge(v, inverse){
  if(v == null) return '';
  const bon = inverse ? v < 0 : v > 0, cls = Math.abs(v) < 0.05 ? 'neutre' : (bon ? 'up' : 'down');
  return `<span class="ic-var ${cls}" title="Variation par rapport à la période de comparaison">${v > 0 ? '▲' : v < 0 ? '▼' : '■'} ${PCT(Math.abs(v))}</span>`;
}
const info = t => `<i class="ic-i" tabindex="0" role="img" aria-label="${esc(t)}" title="${esc(t)}">i</i>`;
function css(v){ return getComputedStyle(document.documentElement).getPropertyValue(v).trim() || '#888'; }
function palette(){
  return [css('--blue'), css('--accent'), css('--green'), '#a78bfa', '#2dd4bf', css('--red2'), '#f472b6', '#facc15', '#60a5fa', '#94a3b8'];
}
const vide = (msg) => `<div class="ic-vide">${msg || 'Aucune donnée pour ces filtres et cette période.'}</div>`;

// ── Graphiques ──────────────────────────────────────────────────────
function graph(id, cfg){
  if(IC.charts[id]){ IC.charts[id].destroy(); delete IC.charts[id]; }
  const c = document.getElementById(id);
  if(!c || typeof Chart === 'undefined') return;
  cfg.options = Object.assign({responsive:true, maintainAspectRatio:false, animation:{duration:400},
    interaction:{mode:'index', intersect:false}}, cfg.options || {});
  cfg.options.plugins = Object.assign({legend:{labels:{color:css('--text2'), boxWidth:12}},
    tooltip:{callbacks:{}}}, cfg.options.plugins || {});
  IC.charts[id] = new Chart(c, cfg);
}
const axeG = {beginAtZero:true, ticks:{color:css('--muted'), callback:v => Number(v).toLocaleString('fr-FR', {notation:'compact'})}, grid:{color:'rgba(127,127,127,.12)'}};
const tipG = {callbacks:{label:c => `${c.dataset.label || c.label}: ${G(c.parsed.y ?? c.parsed.x ?? c.parsed)}`}};
const etiq = (s, g) => g === 'mois' ? jour(s + '-01').toLocaleDateString('fr-FR', {month:'short', year:'2-digit'})
  : (g === 'semaine' ? 'sem. ' + frCourt(s) : frCourt(s));

// ── Page ────────────────────────────────────────────────────────────
window.initIntelligence = async function(){
  const page = document.getElementById('page-intelligence');
  if(!page) return;
  if(!IC.f) IC.f = charger();
  if(!page.dataset.pret){
    page.innerHTML = `<div class="ic-wrap">
      <div class="ic-filtres" id="ic-filtres" role="region" aria-label="Filtres"></div>
      <div id="ic-erreur"></div>
      <nav class="ic-onglets" role="tablist">${ONGLETS.map(([k, l]) => `<button class="ic-onglet" role="tab" data-o="${k}" onclick="IC.voir('${k}')">${l}</button>`).join('')}</nav>
      <div id="ic-contenu"></div></div>
      <div class="ic-panneau" id="ic-panneau" aria-hidden="true"><div class="fond" onclick="IC.fermer()"></div>
        <div class="boite" role="dialog" aria-modal="true" aria-labelledby="ic-p-titre"><header><div><h3 id="ic-p-titre"></h3><p id="ic-p-sous"></p></div>
        <button class="ic-btn" onclick="IC.fermer()" aria-label="Fermer">✕</button></header><div class="corps" id="ic-p-corps"></div></div></div>`;
    page.dataset.pret = '1';
    document.addEventListener('click', e => { if(!e.target.closest('.ic-ms')) IC.fermerPops(); });
    document.addEventListener('keydown', e => { if(e.key === 'Escape'){ IC.fermerPops(); IC.fermer(); } });
  }
  await chargerOptions();
  rendreFiltres();
  IC.voir(IC.onglet, true);
  await IC.actualiser();
  if(!IC.auto) IC.auto = setInterval(() => {
    if(document.getElementById('page-intelligence')?.classList.contains('active') && !document.hidden) IC.actualiser(true);
  }, 5 * 60 * 1000);
};

async function chargerOptions(){
  try{ IC.opts = await GET('/intelligence/filtres?' + new URLSearchParams({debut:IC.f.debut, fin:IC.f.fin})); }
  catch(e){ IC.opts = IC.opts || {stations:[], modules:[], types:[], comptes:[], produits:[], employes:[], multi_stations:false}; }
}

function options(k){
  const o = IC.opts || {};
  if(k === 'stations') return (o.stations || []).map(s => [String(s.id), s.nom]);
  if(k === 'types') return (o.types || []).map(t => [t.id, t.nom]);
  if(k === 'produits') return (o.produits || []).map(p => [p.id, p.nom]);
  return (o[k] || []).map(v => [v, v]);
}
function libelle(k, v){ const x = options(k).find(o => o[0] === v); return x ? x[1] : v; }

function rendreFiltres(){
  const f = IC.f, el = document.getElementById('ic-filtres');
  const multi = MULTI.filter(([k]) => k !== 'stations' || (IC.opts && IC.opts.multi_stations));
  const chips = [];
  for(const [k, l] of MULTI) for(const v of f[k]) chips.push(`<span class="ic-chip"><b>${esc(l)}</b> ${esc(libelle(k, v))}
    <button onclick="IC.retirer('${k}', ${esc(JSON.stringify(v)).replace(/"/g, '&quot;')})" aria-label="Retirer ce filtre">×</button></span>`);
  el.innerHTML = `<div class="ic-ligne">
      <label class="ic-champ">Période<select id="ic-preset" onchange="IC.preset(this.value)">${PRESETS.map(([k, l]) => `<option value="${k}" ${f.preset === k ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
      <label class="ic-champ">Du<input type="date" id="ic-debut" value="${f.debut}" max="${f.fin}" onchange="IC.dates()"></label>
      <label class="ic-champ">Au<input type="date" id="ic-fin" value="${f.fin}" min="${f.debut}" onchange="IC.dates()"></label>
      <span class="ic-sep" aria-hidden="true"></span>
      <label class="ic-champ">Comparer avec<select id="ic-comp" onchange="IC.comp(this.value)">${COMPS.map(([k, l]) => `<option value="${k}" ${f.comparer === k ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
      ${f.comparer === 'perso' ? `<label class="ic-champ">Du<input type="date" id="ic-cdebut" value="${f.cdebut}" onchange="IC.compDates()"></label>
        <label class="ic-champ">Au<input type="date" id="ic-cfin" value="${f.cfin}" onchange="IC.compDates()"></label>` : ''}
      <div class="ic-maj"><span id="ic-heure" aria-live="polite"></span>
        <button class="ic-btn" onclick="IC.actualiser(true)" title="Recharger les données">⟳ Actualiser</button></div>
    </div>
    <div class="ic-ligne">${multi.map(([k, l]) => `<div class="ic-ms ${f[k].length ? 'on' : ''}" id="ic-ms-${k}">
      <button type="button" aria-haspopup="listbox" aria-expanded="false" onclick="IC.ouvrirPop('${k}', event)">${l}${f[k].length ? ` <span class="n">${f[k].length}</span>` : ' ▾'}</button></div>`).join('')}</div>
    ${chips.length ? `<div class="ic-chips" aria-label="Filtres actifs">${chips.join('')}<button class="tout" onclick="IC.toutEffacer()">Tout effacer</button></div>` : ''}`;
}

IC.ouvrirPop = function(k, ev){
  ev.stopPropagation();
  const box = document.getElementById('ic-ms-' + k);
  if(box.querySelector('.ic-pop')){ IC.fermerPops(); return; }
  IC.fermerPops();
  const opts = options(k), sel = new Set(IC.f[k]);
  const pop = document.createElement('div');
  pop.className = 'ic-pop'; pop.setAttribute('role', 'listbox'); pop.setAttribute('aria-multiselectable', 'true');
  pop.innerHTML = `<input type="search" placeholder="Rechercher…" aria-label="Rechercher">
    <div class="liste">${opts.length ? opts.map(([v, l]) => `<label data-t="${esc(l.toLowerCase())}"><input type="checkbox" value="${esc(v)}" ${sel.has(v) ? 'checked' : ''}> ${esc(l)}</label>`).join('')
      : '<div class="ic-vide-liste">Aucune valeur sur la période.</div>'}</div>
    <div class="pied"><button type="button" data-a="effacer">Effacer</button><button type="button" data-a="ok">Appliquer</button></div>`;
  box.appendChild(pop);
  box.querySelector(':scope > button').setAttribute('aria-expanded', 'true');
  pop.addEventListener('click', e => e.stopPropagation());
  const q = pop.querySelector('input[type=search]');
  q.focus();
  q.oninput = () => pop.querySelectorAll('label').forEach(l => l.style.display = l.dataset.t.includes(q.value.toLowerCase()) ? '' : 'none');
  pop.querySelector('[data-a=effacer]').onclick = () => { IC.f[k] = []; IC.appliquer(); };
  pop.querySelector('[data-a=ok]').onclick = () => {
    IC.f[k] = [...pop.querySelectorAll('.liste input:checked')].map(i => i.value); IC.appliquer();
  };
};
IC.fermerPops = () => document.querySelectorAll('.ic-pop').forEach(p => {
  p.parentElement.querySelector(':scope > button')?.setAttribute('aria-expanded', 'false'); p.remove();
});
IC.appliquer = async function(rechargerOptions){
  IC.fermerPops(); garder(); IC.exp.page = 1;
  if(rechargerOptions) await chargerOptions();
  rendreFiltres(); IC.actualiser();
};
IC.retirer = (k, v) => { IC.f[k] = IC.f[k].filter(x => x !== v); IC.appliquer(); };
IC.toutEffacer = () => { for(const [k] of MULTI) IC.f[k] = []; IC.appliquer(); };
IC.preset = function(p){
  IC.f.preset = p;
  if(p !== 'perso'){ const [d, f] = bornes(p); IC.f.debut = iso(d); IC.f.fin = iso(f); }
  IC.appliquer(true);
};
IC.dates = function(){
  const d = document.getElementById('ic-debut').value, f = document.getElementById('ic-fin').value;
  if(!d || !f) return;
  if(d > f){ notify('La date de début doit être avant la date de fin.', 'err'); return; }
  Object.assign(IC.f, {debut:d, fin:f, preset:'perso'}); IC.appliquer(true);
};
IC.comp = function(v){
  IC.f.comparer = v;
  if(v === 'perso' && !(IC.f.cdebut && IC.f.cfin)){ rendreFiltres(); return; }
  IC.appliquer();
};
IC.compDates = function(){
  IC.f.cdebut = document.getElementById('ic-cdebut').value; IC.f.cfin = document.getElementById('ic-cfin').value;
  if(IC.f.cdebut && IC.f.cfin) IC.appliquer();
};

// ── Chargement (pas de requête en double si rien n'a changé) ────────
IC.actualiser = async function(force){
  const cle = requete();
  if(!force && cle === IC.cle && IC.data) return rendreOnglet();
  if(IC.f.comparer === 'perso' && !(IC.f.cdebut && IC.f.cfin)) return;
  IC.cle = cle;
  const contenu = document.getElementById('ic-contenu');
  if(!IC.data) contenu.innerHTML = squelette(); else contenu.classList.add('ic-charge');
  const ici = IC.enCours = Symbol();
  try{
    const d = await GET('/intelligence/analyse?' + cle);
    if(ici !== IC.enCours) return;
    IC.data = d;
    document.getElementById('ic-erreur').innerHTML = '';
    document.getElementById('ic-heure').textContent = 'Mis à jour à ' + new Date().toLocaleTimeString('fr-FR', {hour:'2-digit', minute:'2-digit'});
    if(IC.onglet === 'explorateur') IC.exp.data = null;
    rendreOnglet();
  }catch(e){
    if(ici !== IC.enCours) return;
    IC.cle = null;
    document.getElementById('ic-erreur').innerHTML = `<div class="ic-err" role="alert"><span><b>Les données n'ont pas pu être chargées.</b>
      ${esc(e.message || '')} — vérifiez la connexion internet et les dates choisies, puis réessayez.</span>
      <button class="ic-btn" onclick="IC.actualiser(true)">Réessayer</button></div>`;
    if(!IC.data) contenu.innerHTML = '';
  }finally{ contenu.classList.remove('ic-charge'); }
};
function squelette(){
  return `<div class="ic-wrap" aria-busy="true"><div class="ic-kpis">${'<div class="ic-sq" style="height:110px"></div>'.repeat(8)}</div>
    <div class="ic-grille"><div class="ic-carte l8"><div class="ic-sq" style="height:300px"></div></div><div class="ic-carte l4"><div class="ic-sq" style="height:300px"></div></div></div></div>`;
}

IC.voir = function(o, sansRendu){
  IC.onglet = o;
  document.querySelectorAll('.ic-onglet').forEach(b => { b.classList.toggle('on', b.dataset.o === o); b.setAttribute('aria-selected', b.dataset.o === o); });
  if(!sansRendu) rendreOnglet();
};
function rendreOnglet(){
  const d = IC.data, el = document.getElementById('ic-contenu');
  if(!d || !el) return;
  Object.values(IC.charts).forEach(c => c.destroy()); IC.charts = {};
  ({apercu, finances, operations, departements, explorateur, analyses, rapports})[IC.onglet](el, d);
}
function libComp(d){ return d.comparaison ? `du ${fr(d.comparaison.debut)} au ${fr(d.comparaison.fin)}` : ''; }

// ── Vue d'ensemble ──────────────────────────────────────────────────
function carteKpi(id, titre, aide, valeur, variation, inverse, contexte, action){
  return `<button class="ic-kpi" id="ic-k-${id}" onclick="${action}" aria-label="${esc(titre)} : ${esc(valeur)}">
    <span class="t">${titre}${info(aide)}</span><span class="v">${valeur}</span>${badge(variation, inverse)}
    <span class="c">${contexte}</span></button>`;
}
function apercu(el, d){
  const k = d.kpis, p = d.kpis_prec, v = d.variations, c = libComp(d);
  const vs = (cle, f) => p ? `${f(p[cle])} ${c}` : 'Pas de comparaison choisie';
  el.innerHTML = `<div class="ic-wrap">
    <div class="ic-kpis">
      ${carteKpi('revenu', 'Revenu', 'Ventes de la période (carburant, Bar, Cuisine, Hôtel), d\'après les relevés validés et les ventes encaissées. Ce n\'est pas l\'argent en caisse.', G(k.revenu), v.revenu, false, vs('revenu', G), "IC.drill('revenu')")}
      ${carteKpi('charges', 'Charges', 'Dépenses, achats et salaires de la période, y compris les dépenses payées par les pompistes. Les dépôts en banque (transferts) n\'en font pas partie.', G(k.charges), v.charges, true, vs('charges', G), "IC.voir('finances')")}
      ${carteKpi('resultat', 'Résultat', 'Revenu − charges, en base caisse : un achat de stock compte quand il est payé.', G(k.resultat), v.resultat, false, k.marge != null ? `Marge : ${PCT(k.marge)} du revenu` : 'Pas de revenu sur la période', "IC.voir('finances')")}
      ${carteKpi('treso', 'Trésorerie', 'Solde de la caisse principale à la date de fin (toutes opérations, filtres non appliqués).', G(d.tresorerie.solde_fin), null, false, `Au ${fr(d.periode.fin)} · ouverture ${G(d.tresorerie.ouverture)}`, "goTo('caisse-principale')")}
      ${carteKpi('enc', 'Encaissements', 'Tout l\'argent entré dans la caisse : ventes, entrées des sources, renflouements, remboursements de crédit.', G(k.encaissements), v.encaissements, false, `Sorties : ${G(k.decaissements)} · flux net ${G(k.flux_net)}`, "IC.drill('entrees')")}
      ${carteKpi('gallons', 'Gallons vendus', 'Somme des relevés de compteurs validés (les rapports pompistes en attente ne comptent pas).', GAL(k.gallons), v.gallons, false, `${N(k.nb_releves)} relevés · prix moyen ${k.prix_moyen_gallon ? G(k.prix_moyen_gallon) + '/gal' : '—'}`, "IC.voir('operations')")}
      ${carteKpi('moyen', 'Revenu moyen / jour', 'Revenu divisé par le nombre de jours de la période.', G(k.revenu_moyen_jour), v.revenu_moyen_jour, false, `${k.jours_ventes} jour(s) de vente sur ${k.jours}`, "IC.voir('operations')")}
      ${carteKpi('stock', 'Valeur du stock', 'Stock carburant restant aujourd\'hui × coût moyen d\'achat. Sans coût d\'achat connu, aucune valeur n\'est affichée.', d.stock ? G(d.stock.valeur) : '—', null, false, d.stock ? `${GAL(d.stock.gallons)} en cuve${d.stock.sans_cout.length ? ' · sans coût : ' + esc(d.stock.sans_cout.join(', ')) : ''}` : 'Aucun coût d\'achat enregistré', "goTo('stock')")}
    </div>
    <div class="ic-grille">
      <section class="ic-carte l8"><h3>Revenu et charges</h3><p class="q">Les ventes couvrent-elles les charges, ${d.granularite === 'jour' ? 'jour par jour' : 'par ' + d.granularite} ?${d.series_prec ? ' En pointillé : le revenu de la période de comparaison.' : ''}</p>
        <div class="ic-graph"><canvas id="ic-g-rc" aria-label="Graphique revenu et charges"></canvas></div></section>
      <section class="ic-carte l4"><h3>À retenir</h3><p class="q">Constats calculés sur vos données (voir « Analyses »).</p>
        ${d.analyses.length ? d.analyses.slice(0, 3).map(insight).join('') : vide('Rien de particulier à signaler sur cette période.')}</section>
      <section class="ic-carte"><h3>Répartition des charges</h3><p class="q">Où va l'argent ? Cliquez sur une catégorie pour voir ses opérations.</p>
        ${d.charges_par_categorie.length ? '<div class="ic-graph"><canvas id="ic-g-ch"></canvas></div>' : vide('Aucune charge sur la période.')}</section>
      <section class="ic-carte"><h3>Revenu par département</h3><p class="q">Quelle activité rapporte le plus ? Cliquez pour filtrer sur un département.</p>
        ${d.departements.some(x => x.revenu) ? '<div class="ic-graph"><canvas id="ic-g-dep"></canvas></div>' : vide('Aucun revenu sur la période.')}</section>
    </div></div>`;
  grRevenuCharges('ic-g-rc', d);
  grDonut('ic-g-ch', d.charges_par_categorie.slice(0, 9), x => x.montant, i => IC.drill('categorie', d.charges_par_categorie[i].nom));
  const deps = d.departements.filter(x => x.revenu);
  graph('ic-g-dep', {type:'bar', data:{labels:deps.map(x => x.nom), datasets:[{label:'Revenu', data:deps.map(x => x.revenu), backgroundColor:palette()[0], borderRadius:6}]},
    options:{indexAxis:'y', plugins:{legend:{display:false}, tooltip:tipG}, scales:{x:axeG, y:{ticks:{color:css('--text2')}, grid:{display:false}}},
      onClick:(e, el) => el.length && IC.filtrer('modules', deps[el[0].index].nom)}});
}
function grRevenuCharges(id, d){
  const pal = palette(), s = d.series;
  const ds = [
    {label:'Revenu', data:s.map(x => x.revenu), borderColor:pal[0], backgroundColor:pal[0] + '33', fill:true, tension:.3, pointRadius:s.length > 40 ? 0 : 2},
    {label:'Charges', data:s.map(x => x.charges), borderColor:pal[5], backgroundColor:'transparent', tension:.3, pointRadius:s.length > 40 ? 0 : 2},
  ];
  if(d.series_prec) ds.push({label:'Revenu (comparaison)', data:s.map((x, i) => d.series_prec[i]?.revenu ?? null), borderColor:pal[9], borderDash:[5, 4], pointRadius:0, tension:.3, fill:false});
  graph(id, {type:'line', data:{labels:s.map(x => etiq(x.cle, d.granularite)), datasets:ds},
    options:{plugins:{tooltip:tipG}, scales:{y:axeG, x:{ticks:{color:css('--muted'), maxTicksLimit:12}, grid:{display:false}}},
      onClick:(e, el) => el.length && d.granularite === 'jour' && IC.jourDetail(s[el[0].index].cle)}});
}
function grDonut(id, items, val, clic){
  const tot = items.reduce((a, x) => a + val(x), 0);
  graph(id, {type:'doughnut', data:{labels:items.map(x => x.nom), datasets:[{data:items.map(val), backgroundColor:palette(), borderColor:css('--panel'), borderWidth:2}]},
    options:{cutout:'62%', interaction:{mode:'nearest'}, plugins:{legend:{position:'right', labels:{color:css('--text2'), boxWidth:12}},
      tooltip:{callbacks:{label:c => `${c.label}: ${G(c.parsed)} (${tot ? Math.round(c.parsed / tot * 100) : 0} %)`}}},
      onClick:(e, el) => el.length && clic(el[0].index)}});
}
function insight(x){
  return `<article class="ic-in ${x.niveau}"><h4>${esc(x.titre)}</h4><p>${esc(x.explication)}</p>
    <dl><dt>Indicateur</dt><dd>${esc(x.metrique)}</dd><dt>Preuve</dt><dd>${esc(x.preuve)}</dd>
    ${x.comparaison ? `<dt>Comparé à</dt><dd>${esc(x.comparaison)}</dd>` : ''}<dt>À examiner</dt><dd>${esc(x.a_examiner)}</dd></dl></article>`;
}

// ── Finances ────────────────────────────────────────────────────────
function finances(el, d){
  const k = d.kpis, ratio = k.revenu ? k.charges / k.revenu * 100 : null;
  const sorties = d.composition.filter(f => f.sens === 'sorties'), entrees = d.composition.filter(f => f.sens === 'entrees');
  el.innerHTML = `<div class="ic-grille">
    <section class="ic-carte l8"><h3>Encaissements et décaissements</h3><p class="q">Argent réellement entré et sorti de la caisse${d.granularite === 'jour' ? ', jour par jour' : ''} — différent du revenu.</p>
      <div class="ic-graph"><canvas id="ic-g-flux"></canvas></div></section>
    <section class="ic-carte l4"><h3>Charges / revenu</h3><p class="q">Quelle part du revenu les charges consomment-elles ?</p>
      ${ratio == null ? vide('Pas de revenu sur la période.') : `<div class="ic-graph h1"><canvas id="ic-g-jauge"></canvas></div>
      <p style="text-align:center;margin:-70px 0 30px;font-family:var(--mono);font-size:26px;font-weight:700">${PCT(Math.round(ratio * 10) / 10)}</p>
      <p class="q" style="text-align:center">${ratio <= 70 ? 'Les charges laissent une marge confortable.' : ratio <= 100 ? 'Les charges consomment l\'essentiel du revenu.' : 'Les charges dépassent le revenu sur la période.'}</p>`}</section>
    <section class="ic-carte l12"><h3>Du solde d'ouverture au solde de clôture</h3><p class="q">Comment la caisse principale est passée de ${G(d.cascade[0].montant)} à ${G(d.cascade[d.cascade.length - 1].montant)} (toutes opérations de la période).</p>
      <div class="ic-graph"><canvas id="ic-g-cascade"></canvas></div></section>
    <section class="ic-carte"><h3>Résultat par ${d.granularite}</h3><p class="q">Revenu − charges : les périodes rentables en vert.</p>
      <div class="ic-graph"><canvas id="ic-g-res"></canvas></div></section>
    <section class="ic-carte"><h3>Composition des sorties</h3><p class="q">Familles et catégories, proportionnelles au montant. Cliquez pour voir les opérations.</p>
      ${sorties.length ? treemap(sorties) : vide('Aucune sortie sur la période.')}</section>
    <section class="ic-carte l12"><h3>Charges par catégorie</h3><p class="q">Montant, poids et évolution de chaque catégorie de dépense.</p>
      ${d.charges_par_categorie.length ? tableCharges(d) : vide('Aucune charge sur la période.')}</section>
    <section class="ic-carte l12"><h3>Composition des entrées</h3><p class="q">D'où vient l'argent.</p>${entrees.length ? treemap(entrees) : vide('Aucune entrée sur la période.')}</section>
  </div>`;
  const pal = palette(), s = d.series;
  graph('ic-g-flux', {type:'line', data:{labels:s.map(x => etiq(x.cle, d.granularite)), datasets:[
    {label:'Encaissements', data:s.map(x => x.encaissements), borderColor:pal[2], backgroundColor:pal[2] + '22', fill:true, tension:.3, pointRadius:0},
    {label:'Décaissements', data:s.map(x => x.decaissements), borderColor:pal[5], backgroundColor:pal[5] + '22', fill:true, tension:.3, pointRadius:0}]},
    options:{plugins:{tooltip:tipG}, scales:{y:axeG, x:{ticks:{color:css('--muted'), maxTicksLimit:12}, grid:{display:false}}}}});
  if(ratio != null) graph('ic-g-jauge', {type:'doughnut', data:{labels:['Charges', 'Reste'], datasets:[{data:[Math.min(ratio, 100), Math.max(0, 100 - ratio)],
    backgroundColor:[ratio > 100 ? pal[5] : ratio > 70 ? pal[1] : pal[2], css('--panel3')], borderWidth:0}]},
    options:{rotation:-90, circumference:180, cutout:'75%', plugins:{legend:{display:false}, tooltip:{enabled:false}}}});
  let cumul = 0;
  const cas = d.cascade.map(x => { if(x.type === 'total'){ cumul = x.montant; return [0, x.montant]; } const a = cumul; cumul += x.montant; return [a, cumul]; });
  graph('ic-g-cascade', {type:'bar', data:{labels:d.cascade.map(x => x.nom), datasets:[{label:'Montant', data:cas,
    backgroundColor:d.cascade.map(x => x.type === 'total' ? pal[0] : x.montant >= 0 ? pal[2] : pal[5]), borderRadius:5}]},
    options:{interaction:{mode:'nearest'}, plugins:{legend:{display:false}, tooltip:{callbacks:{label:c => G(d.cascade[c.dataIndex].montant)}}},
      scales:{y:axeG, x:{ticks:{color:css('--text2')}, grid:{display:false}}}}});
  graph('ic-g-res', {type:'bar', data:{labels:s.map(x => etiq(x.cle, d.granularite)), datasets:[{label:'Résultat', data:s.map(x => x.resultat),
    backgroundColor:s.map(x => x.resultat >= 0 ? pal[2] : pal[5]), borderRadius:4}]},
    options:{plugins:{legend:{display:false}, tooltip:tipG}, scales:{y:axeG, x:{ticks:{color:css('--muted'), maxTicksLimit:12}, grid:{display:false}}}}});
}
function treemap(familles){
  const pal = palette(), tot = familles.reduce((a, f) => a + Math.abs(f.montant), 0) || 1;
  return `<div class="ic-tm">${familles.flatMap((f, i) => f.comptes.slice(0, 6).map(c => {
    const part = Math.abs(c.montant) / tot;
    return `<button style="flex-basis:${Math.max(part * 100, 14)}%;background:${pal[i % pal.length]}${part < .05 ? 'b3' : ''}" title="${esc(f.famille)} · ${esc(c.nom)} : ${G(c.montant)}"
      onclick="IC.drill('categorie', ${esc(JSON.stringify(c.nom)).replace(/"/g, '&quot;')})"><b>${esc(c.nom)}</b><span>${esc(f.famille)} · ${G(c.montant)} · ${Math.round(part * 100)} %</span></button>`;
  })).join('')}</div>`;
}
function tableCharges(d){
  const tot = d.kpis.charges || 1;
  return `<div class="ic-tab-wrap"><table class="ic-tab"><thead><tr><th>Catégorie</th><th class="num">Montant</th><th>Poids</th>
    ${d.comparaison ? '<th class="num">Comparaison</th><th class="num">Évolution</th>' : ''}<th class="num">Opérations</th></tr></thead><tbody>
    ${d.charges_par_categorie.map(c => `<tr class="clic" onclick="IC.drill('categorie', ${esc(JSON.stringify(c.nom)).replace(/"/g, '&quot;')})"><td>${esc(c.nom)}</td><td class="num">${G(c.montant)}</td>
      <td><div class="ic-barre" title="${PCT(Math.round(c.montant / tot * 1000) / 10)}"><i style="width:${Math.min(100, c.montant / tot * 100)}%"></i></div></td>
      ${d.comparaison ? `<td class="num ic-dim">${G(c.prec)}</td><td class="num">${badge(c.variation, true) || '—'}</td>` : ''}<td class="num">${c.nb}</td></tr>`).join('')}
    </tbody></table></div>`;
}

// ── Opérations ──────────────────────────────────────────────────────
function operations(el, d){
  const hmMax = Math.max(1, ...d.activite.map(a => Math.max(a.matin / (a.nb_jours || 1), a.apres_midi / (a.nb_jours || 1))));
  const cel = (v, n) => { const m = n ? v / n : 0, a = m / hmMax;
    return `<div style="background:rgba(59,130,246,${(0.08 + a * 0.82).toFixed(2)});color:${a > .55 ? '#fff' : 'var(--text)'}" title="Moyenne par jour : ${G(m)}">${G(m)}</div>`; };
  el.innerHTML = `<div class="ic-grille">
    <section class="ic-carte l8"><h3>Gallons vendus par produit</h3><p class="q">L'activité de chaque carburant dans le temps.</p>
      ${d.produits.length ? '<div class="ic-graph"><canvas id="ic-g-gal"></canvas></div>' : vide('Aucun relevé validé sur la période.')}</section>
    <section class="ic-carte l4"><h3>Classement des produits</h3><p class="q">Ventes en gourdes. Cliquez pour le détail du produit.</p>
      ${d.produits.length ? '<div class="ic-graph"><canvas id="ic-g-prod"></canvas></div>' : vide()}</section>
    <section class="ic-carte l12"><h3>Calendrier des ventes</h3><p class="q">Chaque case est un jour (lignes : lundi → dimanche). Plus elle est foncée, plus le revenu est élevé. Cliquez sur un jour pour ses relevés.</p>
      ${calendrier(d)}</section>
    <section class="ic-carte"><h3>Activité par jour et créneau</h3><p class="q">Ventes carburant moyennes par jour de la semaine, matin et après-midi.</p>
      ${d.kpis.nb_releves ? `<div class="ic-hm"><div class="e"></div><div class="e">Matin</div><div class="e">Après-midi</div>
        ${d.activite.map(a => `<div class="e">${a.jour}</div>${cel(a.matin, a.nb_jours)}${cel(a.apres_midi, a.nb_jours)}`).join('')}</div>` : vide()}</section>
    <section class="ic-carte"><h3>Mètres les plus actifs</h3><p class="q">Gallons par mètre (pompe) sur la période.</p>
      ${d.pompes.length ? '<div class="ic-graph"><canvas id="ic-g-pompes"></canvas></div>' : vide()}</section>
    <section class="ic-carte l12"><h3>Pompistes</h3><p class="q">Volume et ventes des relevés de chaque pompiste (« Saisie au bureau » : relevés entrés par l'administration).</p>
      ${d.pompistes.length ? `<div class="ic-tab-wrap"><table class="ic-tab"><thead><tr><th>Pompiste</th><th class="num">Gallons</th><th class="num">Ventes</th><th class="num">Relevés</th><th class="num">Jours</th><th>Part des ventes</th></tr></thead><tbody>
        ${d.pompistes.map(p => `<tr class="clic" onclick="IC.filtrer('employes', ${esc(JSON.stringify(p.nom)).replace(/"/g, '&quot;')})"><td>${esc(p.nom)}</td><td class="num">${GAL(p.gallons)}</td><td class="num">${G(p.montant)}</td><td class="num">${p.nb}</td><td class="num">${p.jours}</td>
          <td><div class="ic-barre"><i style="width:${d.kpis.revenu ? Math.min(100, p.montant / d.produits.reduce((a, x) => a + x.montant, 0) * 100) : 0}%"></i></div></td></tr>`).join('')}
        </tbody></table></div>` : vide()}</section></div>`;
  const pal = palette();
  graph('ic-g-gal', {type:'line', data:{labels:d.series.map(x => etiq(x.cle, d.granularite)), datasets:d.produits.map((p, i) => ({label:p.nom, data:p.series,
    borderColor:pal[i % pal.length], backgroundColor:pal[i % pal.length] + '40', fill:true, tension:.3, pointRadius:0}))},
    options:{plugins:{tooltip:{callbacks:{label:c => `${c.dataset.label}: ${GAL(c.parsed.y)}`}}}, scales:{y:Object.assign({}, axeG, {stacked:true, beginAtZero:true}), x:{ticks:{color:css('--muted'), maxTicksLimit:12}, grid:{display:false}}}}});
  graph('ic-g-prod', {type:'bar', data:{labels:d.produits.map(p => p.nom), datasets:[{label:'Ventes', data:d.produits.map(p => p.montant), backgroundColor:d.produits.map((p, i) => pal[i % pal.length]), borderRadius:6}]},
    options:{indexAxis:'y', plugins:{legend:{display:false}, tooltip:tipG}, scales:{x:axeG, y:{ticks:{color:css('--text2')}, grid:{display:false}}},
      onClick:(e, el) => el.length && IC.produitDetail(d.produits[el[0].index])}});
  graph('ic-g-pompes', {type:'bar', data:{labels:d.pompes.map(p => p.nom), datasets:[{label:'Gallons', data:d.pompes.map(p => p.gallons), backgroundColor:pal[0], borderRadius:4}]},
    options:{indexAxis:'y', plugins:{legend:{display:false}, tooltip:{callbacks:{label:c => GAL(c.parsed.x)}}}, scales:{x:{ticks:{color:css('--muted')}, grid:{color:'rgba(127,127,127,.12)'}}, y:{ticks:{color:css('--text2')}, grid:{display:false}}}}});
}
function calendrier(d){
  const c = d.calendrier;
  if(!c.length) return vide();
  const max = Math.max(...c.map(x => x.revenu));
  if(!max) return vide('Aucune vente sur la période.');
  const debut = jour(c[0].date), dec = (debut.getDay() + 6) % 7;
  const cases = Array(dec).fill('<span></span>');
  const niv = v => v ? Math.min(4, 1 + Math.floor(v / max * 4)) : 0;
  const coul = ['var(--panel3)', 'rgba(59,130,246,.3)', 'rgba(59,130,246,.5)', 'rgba(59,130,246,.75)', 'var(--blue)'];
  for(const x of c) cases.push(`<button style="background:${coul[niv(x.revenu)]}" title="${fr(x.date)} : ${G(x.revenu)}" aria-label="${fr(x.date)} : ${G(x.revenu)}" onclick="IC.jourDetail('${x.date}')"></button>`);
  return `<div class="ic-cal">${cases.join('')}</div>
    <div class="ic-leg">Moins ${coul.map(x => `<i style="background:${x}"></i>`).join('')} Plus · maximum ${G(max)} / jour</div>`;
}

// ── Départements ────────────────────────────────────────────────────
function departements(el, d){
  const deps = d.departements, k = d.kpis, p = d.kpis_prec;
  el.innerHTML = `<div class="ic-grille">
    <section class="ic-carte l12"><h3>Performance par département</h3><p class="q">Chaque activité de l'établissement (lue dans les opérations, rien n'est fixé à l'avance). Cliquez pour analyser un département.</p>
      ${deps.length ? `<div class="ic-kpis">${deps.map(x => `<button class="ic-kpi" onclick="IC.filtrer('modules', ${esc(JSON.stringify(x.nom)).replace(/"/g, '&quot;')})">
        <span class="t">${esc(x.nom)}</span><span class="v">${G(x.revenu)}</span>${badge(x.variation)}
        <span class="c">Charges ${G(x.charges)} · résultat <b class="${x.resultat >= 0 ? 'ic-pos' : 'ic-neg'}">${G(x.resultat)}</b> · ${x.nb} opérations</span></button>`).join('')}</div>` : vide()}</section>
    <section class="ic-carte"><h3>Comparaison des départements</h3><p class="q">Revenu et charges côte à côte.</p>
      ${deps.length ? '<div class="ic-graph"><canvas id="ic-g-deps"></canvas></div>' : vide()}</section>
    <section class="ic-carte"><h3>Profil de la période</h3><p class="q">Cinq dimensions, en % de la plus forte des deux périodes${p ? '' : ' (choisissez une comparaison pour superposer la période de référence)'}.</p>
      <div class="ic-graph"><canvas id="ic-g-radar"></canvas></div></section>
    ${d.stations.length > 1 ? `<section class="ic-carte l12"><h3>Comparaison des stations</h3><p class="q">Chaque station séparée, sur les mêmes filtres.</p>
      <div class="ic-tab-wrap"><table class="ic-tab"><thead><tr><th>Station</th><th class="num">Revenu</th><th class="num">Charges</th><th class="num">Résultat</th><th class="num">Gallons</th><th class="num">Encaissements</th></tr></thead><tbody>
      ${d.stations.map(s => `<tr><td>${esc(s.nom)}</td><td class="num">${G(s.revenu)}</td><td class="num">${G(s.charges)}</td><td class="num ${s.resultat >= 0 ? 'ic-pos' : 'ic-neg'}">${G(s.resultat)}</td><td class="num">${GAL(s.gallons)}</td><td class="num">${G(s.encaissements)}</td></tr>`).join('')}
      </tbody></table></div></section>` : ''}</div>`;
  const pal = palette();
  graph('ic-g-deps', {type:'bar', data:{labels:deps.map(x => x.nom), datasets:[
    {label:'Revenu', data:deps.map(x => x.revenu), backgroundColor:pal[0], borderRadius:5},
    {label:'Charges', data:deps.map(x => x.charges), backgroundColor:pal[5], borderRadius:5}]},
    options:{indexAxis:'y', plugins:{tooltip:{callbacks:{label:c => `${c.dataset.label}: ${G(c.parsed.x)}`}}}, scales:{x:axeG, y:{ticks:{color:css('--text2')}, grid:{display:false}}},
      onClick:(e, el) => el.length && IC.filtrer('modules', deps[el[0].index].nom)}});
  const dims = [['revenu', 'Revenu'], ['charges', 'Charges'], ['gallons', 'Gallons'], ['nb_operations', 'Opérations'], ['jours_ventes', 'Jours de vente']];
  const norm = (a, b) => { const m = Math.max(Math.abs(a || 0), Math.abs(b || 0)); return m ? Math.round(Math.abs(a || 0) / m * 100) : 0; };
  const ds = [{label:'Période choisie', data:dims.map(([c]) => norm(k[c], p && p[c])), borderColor:pal[0], backgroundColor:pal[0] + '33'}];
  if(p) ds.push({label:'Comparaison', data:dims.map(([c]) => norm(p[c], k[c])), borderColor:pal[1], backgroundColor:pal[1] + '22'});
  graph('ic-g-radar', {type:'radar', data:{labels:dims.map(x => x[1]), datasets:ds},
    options:{interaction:{mode:'nearest'}, scales:{r:{min:0, max:100, ticks:{display:false}, grid:{color:'rgba(127,127,127,.2)'}, angleLines:{color:'rgba(127,127,127,.2)'}, pointLabels:{color:css('--text2'), font:{size:12}}}},
      plugins:{tooltip:{callbacks:{label:c => { const cle = dims[c.dataIndex][0], src = c.datasetIndex === 0 ? k : p;
        return `${c.dataset.label}: ${cle === 'gallons' ? GAL(src[cle]) : ['revenu', 'charges'].includes(cle) ? G(src[cle]) : N(src[cle])}`; }}}}}});
}

// ── Explorateur ─────────────────────────────────────────────────────
function explorateur(el){
  const e = IC.exp;
  if(!e.cols){ try{ e.cols = JSON.parse(localStorage.getItem('ic-cols') || 'null'); }catch(x){} e.cols = e.cols || COLS.map(c => c[0]).filter(c => c !== 'tiers'); }
  el.innerHTML = `<section class="ic-carte l12"><h3>Explorateur des opérations</h3>
    <p class="q">Les opérations du journal de caisse derrière chaque chiffre, avec les filtres en cours.</p>
    <div class="ic-ligne"><label class="ic-champ" style="flex:1 1 240px">Rechercher<input type="search" id="ic-q" value="${esc(e.q)}" placeholder="Description, catégorie, tiers, référence…" oninput="IC.chercher(this.value)"></label>
      <label class="ic-champ">Sens<select onchange="IC.exp.sens=this.value;IC.exp.page=1;IC.chargerExp()"><option value="">Entrées et sorties</option>
        <option value="D" ${e.sens === 'D' ? 'selected' : ''}>Entrées</option><option value="C" ${e.sens === 'C' ? 'selected' : ''}>Sorties</option></select></label>
      <label class="ic-champ">Lignes<select onchange="IC.exp.taille=+this.value;IC.exp.page=1;IC.chargerExp()">${[25, 50, 100, 200].map(n => `<option ${e.taille === n ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
      <div class="ic-maj"><button class="ic-btn" onclick="IC.exporter('operations','csv')">⬇ CSV</button><button class="ic-btn" onclick="IC.exporter('operations','xlsx')">⬇ Excel</button></div></div>
    <details><summary class="ic-dim" style="cursor:pointer;font-size:13px">Colonnes affichées</summary><div class="ic-cols" style="margin-top:8px">${COLS.map(([c, l]) => `<label><input type="checkbox" ${e.cols.includes(c) ? 'checked' : ''} onchange="IC.colonne('${c}', this.checked)"> ${l}</label>`).join('')}</div></details>
    <div id="ic-exp-res">${'<div class="ic-sq" style="height:38px;margin-bottom:6px"></div>'.repeat(6)}</div></section>`;
  IC.chargerExp();
}
let _tChercher = null;
IC.chercher = v => { clearTimeout(_tChercher); _tChercher = setTimeout(() => { IC.exp.q = v.trim(); IC.exp.page = 1; IC.chargerExp(); }, 300); };
IC.colonne = (c, on) => { const e = IC.exp; e.cols = on ? [...new Set([...e.cols, c])] : e.cols.filter(x => x !== c);
  try{ localStorage.setItem('ic-cols', JSON.stringify(e.cols)); }catch(x){} rendreExp(); };
IC.trier = c => { const e = IC.exp; if(e.tri === c) e.ordre = e.ordre === 'asc' ? 'desc' : 'asc'; else { e.tri = c; e.ordre = c === 'date' || c === 'montant' ? 'desc' : 'asc'; } e.page = 1; IC.chargerExp(); };
IC.pageExp = n => { IC.exp.page = n; IC.chargerExp(); };
IC.chargerExp = async function(){
  const e = IC.exp, res = document.getElementById('ic-exp-res');
  if(!res) return;
  res.classList.add('ic-charge');
  try{
    e.data = await GET('/intelligence/operations?' + requete({q:e.q, sens:e.sens, tri:e.tri, ordre:e.ordre, page:e.page, taille:e.taille}));
    rendreExp();
  }catch(x){ res.innerHTML = `<div class="ic-err">${esc(x.message)} <button class="ic-btn" onclick="IC.chargerExp()">Réessayer</button></div>`; }
  finally{ res.classList.remove('ic-charge'); }
};
function rendreExp(){
  const e = IC.exp, d = e.data, res = document.getElementById('ic-exp-res');
  if(!d || !res) return;
  if(!d.total){ res.innerHTML = vide('Aucune opération ne correspond. Élargissez la période ou retirez des filtres.'); return; }
  const tri = {date:'date', type_lib:'type', compte:'compte', entree:'montant', sortie:'montant'};
  const cols = COLS.filter(([c]) => e.cols.includes(c));
  const val = (l, c) => c === 'date' ? fr(l.date) : c === 'entree' ? (l.sens === 'D' && l.type !== 'depense_pompiste' ? G(l.montant) : '') :
    c === 'sortie' ? (l.sens === 'C' || l.type === 'depense_pompiste' ? G(l.montant) : '') : esc(l[c] || '');
  res.innerHTML = `<div class="ic-pagin" style="margin-bottom:8px"><span><b>${N(d.total)}</b> opération(s) · entrées ${G(d.somme_entrees)} · sorties ${G(d.somme_sorties)}</span></div>
    <div class="ic-tab-wrap"><table class="ic-tab"><thead><tr>${cols.map(([c, l]) => tri[c] ? `<th class="tri ${['entree', 'sortie'].includes(c) ? 'num' : ''}" onclick="IC.trier('${tri[c]}')" aria-sort="${e.tri === tri[c] ? (e.ordre === 'asc' ? 'ascending' : 'descending') : 'none'}">${l}${e.tri === tri[c] ? (e.ordre === 'asc' ? ' ▲' : ' ▼') : ''}</th>` : `<th>${l}</th>`).join('')}</tr></thead>
    <tbody>${d.lignes.map(l => `<tr>${cols.map(([c]) => `<td class="${['entree', 'sortie'].includes(c) ? 'num' : ''} ${c === 'entree' ? 'ic-pos' : c === 'sortie' ? 'ic-neg' : ''}">${val(l, c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
    <div class="ic-pagin" style="margin-top:8px"><span>Page ${d.page} sur ${d.pages}</span><span style="display:flex;gap:6px">
      <button class="ic-btn" ${d.page <= 1 ? 'disabled' : ''} onclick="IC.pageExp(${d.page - 1})">← Précédente</button>
      <button class="ic-btn" ${d.page >= d.pages ? 'disabled' : ''} onclick="IC.pageExp(${d.page + 1})">Suivante →</button></span></div>`;
}

// ── Analyses et rapports ────────────────────────────────────────────
function analyses(el, d){
  el.innerHTML = `<section class="ic-carte l12"><h3>Analyses</h3><p class="q">Constats calculés par des règles fixes sur vos données réelles (variation ≥ 10 % du revenu, ≥ 15 % des charges, jours à plus de 2 écarts-types de la moyenne, part ≥ 60 %…). Aucun texte inventé.</p>
    ${d.analyses.length ? `<div class="ic-ins">${d.analyses.map(insight).join('')}</div>` : vide('Aucun constat particulier sur cette période avec ces filtres.')}</section>`;
}
function rapports(el, d){
  const f = Object.entries(d.filtres);
  el.innerHTML = `<section class="ic-carte l12"><h3>Rapports et exports</h3>
    <p class="q">Chaque export reprend la période (${fr(d.periode.debut)} → ${fr(d.periode.fin)}), la comparaison et les filtres en cours${f.length ? ' : ' + esc(f.map(([k, v]) => `${k} = ${v.join(', ')}`).join(' ; ')) : ''}. Les exports sont notés dans le journal d'activité.</p>
    <div class="ic-exports">
      <div class="ic-exp"><h4>Synthèse de gestion</h4><p>Indicateurs, évolution, départements, charges, produits, pompistes et analyses.</p>
        <div class="b"><button class="ic-btn prim" onclick="IC.exporter('synthese','xlsx')">Excel</button><button class="ic-btn" onclick="IC.exporter('synthese','csv')">CSV</button><button class="ic-btn" onclick="IC.pdf()">PDF</button></div></div>
      <div class="ic-exp"><h4>Opérations filtrées</h4><p>Toutes les opérations du journal de caisse correspondant aux filtres.</p>
        <div class="b"><button class="ic-btn prim" onclick="IC.exporter('operations','xlsx')">Excel</button><button class="ic-btn" onclick="IC.exporter('operations','csv')">CSV</button></div></div>
      <div class="ic-exp"><h4>Rapport d'un département</h4><p>Filtrez un département (onglet Départements), puis exportez la synthèse : elle ne contient que lui.</p>
        <div class="b"><button class="ic-btn" onclick="IC.voir('departements')">Choisir un département</button></div></div>
    </div></section>`;
}
IC.exporter = async function(quoi, format){
  try{
    const hdrs = {}; const k = sessionStorage.getItem('api_key'); if(k) hdrs['X-API-Key'] = k;
    const r = await fetch('/api/intelligence/export?' + requete({quoi, format, q:quoi === 'operations' ? IC.exp.q : '', sens:quoi === 'operations' ? IC.exp.sens : ''}), {credentials:'include', headers:hdrs});
    if(!r.ok){ const j = await r.json().catch(() => ({})); throw new Error(j.detail || r.statusText); }
    const b = await r.blob(), a = document.createElement('a');
    a.href = URL.createObjectURL(b); a.download = `konekta-${quoi}-${IC.f.debut}-${IC.f.fin}.${format}`;
    document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    notify('Export prêt.', 'ok');
  }catch(e){ notify('Export impossible : ' + e.message, 'err'); }
};
IC.pdf = async function(){
  const d = IC.data, k = d.kpis, w = window.open('', '_blank');
  if(!w){ notify('Autorisez les fenêtres pour ce site afin de créer le PDF.', 'err'); return; }
  const ancien = IC.onglet; IC.voir('apercu');
  await new Promise(r => setTimeout(r, 700));
  const img = id => { const c = document.getElementById(id); try{ return c ? `<img src="${c.toDataURL('image/png')}" style="width:100%;max-height:300px;object-fit:contain">` : ''; }catch(e){ return ''; } };
  const g1 = img('ic-g-rc'), g2 = img('ic-g-ch'), g3 = img('ic-g-dep');
  IC.voir(ancien);
  const f = Object.entries(d.filtres), lig = (l, v, vr) => `<tr><td>${l}</td><td style="text-align:right">${v}</td><td style="text-align:right">${vr == null ? '' : PCT(vr)}</td></tr>`;
  w.document.write(`<!DOCTYPE html><html><head><meta charset="utf-8"><title>KONEKTA Intelligence Center</title><style>
    body{font:13px Calibri,Arial,sans-serif;color:#0f1e35;margin:28px}h1{font-size:20px;margin:0}h2{font-size:15px;margin:18px 0 6px;border-bottom:1px solid #ccd7e8;padding-bottom:3px}
    table{width:100%;border-collapse:collapse}td,th{padding:5px 6px;border-bottom:1px solid #e3e9f2;text-align:left}.m{color:#5a7898}
    .g{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media print{button{display:none}}</style></head><body>
    <h1>KONEKTA Intelligence Center — synthèse de gestion</h1>
    <p class="m">Période : ${fr(d.periode.debut)} au ${fr(d.periode.fin)}${d.comparaison ? ` · comparée au ${fr(d.comparaison.debut)} – ${fr(d.comparaison.fin)}` : ''} · stations : ${esc(d.stations_analysees.map(s => s.nom).join(', '))}<br>
    Filtres : ${f.length ? esc(f.map(([a, v]) => `${a} = ${v.join(', ')}`).join(' ; ')) : 'aucun'} · généré le ${new Date().toLocaleString('fr-FR')}</p>
    <h2>Indicateurs</h2><table><tr><th>Indicateur</th><th style="text-align:right">Valeur</th><th style="text-align:right">Variation</th></tr>
    ${lig('Revenu (ventes)', G(k.revenu), d.variations.revenu)}${lig('Charges', G(k.charges), d.variations.charges)}${lig('Résultat', G(k.resultat), d.variations.resultat)}
    ${lig('Encaissements', G(k.encaissements), d.variations.encaissements)}${lig('Décaissements', G(k.decaissements), d.variations.decaissements)}
    ${lig('Gallons vendus', GAL(k.gallons), d.variations.gallons)}${lig('Trésorerie (caisse principale, au ' + fr(d.periode.fin) + ')', G(d.tresorerie.solde_fin))}
    ${d.stock ? lig('Valeur du stock carburant', G(d.stock.valeur)) : ''}</table>
    <h2>Revenu et charges</h2>${g1}<div class="g"><div><h2>Charges</h2>${g2}</div><div><h2>Départements</h2>${g3}</div></div>
    <h2>Charges par catégorie</h2><table><tr><th>Catégorie</th><th style="text-align:right">Montant</th><th style="text-align:right">Opérations</th></tr>
    ${d.charges_par_categorie.map(c => `<tr><td>${esc(c.nom)}</td><td style="text-align:right">${G(c.montant)}</td><td style="text-align:right">${c.nb}</td></tr>`).join('')}</table>
    <h2>Produits</h2><table><tr><th>Produit</th><th style="text-align:right">Gallons</th><th style="text-align:right">Ventes</th></tr>
    ${d.produits.map(p => `<tr><td>${esc(p.nom)}</td><td style="text-align:right">${GAL(p.gallons)}</td><td style="text-align:right">${G(p.montant)}</td></tr>`).join('')}</table>
    <h2>Analyses</h2>${d.analyses.length ? d.analyses.map(a => `<p><b>${esc(a.titre)}</b> — ${esc(a.explication)} <span class="m">(${esc(a.preuve)})</span></p>`).join('') : '<p class="m">Aucun constat particulier.</p>'}
    <button onclick="print()">Imprimer / enregistrer en PDF</button></body></html>`);
  w.document.close();
  setTimeout(() => { try{ w.print(); }catch(e){} }, 500);
};

// ── Exploration (panneau de détail) ─────────────────────────────────
IC.ouvrir = function(titre, sous, html){
  document.getElementById('ic-p-titre').textContent = titre;
  document.getElementById('ic-p-sous').textContent = sous || '';
  document.getElementById('ic-p-corps').innerHTML = html;
  const p = document.getElementById('ic-panneau'); p.classList.add('show'); p.setAttribute('aria-hidden', 'false');
  p.querySelector('header button').focus();
};
IC.fermer = () => { const p = document.getElementById('ic-panneau'); if(p){ p.classList.remove('show'); p.setAttribute('aria-hidden', 'true'); } };
IC.filtrer = function(k, v){
  if(!IC.f[k].includes(v)) IC.f[k] = [...IC.f[k], v];
  IC.fermer(); IC.voir('apercu', true); IC.appliquer();
  notify(`Filtre appliqué : ${libelle(k, v)}`, 'ok');
};
IC.drill = function(quoi, v){
  if(quoi === 'revenu'){ IC.f.types = ['vente_carburant', 'vente_bar', 'vente_cuisine', 'vente_hotel'].filter(t => options('types').some(o => o[0] === t)); }
  if(quoi === 'entrees'){ IC.exp.sens = 'D'; }
  if(quoi === 'categorie'){ IC.f.comptes = [v]; }
  garder(); IC.exp.page = 1; rendreFiltres(); IC.voir('explorateur', true); IC.actualiser();
};
IC.jourDetail = async function(date){
  IC.ouvrir(`Le ${fr(date)}`, 'Relevés de compteurs validés et opérations de caisse du jour', '<div class="ic-sq" style="height:200px"></div>');
  try{
    const extra = {}; if(IC.f.stations.length) extra.stations = IC.f.stations.join(',');
    const q = new URLSearchParams(Object.assign({debut:date}, extra, IC.f.produits.length ? {produits:IC.f.produits.join('|')} : {}));
    const [r, o] = await Promise.all([GET('/intelligence/releves?' + q), GET('/intelligence/operations?' + (() => { const p = new URLSearchParams(requete({taille:100})); p.set('debut', date); p.set('fin', date); return p; })())]);
    document.getElementById('ic-p-corps').innerHTML = `<div class="ic-kpis" style="grid-template-columns:1fr 1fr"><div class="ic-kpi"><span class="t">Gallons</span><span class="v">${GAL(r.gallons)}</span></div>
      <div class="ic-kpi"><span class="t">Ventes carburant</span><span class="v">${G(r.montant)}</span></div></div>
      <h3 style="margin:0;font-size:15px">Relevés</h3>${r.releves.length ? `<div class="ic-tab-wrap"><table class="ic-tab"><thead><tr><th>Créneau</th><th>Mètre</th><th>Produit</th><th>Pompiste</th><th class="num">Gallons</th><th class="num">Montant</th></tr></thead><tbody>
      ${r.releves.map(x => `<tr><td>${x.periode === 'Matin' ? 'Matin' : 'Après-midi'}</td><td>${esc(x.pompe)}</td><td>${esc(x.produit)}</td><td>${esc(x.pompiste || 'Bureau')}</td><td class="num">${GAL(x.gallons)}</td><td class="num">${G(x.montant)}</td></tr>`).join('')}</tbody></table></div>` : vide('Aucun relevé validé ce jour.')}
      <h3 style="margin:0;font-size:15px">Opérations de caisse</h3>${o.lignes.length ? `<div class="ic-tab-wrap"><table class="ic-tab"><thead><tr><th>Type</th><th>Catégorie</th><th>Description</th><th class="num">Montant</th></tr></thead><tbody>
      ${o.lignes.map(l => `<tr><td>${esc(l.type_lib)}</td><td>${esc(l.compte || '')}</td><td>${esc(l.description || '')}</td><td class="num ${l.sens === 'D' && l.type !== 'depense_pompiste' ? 'ic-pos' : 'ic-neg'}">${G(l.montant)}</td></tr>`).join('')}</tbody></table></div>` : vide('Aucune opération ce jour.')}
      <button class="ic-btn" onclick="goTo('journal')">Ouvrir le Journal Meters</button>`;
  }catch(e){ document.getElementById('ic-p-corps').innerHTML = `<div class="ic-err">${esc(e.message)}</div>`; }
};
IC.produitDetail = function(p){
  const tot = IC.data.produits.reduce((a, x) => a + x.montant, 0) || 1;
  IC.ouvrir(p.nom, 'Ventes et volumes du produit sur la période',
    `<div class="ic-kpis" style="grid-template-columns:1fr 1fr">
      <div class="ic-kpi"><span class="t">Ventes</span><span class="v">${G(p.montant)}</span>${badge(p.variation)}<span class="c">${Math.round(p.montant / tot * 100)} % des ventes carburant</span></div>
      <div class="ic-kpi"><span class="t">Gallons</span><span class="v">${GAL(p.gallons)}</span><span class="c">${p.nb} relevés${p.gallons_prec != null ? ' · comparaison ' + GAL(p.gallons_prec) : ''}</span></div></div>
    <div class="ic-graph h1"><canvas id="ic-g-pdet"></canvas></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="ic-btn prim" onclick="IC.filtrer('produits', '${p.id}')">Analyser ce produit</button>
      <button class="ic-btn" onclick="goTo('stock')">Voir le stock</button></div>`);
  graph('ic-g-pdet', {type:'bar', data:{labels:IC.data.series.map(x => etiq(x.cle, IC.data.granularite)), datasets:[{label:'Gallons', data:p.series, backgroundColor:palette()[0], borderRadius:3}]},
    options:{plugins:{legend:{display:false}, tooltip:{callbacks:{label:c => GAL(c.parsed.y)}}}, scales:{y:{ticks:{color:css('--muted')}}, x:{ticks:{color:css('--muted'), maxTicksLimit:10}, grid:{display:false}}}}});
};
})();
