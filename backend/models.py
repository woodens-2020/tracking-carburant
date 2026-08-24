from sqlalchemy import (
    Column, Integer, String, Numeric, Boolean, Date, Text, Float,
    ForeignKey, DateTime, UniqueConstraint, CheckConstraint,
    Index, func, text, JSON,
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
    bar_achats = relationship("BarAchat", back_populates="station_produit",
                              foreign_keys="BarAchat.station_produit_id")

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
    metter_avant     = Column(Numeric(14, 4), nullable=False, default=0)
    metter_apres     = Column(Numeric(14, 4), nullable=False, default=0)
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
        return round(float(self.metter_apres) - float(self.metter_avant), 4)

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

    # ── Clôture de cargaison (suivi FIFO, indépendant du stock agrégé) ──────
    # Marquer une livraison "terminée" l'exclut des futures allocations FIFO
    # (stock_service.fifo_allocation_livraisons) ; son reste peut être
    # explicitement reporté sur une cargaison plus récente au lieu d'être
    # implicitement mélangé.
    terminee                = Column(Boolean, nullable=False, default=False)
    gallons_report_recu     = Column(Numeric(14, 3), nullable=False, default=0)   # reçu d'une cargaison clôturée (transfert interne, déjà compté dans gallons_livres via la cargaison source)
    gallons_reste_manuel    = Column(Numeric(14, 3), nullable=False, default=0)   # "reste avant" saisi manuellement à la création — pas un transfert, compté en plus dans gallons_livres
    gallons_restants_cloture = Column(Numeric(14, 3), nullable=True)              # figé au moment de la clôture
    date_cloture             = Column(DateTime(timezone=True), nullable=True)
    utilisateur_cloture_id   = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    report_vers_livraison_id = Column(Integer, ForeignKey("livraisons.id", ondelete="SET NULL"), nullable=True)

    # Rapport de vente figé à la clôture (jamais recalculé ensuite — protège
    # cette cargaison des effets rétroactifs d'un nouveau prix d'achat sur
    # une autre livraison). Le coût/bénéfice se dérivent à la lecture à
    # partir de ces gallons figés × prix_achat_gallon (immuable), donc ne
    # sont pas stockés séparément.
    rapport_gallons_vendus = Column(Numeric(14, 3), nullable=True)
    rapport_revenu         = Column(Numeric(14, 2), nullable=True)

    produit             = relationship("Produit")
    utilisateur_cloture = relationship("Utilisateur", foreign_keys=[utilisateur_cloture_id])
    report_vers         = relationship("Livraison", remote_side=[id], foreign_keys=[report_vers_livraison_id])

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


class Role(Base):
    """Rôle utilisateur avec matrice de permissions par domaine fonctionnel."""
    __tablename__ = "roles"

    id            = Column(Integer, primary_key=True)
    nom           = Column(String(100), unique=True, nullable=False)
    description   = Column(String(300), nullable=True)
    permissions   = Column(JSON, nullable=False, default=dict)
    est_admin     = Column(Boolean, nullable=False, default=False)
    est_systeme   = Column(Boolean, nullable=False, default=False)
    date_creation = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    utilisateurs = relationship("Utilisateur", back_populates="role_obj")

    __table_args__ = (
        Index("idx_roles_nom", "nom"),
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
    poste             = Column(String(100), nullable=True)
    actif             = Column(Boolean,     nullable=False, default=True)
    created_at        = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    email             = Column(String(254), unique=True, nullable=True)
    code_acces_hash   = Column(String(255), nullable=True)
    oauth_provider    = Column(String(32),  nullable=True)
    oauth_sub         = Column(String(255), unique=True, nullable=True)
    role_id           = Column(Integer, ForeignKey("roles.id", ondelete="SET NULL"), nullable=True)
    telephone         = Column(String(20),  nullable=True)  # format E.164 (+509XXXXXXXX) — second canal OTP (SMS)

    role_obj = relationship("Role", back_populates="utilisateurs")

    __table_args__ = (
        CheckConstraint("role IN ('admin', 'operateur', 'pdg')", name="chk_utilisateur_role"),
        Index("idx_utilisateurs_role_id", "role_id"),
    )


class SessionToken(Base):
    """Jeton de session émis après une connexion réussie."""
    __tablename__ = "sessions"

    id         = Column(Integer, primary_key=True)
    token      = Column(String(64), unique=True, nullable=False, index=True)
    user_id    = Column(Integer, ForeignKey("utilisateurs.id", ondelete="CASCADE"), nullable=False)
    ip_address = Column(String(45),  nullable=True)
    user_agent = Column(String(255), nullable=True)
    created_at       = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at       = Column(DateTime(timezone=True), nullable=False)
    last_activity_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("Utilisateur")

    __table_args__ = (
        Index("idx_sessions_user", "user_id"),
    )


class LoginSecurityEvent(Base):
    """Photo + géolocalisation capturées à chaque connexion."""
    __tablename__ = "login_security_events"

    id           = Column(Integer, primary_key=True)
    user_id      = Column(Integer, ForeignKey("utilisateurs.id", ondelete="CASCADE"), nullable=False)
    session_id   = Column(Integer, nullable=True)
    photo_b64    = Column(Text, nullable=True)
    latitude     = Column(Float, nullable=True)
    longitude    = Column(Float, nullable=True)
    distance_km   = Column(Float, nullable=True)      # distance a l'institution (calculee uniquement si statut_geoloc='ok')
    statut_geoloc = Column(String(20), nullable=True)  # 'ok' | 'refuse' | 'erreur'
    ip_address   = Column(String(45), nullable=True)
    user_agent   = Column(String(255), nullable=True)
    created_at   = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    user = relationship("Utilisateur")

    __table_args__ = (
        Index("idx_lse_user", "user_id"),
        Index("idx_lse_created", "created_at"),
    )


class OTPCode(Base):
    """Code OTP à usage unique pour la vérification en deux étapes."""
    __tablename__ = "otp_codes"

    id            = Column(Integer, primary_key=True)
    user_id       = Column(Integer, ForeignKey("utilisateurs.id", ondelete="CASCADE"), nullable=False)
    code_hash     = Column(String(64),  nullable=False)
    pending_token = Column(String(64),  nullable=True, unique=True)
    created_at    = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at    = Column(DateTime(timezone=True), nullable=False)
    attempts      = Column(Integer,     nullable=False, default=0)
    used          = Column(Boolean,     nullable=False, default=False)

    user = relationship("Utilisateur")

    __table_args__ = (
        Index("idx_otp_user_id", "user_id"),
        Index("idx_otp_pending", "pending_token"),
        Index("idx_otp_expires", "expires_at"),
    )


class AdminCode(Base):
    """Code 5 chiffres généré par l'admin pour débloquer un employé sans email OTP."""
    __tablename__ = "admin_codes"

    id         = Column(Integer, primary_key=True)
    user_id    = Column(Integer, ForeignKey("utilisateurs.id", ondelete="CASCADE"), nullable=False)
    code_hash  = Column(String(64),  nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used       = Column(Boolean,     nullable=False, default=False)
    attempts   = Column(Integer,     nullable=False, default=0)

    user = relationship("Utilisateur")

    __table_args__ = (
        Index("idx_admin_code_user",    "user_id"),
        Index("idx_admin_code_expires", "expires_at"),
    )


class AuditLog(Base):
    """Journal d'activité — toutes les actions sensibles du système."""
    __tablename__ = "audit_logs"

    id             = Column(Integer, primary_key=True)
    user_id        = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    action         = Column(String(50),  nullable=False)
    target_user_id = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    ip_address     = Column(String(45),  nullable=True)
    details        = Column(Text,        nullable=True)   # JSON sérialisé
    created_at     = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    user        = relationship("Utilisateur", foreign_keys=[user_id])
    target_user = relationship("Utilisateur", foreign_keys=[target_user_id])

    __table_args__ = (
        Index("idx_audit_user",    "user_id"),
        Index("idx_audit_action",  "action"),
        Index("idx_audit_created", "created_at"),
    )


# ══════════════════════════════════════════════════════════════════
# MODULES DE GESTION INSTITUTIONNELLE
# ══════════════════════════════════════════════════════════════════

class Employe(Base):
    """Employé de la station."""
    __tablename__ = "employes"

    id             = Column(Integer, primary_key=True)
    nom            = Column(String(100), nullable=False)
    prenom         = Column(String(100), nullable=False)
    poste          = Column(String(100), nullable=False)
    date_embauche  = Column(Date, nullable=False)
    salaire_base   = Column(Numeric(12, 2), nullable=False)
    type_contrat   = Column(String(30), nullable=False, default="CDI")
    telephone      = Column(String(30), nullable=True)
    email          = Column(String(254), nullable=True)
    actif          = Column(Boolean, nullable=False, default=True)
    notes          = Column(String(500), nullable=True)
    utilisateur_id = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    created_at     = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    fiches_paie = relationship("FichePaie", back_populates="employe", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("salaire_base >= 0", name="chk_employe_salaire_pos"),
        CheckConstraint(
            "type_contrat IN ('CDI','CDD','Temps partiel','Journalier','Stage')",
            name="chk_employe_contrat",
        ),
        Index("idx_employes_actif",       "actif"),
        Index("idx_employe_utilisateur",  "utilisateur_id"),
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
    # Caisse d'origine (Gazoline / Diesel) — NULL = depense generale, non
    # attribuee a une caisse carburant specifique.
    produit_id   = Column(Integer, ForeignKey("produits.id", ondelete="SET NULL"), nullable=True)
    created_at   = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    produit = relationship("Produit")

    __table_args__ = (
        CheckConstraint("montant > 0", name="chk_depense_montant_pos"),
        CheckConstraint(
            "categorie IN ('Salaires','Maintenance','Fournitures','Electricite',"
            "'Eau','Loyer','Transport','Taxes','Assurance','Divers')",
            name="chk_depense_categorie",
        ),
        Index("idx_depenses_date",      "date_depense"),
        Index("idx_depenses_categorie", "categorie"),
        Index("idx_depenses_produit",   "produit_id"),
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

    produits = relationship("BarProduit", back_populates="categorie_obj",
                            foreign_keys="BarProduit.categorie")

    __table_args__ = (
        UniqueConstraint("nom", name="uq_bar_categories_nom"),
    )


class BarProduit(Base):
    """Article du bar/restaurant : boisson, plat, snack, etc."""
    __tablename__ = "bar_produits"

    id                 = Column(Integer, primary_key=True)
    nom                = Column(String(150), nullable=False)
    categorie          = Column(String(80),  ForeignKey("bar_categories.nom", onupdate="CASCADE", ondelete="RESTRICT"), nullable=False)
    unite              = Column(String(30),  nullable=False, default="unite")  # bouteille, verre, assiette…
    code_barre         = Column(String(50),  nullable=True)
    actif              = Column(Boolean,     nullable=False, default=True)
    seuil_alerte_stock = Column(Numeric(12, 3), nullable=False, default=0)
    date_creation      = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # Logique caisse/unité
    vendu_par_caisse   = Column(Boolean,  nullable=False, default=False)
    unites_par_caisse  = Column(Integer,  nullable=True)   # obligatoire si vendu_par_caisse=True
    # Photo illustrative — même approche que PieceJointe (base64 en base, pas de stockage fichier externe)
    photo_base64       = Column(Text,        nullable=True)
    photo_mime         = Column(String(50),  nullable=True)

    prix_historique = relationship("BarPrixHistorique", back_populates="produit",
                                   cascade="all, delete-orphan",
                                   order_by="desc(BarPrixHistorique.date_debut)")
    mouvements      = relationship("BarMouvementStock", back_populates="produit",
                                   foreign_keys="BarMouvementStock.produit_id")
    lignes_vente    = relationship("BarLigneVente", back_populates="produit")
    bar_achats      = relationship("BarAchat", back_populates="bar_produit",
                                   foreign_keys="BarAchat.produit_id")
    categorie_obj   = relationship("BarCategorie", back_populates="produits",
                                   foreign_keys=[categorie])

    __table_args__ = (
        UniqueConstraint("nom", name="uq_bar_produit_nom"),
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
    """Réception de marchandises — bar OU station service.

    Exactement un des deux FK doit être non-null :
      - produit_id          → BarProduit (bar_produits)  : génère un mouvement de stock bar
      - station_produit_id  → Produit    (produits)       : carburant, pas de mouvement de stock bar
    """
    __tablename__ = "bar_achats"

    id                   = Column(Integer, primary_key=True)
    produit_id           = Column(Integer, ForeignKey("bar_produits.id", ondelete="RESTRICT"), nullable=True)
    station_produit_id   = Column(Integer, ForeignKey("produits.id",     ondelete="RESTRICT"), nullable=True)
    quantite             = Column(Numeric(12, 3), nullable=False)
    prix_achat_unitaire  = Column(Numeric(12, 2), nullable=False)
    fournisseur          = Column(String(150), nullable=True)
    date_achat           = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    utilisateur_id       = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    notes                = Column(String(300), nullable=True)

    statut           = Column(String(20), nullable=False, default='EN_ATTENTE')
    # EN_ATTENTE  → achat enregistré, stock pas encore mis à jour
    # CONFIRME    → stock mis à jour (ou non applicable pour carburants)

    bar_produit      = relationship("BarProduit", back_populates="bar_achats",
                                    foreign_keys=[produit_id])
    station_produit  = relationship("Produit", back_populates="bar_achats",
                                    foreign_keys=[station_produit_id])
    mouvement = relationship("BarMouvementStock", back_populates="achat", uselist=False,
                             foreign_keys="BarMouvementStock.achat_id")
    depenses  = relationship("BarAchatDepense", back_populates="achat",
                             cascade="all, delete-orphan", order_by="BarAchatDepense.id")

    __table_args__ = (
        CheckConstraint("quantite > 0",            name="chk_bar_achat_qte_pos"),
        CheckConstraint("prix_achat_unitaire >= 0", name="chk_bar_achat_prix_pos"),
        CheckConstraint(
            "(produit_id IS NOT NULL)::int + (station_produit_id IS NOT NULL)::int = 1",
            name="chk_bar_achat_produit_xor",
        ),
        CheckConstraint("statut IN ('EN_ATTENTE','CONFIRME','ANNULE')", name="chk_bar_achat_statut"),
        Index("idx_bar_achats_produit",         "produit_id"),
        Index("idx_bar_achats_station_produit",  "station_produit_id"),
        Index("idx_bar_achats_date",             "date_achat"),
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
    client_id        = Column(Integer, ForeignKey("clients.id", ondelete="SET NULL"), nullable=True)
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

    id                     = Column(Integer, primary_key=True)
    vente_id               = Column(Integer, ForeignKey("bar_ventes.id",      ondelete="CASCADE"),  nullable=False)
    produit_id             = Column(Integer, ForeignKey("bar_produits.id",    ondelete="RESTRICT"), nullable=True)
    cuisine_plat_id        = Column(Integer, ForeignKey("cuisine_plats.id",   ondelete="RESTRICT"), nullable=True)
    quantite               = Column(Numeric(12, 3), nullable=False)
    prix_unitaire_applique = Column(Numeric(12, 2), nullable=False)
    sous_total             = Column(Numeric(14, 2), nullable=False)

    vente        = relationship("BarVente",    back_populates="lignes")
    produit      = relationship("BarProduit",  back_populates="lignes_vente")
    cuisine_plat = relationship("CuisinePlat")

    __table_args__ = (
        CheckConstraint("quantite > 0",                                              name="chk_bar_ligne_qte_pos"),
        CheckConstraint("sous_total >= 0",                                           name="chk_bar_ligne_total_pos"),
        CheckConstraint("produit_id IS NOT NULL OR cuisine_plat_id IS NOT NULL",     name="chk_bar_ligne_produit_ou_cuisine"),
        Index("idx_bar_lignes_vente",        "vente_id"),
        Index("idx_bar_lignes_produit",      "produit_id"),
        Index("idx_bar_lignes_cuisine_plat", "cuisine_plat_id"),
    )


class Client(Base):
    """Client régulier — registre partagé pour que le nom saisi lors d'une
    vente ou d'un crédit reste toujours orthographié de la même façon
    (évite les doublons du type « Jean Pierre » / « Jean-Pierre » qui
    cassent le suivi d'historique d'un même client)."""
    __tablename__ = "clients"

    id            = Column(Integer, primary_key=True)
    nom           = Column(String(150), nullable=False)
    telephone     = Column(String(30),  nullable=True)
    notes         = Column(String(300), nullable=True)
    # Éligibilité au crédit — définie uniquement par un administrateur (voir
    # main.require_admin). Un nouveau client est éligible par défaut pour ne
    # pas interrompre les habitudes actuelles ; l'admin bloque au cas par cas
    # les mauvais payeurs.
    statut_credit = Column(String(20), nullable=False, default="ELIGIBLE")
    created_at    = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("statut_credit IN ('ELIGIBLE','NON_ELIGIBLE')", name="chk_client_statut_credit"),
        Index("idx_clients_nom", "nom"),
    )


class BarCredit(Base):
    """Crédit accordé à un client (vente partiellement ou non payée)."""
    __tablename__ = "bar_credits"

    id                = Column(Integer, primary_key=True)
    vente_id          = Column(Integer, ForeignKey("bar_ventes.id", ondelete="RESTRICT"), nullable=False)
    client_nom        = Column(String(150), nullable=False)
    client_id         = Column(Integer, ForeignKey("clients.id", ondelete="SET NULL"), nullable=True)
    client_contact    = Column(String(100), nullable=True)
    client_nif        = Column(String(50),  nullable=True)
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


# ══════════════════════════════════════════════════════════════════
# HOTEL
# ══════════════════════════════════════════════════════════════════

class HotelChambre(Base):
    """Chambre de l'hôtel — configuration et état."""
    __tablename__ = "hotel_chambres"

    id            = Column(Integer, primary_key=True)
    numero        = Column(String(20),  nullable=False)
    type_chambre  = Column(String(20),  nullable=False, default="SIMPLE")
    etage         = Column(Integer,     nullable=True)
    capacite      = Column(Integer,     nullable=False, default=1)
    prix_nuit     = Column(Numeric(12,2), nullable=False)
    prix_moment   = Column(Numeric(12,2), nullable=True)   # prix/heure pour séjour moment
    statut        = Column(String(20),  nullable=False, default="DISPONIBLE")
    description   = Column(String(300), nullable=True)
    actif         = Column(Boolean,     nullable=False, default=True)
    date_creation = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    reservations = relationship("HotelReservation", back_populates="chambre")

    __table_args__ = (
        UniqueConstraint("numero", name="uq_hotel_chambre_numero"),
        CheckConstraint("type_chambre IN ('SIMPLE','DOUBLE','SUITE','VIP')",
                        name="chk_hotel_chambre_type"),
        CheckConstraint("statut IN ('DISPONIBLE','OCCUPEE','MAINTENANCE','FERMEE')",
                        name="chk_hotel_chambre_statut"),
        CheckConstraint("prix_nuit > 0", name="chk_hotel_chambre_prix_nuit"),
        CheckConstraint("capacite >= 1", name="chk_hotel_chambre_capacite"),
        Index("idx_hotel_chambres_statut", "statut"),
        Index("idx_hotel_chambres_actif",  "actif"),
    )


class HotelEmploye(Base):
    """Employé de la section hôtel."""
    __tablename__ = "hotel_employes"

    id            = Column(Integer, primary_key=True)
    nom           = Column(String(100), nullable=False)
    prenom        = Column(String(100), nullable=False)
    poste         = Column(String(40),  nullable=False, default="RECEPTIONNISTE")
    telephone     = Column(String(30),  nullable=True)
    email         = Column(String(100), nullable=True)
    date_embauche = Column(Date,        nullable=True)
    salaire_base  = Column(Numeric(12,2), nullable=True)
    actif         = Column(Boolean,     nullable=False, default=True)
    notes         = Column(String(300), nullable=True)
    created_at    = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    reservations = relationship("HotelReservation", back_populates="employe")

    __table_args__ = (
        CheckConstraint(
            "poste IN ('RECEPTIONNISTE','FEMME_DE_CHAMBRE','GERANT','SECURITE','AUTRE')",
            name="chk_hotel_emp_poste",
        ),
        Index("idx_hotel_employes_actif", "actif"),
    )


class HotelReservation(Base):
    """Enregistrement client — nuit ou moment."""
    __tablename__ = "hotel_reservations"

    id                 = Column(Integer, primary_key=True)
    chambre_id         = Column(Integer, ForeignKey("hotel_chambres.id", ondelete="RESTRICT"), nullable=False)
    client_nom         = Column(String(150), nullable=False)
    client_contact     = Column(String(100), nullable=True)
    client_id_piece    = Column(String(80),  nullable=True)
    type_sejour        = Column(String(10),  nullable=False)   # NUIT | MOMENT
    date_arrivee       = Column(DateTime(timezone=True), nullable=False)
    date_depart_prevue = Column(DateTime(timezone=True), nullable=False)
    date_depart_reel   = Column(DateTime(timezone=True), nullable=True)
    nb_nuits           = Column(Integer,     nullable=True)
    nb_heures          = Column(Numeric(5,2), nullable=True)
    prix_unitaire      = Column(Numeric(12,2), nullable=False)
    montant_total      = Column(Numeric(12,2), nullable=False)
    montant_paye       = Column(Numeric(12,2), nullable=False, default=0)
    solde              = Column(Numeric(12,2), nullable=False, default=0)
    statut             = Column(String(20),  nullable=False, default="EN_COURS")
    mode_paiement      = Column(String(20),  nullable=True)
    notes              = Column(String(300), nullable=True)
    employe_id         = Column(Integer, ForeignKey("hotel_employes.id", ondelete="SET NULL"), nullable=True)
    created_at         = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    chambre = relationship("HotelChambre", back_populates="reservations")
    employe = relationship("HotelEmploye", back_populates="reservations")

    __table_args__ = (
        CheckConstraint("type_sejour IN ('NUIT','MOMENT')",   name="chk_hotel_res_type"),
        CheckConstraint("statut IN ('EN_COURS','TERMINEE','ANNULEE')", name="chk_hotel_res_statut"),
        CheckConstraint("montant_total >= 0",  name="chk_hotel_res_total_pos"),
        CheckConstraint("montant_paye  >= 0",  name="chk_hotel_res_paye_pos"),
        CheckConstraint("solde         >= 0",  name="chk_hotel_res_solde_pos"),
        Index("idx_hotel_res_chambre", "chambre_id"),
        Index("idx_hotel_res_statut",  "statut"),
        Index("idx_hotel_res_date",    "date_arrivee"),
    )


class HotelDepense(Base):
    """Dépense opérationnelle de l'hôtel (entretien, blanchisserie,
    fournitures, maintenance…) — même modèle que CuisineDepense."""
    __tablename__ = "hotel_depenses"

    id           = Column(Integer,     primary_key=True)
    description  = Column(String(200), nullable=False)
    categorie    = Column(String(80),  nullable=True)
    montant      = Column(Numeric(12, 2), nullable=False)
    date_depense = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    fournisseur  = Column(String(150), nullable=True)
    notes        = Column(String(300), nullable=True)

    __table_args__ = (
        CheckConstraint("montant > 0", name="chk_hotel_dep_montant_pos"),
        Index("idx_hotel_depenses_date", "date_depense"),
    )


# ══════════════════════════════════════════════════════════════════
# MODULE CUISINE
# ══════════════════════════════════════════════════════════════════

class CuisinePlat(Base):
    """Plat du menu de la cuisine."""
    __tablename__ = "cuisine_plats"

    id            = Column(Integer,     primary_key=True)
    nom           = Column(String(150), nullable=False)
    categorie     = Column(String(80),  nullable=True)
    description   = Column(String(300), nullable=True)
    prix_vente    = Column(Numeric(12, 2), nullable=False)
    cout_estime   = Column(Numeric(12, 2), nullable=True)
    actif         = Column(Boolean, nullable=False, default=True)
    date_creation = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    lignes_vente = relationship("CuisineLigneVente", back_populates="plat")
    achats       = relationship("CuisineAchat",      back_populates="plat")

    __table_args__ = (
        CheckConstraint("prix_vente > 0", name="chk_cuisine_plat_prix_pos"),
    )


class CuisineDepense(Base):
    """Achat ou dépense cuisine (ingrédients, équipement, gaz, personnel…)."""
    __tablename__ = "cuisine_depenses"

    id           = Column(Integer,     primary_key=True)
    description  = Column(String(200), nullable=False)
    categorie    = Column(String(80),  nullable=True)
    montant      = Column(Numeric(12, 2), nullable=False)
    date_depense = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    fournisseur  = Column(String(150), nullable=True)
    notes        = Column(String(300), nullable=True)

    __table_args__ = (
        CheckConstraint("montant > 0", name="chk_cuisine_dep_montant_pos"),
        Index("idx_cuisine_depenses_date", "date_depense"),
    )


class CuisineVente(Base):
    """Vente de plats — ticket de caisse cuisine."""
    __tablename__ = "cuisine_ventes"

    id             = Column(Integer,    primary_key=True)
    numero_ticket  = Column(String(30), unique=True, nullable=False)
    date_heure     = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    total          = Column(Numeric(12, 2), nullable=False)
    mode_paiement  = Column(String(20), nullable=False, default="CASH")
    client_nom     = Column(String(100), nullable=True)
    notes          = Column(String(200), nullable=True)
    statut         = Column(String(20), nullable=False, default="VALIDEE")

    lignes = relationship("CuisineLigneVente", back_populates="vente",
                          cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("total >= 0",                              name="chk_cuisine_vente_total_pos"),
        CheckConstraint("statut IN ('VALIDEE','ANNULEE')",         name="chk_cuisine_vente_statut"),
        CheckConstraint("mode_paiement IN ('CASH','CREDIT')",      name="chk_cuisine_vente_mode"),
        Index("idx_cuisine_ventes_date",   "date_heure"),
        Index("idx_cuisine_ventes_statut", "statut"),
    )


class CuisineLigneVente(Base):
    """Ligne d'une vente cuisine."""
    __tablename__ = "cuisine_lignes_vente"

    id            = Column(Integer, primary_key=True)
    vente_id      = Column(Integer, ForeignKey("cuisine_ventes.id", ondelete="CASCADE"), nullable=False)
    plat_id       = Column(Integer, ForeignKey("cuisine_plats.id",  ondelete="SET NULL"), nullable=True)
    nom_plat      = Column(String(150), nullable=False)
    quantite      = Column(Integer,     nullable=False)
    prix_unitaire = Column(Numeric(12, 2), nullable=False)
    sous_total    = Column(Numeric(12, 2), nullable=False)

    vente = relationship("CuisineVente",  back_populates="lignes")
    plat  = relationship("CuisinePlat",   back_populates="lignes_vente")

    __table_args__ = (
        CheckConstraint("quantite > 0",       name="chk_cuisine_lv_qte_pos"),
        CheckConstraint("prix_unitaire >= 0",  name="chk_cuisine_lv_prix_pos"),
        CheckConstraint("sous_total >= 0",     name="chk_cuisine_lv_sous_pos"),
        Index("idx_cuisine_lv_vente", "vente_id"),
        Index("idx_cuisine_lv_plat",  "plat_id"),
    )


class CuisineAchat(Base):
    """Déclaration d'achat ingrédients / matières premières cuisine."""
    __tablename__ = "cuisine_achats"

    id            = Column(Integer,      primary_key=True)
    plat_id       = Column(Integer,      ForeignKey("cuisine_plats.id", ondelete="SET NULL"), nullable=True)
    description   = Column(String(200),  nullable=False)
    categorie     = Column(String(80),   nullable=True,  default="INGREDIENTS")
    quantite      = Column(Numeric(10, 3), nullable=False)
    unite         = Column(String(20),   nullable=True,  default="kg")
    cout_unitaire = Column(Numeric(12, 2), nullable=False)
    total         = Column(Numeric(14, 2), nullable=False)
    date_achat    = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    fournisseur   = Column(String(150),  nullable=True)
    notes         = Column(String(300),  nullable=True)

    plat = relationship("CuisinePlat", back_populates="achats")

    __table_args__ = (
        CheckConstraint("quantite > 0",       name="chk_cuisine_achat_qte_pos"),
        CheckConstraint("cout_unitaire >= 0",  name="chk_cuisine_achat_cout_pos"),
        CheckConstraint("total >= 0",          name="chk_cuisine_achat_total_pos"),
        Index("idx_cuisine_achats_plat",  "plat_id"),
        Index("idx_cuisine_achats_date",  "date_achat"),
    )


class PasswordResetToken(Base):
    """Token de réinitialisation de mot de passe — URL sécurisée, usage unique, 30 min."""
    __tablename__ = "password_reset_tokens"

    id         = Column(Integer, primary_key=True)
    user_id    = Column(Integer, ForeignKey("utilisateurs.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used       = Column(Boolean, nullable=False, default=False)
    ip_address = Column(String(45), nullable=True)

    user = relationship("Utilisateur")

    __table_args__ = (
        Index("idx_prt_user",    "user_id"),
        Index("idx_prt_expires", "expires_at"),
        Index("idx_prt_hash",    "token_hash"),
    )


class BarSessionCaisse(Base):
    """Session de caisse — suivi des ventes par caissière par jour."""
    __tablename__ = "bar_sessions_caisse"

    id            = Column(Integer, primary_key=True)
    caissier_id   = Column(Integer, ForeignKey("employes.id",    ondelete="RESTRICT"), nullable=False)
    date_session  = Column(Date, nullable=False)
    statut        = Column(String(20), nullable=False, default="EN_COURS")  # EN_COURS, SOUMIS, VALIDE
    soumis_at     = Column(DateTime(timezone=True), nullable=True)
    valide_at     = Column(DateTime(timezone=True), nullable=True)
    valide_par_id = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    notes_admin   = Column(String(500), nullable=True)
    # Réconciliation de caisse, figée au moment de la soumission (jamais
    # recalculée après coup) — null tant que la session est EN_COURS, et
    # reste null pour les sessions historiques créées avant cette colonne.
    cash_attendu_soumission = Column(Numeric(14, 2), nullable=True)
    montant_compte          = Column(Numeric(14, 2), nullable=True)
    ecart                   = Column(Numeric(14, 2), nullable=True)
    # Évaluation produit par produit du rapport par un responsable (page
    # "Rapports Soumis") — distincte de la validation existante (valide_at/
    # valide_par_id), qui reste utilisable telle quelle pour un accept rapide
    # sans passer par l'évaluation détaillée.
    evaluation_statut = Column(String(20), nullable=False, default="NON_EVALUE")  # NON_EVALUE, TERMINEE
    score              = Column(Numeric(5, 2), nullable=True)
    evalue_par_id      = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    evalue_le          = Column(DateTime(timezone=True), nullable=True)
    created_at    = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    caissier   = relationship("Employe",      foreign_keys=[caissier_id])
    valide_par = relationship("Utilisateur",  foreign_keys=[valide_par_id])
    evalue_par = relationship("Utilisateur",  foreign_keys=[evalue_par_id])
    evaluations = relationship("BarSessionEvaluation", back_populates="session", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("caissier_id", "date_session", name="uq_session_caissier_date"),
        CheckConstraint("statut IN ('EN_COURS','SOUMIS','VALIDE')", name="chk_session_statut"),
        CheckConstraint("evaluation_statut IN ('NON_EVALUE','TERMINEE')", name="chk_session_eval_statut"),
        Index("idx_session_caissier", "caissier_id"),
        Index("idx_session_date",     "date_session"),
        Index("idx_session_statut",   "statut"),
    )


class BarSessionEvaluation(Base):
    """Évaluation d'un article par le responsable, lors de la ré-évaluation
    d'un rapport de session déjà soumis (page "Rapports Soumis") — un
    enregistrement par article distinct de la session."""
    __tablename__ = "bar_session_evaluations"

    id             = Column(Integer, primary_key=True)
    session_id     = Column(Integer, ForeignKey("bar_sessions_caisse.id", ondelete="CASCADE"), nullable=False)
    produit_nom    = Column(String(200), nullable=False)
    statut         = Column(String(20), nullable=False)  # CORRECT, NON_CORRECT, INTROUVABLE
    evalue_par_id  = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    evalue_at      = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    session    = relationship("BarSessionCaisse", back_populates="evaluations")
    evalue_par = relationship("Utilisateur", foreign_keys=[evalue_par_id])

    __table_args__ = (
        UniqueConstraint("session_id", "produit_nom", name="uq_eval_session_produit"),
        CheckConstraint("statut IN ('CORRECT','NON_CORRECT','INTROUVABLE')", name="chk_eval_statut"),
        Index("idx_eval_session", "session_id"),
    )


class ZelleConfig(Base):
    """Configuration du département Zelle (taux de change, balance initiale)."""
    __tablename__ = "zelle_config"

    id                = Column(Integer, primary_key=True)
    taux              = Column(Numeric(10, 4), nullable=False, default=130)
    balance_avant_usd = Column(Numeric(14, 2), nullable=False, default=0)
    date_maj          = Column(DateTime(timezone=True), server_default=func.now())


class GeolocalisationConfig(Base):
    """Coordonnées de l'institution et rayon d'alerte pour la visibilité
    géographique des connexions. Table singleton (toujours 1 ligne).
    Tant que institution_latitude/longitude sont NULL, la fonctionnalité
    est inerte : aucune anomalie CONNEXION_HORS_PERIMETRE n'est générée."""
    __tablename__ = "geolocalisation_config"

    id                     = Column(Integer, primary_key=True)
    institution_latitude   = Column(Float, nullable=True)
    institution_longitude  = Column(Float, nullable=True)
    rayon_alerte_km        = Column(Float, nullable=False, default=1.0)
    updated_at             = Column(DateTime(timezone=True), nullable=True)


class ZelleTransaction(Base):
    """Transaction Zelle — réception USD et remise HTG."""
    __tablename__ = "zelle_transactions"

    id                  = Column(Integer, primary_key=True)
    numero_int          = Column(String(30),  nullable=True)
    nom_prenom          = Column(String(150), nullable=False)
    identifiant         = Column(String(150), nullable=True)
    contact             = Column(String(100), nullable=True)
    expediteur_nom      = Column(String(150), nullable=True)
    expediteur_contact  = Column(String(100), nullable=True)
    montant_usd         = Column(Numeric(14, 2), nullable=False)
    frais               = Column(Numeric(14, 2), nullable=False, default=0)
    taux_applique       = Column(Numeric(10, 4), nullable=False, default=130)
    statut              = Column(String(20), nullable=False, default="EN_ATTENTE")
    source_fond         = Column(String(50), nullable=True)
    date_transaction    = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    notes               = Column(String(300), nullable=True)

    __table_args__ = (
        CheckConstraint("montant_usd > 0",   name="chk_zelle_montant_pos"),
        CheckConstraint("frais >= 0",         name="chk_zelle_frais_pos"),
        CheckConstraint("statut IN ('EN_ATTENTE','REMIS','ANNULE')", name="chk_zelle_statut"),
        Index("idx_zelle_date",   "date_transaction"),
        Index("idx_zelle_statut", "statut"),
    )


class ZelleFond(Base):
    """Réception de fonds dans le département Zelle (renflouement de la caisse)."""
    __tablename__ = "zelle_fonds"

    id             = Column(Integer, primary_key=True)
    montant_usd    = Column(Numeric(14, 2), nullable=False)
    source         = Column(String(50), nullable=False, default="PDG")
    date_reception = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    notes          = Column(String(300), nullable=True)

    __table_args__ = (
        CheckConstraint("montant_usd > 0", name="chk_fond_montant_pos"),
        Index("idx_fond_date", "date_reception"),
    )


class Notification(Base):
    """Événement notifiable du système — partagé entre tous les utilisateurs.
    Le statut lu/non-lu/supprimé est individuel, voir NotificationEtat.
    Le statut résolu est global : résoudre un problème (ex: stock bas traité)
    est un fait objectif sur le systeme, pas une préférence de lecture
    personnelle — donc visible identiquement par tous les utilisateurs."""
    __tablename__ = "notifications"

    id            = Column(Integer, primary_key=True)
    module        = Column(String(30),  nullable=False)   # carburant, hotel, cuisine, pos, zelle, rh, systeme
    type          = Column(String(50),  nullable=False)    # ex: 'stock_bas', 'nouvelle_reservation'...
    titre         = Column(String(200), nullable=False)
    message       = Column(String(500), nullable=True)
    lien          = Column(String(100), nullable=True)     # page frontend a ouvrir au clic (data-page)
    created_at    = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    resolu        = Column(Boolean, nullable=False, default=False)
    resolu_par_id = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    resolu_at     = Column(DateTime(timezone=True), nullable=True)

    resolu_par = relationship("Utilisateur")

    __table_args__ = (
        Index("idx_notif_created", "created_at"),
        Index("idx_notif_module",  "module"),
        Index("idx_notif_resolu",  "resolu"),
    )


class NotificationEtat(Base):
    """Statut lu/non-lu/supprimé d'une notification, propre à chaque utilisateur.
    Absence de ligne = non-lu, non-supprimé (état par défaut)."""
    __tablename__ = "notification_etats"

    id              = Column(Integer, primary_key=True)
    notification_id = Column(Integer, ForeignKey("notifications.id",  ondelete="CASCADE"), nullable=False)
    utilisateur_id  = Column(Integer, ForeignKey("utilisateurs.id",   ondelete="CASCADE"), nullable=False)
    lu              = Column(Boolean, nullable=False, default=False)
    supprime        = Column(Boolean, nullable=False, default=False)
    lu_at           = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("notification_id", "utilisateur_id", name="uq_notif_etat_user"),
        Index("idx_notif_etat_user", "utilisateur_id"),
    )


class ZelleDepense(Base):
    """Dépense payée depuis le fonds Zelle (USD) — reste EN_ATTENTE et ne
    réduit jamais le solde disponible tant que le PDG ne l'a pas approuvée."""
    __tablename__ = "zelle_depenses"

    id            = Column(Integer, primary_key=True)
    description   = Column(String(200), nullable=False)
    montant_usd   = Column(Numeric(14, 2), nullable=False)
    # Taux HTG/USD au moment de la saisie — fige la conversion (comme
    # ZelleTransaction.taux_applique) meme si la saisie initiale etait en HTG.
    taux_applique = Column(Numeric(10, 4), nullable=False, default=130)
    categorie     = Column(String(50), nullable=True)
    date_depense  = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    statut        = Column(String(20), nullable=False, default="EN_ATTENTE")  # EN_ATTENTE, APPROUVEE, REJETEE
    demandeur_id  = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    valide_par_id = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    valide_at     = Column(DateTime(timezone=True), nullable=True)
    notes         = Column(String(300), nullable=True)
    created_at    = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    demandeur  = relationship("Utilisateur", foreign_keys=[demandeur_id])
    valide_par = relationship("Utilisateur", foreign_keys=[valide_par_id])

    __table_args__ = (
        CheckConstraint("montant_usd > 0", name="chk_zelle_dep_montant_pos"),
        CheckConstraint("statut IN ('EN_ATTENTE','APPROUVEE','REJETEE')", name="chk_zelle_dep_statut"),
        Index("idx_zelle_dep_statut", "statut"),
        Index("idx_zelle_dep_date",   "date_depense"),
    )


class RenflouementCaisse(Base):
    """Injection manuelle de fonds (HTG) dans la Grande Caisse — capital,
    prêt, transfert externe... distincte des ventes. Compte comme entrée
    dans la synthèse cash consolidée de l'institution (voir
    _synthese_cash_institution dans main.py). Réservé au PDG/admin."""
    __tablename__ = "renflouements_caisse"

    id                 = Column(Integer, primary_key=True)
    montant            = Column(Numeric(14, 2), nullable=False)
    source             = Column(String(100), nullable=True)
    date_renflouement  = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    enregistre_par_id  = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    notes              = Column(String(300), nullable=True)

    enregistre_par = relationship("Utilisateur")

    __table_args__ = (
        CheckConstraint("montant > 0", name="chk_renfl_caisse_montant_pos"),
        Index("idx_renfl_caisse_date", "date_renflouement"),
    )


class RenflouementDepartement(Base):
    """Injection manuelle de fonds dans la caisse d'un département
    opérationnel (Hôtel, Cuisine, Bar) — distinct du renflouement de la
    Grande Caisse (institution entière, voir RenflouementCaisse). Compte
    comme entrée dans le calcul de la caisse disponible de ce département.
    Réservé au PDG/admin."""
    __tablename__ = "renflouements_departement"

    id                 = Column(Integer, primary_key=True)
    departement        = Column(String(20), nullable=False)  # HOTEL, CUISINE, BAR
    montant            = Column(Numeric(14, 2), nullable=False)
    source             = Column(String(100), nullable=True)
    date_renflouement  = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    enregistre_par_id  = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    notes              = Column(String(300), nullable=True)

    enregistre_par = relationship("Utilisateur")

    __table_args__ = (
        CheckConstraint("montant > 0", name="chk_renfl_dept_montant_pos"),
        CheckConstraint("departement IN ('HOTEL','CUISINE','BAR')", name="chk_renfl_dept_departement"),
        Index("idx_renfl_dept_date", "date_renflouement"),
        Index("idx_renfl_dept_departement", "departement"),
    )


class Tache(Base):
    """Tâche planifiée par un employé — individuelle ou collective selon le
    nombre de participants. Statut unique partagé entre tous les participants
    (n'importe lequel peut le faire évoluer)."""
    __tablename__ = "taches"

    id            = Column(Integer, primary_key=True)
    titre         = Column(String(200), nullable=False)
    description   = Column(String(1000), nullable=True)
    createur_id   = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    date_echeance = Column(Date, nullable=True)
    statut        = Column(String(20), nullable=False, default="A_FAIRE")  # A_FAIRE, EN_COURS, TERMINEE, ANNULEE
    created_at    = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at    = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    createur     = relationship("Utilisateur")
    participants = relationship("TacheParticipant", back_populates="tache", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("statut IN ('A_FAIRE','EN_COURS','TERMINEE','ANNULEE')", name="chk_tache_statut"),
        Index("idx_tache_createur",  "createur_id"),
        Index("idx_tache_statut",    "statut"),
        Index("idx_tache_echeance",  "date_echeance"),
    )


class TacheParticipant(Base):
    """Employé sélectionné pour voir/participer à une tâche."""
    __tablename__ = "tache_participants"

    id             = Column(Integer, primary_key=True)
    tache_id       = Column(Integer, ForeignKey("taches.id",       ondelete="CASCADE"), nullable=False)
    utilisateur_id = Column(Integer, ForeignKey("utilisateurs.id", ondelete="CASCADE"), nullable=False)

    tache       = relationship("Tache", back_populates="participants")
    utilisateur = relationship("Utilisateur")

    __table_args__ = (
        UniqueConstraint("tache_id", "utilisateur_id", name="uq_tache_participant"),
        Index("idx_tache_part_user", "utilisateur_id"),
    )


class PieceJointe(Base):
    """Document justificatif (facture, reçu…) attaché à une dépense/achat,
    quel que soit le département — système générique via (type_entite,
    entite_id) plutôt qu'une FK par table de dépense."""
    __tablename__ = "pieces_jointes"

    id              = Column(Integer, primary_key=True)
    type_entite     = Column(String(30),  nullable=False)  # depense, achat, cuisine_depense, cuisine_achat, zelle_depense, bar_achat
    entite_id       = Column(Integer,     nullable=False)
    nom_fichier     = Column(String(255), nullable=False)
    type_mime       = Column(String(50),  nullable=False)
    taille_octets   = Column(Integer,     nullable=False)
    contenu_base64  = Column(Text,        nullable=False)
    uploaded_par_id = Column(Integer, ForeignKey("utilisateurs.id", ondelete="SET NULL"), nullable=True)
    created_at      = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    uploaded_par = relationship("Utilisateur")

    __table_args__ = (
        Index("idx_pj_entite", "type_entite", "entite_id"),
    )


class ChatConversation(Base):
    """Conversation avec l'assistant IA — historique persistant par utilisateur."""
    __tablename__ = "chat_conversations"

    id             = Column(Integer, primary_key=True)
    utilisateur_id = Column(Integer, ForeignKey("utilisateurs.id", ondelete="CASCADE"), nullable=False)
    titre          = Column(String(200), nullable=True)
    historique     = Column(JSON, nullable=False, default=list)
    created_at     = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at     = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    utilisateur = relationship("Utilisateur")

    __table_args__ = (
        Index("idx_chat_conv_utilisateur", "utilisateur_id"),
        Index("idx_chat_conv_updated", "updated_at"),
    )
