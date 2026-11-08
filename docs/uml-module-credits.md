# Module Crédits — Diagramme de classes UML

```mermaid
classDiagram
  direction LR
  class Credit {
    +int id
    +SensCredit sens
    +str tiers_nom
    +str tiers_contact
    +str description
    +Decimal montant_initial
    +Devise devise
    +date date_credit
    +date date_echeance
    +StatutCredit statut
    +str notes
    +int createur_id
    +datetime created_at
    +datetime updated_at
    +total_du() Decimal
    +total_depose() Decimal
    +reste() Decimal
    +en_retard() bool
    +recalculer_statut()
  }
  class CreditMouvement {
    +int id
    +int credit_id
    +TypeMouvement type
    +Decimal montant
    +date date_mouvement
    +str mode_paiement
    +str reference
    +str note
    +int createur_id
    +datetime created_at
  }
  class Utilisateur {
    +int id
    +str nom_complet
    +str role
    +Role role_obj
  }
  class SensCredit {
    <<enumeration>>
    DETTE
    CREANCE
  }
  class StatutCredit {
    <<enumeration>>
    ACTIF
    SOLDE
    ANNULE
  }
  class TypeMouvement {
    <<enumeration>>
    DEPOT
    AUGMENTATION
  }
  class Devise {
    <<enumeration>>
    HTG
    USD
  }
  Credit "1" *-- "0..*" CreditMouvement : mouvements
  Utilisateur "0..1" <-- "0..*" Credit : createur
  Utilisateur "0..1" <-- "0..*" CreditMouvement : createur
  Credit ..> SensCredit
  Credit ..> StatutCredit
  Credit ..> Devise
  CreditMouvement ..> TypeMouvement
```

## Règles métier

- `total_du = montant_initial + Σ AUGMENTATION`
- `reste = total_du − Σ DEPOT` (calculé, jamais stocké)
- Un DEPOT ne peut pas dépasser le reste ; sa date ne peut pas précéder `date_credit`.
- Statut : `SOLDE` automatiquement quand `reste = 0`, `ACTIF` sinon ; `ANNULE` uniquement manuel (exclu des totaux).
- Suppression d'un crédit en cascade sur ses mouvements (composition).
- `en_retard = statut ACTIF et date_echeance < aujourd'hui`.

## Implémentation (bpccp)

- Modèles : `backend/models.py` (`Credit`, `CreditMouvement`, tables `credits` et `credit_mouvements`).
- API : `backend/credits_routes.py` (`/api/credits`), réservée à la direction.
- Page : « Crédits » dans `frontend/index.html` (section Gestion).
- Ajouts au diagramme : retrait d'un mouvement saisi par erreur et suppression d'un crédit avec motif
  obligatoire (journal d'audit) ; totaux toujours séparés par devise (HTG et USD jamais additionnés).
- Tests : `backend/tests/test_credits.py`.
