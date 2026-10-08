"""Opérations communes sur l'organisation : affectations, matricules, postes."""
from __future__ import annotations

import unicodedata

from sqlalchemy import func, select

from ..extensions import db
from ..models import Affectation, Departement, Employe, Poste, Projet


def cle_texte(s: str) -> str:
    """Normalise un libellé pour les rapprochements (casse, accents, espaces)."""
    s = unicodedata.normalize("NFKD", (s or "").strip().lower())
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).split())


def matricule_auto(utilises: set | None = None) -> str:
    utilises = utilises if utilises is not None else set()
    n = (db.session.scalar(select(func.count(Employe.id))) or 0) + 1
    while True:
        m = f"FUT-{n:04d}"
        if m not in utilises and not db.session.scalar(select(Employe.id).where(Employe.matricule == m)):
            utilises.add(m)
            return m
        n += 1


def postes_actifs() -> list[Poste]:
    return db.session.scalars(select(Poste).where(Poste.actif.is_(True)).order_by(Poste.libelle)).all()


def poste_canonique(libelle: str, creer: bool) -> str | None:
    """Renvoie le libellé du référentiel correspondant (créé si besoin et autorisé)."""
    libelle = (libelle or "").strip()
    if not libelle:
        return None
    k = cle_texte(libelle)
    for p in db.session.scalars(select(Poste)):
        if cle_texte(p.libelle) == k:
            if not p.actif and creer:
                p.actif = True
            return p.libelle
    if not creer:
        return libelle
    p = Poste(libelle=libelle[:120])
    db.session.add(p)
    db.session.flush()
    return p.libelle


def affecter(e: Employe, departement: Departement | None = None, projet: Projet | None = None,
             evaluateur_id: int | None = None) -> Affectation:
    """Crée (ou réactive) l'affectation. Sans N+1 précisé, le responsable de l'entité est retenu."""
    ctx = departement or projet
    col = Affectation.departement_id if departement else Affectation.projet_id
    if not evaluateur_id and ctx.responsable_id and ctx.responsable_id != e.id:
        evaluateur_id = ctx.responsable_id
    if evaluateur_id == e.id:
        evaluateur_id = None
    a = db.session.scalar(select(Affectation).where(Affectation.employe_id == e.id, col == ctx.id))
    if a:
        a.actif = True
        if evaluateur_id:
            a.evaluateur_id = evaluateur_id
    else:
        a = Affectation(employe_id=e.id, evaluateur_id=evaluateur_id,
                        departement_id=departement.id if departement else None,
                        projet_id=projet.id if projet else None)
        db.session.add(a)
    db.session.flush()
    return a


def completer_n1_par_responsable(ctx) -> int:
    """Donne le responsable comme N+1 aux affectations actives de l'entité qui n'en ont pas."""
    if not ctx.responsable_id:
        return 0
    col = Affectation.departement_id if isinstance(ctx, Departement) else Affectation.projet_id
    affs = db.session.scalars(select(Affectation).where(
        col == ctx.id, Affectation.actif.is_(True), Affectation.evaluateur_id.is_(None),
        Affectation.employe_id != ctx.responsable_id)).all()
    for a in affs:
        a.evaluateur_id = ctx.responsable_id
    return len(affs)
