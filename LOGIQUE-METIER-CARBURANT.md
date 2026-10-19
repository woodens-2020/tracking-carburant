# Logique métier — station carburant (bpccp)

Ce document décrit les règles appliquées par le serveur depuis la version
« logique métier v26 ». Chaque règle est vérifiée par un test automatique
(`backend/tests/test_logique_stock.py`, `backend/tests/test_logique_metier.py`)
qui tourne avant chaque déploiement.

## 1. Stock et cargaisons

### Répartition des ventes entre cargaisons (FIFO en flux)

Les ventes (relevés de compteur) sont prises dans l'ordre chronologique :

1. Une cargaison **clôturée** garde la consommation figée au moment de sa
   clôture. Elle n'est jamais recalculée.
2. Les premiers gallons vendus, à hauteur de ce total figé, appartiennent
   déjà aux cargaisons clôturées : ils ne sont pas recomptés.
3. Le reste remplit les cargaisons **ouvertes**, de la plus ancienne à la
   plus récente, chacune jusqu'à sa quantité disponible.
4. Une vente ne peut jamais venir d'une cargaison arrivée après elle. Si la
   cargaison en place est vide, le surplus reste sur elle comme **survente**.

Résultat garanti : **reste des cargaisons ouvertes = total livré − total
vendu − restes déclarés perdus**. La page Stock affiche donc le même chiffre
que le calcul « depuis le début ».

Ce qui a changé : avant, une cargaison prenait toutes les ventes faites
depuis sa date. Si elle était datée avant la clôture de la précédente, les
ventes de la période commune étaient comptées deux fois. Chez bpccp, les
cargaisons #43 et #50 se partageaient ainsi 99,822 gal.

### Clôture d'une cargaison

- **Additionner à la cargaison suivante** : le reste passe sur la cargaison
  ouverte la plus proche. Aucun motif n'est demandé.
- **Mettre le reste à zéro** : les gallons sortent définitivement du stock.
  Un **motif écrit est obligatoire** (perte constatée, jauge, erreur…). Il
  est conservé sur la cargaison (`motif_cloture`).

### « Reste avant » saisi à la main

Ce champ ajoute des gallons sans livraison. Il est désormais :
- **refusé** si une cargaison du produit est encore ouverte. Il faut la
  clôturer avec « Additionner à la cargaison… », sinon le reste serait
  compté deux fois ;
- **accepté avec un motif obligatoire** (ex. « jauge mesurée le 10/10 »)
  quand aucune cargaison n'est ouverte.

## 2. Relevés de compteur

| Règle | Ce que fait le serveur |
|---|---|
| Prix | Le prix appliqué est celui de **Paramètres › Prix de vente** à la date du relevé, jamais celui envoyé par le navigateur. Si aucun prix n'a jamais été configuré, le prix saisi est utilisé. |
| Continuité | Le mètre avant doit être égal au mètre après du relevé précédent de la même pompe. Sinon, l'enregistrement est **refusé sans motif**. Avec un motif, l'écart et le motif sont enregistrés. |
| Vente sans stock | Si le produit n'a aucune cargaison ouverte, le relevé est enregistré mais un **avertissement** s'affiche : il faut saisir la livraison. |

Pourquoi : un mètre avant plus haut que le mètre après précédent signifie
que des gallons sont sortis de la pompe sans être vendus dans le système.
Chez bpccp, environ 542 gal de Diesel sont sortis ainsi entre août et octobre.

## 3. Caisse : transferts et charges

Chaque catégorie de dépense a un type :
- **Charge** : électricité, maintenance, salaires… Comptée dans les dépenses.
- **Transfert** : dépôt en banque, Zelle, virement. L'argent quitte la
  caisse sans être une dépense de fonctionnement.

Le rapport de caisse affiche les transferts à part. Le **cash disponible ne
change pas** (l'argent est bien sorti), mais les dépenses montrées sont
enfin les vraies charges. Les catégories dont le nom contient « banque »,
« zelle », « transfert » ou « virement » sont marquées transfert
automatiquement. Le type peut être changé par l'API
`PATCH /api/listes/categories-depense/{id}` (`est_transfert`).

## 4. Anomalies

- La liste du jour ne montre que les anomalies **datées de ce jour**. Avant,
  toutes les erreurs de compteur depuis mai revenaient chaque jour.
  `?historique=true` donne tout l'historique.
- Nouveau type **COMPTEUR_DISCONTINU** : un compteur qui avance entre deux
  relevés. C'est une erreur sans motif, un avertissement avec motif.
- `DECALAGE_STOCK_BAR` devient **STOCK_BAS_BAR**, ce qu'il signale réellement.
- Un manager, un admin ou le PDG peut **marquer une anomalie traitée**. Elle
  disparaît de la liste (`?inclure_traitees=true` pour la revoir).
