# Smart Meter Intelligence & Reconciliation Engine — Audit, diagnostic et proposition

*Étapes 1 à 3 de la mission. Rédigé le 09/11/2026 sur la branche `staging-cpv` (identique à la production konekta-cpv.com, plus deux changements d'interface en attente). Aucun code n'a été modifié pour cet audit.*

---

## 1. Architecture actuelle

| Élément | Où | Remarques |
|---|---|---|
| Application | FastAPI (`backend/main.py` + routeurs) ; interface = un fichier HTML servi tel quel (`frontend/index.html` pour la direction, `frontend/pompiste.html` pour les pompistes) | Pas de Flask. PostgreSQL en production, SQLite pour les tests. |
| Stations | Un schéma PostgreSQL par station (`esp_1` = Champin…), choisi par le cookie `konekta_espace` (`EspaceMiddleware`) | Toutes les recherches sont déjà limitées à la station par construction. |
| Pompe physique | `Distributeur` (ex. « Vertières 1 ») → `Face` | |
| **Pistolet / meeter** | `Pompe` (ex. « Gazoline 1 · Gazoline__91 ») : `produit_id`, `face_id`, `metre_depart`, `date_depart`, `actif` | Le « pistolet » de la mission est la table `pompes`. |
| Relevé officiel | `Releve` : `date`, `periode` (Matin / Apres-midi), `pompe_id`, `metter_avant`, `metter_apres`, `prix_gallon`, `pompiste_id` (employé), `ecart_compteur`, `motif_ecart_compteur`, `nb_modifications`, `pompe_saisie_id`, `metter_avant_saisi`, `metter_apres_saisi`, `rapport_pompiste_id` | Contrainte unique `uq_releve (date, periode, pompe_id)`. |
| Rapport pompiste | `RapportPompiste` (statut `EN_COURS → SOUMIS → PROBLEME / CONFIRME / RESOLU / REJETE`, corbeille `supprime_at`) + `RapportPompisteLigne` (index saisis, `*_corrige`, `confirmee`, `pompe_saisie_id`) | Un rapport n'entre au journal qu'après confirmation ligne par ligne par la direction. |
| Mémoire des erreurs | `ErreurMetreApprise` (pompiste × meeter saisi → bon meeter, nombre de fois) | Apprend à chaque correction de la direction. |
| Audit | `AuditLog` via `main._tracer(...)` (qui, quand, détails avant → après) | Utilisé par les corrections, suppressions, changements de date et de pompe. |

### Comment le système détermine aujourd'hui le dernier meeter
`pompiste_routes._index_precedent(pompe, date, période)` :
1. le dernier `Releve` du journal **avant** ce créneau (après le point de départ du meeter s'il existe) ;
2. sinon / ou plus récent : une ligne de rapport pompiste **en attente** des 3 derniers jours — seulement si elle peut suivre la référence fiable (pas de recul, pas de saut > 1 500 gal ; garde-fou ajouté le 07/11) ;
3. sinon le **meeter de départ** de la pompe (`metre_depart`).

### Moteur de rapprochement déjà en place (`backend/placement_metres.py`)
Il couvre déjà une bonne partie de la mission, **côté direction** (à la vérification du rapport) :
- score par meeter candidat : reprise exacte de l'index (100), faute de frappe d'un chiffre (55), à moins de 2 gal (40), volume habituel du meeter sur 90 jours (+5 / −25), autre carburant (−40, seulement sur reprise exacte), autre station (−30), propane jamais avec carburant, habitude du pompiste (+25 max) ;
- placement **global** de toutes les lignes d'un rapport (échanges, rotations) ;
- pistes hors créneau : index qui s'enchaînent ailleurs (±7 jours, journal, rapports en attente, corbeille) → rapport à restaurer, mauvaise date ;
- application d'un geste, valeurs saisies d'origine **conservées**, erreur apprise ;
- registre des erreurs de meeter par pompiste (Journal Meeters).

### Contrôles côté serveur à la saisie (`_controler_lignes`)
Pompe active et de la station du pompiste ; pas de doublon dans l'envoi ; index ≥ 0 ; **fin ≥ début (refusé sinon)** ; **> 5 000 gal sur une période refusé** ; début ≠ dernier index connu → le pompiste doit relire et confirmer (`debut_different`) — **sans que la valeur attendue lui soit montrée**.

### Statistiques officielles
`main.releves_comptes()` est la règle unique de TOUS les totaux d'argent (journal, rapport journalier, caisse, tableaux de bord, rentabilité) : seuls les relevés du journal comptent, et un saut / recul de compteur non confirmé par la direction est exclu. Les rapports pompistes en attente n'y entrent jamais.

---

## 2. Diagnostic

### Ce qui fonctionne et sera réutilisé
1. Le modèle de données (stations, pompes/meeters, relevés, rapports, lignes, mémoire des erreurs, audit).
2. Le moteur de score et de placement (`placement_metres.py`) : il devient le cœur du « Smart Meter Engine ».
3. La conservation des valeurs saisies (`pompe_saisie_id`, `*_saisi`) et le registre des erreurs.
4. La séparation données brutes (lignes de rapport) / données en attente / données validées (journal) / exclues (sauts non confirmés) / supprimées (corbeille).
5. Le point de départ des meeters (`metre_depart`, `date_depart`) comme base d'un changement de compteur.

### Problèmes constatés
| # | Problème | Conséquence |
|---|---|---|
| P1 | Le rapprochement n'intervient **qu'au moment où la direction vérifie** le rapport, jamais pendant la saisie du pompiste. | Le pompiste envoie sur la mauvaise pompe sans le savoir ; l'erreur n'est vue qu'après. |
| P2 | Le résultat du rapprochement n'est **pas enregistré** : il est recalculé à chaque affichage. Aucun statut de rapprochement, aucun niveau de confiance, aucune décision du pompiste ne sont stockés. | Impossible de filtrer « rapports sans correspondance », de suivre les décisions, d'auditer ce que le système proposait. |
| P3 | Pas de niveaux HIGH / MEDIUM / LOW / NO MATCH ni de seuils configurables : seuils codés en dur (`SEUIL=45`, `MARGE=20`). | |
| P4 | Pas de **repli « meeter le plus proche »** : quand aucune reprise n'est fiable, aucune proposition n'est faite (cas des rapports n°19/20 du 06/11). | |
| P5 | Consommation nulle (début = fin) non signalée. Limite de volume unique pour toutes les pompes (5 000 gal), non configurable par meeter. | |
| P6 | Changement de compteur : seul un « meeter de départ » existe ; pas d'ancien/nouveau numéro de compteur, d'ancien relevé final, de motif ni de responsable. | Un remplacement ressemble à un saut ou un recul. |
| P7 | **Concurrence** : la règle « une pompe = un rapport actif par créneau » est vérifiée en Python sans verrou. Deux pompistes qui envoient au même instant la même pompe peuvent tous deux passer. | Doublon possible (rare). |
| P8 | Calculs de volumes et montants en `float` Python (stockés en `Numeric`). | Arrondis possibles au 1/1000. |
| P9 | Le nouveau « Ventes par pompiste » (07/11) totalise **tous** les relevés, y compris les sauts non confirmés, au lieu de `releves_comptes()`. | Incohérence avec les totaux officiels — à corriger. |
| P10 | Tableau de suivi : le registre des erreurs existe (Journal Meeters) mais sans filtres par statut de rapprochement ni de validation, et sans historique des interventions par rapport. | |

### Données (Champin, production)
Historique suffisant : relevés continus depuis le 30/10, 8 meeters de carburant + propane. Incohérences connues : rapports n°16-18 du 06/11 à la corbeille alors que n°19-21 s'enchaînent dessus ; 2 petits écarts d'index (30/10, 31/10) ; Myrtha a 3 fiches employé.

### ⚠ Point de conflit avec une règle de sécurité existante
Le serveur est écrit pour que **le pompiste ne voie jamais le dernier index enregistré** (anti-copie : il doit lire le compteur réel ; un index lu honnêtement révèle les gallons manquants entre deux pompistes). La mission demande d'afficher au pompiste « Dernier Meter enregistré : 238600 ». **Afficher cette valeur supprime ce contrôle.** Décision à prendre par la direction (voir § 4).

---

## 3. Proposition (modèle de données et règles)

### 3.1 Moteur `Smart Meter` (backend, réutilise `placement_metres.py`)
`analyser_index(station, pompiste, date, période, meeter choisi, index début [, index fin])` → candidats classés :

| Niveau | Règle (seuils dans `config_rapprochement`, modifiables par la direction) |
|---|---|
| HIGH | reprise exacte (≤ 0,001 gal) de la référence fiable du meeter, volume dans la limite du meeter |
| MEDIUM | reprise à un chiffre près, ou écart ≤ `ecart_medium` (défaut 2 gal), ou exacte mais volume inhabituel |
| LOW | écart ≤ `ecart_low` (défaut 50 gal) et même carburant |
| NO_MATCH | rien de tout cela → **repli : meeter numériquement le plus proche** (même carburant, même station), présenté comme « référence provisoire, vérification nécessaire » |
| INITIAL_READING | aucun historique ni meeter de départ pour le meeter |

Référence fiable = dernier relevé **validé** du journal ; une ligne en attente ne sert de référence que si elle suit cette base (règle déjà en place). Le propane ne se rapproche jamais du carburant.

### 3.2 Nouvelles colonnes (aucune table existante modifiée ni donnée supprimée)
Sur `rapports_pompiste_lignes` (ajout de colonnes nullable, migrations SQLite + PostgreSQL + espaces) :
- `statut_rapprochement` : `MATCH_CONFIRMED | MATCH_REVIEW | UNMATCHED | INITIAL_READING`
- `confiance` : `HIGH | MEDIUM | LOW | NONE`, `score`
- `pompe_proposee_id`, `index_reference`, `source_reference`, `ecart_reference` (Numeric)
- `decision_pompiste` : `ACCEPTE | REFUSE | AUCUNE` (+ horodatage)
- `anomalies` (JSON : `ECART_POSITIF`, `RECUL`, `VOLUME_ELEVE`, `VOLUME_NUL`, `COMPTEUR_CHANGE`…)

Le **statut de validation** reste celui du rapport existant (`SOUMIS` = en attente, `CONFIRME/RESOLU` = vérifié, `REJETE`, corbeille) — pas de second circuit de validation concurrent.

Nouvelle table `changements_compteur` : `pompe_id`, ancien / nouveau numéro, ancien relevé final, nouveau relevé initial, date, motif, autorisé par, créé le. Elle alimente le point de départ (`metre_depart`) déjà utilisé partout.

Nouvelle table `config_rapprochement` (une ligne par station) : seuils et limite de volume par meeter (`volume_max_periode`, défaut = règle actuelle 5 000 gal).

### 3.3 Saisie du pompiste (`pompiste.html`)
Pendant la saisie du début : appel `POST /api/pompiste/analyser-index` (calcul **côté serveur**) → bandeau :
- HIGH sur le meeter choisi : rien à faire ;
- meilleur candidat ailleurs : « Ce meeter ressemble à **Gazoline 2 · Gazoline_91** — confirmer / garder mon choix » ;
- plusieurs candidats : liste à choisir ;
- NO_MATCH : alerte + meeter le plus proche proposé, envoi toujours possible.
La décision est enregistrée sur la ligne ; le rapport s'enregistre toujours, mais une ligne non HIGH reste « à vérifier » et n'entre jamais seule dans les totaux officiels (règle actuelle conservée).

### 3.4 Tableau de réconciliation (direction)
Nouvel onglet dans **Pompistes** : filtres station (station courante), distributeur, meeter, pompiste, dates, type d'anomalie, statut de rapprochement, statut de validation ; actions = ouvrir le rapport, comparer avec les lectures précédentes, confirmer l'attribution, réattribuer (réutilise « Changer de pompe » / « Appliquer le placement »), rejeter avec motif, note de vérification, historique des interventions (journal d'audit filtré sur le rapport).

### 3.5 Sécurité et intégrité
- Verrou transactionnel PostgreSQL (`pg_advisory_xact_lock` sur date + période + meeter) à l'envoi des compteurs → P7 ;
- calculs de volumes et montants du moteur en `Decimal` → P8 ;
- « Ventes par pompiste » sur `releves_comptes()` → P9 ;
- valeurs saisies jamais écrasées (déjà le cas), chaque décision tracée dans `AuditLog`.

### 3.6 Tests
Les 17 scénarios de la mission, en tests d'intégration FastAPI (même cadre que les 200 tests existants), plus le passage complet de la suite actuelle.

---

## 4. Décisions à prendre avant l'implémentation
1. **Montrer le dernier index au pompiste ?** (conflit § 2)
   - *Recommandé* : le pompiste voit le **meeter proposé et le niveau de confiance**, jamais la valeur du dernier index ni l'écart chiffré ; la direction voit tout.
   - Ou : afficher tout comme dans la mission (le contrôle anti-copie disparaît).
2. **Fin < début ou volume > limite** : aujourd'hui l'envoi est refusé (faute de frappe dans la même ligne). Proposition : garder ce refus pour fin < début (physiquement impossible) ; pour un volume au-dessus de la limite, accepter avec anomalie `VOLUME_ELEVE` au lieu de refuser.

---

## 5. Décisions de la direction (09/11/2026)
1. **Le pompiste voit tous les chiffres** pendant la saisie (dernier meeter, nouveau meeter, écart, confiance). Le contrôle « anti-copie » est donc levé : le pompiste peut recopier l'index affiché. Les écarts restent détectés par le journal et la réconciliation.
2. **Volume au-dessus de la limite : enregistré avec l'anomalie `VOLUME_ELEVE`** (plus refusé). Fin < début reste refusé (physiquement impossible, contrainte en base).

## 6. Ce qui est implémenté (étapes 4 à 8)

### Moteur — `backend/smart_meter.py`
- `analyser()` : référence fiable de chaque meeter (journal validé → ligne en attente qui suit le journal → point de départ ; rien d'inventé sans historique), écart en `Decimal`, niveau HIGH / MEDIUM / LOW / NONE selon `config_rapprochement`, classement (niveau, meeter choisi, mémoire des erreurs du pompiste, écart), statut, recommandation, candidats (`ambigu` si plusieurs au même niveau), repli `plus_proche` (même carburant, présenté comme référence provisoire), anomalies du meeter choisi. Propane jamais rapproché du carburant ; changement de carburant seulement sur reprise exacte ; meeters déjà rapportés sur le créneau exclus.
- À l'envoi des compteurs (`pompiste_routes.envoyer_compteurs` / `modifier_compteur`) : l'analyse est **recalculée côté serveur** et figée sur la ligne (`statut_rapprochement`, `confiance`, `pompe_proposee_id`, `index_reference`, `source_reference`, `ecart_reference`, `decision_pompiste`, `anomalies`). Si le pompiste a pris le meeter proposé, le meeter choisi au départ est gardé (`pompe_saisie_id`) et l'erreur est apprise.
- Verrou `pg_advisory_xact_lock` par jour + créneau + meeter à l'envoi (PostgreSQL ; sans effet sur SQLite).

### API
| Méthode | Chemin | Qui | Rôle |
|---|---|---|---|
| POST | `/api/pompiste/analyser-index` | pompiste | analyse pendant la saisie |
| GET / PUT | `/api/smart-meter/config` | direction | seuils (exact ≤ moyen ≤ faible), volume max par défaut et par meeter |
| GET / POST | `/api/smart-meter/changements-compteur` | direction | compteur remplacé / remis à zéro (refusé si un relevé existe déjà à cette date ou après) |
| GET | `/api/smart-meter/reconciliation` | direction | lignes + filtres (dates, pompe, meeter, pompiste, anomalie, rapprochement, validation) + compteurs |
| POST | `/api/smart-meter/rapports/{id}/lignes/{pompe}/verifier` | direction | note de vérification (qui, quand, note) |
| GET | `/api/smart-meter/rapports/{id}/historique` | direction | interventions tracées dans le journal d'audit |

Réattribuer = outils existants (« Changer de pompe », « Appliquer le placement ») ; rejeter = « Renvoyer au pompiste » existant (motif obligatoire).

### Interfaces
- **Espace pompiste** : bandeau sous les deux cases d'index (kreyòl / français) : meeter reconnu, ou meeter proposé avec « Pran … » / « Kenbe chwa mwen », ou plusieurs candidats, ou aucune correspondance avec le plus proche, ou première lecture ; anomalies affichées ; l'envoi n'est jamais bloqué. Vérifié dans Edge (sans fenêtre) sur un serveur local.
- **Direction** : Rapports pompistes → onglet « 🔎 Réconciliation des meeters » : filtres, compteurs cliquables, tableau (saisi / retenu / référence / écart / rapprochement / décision / anomalies / validation), actions Ouvrir / 📝 Vérifié / Historique, seuils du moteur, compteurs remplacés. Vérifié dans Edge.

### Totaux officiels
Inchangés : seuls les relevés du journal validés comptent (`main.releves_comptes`). « Ventes par pompiste » utilise désormais cette même règle (P9 corrigé).

### Tests — `backend/tests/test_smart_meter.py` (14 tests, 17 scénarios)
1 bon pistolet · 2 mauvais pistolet (index exact d'un autre) · 3 plusieurs candidats · 4-5 aucune correspondance + plus proche · 6 le pompiste prend la proposition · 7 il la refuse · 8 recul · 9 volume élevé · 10 volume nul · 11 sans historique · 12 compteur remplacé · 13-14 double soumission / même pistolet par deux pompistes · 15-16-17 correction par la direction, rapport en attente hors totaux, valeurs d'origine conservées · seuils et droits. Suite complète : 214 réussis ; 1 échec ancien sans rapport (`test_auth::TestOAuth::test_callback_known_email_creates_session`).

### Limites connues
- La simultanéité réelle (deux envois à la même milliseconde) n'est testable que sur PostgreSQL : le verrou est en place, le test automatique vérifie l'envoi successif.
- Le moteur calcule en `Decimal` ; les totaux historiques de l'application restent calculés comme avant.
- Le rapprochement est figé à l'envoi : les lignes envoyées avant cette version apparaissent « Saisi avant le moteur ».

## 7. Fichiers
- Créés : `backend/smart_meter.py`, `backend/tests/test_smart_meter.py`, `docs/smart-meter-audit.md`.
- Modifiés : `backend/models.py` (12 colonnes sur `rapports_pompiste_lignes`, tables `changements_compteur`, `config_rapprochement`), `backend/database.py` et `backend/main.py` (migrations SQLite / PostgreSQL), `backend/pompiste_routes.py` (envoi, verrou, volume élevé accepté, champs dans les rapports), `backend/classement_pompistes.py` (totaux officiels), `backend/tests/test_pompiste.py` (volume élevé désormais accepté), `frontend/pompiste.html`, `frontend/index.html`.
- Supprimés : aucun.
