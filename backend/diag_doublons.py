"""
Outil ponctuel, LECTURE SEULE : recherche les enregistrements probablement
en double créés AVANT la mise en place de l'anti-doublon (connexion
instable / double clic) — même contenu, à moins de 2 minutes d'écart.

N'écrit rien. Les résultats sont des CANDIDATS : deux ventes identiques à
une minute d'écart peuvent être légitimes (deux clients, même boisson). La
décision de supprimer revient à l'administrateur.

Script ponctuel : à retirer du preDeployCommand (railway.json) et du dépôt
après lecture des logs, comme les autres outils ponctuels de cette session.
"""
import os

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL", "")
FENETRE_S = 120

# (table, horodatage, colonnes identiques, filtre, colonnes affichées)
_CIBLES = [
    ("hotel_reservations", "created_at", ["chambre_id", "client_nom", "type_sejour", "montant_total"],
     "a.statut <> 'ANNULEE'", ["chambre_id", "client_nom", "montant_total", "statut"]),
    ("bar_remboursements", "date_remb", ["credit_id", "montant"], "TRUE", ["credit_id", "montant"]),
    ("depenses", "created_at", ["description", "montant", "categorie"], "TRUE", ["description", "montant"]),
    ("achats", "created_at", ["description", "montant", "fournisseur"], "TRUE", ["description", "montant"]),
    ("hotel_depenses", "date_depense", ["description", "montant"], "TRUE", ["description", "montant"]),
    ("cuisine_depenses", "date_depense", ["description", "montant"], "TRUE", ["description", "montant"]),
    ("bar_achats", "date_achat", ["produit_id", "quantite", "prix_achat_unitaire"], "a.statut <> 'ANNULE'",
     ["produit_id", "quantite", "statut"]),
    ("bar_ventes", "date_heure", ["caissier_id", "montant_total", "mode_paiement"], "a.statut <> 'ANNULEE'",
     ["numero_ticket", "montant_total", "mode_paiement"]),
    ("patisserie_ventes", "date_heure", ["caissier_id", "montant_total", "mode_paiement"], "a.statut <> 'ANNULEE'",
     ["numero_ticket", "montant_total", "mode_paiement"]),
    ("cuisine_ventes", "date_heure", ["total", "mode_paiement"], "a.statut <> 'ANNULEE'",
     ["numero_ticket", "total", "mode_paiement"]),
]


def main() -> None:
    if not DATABASE_URL.startswith("postgresql"):
        print("diag_doublons : base non-Postgres ou absente — ignoré.")
        return
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        for table, ts, egal, filtre, affiche in _CIBLES:
            try:
                cond = " AND ".join(f"a.{c} IS NOT DISTINCT FROM b.{c}" for c in egal)
                filtre_b = filtre.replace("a.", "b.")
                cols = ", ".join([f"a.{c}" for c in affiche])
                sql = (
                    f"SELECT a.id, b.id, a.{ts}, b.{ts}, {cols} FROM {table} a JOIN {table} b "
                    f"ON a.id < b.id AND {cond} "
                    f"AND ABS(EXTRACT(EPOCH FROM (b.{ts} - a.{ts}))) <= {FENETRE_S} "
                    f"WHERE {filtre} AND {filtre_b} AND a.{ts} >= '2026-09-29' "
                    f"ORDER BY a.{ts} DESC LIMIT 25"
                )
                lignes = conn.execute(text(sql)).fetchall()
            except Exception as e:
                conn.rollback()
                print(f"diag_doublons : {table} — vérification impossible ({type(e).__name__}).")
                continue
            if not lignes:
                print(f"diag_doublons : {table} — aucun doublon probable.")
                continue
            print(f"diag_doublons : {table} — {len(lignes)} paire(s) suspecte(s) :")
            for l in lignes:
                ecart = f"{abs((l[3] - l[2]).total_seconds()):.0f} s" if l[2] and l[3] else "?"
                details = " | ".join(f"{c}={v!r}" for c, v in zip(affiche, l[4:]))
                print(f"diag_doublons :   ids {l[0]} et {l[1]} — {ecart} d'écart — {details} — le {l[2]}")
    print("diag_doublons : terminé (lecture seule, rien modifié).")


if __name__ == "__main__":
    # Diagnostic seulement : une erreur ici ne doit jamais bloquer le déploiement.
    try:
        main()
    except Exception as e:
        print(f"diag_doublons : erreur ignorée ({type(e).__name__}: {e}).")
