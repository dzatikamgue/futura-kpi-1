"""Opérations communes sur l'organisation : affectations, matricules, postes."""
from __future__ import annotations

import unicodedata

from sqlalchemy import func, select

from ..extensions import db
from ..models import Affectation, Departement, Employe, Entite, Poste, Projet
from .entites import cond_entite, id_effectif


def cle_texte(s: str) -> str:
    """Normalise un libellé pour les rapprochements (casse, accents, espaces)."""
    s = unicodedata.normalize("NFKD", (s or "").strip().lower())
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).split())


def matricule_auto(entite: Entite, utilises: set | None = None) -> str:
    """Matricule libre dans l'entité : <CODE>-0001…"""
    utilises = utilises if utilises is not None else set()
    n = (db.session.scalar(select(func.count(Employe.id)).where(cond_entite(Employe.entite_id, entite.id))) or 0) + 1
    while True:
        m = f"{entite.code}-{n:04d}"
        if m not in utilises and not matricule_pris(m, entite.id):
            utilises.add(m)
            return m
        n += 1


def matricule_pris(matricule: str, entite_id: int, sauf_id: int | None = None) -> bool:
    q = select(Employe.id).where(Employe.matricule == matricule, cond_entite(Employe.entite_id, entite_id))
    if sauf_id:
        q = q.where(Employe.id != sauf_id)
    return db.session.scalar(q) is not None


def evaluateurs_groupe(exclure_id: int | None = None):
    """N+1 possibles : tout le personnel actif du groupe, regroupé par entité (N+1 croisé autorisé)."""
    entites = db.session.scalars(select(Entite).where(Entite.actif.is_(True))
                                 .order_by(Entite.principale.desc(), Entite.nom)).all()
    q = select(Employe).where(Employe.actif.is_(True)).order_by(Employe.nom, Employe.prenom)
    if exclure_id:
        q = q.where(Employe.id != exclure_id)
    par_entite = {e.id: [] for e in entites}
    for emp in db.session.scalars(q):
        par_entite.setdefault(id_effectif(emp.entite_id), []).append(emp)
    return [(e, par_entite[e.id]) for e in entites if par_entite.get(e.id)]


# Proposés dans la liste sans jamais être écrits en base (aucune donnée modifiée au démarrage)
POSTES_DEFAUT = [
    "Directeur général", "Directeur technique", "Directeur administratif et financier",
    "Responsable ressources humaines", "Comptable", "Assistant(e) administratif(ve)",
    "Chef de projet", "Conducteur de travaux", "Chef de chantier", "Ingénieur structure",
    "Ingénieur génie civil", "Ingénieur électricité", "Technicien", "Topographe", "Métreur",
    "Dessinateur projeteur", "Responsable HSE", "Responsable achats", "Magasinier",
    "Chauffeur", "Chef d'équipe", "Ouvrier qualifié", "Manœuvre",
]


def postes_actifs(entite_id: int) -> list[str]:
    """Libellés proposés : référentiel + postes déjà présents sur les fiches + liste par défaut.

    Lecture seule. Un poste archivé dans l'écran Postes n'est plus proposé.
    """
    archives, libelles = set(), {}
    for p in db.session.scalars(select(Poste).where(cond_entite(Poste.entite_id, entite_id))):
        if p.actif:
            libelles.setdefault(cle_texte(p.libelle), p.libelle)
        else:
            archives.add(cle_texte(p.libelle))
    existants = db.session.scalars(select(Employe.poste).where(
        Employe.poste.isnot(None), cond_entite(Employe.entite_id, entite_id)).distinct())
    for lib in list(existants) + POSTES_DEFAUT:
        k = cle_texte(lib)
        if k and k not in archives:
            libelles.setdefault(k, lib)
    return sorted(libelles.values(), key=lambda x: cle_texte(x))


def poste_canonique(libelle: str, creer: bool, entite_id: int) -> str | None:
    """Renvoie le libellé de la liste correspondant (même s'il diffère par la casse / les accents).

    Un poste réellement nouveau saisi par la RH est ajouté au référentiel si `creer`.
    """
    libelle = " ".join((libelle or "").split())[:120]
    if not libelle:
        return None
    k = cle_texte(libelle)
    for lib in postes_actifs(entite_id):
        if cle_texte(lib) == k:
            return lib
    if creer and not db.session.scalar(select(Poste.id).where(
            func.lower(Poste.libelle) == libelle.lower(), cond_entite(Poste.entite_id, entite_id))):
        db.session.add(Poste(libelle=libelle, entite_id=entite_id))
        db.session.flush()
    return libelle


def affecter(e: Employe, departement: Departement | None = None, projet: Projet | None = None,
             evaluateur_id: int | None = None) -> Affectation:
    """Crée (ou réactive) l'affectation. Le N+1 est celui désigné par la RH, jamais déduit."""
    ctx = departement or projet
    col = Affectation.departement_id if departement else Affectation.projet_id
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
