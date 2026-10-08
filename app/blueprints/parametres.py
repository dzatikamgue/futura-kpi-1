"""Paramètres (RH) : départements, projets, grille KPI, comptes utilisateurs."""
from flask import (Blueprint, abort, current_app, flash, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..models import (Affectation, Critere, Departement, Employe,
                      EvaluationNote, Poste, Projet, Role, Utilisateur)
from ..permissions import rh_requis
from ..services import reglages
from ..services.audit import journaliser
from ..services.comptes import (generer_acces, ids_responsables, lier_ou_creer,
                                mot_de_passe_temporaire, synchroniser_comptes)
from ..services.organisation import completer_n1_par_responsable
from ..utils import lire_date

bp = Blueprint("parametres", __name__, url_prefix="/parametres")


def _employes_actifs():
    return db.session.scalars(select(Employe).where(Employe.actif.is_(True)).order_by(Employe.nom, Employe.prenom)).all()


# --- Départements & projets -------------------------------------------------
def _gerer_contexte(model, modele_tpl, titre, avec_projet=False):
    erreurs, donnees = {}, {}
    edition = None
    if request.method == "POST":
        action = request.form.get("action", "enregistrer")
        obj_id = request.form.get("id", type=int)
        obj = db.session.get(model, obj_id) if obj_id else None
        if action == "basculer" and obj:
            obj.actif = not obj.actif
            journaliser(f"{model.__tablename__}_statut", obj.nom, "actif" if obj.actif else "inactif")
            db.session.commit()
            flash(f"« {obj.nom} » {'réactivé' if obj.actif else 'archivé'}.", "succes")
            return redirect(request.path)
        donnees = {k: (request.form.get(k) or "").strip() for k in
                   ("code", "nom", "responsable_id", "localisation", "date_debut", "date_fin")}
        code = donnees["code"].upper()
        if not code:
            erreurs["code"] = "Le code est obligatoire."
        elif db.session.scalar(select(model.id).where(model.code == code, model.id != (obj.id if obj else -1))):
            erreurs["code"] = "Ce code existe déjà."
        if not donnees["nom"]:
            erreurs["nom"] = "Le nom est obligatoire."
        elif model is Departement and db.session.scalar(
                select(model.id).where(func.lower(model.nom) == donnees["nom"].lower(), model.id != (obj.id if obj else -1))):
            erreurs["nom"] = "Ce département existe déjà."
        dd = df = None
        if avec_projet:
            try:
                dd = lire_date(donnees["date_debut"])
            except ValueError as exc:
                erreurs["date_debut"] = str(exc)
            try:
                df = lire_date(donnees["date_fin"])
            except ValueError as exc:
                erreurs["date_fin"] = str(exc)
            if dd and df and df < dd:
                erreurs["date_fin"] = "La date de fin doit suivre la date de début."
        if not erreurs:
            nouveau = obj is None
            obj = obj or model()
            obj.code, obj.nom = code, donnees["nom"]
            obj.responsable_id = int(donnees["responsable_id"]) if donnees["responsable_id"] else None
            if avec_projet:
                obj.localisation = donnees["localisation"] or None
                obj.date_debut, obj.date_fin = dd, df
            if nouveau:
                db.session.add(obj)
            db.session.flush()
            completes = completer_n1_par_responsable(obj)
            journaliser(f"{model.__tablename__}_{'cree' if nouveau else 'modifie'}", obj.nom, code)
            synchroniser_comptes()
            db.session.commit()
            msg = f"« {obj.nom} » enregistré."
            if completes:
                msg += f" {completes} salarié(s) sans N+1 seront notés par {obj.responsable.nom_complet}."
            flash(msg, "succes")
            return redirect(request.path)
        edition = obj
    elif request.args.get("modifier", type=int):
        edition = db.session.get(model, request.args.get("modifier", type=int)) or abort(404)
        donnees = {"code": edition.code, "nom": edition.nom,
                   "responsable_id": str(edition.responsable_id or ""),
                   "localisation": getattr(edition, "localisation", "") or "",
                   "date_debut": edition.date_debut.isoformat() if getattr(edition, "date_debut", None) else "",
                   "date_fin": edition.date_fin.isoformat() if getattr(edition, "date_fin", None) else ""}

    col = Affectation.departement_id if model is Departement else Affectation.projet_id
    effectifs = dict(db.session.execute(
        select(col, func.count(Affectation.id)).where(Affectation.actif.is_(True), col.isnot(None)).group_by(col)).all())
    elements = db.session.scalars(select(model).options(joinedload(model.responsable))
                                  .order_by(model.actif.desc(), model.nom)).all()
    return render_template(modele_tpl, elements=elements, effectifs=effectifs, edition=edition,
                           donnees=donnees, erreurs=erreurs, employes=_employes_actifs(),
                           titre=titre, avec_projet=avec_projet)


@bp.route("/departements", methods=["GET", "POST"])
@login_required
@rh_requis
def departements():
    return _gerer_contexte(Departement, "parametres/contextes.html", "Départements")


@bp.route("/projets", methods=["GET", "POST"])
@login_required
@rh_requis
def projets():
    return _gerer_contexte(Projet, "parametres/contextes.html", "Projets", avec_projet=True)


# --- Grille KPI --------------------------------------------------------------
@bp.route("/criteres", methods=["GET", "POST"])
@login_required
@rh_requis
def criteres():
    erreurs, donnees, edition = {}, {}, None
    if request.method == "POST":
        action = request.form.get("action", "enregistrer")
        c = db.session.get(Critere, request.form.get("id", type=int) or 0)
        if action == "basculer" and c:
            c.actif = not c.actif
            journaliser("critere_statut", c.libelle, "actif" if c.actif else "inactif")
            db.session.commit()
            flash(f"Critère « {c.libelle} » {'activé' if c.actif else 'désactivé'}. "
                  "Les évaluations déjà soumises ne changent pas.", "succes")
            return redirect(request.path)
        donnees = {k: (request.form.get(k) or "").strip() for k in ("libelle", "description", "poids", "ordre")}
        if not donnees["libelle"]:
            erreurs["libelle"] = "Le libellé est obligatoire."
        elif db.session.scalar(select(Critere.id).where(func.lower(Critere.libelle) == donnees["libelle"].lower(),
                                                        Critere.id != (c.id if c else -1))):
            erreurs["libelle"] = "Ce critère existe déjà."
        try:
            poids = int(donnees["poids"] or 1)
            if not 1 <= poids <= 5:
                raise ValueError
        except ValueError:
            erreurs["poids"] = "Le poids doit être un entier de 1 à 5."
            poids = 1
        try:
            ordre = int(donnees["ordre"] or 0)
        except ValueError:
            erreurs["ordre"] = "Ordre invalide."
            ordre = 0
        if not erreurs:
            nouveau = c is None
            c = c or Critere()
            c.libelle, c.description, c.poids, c.ordre = donnees["libelle"], donnees["description"] or None, poids, ordre
            if nouveau:
                db.session.add(c)
            journaliser("critere_" + ("cree" if nouveau else "modifie"), c.libelle, f"poids={poids}")
            db.session.commit()
            flash("Critère enregistré. Le nouveau poids s'applique aux évaluations à venir.", "succes")
            return redirect(request.path)
        edition = c
    elif request.args.get("modifier", type=int):
        edition = db.session.get(Critere, request.args.get("modifier", type=int)) or abort(404)
        donnees = {"libelle": edition.libelle, "description": edition.description or "",
                   "poids": str(edition.poids), "ordre": str(edition.ordre)}
    elements = db.session.scalars(select(Critere).order_by(Critere.actif.desc(), Critere.ordre, Critere.id)).all()
    utilises = set(db.session.scalars(select(EvaluationNote.critere_id).distinct()))
    total_poids = sum(c.poids for c in elements if c.actif)
    return render_template("parametres/criteres.html", elements=elements, edition=edition, donnees=donnees,
                           erreurs=erreurs, utilises=utilises, total_poids=total_poids)


# --- Postes -------------------------------------------------------------------
@bp.route("/postes", methods=["GET", "POST"])
@login_required
@rh_requis
def postes():
    erreurs, donnees, edition = {}, {}, None
    if request.method == "POST":
        action = request.form.get("action", "enregistrer")
        p = db.session.get(Poste, request.form.get("id", type=int) or 0)
        if action == "basculer" and p:
            p.actif = not p.actif
            journaliser("poste_statut", p.libelle, "actif" if p.actif else "archivé")
            db.session.commit()
            flash(f"Poste « {p.libelle} » {'réactivé' if p.actif else 'archivé (retiré des listes)'}.", "succes")
            return redirect(request.path)
        libelle = " ".join((request.form.get("libelle") or "").split())[:120]
        donnees = {"libelle": libelle}
        if not libelle:
            erreurs["libelle"] = "Le libellé est obligatoire."
        elif db.session.scalar(select(Poste.id).where(func.lower(Poste.libelle) == libelle.lower(),
                                                      Poste.id != (p.id if p else -1))):
            erreurs["libelle"] = "Ce poste existe déjà."
        if not erreurs:
            if p:
                ancien = p.libelle
                # Renommage répercuté sur les fiches
                for e in db.session.scalars(select(Employe).where(Employe.poste == ancien)):
                    e.poste = libelle
                p.libelle = libelle
                journaliser("poste_modifie", libelle, f"ancien : {ancien}")
            else:
                db.session.add(Poste(libelle=libelle))
                journaliser("poste_cree", libelle)
            db.session.commit()
            flash(f"Poste « {libelle} » enregistré.", "succes")
            return redirect(request.path)
        edition = p
    elif request.args.get("modifier", type=int):
        edition = db.session.get(Poste, request.args.get("modifier", type=int)) or abort(404)
        donnees = {"libelle": edition.libelle}
    elements = db.session.scalars(select(Poste).order_by(Poste.actif.desc(), Poste.libelle)).all()
    effectifs = dict(db.session.execute(select(Employe.poste, func.count(Employe.id))
                                        .where(Employe.actif.is_(True), Employe.poste.isnot(None))
                                        .group_by(Employe.poste)).all())
    return render_template("parametres/postes.html", elements=elements, effectifs=effectifs,
                           edition=edition, donnees=donnees, erreurs=erreurs)


# --- Clé API Claude -----------------------------------------------------------
@bp.route("/claude", methods=["GET", "POST"])
@login_required
@rh_requis
def claude():
    erreurs, resultat_test = {}, None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "enregistrer":
            cle = "".join((request.form.get("cle") or "").split())
            if not cle:
                erreurs["cle"] = "Collez la clé API."
            elif not cle.startswith("sk-ant-") or len(cle) < 30:
                erreurs["cle"] = "Ce n'est pas une clé Claude : elle commence par « sk-ant- » (console.anthropic.com → API Keys)."
            if not erreurs:
                ok, message = reglages.tester_cle_api(cle)
                if not ok and request.form.get("forcer") != "1":
                    erreurs["cle"] = message + " La clé n'a pas été enregistrée."
                else:
                    reglages.enregistrer_cle_api(cle, current_user.id)
                    journaliser("cle_api_enregistree", reglages.masquer(cle), "testée OK" if ok else "non vérifiée")
                    db.session.commit()
                    flash("Clé API Claude enregistrée (chiffrée). L'import par photo, PDF ou texte est activé." if ok
                          else "Clé enregistrée sans vérification : " + message, "succes" if ok else "info")
                    return redirect(request.path)
        elif action == "tester":
            cle = reglages.cle_api()
            resultat_test = reglages.tester_cle_api(cle) if cle else (False, "Aucune clé configurée.")
        elif action == "supprimer":
            reglages.supprimer_cle_api()
            journaliser("cle_api_supprimee", "")
            db.session.commit()
            flash("Clé API supprimée de l'application.", "succes")
            return redirect(request.path)
    return render_template("parametres/claude.html", etat=reglages.etat_cle_api(), erreurs=erreurs,
                           resultat_test=resultat_test, modele=current_app.config["ANTHROPIC_MODEL"])


# --- Comptes utilisateurs ------------------------------------------------------
@bp.route("/utilisateurs", methods=["GET", "POST"])
@login_required
@rh_requis
def utilisateurs():
    erreurs, donnees, identifiants = {}, {}, []
    if request.method == "POST":
        action = request.form.get("action")
        if action == "creer":
            # Le compte prend l'e-mail de la fiche : rien d'autre à saisir que le rôle
            donnees = {k: (request.form.get(k) or "").strip() for k in ("role", "employe_id")}
            e = db.session.get(Employe, int(donnees["employe_id"])) if donnees["employe_id"].isdigit() else None
            if donnees["role"] not in Role.TOUS:
                erreurs["role"] = "Rôle invalide."
            if not e:
                erreurs["employe_id"] = "Choisissez un salarié."
            elif e.compte:
                erreurs["employe_id"] = "Ce salarié a déjà un compte."
            elif not e.email:
                erreurs["employe_id"] = "Ce salarié n'a pas d'e-mail : complétez d'abord sa fiche."
            if not erreurs:
                u = lier_ou_creer(e, creer=True)
                if not u:
                    erreurs["employe_id"] = f"L'e-mail {e.email} est déjà utilisé par un autre compte."
                else:
                    db.session.flush()
                    identifiants = []
                    if u.en_attente_acces:
                        u.role = donnees["role"]
                        identifiants = generer_acces([u])
                    journaliser("compte_cree", u.email, Role.LIBELLES[u.role])
                    db.session.commit()
                    if not identifiants:
                        flash(f"Compte existant {u.email} lié à la fiche de {e.nom_complet}.", "succes")
                        return redirect(request.path)
                    donnees = {}
        elif action == "generer_attente":
            synchroniser_comptes()
            attente = [u for u in db.session.scalars(select(Utilisateur).where(
                Utilisateur.actif.is_(True), Utilisateur.acces_remis_le.is_(None),
                Utilisateur.derniere_connexion.is_(None), Utilisateur.id != current_user.id))]
            identifiants = generer_acces(attente)
            journaliser("acces_generes", f"{len(identifiants)} comptes")
            db.session.commit()
            if not identifiants:
                flash("Aucun accès en attente.", "info")
                return redirect(request.path)
        elif action == "synchroniser":
            stats = synchroniser_comptes()
            db.session.commit()
            flash(f"{stats['crees']} compte(s) créé(s), {stats['lies']} compte(s) lié(s) à une fiche.", "succes")
            return redirect(request.path)
        else:
            u = db.session.get(Utilisateur, request.form.get("id", type=int) or 0) or abort(404)
            if u.id == current_user.id and action in ("basculer", "role"):
                flash("Vous ne pouvez pas modifier votre propre statut ou rôle.", "erreur")
                return redirect(request.path)
            if action == "basculer":
                u.actif = not u.actif
                journaliser("compte_" + ("active" if u.actif else "desactive"), u.email)
                flash(f"Compte {u.email} {'activé' if u.actif else 'désactivé'}.", "succes")
            elif action == "role":
                role = request.form.get("role")
                if role in Role.TOUS:
                    u.role = role
                    journaliser("compte_role", u.email, Role.LIBELLES[role])
                    flash(f"Rôle de {u.email} : {Role.LIBELLES[role]}.", "succes")
            elif action == "reinitialiser":
                identifiants = generer_acces([u])
                journaliser("compte_mdp_reinitialise", u.email)
            db.session.commit()
            if not identifiants:
                return redirect(request.path)

    comptes = db.session.scalars(select(Utilisateur).options(joinedload(Utilisateur.employe))
                                 .order_by(Utilisateur.actif.desc(), Utilisateur.role, Utilisateur.email)).all()
    responsables = ids_responsables()
    resp_sans_email = db.session.scalars(
        select(Employe).where(Employe.id.in_(responsables), Employe.actif.is_(True),
                              (Employe.email.is_(None)) | (Employe.email == ""))
        .order_by(Employe.nom)).all()
    sans_compte = db.session.scalars(
        select(Employe).where(Employe.actif.is_(True), Employe.email.isnot(None), Employe.email != "",
                              ~Employe.id.in_(select(Utilisateur.employe_id).where(Utilisateur.employe_id.isnot(None))))
        .order_by(Employe.nom)).all()
    nb_attente = sum(1 for u in comptes if u.en_attente_acces and u.id != current_user.id)
    return render_template("parametres/utilisateurs.html", comptes=comptes, sans_compte=sans_compte,
                           roles=Role.LIBELLES, erreurs=erreurs, donnees=donnees, identifiants=identifiants,
                           resp_sans_email=resp_sans_email, nb_attente=nb_attente, responsables=responsables)
