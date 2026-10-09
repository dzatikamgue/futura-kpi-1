"""Agrégations pour le tableau de bord et le suivi trimestriel / annuel.

Convention : la note mensuelle d'un salarié est la moyenne de ses évaluations
SOUMISES du mois (s'il a plusieurs affectations). La note trimestrielle est la
moyenne de ses notes mensuelles du trimestre ; l'annuelle, la moyenne de ses
notes mensuelles de l'année. Chaque mois pèse donc le même poids.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func, or_, select

from ..extensions import db
from ..models import (Affectation, Departement, Employe, Evaluation, Projet,
                      StatutEvaluation)
from .notation import moyenne, mois_du_trimestre, niveau


def _base_evals(per, annee, departement_id=None, projet_id=None):
    q = (select(Evaluation.employe_id, Evaluation.mois, func.avg(Evaluation.note_globale))
         .where(Evaluation.annee == annee,
                Evaluation.statut == StatutEvaluation.SOUMISE,
                per.filtre_evaluations()))
    if departement_id:
        # Le département inclut les notes des projets qui lui sont rattachés
        from .projets import projets_du_departement
        pids = projets_du_departement(int(departement_id))
        cond = Evaluation.departement_id == departement_id
        q = q.where(or_(cond, Evaluation.projet_id.in_(pids)) if pids else cond)
    if projet_id:
        q = q.where(Evaluation.projet_id == projet_id)
    return q.group_by(Evaluation.employe_id, Evaluation.mois)


def notes_mensuelles(per, annee, departement_id=None, projet_id=None) -> dict[int, dict[int, float]]:
    """{employe_id: {mois: note}}"""
    res: dict[int, dict[int, float]] = defaultdict(dict)
    for emp_id, mois, note in db.session.execute(_base_evals(per, annee, departement_id, projet_id)):
        res[emp_id][mois] = round(float(note), 2) if note is not None else None
    return res


def tableau_suivi(per, annee, vue="mensuelle", departement_id=None, projet_id=None, recherche=""):
    """Matrice salariés × périodes pour l'écran Suivi et les exports."""
    mensuel = notes_mensuelles(per, annee, departement_id, projet_id)
    if vue == "trimestrielle":
        colonnes = [(t, f"T{t}") for t in range(1, 5)]
    elif vue == "annuelle":
        colonnes = []
    else:
        from .notation import MOIS_COURTS
        colonnes = [(m, MOIS_COURTS[m - 1]) for m in range(1, 13)]

    if not mensuel:
        return {"colonnes": colonnes, "lignes": [], "totaux": [], "total_annuel": None}

    employes = {e.id: e for e in db.session.scalars(
        select(Employe).where(Employe.id.in_(mensuel.keys())))}
    lignes = []
    terme = (recherche or "").strip().lower()
    for emp_id, par_mois in mensuel.items():
        e = employes.get(emp_id)
        if not e:
            continue
        if terme and terme not in f"{e.nom} {e.prenom} {e.matricule} {e.poste or ''}".lower():
            continue
        if vue == "trimestrielle":
            valeurs = [moyenne(par_mois.get(m) for m in mois_du_trimestre(t)) for t, _ in colonnes]
        elif vue == "annuelle":
            valeurs = []
        else:
            valeurs = [par_mois.get(m) for m, _ in colonnes]
        annuel = moyenne(par_mois.values())
        lignes.append({
            "employe": e,
            "valeurs": valeurs,
            "annuel": annuel,
            "niveau": niveau(annuel),
            "nb_mois": len([v for v in par_mois.values() if v is not None]),
        })
    lignes.sort(key=lambda l: (-(l["annuel"] or 0), l["employe"].nom))
    totaux = [moyenne(l["valeurs"][i] for l in lignes) for i in range(len(colonnes))]
    total_annuel = moyenne(l["annuel"] for l in lignes)
    return {"colonnes": colonnes, "lignes": lignes, "totaux": totaux, "total_annuel": total_annuel}


def indicateurs_mois(per, annee, mois):
    """KPI du tableau de bord pour un mois donné."""
    attendues = db.session.scalar(
        select(func.count(Affectation.id)).where(
            per.filtre_affectations_visibles(),
            Affectation.actif.is_(True),
            Affectation.evaluateur_id.isnot(None))) or 0
    base = select(Evaluation).where(Evaluation.annee == annee, Evaluation.mois == mois,
                                    per.filtre_evaluations())
    soumises = db.session.scalar(
        select(func.count()).select_from(base.where(Evaluation.statut == StatutEvaluation.SOUMISE).subquery())) or 0
    brouillons = db.session.scalar(
        select(func.count()).select_from(base.where(Evaluation.statut == StatutEvaluation.BROUILLON).subquery())) or 0
    moy = db.session.scalar(
        select(func.avg(Evaluation.note_globale)).where(
            Evaluation.annee == annee, Evaluation.mois == mois,
            Evaluation.statut == StatutEvaluation.SOUMISE, per.filtre_evaluations()))
    return {
        "attendues": attendues,
        "soumises": soumises,
        "brouillons": brouillons,
        "restantes": max(attendues - soumises, 0),
        "taux": round(100 * soumises / attendues) if attendues else 0,
        "moyenne": round(float(moy), 2) if moy is not None else None,
    }


def evolution_annuelle(per, annee):
    """Moyenne générale par mois (12 valeurs, None si aucune note)."""
    rows = db.session.execute(
        select(Evaluation.mois, func.avg(Evaluation.note_globale))
        .where(Evaluation.annee == annee, Evaluation.statut == StatutEvaluation.SOUMISE,
               per.filtre_evaluations())
        .group_by(Evaluation.mois)).all()
    d = {m: round(float(v), 2) for m, v in rows if v is not None}
    return [d.get(m) for m in range(1, 13)]


def repartition_niveaux(per, annee, mois):
    notes = db.session.scalars(
        select(Evaluation.note_globale).where(
            Evaluation.annee == annee, Evaluation.mois == mois,
            Evaluation.statut == StatutEvaluation.SOUMISE, per.filtre_evaluations())).all()
    ordre = ["excellent", "tres-bien", "bien", "passable", "insuffisant"]
    compte = {k: 0 for k in ordre}
    for n in notes:
        compte[niveau(n)[0]] += 1
    return compte


def moyennes_par_contexte(per, annee, mois):
    """Moyenne du mois par département et par projet (vue Direction / RH)."""
    def _agg(col, model):
        rows = db.session.execute(
            select(model.id, model.nom, func.avg(Evaluation.note_globale), func.count(Evaluation.id))
            .join(model, col == model.id)
            .where(Evaluation.annee == annee, Evaluation.mois == mois,
                   Evaluation.statut == StatutEvaluation.SOUMISE, per.filtre_evaluations())
            .group_by(model.id, model.nom)
            .order_by(func.avg(Evaluation.note_globale).desc())).all()
        return [{"id": r[0], "nom": r[1], "moyenne": round(float(r[2]), 2), "nb": r[3]} for r in rows]
    return {"departements": _par_departement(per, annee, mois),
            "projets": _agg(Evaluation.projet_id, Projet)}


def _par_departement(per, annee, mois):
    """Moyenne par département, notes des projets rattachés comprises."""
    from .projets import carte
    c = carte()
    rows = db.session.execute(
        select(Evaluation.departement_id, Evaluation.projet_id, Evaluation.note_globale)
        .where(Evaluation.annee == annee, Evaluation.mois == mois,
               Evaluation.statut == StatutEvaluation.SOUMISE, per.filtre_evaluations())).all()
    notes = defaultdict(list)
    for dep, proj, note in rows:
        d = dep or c.get(proj)
        if d and note is not None:
            notes[d].append(float(note))
    if not notes:
        return []
    noms = dict(db.session.execute(select(Departement.id, Departement.nom).where(Departement.id.in_(notes))).all())
    res = [{"id": d, "nom": noms[d], "moyenne": round(sum(v) / len(v), 2), "nb": len(v)}
           for d, v in notes.items() if d in noms]
    return sorted(res, key=lambda r: r["moyenne"], reverse=True)
