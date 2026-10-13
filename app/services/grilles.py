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
    return {k: {"libelle": v.get("libelle", k), "criteres": {int(i): int(p) for i, p in v.get("criteres", {}).items()}}
            for k, v in brut.items()}


def _criteres_actifs(entite_id: int) -> list[Critere]:
    return db.session.scalars(select(Critere).where(Critere.actif.is_(True), cond_entite(Critere.entite_id, entite_id))
                              .order_by(Critere.ordre, Critere.id)).all()


def grille_commune(entite_id: int) -> list[CritereGrille]:
    spe = specifiques(entite_id)
    return [CritereGrille(c, c.poids) for c in _criteres_actifs(entite_id) if c.id not in spe]


def grille_du_poste(entite_id: int, poste: str | None) -> list[CritereGrille] | None:
    """Grille propre au poste (critères actifs), ou None si le poste n'en a pas."""
    if not poste:
        return None
    defn = grilles(entite_id).get(cle_texte(poste))
    if not defn or not defn["criteres"]:
        return None
    res = [CritereGrille(c, defn["criteres"][c.id]) for c in _criteres_actifs(entite_id) if c.id in defn["criteres"]]
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
        ent[k] = {"libelle": poste, "criteres": {str(i): int(p) for i, p in criteres.items()}}
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
