// Vérifie que chaque bloc <script> inline des pages HTML se compile.
// Une seule faute de syntaxe dans index.html casse toute l'interface :
// ce contrôle tourne en CI avant tout déploiement.
//
//   node scripts/verifier_frontend.js
const fs = require("fs");
const path = require("path");

const pages = ["frontend/index.html", "frontend/login.html", "frontend/pompiste.html", "frontend/pompiste-app/installer.html"];
let erreurs = 0;

for (const page of pages) {
  const html = fs.readFileSync(path.join(__dirname, "..", page), "utf8");
  const re = /<script(\s[^>]*)?>([\s\S]*?)<\/script>/gi;
  let m, n = 0;
  while ((m = re.exec(html))) {
    const attrs = m[1] || "";
    if (/\bsrc=/.test(attrs) || /type=["'](?!text\/javascript|module)/i.test(attrs)) continue;
    n++;
    const ligne = html.slice(0, m.index).split("\n").length;
    try {
      new Function(m[2]);
    } catch (e) {
      erreurs++;
      console.error(`✗ ${page} — script commençant ligne ${ligne} : ${e.message}`);
    }
  }
  console.log(`${page} : ${n} blocs <script> vérifiés`);
}

if (erreurs) {
  console.error(`${erreurs} erreur(s) de syntaxe JavaScript`);
  process.exit(1);
}
console.log("Interface OK");
