"""Personnel « groupe » : direction et ressources humaines visibles dans toutes les entités.

Aucune structure ajoutée : un salarié est « groupe » si
  - son poste est un poste de direction / RH (détection par mots-clés, ex. « Directeur
    général », « Responsable ressources humaines », « DRH »), sauf si la RH a retiré ce
    poste de la liste ;
  - ou son poste a été ajouté à la liste par la RH (écran Postes) ;
  - ou son compte a le rôle RH du groupe.
Les ajouts / retraits sont mémorisés dans la table de réglages existante (Parametre).
"""
from __future__ import annotations

import json
import re

from flask import g
from sqlalchemy import select

from ..extensions import db
from ..models import Employe, Parametre, Role, Utilisateur

CLE = "postes_groupe"
MOTIF = re.compile(r"\b(directeur|directrice|dg|dga|pdg|gerant|gerante|drh|rrh|rh|ressources humaines)\b")


def _cle(libelle: str) -> str:
    from .organisation import cle_texte
    return cle_texte(libelle)


def _reglage() -> dict:
    p = db.session.get(Parametre, CLE)
    try:
        d = json.loads(p.valeur) if p else {}
    except ValueError:
        d = {}
    return {"ajoutes": set(d.get("ajoutes", [])), "retires": set(d.get("retires", []))}


def poste_groupe_auto(libelle: str | None) -> bool:
    return bool(libelle) and bool(MOTIF.search(_cle(libelle)))


def poste_est_groupe(libelle: str | None, reglage: dict | None = None) -> bool:
    if not libelle:
        return False
    r = reglage or _reglage()
    k = _cle(libelle)
    if k in r["ajoutes"]:
        return True
    return poste_groupe_auto(libelle) and k not in r["retires"]


def basculer_poste(libelle: str, utilisateur_id: int | None) -> bool:
    """Inverse le statut « groupe » d'un poste. Renvoie le nouveau statut."""
    r = _reglage()
    k = _cle(libelle)
    nouveau = not poste_est_groupe(libelle, r)
    r["ajoutes"].discard(k)
    r["retires"].discard(k)
    if nouveau and not poste_groupe_auto(libelle):
        r["ajoutes"].add(k)
    if not nouveau and poste_groupe_auto(libelle):
        r["retires"].add(k)
    valeur = json.dumps({"ajoutes": sorted(r["ajoutes"]), "retires": sorted(r["retires"])})
    p = db.session.get(Parametre, CLE)
    if p:
        p.valeur, p.updated_by_id = valeur, utilisateur_id
    else:
        db.session.add(Parametre(cle=CLE, valeur=valeur, updated_by_id=utilisateur_id))
    g.pop("_ids_groupe", None)
    return nouveau


def ids_personnel_groupe() -> set[int]:
    """Salariés actifs visibles dans toutes les entités (calculé une fois par requête)."""
    if getattr(g, "_ids_groupe", None) is not None:
        return g._ids_groupe
    r = _reglage()
    ids = {i for i, poste in db.session.execute(
        select(Employe.id, Employe.poste).where(Employe.actif.is_(True), Employe.poste.isnot(None)))
        if poste_est_groupe(poste, r)}
    ids |= set(db.session.scalars(
        select(Utilisateur.employe_id).join(Employe, Employe.id == Utilisateur.employe_id)
        .where(Utilisateur.role == Role.RH, Utilisateur.actif.is_(True), Employe.actif.is_(True))))
    g._ids_groupe = ids
    return ids


# ---------------------------------------------------------------------------
# Portée d'une personne « groupe » : tout le groupe ou certaines entités, et
# « main mise » (droits Direction dans ces entités) ou simple visibilité.
# Table de réglages existante, clé « portee_groupe » :
#   {employe_id: {"entites": "tous" | [entite_id, ...], "main": bool}}
# Sans réglage : tout le groupe, visible seulement (comportement d'origine).
# ---------------------------------------------------------------------------
CLE_PORTEE = "portee_groupe"


def _portees() -> dict:
    if getattr(g, "_portees_groupe", None) is None:
        p = db.session.get(Parametre, CLE_PORTEE)
        try:
            g._portees_groupe = json.loads(p.valeur) if p else {}
        except ValueError:
            g._portees_groupe = {}
    return g._portees_groupe


def portee(employe_id: int) -> dict:
    """{"entites": None (tout le groupe) | set(ids), "main": bool}."""
    brut = _portees().get(str(employe_id)) or {}
    ents = brut.get("entites", "tous")
    return {"entites": None if ents == "tous" else {int(i) for i in ents}, "main": bool(brut.get("main"))}


def definir_portee(employe_id: int, entites: set[int] | None, main: bool, utilisateur_id: int | None) -> None:
    carte = dict(_portees())
    if entites is None and not main:
        carte.pop(str(employe_id), None)              # valeur par défaut : rien à mémoriser
    else:
        carte[str(employe_id)] = {"entites": "tous" if entites is None else sorted(entites), "main": main}
    valeur = json.dumps(carte)
    p = db.session.get(Parametre, CLE_PORTEE)
    if p:
        p.valeur, p.updated_by_id = valeur, utilisateur_id
    else:
        db.session.add(Parametre(cle=CLE_PORTEE, valeur=valeur, updated_by_id=utilisateur_id))
    g._portees_groupe = carte


def couvre(employe_id: int, entite_id: int) -> bool:
    ents = portee(employe_id)["entites"]
    return ents is None or entite_id in ents


def ids_groupe_dans(entite_id: int) -> set[int]:
    """Personnel groupe dont la portée couvre cette entité."""
    return {i for i in ids_personnel_groupe() if couvre(i, entite_id)}


def entites_main_mise(employe_id: int, actives) -> set[int]:
    """Entités où la personne groupe a la main (droits Direction)."""
    if employe_id not in ids_personnel_groupe():
        return set()
    p = portee(employe_id)
    if not p["main"]:
        return set()
    return set(actives) if p["entites"] is None else p["entites"] & set(actives)
