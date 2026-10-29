"""Campagne mensuelle et saisie des évaluations (flux principal)."""
from datetime import timezone

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)
from flask_login import current_user, login_required
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import aliased, joinedload

from ..extensions import db
from ..models import (Affectation, Critere, Employe, Evaluation,
                      EvaluationNote, StatutEvaluation, utcnow)
from ..permissions import perimetre, rh_requis
from ..services.audit import journaliser
from ..services.entites import cond_entite, id_effectif
from ..services.exports import export_excel
from ..services.grilles import memoriser_coefficients_note, poids_pour_note
from ..services.notation import (NOTE_MAX, NOTE_MIN, SEUIL_JUSTIFICATION,
                                 calculer_note_globale, libelle_periode,
                                 periode_courante, periode_ouverte, periodes_saisissables,
                                 niveau)
from ..filters import note_fr
from ..utils import paginer
from .tableau_bord import lire_periode

bp = Blueprint("evaluations", __name__, url_prefix="/evaluations")


def _requete_campagne(per, annee, mois):
    Evaluateur = aliased(Employe)
    q = (select(Affectation, Evaluation)
         .join(Employe, Affectation.employe_id == Employe.id)
         .outerjoin(Evaluateur, Affectation.evaluateur_id == Evaluateur.id)
         .outerjoin(Evaluation, and_(Evaluation.affectation_id == Affectation.id,
                                     Evaluation.annee == annee, Evaluation.mois == mois))
         .options(joinedload(Affectation.employe), joinedload(Affectation.evaluateur),
                  joinedload(Affectation.departement), joinedload(Affectation.projet))
         .where(Affectation.actif.is_(True), Employe.actif.is_(True),
                per.filtre_affectations_visibles()))

    statut = request.args.get("statut", "")
    if statut == "a_faire":
        q = q.where(Evaluation.id.is_(None))
    elif statut in (StatutEvaluation.BROUILLON, StatutEvaluation.SOUMISE):
        q = q.where(Evaluation.statut == statut)
    elif statut == "non_soumise":
        q = q.where(or_(Evaluation.id.is_(None), Evaluation.statut == StatutEvaluation.BROUILLON))

    if dep := request.args.get("departement", type=int):
        from ..services.hierarchie import branche
        from ..services.projets import projets_du_departement
        deps = branche(dep)
        pids = set().union(*(projets_du_departement(d) for d in deps))
        q = q.where(or_(Affectation.departement_id.in_(deps), Affectation.projet_id.in_(pids or {-1})))
    if proj := request.args.get("projet", type=int):
        q = q.where(Affectation.projet_id == proj)
    if terme := (request.args.get("q") or "").strip():
        like = f"%{terme}%"
        q = q.where(or_(Employe.nom.ilike(like), Employe.prenom.ilike(like),
                        Employe.matricule.ilike(like), Employe.poste.ilike(like)))
    if request.args.get("sans_evaluateur") == "1" and per.voit_tout:
        q = q.where(Affectation.evaluateur_id.is_(None))

    tri, sens = request.args.get("tri", "nom"), request.args.get("sens", "asc")
    ordre_statut = case((Evaluation.id.is_(None), 0),
                        (Evaluation.statut == StatutEvaluation.BROUILLON, 1), else_=2)
    colonnes = {"nom": Employe.nom, "note": Evaluation.note_globale, "statut": ordre_statut,
                "evaluateur": Evaluateur.nom}
    col = colonnes.get(tri, Employe.nom)
    q = q.order_by(col.desc().nulls_last() if sens == "desc" else col.asc().nulls_last(), Employe.nom)
    return q


def _mois_recents(n: int) -> list[tuple[int, int]]:
    """Les n derniers mois (mois en cours compris), du plus récent au plus ancien."""
    a, m = periode_courante()
    res = []
    for _ in range(n):
        res.append((a, m))
        a, m = (a - 1, 12) if m == 1 else (a, m - 1)
    return res


@bp.get("/")
@login_required
def campagne():
    per = perimetre()
    annee, mois = lire_periode()
    q = _requete_campagne(per, annee, mois)

    if request.args.get("export") == "xlsx":
        lignes = []
        for aff, ev in db.session.execute(q).all():
            lignes.append([aff.employe.matricule, aff.employe.nom_complet, aff.employe.poste or "",
                           aff.contexte_libelle, aff.evaluateur.nom_complet if aff.evaluateur else "",
                           StatutEvaluation.LIBELLES.get(ev.statut, "") if ev else "À faire",
                           ev.note_globale if ev else None])
        journaliser("export_campagne", libelle_periode(annee, mois), commit=True)
        return export_excel(f"Campagne {libelle_periode(annee, mois)}",
                            ["Matricule", "Salarié", "Poste", "Contexte", "Évaluateur (N+1)", "Statut", "Note /100"],
                            lignes, f"campagne_{annee}_{mois:02d}", colonnes_num={6})

    page = paginer(q, request.args.get("page", 1, type=int), 25, scalars=False)
    lignes = page.items  # tuples (Affectation, Evaluation | None)

    # Compteurs de la période (sur tout le périmètre, hors filtres)
    base = (select(func.count(Affectation.id), func.count(Evaluation.id),
                   func.count(case((Evaluation.statut == StatutEvaluation.SOUMISE, 1))))
            .join(Employe, Affectation.employe_id == Employe.id)
            .outerjoin(Evaluation, and_(Evaluation.affectation_id == Affectation.id,
                                        Evaluation.annee == annee, Evaluation.mois == mois))
            .where(Affectation.actif.is_(True), Employe.actif.is_(True), per.filtre_affectations_visibles()))
    total, commencees, soumises = db.session.execute(base).one()

    # Mois clôturés où la RH a rouvert des évaluations à faire par cet utilisateur
    rouverts = []
    if per.employe_id:
        rouverts = sorted({(a, m) for a, m in db.session.execute(
            select(Evaluation.annee, Evaluation.mois).where(
                Evaluation.rouverte.is_(True), Evaluation.statut == StatutEvaluation.BROUILLON,
                Evaluation.evaluateur_id == per.employe_id)).all()} - set(periodes_saisissables()))
    return render_template(
        "evaluations/campagne.html", page=page, lignes=lignes, annee=annee, mois=mois,
        periodes_rouvertes=rouverts, mois_recents=_mois_recents(24),
        periode=libelle_periode(annee, mois), ouverte=periode_ouverte(annee, mois),
        periodes_ouvertes=periodes_saisissables(),
        compteurs={"total": total, "a_faire": total - commencees, "brouillons": commencees - soumises,
                   "soumises": soumises},
        departements=per.departements_visibles(), projets=per.projets_visibles())


def _criteres_pour(ev, aff):
    """Grille applicable : celle du poste du salarié si elle existe, sinon la grille commune
    de l'entité où il travaille sur cette affectation (chaque entité a ses grilles)."""
    from ..services.entites import entite_affectation
    from ..services.grilles import CritereGrille, grille_pour, poids_note
    if ev and ev.est_soumise:
        return [CritereGrille(n.critere, poids_note(n)) for n in ev.notes]
    from ..services.multi_entites import poste_dans_entite
    eid = entite_affectation(aff)
    actifs, _ = grille_pour(eid, poste_dans_entite(aff.employe, eid))
    ids = {c.id for c in actifs}
    # Brouillon commencé avec un critère qui n'est plus dans la grille : conservé
    extra = [CritereGrille(n.critere, poids_note(n)) for n in (ev.notes if ev else []) if n.critere_id not in ids]
    return actifs + extra


def _grille_propre(aff) -> bool:
    from ..services.entites import entite_affectation
    from ..services.grilles import grille_du_poste
    from ..services.multi_entites import poste_dans_entite
    eid = entite_affectation(aff)
    return grille_du_poste(eid, poste_dans_entite(aff.employe, eid)) is not None


@bp.route("/noter/<int:affectation_id>", methods=["GET", "POST"])
@login_required
def noter(affectation_id):
    per = perimetre()
    aff = db.session.get(Affectation, affectation_id) or abort(404)
    annee, mois = lire_periode()
    ev = db.session.scalar(select(Evaluation).where(Evaluation.affectation_id == aff.id,
                                                    Evaluation.annee == annee, Evaluation.mois == mois))
    if not per.peut_noter(aff):
        if ev and per.peut_voir_evaluation(ev):
            return redirect(url_for("evaluations.detail", evaluation_id=ev.id))
        abort(403)
    if ev and ev.est_soumise:
        return redirect(url_for("evaluations.detail", evaluation_id=ev.id))
    modifiable = periode_ouverte(annee, mois) or (ev is not None and ev.rouverte)

    criteres = _criteres_pour(ev, aff)
    if not criteres:
        flash("Aucun critère KPI n'est défini pour ce salarié (grille vide). La RH doit d'abord définir la grille "
              "dans Administration → Grille KPI ou sur le poste.", "erreur")
        return redirect(url_for("evaluations.campagne", annee=annee, mois=mois))
    notes_existantes = {n.critere_id: n for n in (ev.notes if ev else [])}
    erreurs, valeurs = {}, {}

    if request.method == "POST":
        if not modifiable:
            flash("Cette période est clôturée. Demandez à la RH de rouvrir l'évaluation.", "erreur")
            return redirect(url_for("evaluations.campagne", annee=annee, mois=mois))
        action = request.form.get("action", "brouillon")
        saisie = []
        for c in criteres:
            brut = request.form.get(f"note_{c.id}", "")
            com = (request.form.get(f"commentaire_{c.id}") or "").strip()[:500]
            valeurs[c.id] = {"note": brut, "commentaire": com}
            n = None
            if brut:
                try:
                    n = int(brut)
                    if not NOTE_MIN <= n <= NOTE_MAX:
                        raise ValueError
                except ValueError:
                    erreurs[f"note_{c.id}"] = "Note invalide : nombre entier de 0 à 100."
                    n = None
            if action == "soumettre" and n is None and f"note_{c.id}" not in erreurs:
                erreurs[f"note_{c.id}"] = "Note obligatoire pour soumettre."
            if action == "soumettre" and n is not None and n < SEUIL_JUSTIFICATION and not com:
                erreurs[f"commentaire_{c.id}"] = f"Justifiez une note inférieure à {SEUIL_JUSTIFICATION}."
            saisie.append((c, n, com))
        commentaire = (request.form.get("commentaire") or "").strip()[:4000]
        axes = (request.form.get("axes_amelioration") or "").strip()[:4000]

        if not erreurs:
            nouveau = ev is None
            if nouveau:
                ev = Evaluation(affectation_id=aff.id, annee=annee, mois=mois,
                                created_by_id=current_user.id)
                db.session.add(ev)
            ev.employe_id = aff.employe_id
            ev.evaluateur_id = per.employe_id
            ev.departement_id = aff.departement_id
            ev.projet_id = aff.projet_id
            ev.commentaire = commentaire
            ev.axes_amelioration = axes
            ev.updated_by_id = current_user.id
            for c, n, com in saisie:
                existante = notes_existantes.get(c.id)
                if n is None:
                    if existante:
                        db.session.delete(existante)
                    continue
                if existante:
                    existante.note, existante.commentaire, existante.poids = n, com, poids_pour_note(c.poids)
                else:
                    ev.notes.append(EvaluationNote(critere_id=c.id, note=n, commentaire=com,
                                                   poids=poids_pour_note(c.poids)))
            ev.note_globale = calculer_note_globale([(n, c.poids) for c, n, _ in saisie if n is not None])
            if action == "soumettre":
                ev.statut = StatutEvaluation.SOUMISE
                ev.soumise_le = utcnow()
                ev.rouverte = False
            try:
                db.session.flush()
                # Coefficients décimaux : la valeur exacte est gardée pour l'historique
                memoriser_coefficients_note(ev.id, {c.id: c.poids for c, n, _ in saisie if n is not None},
                                            current_user.id)
            except Exception:
                db.session.rollback()
                flash("Cette évaluation vient d'être enregistrée par une autre personne. Rechargez la page.", "erreur")
                return redirect(url_for("evaluations.noter", affectation_id=aff.id, annee=annee, mois=mois))
            journaliser("evaluation_soumise" if action == "soumettre" else "evaluation_brouillon",
                        f"{aff.employe.nom_complet} — {libelle_periode(annee, mois)}",
                        f"note={ev.note_globale}; contexte={aff.contexte_libelle}")
            db.session.commit()
            if action == "soumettre":
                flash(f"Évaluation de {aff.employe.nom_complet} soumise ({note_fr(ev.note_globale)}/100).", "succes")
                return redirect(url_for("evaluations.campagne", annee=annee, mois=mois, statut="non_soumise"))
            flash("Brouillon enregistré.", "succes")
            return redirect(url_for("evaluations.noter", affectation_id=aff.id, annee=annee, mois=mois))
        flash("Le formulaire contient des erreurs. Vérifiez les champs signalés.", "erreur")
    else:
        for c in criteres:
            n = notes_existantes.get(c.id)
            valeurs[c.id] = {"note": str(n.note) if n else "", "commentaire": n.commentaire if n and n.commentaire else ""}

    # Historique récent de la même affectation pour aider le N+1
    historique = db.session.scalars(
        select(Evaluation).where(Evaluation.affectation_id == aff.id,
                                 Evaluation.statut == StatutEvaluation.SOUMISE,
                                 or_(Evaluation.annee < annee, and_(Evaluation.annee == annee, Evaluation.mois < mois)))
        .order_by(Evaluation.annee.desc(), Evaluation.mois.desc()).limit(6)).all()

    return render_template(
        "evaluations/noter.html", aff=aff, ev=ev, annee=annee, mois=mois,
        grille_propre=_grille_propre(aff),
        periode=libelle_periode(annee, mois), criteres=criteres, valeurs=valeurs, erreurs=erreurs,
        commentaire=request.form.get("commentaire", ev.commentaire if ev else "") or "",
        axes=request.form.get("axes_amelioration", ev.axes_amelioration if ev else "") or "",
        modifiable=modifiable, historique=list(reversed(historique)), seuil=SEUIL_JUSTIFICATION,
        server_ts=int(ev.updated_at.replace(tzinfo=timezone.utc).timestamp() * 1000) if ev else 0)


@bp.get("/<int:evaluation_id>")
@login_required
def detail(evaluation_id):
    per = perimetre()
    ev = db.session.get(Evaluation, evaluation_id) or abort(404)
    if not per.peut_voir_evaluation(ev):
        abort(403)
    peut_modifier = (not ev.est_soumise and ev.affectation and per.peut_noter(ev.affectation))
    return render_template("evaluations/detail.html", ev=ev, periode=libelle_periode(ev.annee, ev.mois),
                           niveau=niveau(ev.note_globale), peut_modifier=peut_modifier)


@bp.post("/<int:evaluation_id>/rouvrir")
@login_required
@rh_requis
def rouvrir(evaluation_id):
    ev = db.session.get(Evaluation, evaluation_id) or abort(404)
    motif = (request.form.get("motif") or "").strip()
    if not motif:
        flash("Indiquez le motif de réouverture.", "erreur")
        return redirect(url_for("evaluations.detail", evaluation_id=ev.id))
    ev.statut = StatutEvaluation.BROUILLON
    ev.rouverte = True
    ev.updated_by_id = current_user.id
    journaliser("evaluation_rouverte", f"{ev.employe.nom_complet} — {libelle_periode(ev.annee, ev.mois)}", motif)
    db.session.commit()
    flash(f"Évaluation de {ev.employe.nom_complet} rouverte : "
          f"{ev.evaluateur.nom_complet if ev.evaluateur else 'l’évaluateur'} peut la refaire, même si le mois est clôturé.", "succes")
    retour = request.form.get("retour") or ""
    if retour.startswith("/") and not retour.startswith("//"):
        return redirect(retour)
    return redirect(url_for("evaluations.detail", evaluation_id=ev.id))


@bp.post("/autoriser")
@login_required
@rh_requis
def autoriser_saisie():
    """Mois clôturé : la RH / l'admin autorise le N+1 à noter quand même (saisie en retard).

    Aucune structure ajoutée : une évaluation « brouillon rouverte » est préparée pour chaque
    affectation choisie (ou les brouillons existants sont rouverts). L'évaluateur la complète
    puis la soumet comme d'habitude ; à la soumission, la réouverture prend fin.
    """
    per = perimetre()
    annee, mois = lire_periode()
    retour = url_for("evaluations.campagne", annee=annee, mois=mois)
    if request.form.get("tout") == "1":
        ids = {a.id for a, ev in db.session.execute(
            _requete_campagne(per, annee, mois).order_by(None)).all() if not (ev and ev.est_soumise)}
    else:
        ids = {int(x) for x in request.form.getlist("aff_ids") if x.isdigit()}
    affs = db.session.scalars(select(Affectation).where(Affectation.id.in_(ids or {-1}),
                                                        Affectation.actif.is_(True))).all()
    existantes = {e.affectation_id: e for e in db.session.scalars(select(Evaluation).where(
        Evaluation.affectation_id.in_(ids or {-1}), Evaluation.annee == annee, Evaluation.mois == mois))}
    n = 0
    for aff in affs:
        ev = existantes.get(aff.id)
        if ev and ev.est_soumise:
            continue                                   # déjà soumise : utiliser « Faire refaire »
        if ev is None:
            ev = Evaluation(affectation_id=aff.id, annee=annee, mois=mois, employe_id=aff.employe_id,
                            evaluateur_id=aff.evaluateur_id, departement_id=aff.departement_id,
                            projet_id=aff.projet_id, created_by_id=current_user.id)
            db.session.add(ev)
        ev.rouverte = True
        ev.updated_by_id = current_user.id
        n += 1
    if not n:
        flash("Aucune évaluation à autoriser (déjà soumises ou aucune sélection).", "info")
        return redirect(retour)
    journaliser("saisie_tardive_autorisee", libelle_periode(annee, mois), f"{n} évaluation(s)")
    db.session.commit()
    flash(f"Saisie autorisée pour {n} évaluation(s) de {libelle_periode(annee, mois)} : "
          "les N+1 concernés peuvent noter maintenant, puis soumettre.", "succes")
    return redirect(retour)
