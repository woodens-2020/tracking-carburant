/*
 * Traduction des messages d'erreur renvoyés par le serveur (FastAPI), côté
 * frontend uniquement — aucune modification du backend nécessaire.
 *
 * Le backend renvoie toujours du texte en français (ex. "Rôle introuvable").
 * Cette table associe chaque message connu à ses traductions EN/ES. Un
 * message non catalogué est simplement renvoyé tel quel (repli gracieux,
 * même philosophie que t() dans index.html) — jamais d'erreur, jamais de
 * texte vide.
 *
 * Deux niveaux :
 *   1. EXACT   — correspondance exacte, pour les messages statiques.
 *   2. PATTERNS — regex + template, pour les messages contenant une valeur
 *      variable (montant, nom, identifiant...).
 *
 * Catalogue alimenté progressivement (voir plan "Traduction complète
 * FR/EN/ES" — étape "Catalogue des erreurs backend").
 */
(function(){
  var EXACT = {
    'Email, mot de passe ou code d\'accès incorrect': {
      en: 'Incorrect email, password or access code',
      es: 'Correo, contraseña o código de acceso incorrectos',
    },
    'Session expirée': {
      en: 'Session expired',
      es: 'Sesión expirada',
    },
  };

  // { re: RegExp, en: fn(match)->string, es: fn(match)->string }
  var PATTERNS = [];

  function translateApiError(rawMsg, lang){
    lang = lang || (window.currentLang || 'fr');
    if(lang === 'fr' || !rawMsg) return rawMsg;

    var exact = EXACT[rawMsg];
    if(exact && exact[lang]) return exact[lang];

    for(var i = 0; i < PATTERNS.length; i++){
      var p = PATTERNS[i];
      var m = rawMsg.match(p.re);
      if(m && typeof p[lang] === 'function') return p[lang](m);
    }
    return rawMsg;
  }

  window.translateApiError = translateApiError;
})();
