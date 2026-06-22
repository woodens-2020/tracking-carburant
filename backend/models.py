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

    id            = Column(Integer, primary_key=True)
    username      = Column(String(80), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    api_key_hash  = Column(String(64), unique=True, nullable=True)
    nom_complet   = Column(String(150), nullable=False, default="")
    role          = Column(String(20), nullable=False, default="operateur")
    actif         = Column(Boolean, nullable=False, default=True)
    created_at    = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("role IN ('admin', 'operateur')", name="chk_utilisateur_role"),
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
