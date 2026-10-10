"""Paramètres (RH) : départements, projets, grille KPI, comptes utilisateurs."""
from flask import (Blueprint, abort, current_app, flash, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..models import (Affectation, Critere, Departement, Employe, Entite,
                      EvaluationNote, Poste, Projet, Role, Utilisateur)
from ..permissions import perimetre, rh_requis
from .auth import valider_mot_de_passe
from ..services.entites import cond_entite, id_effectif, roles_par_entite
from ..services import reglages
from ..services.audit import journaliser
from ..services.organisation import cle_texte, marquer_poste_supprime, postes_actifs
from ..services.projets import carte, definir, departement_du_projet
from ..services.comptes import (generer_acces, ids_evaluateurs, lier_ou_creer,
                                mot_de_passe_temporaire, synchroniser_comptes)
from ..utils import lire_date

bp = Blueprint("parametres", __name__, url_prefix="/parametres")


def _eid():
    return perimetre().entite_id


def _dans_entite(obj):
    """404 si l'objet n'appartient pas à l'entité de l'onglet actif."""
    if obj is not None and id_effectif(obj.entite_id) != _eid():
        abort(404)
    return obj


def _employes_actifs():
    """Responsables possibles : personnel de l'entité active + direction / RH du groupe."""
    from ..services.transverses import ids_personnel_groupe
    cond = cond_entite(Employe.entite_id, _eid())
    groupe = ids_personnel_groupe()
    if groupe:
        cond = cond | Employe.id.in_(groupe)
    return db.session.scalars(select(Employe).where(Employe.actif.is_(True), cond)
                              .order_by(Employe.nom, Employe.prenom)).all()


# --- Départements & projets -------------------------------------------------
def _gerer_contexte(model, modele_tpl, titre, avec_projet=False):
    erreurs, donnees = {}, {}
    edition = None
    if request.method == "POST":
        action = request.form.get("action", "enregistrer")
        obj_id = request.form.get("id", type=int)
        obj = _dans_entite(db.session.get(model, obj_id) if obj_id else None)
        if action == "basculer" and obj:
            obj.actif = not obj.actif
            journaliser(f"{model.__tablename__}_statut", obj.nom, "actif" if obj.actif else "inactif")
            db.session.commit()
            flash(f"« {obj.nom} » {'réactivé' if obj.actif else 'archivé'}.", "succes")
            return redirect(request.path)
        donnees = {k: (request.form.get(k) or "").strip() for k in
                   ("code", "nom", "responsable_id", "localisation", "date_debut", "date_fin", "departement_id")}
        code = donnees["code"].upper()
        if not code:
            erreurs["code"] = "Le code est obligatoire."
        elif db.session.scalar(select(model.id).where(model.code == code, model.id != (obj.id if obj else -1),
                                                      cond_entite(model.entite_id, _eid()))):
            erreurs["code"] = "Ce code existe déjà."
        if not donnees["nom"]:
            erreurs["nom"] = "Le nom est obligatoire."
        elif model is Departement and db.session.scalar(
                select(model.id).where(func.lower(model.nom) == donnees["nom"].lower(), model.id != (obj.id if obj else -1),
                                       cond_entite(model.entite_id, _eid()))):
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
            dep = donnees["departement_id"]
            if dep and not (dep.isdigit() and _dans_entite_ou_none(Departement, int(dep))):
                erreurs["departement_id"] = "Département inconnu dans cette entité."
            elif not dep and db.session.scalar(select(Departement.id).where(
                    cond_entite(Departement.entite_id, _eid()), Departement.actif.is_(True)).limit(1)):
                erreurs["departement_id"] = "Choisissez le département de rattachement du projet."
        if not erreurs:
            nouveau = obj is None
            obj = obj or model(entite_id=_eid())
            obj.code, obj.nom = code, donnees["nom"]
            obj.responsable_id = int(donnees["responsable_id"]) if donnees["responsable_id"] else None
            if avec_projet:
                obj.localisation = donnees["localisation"] or None
                obj.date_debut, obj.date_fin = dd, df
            if nouveau:
                db.session.add(obj)
            if avec_projet:
                db.session.flush()
                definir(obj.id, int(donnees["departement_id"]) if donnees["departement_id"] else None, current_user.id)
            journaliser(f"{model.__tablename__}_{'cree' if nouveau else 'modifie'}", obj.nom, code)
            db.session.commit()
            flash(f"« {obj.nom} » enregistré.", "succes")
            return redirect(request.path)
        edition = obj
    elif request.args.get("modifier", type=int):
        edition = _dans_entite(db.session.get(model, request.args.get("modifier", type=int)) or abort(404))
        donnees = {"code": edition.code, "nom": edition.nom,
                   "responsable_id": str(edition.responsable_id or ""),
                   "localisation": getattr(edition, "localisation", "") or "",
                   "date_debut": edition.date_debut.isoformat() if getattr(edition, "date_debut", None) else "",
                   "date_fin": edition.date_fin.isoformat() if getattr(edition, "date_fin", None) else "",
                   "departement_id": str(departement_du_projet(edition.id) or "") if avec_projet else ""}

    col = Affectation.departement_id if model is Departement else Affectation.projet_id
    effectifs = dict(db.session.execute(
        select(col, func.count(Affectation.id)).where(Affectation.actif.is_(True), col.isnot(None)).group_by(col)).all())
    elements = db.session.scalars(select(model).options(joinedload(model.responsable))
                                  .where(cond_entite(model.entite_id, _eid()))
                                  .order_by(model.actif.desc(), model.nom)).all()
    departements = rattachements = projets_par_dep = None
    if avec_projet:
        departements = db.session.scalars(select(Departement).where(
            cond_entite(Departement.entite_id, _eid()), Departement.actif.is_(True)).order_by(Departement.nom)).all()
        noms = dict(db.session.execute(select(Departement.id, Departement.nom)
                                       .where(cond_entite(Departement.entite_id, _eid()))).all())
        rattachements = {p: noms.get(d) for p, d in carte().items()}
    else:
        noms_p = dict(db.session.execute(select(Projet.id, Projet.nom)
                                         .where(cond_entite(Projet.entite_id, _eid()))).all())
        projets_par_dep = {}
        for p, d in carte().items():
            if p in noms_p:
                projets_par_dep.setdefault(d, []).append(noms_p[p])
    return render_template(modele_tpl, elements=elements, effectifs=effectifs, edition=edition,
                           donnees=donnees, erreurs=erreurs, employes=_employes_actifs(),
                           titre=titre, avec_projet=avec_projet, departements=departements,
                           rattachements=rattachements, projets_par_dep=projets_par_dep)


def _dans_entite_ou_none(model, obj_id):
    obj = db.session.get(model, obj_id)
    if obj is None:
        return None
    return obj if db.session.scalar(select(model.id).where(model.id == obj_id, cond_entite(model.entite_id, _eid()))) else None


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
        c = _dans_entite(db.session.get(Critere, request.form.get("id", type=int) or 0))
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
                                                        Critere.id != (c.id if c else -1),
                                                        cond_entite(Critere.entite_id, _eid()))):
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
            c = c or Critere(entite_id=_eid())
            c.libelle, c.description, c.poids, c.ordre = donnees["libelle"], donnees["description"] or None, poids, ordre
            if nouveau:
                db.session.add(c)
            journaliser("critere_" + ("cree" if nouveau else "modifie"), c.libelle, f"poids={poids}")
            db.session.commit()
            flash("Critère enregistré. Le nouveau poids s'applique aux évaluations à venir.", "succes")
            return redirect(request.path)
        edition = c
    elif request.args.get("modifier", type=int):
        edition = _dans_entite(db.session.get(Critere, request.args.get("modifier", type=int)) or abort(404))
        donnees = {"libelle": edition.libelle, "description": edition.description or "",
                   "poids": str(edition.poids), "ordre": str(edition.ordre)}
    elements = db.session.scalars(select(Critere).where(cond_entite(Critere.entite_id, _eid()))
                                  .order_by(Critere.actif.desc(), Critere.ordre, Critere.id)).all()
    utilises = set(db.session.scalars(select(EvaluationNote.critere_id).distinct()))
    total_poids = sum(c.poids for c in elements if c.actif)
    return render_template("parametres/criteres.html", elements=elements, edition=edition, donnees=donnees,
                           erreurs=erreurs, utilises=utilises, total_poids=total_poids)


# --- Postes -------------------------------------------------------------------
@bp.route("/postes", methods=["GET", "POST"])
@login_required
@rh_requis
def postes():
    """Liste des postes proposés. Une ligne n'est écrite en base que lorsque la RH agit dessus."""
    erreurs, donnees, edition = {}, {}, None

    eid = _eid()

    def ligne(libelle):
        return db.session.scalar(select(Poste).where(func.lower(Poste.libelle) == libelle.lower(),
                                                     cond_entite(Poste.entite_id, eid)))

    if request.method == "POST":
        action = request.form.get("action", "enregistrer")
        if action == "groupe":
            from ..services.transverses import basculer_poste
            lib = (request.form.get("libelle") or "").strip()
            if lib:
                etat = basculer_poste(lib, current_user.id)
                journaliser("poste_groupe", lib, "visible dans tout le groupe" if etat else "entité seulement")
                db.session.commit()
                flash(f"« {lib} » : {'visible dans toutes les entités' if etat else 'visible dans son entité seulement'}.",
                      "succes")
            return redirect(request.path)
        if action == "basculer":
            lib = (request.form.get("libelle") or "").strip()
            p = ligne(lib)
            if p:
                p.actif = not p.actif
            elif lib:
                p = Poste(libelle=lib, actif=False, entite_id=eid)
                db.session.add(p)
            if p:
                journaliser("poste_statut", p.libelle, "actif" if p.actif else "archivé")
                db.session.commit()
                flash(f"Poste « {p.libelle} » {'réactivé' if p.actif else 'archivé (retiré des listes)'}.", "succes")
            return redirect(request.path)
        if action == "supprimer":
            lib = (request.form.get("libelle") or "").strip()
            if not lib:
                return redirect(request.path)
            k = cle_texte(lib)
            remplacement = (request.form.get("remplacement") or "").strip()
            if remplacement and (cle_texte(remplacement) == k or
                                 cle_texte(remplacement) not in {cle_texte(x) for x in postes_actifs(eid)}):
                flash("Poste de remplacement invalide.", "erreur")
                return redirect(request.path)
            # Fiches qui portent ce poste (actives ou non) : remplacé ou vidé, à la demande de la RH
            n = 0
            for e in db.session.scalars(select(Employe).where(Employe.poste.isnot(None),
                                                              cond_entite(Employe.entite_id, eid))):
                if cle_texte(e.poste) == k:
                    e.poste = remplacement or None
                    n += 1
            for p in db.session.scalars(select(Poste).where(cond_entite(Poste.entite_id, eid))):
                if cle_texte(p.libelle) == k:
                    db.session.delete(p)
            marquer_poste_supprime(eid, lib, True, current_user.id)
            journaliser("poste_supprime", lib, f"{n} fiche(s) → {remplacement or 'sans poste'}")
            db.session.commit()
            msg = f"Poste « {lib} » supprimé."
            if n:
                msg += f" {n} fiche(s) : poste {'remplacé par « ' + remplacement + ' »' if remplacement else 'laissé vide'}."
            flash(msg, "succes")
            return redirect(request.path)
        ancien = (request.form.get("ancien") or "").strip()
        libelle = " ".join((request.form.get("libelle") or "").split())[:120]
        donnees = {"libelle": libelle, "ancien": ancien}
        if not libelle:
            erreurs["libelle"] = "Le libellé est obligatoire."
        elif libelle.lower() != ancien.lower() and (
                ligne(libelle) or cle_texte(libelle) in {cle_texte(x) for x in postes_actifs(eid)}):
            erreurs["libelle"] = "Ce poste existe déjà."
        if not erreurs:
            if ancien:
                p = ligne(ancien)
                if p:
                    p.libelle = libelle
                else:  # poste de la liste par défaut : on le masque et on crée le nouveau libellé
                    db.session.add(Poste(libelle=ancien, actif=False, entite_id=eid))
                    db.session.add(Poste(libelle=libelle, entite_id=eid))
                n = 0
                # Renommage demandé par la RH : répercuté sur les fiches qui portent ce poste
                for e in db.session.scalars(select(Employe).where(Employe.poste == ancien,
                                                                  cond_entite(Employe.entite_id, eid))):
                    e.poste = libelle
                    n += 1
                journaliser("poste_renomme", libelle, f"ancien : {ancien}, {n} fiche(s)")
            else:
                db.session.add(Poste(libelle=libelle, entite_id=eid))
                marquer_poste_supprime(eid, libelle, False, current_user.id)  # recréé après suppression
                journaliser("poste_cree", libelle)
            db.session.commit()
            flash(f"Poste « {libelle} » enregistré.", "succes")
            return redirect(request.path)
        edition = ancien or None
    elif request.args.get("modifier"):
        edition = request.args.get("modifier")
        donnees = {"libelle": edition, "ancien": edition}
    effectifs = dict(db.session.execute(select(Employe.poste, func.count(Employe.id))
                                        .where(Employe.actif.is_(True), Employe.poste.isnot(None),
                                               cond_entite(Employe.entite_id, eid))
                                        .group_by(Employe.poste)).all())
    # Toutes les fiches (actives ou non) par poste : ce qu'une suppression touchera
    fiches = {}
    for lib, nb in db.session.execute(select(Employe.poste, func.count(Employe.id)).where(
            Employe.poste.isnot(None), cond_entite(Employe.entite_id, eid)).group_by(Employe.poste)):
        fiches[cle_texte(lib)] = fiches.get(cle_texte(lib), 0) + nb
    actifs = postes_actifs(eid)
    archives = db.session.scalars(select(Poste.libelle).where(Poste.actif.is_(False), cond_entite(Poste.entite_id, eid))
                                  .order_by(Poste.libelle)).all()
    from ..services.transverses import _reglage, poste_est_groupe
    reglage = _reglage()
    elements = [{"libelle": x, "actif": True, "effectif": effectifs.get(x, 0), "groupe": poste_est_groupe(x, reglage),
                 "fiches": fiches.get(cle_texte(x), 0)} for x in actifs] + \
               [{"libelle": x, "actif": False, "effectif": effectifs.get(x, 0), "groupe": False,
                 "fiches": fiches.get(cle_texte(x), 0)} for x in archives]
    return render_template("parametres/postes.html", elements=elements, edition=edition,
                           donnees=donnees, erreurs=erreurs, actifs=actifs)


@bp.get("/postes/supprimer")
@login_required
@rh_requis
def supprimer_poste():
    """Confirmation de suppression d'un poste porté par des fiches : choix du poste de remplacement."""
    eid = _eid()
    lib = (request.args.get("libelle") or "").strip() or abort(404)
    k = cle_texte(lib)
    fiches = [e for e in db.session.scalars(select(Employe).where(
        Employe.poste.isnot(None), cond_entite(Employe.entite_id, eid)).order_by(Employe.nom, Employe.prenom))
        if cle_texte(e.poste) == k]
    autres = [x for x in postes_actifs(eid) if cle_texte(x) != k]
    return render_template("parametres/supprimer_poste.html", libelle=lib, fiches=fiches, autres=autres)


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
            mdp_impose = (request.form.get("mot_de_passe") or "").strip()
            if mdp_impose and not current_user.est_superadmin:
                abort(403)
            if mdp_impose and (m := valider_mot_de_passe(mdp_impose)):
                erreurs["mot_de_passe"] = m
            e = db.session.get(Employe, int(donnees["employe_id"])) if donnees["employe_id"].isdigit() else None
            if donnees["role"] not in Role.TOUS or (donnees["role"] == Role.RH and not current_user.est_superadmin):
                erreurs["role"] = "Rôle invalide (seul un superadmin peut nommer un compte RH)."
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
                    nouveau = u.en_attente_acces
                    if nouveau:
                        u.role = donnees["role"]
                        # Seul le superadmin attribue un mot de passe ; sinon le compte attend
                        if current_user.est_superadmin:
                            identifiants = generer_acces([u], {u.id: mdp_impose} if mdp_impose else None)
                    journaliser("compte_cree", u.email, Role.LIBELLES[u.role])
                    db.session.commit()
                    if not identifiants:
                        flash(f"Compte {u.email} créé : le superadmin doit lui attribuer un mot de passe." if nouveau
                              else f"Compte existant {u.email} lié à la fiche de {e.nom_complet}.", "succes")
                        return redirect(request.path)
                    donnees = {}
        elif action in ("generer_attente", "reinitialiser", "definir_mdp") and not current_user.est_superadmin:
            abort(403)  # attribution des mots de passe : superadmin uniquement
        elif action == "generer_attente":
            attente = [u for u in db.session.scalars(select(Utilisateur).where(
                Utilisateur.actif.is_(True), Utilisateur.acces_en_attente.is_(True),
                Utilisateur.id != current_user.id))]
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
                if (Role.RH in (role, u.role)) and not current_user.est_superadmin:
                    flash("Seul un superadmin peut attribuer ou retirer le rôle RH.", "erreur")
                elif role in Role.TOUS:
                    u.role = role
                    journaliser("compte_role", u.email, Role.LIBELLES[role])
                    flash(f"Rôle de {u.email} : {Role.LIBELLES[role]}.", "succes")
            elif action == "reinitialiser":
                identifiants = generer_acces([u])
                journaliser("compte_mdp_attribue", u.email, "généré")
            elif action == "definir_mdp":
                mdp = (request.form.get("mot_de_passe") or "").strip()
                if (m := valider_mot_de_passe(mdp)):
                    flash(f"{u.email} : {m}", "erreur")
                    return redirect(request.path)
                identifiants = generer_acces([u], {u.id: mdp})
                journaliser("compte_mdp_attribue", u.email, "choisi par le superadmin")
            db.session.commit()
            if not identifiants:
                return redirect(request.path)

    comptes = db.session.scalars(select(Utilisateur).options(joinedload(Utilisateur.employe))
                                 .order_by(Utilisateur.actif.desc(), Utilisateur.role, Utilisateur.email)).all()
    responsables = ids_evaluateurs()
    resp_sans_email = db.session.scalars(
        select(Employe).where(Employe.id.in_(responsables), Employe.actif.is_(True),
                              (Employe.email.is_(None)) | (Employe.email == ""))
        .order_by(Employe.nom)).all()
    sans_compte = db.session.scalars(
        select(Employe).where(Employe.actif.is_(True), Employe.email.isnot(None), Employe.email != "",
                              cond_entite(Employe.entite_id, _eid()),
                              ~Employe.id.in_(select(Utilisateur.employe_id).where(Utilisateur.employe_id.isnot(None))))
        .order_by(Employe.nom)).all()
    nb_attente = sum(1 for u in comptes if u.en_attente_acces and u.id != current_user.id)
    # Entités visibles par chaque compte (rôle par entité)
    noms = {e.id: e for e in db.session.scalars(select(Entite))}
    entites_compte = {u.id: [(noms[i], r) for i, r in sorted(roles_par_entite(u).items(), key=lambda x: noms[x[0]].nom)]
                      for u in comptes if not u.est_rh}
    return render_template("parametres/utilisateurs.html", comptes=comptes, sans_compte=sans_compte,
                           roles=Role.LIBELLES, erreurs=erreurs, donnees=donnees, identifiants=identifiants,
                           resp_sans_email=resp_sans_email, nb_attente=nb_attente, responsables=responsables,
                           entites_compte=entites_compte, nb_entites=len(noms))
