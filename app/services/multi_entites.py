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


def entites_du_salarie(employe) -> list[int]:
    """Entités où le salarié travaille : son entité d'origine + celles de ses affectations actives."""
    from .entites import entite_affectation
    ids = [id_effectif(employe.entite_id)]
    for a in employe.affectations:
        if a.actif:
            eid = entite_affectation(a)
            if eid not in ids:
                ids.append(eid)
    return ids
