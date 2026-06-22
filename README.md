# Suivi des Meters — Station

Application web de suivi des compteurs (meters) d'une station de carburant.
Backend FastAPI + base de données SQLite + frontend HTML/JS.

## Ce que fait le système

- Saisie des relevés par **produit** (Gazoline, Diesel, …), par **période** (Matin / Après-midi) et par **pompe**.
- Calcul automatique : `quantité = meter après − meter avant`, `montant = quantité × prix gallon`.
- Suivi des **dépenses** par produit/période.
- **Dashboard** : total ventes, total dépenses, montant disponible.
- **Historique multi-jours** : changez la date en haut pour consulter/saisir n'importe quel jour.
- **Flexible** : ajoutez autant de produits et de pompes que nécessaire (bouton « Gérer produits / pompes »).
- Toutes les données sont stockées en base (`backend/station.db`).

## Installation (une seule fois)

Pré-requis : Python 3.10+.

```bash
cd backend
pip install -r requirements.txt
```

## Lancer l'application

```bash
cd backend
uvicorn main:app --reload --port 8000
```

Puis ouvrez votre navigateur sur :  **http://127.0.0.1:8000**

## Structure du projet

```
station/
├── backend/
│   ├── main.py        # API REST (FastAPI) + sert le frontend
│   ├── models.py      # Schéma de la base (Produit, Pompe, Releve, Depense)
│   ├── database.py    # Connexion SQLite + données de départ
│   ├── requirements.txt
│   └── station.db     # base de données (créée au 1er lancement)
└── frontend/
    └── index.html     # interface web complète
```

## Modèle de données

- **Produit** : nom + prix gallon par défaut. Possède plusieurs pompes.
- **Pompe** : rattachée à un produit (nombre variable).
- **Releve** : 1 par (date, période, pompe) — prix, meter avant, meter après.
- **Depense** : (date, période, produit) — description + montant.

## Passer à PostgreSQL plus tard

Le code utilise SQLAlchemy. Pour migrer, changez seulement `DATABASE_URL`
dans `database.py` (ex : `postgresql://user:pass@localhost/station`) — aucune
autre ligne à modifier.

## Chatbot de rapports (assistant IA)

L'onglet « 📊 Rapports / Chatbot » permet de générer des rapports en langage
naturel. Exemples :
  - « Donne-moi le rapport du 1er au 15 avril 2026 »
  - « Combien de gallons de Diesel vendus cette semaine ? »
  - « Quelle pompe a vendu le plus ce mois-ci ? »

Le chatbot n'invente jamais de chiffres : il interroge la base via l'endpoint
`/api/stats` (source de vérité) grâce au mécanisme de *tool use* de l'API Claude.

### Configurer la clé API Claude

La clé est lue depuis la variable d'environnement `ANTHROPIC_API_KEY`
(jamais codée en dur). Obtenez une clé sur https://console.anthropic.com

Linux / macOS :
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
uvicorn main:app --reload --port 8000
```

Windows (PowerShell) :
```powershell
$env:ANTHROPIC_API_KEY="sk-ant-..."
uvicorn main:app --reload --port 8000
```

Sans clé configurée, l'app fonctionne normalement mais le chatbot affiche un
message indiquant que la clé est manquante.

### Endpoints ajoutés

- `GET /api/stats?date_debut=...&date_fin=...&produit_id=...&periode=...`
  → totaux (quantité, montant), détail par produit / pompe / période.
- `POST /api/chat` body `{ "message": "...", "historique": [...] }`
  → réponse de l'assistant + historique mis à jour.

### Tester rapidement

```bash
# stats brutes (sans clé API)
curl "http://127.0.0.1:8000/api/stats?date_debut=2026-04-01&date_fin=2026-04-30"

# chatbot (nécessite ANTHROPIC_API_KEY)
curl -X POST http://127.0.0.1:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"rapport du mois d avril 2026","historique":[]}'
```
