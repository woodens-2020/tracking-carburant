"""bar_mouvement_volontaire — ajoute VOLONTAIRE comme type de mouvement de stock

Un ajustement "volontaire" (dégustation, offert, consommation interne) est
une sortie de stock déclenchée délibérément par le personnel — distincte
d'une PERTE (accidentelle, ex. vol/évaporation) ou d'une CASSE (bris),
utile pour que le rapport de mouvements distingue les deux causes plutôt
que de tout regrouper sous "Perte".

Revision ID: r001s0000001
Revises: q001r0000001
Create Date: 2026-09-16
"""
from alembic import op
import sqlalchemy as sa

revision      = 'r001s0000001'
down_revision = 'q001r0000001'
branch_labels = None
depends_on    = None


def upgrade():
    # Postgres ne permet pas d'ALTER un CHECK CONSTRAINT directement :
    # DROP puis CREATE avec la liste élargie.
    op.drop_constraint("chk_bar_mouv_type", "bar_mouvements_stock", type_="check")
    op.create_check_constraint(
        "chk_bar_mouv_type",
        "bar_mouvements_stock",
        "type_mouvement IN ('ENTREE','SORTIE_VENTE','AJUSTEMENT','PERTE','CASSE','VOLONTAIRE')",
    )


def downgrade():
    # Aucune ligne VOLONTAIRE ne peut exister avant cette migration ; le
    # downgrade retire simplement la valeur autorisée en plus.
    op.drop_constraint("chk_bar_mouv_type", "bar_mouvements_stock", type_="check")
    op.create_check_constraint(
        "chk_bar_mouv_type",
        "bar_mouvements_stock",
        "type_mouvement IN ('ENTREE','SORTIE_VENTE','AJUSTEMENT','PERTE','CASSE')",
    )
