"""Suppression définitive de salariés (individuelle ou en masse).

Aucune modification de structure : on efface uniquement les lignes concernées,
dans une seule transaction, et chaque suppression est tracée dans le journal.
Ce qui est supprimé avec la fiche : affectations, évaluations reçues (et leurs notes),
compte d'accès. Ce qui est conservé : les évaluations que la personne a données à
d'autres (le nom de l'évaluateur disparaît), les départements / projets qu'elle dirigeait
(ils n'ont plus de responsable), les affectations dont elle était le N+1 (« à désigner »).
"""
from __future__ import annotations

from sqlalchemy import delete, func, select, update

from ..extensions import db
from ..models import (AccesEntite, Affectation, Departement, Employe, Evaluation,
                      EvaluationNote, ImportBrouillon, JournalAction, Projet,
                      Utilisateur)


def impacts(ids: list[int]) -> dict:
    """Ce que la suppression va effacer ou modifier (affiché avant confirmation)."""
    if not ids:
        return {}
    compte = lambda q: db.session.scalar(q) or 0  # noqa: E731
    return {
        "evaluations": compte(select(func.count(Evaluation.id)).where(Evaluation.employe_id.in_(ids))),
        "affectations": compte(select(func.count(Affectation.id)).where(Affectation.employe_id.in_(ids))),
        "comptes": compte(select(func.count(Utilisateur.id)).where(Utilisateur.employe_id.in_(ids))),
        "n_moins_1": compte(select(func.count(Affectation.id)).where(
            Affectation.evaluateur_id.in_(ids), Affectation.employe_id.notin_(ids), Affectation.actif.is_(True))),
        "evaluations_donnees": compte(select(func.count(Evaluation.id)).where(
            Evaluation.evaluateur_id.in_(ids), Evaluation.employe_id.notin_(ids))),
        "responsabilites": compte(select(func.count(Departement.id)).where(Departement.responsable_id.in_(ids)))
                          + compte(select(func.count(Projet.id)).where(Projet.responsable_id.in_(ids))),
    }


def supprimer_employes(ids: list[int]) -> int:
    """Supprime définitivement les salariés indiqués. Renvoie le nombre de fiches supprimées."""
    if not ids:
        return 0
    # 1) Évaluations reçues (et leurs notes)
    ev_ids = select(Evaluation.id).where(Evaluation.employe_id.in_(ids)).scalar_subquery()
    db.session.execute(delete(EvaluationNote).where(EvaluationNote.evaluation_id.in_(ev_ids)))
    db.session.execute(delete(Evaluation).where(Evaluation.employe_id.in_(ids)))
    # 2) Ce qui reste chez les autres : on retire seulement la référence
    db.session.execute(update(Evaluation).where(Evaluation.evaluateur_id.in_(ids)).values(evaluateur_id=None))
    db.session.execute(update(Affectation).where(Affectation.evaluateur_id.in_(ids)).values(evaluateur_id=None))
    db.session.execute(update(Departement).where(Departement.responsable_id.in_(ids)).values(responsable_id=None))
    db.session.execute(update(Projet).where(Projet.responsable_id.in_(ids)).values(responsable_id=None))
    # 3) Comptes d'accès liés à ces fiches
    u_ids = [i for i in db.session.scalars(select(Utilisateur.id).where(Utilisateur.employe_id.in_(ids)))]
    if u_ids:
        for col in (Evaluation.created_by_id, Evaluation.updated_by_id):
            db.session.execute(update(Evaluation).where(col.in_(u_ids)).values({col.key: None}))
        db.session.execute(update(JournalAction).where(JournalAction.utilisateur_id.in_(u_ids)).values(utilisateur_id=None))
        db.session.execute(delete(ImportBrouillon).where(ImportBrouillon.utilisateur_id.in_(u_ids)))
        db.session.execute(delete(AccesEntite).where(AccesEntite.utilisateur_id.in_(u_ids)))
        db.session.execute(delete(Utilisateur).where(Utilisateur.id.in_(u_ids)))
    # 4) Affectations puis fiches
    db.session.execute(delete(Affectation).where(Affectation.employe_id.in_(ids)))
    n = db.session.execute(delete(Employe).where(Employe.id.in_(ids))).rowcount
    db.session.expire_all()
    return n
