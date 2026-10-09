"""Comptes d'accès liés automatiquement aux fiches salariés.

Règles — appliquées UNIQUEMENT aux salariés concernés par une action de la RH
(enregistrement d'une fiche, désignation d'un N+1, import). Rien ne s'exécute au
démarrage ni sur les données existantes sans action de la RH.

1. L'identifiant de connexion d'un salarié est l'e-mail de sa fiche.
2. Un compte existant non lié dont l'e-mail correspond à la fiche enregistrée est
   lié à cette fiche (ex. le compte RH initial et la fiche du DRH).
3. Un salarié désigné N+1 par la RH, qui a un e-mail, reçoit un compte
   « Collaborateur » marqué « accès à remettre ». Son mot de passe est généré par
   la RH (Comptes & accès → « Générer les accès »), puis changé à la 1re connexion.
"""
from __future__ import annotations

import secrets
import string

from sqlalchemy import func, select

from ..extensions import db
from ..models import Affectation, Employe, Role, Utilisateur, utcnow


def mot_de_passe_temporaire() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        mdp = "".join(secrets.choice(alphabet) for _ in range(12))
        if any(c.isdigit() for c in mdp) and any(c.isalpha() for c in mdp):
            return mdp


def ids_evaluateurs() -> set[int]:
    """Salariés désignés N+1 sur au moins une affectation active."""
    return set(db.session.scalars(select(Affectation.evaluateur_id).where(
        Affectation.actif.is_(True), Affectation.evaluateur_id.isnot(None))))


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
                    actif=True, echecs_connexion=0, acces_en_attente=True)
    # Mot de passe aléatoire inutilisable tant que la RH n'a pas généré les accès
    u.set_password(secrets.token_urlsafe(32))
    db.session.add(u)
    return u


def synchroniser_comptes(employe_ids=None) -> dict:
    """Lie / crée les comptes des salariés indiqués (tous si None : bouton « Resynchroniser »).

    Renvoie {'lies': n, 'crees': n}.
    """
    stats = {"lies": 0, "crees": 0}
    q = select(Employe).where(Employe.email.isnot(None), Employe.email != "",
                              ~Employe.id.in_(select(Utilisateur.employe_id)
                                              .where(Utilisateur.employe_id.isnot(None))))
    if employe_ids is not None:
        ids = {i for i in employe_ids if i}
        if not ids:
            return stats
        q = q.where(Employe.id.in_(ids))
    evaluateurs = ids_evaluateurs()
    for e in db.session.scalars(q).all():
        avant_existe = db.session.scalar(
            select(Utilisateur.id).where(func.lower(Utilisateur.email) == e.email.strip().lower()))
        u = lier_ou_creer(e, creer=e.id in evaluateurs)
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


def generer_acces(comptes: list[Utilisateur], imposes: dict | None = None) -> list[tuple[Utilisateur, str]]:
    """Attribue un mot de passe à chaque compte (choisi par le superadmin ou généré), affiché une seule fois."""
    resultat = []
    imposes = imposes or {}
    for u in comptes:
        mdp = imposes.get(u.id) or mot_de_passe_temporaire()
        u.set_password(mdp)
        u.doit_changer_mdp = False  # attribué par le superadmin, non modifiable par l'utilisateur
        u.echecs_connexion, u.bloque_jusqua = 0, None
        u.acces_remis_le = utcnow()
        u.acces_en_attente = False
        resultat.append((u, mdp))
    return resultat
