"""Fuseau horaire métier — le serveur (Railway) tourne en UTC, mais toutes les
institutions opèrent en Haïti. Toute notion de "date du jour" utilisée pour de
la logique métier (limites mensuelles, stock du jour, dashboard, rapports)
doit passer par ce module plutôt que par date.today()/datetime.now(), qui
donnent la date UTC — en avance de 4 à 5h sur Haïti selon la saison, ce qui
fait basculer "aujourd'hui" sur le jour suivant chaque soir entre ~19h et
minuit, heure d'Haïti.
"""
from datetime import date, datetime
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
