# Module de Génération de Rapports — PétroSync

Génère un rapport professionnel téléchargeable sur une période donnée,
dans trois formats : **PDF**, **Word (.docx)** ou **Excel (.xlsx)**.

Toutes les valeurs proviennent de la base de données — aucun chiffre inventé.

---

## Endpoint

```
GET /api/rapport/export
```

### Paramètres

| Paramètre    | Type   | Requis | Description                             |
|-------------|--------|--------|-----------------------------------------|
| `date_debut` | string | ✓      | Date de début au format `YYYY-MM-DD`    |
| `date_fin`   | string | ✓      | Date de fin au format `YYYY-MM-DD`      |
| `format`     | string |        | `pdf` (défaut), `docx` ou `xlsx`        |
| `produit_id` | int    |        | Filtre sur un produit spécifique        |

### Authentification

Requiert une session active (cookie `session_token`) **ou** une clé API (`X-API-Key`).

### Exemple

```bash
# PDF sur juin 2026
curl -H "X-API-Key: knt_..." \
  "http://localhost:8001/api/rapport/export?date_debut=2026-06-01&date_fin=2026-06-30&format=pdf" \
  -o rapport_juin2026.pdf

# Excel du mois courant
curl -H "X-API-Key: knt_..." \
  "http://localhost:8001/api/rapport/export?date_debut=2026-06-01&date_fin=2026-06-21&format=xlsx" \
  -o rapport.xlsx

# Word sur 7 jours (filtré gazoline)
curl -H "X-API-Key: knt_..." \
  "http://localhost:8001/api/rapport/export?date_debut=2026-06-14&date_fin=2026-06-21&format=docx&produit_id=1" \
  -o rapport.docx
```

### Nom de fichier

Le fichier téléchargé est automatiquement nommé :
```
rapport_station_YYYY-MM-DD_YYYY-MM-DD.{pdf|docx|xlsx}
```

---

## Contenu du rapport

Le rapport est **structuré, narratif et professionnel** :

1. **Page de titre** — station, période, date de génération, KPIs
2. **Résumé exécutif** — chiffres clés + variation vs période précédente
3. **Analyse des ventes** — par produit, par pompe, par période, évolution journalière
4. **Analyse des anomalies** — compteurs + stock, détail des erreurs/avertissements
5. **Stock & Rentabilité** — niveaux actuels, alertes, bénéfice WAC si livraisons saisies
6. **Conclusion** — points forts / vigilance basés sur les données
7. **Recommandations** — suggestions concrètes dérivées des constats

### Texte narratif dynamique

Le texte s'adapte aux données réelles :
- Compare la période avec la précédente (même durée)
- Met en avant les pompes sous/sur-performantes
- Signale les alertes stock et les erreurs de saisie
- Reste honnête si des données manquent

---

## Spécificités par format

### PDF (`reportlab`)
- Document mis en page A4, marges 1.8 cm
- Page de titre + table structurée + 4 graphiques matplotlib intégrés
- Tableau KPI en couleurs, tableaux de données formatés
- Couleurs : bleu marine `#0f1e35`, ambre `#f7a93b`, teal `#3fb6a8`

### Word (.docx) (`python-docx`)
- Document éditable avec styles Heading 1/2/3 natifs
- Graphiques matplotlib intégrés en images
- Tableaux Word stylisés (fond bleu marine en-tête)
- Idéal pour retouche et annotation

### Excel (.xlsx) (`openpyxl`)
- **Feuille Synthèse** — KPIs, résumé narratif, recommandations
- **Feuille Ventes détaillées** — série journalière + graphique barres natif Excel
- **Feuille Répartition** — par produit et par pompe + camembert natif Excel
- **Feuille Anomalies** — détail avec filtre automatique, fond rouge pour erreurs
- **Feuille Stock & Rentabilité** — niveaux actuels + tableau WAC

---

## Graphiques intégrés (matplotlib)

| Graphique                   | Formats | Description                        |
|-----------------------------|---------|-------------------------------------|
| Évolution journalière       | PDF, DOCX | Barres par jour sur la période    |
| Répartition par produit     | PDF, DOCX | Camembert revenu par produit      |
| Ventes par pompe            | PDF, DOCX | Barres horizontales par pompe     |
| Anomalies par type          | PDF, DOCX | Barres si anomalies détectées     |
| Graphique barres Excel      | XLSX    | Lié aux données — mise à jour auto  |
| Camembert Excel             | XLSX    | Lié aux données — mise à jour auto  |

---

## Architecture

```
rapport_service.py      ← Collecte + narratif + graphiques matplotlib
rapport_renderers.py    ← 3 rendeurs (PDF/DOCX/XLSX) sur la même structure
main.py                 ← Endpoint GET /api/rapport/export
tests/test_rapport.py   ← 18 tests (MIME, noms, cohérence, vide, auth)
```

### Principe de séparation

```python
# Dans l'endpoint
payload   = build_report_payload(db, d_debut, d_fin, produit_id)   # données
narrative = build_narrative(payload)                                  # texte
charts    = build_charts(payload)                                     # images

data = render_pdf(payload, narrative, charts)   # ou render_docx / render_xlsx
```

Les rendeurs ne font aucun calcul métier — ils consomment uniquement le `payload`.

---

## Dépendances

| Bibliothèque   | Version | Usage                   | Statut avant ce module |
|---------------|---------|-------------------------|------------------------|
| `reportlab`   | ≥ 3.6   | Génération PDF          | Déjà installée         |
| `openpyxl`    | ≥ 3.1   | Génération Excel        | Déjà installée         |
| `python-docx` | ≥ 1.0   | Génération Word         | **Nouvellement ajoutée** |
| `matplotlib`  | ≥ 3.7   | Graphiques PNG en mémoire | **Nouvellement ajoutée** |

Installation :
```bash
pip install python-docx matplotlib
```

---

## Tests

```bash
cd backend
pytest tests/test_rapport.py -v
```

Résultat attendu : **18 tests passent**.

Couverture :
- `test_pdf_mime` / `test_docx_mime` / `test_xlsx_mime` — types MIME corrects
- `test_pdf_filename` / `test_docx_filename` / `test_xlsx_filename` — noms horodatés
- `test_invalid_format_returns_400` — format inconnu → 400
- `test_invalid_date_range_returns_400` — début > fin → 400
- `test_payload_structure` — structure du payload complète et cohérente
- `test_serie_jours_all_non_negative` — pas de montants négatifs
- `test_pdf_empty_period` / `test_docx_empty_period` / `test_xlsx_empty_period` — période vide OK
- `test_empty_period_narrative_coherent` — narratif cohérent si 0 relevé
- `test_no_auth_returns_401` — accès refusé sans auth
- `test_api_key_auth_works` — clé API acceptée
- `test_invalid_api_key_returns_401` — mauvaise clé → 401
