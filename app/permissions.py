"""Règles d'accès — le cœur de la confidentialité de l'application.

Règle métier :
- RH et Direction voient toutes les évaluations.
- Un collaborateur voit :
    * les évaluations des départements / projets dont il est responsable,
    * les évaluations des affectations dont il est le N+1 (évaluateur),
    * ses propres évaluations, uniquement une fois soumises.
- Un collaborateur note :
    * les affectations dont il est le N+1,
    * les affectations d'un département / projet dont il est responsable,
  jamais lui-même.

Toutes les listes passent par ces fonctions : aucune route ne filtre « à la main ».
"""
from __future__ import annotations

from functools import wraps

from flask import abort
from flask_login import current_user
from sqlalchemy import and_, false, or_, select

from .extensions import db
from .models import (Affectation, Departement, Employe, Evaluation, Projet,
                     StatutEvaluation)


def rh_requis(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.est_rh:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def direction_ou_rh_requis(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.voit_tout:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


class Perimetre:
    """Périmètre calculé une fois par requête pour un utilisateur."""

    def __init__(self, user):
        self.user = user
        self.voit_tout = user.voit_tout
        self.employe_id = user.employe_id
        if self.employe_id:
            self.dept_ids = set(db.session.scalars(
                select(Departement.id).where(Departement.responsable_id == self.employe_id)))
            self.projet_ids = set(db.session.scalars(
                select(Projet.id).where(Projet.responsable_id == self.employe_id)))
        else:
            self.dept_ids, self.projet_ids = set(), set()

    # -- Affectations que l'utilisateur peut NOTER -------------------------
    def filtre_affectations_a_noter(self):
        """Expression SQL sur Affectation : ce que l'utilisateur doit / peut noter."""
        if not self.employe_id:
            return false()
        conds = [Affectation.evaluateur_id == self.employe_id]
        if self.dept_ids:
            conds.append(Affectation.departement_id.in_(self.dept_ids))
        if self.projet_ids:
            conds.append(Affectation.projet_id.in_(self.projet_ids))
        return and_(Affectation.actif.is_(True),
                    Affectation.employe_id != self.employe_id,
                    or_(*conds))

    def peut_noter(self, affectation: Affectation) -> bool:
        if not self.employe_id or not affectation.actif:
            return False
        if affectation.employe_id == self.employe_id:
            return False
        return (affectation.evaluateur_id == self.employe_id
                or (affectation.departement_id in self.dept_ids)
                or (affectation.projet_id in self.projet_ids))

    # -- Affectations visibles (suivi, campagne) ---------------------------
    def filtre_affectations_visibles(self):
        if self.voit_tout:
            return Affectation.id.isnot(None)
        return self.filtre_affectations_a_noter()

    # -- Évaluations visibles ----------------------------------------------
    def filtre_evaluations(self):
        """Expression SQL sur Evaluation (jointure Affectation non requise)."""
        if self.voit_tout:
            return Evaluation.id.isnot(None)
        if not self.employe_id:
            return false()
        conds = [
            Evaluation.evaluateur_id == self.employe_id,
            Evaluation.affectation_id.in_(
                select(Affectation.id).where(Affectation.evaluateur_id == self.employe_id)),
            and_(Evaluation.employe_id == self.employe_id,
                 Evaluation.statut == StatutEvaluation.SOUMISE),
        ]
        if self.dept_ids:
            conds.append(Evaluation.departement_id.in_(self.dept_ids))
        if self.projet_ids:
            conds.append(Evaluation.projet_id.in_(self.projet_ids))
        return or_(*conds)

    def peut_voir_evaluation(self, ev: Evaluation) -> bool:
        if self.voit_tout:
            return True
        if not self.employe_id:
            return False
        if ev.employe_id == self.employe_id:
            return ev.est_soumise
        return (ev.evaluateur_id == self.employe_id
                or (ev.affectation and ev.affectation.evaluateur_id == self.employe_id)
                or (ev.departement_id is not None and ev.departement_id in self.dept_ids)
                or (ev.projet_id is not None and ev.projet_id in self.projet_ids))

    # -- Salariés visibles ---------------------------------------------------
    def filtre_employes(self):
        if self.voit_tout:
            return Employe.id.isnot(None)
        ids_affectations = select(Affectation.employe_id).where(self.filtre_affectations_a_noter())
        conds = [Employe.id.in_(ids_affectations)]
        if self.employe_id:
            conds.append(Employe.id == self.employe_id)
        return or_(*conds)

    def peut_voir_employe(self, employe: Employe) -> bool:
        if self.voit_tout or employe.id == self.employe_id:
            return True
        return any(self.peut_noter(a) for a in employe.affectations)

    # -- Listes de filtres proposées dans l'interface ------------------------
    def departements_visibles(self):
        q = select(Departement).where(Departement.actif.is_(True)).order_by(Departement.nom)
        if not self.voit_tout:
            ids = set(self.dept_ids) | set(db.session.scalars(
                select(Affectation.departement_id).where(
                    Affectation.evaluateur_id == self.employe_id,
                    Affectation.departement_id.isnot(None)))) if self.employe_id else set()
            q = q.where(Departement.id.in_(ids or {-1}))
        return db.session.scalars(q).all()

    def projets_visibles(self):
        q = select(Projet).where(Projet.actif.is_(True)).order_by(Projet.nom)
        if not self.voit_tout:
            ids = set(self.projet_ids) | set(db.session.scalars(
                select(Affectation.projet_id).where(
                    Affectation.evaluateur_id == self.employe_id,
                    Affectation.projet_id.isnot(None)))) if self.employe_id else set()
            q = q.where(Projet.id.in_(ids or {-1}))
        return db.session.scalars(q).all()

    @property
    def est_evaluateur(self) -> bool:
        if not self.employe_id:
            return False
        if self.dept_ids or self.projet_ids:
            return True
        return db.session.scalar(
            select(Affectation.id).where(Affectation.evaluateur_id == self.employe_id).limit(1)) is not None


def perimetre() -> Perimetre:
    """Périmètre de l'utilisateur courant, mis en cache sur la requête."""
    from flask import g
    if getattr(g, "_perimetre", None) is None:
        g._perimetre = Perimetre(current_user)
    return g._perimetre
