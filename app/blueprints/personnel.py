"""Gestion du personnel : liste, fiche, création, modification, affectations."""
from collections import defaultdict

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)
from flask_login import login_required
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import selectinload

from ..extensions import db
from ..models import (Affectation, Departement, Employe, Evaluation, Projet,
                      StatutEvaluation)
from ..permissions import perimetre, rh_requis
from ..services.audit import journaliser
from ..services.comptes import aligner_email, synchroniser_comptes
from ..services.entites import cond_entite, id_effectif, principale
from ..services.organisation import (affecter, affecter_equipe, changer_projet, contextes_groupe, evaluateurs_groupe, matricule_auto,
                                     matricule_pris, poste_canonique, postes_actifs)
from ..services.exports import export_excel, export_pdf
from ..services.notation import MOIS_COURTS, moyenne, mois_du_trimestre, niveau
from ..utils import lire_date, paginer

bp = Blueprint("personnel", __name__, url_prefix="/personnel")

CHAMPS = ("matricule", "nom", "prenom", "poste", "email", "telephone", "date_embauche")


def _requete_liste(per, args=None):
    args = request.args if args is None else args
    q = (select(Employe).options(selectinload(Employe.affectations).selectinload(Affectation.departement),
                                 selectinload(Employe.affectations).selectinload(Affectation.projet),
                                 selectinload(Employe.affectations).selectinload(Affectation.evaluateur))
         .where(per.filtre_employes()))
    statut = args.get("statut", "actif")
    if statut == "actif":
        q = q.where(Employe.actif.is_(True))
    elif statut == "inactif":
        q = q.where(Employe.actif.is_(False))
    if terme := (args.get("q") or "").strip():
        like = f"%{terme}%"
        q = q.where(or_(Employe.nom.ilike(like), Employe.prenom.ilike(like),
                        Employe.matricule.ilike(like), Employe.poste.ilike(like), Employe.email.ilike(like)))
    if poste := (args.get("poste") or "").strip():
        q = q.where(Employe.poste == poste)
    if dep := args.get("departement", type=int):
        q = q.where(exists().where(Affectation.employe_id == Employe.id, Affectation.departement_id == dep,
                                   Affectation.actif.is_(True)))
    if proj := args.get("projet", type=int):
        q = q.where(exists().where(Affectation.employe_id == Employe.id, Affectation.projet_id == proj,
                                   Affectation.actif.is_(True)))
    if args.get("sans_affectation") == "1":
        q = q.where(~exists().where(Affectation.employe_id == Employe.id, Affectation.actif.is_(True)))
    tri, sens = args.get("tri", "nom"), args.get("sens", "asc")
    col = {"nom": Employe.nom, "matricule": Employe.matricule, "poste": Employe.poste,
           "embauche": Employe.date_embauche}.get(tri, Employe.nom)
    return q.order_by(col.desc().nulls_last() if sens == "desc" else col.asc().nulls_last(), Employe.prenom)


@bp.get("/")
@login_required
def liste():
    per = perimetre()
    q = _requete_liste(per)
    fmt = request.args.get("export")
    if fmt in ("xlsx", "pdf"):
        employes = db.session.scalars(q).all()
        entetes = ["Matricule", "Nom", "Prénom", "Poste", "Affectations", "Email", "Téléphone", "Embauche"]
        lignes = [[e.matricule, e.nom, e.prenom, e.poste or "",
                   " / ".join(a.contexte_libelle for a in e.affectations_actives),
                   e.email or "", e.telephone or "",
                   e.date_embauche.strftime("%d/%m/%Y") if e.date_embauche else ""] for e in employes]
        journaliser("export_personnel", fmt, f"{len(lignes)} lignes", commit=True)
        if fmt == "xlsx":
            return export_excel("Liste du personnel", entetes, lignes, "personnel")
        return export_pdf("Liste du personnel", entetes, lignes, "personnel")
    page = paginer(q, request.args.get("page", 1, type=int), 25)
    # Postes présents dans le personnel visible (liste du filtre)
    postes = sorted(set(db.session.scalars(
        select(Employe.poste).where(per.filtre_employes(), Employe.poste.isnot(None), Employe.poste != "")
        .distinct())), key=str.lower)
    from ..services.transverses import ids_personnel_groupe
    return render_template("personnel/liste.html", page=page, postes=postes, ids_groupe=ids_personnel_groupe(),
                           departements=per.departements_visibles(), projets=per.projets_visibles())


def _contextes(entite_id: int):
    """Départements et projets actifs d'une entité."""
    return (db.session.scalars(select(Departement).where(Departement.actif.is_(True),
                                                         cond_entite(Departement.entite_id, entite_id))
                               .order_by(Departement.nom)).all(),
            db.session.scalars(select(Projet).where(Projet.actif.is_(True), cond_entite(Projet.entite_id, entite_id))
                               .order_by(Projet.nom)).all())


def _choix_formulaire(employe: Employe | None, entite_id: int):
    """Listes déroulantes du formulaire salarié (entité de la fiche ; N+1 dans tout le groupe)."""
    postes = postes_actifs(entite_id)
    if employe and employe.poste and employe.poste not in postes:
        postes.append(employe.poste)  # poste historique hors référentiel
    departements, projets = _contextes(entite_id)
    return dict(postes=postes, departements=departements, projets=projets,
                groupes_evaluateurs=evaluateurs_groupe(employe.id if employe else None))


def _lire_formulaire(employe: Employe | None, entite_id: int):
    donnees = {k: (request.form.get(k) or "").strip() for k in CHAMPS + ("poste_nouveau", "departement_id",
                                                                            "projet_id", "evaluateur_id")}
    erreurs = {}
    if len(donnees["matricule"]) > 30:
        erreurs["matricule"] = "30 caractères maximum."
    elif donnees["matricule"]:
        if matricule_pris(donnees["matricule"], entite_id, employe.id if employe else None):
            erreurs["matricule"] = "Ce matricule est déjà attribué dans cette entité."
    elif employe:
        erreurs["matricule"] = "Le matricule est obligatoire."
    if not donnees["nom"]:
        erreurs["nom"] = "Le nom est obligatoire."
    if donnees["poste"] == "__nouveau__":
        if not donnees["poste_nouveau"]:
            erreurs["poste"] = "Saisissez le libellé du nouveau poste."
        donnees["poste"] = donnees["poste_nouveau"][:120]
        donnees["poste_cree"] = True
    if donnees["email"]:
        if "@" not in donnees["email"] or "." not in donnees["email"].split("@")[-1]:
            erreurs["email"] = "Adresse e-mail invalide."
        else:
            q = select(Employe.id).where(func.lower(Employe.email) == donnees["email"].lower())
            if employe:
                q = q.where(Employe.id != employe.id)
            if db.session.scalar(q):
                erreurs["email"] = "Cet e-mail est déjà utilisé par un autre salarié (il sert d'identifiant de connexion)."
    try:
        donnees["date_embauche_val"] = lire_date(donnees["date_embauche"])
    except ValueError as exc:
        erreurs["date_embauche"] = str(exc)
    return donnees, erreurs


def _apres_enregistrement(e: Employe, n1_id: int | None = None):
    """Liaison du compte d'accès de CE salarié (et de son N+1) + alerte si l'identifiant n'a pas pu suivre."""
    alerte = aligner_email(e)
    stats = synchroniser_comptes([e.id, n1_id])
    if alerte:
        flash(alerte, "info")
    if e.compte and stats["lies"]:
        flash(f"Compte d'accès {e.compte.email} lié à cette fiche.", "info")


@bp.route("/nouveau", methods=["GET", "POST"])
@login_required
@rh_requis
def nouveau():
    per = perimetre()
    eid = per.entite_id
    donnees, erreurs = {}, {}
    if request.method == "POST":
        donnees, erreurs = _lire_formulaire(None, eid)
        dep = db.session.get(Departement, int(donnees["departement_id"])) if donnees["departement_id"].isdigit() else None
        proj = db.session.get(Projet, int(donnees["projet_id"])) if donnees["projet_id"].isdigit() else None
        # Le rattachement doit appartenir à l'entité de la fiche
        if dep and id_effectif(dep.entite_id) != eid:
            dep = None
        if proj and id_effectif(proj.entite_id) != eid:
            proj = None
        if not erreurs:
            poste = poste_canonique(donnees["poste"], True, eid) if donnees["poste"] else None
            e = Employe(entite_id=eid, matricule=donnees["matricule"] or matricule_auto(per.entite), nom=donnees["nom"].upper(),
                        prenom=donnees["prenom"], poste=poste,
                        email=donnees["email"].lower() or None, telephone=donnees["telephone"] or None,
                        date_embauche=donnees["date_embauche_val"])
            db.session.add(e)
            db.session.flush()
            n1 = int(donnees["evaluateur_id"]) if donnees["evaluateur_id"].isdigit() else None
            affs = [affecter(e, departement=dep, evaluateur_id=n1) if dep else None,
                    affecter(e, projet=proj, evaluateur_id=n1) if proj else None]
            affs = [a for a in affs if a]
            journaliser("employe_cree", e.nom_complet, e.matricule)
            _apres_enregistrement(e, n1)
            db.session.commit()
            if affs:
                sans_n1 = [a for a in affs if not a.evaluateur_id]
                msg = f"{e.nom_complet} ajouté ({', '.join(a.contexte_libelle for a in affs)})."
                if sans_n1:
                    msg += " Aucun N+1 désigné : choisissez-le ci-dessous, sinon personne ne peut le noter."
                flash(msg, "succes")
            else:
                flash(f"{e.nom_complet} ajouté. Rattachez-le à un département ou un projet pour qu'il soit noté.", "succes")
            if request.form.get("suivant") == "1":
                return redirect(url_for("personnel.nouveau", departement_id=dep.id if dep else None,
                                        projet_id=proj.id if proj else None, evaluateur_id=n1))
            return redirect(url_for("personnel.fiche", employe_id=e.id))
    else:
        # « Enregistrer et ajouter un autre » : on garde le rattachement précédent
        donnees = {k: request.args.get(k, "") for k in ("departement_id", "projet_id", "evaluateur_id")}
    return render_template("personnel/formulaire.html", employe=None, donnees=donnees, erreurs=erreurs,
                           **_choix_formulaire(None, eid))


@bp.route("/<int:employe_id>/modifier", methods=["GET", "POST"])
@login_required
@rh_requis
def modifier(employe_id):
    e = db.session.get(Employe, employe_id) or abort(404)
    eid = id_effectif(e.entite_id)
    erreurs = {}
    if request.method == "POST":
        donnees, erreurs = _lire_formulaire(e, eid)
        if not erreurs:
            e.matricule = donnees["matricule"]
            e.nom = donnees["nom"].upper()
            e.prenom = donnees["prenom"]
            e.poste = poste_canonique(donnees["poste"], bool(donnees.get("poste_cree")), eid) if donnees["poste"] else None
            e.email = donnees["email"].lower() or None
            e.telephone = donnees["telephone"] or None
            e.date_embauche = donnees["date_embauche_val"]
            n1 = _maj_rattachement(e, eid, donnees)
            journaliser("employe_modifie", e.nom_complet, e.matricule)
            _apres_enregistrement(e, n1)
            db.session.commit()
            flash("Fiche mise à jour.", "succes")
            return redirect(url_for("personnel.fiche", employe_id=e.id))
    else:
        donnees = {k: getattr(e, k) or "" for k in CHAMPS}
        donnees["date_embauche"] = e.date_embauche.isoformat() if e.date_embauche else ""
        dep_aff, proj_aff = _affectations_principales(e)
        donnees["departement_id"] = str(dep_aff.departement_id) if dep_aff else ""
        donnees["projet_id"] = str(proj_aff.projet_id) if proj_aff else ""
        n1 = (dep_aff.evaluateur_id if dep_aff and dep_aff.evaluateur_id else
              proj_aff.evaluateur_id if proj_aff else None)
        donnees["evaluateur_id"] = str(n1 or "")
    autres = len(e.affectations_actives) - sum(1 for a in _affectations_principales(e) if a)
    return render_template("personnel/formulaire.html", employe=e, donnees=donnees, erreurs=erreurs,
                           autres_affectations=autres, **_choix_formulaire(e, eid))


def _affectations_principales(e: Employe):
    """Première affectation active de type département et de type projet (gérées par le formulaire)."""
    actives = sorted(e.affectations_actives, key=lambda a: a.id)
    return (next((a for a in actives if a.departement_id), None),
            next((a for a in actives if a.projet_id), None))


def _maj_rattachement(e: Employe, eid: int, donnees: dict) -> int | None:
    """Applique les listes Département / Projet / N+1 du formulaire de modification."""
    n1 = int(donnees["evaluateur_id"]) if donnees.get("evaluateur_id", "").isdigit() else None
    if n1 == e.id:
        n1 = None
    dep_aff, proj_aff = _affectations_principales(e)
    for model, champ, actuelle, cle in ((Departement, "departement_id", dep_aff, "departement_id"),
                                         (Projet, "projet_id", proj_aff, "projet_id")):
        if cle not in request.form:  # champ absent du formulaire : on ne touche à rien
            continue
        brut = donnees.get(cle, "")
        cible = db.session.get(model, int(brut)) if brut.isdigit() else None
        if cible and id_effectif(cible.entite_id) != eid:
            cible = None
        actuel_id = getattr(actuelle, champ) if actuelle else None
        if (cible.id if cible else None) != actuel_id:
            if actuelle:
                actuelle.actif = False  # clôturée : l'historique des notes est conservé
                journaliser("affectation_retiree", e.nom_complet, actuelle.contexte_libelle)
            if cible:
                a = affecter(e, **{("departement" if model is Departement else "projet"): cible}, evaluateur_id=n1)
                journaliser("affectation_ajoutee", e.nom_complet, a.contexte_libelle)
        elif actuelle and n1 != actuelle.evaluateur_id and "evaluateur_id" in request.form:
            actuelle.evaluateur_id = n1
            journaliser("affectation_n1_modifie", e.nom_complet, actuelle.contexte_libelle)
    return n1


@bp.post("/<int:employe_id>/statut")
@login_required
@rh_requis
def basculer_statut(employe_id):
    e = db.session.get(Employe, employe_id) or abort(404)
    e.actif = not e.actif
    if not e.actif:
        for a in e.affectations:
            a.actif = False
        if e.compte:
            e.compte.actif = False
    journaliser("employe_" + ("reactive" if e.actif else "desactive"), e.nom_complet)
    db.session.commit()
    flash(f"{e.nom_complet} {'réactivé' if e.actif else 'désactivé (affectations et compte suspendus)'}.", "succes")
    return redirect(url_for("personnel.fiche", employe_id=e.id))


@bp.get("/<int:employe_id>")
@login_required
def fiche(employe_id):
    per = perimetre()
    e = db.session.get(Employe, employe_id) or abort(404)
    if not per.peut_voir_employe(e):
        abort(403)
    annees = sorted(set(db.session.scalars(
        select(Evaluation.annee).where(Evaluation.employe_id == e.id, per.filtre_evaluations()).distinct())),
        reverse=True)
    annee = request.args.get("annee", type=int) or (annees[0] if annees else None)
    from datetime import date
    annee = annee or date.today().year

    evals = db.session.scalars(
        select(Evaluation).options(selectinload(Evaluation.notes))
        .where(Evaluation.employe_id == e.id, Evaluation.annee == annee, per.filtre_evaluations())
        .order_by(Evaluation.mois.desc(), Evaluation.id)).all()

    par_mois = defaultdict(list)
    for ev in evals:
        if ev.est_soumise and ev.note_globale is not None:
            par_mois[ev.mois].append(ev.note_globale)
    mensuel = {m: moyenne(v) for m, v in par_mois.items()}
    trimestres = [(t, moyenne(mensuel.get(m) for m in mois_du_trimestre(t))) for t in range(1, 5)]
    annuel = moyenne(mensuel.values())

    affs_visibles = [a for a in e.affectations if per.voit_tout or per.voit_affectation(a) or a.employe_id == per.employe_id]
    return render_template(
        "personnel/fiche.html", e=e, annee=annee, annees=annees or [annee], evals=evals,
        serie=[mensuel.get(m) for m in range(1, 13)], mois_courts=MOIS_COURTS,
        trimestres=trimestres, annuel=annuel, niveau_annuel=niveau(annuel),
        affectations=affs_visibles,
        contextes=contextes_groupe() if per.user.est_rh else [],
        groupes_evaluateurs=evaluateurs_groupe(e.id) if per.user.est_rh else [])


@bp.post("/<int:employe_id>/affectations")
@login_required
@rh_requis
def ajouter_affectation(employe_id):
    """Ajoute une ou deux affectations (département et/ou projet) choisies dans des listes."""
    e = db.session.get(Employe, employe_id) or abort(404)
    eval_id = request.form.get("evaluateur_id", type=int)
    if eval_id == e.id:
        flash("Un salarié ne peut pas être son propre N+1.", "erreur")
        return redirect(url_for("personnel.fiche", employe_id=e.id))
    choix = []
    for model, cle in ((Departement, "departement_id"), (Projet, "projet_id")):
        ctx_id = request.form.get(cle, type=int)
        if ctx_id:
            # Un contexte d'une autre entité est permis : le salarié travaille alors pour plusieurs entités
            ctx = db.session.get(model, ctx_id) or abort(404)
            choix.append((model, ctx))
    # Compatibilité : ancienne liste unique « departement:ID » / « projet:ID »
    type_ctx, _, brut_id = (request.form.get("contexte") or "").partition(":")
    if not choix and type_ctx in ("departement", "projet") and brut_id.isdigit():
        model = Departement if type_ctx == "departement" else Projet
        ctx = db.session.get(model, int(brut_id)) or abort(404)
        choix.append((model, ctx))
    if not choix:
        flash("Choisissez un département et/ou un projet.", "erreur")
        return redirect(url_for("personnel.fiche", employe_id=e.id))
    libelles = []
    for model, ctx in choix:
        a = affecter(e, departement=ctx if model is Departement else None,
                     projet=ctx if model is Projet else None, evaluateur_id=eval_id)
        journaliser("affectation_ajoutee", e.nom_complet, a.contexte_libelle)
        libelles.append(a.contexte_libelle)
    synchroniser_comptes([eval_id])
    db.session.commit()
    msg = "Affectation(s) enregistrée(s) : " + ", ".join(libelles) + "."
    if not eval_id:
        msg += " Aucun N+1 désigné : personne ne peut encore noter ce salarié sur ce rattachement."
    flash(msg, "succes")
    return redirect(url_for("personnel.fiche", employe_id=e.id))


@bp.post("/affectations/<int:affectation_id>")
@login_required
@rh_requis
def maj_affectation(affectation_id):
    a = db.session.get(Affectation, affectation_id) or abort(404)
    action = request.form.get("action")
    if action == "retirer":
        # On ne supprime pas : l'historique des notes reste rattaché
        a.actif = False
        journaliser("affectation_retiree", a.employe.nom_complet, a.contexte_libelle)
        flash("Affectation clôturée. L'historique des notes est conservé.", "succes")
    elif action == "evaluateur":
        nouvel = request.form.get("evaluateur_id", type=int)
        if nouvel == a.employe_id:
            flash("Un salarié ne peut pas être son propre N+1.", "erreur")
        else:
            a.evaluateur_id = nouvel
            journaliser("affectation_n1_modifie", a.employe.nom_complet, a.contexte_libelle)
            synchroniser_comptes([nouvel])
            flash("N+1 mis à jour.", "succes")
    db.session.commit()
    return redirect(url_for("personnel.fiche", employe_id=a.employe_id))


# --- Équipe d'un département / projet : affectation en masse -----------------
_TYPES = {"departement": Departement, "projet": Projet}


def _contexte_ou_404(type_ctx, ctx_id):
    model = _TYPES.get(type_ctx) or abort(404)
    return db.session.get(model, ctx_id) or abort(404)


@bp.get("/equipe/<type_ctx>/<int:ctx_id>")
@login_required
@rh_requis
def equipe(type_ctx, ctx_id):
    ctx = _contexte_ou_404(type_ctx, ctx_id)
    col = Affectation.projet_id if type_ctx == "projet" else Affectation.departement_id
    membres = db.session.scalars(
        select(Affectation).join(Employe, Employe.id == Affectation.employe_id)
        .options(selectinload(Affectation.employe), selectinload(Affectation.evaluateur))
        .where(col == ctx.id, Affectation.actif.is_(True), Employe.actif.is_(True))
        .order_by(Employe.nom, Employe.prenom)).all()
    deja = {a.employe_id for a in membres}
    from ..models import Entite
    candidats = [e for e in db.session.scalars(
        select(Employe).options(selectinload(Employe.affectations))
        .where(Employe.actif.is_(True)).order_by(Employe.nom, Employe.prenom)) if e.id not in deja]
    entites = db.session.scalars(select(Entite).where(Entite.actif.is_(True))
                                 .order_by(Entite.principale.desc(), Entite.nom)).all()
    from ..services.projets import affectation_notee
    return render_template(
        "personnel/equipe.html", ctx=ctx, type_ctx=type_ctx, membres=membres, candidats=candidats,
        entites=entites, entite_ctx=id_effectif(ctx.entite_id), id_principale=principale().id,
        postes=sorted({e.poste for e in candidats if e.poste}, key=str.lower),
        notee={a.id: affectation_notee(a) for a in membres},
        groupes_evaluateurs=evaluateurs_groupe())


@bp.post("/equipe/<type_ctx>/<int:ctx_id>/ajouter")
@login_required
@rh_requis
def equipe_ajouter(type_ctx, ctx_id):
    ctx = _contexte_ou_404(type_ctx, ctx_id)
    retour = redirect(url_for("personnel.equipe", type_ctx=type_ctx, ctx_id=ctx.id))
    ids = {int(i) for i in request.form.getlist("ids") if str(i).isdigit()}
    evaluateur_id = request.form.get("evaluateur_id", type=int)
    if not ids:
        flash("Cochez au moins un salarié à affecter.", "erreur")
        return retour
    if not evaluateur_id or db.session.get(Employe, evaluateur_id) is None:
        flash("Désignez le N+1 qui notera ces salariés.", "erreur")
        return retour
    employes = db.session.scalars(select(Employe).where(Employe.id.in_(ids), Employe.actif.is_(True))).all()
    n, exclus = affecter_equipe(employes, ctx, evaluateur_id, remplacer=request.form.get("remplacer") == "1")
    journaliser("equipe_affectee", ctx.nom, f"{n} salarié(s)")
    synchroniser_comptes([evaluateur_id])
    db.session.commit()
    msg = f"{n} salarié(s) affecté(s) à « {ctx.nom} »."
    if exclus:
        msg += " Non affectés : " + ", ".join(exclus) + "."
    flash(msg, "succes")
    return retour


@bp.post("/equipe/<type_ctx>/<int:ctx_id>/membres")
@login_required
@rh_requis
def equipe_membres(type_ctx, ctx_id):
    ctx = _contexte_ou_404(type_ctx, ctx_id)
    retour = redirect(url_for("personnel.equipe", type_ctx=type_ctx, ctx_id=ctx.id))
    col = Affectation.projet_id if type_ctx == "projet" else Affectation.departement_id
    ids = {int(i) for i in request.form.getlist("aff_ids") if str(i).isdigit()}
    affs = db.session.scalars(select(Affectation).where(Affectation.id.in_(ids or {-1}), col == ctx.id,
                                                        Affectation.actif.is_(True))).all()
    if not affs:
        flash("Cochez au moins un membre.", "erreur")
        return retour
    action = request.form.get("action")
    if action == "retirer":
        for a in affs:
            a.actif = False
        journaliser("equipe_retrait", ctx.nom, f"{len(affs)} salarié(s)")
        flash(f"{len(affs)} salarié(s) retiré(s) de « {ctx.nom} ». L'historique des notes est conservé.", "succes")
    elif action == "n1":
        nouvel = request.form.get("evaluateur_id", type=int)
        if not nouvel or db.session.get(Employe, nouvel) is None:
            flash("Choisissez le nouveau N+1.", "erreur")
            return retour
        n = 0
        for a in affs:
            if a.employe_id != nouvel:
                a.evaluateur_id = nouvel
                n += 1
        journaliser("equipe_n1", ctx.nom, f"{n} salarié(s)")
        synchroniser_comptes([nouvel])
        flash(f"N+1 mis à jour pour {n} salarié(s).", "succes")
    else:
        abort(400)
    db.session.commit()
    return retour


# --- Changement de projet (mutation) -----------------------------------------
def _lire_mutation():
    """Nouveau projet + N+1 (obligatoire : il est redéfini à chaque changement)."""
    projet = db.session.get(Projet, request.form.get("projet_id", type=int) or -1)
    evaluateur_id = request.form.get("evaluateur_id", type=int)
    erreur = None
    if projet is None or not projet.actif:
        erreur = "Choisissez le nouveau projet."
    elif not evaluateur_id or db.session.get(Employe, evaluateur_id) is None:
        erreur = "Désignez le N+1 qui notera le salarié sur le nouveau projet."
    return projet, evaluateur_id, erreur


@bp.post("/affectations/<int:affectation_id>/changer-projet")
@login_required
@rh_requis
def changer_projet_individuel(affectation_id):
    a = db.session.get(Affectation, affectation_id) or abort(404)
    retour = redirect(url_for("personnel.fiche", employe_id=a.employe_id))
    if not (a.actif and a.projet_id):
        abort(400)
    projet, evaluateur_id, erreur = _lire_mutation()
    if not erreur and projet.id == a.projet_id:
        erreur = "Le salarié est déjà sur ce projet."
    if not erreur and evaluateur_id == a.employe_id:
        erreur = "Un salarié ne peut pas être son propre N+1."
    if erreur:
        flash(erreur, "erreur")
        return retour
    nouvelle, fermees = changer_projet(a.employe, projet, evaluateur_id, ancienne=a)
    journaliser("changement_projet", a.employe.nom_complet, f"{', '.join(fermees)} → {nouvelle.contexte_libelle}")
    synchroniser_comptes([evaluateur_id])
    db.session.commit()
    flash(f"{a.employe.nom_complet} passe sur « {projet.nom} » ; N+1 : {nouvelle.evaluateur.nom_complet}. "
          "L'historique des notes de l'ancien projet est conservé.", "succes")
    return retour


@bp.post("/changer-projet")
@login_required
@rh_requis
def changer_projet_groupe():
    """Mutation groupée (ex. fin de chantier : toute l'équipe passe sur le projet suivant)."""
    per = perimetre()
    ids = [int(i) for i in request.form.getlist("ids") if str(i).isdigit()]
    employes = db.session.scalars(select(Employe).where(Employe.id.in_(ids or [-1]), per.filtre_employes())).all()
    projet, evaluateur_id, erreur = _lire_mutation()
    if erreur or not employes:
        flash(erreur or "Sélectionnez au moins un salarié.", "erreur")
        return _page_changer_projet(employes, request.form)
    n, exclus = 0, []
    for e in employes:
        if e.id == evaluateur_id:
            exclus.append(f"{e.nom_complet} (ne peut pas être son propre N+1)")
            continue
        changer_projet(e, projet, evaluateur_id)
        n += 1
    journaliser("changement_projet_groupe", f"{n} salarié(s)", projet.nom)
    synchroniser_comptes([evaluateur_id])
    db.session.commit()
    msg = f"{n} salarié(s) affecté(s) à « {projet.nom} »; leur ancien projet dans cette entité est clôturé, l'historique est conservé."
    if exclus:
        msg += " Non modifiés : " + ", ".join(exclus) + "."
    flash(msg, "succes")
    return redirect(url_for("personnel.liste"))


def _page_changer_projet(employes, donnees=None):
    return render_template("personnel/changer_projet.html", employes=employes, donnees=donnees or {},
                           contextes=contextes_groupe(), groupes_evaluateurs=evaluateurs_groupe())


# --- Sélection : actions groupées et suppression -------------------------------
def _ids_selectionnes(per, source) -> list[int]:
    """Fiches choisies (cases cochées, ou « tous les résultats » des filtres), limitées au périmètre."""
    if source.get("tout") == "1":
        from urllib.parse import parse_qs

        from werkzeug.datastructures import MultiDict
        filtres = MultiDict([(k, v) for k, vs in parse_qs(source.get("filtres", "")).items() for v in vs])
        # Le personnel « groupe » d'une autre entité est exclu des actions groupées
        return [e.id for e in db.session.scalars(_requete_liste(per, filtres))
                if id_effectif(e.entite_id) == per.entite_id]
    demandes = {int(i) for i in source.getlist("ids") if str(i).isdigit()}
    if not demandes:
        return []
    return list(db.session.scalars(select(Employe.id).where(
        Employe.id.in_(demandes), per.filtre_employes(), cond_entite(Employe.entite_id, per.entite_id))))


def _proteges(ids) -> tuple[list[int], list[str]]:
    """Retire la propre fiche de l'utilisateur et celles liées à un compte superadmin."""
    from flask_login import current_user
    gardes, exclus = [], []
    for e in db.session.scalars(select(Employe).where(Employe.id.in_(ids or [-1]))):
        if e.id == current_user.employe_id:
            exclus.append(f"{e.nom_complet} (votre propre fiche)")
        elif e.compte and e.compte.est_superadmin:
            exclus.append(f"{e.nom_complet} (compte superadmin)")
        else:
            gardes.append(e.id)
    return gardes, exclus


def _page_suppression(per, ids):
    ids, exclus = _proteges(ids)
    employes = db.session.scalars(select(Employe).where(Employe.id.in_(ids or [-1]))
                                  .order_by(Employe.nom, Employe.prenom)).all()
    if not employes:
        flash("Aucune fiche à supprimer dans la sélection." + (" Exclues : " + ", ".join(exclus) if exclus else ""), "info")
        return redirect(url_for("personnel.liste"))
    from ..services.suppression import impacts
    return render_template("personnel/suppression.html", employes=employes, impacts=impacts(ids), exclus=exclus)


@bp.post("/selection")
@login_required
@rh_requis
def selection():
    per = perimetre()
    action = request.form.get("action")
    ids = _ids_selectionnes(per, request.form)
    retour = url_for("personnel.liste") + ("?" + request.form.get("filtres") if request.form.get("filtres") else "")
    if not ids:
        flash("Sélectionnez au moins un salarié.", "info")
        return redirect(retour)
    if action == "supprimer":
        return _page_suppression(per, ids)
    if action == "changer_projet":
        return _page_changer_projet(db.session.scalars(
            select(Employe).where(Employe.id.in_(ids)).order_by(Employe.nom, Employe.prenom)).all())
    if action in ("desactiver", "reactiver"):
        ids, exclus = _proteges(ids) if action == "desactiver" else (ids, [])
        actif = action == "reactiver"
        n = 0
        for e in db.session.scalars(select(Employe).where(Employe.id.in_(ids or [-1]))):
            if e.actif == actif:
                continue
            e.actif = actif
            if not actif:
                for a in e.affectations:
                    a.actif = False
                if e.compte:
                    e.compte.actif = False
            n += 1
        journaliser("employes_" + ("reactives" if actif else "desactives"), f"{n} salarié(s)", "sélection groupée")
        db.session.commit()
        msg = f"{n} salarié(s) {'réactivé(s)' if actif else 'désactivé(s) : affectations et comptes suspendus, historique conservé'}."
        if exclus:
            msg += " Non modifiés : " + ", ".join(exclus) + "."
        flash(msg, "succes")
        return redirect(retour)
    abort(400)


@bp.get("/<int:employe_id>/supprimer")
@login_required
@rh_requis
def confirmer_suppression(employe_id):
    per = perimetre()
    e = db.session.get(Employe, employe_id) or abort(404)
    if not per.voit_tout_dans(e.entite_id):
        abort(403)
    return _page_suppression(per, [e.id])


@bp.post("/supprimer")
@login_required
@rh_requis
def supprimer():
    from ..services.suppression import supprimer_employes
    per = perimetre()
    demandes = {int(i) for i in request.form.getlist("ids") if i.isdigit()}
    # Contrôle du périmètre : uniquement des fiches d'entités où l'on a tous les droits
    employes = [e for e in db.session.scalars(select(Employe).where(Employe.id.in_(demandes or {-1})))
                if per.voit_tout_dans(e.entite_id)]
    ids, _ = _proteges([e.id for e in employes])
    if (request.form.get("confirmation") or "").strip().upper() != "SUPPRIMER":
        flash("Suppression annulée : tapez SUPPRIMER pour confirmer.", "erreur")
        return _page_suppression(per, ids)
    noms = {e.id: f"{e.nom_complet} ({e.matricule})" for e in employes if e.id in ids}
    for i in ids:
        journaliser("employe_supprime", noms[i], "suppression définitive")
    n = supprimer_employes(ids)
    db.session.commit()
    flash(f"{n} fiche(s) supprimée(s) définitivement, avec leurs évaluations et leurs comptes.", "succes")
    return redirect(url_for("personnel.liste"))
