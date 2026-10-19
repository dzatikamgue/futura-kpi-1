"""Grille KPI par poste.

Aucune structure ajoutée. Les critères restent dans la table `criteres` (la bibliothèque de
l'entité). Mémorisés dans la table de réglages existante (Parametre) :
  - « grilles_postes »       : {entite_id: {clé_poste: {"libelle": poste, "criteres": {critere_id: poids}}}}
  - « criteres_specifiques » : {entite_id: [critere_id, …]} — critères créés pour un poste,
                                exclus de la grille commune.

Règle : un salarié est noté avec la grille de son poste si elle existe (au moins un critère
actif), sinon avec la grille commune (critères actifs non spécifiques).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from flask import g
from sqlalchemy import select

from ..extensions import db
from ..models import Critere, Parametre
from .entites import cond_entite
from .organisation import cle_texte

CLE_GRILLES = "grilles_postes"
CLE_SPECIFIQUES = "criteres_specifiques"


@dataclass
class CritereGrille:
    """Critère tel qu'il s'applique dans une grille (poids propre au poste le cas échéant)."""
    critere: Critere
    poids: int

    @property
    def id(self):
        return self.critere.id

    @property
    def libelle(self):
        return self.critere.libelle

    @property
    def description(self):
        return self.critere.description


def _lire(cle: str) -> dict:
    cache = g.setdefault("_reglages_grilles", {})
    if cle not in cache:
        p = db.session.get(Parametre, cle)
        try:
            cache[cle] = json.loads(p.valeur) if p else {}
        except ValueError:
            cache[cle] = {}
    return cache[cle]


def _ecrire(cle: str, valeur: dict, utilisateur_id: int | None) -> None:
    texte = json.dumps(valeur, ensure_ascii=False)
    p = db.session.get(Parametre, cle)
    if p:
        p.valeur, p.updated_by_id = texte, utilisateur_id
    else:
        db.session.add(Parametre(cle=cle, valeur=texte, updated_by_id=utilisateur_id))
    g.setdefault("_reglages_grilles", {})[cle] = valeur


# --- Lecture -------------------------------------------------------------------
def specifiques(entite_id: int) -> set[int]:
    return {int(x) for x in _lire(CLE_SPECIFIQUES).get(str(entite_id), [])}


def grilles(entite_id: int) -> dict:
    """{clé_poste: {"libelle": poste, "criteres": {critere_id(int): poids}}}"""
    brut = _lire(CLE_GRILLES).get(str(entite_id), {})
    return {k: {"libelle": v.get("libelle", k),
                "criteres": {int(i): _nombre(p) for i, p in v.get("criteres", {}).items()}}
            for k, v in brut.items()}


def _nombre(x):
    """Coefficient stocké → int s'il est entier, sinon float (2 décimales)."""
    f = round(float(x), 2)
    return int(f) if f.is_integer() else f


def _criteres_actifs(entite_id: int) -> list[Critere]:
    return db.session.scalars(select(Critere).where(Critere.actif.is_(True), cond_entite(Critere.entite_id, entite_id))
                              .order_by(Critere.ordre, Critere.id)).all()


def grille_commune(entite_id: int) -> list[CritereGrille]:
    spe = specifiques(entite_id)
    return [CritereGrille(c, coefficient(c)) for c in _criteres_actifs(entite_id) if c.id not in spe]


def grille_du_poste(entite_id: int, poste: str | None) -> list[CritereGrille] | None:
    """Grille propre au poste (critères actifs), ou None si le poste n'en a pas."""
    if not poste:
        return None
    defn = grilles(entite_id).get(cle_texte(poste))
    if not defn or not defn["criteres"]:
        return None
    # Ordre propre au poste : celui de la grille enregistrée
    actifs = {c.id: c for c in _criteres_actifs(entite_id)}
    res = [CritereGrille(actifs[i], p) for i, p in defn["criteres"].items() if i in actifs]
    return res or None


def grille_pour(entite_id: int, poste: str | None) -> tuple[list[CritereGrille], bool]:
    """(critères, True si grille propre au poste)."""
    propre = grille_du_poste(entite_id, poste)
    return (propre, True) if propre else (grille_commune(entite_id), False)


# --- Écriture ------------------------------------------------------------------
def definir_grille(entite_id: int, poste: str, criteres: dict[int, int], utilisateur_id: int | None) -> None:
    """Enregistre la grille d'un poste ; un dictionnaire vide revient à la grille commune."""
    tout = _lire(CLE_GRILLES)
    ent = dict(tout.get(str(entite_id), {}))
    k = cle_texte(poste)
    if criteres:
        ent[k] = {"libelle": poste, "criteres": {str(i): _nombre(p) for i, p in criteres.items()}}
    else:
        ent.pop(k, None)
    tout = dict(tout)
    tout[str(entite_id)] = ent
    _ecrire(CLE_GRILLES, tout, utilisateur_id)


def marquer_specifique(entite_id: int, critere_id: int, specifique: bool, utilisateur_id: int | None) -> None:
    tout = dict(_lire(CLE_SPECIFIQUES))
    ids = set(int(x) for x in tout.get(str(entite_id), []))
    (ids.add if specifique else ids.discard)(critere_id)
    tout[str(entite_id)] = sorted(ids)
    _ecrire(CLE_SPECIFIQUES, tout, utilisateur_id)


def renommer_poste(entite_id: int, ancien: str, nouveau: str, utilisateur_id: int | None) -> None:
    """Suit un renommage de poste (la grille reste attachée au poste)."""
    gr = grilles(entite_id)
    defn = gr.get(cle_texte(ancien))
    if defn:
        definir_grille(entite_id, ancien, {}, utilisateur_id)
        definir_grille(entite_id, nouveau, defn["criteres"], utilisateur_id)


def postes_utilisant(entite_id: int, critere_id: int) -> list[str]:
    return [v["libelle"] for v in grilles(entite_id).values() if critere_id in v["criteres"]]


# --- Suppression de critères ---------------------------------------------------
CLE_MASQUES = "criteres_supprimes"
CLE_SANS_DEFAUT = "grilles_sans_defaut"


def masques(entite_id: int) -> set[int]:
    """Critères supprimés par la RH mais déjà utilisés dans des évaluations : cachés, gardés pour l'historique."""
    return {int(x) for x in _lire(CLE_MASQUES).get(str(entite_id), [])}


def _maj_ensemble(cle: str, entite_id: int, ids: set[int], ajouter: bool, utilisateur_id: int | None) -> None:
    tout = dict(_lire(cle))
    actuels = {int(x) for x in tout.get(str(entite_id), [])}
    actuels = (actuels | ids) if ajouter else (actuels - ids)
    tout[str(entite_id)] = sorted(actuels)
    _ecrire(cle, tout, utilisateur_id)


def reprendre_masque(entite_id: int, critere: Critere, utilisateur_id: int | None) -> None:
    """Un critère supprimé est recréé sous le même libellé : on réutilise la ligne existante."""
    _maj_ensemble(CLE_MASQUES, entite_id, {critere.id}, False, utilisateur_id)
    critere.actif = True


def supprimer_criteres(entite_id: int, ids: set[int], utilisateur_id: int | None) -> tuple[int, int]:
    """Supprime des critères de l'entité. Renvoie (supprimés définitivement, cachés car déjà utilisés).

    Un critère déjà noté dans une évaluation n'est pas effacé (les notes passées en dépendent) :
    il est désactivé et retiré de toutes les listes. Les grilles de poste sont nettoyées.
    """
    from ..models import EvaluationNote
    criteres = db.session.scalars(select(Critere).where(Critere.id.in_(ids or {-1}),
                                                        cond_entite(Critere.entite_id, entite_id))).all()
    ids = {c.id for c in criteres}
    if not ids:
        return 0, 0
    utilises = set(db.session.scalars(select(EvaluationNote.critere_id).where(
        EvaluationNote.critere_id.in_(ids)).distinct()))
    # Grilles de poste : retirer ces critères
    tout = dict(_lire(CLE_GRILLES))
    ent = {k: dict(v) for k, v in tout.get(str(entite_id), {}).items()}
    for k, v in list(ent.items()):
        crit = {i: p for i, p in v.get("criteres", {}).items() if int(i) not in ids}
        if crit:
            ent[k] = {**v, "criteres": crit}
        else:
            ent.pop(k)
    tout[str(entite_id)] = ent
    _ecrire(CLE_GRILLES, tout, utilisateur_id)
    _maj_ensemble(CLE_SPECIFIQUES, entite_id, ids, False, utilisateur_id)
    definitifs, caches = 0, 0
    for c in criteres:
        if c.id in utilises:
            c.actif = False
            caches += 1
        else:
            db.session.delete(c)
            definitifs += 1
    _maj_ensemble(CLE_MASQUES, entite_id, ids & utilises, True, utilisateur_id)
    # Ne jamais recréer la grille par défaut au démarrage : la RH a choisi de la redéfinir
    marquer_sans_defaut(entite_id, utilisateur_id)
    return definitifs, caches


def marquer_sans_defaut(entite_id: int, utilisateur_id: int | None) -> None:
    from .entites import principale
    cles = {str(entite_id)} | ({"principale"} if entite_id == principale().id else set())
    tout = dict(_lire(CLE_SANS_DEFAUT))
    tout["entites"] = sorted(set(tout.get("entites", [])) | cles)
    _ecrire(CLE_SANS_DEFAUT, tout, utilisateur_id)


def sans_defaut(entite_id: int | None) -> bool:
    """La RH a vidé la grille de cette entité (None = principale) : pas de grille par défaut."""
    p = db.session.get(Parametre, CLE_SANS_DEFAUT)
    try:
        cles = set(json.loads(p.valeur).get("entites", [])) if p else set()
    except ValueError:
        cles = set()
    return ("principale" if entite_id is None else str(entite_id)) in cles


# --- Coefficients libres (0,01 à 100, décimales autorisées) ------------------------
# La table des critères limite le poids à un entier 1–5 et celle des notes à un entier :
# le coefficient réel est mémorisé dans Parametre — « coefficients » {critere_id: coef} pour la
# grille commune, et « coefficients_notes » {"evaluation_id:critere_id": coef} pour garder dans
# l'historique un coefficient décimal utilisé lors d'une notation. Aucune colonne modifiée.
CLE_COEFFS = "coefficients"
CLE_COEFFS_NOTES = "coefficients_notes"
COEF_MIN, COEF_MAX = 0.01, 100


def lire_coefficient(valeur, defaut=1):
    """Coefficient saisi (« 1,5 » ou « 1.5 ») → nombre de 0,01 à 100 (2 décimales), ou None si invalide."""
    texte = str(valeur if valeur is not None else "").strip().replace(",", ".").replace(" ", "")
    try:
        n = round(float(texte or defaut), 2)
    except (TypeError, ValueError):
        return None
    if n != n or not COEF_MIN <= n <= COEF_MAX:        # NaN ou hors limites
        return None
    return _nombre(n)


def coefficient(c: Critere):
    return _nombre(_lire(CLE_COEFFS).get(str(c.id), c.poids))


def definir_coefficient(c: Critere, n, utilisateur_id: int | None) -> None:
    c.poids = min(max(int(round(n)), 1), 5)          # valeur compatible avec la contrainte de la table
    tout = dict(_lire(CLE_COEFFS))
    if n == c.poids:
        tout.pop(str(c.id), None)
    else:
        tout[str(c.id)] = _nombre(n)
    _ecrire(CLE_COEFFS, tout, utilisateur_id)


def poids_pour_note(n) -> int:
    """Valeur entière à ranger dans la colonne des notes (le coefficient exact va dans Parametre)."""
    return max(1, int(round(n)))


def memoriser_coefficients_note(evaluation_id: int, coefs: dict[int, float], utilisateur_id: int | None) -> None:
    """Garde le coefficient exact (décimal) de chaque critère d'une évaluation."""
    tout = dict(_lire(CLE_COEFFS_NOTES))
    for cid, n in coefs.items():
        cle = f"{evaluation_id}:{cid}"
        if float(n).is_integer():
            tout.pop(cle, None)
        else:
            tout[cle] = _nombre(n)
    _ecrire(CLE_COEFFS_NOTES, tout, utilisateur_id)


def poids_note(note) -> float | int:
    """Coefficient exact d'une note d'évaluation (décimal si besoin)."""
    v = _lire(CLE_COEFFS_NOTES).get(f"{note.evaluation_id}:{note.critere_id}")
    return _nombre(v) if v is not None else note.poids


def format_coef(n) -> str:
    """1.5 → « 1,5 » ; 2 → « 2 »."""
    n = _nombre(n)
    return str(n).replace(".", ",")
