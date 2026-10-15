"""Sous-départements : un département peut être rattaché à un département parent.

Aucune structure ajoutée : le lien est mémorisé dans la table de réglages existante
(Parametre, clé « departements_parents », JSON {departement_id: parent_id}).

Effets : le chef d'un département voit aussi ses sous-départements (et leurs projets) ;
les filtres et moyennes d'un département incluent ses sous-départements.
"""
from __future__ import annotations

import json

from flask import g

from sqlalchemy import func, select

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


def ordonner(departements, cle=None) -> list[tuple[object, int]]:
    """Liste en arbre : [(département, profondeur)], parents suivis de leurs sous-départements.

    cle : fonction de tri des départements frères (ex. effectif décroissant).
    """
    par_id = {d.id: d for d in departements}
    enfants: dict[int | None, list] = {}
    for d in departements:
        p = parent_de(d.id)
        enfants.setdefault(p if p in par_id else None, []).append(d)
    if cle is not None:
        for liste in enfants.values():
            liste.sort(key=cle)
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


# --- Suppression de départements ------------------------------------------------
CLE_SUPPRIMES = "departements_supprimes"


def supprimes() -> set[int]:
    """Départements supprimés par la RH mais gardés en base car ils ont un historique (affectations / notes)."""
    p = db.session.get(Parametre, CLE_SUPPRIMES)
    try:
        return {int(x) for x in (json.loads(p.valeur) if p else [])}
    except (ValueError, TypeError):
        return set()


def _ecrire_supprimes(ids: set[int], utilisateur_id: int | None) -> None:
    valeur = json.dumps(sorted(ids))
    p = db.session.get(Parametre, CLE_SUPPRIMES)
    if p:
        p.valeur, p.updated_by_id = valeur, utilisateur_id
    else:
        db.session.add(Parametre(cle=CLE_SUPPRIMES, valeur=valeur, updated_by_id=utilisateur_id))


def restaurer(departement, utilisateur_id: int | None) -> None:
    """Département recréé sous le même code / nom : on reprend la ligne existante."""
    _ecrire_supprimes(supprimes() - {departement.id}, utilisateur_id)
    departement.actif = True


def impacts_suppression(departements) -> dict:
    from ..models import Affectation
    from .projets import carte
    ids = {d.id for d in departements}
    membres = db.session.scalar(select(func.count(Affectation.id)).where(
        Affectation.departement_id.in_(ids or {-1}), Affectation.actif.is_(True))) or 0
    enfants = [d for d, p in parents().items() if p in ids and d not in ids]
    projets = [p for p, d in carte().items() if d in ids]
    return {"membres": membres, "sous_departements": len(enfants), "projets": len(projets)}


def supprimer_departements(departements, utilisateur_id: int | None) -> tuple[int, int]:
    """Supprime des départements. Renvoie (effacés, gardés pour l'historique).

    - Les sous-départements remontent d'un niveau (rattachés au parent du département supprimé).
    - Les projets rattachés deviennent « à rattacher ».
    - Les affectations actives sont clôturées.
    - Un département qui a déjà eu des affectations n'est pas effacé (les notes passées en
      dépendent) : il est archivé et retiré de toutes les listes.
    """
    from ..models import Affectation
    from .projets import carte, definir
    ids = {d.id for d in departements}
    for d in departements:
        par = parent_de(d.id)
        while par in ids:                       # remonter au premier ancêtre conservé
            par = parent_de(par)
        for enfant, p in list(parents().items()):
            if p == d.id and enfant not in ids:
                definir_parent(enfant, par, utilisateur_id)
        definir_parent(d.id, None, utilisateur_id)
    for p, dep in list(carte().items()):
        if dep in ids:
            definir(p, None, utilisateur_id)
    effaces, gardes = 0, 0
    caches = supprimes()
    for d in departements:
        a_un_historique = db.session.scalar(select(Affectation.id).where(Affectation.departement_id == d.id).limit(1))
        if a_un_historique:
            for a in db.session.scalars(select(Affectation).where(Affectation.departement_id == d.id,
                                                                  Affectation.actif.is_(True))):
                a.actif = False
            d.actif = False
            caches.add(d.id)
            gardes += 1
        else:
            db.session.delete(d)
            effaces += 1
    _ecrire_supprimes(caches, utilisateur_id)
    return effaces, gardes



def effectifs_branche(departement_ids) -> dict[int, dict[str, int]]:
    """Effectifs par département : {"direct": membres du département, "total": membres distincts
    du département, de tous ses sous-départements et des projets qui leur sont rattachés}."""
    from ..models import Affectation
    from .projets import carte
    ids = set(departement_ids)
    membres_dep: dict[int, set[int]] = {}
    for dep, emp in db.session.execute(select(Affectation.departement_id, Affectation.employe_id).where(
            Affectation.actif.is_(True), Affectation.departement_id.in_(ids or {-1}))):
        membres_dep.setdefault(dep, set()).add(emp)
    projets_par_dep: dict[int, set[int]] = {}
    for p, d in carte().items():
        projets_par_dep.setdefault(d, set()).add(p)
    tous_projets = set().union(*projets_par_dep.values()) if projets_par_dep else set()
    membres_proj: dict[int, set[int]] = {}
    for proj, emp in db.session.execute(select(Affectation.projet_id, Affectation.employe_id).where(
            Affectation.actif.is_(True), Affectation.projet_id.in_(tous_projets or {-1}))):
        membres_proj.setdefault(proj, set()).add(emp)
    res = {}
    for d in ids:
        branche_d = branche(d) & ids | {d}
        tous: set[int] = set()
        for x in branche_d:
            tous |= membres_dep.get(x, set())
            for p in projets_par_dep.get(x, ()):
                tous |= membres_proj.get(p, set())
        res[d] = {"direct": len(membres_dep.get(d, set())), "total": len(tous)}
    return res
