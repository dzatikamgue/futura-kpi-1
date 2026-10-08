"""Suivi mensuel, trimestriel et annuel des notes (avec exports)."""
from datetime import date

from flask import Blueprint, render_template, request
from flask_login import login_required
from sqlalchemy import select

from ..extensions import db
from ..models import Departement, Evaluation, Projet
from ..permissions import perimetre
from ..services.audit import journaliser
from ..services.exports import export_excel, export_pdf
from ..services.statistiques import tableau_suivi

bp = Blueprint("suivi", __name__, url_prefix="/suivi")

VUES = {"mensuelle": "Mensuelle", "trimestrielle": "Trimestrielle", "annuelle": "Annuelle"}


@bp.get("/")
@login_required
def index():
    per = perimetre()
    annee = request.args.get("annee", type=int) or date.today().year
    vue = request.args.get("vue", "trimestrielle")
    vue = vue if vue in VUES else "trimestrielle"
    dep = request.args.get("departement", type=int)
    proj = request.args.get("projet", type=int)
    # Sécurité : un filtre hors périmètre ne doit rien révéler
    if dep and not per.voit_tout and dep not in {d.id for d in per.departements_visibles()}:
        dep = -1
    if proj and not per.voit_tout and proj not in {p.id for p in per.projets_visibles()}:
        proj = -1
    recherche = request.args.get("q", "")
    data = tableau_suivi(per, annee, vue, dep, proj, recherche)

    filtre_txt = []
    if dep and dep > 0:
        d = db.session.get(Departement, dep)
        filtre_txt.append(f"Département : {d.nom}" if d else "")
    if proj and proj > 0:
        p = db.session.get(Projet, proj)
        filtre_txt.append(f"Projet : {p.nom}" if p else "")
    sous_titre = f"Année {annee} — vue {VUES[vue].lower()}" + (" — " + ", ".join(filtre_txt) if filtre_txt else "")

    fmt = request.args.get("export")
    if fmt in ("xlsx", "pdf"):
        entetes = ["Matricule", "Salarié", "Poste"] + [lib for _, lib in data["colonnes"]] + ["Moyenne annuelle", "Niveau"]
        n0 = 3
        num = set(range(n0, n0 + len(data["colonnes"]) + 1))
        lignes = [[l["employe"].matricule, l["employe"].nom_complet, l["employe"].poste or ""]
                  + l["valeurs"] + [l["annuel"], l["niveau"][1]] for l in data["lignes"]]
        total = ["", "Moyenne générale", ""] + data["totaux"] + [data["total_annuel"], ""]
        journaliser("export_suivi", sous_titre, f"{fmt}, {len(lignes)} lignes", commit=True)
        fn = export_excel if fmt == "xlsx" else export_pdf
        return fn(f"Suivi des performances {annee}", entetes, lignes, f"suivi_{vue}_{annee}",
                  colonnes_num=num, total=total, sous_titre=sous_titre)

    annees = sorted(set(db.session.scalars(select(Evaluation.annee).distinct())) | {date.today().year}, reverse=True)
    return render_template("suivi.html", data=data, annee=annee, vue=vue, vues=VUES, annees=annees,
                           departements=per.departements_visibles(), projets=per.projets_visibles(),
                           sous_titre=sous_titre)
