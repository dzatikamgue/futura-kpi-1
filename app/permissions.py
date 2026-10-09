"""Règles d'accès — le cœur de la confidentialité de l'application.

Règle métier :
- RH et Direction voient toutes les évaluations.
- Un collaborateur voit :
    * les évaluations des départements / projets dont il est responsable,
    * les évaluations des affectations dont il est le N+1 (évaluateur),
    * ses propres évaluations, uniquement une fois soumises.
- Seul le N+1 désigné par la RH sur l'affectation note, même si le salarié est sur
  le projet (ou dans le département) d'une autre personne. Le responsable d'un
  projet / département consulte les notes de son périmètre mais ne note pas,
  sauf s'il est lui-même désigné N+1. Personne ne se note soi-même.

Multi-entités : chaque liste est limitée à l'entité de l'onglet actif, et le rôle
(RH / Direction / Collaborateur) est évalué entité par entité. Pour un objet précis
(fiche, évaluation), le contrôle se fait avec le rôle dans l'entité de cet objet.

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
from .services.entites import (ROLE_RH, cond_affectation_entite, cond_entite,
                               cond_evaluation_entite, entite_affectation, entite_courante,
                               id_effectif, ids_employes_affectes, roles_par_entite)


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
    """Périmètre calculé une fois par requête pour un utilisateur et l'entité active."""

    def __init__(self, user):
        self.user = user
        self.roles = roles_par_entite(user)
        self.entite = entite_courante(user)
        self.entite_id = self.entite.id
        self.role = self.roles.get(self.entite_id)
        self.voit_tout = self.role in (ROLE_RH, "direction")
        self.employe_id = user.employe_id
        if self.employe_id:
            self.dept_ids = set(db.session.scalars(
                select(Departement.id).where(Departement.responsable_id == self.employe_id)))
            self.projet_ids = set(db.session.scalars(
                select(Projet.id).where(Projet.responsable_id == self.employe_id)))
            # Le chef de département consulte aussi les projets rattachés à son département
            from .services.projets import carte
            self.projet_ids |= {p for p, d in carte().items() if d in self.dept_ids}
        else:
            self.dept_ids, self.projet_ids = set(), set()

    # -- Entité ----------------------------------------------------------------
    def employes_de_l_entite(self):
        return select(Employe.id).where(cond_entite(Employe.entite_id, self.entite_id))

    def voit_tout_dans(self, entite_id) -> bool:
        return self.roles.get(id_effectif(entite_id)) in (ROLE_RH, "direction")

    # -- Affectations que l'utilisateur peut NOTER -------------------------
    def filtre_affectations_a_noter(self):
        """Expression SQL sur Affectation : uniquement celles dont l'utilisateur est le N+1 désigné."""
        if not self.employe_id:
            return false()
        from .services.projets import cond_affectation_notee
        return and_(Affectation.actif.is_(True),
                    Affectation.employe_id != self.employe_id,
                    Affectation.evaluateur_id == self.employe_id,
                    cond_affectation_entite(self.entite_id),
                    cond_affectation_notee())

    def peut_noter(self, affectation: Affectation) -> bool:
        from .services.projets import affectation_notee
        return bool(self.employe_id and affectation.actif
                    and affectation.employe_id != self.employe_id
                    and affectation.evaluateur_id == self.employe_id
                    and affectation_notee(affectation))

    # -- Affectations visibles (suivi, campagne) ---------------------------
    def filtre_affectations_visibles(self):
        """Ce que l'utilisateur voit : ses N-1 désignés + son périmètre de responsable (lecture seule).

        Une seule ligne par salarié noté : le projet s'il en a un, sinon le département.
        """
        from .services.projets import cond_affectation_notee
        dans_entite = and_(cond_affectation_entite(self.entite_id), cond_affectation_notee())
        if self.voit_tout:
            return dans_entite
        if not self.employe_id:
            return false()
        conds = [Affectation.evaluateur_id == self.employe_id]
        if self.dept_ids:
            conds.append(Affectation.departement_id.in_(self.dept_ids))
        if self.projet_ids:
            conds.append(Affectation.projet_id.in_(self.projet_ids))
        return and_(dans_entite, Affectation.actif.is_(True), Affectation.employe_id != self.employe_id,
                    or_(*conds))

    def voit_affectation(self, affectation: Affectation) -> bool:
        if self.voit_tout_dans(entite_affectation(affectation)):
            return True
        if not self.employe_id or affectation.employe_id == self.employe_id:
            return False
        return (affectation.evaluateur_id == self.employe_id
                or affectation.departement_id in self.dept_ids
                or affectation.projet_id in self.projet_ids)

    # -- Évaluations visibles ----------------------------------------------
    def filtre_evaluations(self):
        """Expression SQL sur Evaluation (jointure Affectation non requise)."""
        dans_entite = cond_evaluation_entite(self.entite_id)
        if self.voit_tout:
            return dans_entite
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
        return and_(dans_entite, or_(*conds))

    def peut_voir_evaluation(self, ev: Evaluation) -> bool:
        if self.voit_tout_dans(ev.employe.entite_id):
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
        dans_entite = cond_entite(Employe.entite_id, self.entite_id)
        if self.voit_tout:
            # Direction et RH du groupe apparaissent dans le personnel de toutes les entités
            from .services.transverses import ids_personnel_groupe
            groupe = ids_personnel_groupe()
            # + salariés d'une autre entité qui travaillent aussi pour celle-ci
            conds = [dans_entite, Employe.id.in_(ids_employes_affectes(self.entite_id))]
            if groupe:
                conds.append(Employe.id.in_(groupe))
            return or_(*conds)
        ids_affectations = select(Affectation.employe_id).where(self.filtre_affectations_visibles())
        conds = [Employe.id.in_(ids_affectations)]
        if self.employe_id:
            conds.append(and_(dans_entite, Employe.id == self.employe_id))
        return or_(*conds)

    def peut_voir_employe(self, employe: Employe) -> bool:
        if employe.id == self.employe_id or self.voit_tout_dans(employe.entite_id):
            return True
        from .services.transverses import ids_personnel_groupe
        if self.voit_tout and employe.id in ids_personnel_groupe():
            return True
        return any(self.voit_affectation(a) for a in employe.affectations)

    # -- Listes de filtres proposées dans l'interface ------------------------
    def departements_visibles(self):
        q = (select(Departement).where(Departement.actif.is_(True), cond_entite(Departement.entite_id, self.entite_id))
             .order_by(Departement.nom))
        if not self.voit_tout:
            ids = set(self.dept_ids) | set(db.session.scalars(
                select(Affectation.departement_id).where(
                    Affectation.evaluateur_id == self.employe_id,
                    Affectation.departement_id.isnot(None)))) if self.employe_id else set()
            q = q.where(Departement.id.in_(ids or {-1}))
        return db.session.scalars(q).all()

    def projets_visibles(self):
        q = (select(Projet).where(Projet.actif.is_(True), cond_entite(Projet.entite_id, self.entite_id))
             .order_by(Projet.nom))
        if not self.voit_tout:
            ids = set(self.projet_ids) | set(db.session.scalars(
                select(Affectation.projet_id).where(
                    Affectation.evaluateur_id == self.employe_id,
                    Affectation.projet_id.isnot(None)))) if self.employe_id else set()
            q = q.where(Projet.id.in_(ids or {-1}))
        return db.session.scalars(q).all()

    @property
    def est_responsable(self) -> bool:
        """Responsable d'un département / projet de l'entité active."""
        if not (self.dept_ids or self.projet_ids):
            return False
        d = db.session.scalar(select(Departement.id).where(
            Departement.id.in_(self.dept_ids or {-1}), cond_entite(Departement.entite_id, self.entite_id)).limit(1))
        p = db.session.scalar(select(Projet.id).where(
            Projet.id.in_(self.projet_ids or {-1}), cond_entite(Projet.entite_id, self.entite_id)).limit(1))
        return bool(d or p)

    @property
    def est_evaluateur(self) -> bool:
        """N+1 désigné d'au moins une affectation active dans l'entité active."""
        if not self.employe_id:
            return False
        return db.session.scalar(
            select(Affectation.id).where(self.filtre_affectations_a_noter()).limit(1)) is not None


def perimetre() -> Perimetre:
    """Périmètre de l'utilisateur courant, mis en cache sur la requête."""
    from flask import g
    if getattr(g, "_perimetre", None) is None:
        g._perimetre = Perimetre(current_user)
    return g._perimetre
