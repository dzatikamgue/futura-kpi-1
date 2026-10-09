"""Tableau de bord : KPI du mois, évolution annuelle, répartition, à faire."""
from flask import Blueprint, render_template, request
from flask_login import login_required
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..models import Affectation, Evaluation, StatutEvaluation
from ..permissions import perimetre
from ..services import statistiques as stats
from ..services.notation import (MOIS_COURTS, libelle_periode, periode_courante,
                                 periodes_saisissables, trimestre_de)

bp = Blueprint("tableau_bord", __name__)


def lire_periode():
    a, m = periode_courante()
    try:
        annee = int(request.args.get("annee", a))
        mois = int(request.args.get("mois", m))
        if not (2000 <= annee <= 2100 and 1 <= mois <= 12):
            raise ValueError
    except ValueError:
        annee, mois = a, m
    return annee, mois


@bp.get("/")
@login_required
def index():
    per = perimetre()
    annee, mois = lire_periode()
    kpi = stats.indicateurs_mois(per, annee, mois)
    evolution = stats.evolution_annuelle(per, annee)
    t = trimestre_de(mois)
    valeurs_t = [v for v in evolution[3 * (t - 1):3 * t] if v is not None]
    moyenne_t = round(sum(valeurs_t) / len(valeurs_t), 2) if valeurs_t else None
    valeurs_a = [v for v in evolution if v is not None]
    moyenne_a = round(sum(valeurs_a) / len(valeurs_a), 2) if valeurs_a else None
    repartition = stats.repartition_niveaux(per, annee, mois)
    contextes = stats.moyennes_par_contexte(per, annee, mois) if per.voit_tout else None

    # « À faire » : les affectations que JE dois noter sur les périodes ouvertes
    a_faire = []
    if per.est_evaluateur:
        for (pa, pm) in periodes_saisissables():
            affs = db.session.scalars(
                select(Affectation).options(joinedload(Affectation.employe),
                                            joinedload(Affectation.departement),
                                            joinedload(Affectation.projet))
                .where(per.filtre_affectations_a_noter())).all()
            evals = {e.affectation_id: e for e in db.session.scalars(
                select(Evaluation).where(Evaluation.annee == pa, Evaluation.mois == pm,
                                         Evaluation.affectation_id.in_([x.id for x in affs] or [-1])))}
            for x in affs:
                ev = evals.get(x.id)
                if not ev or ev.statut != StatutEvaluation.SOUMISE:
                    a_faire.append({"affectation": x, "evaluation": ev, "annee": pa, "mois": pm})
        a_faire.sort(key=lambda r: (r["annee"], r["mois"], r["affectation"].employe.nom))

    # Mes propres dernières notes (pour un collaborateur évalué)
    mes_notes = []
    if per.employe_id:
        mes_notes = db.session.scalars(
            select(Evaluation).where(Evaluation.employe_id == per.employe_id,
                                     Evaluation.statut == StatutEvaluation.SOUMISE)
            .order_by(Evaluation.annee.desc(), Evaluation.mois.desc()).limit(6)).all()

    from datetime import datetime
    from zoneinfo import ZoneInfo
    heure = datetime.now(ZoneInfo("Africa/Douala")).hour
    salutation = "Bonsoir" if heure >= 18 or heure < 4 else "Bonjour"
    return render_template(
        "tableau_bord.html", salutation=salutation, annee=annee, mois=mois, periode=libelle_periode(annee, mois),
        kpi=kpi, evolution=evolution, mois_courts=MOIS_COURTS, trimestre=t,
        moyenne_t=moyenne_t, moyenne_a=moyenne_a, repartition=repartition,
        contextes=contextes, a_faire=a_faire[:12], nb_a_faire=len(a_faire), mes_notes=mes_notes)
