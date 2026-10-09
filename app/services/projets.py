"""Rattachement des projets à un département, et règle « une seule note par salarié ».

Aucune structure ajoutée : le lien projet → département est mémorisé dans la table
de réglages existante (Parametre, clé « projets_departements », JSON {projet_id: departement_id}).

Règle de notation : un salarié affecté à un projet est noté dans son projet ;
ses affectations « département » ne sont alors pas notées (elles restent visibles
sur sa fiche, l'historique est conservé). Sans projet, il est noté dans son département.
"""
from __future__ import annotations

import json

from flask import g
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import aliased

from ..extensions import db
from ..models import Affectation, Departement, Parametre, Projet

CLE = "projets_departements"


def carte() -> dict[int, int]:
    """{projet_id: departement_id} (calculé une fois par requête)."""
    if getattr(g, "_carte_projets", None) is not None:
        return g._carte_projets
    p = db.session.get(Parametre, CLE)
    try:
        brut = json.loads(p.valeur) if p else {}
    except ValueError:
        brut = {}
    g._carte_projets = {int(k): int(v) for k, v in brut.items() if str(k).isdigit() and str(v).isdigit()}
    return g._carte_projets


def departement_du_projet(projet_id: int | None) -> int | None:
    return carte().get(projet_id) if projet_id else None


def projets_du_departement(departement_id: int) -> set[int]:
    return {p for p, d in carte().items() if d == departement_id}


def definir(projet_id: int, departement_id: int | None, utilisateur_id: int | None) -> None:
    c = dict(carte())
    if departement_id:
        c[projet_id] = departement_id
    else:
        c.pop(projet_id, None)
    valeur = json.dumps({str(k): v for k, v in sorted(c.items())})
    p = db.session.get(Parametre, CLE)
    if p:
        p.valeur, p.updated_by_id = valeur, utilisateur_id
    else:
        db.session.add(Parametre(cle=CLE, valeur=valeur, updated_by_id=utilisateur_id))
    g._carte_projets = c


def cond_affectation_notee():
    """SQL : l'affectation donne lieu à une note.

    Projet : toujours. Département : seulement si le salarié n'a pas de projet actif
    dans la MÊME entité (un salarié multi-entités est noté une fois par entité).
    """
    from ..services.entites import principale
    pid = principale().id
    autre, proj, dep = aliased(Affectation), aliased(Projet), aliased(Departement)
    entite_du_dep = (select(func.coalesce(dep.entite_id, pid))
                     .where(dep.id == Affectation.departement_id).scalar_subquery())
    a_un_projet = exists().where(and_(autre.employe_id == Affectation.employe_id,
                                      autre.actif.is_(True), autre.projet_id == proj.id,
                                      func.coalesce(proj.entite_id, pid) == entite_du_dep))
    return or_(Affectation.projet_id.isnot(None), ~a_un_projet)


def affectation_notee(a: Affectation) -> bool:
    if a.projet_id:
        return True
    from ..services.entites import entite_affectation
    eid = entite_affectation(a)
    return not any(x.projet_id and x.actif and entite_affectation(x) == eid for x in a.employe.affectations)
