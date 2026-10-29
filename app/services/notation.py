"""Calcul des notes, niveaux de performance et règles de période."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from flask import current_app

MOIS = ["Janvier", "Février", "Mars", "Avril", "Mai", "Juin", "Juillet",
        "Août", "Septembre", "Octobre", "Novembre", "Décembre"]
MOIS_COURTS = ["Janv.", "Févr.", "Mars", "Avr.", "Mai", "Juin", "Juil.",
               "Août", "Sept.", "Oct.", "Nov.", "Déc."]

NOTE_MIN, NOTE_MAX = 0, 100
SEUIL_JUSTIFICATION = 60  # une note de critère < 60 doit être commentée

# Seuils sur 100 → (code, libellé) ; utilisés pour les critères ET la note globale
NIVEAUX = [
    # (borne basse incluse, code couleur, libellé) — barème Futura
    (95, "excellent", "Excellent"),
    (90, "tres-bien", "Très bien"),
    (85, "bien", "Bien"),
    (80, "passable", "Acceptable"),
    (0, "insuffisant", "Insuffisant — amélioration nécessaire"),
]

# Légende affichée aux évaluateurs (borne basse, libellé, description)
ECHELLE = [
    (95, "Excellent", "95 et plus — performance exceptionnelle, exemplaire"),
    (90, "Très bien", "90 à 94 — dépasse régulièrement les attentes"),
    (85, "Bien", "85 à 89 — répond pleinement aux attentes du poste"),
    (80, "Acceptable", "80 à 84 — conforme, des progrès sont attendus"),
    (0, "Insuffisant — amélioration nécessaire", "Moins de 80 — en dessous des attentes"),
]


def calculer_note_globale(notes_poids: list[tuple[int, int]]) -> float | None:
    """Moyenne pondérée des notes de critères (0–100) : note globale sur 100."""
    total_poids = sum(p for _, p in notes_poids)
    if not total_poids:
        return None
    return round(sum(n * p for n, p in notes_poids) / total_poids, 2)


def niveau(note: float | None) -> tuple[str, str]:
    if note is None:
        return "aucun", "—"
    for seuil, code, libelle in NIVEAUX:
        if note >= seuil:
            return code, libelle
    return "insuffisant", "Insuffisant — amélioration nécessaire"


def trimestre_de(mois: int) -> int:
    return (mois - 1) // 3 + 1


def mois_du_trimestre(t: int) -> list[int]:
    return [3 * (t - 1) + 1, 3 * (t - 1) + 2, 3 * (t - 1) + 3]


def libelle_periode(annee: int, mois: int) -> str:
    return f"{MOIS[mois - 1]} {annee}"


def mois_precedent(annee: int, mois: int) -> tuple[int, int]:
    return (annee - 1, 12) if mois == 1 else (annee, mois - 1)


def aujourdhui_local() -> date:
    """Date du jour à Douala (le serveur Railway tourne en UTC)."""
    return datetime.now(ZoneInfo("Africa/Douala")).date()


def periode_courante() -> tuple[int, int]:
    d = aujourdhui_local()
    return d.year, d.month


def periodes_saisissables(aujourdhui: date | None = None) -> list[tuple[int, int]]:
    """Mois ouverts à la saisie pour les évaluateurs.

    Le mois en cours, plus le mois précédent jusqu'au jour limite (10 par défaut)
    du mois en cours : laisse le temps de noter le mois écoulé.
    """
    d = aujourdhui or aujourdhui_local()
    courant = (d.year, d.month)
    limite = current_app.config.get("SAISIE_JOUR_LIMITE", 10)
    periodes = [courant]
    if d.day <= limite:
        periodes.insert(0, mois_precedent(*courant))
    return periodes


def periode_ouverte(annee: int, mois: int) -> bool:
    return (annee, mois) in periodes_saisissables()


def moyenne(valeurs) -> float | None:
    v = [x for x in valeurs if x is not None]
    return round(sum(v) / len(v), 2) if v else None
