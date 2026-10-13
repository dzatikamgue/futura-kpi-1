"""Sous-départements : un département peut être rattaché à un département parent.

Aucune structure ajoutée : le lien est mémorisé dans la table de réglages existante
(Parametre, clé « departements_parents », JSON {departement_id: parent_id}).

Effets : le chef d'un département voit aussi ses sous-départements (et leurs projets) ;
les filtres et moyennes d'un département incluent ses sous-départements.
"""
from __future__ import annotations

import json

from flask import g

from ..extensions import db
from ..models import Parametre

CLE = "departements_parents"


def parents() -> dict[int, int]:
    """{departement_id: parent_id} (calculé une fois par requête)."""
    if getattr(g, "_parents_dep", None) is not None:
        return g._parents_dep
    p = db.session.get(Parametre, CLE)
    try:
        brut = json.loads(p.valeur) if p else {}
    except ValueError:
        brut = {}
    g._parents_dep = {int(k): int(v) for k, v in brut.items() if str(k).isdigit() and str(v).isdigit()}
    return g._parents_dep


def parent_de(departement_id: int | None) -> int | None:
    return parents().get(departement_id) if departement_id else None


def sous_departements(departement_id: int) -> set[int]:
    """Tous les descendants (enfants, petits-enfants…), sans le département lui-même."""
    enfants: dict[int, list[int]] = {}
    for d, p in parents().items():
        enfants.setdefault(p, []).append(d)
    res, pile = set(), list(enfants.get(departement_id, []))
    while pile:
        d = pile.pop()
        if d not in res:
            res.add(d)
            pile.extend(enfants.get(d, []))
    return res


def branche(departement_id: int) -> set[int]:
    """Le département et tous ses sous-départements."""
    return {departement_id} | sous_departements(departement_id)


def ancetres(departement_id: int) -> list[int]:
    res, d, vus = [], parent_de(departement_id), set()
    while d and d not in vus:
        vus.add(d)
        res.append(d)
        d = parent_de(d)
    return res


def definir_parent(departement_id: int, parent_id: int | None, utilisateur_id: int | None) -> None:
    """Rattache (ou détache) un département. Refuse un cycle (parent = lui-même ou un descendant)."""
    if parent_id and (parent_id == departement_id or parent_id in sous_departements(departement_id)):
        raise ValueError("Un département ne peut pas être rattaché à lui-même ni à l'un de ses sous-départements.")
    c = dict(parents())
    if parent_id:
        c[departement_id] = parent_id
    else:
        c.pop(departement_id, None)
    valeur = json.dumps({str(k): v for k, v in sorted(c.items())})
    p = db.session.get(Parametre, CLE)
    if p:
        p.valeur, p.updated_by_id = valeur, utilisateur_id
    else:
        db.session.add(Parametre(cle=CLE, valeur=valeur, updated_by_id=utilisateur_id))
    g._parents_dep = c


def ordonner(departements) -> list[tuple[object, int]]:
    """Liste en arbre : [(département, profondeur)], parents suivis de leurs sous-départements."""
    par_id = {d.id: d for d in departements}
    enfants: dict[int | None, list] = {}
    for d in departements:
        p = parent_de(d.id)
        enfants.setdefault(p if p in par_id else None, []).append(d)
    res, vus = [], set()

    def visiter(d, niveau):
        if d.id in vus:
            return
        vus.add(d.id)
        res.append((d, niveau))
        for e in enfants.get(d.id, []):
            visiter(e, niveau + 1)

    for d in enfants.get(None, []):
        visiter(d, 0)
    for d in departements:          # sécurité : rien ne disparaît
        visiter(d, 0)
    return res
