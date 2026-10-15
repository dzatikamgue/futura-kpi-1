"""Salarié travaillant pour plusieurs entités : poste propre à chaque entité.

Aucune structure ajoutée : la fiche garde le poste de l'entité d'origine (Employe.poste) ;
le poste occupé dans une autre entité est mémorisé dans la table de réglages existante
(Parametre, clé « postes_par_entite », JSON {employe_id: {entite_id: poste}}).
"""
from __future__ import annotations

import json

from flask import g

from ..extensions import db
from ..models import Parametre
from .entites import id_effectif

CLE = "postes_par_entite"


def _carte() -> dict:
    if getattr(g, "_postes_entites", None) is None:
        p = db.session.get(Parametre, CLE)
        try:
            g._postes_entites = json.loads(p.valeur) if p else {}
        except ValueError:
            g._postes_entites = {}
    return g._postes_entites


def poste_dans_entite(employe, entite_id: int | None) -> str | None:
    """Poste du salarié dans l'entité donnée (celui de sa fiche dans son entité d'origine)."""
    if employe is None:
        return None
    if entite_id is None or id_effectif(employe.entite_id) == entite_id:
        return employe.poste
    return _carte().get(str(employe.id), {}).get(str(entite_id)) or employe.poste


def poste_propre(employe_id: int, entite_id: int) -> str | None:
    """Poste explicitement défini pour cette entité (None si aucun)."""
    return _carte().get(str(employe_id), {}).get(str(entite_id))


def definir_poste(employe_id: int, entite_id: int, poste: str | None, utilisateur_id: int | None) -> None:
    carte = {k: dict(v) for k, v in _carte().items()}
    postes = carte.setdefault(str(employe_id), {})
    if poste:
        postes[str(entite_id)] = poste
    else:
        postes.pop(str(entite_id), None)
    if not postes:
        carte.pop(str(employe_id), None)
    valeur = json.dumps(carte, ensure_ascii=False)
    p = db.session.get(Parametre, CLE)
    if p:
        p.valeur, p.updated_by_id = valeur, utilisateur_id
    else:
        db.session.add(Parametre(cle=CLE, valeur=valeur, updated_by_id=utilisateur_id))
    g._postes_entites = carte


def impacts_retrait(employe, entite_id: int) -> dict:
    """Ce que retire le détachement du salarié d'une entité (autre que son entité d'origine)."""
    from sqlalchemy import select
    from ..models import Affectation, Departement, Projet
    from .entites import cond_entite, entite_affectation
    affs = [a for a in employe.affectations if a.actif and entite_affectation(a) == entite_id]
    deps = db.session.scalars(select(Departement).where(
        Departement.responsable_id == employe.id, cond_entite(Departement.entite_id, entite_id))).all()
    projs = db.session.scalars(select(Projet).where(
        Projet.responsable_id == employe.id, cond_entite(Projet.entite_id, entite_id))).all()
    from .organisation import n1_designes
    designes = n1_designes()
    # N+1 choisis par la RH dans l'entité : projets, et départements où il a été désigné
    notes = [a for a in db.session.scalars(select(Affectation).where(
        Affectation.actif.is_(True), Affectation.evaluateur_id == employe.id))
        if (a.projet_id or a.id in designes) and entite_affectation(a) == entite_id]
    return {"affectations": affs, "departements": deps, "projets": projs, "n1_projets": notes}


def retirer_de_entite(employe, entite_id: int, utilisateur_id: int | None) -> dict:
    """Détache le salarié d'une autre entité.

    Ses affectations dans l'entité sont clôturées (rien n'est supprimé : l'historique des notes
    reste consultable), son poste propre à l'entité est oublié, et il perd ses rôles dans
    l'entité (chef de département / de projet, N+1 désigné sur un projet). Les N+1 des
    départements sont ensuite recalculés (le chef du département parent prend le relais).
    """
    from .organisation import designer_n1, synchroniser_n1_departements
    imp = impacts_retrait(employe, entite_id)
    for a in imp["affectations"]:
        a.actif = False
    for x in imp["departements"] + imp["projets"]:
        x.responsable_id = None
    db.session.flush()
    for a in imp["n1_projets"]:
        if a.departement_id:
            designer_n1(a, None, utilisateur_id)      # retour au responsable du département
        else:
            a.evaluateur_id = None
    if poste_propre(employe.id, entite_id):
        definir_poste(employe.id, entite_id, None, utilisateur_id)
    db.session.flush()
    synchroniser_n1_departements()
    return imp


def entites_du_salarie(employe) -> list[int]:
    """Entités où le salarié travaille : son entité d'origine, celles de ses affectations actives
    et celles où il est chef d'un département ou d'un projet actif."""
    from sqlalchemy import select
    from ..models import Departement, Projet
    from .entites import entite_affectation
    ids = [id_effectif(employe.entite_id)]
    autres = [entite_affectation(a) for a in employe.affectations if a.actif]
    for modele in (Departement, Projet):
        autres += [id_effectif(x) for x in db.session.scalars(select(modele.entite_id).where(
            modele.responsable_id == employe.id, modele.actif.is_(True)))]
    for eid in autres:
        if eid not in ids:
            ids.append(eid)
    return ids
