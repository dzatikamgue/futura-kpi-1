"""Comptes d'accès liés automatiquement aux fiches salariés.

Règles (appliquées par synchroniser_comptes, appelée après chaque modification
du personnel, des affectations, des responsables ou après un import) :

1. L'identifiant de connexion d'un salarié est l'e-mail de sa fiche.
2. Un compte existant non lié dont l'e-mail correspond à une fiche est lié
   automatiquement à cette fiche (ex. le compte RH initial et la fiche du DRH).
3. Tout salarié actif qui a des N-1 à noter (N+1 d'une affectation, responsable
   d'un département ou d'un projet) et qui a un e-mail reçoit un compte
   « Collaborateur ». Son mot de passe est généré par la RH en un clic
   (Comptes & accès → « Générer les accès »), puis changé à la 1re connexion.
"""
from __future__ import annotations

import secrets
import string

from sqlalchemy import func, select

from ..extensions import db
from ..models import (Affectation, Departement, Employe, Projet, Role,
                      Utilisateur, utcnow)


def mot_de_passe_temporaire() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        mdp = "".join(secrets.choice(alphabet) for _ in range(12))
        if any(c.isdigit() for c in mdp) and any(c.isalpha() for c in mdp):
            return mdp


def ids_responsables() -> set[int]:
    """Salariés qui ont au moins une personne à noter."""
    ids = set(db.session.scalars(select(Affectation.evaluateur_id).where(
        Affectation.actif.is_(True), Affectation.evaluateur_id.isnot(None))))
    ids |= set(db.session.scalars(select(Departement.responsable_id).where(
        Departement.actif.is_(True), Departement.responsable_id.isnot(None))))
    ids |= set(db.session.scalars(select(Projet.responsable_id).where(
        Projet.actif.is_(True), Projet.responsable_id.isnot(None))))
    return ids


def lier_ou_creer(e: Employe, creer: bool) -> Utilisateur | None:
    """Lie le compte portant l'e-mail de la fiche, ou le crée si `creer`."""
    if e.compte:
        return e.compte
    if not e.email:
        return None
    email = e.email.strip().lower()
    u = db.session.scalar(select(Utilisateur).where(func.lower(Utilisateur.email) == email))
    if u:
        if u.employe_id is None:
            u.employe_id = e.id
            return u
        return None  # e-mail déjà utilisé par une autre fiche
    if not creer or not e.actif:
        return None
    u = Utilisateur(email=email, role=Role.COLLABORATEUR, employe_id=e.id, doit_changer_mdp=True,
                    actif=True, echecs_connexion=0)
    # Mot de passe aléatoire inutilisable tant que la RH n'a pas généré les accès
    u.set_password(secrets.token_urlsafe(32))
    db.session.add(u)
    return u


def synchroniser_comptes() -> dict:
    """Met en cohérence comptes et fiches. Renvoie {'lies': n, 'crees': n}."""
    stats = {"lies": 0, "crees": 0}
    responsables = ids_responsables()
    sans_compte = db.session.scalars(
        select(Employe).where(Employe.email.isnot(None), Employe.email != "",
                              ~Employe.id.in_(select(Utilisateur.employe_id)
                                              .where(Utilisateur.employe_id.isnot(None))))).all()
    for e in sans_compte:
        avant_existe = db.session.scalar(
            select(Utilisateur.id).where(func.lower(Utilisateur.email) == e.email.strip().lower()))
        u = lier_ou_creer(e, creer=e.id in responsables)
        if u:
            stats["lies" if avant_existe else "crees"] += 1
    db.session.flush()
    return stats


def aligner_email(e: Employe) -> str | None:
    """Après modification d'une fiche : l'identifiant suit l'e-mail. Renvoie un message d'alerte éventuel."""
    if not e.compte or not e.email:
        return None
    nouvel = e.email.strip().lower()
    if e.compte.email == nouvel:
        return None
    pris = db.session.scalar(select(Utilisateur.id).where(
        func.lower(Utilisateur.email) == nouvel, Utilisateur.id != e.compte.id))
    if pris:
        return f"L'e-mail {nouvel} est déjà utilisé par un autre compte : l'identifiant de connexion n'a pas changé."
    e.compte.email = nouvel
    return None


def generer_acces(comptes: list[Utilisateur]) -> list[tuple[Utilisateur, str]]:
    """Génère un mot de passe temporaire pour chaque compte (affiché une seule fois)."""
    resultat = []
    for u in comptes:
        mdp = mot_de_passe_temporaire()
        u.set_password(mdp)
        u.doit_changer_mdp = True
        u.echecs_connexion, u.bloque_jusqua = 0, None
        u.acces_remis_le = utcnow()
        resultat.append((u, mdp))
    return resultat
