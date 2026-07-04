"""bar_audit_constraints — contraintes d'intégrité bar (audit fix)

Revision ID: l001m0000001
Revises: k001l0000001
Create Date: 2026-07-04
"""
from alembic import op
import sqlalchemy as sa

revision     = 'l001m0000001'
down_revision = 'k001l0000001'
branch_labels = None
depends_on    = None


def upgrade():
    conn = op.get_bind()

    # 1. BarProduit — unicité du nom (si pas encore présente)
    exists = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.table_constraints "
        "WHERE constraint_name='uq_bar_produit_nom' AND table_name='bar_produits'"
    )).fetchone()
    if not exists:
        op.create_unique_constraint("uq_bar_produit_nom", "bar_produits", ["nom"])

    # 2. BarAchat — contrainte statut (déjà présente dans certains environnements)
    exists = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.table_constraints "
        "WHERE constraint_name='chk_bar_achat_statut' AND table_name='bar_achats'"
    )).fetchone()
    if not exists:
        op.create_check_constraint(
            "chk_bar_achat_statut",
            "bar_achats",
            "statut IN ('EN_ATTENTE','CONFIRME','ANNULE')",
        )

    # 3. BarLigneVente — cuisine_plat_id : SET NULL → RESTRICT
    #    PostgreSQL ne permet pas ALTER CONSTRAINT directement ; il faut DROP + ADD.
    row = conn.execute(sa.text(
        "SELECT confdeltype FROM pg_constraint WHERE conname='bar_lignes_vente_cuisine_plat_id_fkey'"
    )).fetchone()
    if row and row[0] != 'r':   # 'r' = RESTRICT, 'n' = SET NULL
        op.drop_constraint("bar_lignes_vente_cuisine_plat_id_fkey", "bar_lignes_vente", type_="foreignkey")
        op.create_foreign_key(
            "bar_lignes_vente_cuisine_plat_id_fkey",
            "bar_lignes_vente", "cuisine_plats",
            ["cuisine_plat_id"], ["id"],
            ondelete="RESTRICT",
        )


def downgrade():
    # 3. Revenir à SET NULL
    op.drop_constraint("bar_lignes_vente_cuisine_plat_id_fkey", "bar_lignes_vente", type_="foreignkey")
    op.create_foreign_key(
        "bar_lignes_vente_cuisine_plat_id_fkey",
        "bar_lignes_vente", "cuisine_plats",
        ["cuisine_plat_id"], ["id"],
        ondelete="SET NULL",
    )

    # 2. Supprimer la contrainte statut
    op.drop_constraint("chk_bar_achat_statut", "bar_achats", type_="check")

    # 1. Supprimer l'unicité nom
    op.drop_constraint("uq_bar_produit_nom", "bar_produits", type_="unique")
