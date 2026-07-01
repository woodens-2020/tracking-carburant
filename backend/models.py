from sqlalchemy import (
    Column, Integer, String, Numeric, Boolean, Date,
    ForeignKey, DateTime, UniqueConstraint, CheckConstraint,
    Index, func, text,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Produit(Base):
    """Type de carburant : Gazoline, Diesel, Kérosène…"""
    __tablename__ = "produits"

    id          = Column(Integer, primary_key=True)
    nom         = Column(String(100), unique=True, nullable=False)
    prix_gallon = Column(Numeric(12, 3), nullable=False, default=0)
    actif       = Column(Boolean, nullable=False, default=True)
    created_at  = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    pompes = relationship(
        "Pompe",
        back_populates="produit",
        cascade="all, delete-orphan",
        order_by="Pompe.id",
    )

    __table_args__ = (
        CheckConstraint("prix_gallon >= 0", name="chk_produit_prix_pos"),
    )


class Pompe(Base):
    """Pompe / distributeur physique rattaché à un produit."""
    __tablename__ = "pompes"

    id         = Column(Integer, primary_key=True)
    produit_id = Column(Integer, ForeignKey("produits.id", ondelete="CASCADE"), nullable=False)
    nom        = Column(String(100), nullable=False)
    actif      = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    produit = relationship("Produit", back_populates="pompes")

    __table_args__ = (
        UniqueConstraint("produit_id", "nom", name="uq_pompe_nom"),
        Index("idx_pompes_produit", "produit_id"),
    )


class Releve(Base):
    """Relevé de compteur : 1 ligne par (date, periode, pompe)."""
    __tablename__ = "releves"

    id           = Column(Integer, primary_key=True)
    date         = Column(Date, nullable=False)
    periode      = Column(String(20), nullable=False)          # "Matin" | "Apres-midi"
    pompe_id     = Column(Integer, ForeignKey("pompes.id", ondelete="RESTRICT"), nullable=False)
    prix_gallon  = Column(Numeric(12, 3), nullable=False, default=0)
    metter_avant     = Column(Numeric(14, 3), nullable=False, default=0)
    metter_apres     = Column(Numeric(14, 3), nullable=False, default=0)
    nb_modifications = Column(Integer, nullable=False, default=0)
    created_at       = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at   = Column(DateTime(timezone=True), nullable=False, server_default=func.now(),
                          onupdate=func.now())

    pompe = relationship("Pompe")

    __table_args__ = (
        UniqueConstraint("date", "periode", "pompe_id", name="uq_releve"),
        CheckConstraint("periode IN ('Matin', 'Apres-midi')", name="chk_releve_periode"),
        CheckConstraint("metter_apres >= metter_avant",       name="chk_releve_meter_ordre"),
        CheckConstraint("prix_gallon >= 0",                   name="chk_releve_prix_pos"),
        CheckConstraint("metter_avant >= 0",                  name="chk_releve_meter_pos"),
        Index("idx_releves_date",         "date"),
        # Bug 6 fix : l'ancien idx_releves_date_desc était identique à idx_releves_date
        # (SQLAlchemy ne crée pas un DESC sans syntaxe explicite). Remplacé par un
        # index composite (date DESC, pompe_id) utile pour les lookups de continuité.
        Index("idx_releves_date_pompe",   "date", "pompe_id"),
        Index("idx_releves_pompe",        "pompe_id"),
        Index("idx_releves_date_periode", "date", "periode"),
    )

    @property
    def quantite(self) -> float:
        """Gallons vendus durant la période."""
        return round(float(self.metter_apres) - float(self.metter_avant), 3)

    @property
    def montant_vente(self) -> float:
        """Montant en gourdes pour ce relevé."""
        return round(self.quantite * float(self.prix_gallon), 2)


class Livraison(Base):
    """Livraison de carburant : approvisionnement en gallons pour un produit."""
    __tablename__ = "livraisons"

    id                = Column(Integer, primary_key=True)
    produit_id        = Column(Integer, ForeignKey("produits.id", ondelete="RESTRICT"), nullable=False)
    date_livraison    = Column(Date, nullable=False)
    gallons_recus     = Column(Numeric(14, 3), nullable=False)
    prix_achat_gallon = Column(Numeric(12, 3), nullable=False)
    fournisseur       = Column(String(150), nullable=True)
    reference_camion  = Column(String(100), nullable=True)
    notes             = Column(String(500), nullable=True)
    created_at        = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    produit = relationship("Produit")

    __table_args__ = (
        CheckConstraint("gallons_recus > 0",      name="chk_livraison_gallons_pos"),
        CheckConstraint("prix_achat_gallon >= 0", name="chk_livraison_prix_pos"),
        Index("idx_livraisons_produit", "produit_id"),
        Index("idx_livraisons_date",    "date_livraison"),
    )


class PrixVente(Base):
    """Historique du prix de vente par produit. Insert-only : jamais mis à jour."""
    __tablename__ = "prix_vente"

    id                = Column(Integer, primary_key=True)
    produit_id        = Column(Integer, ForeignKey("produits.id", ondelete="RESTRICT"), nullable=False)
    prix_vente_gallon = Column(Numeric(12, 3), nullable=False)
    date_effet        = Column(Date, nullable=False)
    created_at        = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    produit = relationship("Produit")

    __table_args__ = (
        CheckConstraint("prix_vente_gallon > 0", name="chk_prix_vente_pos"),
        Index("idx_prix_vente_produit_date", "produit_id", "date_effet"),
    )


class Utilisateur(Base):
    """Compte utilisateur pouvant se connecter à l'application."""
    __tablename__ = "utilisateurs"

    id                = Column(Integer, primary_key=True)
    username          = Column(String(80), unique=True, nullable=False)
    password_hash     = Column(String(255), nullable=False)
    api_key_hash      = Column(String(64),  unique=True, nullable=True)
    nom_complet       = Column(String(150), nullable=False, default="")
    role              = Column(String(20),  nullable=False, default="operateur")
    poste             = Column(String(100), nullable=True)   # poste de l'employé → contrôle d'accès
    actif             = Column(Boolean,     nullable=False, default=True)
    created_at        = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # Authentification par email + mot de passe + code d'accès à 9 chiffres
    email             = Column(String(254), unique=True, nullable=True)
    code_acces_hash   = Column(String(255), nullable=True)   # hash du code 9 chiffres
    # Champs OAuth (nullable — comptes locaux n'en ont pas)
    oauth_provider    = Column(String(32),  nullable=True)   # "google" | "microsoft"
    oauth_sub         = Column(String(255), unique=True, nullable=True)

    __table_args__ = (
        CheckConstraint("role IN ('admin', 'operateur', 'pdg')", name="chk_utilisateur_role"),
    )


class SessionToken(Base):
    """Jeton de session émis après une connexion réussie."""
    __tablename__ = "sessions"

    id         = Column(Integer, primary_key=True)
    token      = Column(String(64), unique=True, nullable=False, index=True)
    user_id    = Column(Integer, ForeignKey("utilisateurs.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("idx_sessions_user", "user_id"),
    )


# ══════════════════════════════════════════════════════════════════
# MODULES DE GESTION INSTITUTIONNELLE
# ══════════════════════════════════════════════════════════════════

class Employe(Base):
    """Employé de la station."""
    __tablename__ = "employes"

    id            = Column(Integer, primary_key=True)
    nom           = Column(String(100), nullable=False)
    prenom        = Column(String(100), nullable=False)
    poste         = Column(String(100), nullable=False)
    date_embauche = Column(Date, nullable=False)
    salaire_base  = Column(Numeric(12, 2), nullable=False)
    type_contrat  = Column(String(30), nullable=False, default="CDI")
    telephone     = Column(String(30), nullable=True)
    email         = Column(String(254), nullable=True)
    actif         = Column(Boolean, nullable=False, default=True)
    notes         = Column(String(500), nullable=True)
    created_at    = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    fiches_paie = relationship("FichePaie", back_populates="employe", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("salaire_base >= 0", name="chk_employe_salaire_pos"),
        CheckConstraint(
            "type_contrat IN ('CDI','CDD','Temps partiel','Journalier','Stage')",
            name="chk_employe_contrat",
        ),
        Index("idx_employes_actif", "actif"),
    )


class FichePaie(Base):
    """Fiche de paie mensuelle d'un employé."""
    __tablename__ = "fiches_paie"

    id            = Column(Integer, primary_key=True)
    employe_id    = Column(Integer, ForeignKey("employes.id", ondelete="CASCADE"), nullable=False)
    periode_debut = Column(Date, nullable=False)
    periode_fin   = Column(Date, nullable=False)
    salaire_base  = Column(Numeric(12, 2), nullable=False)
    heures_sup    = Column(Numeric(8, 2),  nullable=False, default=0)
    taux_hs       = Column(Numeric(12, 2), nullable=False, default=0)
    primes        = Column(Numeric(12, 2), nullable=False, default=0)
    deductions    = Column(Numeric(12, 2), nullable=False, default=0)
    net_a_payer   = Column(Numeric(12, 2), nullable=False)
    statut        = Column(String(20), nullable=False, default="brouillon")
    date_paiement = Column(Date, nullable=True)
    notes         = Column(String(500), nullable=True)
    created_at    = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    employe = relationship("Employe", back_populates="fiches_paie")

    __table_args__ = (
        CheckConstraint("statut IN ('brouillon','paye')", name="chk_fiche_statut"),
        CheckConstraint("net_a_payer >= 0",              name="chk_fiche_net_pos"),
        CheckConstraint("salaire_base >= 0",             name="chk_fiche_salaire_pos"),
        CheckConstraint("heures_sup >= 0",               name="chk_fiche_hs_pos"),
        CheckConstraint("primes >= 0",                   name="chk_fiche_primes_pos"),
        CheckConstraint("deductions >= 0",               name="chk_fiche_deductions_pos"),
        Index("idx_fiches_employe",  "employe_id"),
        Index("idx_fiches_periode",  "periode_debut", "periode_fin"),
        Index("idx_fiches_statut",   "statut"),
    )


class Depense(Base):
    """Dépense opérationnelle de la station."""
    __tablename__ = "depenses"

    id           = Column(Integer, primary_key=True)
    categorie    = Column(String(50), nullable=False)
    description  = Column(String(300), nullable=False)
    montant      = Column(Numeric(12, 2), nullable=False)
    date_depense = Column(Date, nullable=False)
    beneficiaire = Column(String(150), nullable=True)
    reference    = Column(String(100), nullable=True)
    notes        = Column(String(500), nullable=True)
    created_at   = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("montant > 0", name="chk_depense_montant_pos"),
        CheckConstraint(
            "categorie IN ('Salaires','Maintenance','Fournitures','Electricite',"
            "'Eau','Loyer','Transport','Taxes','Assurance','Divers')",
            name="chk_depense_categorie",
        ),
        Index("idx_depenses_date",      "date_depense"),
        Index("idx_depenses_categorie", "categorie"),
    )


class ParametreDepense(Base):
    """Configuration de la limite budgétaire mensuelle des dépenses.
    Table singleton : toujours id=1."""
    __tablename__ = "parametres_depenses"

    id         = Column(Integer, primary_key=True)
    limite     = Column(Numeric(14, 2), nullable=True)   # None = pas de limite
    active     = Column(Boolean, nullable=False, default=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)
    updated_by = Column(String(100), nullable=True)


class Achat(Base):
    """Achat de matériel, équipement ou fournitures (hors carburant)."""
    __tablename__ = "achats"

    id           = Column(Integer, primary_key=True)
    fournisseur  = Column(String(150), nullable=False)
    description  = Column(String(300), nullable=False)
    categorie    = Column(String(50),  nullable=False)
    montant      = Column(Numeric(12, 2), nullable=False)
    date_achat   = Column(Date, nullable=False)
    reference    = Column(String(100), nullable=True)
    notes        = Column(String(500), nullable=True)
    created_at   = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("montant > 0", name="chk_achat_montant_pos"),
        CheckConstraint(
            "categorie IN ('Equipement','Pieces detachees','Fournitures bureau',"
            "'Informatique','Securite','Nettoyage','Autre')",
            name="chk_achat_categorie",
        ),
        Index("idx_achats_date",       "date_achat"),
        Index("idx_achats_categorie",  "categorie"),
        Index("idx_achats_fournisseur","fournisseur"),
    )


# ══════════════════════════════════════════════════════════════════
# MODULE BAR / RESTAURANT — POS (Point of Sale)
# ══════════════════════════════════════════════════════════════════

class BarCategorie(Base):
    """Catégories d'articles du bar (boisson, alcool, plat…)."""
    __tablename__ = "bar_categories"

    id            = Column(Integer, primary_key=True)
    nom           = Column(String(80), nullable=False)
    couleur       = Column(String(20), nullable=True)
    date_creation = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("nom", name="uq_bar_categories_nom"),
    )


class BarProduit(Base):
    """Article du bar/restaurant : boisson, plat, snack, etc."""
    __tablename__ = "bar_produits"

    id                 = Column(Integer, primary_key=True)
    nom                = Column(String(150), nullable=False)
    categorie          = Column(String(50),  nullable=False)   # boisson, plat, snack, alcool, soft…
    unite              = Column(String(30),  nullable=False, default="unite")  # bouteille, verre, assiette…
    code_barre         = Column(String(50),  nullable=True)
    actif              = Column(Boolean,     nullable=False, default=True)
    seuil_alerte_stock = Column(Numeric(12, 3), nullable=False, default=0)
    date_creation      = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # Logique caisse/unité
    vendu_par_caisse   = Column(Boolean,  nullable=False, default=False)
    unites_par_caisse  = Column(Integer,  nullable=True)   # obligatoire si vendu_par_caisse=True

    prix_historique = relationship("BarPrixHistorique", back_populates="produit",
                                   cascade="all, delete-orphan",
                                   order_by="desc(BarPrixHistorique.date_debut)")
    mouvements      = relationship("BarMouvementStock", back_populates="produit",
                                   foreign_keys="BarMouvementStock.produit_id")
    lignes_vente    = relationship("BarLigneVente", back_populates="produit")
    bar_achats      = relationship("BarAchat", back_populates="produit")

    __table_args__ = (
        Index("idx_bar_produits_categorie", "categorie"),
        Index("idx_bar_produits_actif",     "actif"),
    )


class BarPrixHistorique(Base):
    """Historique des prix de vente par article — insert-only, jamais mis à jour."""
    __tablename__ = "bar_prix_historique"

    id             = Column(Integer, primary_key=True)
    produit_id     = Column(Integer, ForeignKey("bar_produits.id", ondelete="RESTRICT"), nullable=False)
    prix           = Column(Numeric(12, 2), nullable=False)
    date_debut     = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    date_fin       = Column(DateTime(timezone=True), nullable=True)   # NULL = prix actuel
    utilisateur_id = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)

    produit = relationship("BarProduit", back_populates="prix_historique")

    __table_args__ = (
        CheckConstraint("prix > 0", name="chk_bar_prix_pos"),
        Index("idx_bar_prix_produit", "produit_id", "date_debut"),
    )


class BarAchat(Base):
    """Réception de marchandises au bar (entrée de stock avec prix d'achat)."""
    __tablename__ = "bar_achats"

    id                  = Column(Integer, primary_key=True)
    produit_id          = Column(Integer, ForeignKey("bar_produits.id", ondelete="RESTRICT"), nullable=False)
    quantite            = Column(Numeric(12, 3), nullable=False)
    prix_achat_unitaire = Column(Numeric(12, 2), nullable=False)
    fournisseur         = Column(String(150), nullable=True)
    date_achat          = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    utilisateur_id      = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    notes               = Column(String(300), nullable=True)

    produit   = relationship("BarProduit", back_populates="bar_achats")
    mouvement = relationship("BarMouvementStock", back_populates="achat", uselist=False,
                             foreign_keys="BarMouvementStock.achat_id")
    depenses  = relationship("BarAchatDepense", back_populates="achat",
                             cascade="all, delete-orphan", order_by="BarAchatDepense.id")

    __table_args__ = (
        CheckConstraint("quantite > 0",            name="chk_bar_achat_qte_pos"),
        CheckConstraint("prix_achat_unitaire >= 0", name="chk_bar_achat_prix_pos"),
        Index("idx_bar_achats_produit", "produit_id"),
        Index("idx_bar_achats_date",    "date_achat"),
    )


class BarAchatDepense(Base):
    """Dépense supplémentaire liée à un achat bar (transport, manutention, etc.)."""
    __tablename__ = "bar_achat_depenses"

    id          = Column(Integer, primary_key=True)
    achat_id    = Column(Integer, ForeignKey("bar_achats.id", ondelete="CASCADE"), nullable=False)
    description = Column(String(150), nullable=False)
    montant     = Column(Numeric(12, 2), nullable=False)

    achat = relationship("BarAchat", back_populates="depenses")

    __table_args__ = (
        CheckConstraint("montant >= 0", name="chk_bar_achat_dep_montant_pos"),
        Index("idx_bar_achat_dep_achat", "achat_id"),
    )


class BarVente(Base):
    """Vente encaissée au bar (ticket de caisse)."""
    __tablename__ = "bar_ventes"

    id               = Column(Integer, primary_key=True)
    numero_ticket    = Column(String(20),  nullable=False, unique=True)
    caissier_id      = Column(Integer, ForeignKey("employes.id", ondelete="RESTRICT"), nullable=True)
    date_heure       = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    montant_total    = Column(Numeric(14, 2), nullable=False)
    mode_paiement    = Column(String(20),  nullable=False, default="CASH")  # CASH, CREDIT, MIXTE
    statut           = Column(String(20),  nullable=False, default="PAYEE") # PAYEE, CREDIT_EN_COURS, ANNULEE
    client_nom       = Column(String(150), nullable=True)
    montant_paye     = Column(Numeric(14, 2), nullable=False, default=0)
    montant_restant  = Column(Numeric(14, 2), nullable=False, default=0)

    caissier  = relationship("Employe")
    lignes    = relationship("BarLigneVente",    back_populates="vente", cascade="all, delete-orphan")
    credit    = relationship("BarCredit",        back_populates="vente", uselist=False)
    mouvements = relationship("BarMouvementStock",
                              primaryjoin="BarVente.id == foreign(BarMouvementStock.reference_vente_id)")

    __table_args__ = (
        CheckConstraint("montant_total >= 0",   name="chk_bar_vente_total_pos"),
        CheckConstraint("montant_paye >= 0",    name="chk_bar_vente_paye_pos"),
        CheckConstraint("montant_restant >= 0", name="chk_bar_vente_restant_pos"),
        CheckConstraint("mode_paiement IN ('CASH','CREDIT','MIXTE')", name="chk_bar_vente_mode"),
        CheckConstraint("statut IN ('PAYEE','CREDIT_EN_COURS','ANNULEE')", name="chk_bar_vente_statut"),
        Index("idx_bar_ventes_date",     "date_heure"),
        Index("idx_bar_ventes_caissier", "caissier_id"),
        Index("idx_bar_ventes_statut",   "statut"),
    )


class BarMouvementStock(Base):
    """Mouvement de stock bar — source unique de vérité pour le stock courant."""
    __tablename__ = "bar_mouvements_stock"

    id                 = Column(Integer, primary_key=True)
    produit_id         = Column(Integer, ForeignKey("bar_produits.id", ondelete="RESTRICT"), nullable=False)
    type_mouvement     = Column(String(20), nullable=False)  # ENTREE, SORTIE_VENTE, AJUSTEMENT, PERTE, CASSE
    quantite           = Column(Numeric(12, 3), nullable=False)   # signée : + entrée, - sortie
    motif              = Column(String(300), nullable=True)
    reference_vente_id = Column(Integer, ForeignKey("bar_ventes.id", ondelete="SET NULL"), nullable=True)
    achat_id           = Column(Integer, ForeignKey("bar_achats.id", ondelete="SET NULL"), nullable=True)
    date_mouvement     = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    utilisateur_id     = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)

    produit = relationship("BarProduit", back_populates="mouvements",
                           foreign_keys=[produit_id])
    achat   = relationship("BarAchat",   back_populates="mouvement",
                           foreign_keys=[achat_id])

    __table_args__ = (
        CheckConstraint(
            "type_mouvement IN ('ENTREE','SORTIE_VENTE','AJUSTEMENT','PERTE','CASSE')",
            name="chk_bar_mouv_type",
        ),
        Index("idx_bar_mouv_produit", "produit_id"),
        Index("idx_bar_mouv_date",    "date_mouvement"),
        Index("idx_bar_mouv_vente",   "reference_vente_id"),
    )


class BarLigneVente(Base):
    """Ligne d'une vente bar (1 produit, quantité, prix historisé)."""
    __tablename__ = "bar_lignes_vente"

    id                    = Column(Integer, primary_key=True)
    vente_id              = Column(Integer, ForeignKey("bar_ventes.id", ondelete="CASCADE"), nullable=False)
    produit_id            = Column(Integer, ForeignKey("bar_produits.id", ondelete="RESTRICT"), nullable=False)
    quantite              = Column(Numeric(12, 3), nullable=False)
    prix_unitaire_applique = Column(Numeric(12, 2), nullable=False)  # prix au moment de la vente
    sous_total            = Column(Numeric(14, 2), nullable=False)

    vente   = relationship("BarVente",   back_populates="lignes")
    produit = relationship("BarProduit", back_populates="lignes_vente")

    __table_args__ = (
        CheckConstraint("quantite > 0",    name="chk_bar_ligne_qte_pos"),
        CheckConstraint("sous_total >= 0", name="chk_bar_ligne_total_pos"),
        Index("idx_bar_lignes_vente",   "vente_id"),
        Index("idx_bar_lignes_produit", "produit_id"),
    )


class BarCredit(Base):
    """Crédit accordé à un client (vente partiellement ou non payée)."""
    __tablename__ = "bar_credits"

    id                = Column(Integer, primary_key=True)
    vente_id          = Column(Integer, ForeignKey("bar_ventes.id", ondelete="RESTRICT"), nullable=False)
    client_nom        = Column(String(150), nullable=False)
    client_contact    = Column(String(100), nullable=True)
    montant_du        = Column(Numeric(14, 2), nullable=False)
    montant_rembourse = Column(Numeric(14, 2), nullable=False, default=0)
    solde             = Column(Numeric(14, 2), nullable=False)
    statut            = Column(String(20), nullable=False, default="OUVERT")  # OUVERT, SOLDE, EN_RETARD
    date_creation     = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    date_echeance     = Column(Date, nullable=True)

    vente          = relationship("BarVente",        back_populates="credit")
    remboursements = relationship("BarRemboursement", back_populates="credit",
                                  cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("montant_du > 0",        name="chk_bar_credit_du_pos"),
        CheckConstraint("montant_rembourse >= 0", name="chk_bar_credit_rembourse_pos"),
        CheckConstraint("solde >= 0",            name="chk_bar_credit_solde_pos"),
        CheckConstraint("statut IN ('OUVERT','SOLDE','EN_RETARD')", name="chk_bar_credit_statut"),
        Index("idx_bar_credits_statut", "statut"),
        Index("idx_bar_credits_vente",  "vente_id"),
    )


class BarRemboursement(Base):
    """Remboursement partiel ou total d'un crédit bar."""
    __tablename__ = "bar_remboursements"

    id             = Column(Integer, primary_key=True)
    credit_id      = Column(Integer, ForeignKey("bar_credits.id", ondelete="CASCADE"), nullable=False)
    montant        = Column(Numeric(14, 2), nullable=False)
    date_remb      = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    utilisateur_id = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    notes          = Column(String(300), nullable=True)

    credit = relationship("BarCredit", back_populates="remboursements")

    __table_args__ = (
        CheckConstraint("montant > 0", name="chk_bar_remb_montant_pos"),
        Index("idx_bar_remb_credit", "credit_id"),
    )


class BarCommande(Base):
    """Commande en cours (table ou emporter) avant encaissement."""
    __tablename__ = "bar_commandes"

    id                = Column(Integer, primary_key=True)
    numero_table      = Column(String(50),  nullable=True)
    client            = Column(String(150), nullable=True)
    statut            = Column(String(20),  nullable=False, default="OUVERTE")
    # OUVERTE, ENVOYEE_CUISINE, SERVIE, ENCAISSEE, ANNULEE
    caissier_id       = Column(Integer, ForeignKey("employes.id", ondelete="RESTRICT"), nullable=True)
    date_ouverture    = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    date_modification = Column(DateTime(timezone=True), nullable=True)

    caissier = relationship("Employe")
    lignes   = relationship("BarLigneCommande", back_populates="commande",
                            cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "statut IN ('OUVERTE','ENVOYEE_CUISINE','SERVIE','ENCAISSEE','ANNULEE')",
            name="chk_bar_cmd_statut",
        ),
        Index("idx_bar_cmd_statut", "statut"),
        Index("idx_bar_cmd_date",   "date_ouverture"),
    )


class BarLigneCommande(Base):
    """Ligne d'une commande bar (avant encaissement)."""
    __tablename__ = "bar_lignes_commande"

    id          = Column(Integer, primary_key=True)
    commande_id = Column(Integer, ForeignKey("bar_commandes.id", ondelete="CASCADE"), nullable=False)
    produit_id  = Column(Integer, ForeignKey("bar_produits.id", ondelete="RESTRICT"), nullable=False)
    quantite    = Column(Numeric(12, 3), nullable=False)
    notes       = Column(String(200), nullable=True)

    commande = relationship("BarCommande", back_populates="lignes")
    produit  = relationship("BarProduit")

    __table_args__ = (
        CheckConstraint("quantite > 0", name="chk_bar_ligne_cmd_qte_pos"),
        Index("idx_bar_lcmd_commande", "commande_id"),
        Index("idx_bar_lcmd_produit",  "produit_id"),
    )


class BarPaiementEmploye(Base):
    """Paiement ad hoc à un employé du bar (salaire, avance, bonus, commission)."""
    __tablename__ = "bar_paiements_employes"

    id             = Column(Integer, primary_key=True)
    employe_id     = Column(Integer, ForeignKey("employes.id", ondelete="RESTRICT"), nullable=False)
    montant        = Column(Numeric(12, 2), nullable=False)
    periode_debut  = Column(Date, nullable=True)
    periode_fin    = Column(Date, nullable=True)
    type_paiement  = Column(String(20), nullable=False, default="SALAIRE")
    # SALAIRE, AVANCE, BONUS, COMMISSION
    date_paiement  = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    mode           = Column(String(20), nullable=False, default="CASH")  # CASH, VIREMENT, CHEQUE
    utilisateur_id = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    notes          = Column(String(300), nullable=True)

    employe = relationship("Employe")

    __table_args__ = (
        CheckConstraint("montant > 0", name="chk_bar_paie_montant_pos"),
        CheckConstraint(
            "type_paiement IN ('SALAIRE','AVANCE','BONUS','COMMISSION')",
            name="chk_bar_paie_type",
        ),
        CheckConstraint("mode IN ('CASH','VIREMENT','CHEQUE')", name="chk_bar_paie_mode"),
        Index("idx_bar_paie_employe", "employe_id"),
        Index("idx_bar_paie_date",    "date_paiement"),
    )
