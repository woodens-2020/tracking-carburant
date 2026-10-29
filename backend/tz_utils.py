"""Fuseau horaire métier — le serveur (Railway) tourne en UTC, mais toutes les
institutions opèrent en Haïti. Toute notion de "date du jour" utilisée pour de
la logique métier (limites mensuelles, stock du jour, dashboard, rapports)
doit passer par ce module plutôt que par date.today()/datetime.now(), qui
donnent la date UTC — en avance de 4 à 5h sur Haïti selon la saison, ce qui
fait basculer "aujourd'hui" sur le jour suivant chaque soir entre ~19h et
minuit, heure d'Haïti.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HAITI_TZ = ZoneInfo("America/Port-au-Prince")


def now_haiti() -> datetime:
    return datetime.now(HAITI_TZ)


def today_haiti() -> date:
    return now_haiti().date()


def bounds_haiti(jour: date) -> tuple[datetime, datetime]:
    """Bornes [début, fin] du jour calendaire Haïti `jour`, en instants
    tz-aware corrects pour filtrer une colonne timestamptz (ex. date_heure) —
    à utiliser à la place de `datetime.combine(jour, ...).replace(tzinfo=timezone.utc)`,
    qui traite à tort `jour` comme un jour calendaire UTC plutôt qu'Haïti."""
    debut = datetime.combine(jour, datetime.min.time(), tzinfo=HAITI_TZ)
    fin   = datetime.combine(jour, datetime.max.time(), tzinfo=HAITI_TZ)
    return debut, fin


def _aware(dt: datetime) -> datetime:
    # SQLite (tests, démo locale) rend des datetimes naïfs, Postgres des
    # datetimes tz-aware : on normalise en UTC pour pouvoir les comparer.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def fin_fenetre_session(session, suivante) -> datetime:
    """Fin [exclue] de la fenêtre temporelle d'une session de caisse (Bar ou
    Pâtisserie) — sert à rattacher par le temps les ventes sans session_id.

    Une session dure jusqu'à sa soumission, PAS jusqu'à minuit : une caissière
    qui travaille de 18h à 2h du matin reste sur la même session. D'où :
      - session EN_COURS → pas de fin (borne lointaine) ;
      - session soumise  → fin de son jour calendaire, ou l'heure de
        soumission si elle a eu lieu après minuit ;
      - dans tous les cas, jamais au-delà de l'ouverture de la session
        suivante du même caissier (`suivante`, quel que soit son jour)."""
    _, jour_fin = bounds_haiti(session.date_session)
    if session.statut == "EN_COURS":
        fin = now_haiti() + timedelta(days=1)
    elif session.soumis_at is not None:
        fin = max(jour_fin, _aware(session.soumis_at))
    else:
        fin = jour_fin
    if suivante is not None:
        fin = min(_aware(fin), _aware(suivante.created_at))
    # En UTC : indifférent pour Postgres (timestamptz), indispensable pour
    # SQLite qui stocke l'heure murale sans fuseau.
    return fin.astimezone(timezone.utc)
