"""Paramètres (RH) : départements, projets, grille KPI, comptes utilisateurs."""
from flask import (Blueprint, abort, current_app, flash, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required
from sqlalchemy import func, or_, select
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
from ..services.hierarchie import parent_de
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
    """Responsables possibles : tout le personnel actif du groupe (un salarié peut diriger et
    noter dans plusieurs entités). Le personnel de l'entité active vient en premier."""
    from ..services.entites import id_effectif
    eid = _eid()
    tous = db.session.scalars(select(Employe).where(Employe.actif.is_(True))
                              .order_by(Employe.nom, Employe.prenom)).all()
    return sorted(tous, key=lambda e: id_effectif(e.entite_id) != eid)


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
                   ("code", "nom", "responsable_id", "localisation", "date_debut", "date_fin", "departement_id",
                    "parent_id")}
        code = donnees["code"].upper()
        if model is Departement and obj is None and code:
            from ..services.hierarchie import restaurer, supprimes as _deps_supprimes
            cache = db.session.scalar(select(Departement).where(
                or_(Departement.code == code, func.lower(Departement.nom) == donnees["nom"].lower()),
                Departement.id.in_(_deps_supprimes() or {-1}), cond_entite(Departement.entite_id, _eid())))
            if cache:                    # recréé après suppression : même ligne, historique intact
                restaurer(cache, current_user.id)
                obj = cache
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
        if model is Departement and donnees["parent_id"]:
            from ..services.hierarchie import sous_departements
            par = donnees["parent_id"]
            if not (par.isdigit() and _dans_entite_ou_none(Departement, int(par))):
                erreurs["parent_id"] = "Département parent inconnu dans cette entité."
            elif obj and (int(par) == obj.id or int(par) in sous_departements(obj.id)):
                erreurs["parent_id"] = "Un département ne peut pas dépendre de lui-même ni de l'un de ses sous-départements."
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
            else:
                from ..services.hierarchie import definir_parent
                from ..services.organisation import synchroniser_n1_departements
                db.session.flush()
                definir_parent(obj.id, int(donnees["parent_id"]) if donnees["parent_id"] else None, current_user.id)
                # Le responsable devient le N+1 de tous les membres du département
                synchroniser_comptes(synchroniser_n1_departements())
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
                   "departement_id": str(departement_du_projet(edition.id) or "") if avec_projet else "",
                   "parent_id": "" if avec_projet else str(parent_de(edition.id) or "")}

    col = Affectation.departement_id if model is Departement else Affectation.projet_id
    effectifs = dict(db.session.execute(
        select(col, func.count(Affectation.id)).where(Affectation.actif.is_(True), col.isnot(None)).group_by(col)).all())
    elements = db.session.scalars(select(model).options(joinedload(model.responsable))
                                  .where(cond_entite(model.entite_id, _eid()))
                                  .order_by(model.actif.desc(), model.nom)).all()
    if model is Departement:
        from ..services.hierarchie import supprimes as _deps_supprimes
        caches = _deps_supprimes()
        elements = [x for x in elements if x.id not in caches]
    departements = rattachements = projets_par_dep = None
    if avec_projet:
        from ..services.hierarchie import ordonner
        departements = ordonner(db.session.scalars(select(Departement).where(
            cond_entite(Departement.entite_id, _eid()), Departement.actif.is_(True)).order_by(Departement.nom)).all())
        noms = dict(db.session.execute(select(Departement.id, Departement.nom)
                                       .where(cond_entite(Departement.entite_id, _eid()))).all())
        rattachements = {p: noms.get(d) for p, d in carte().items()}
        niveaux, parents_possibles, noms_dep = {}, [], {}
    else:
        from ..services.hierarchie import ordonner, sous_departements
        arbre = ordonner(elements)
        elements = [d for d, _ in arbre]
        niveaux = {d.id: n for d, n in arbre}
        noms_dep = {d.id: d.nom for d in elements}
        exclus = ({edition.id} | sous_departements(edition.id)) if edition is not None and edition.id else set()
        parents_possibles = [(d, n) for d, n in arbre if d.actif and d.id not in exclus]
        noms_p = dict(db.session.execute(select(Projet.id, Projet.nom)
                                         .where(cond_entite(Projet.entite_id, _eid()))).all())
        projets_par_dep = {}
        for p, d in carte().items():
            if p in noms_p:
                projets_par_dep.setdefault(d, []).append(noms_p[p])
    return render_template(modele_tpl, elements=elements, effectifs=effectifs, edition=edition,
                           donnees=donnees, erreurs=erreurs, employes=_employes_actifs(),
                           titre=titre, avec_projet=avec_projet, departements=departements,
                           rattachements=rattachements, projets_par_dep=projets_par_dep,
                           niveaux=niveaux, parents_possibles=parents_possibles, noms_dep=noms_dep,
                           parent_de=parent_de)


def _dans_entite_ou_none(model, obj_id):
    obj = db.session.get(model, obj_id)
    if obj is None:
        return None
    return obj if db.session.scalar(select(model.id).where(model.id == obj_id, cond_entite(model.entite_id, _eid()))) else None


@bp.get("/organigramme")
@login_required
def organigramme():
    """Organigramme de l'entité : départements, sous-départements et projets rattachés (RH et direction)."""
    if not perimetre().voit_tout:
        abort(403)
    from ..services.hierarchie import parent_de
    eid = _eid()
    deps = db.session.scalars(select(Departement).options(joinedload(Departement.responsable))
                              .where(Departement.actif.is_(True), cond_entite(Departement.entite_id, eid))
                              .order_by(Departement.nom)).all()
    projets = db.session.scalars(select(Projet).options(joinedload(Projet.responsable))
                                 .where(Projet.actif.is_(True), cond_entite(Projet.entite_id, eid))
                                 .order_by(Projet.nom)).all()
    eff_dep = dict(db.session.execute(select(Affectation.departement_id, func.count(Affectation.id)).where(
        Affectation.actif.is_(True), Affectation.departement_id.isnot(None)).group_by(Affectation.departement_id)).all())
    eff_proj = dict(db.session.execute(select(Affectation.projet_id, func.count(Affectation.id)).where(
        Affectation.actif.is_(True), Affectation.projet_id.isnot(None)).group_by(Affectation.projet_id)).all())
    ids = {d.id for d in deps}
    c = carte()
    noeuds = {d.id: {"dep": d, "enfants": [], "projets": [p for p in projets if c.get(p.id) == d.id],
                     "effectif": eff_dep.get(d.id, 0)} for d in deps}
    racines = []
    for d in deps:
        par = parent_de(d.id)
        (noeuds[par]["enfants"] if par in ids else racines).append(noeuds[d.id])

    def total(n):  # effectif de la branche (sous-départements et projets compris)
        n["total"] = n["effectif"] + sum(eff_proj.get(p.id, 0) for p in n["projets"]) + sum(total(e) for e in n["enfants"])
        return n["total"]
    for r in racines:
        total(r)
    sans_dep = [p for p in projets if c.get(p.id) not in ids]
    return render_template("parametres/organigramme.html", racines=racines, sans_dep=sans_dep,
                           eff_proj=eff_proj, nb_deps=len(deps), nb_projets=len(projets))


@bp.route("/departements/supprimer", methods=["GET", "POST"])
@login_required
@rh_requis
def supprimer_departements():
    """Confirmation puis suppression d'un ou plusieurs départements (impacts affichés)."""
    from ..services.hierarchie import impacts_suppression, supprimer_departements as _supprimer, supprimes
    caches = supprimes()
    if request.values.get("tout") == "1":
        ids = set(db.session.scalars(select(Departement.id).where(cond_entite(Departement.entite_id, _eid()))))
    else:
        ids = {int(x) for x in request.values.getlist("ids") if str(x).isdigit()}
    deps = [d for d in db.session.scalars(select(Departement).where(
        Departement.id.in_(ids or {-1}), cond_entite(Departement.entite_id, _eid())).order_by(Departement.nom))
        if d.id not in caches]
    if not deps:
        flash("Cochez au moins un département.", "info")
        return redirect(url_for("parametres.departements"))
    if request.method == "POST":
        effaces, gardes = _supprimer(deps, current_user.id)
        from ..services.organisation import synchroniser_n1_departements
        synchroniser_n1_departements()   # responsables des sous-départements remontés
        journaliser("departements_supprimes", f"{len(deps)} département(s)",
                    ", ".join(d.nom for d in deps)[:400])
        db.session.commit()
        msg = f"{len(deps)} département(s) supprimé(s)."
        if gardes:
            msg += f" {gardes} avai(en)t un historique de notes : conservé(s) uniquement pour l'historique."
        flash(msg, "succes")
        return redirect(url_for("parametres.departements"))
    return render_template("parametres/supprimer_departements.html", deps=deps, impacts=impacts_suppression(deps))


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
def __coef(c):
    from ..services.grilles import coefficient
    return coefficient(c)


@bp.route("/criteres", methods=["GET", "POST"])
@login_required
@rh_requis
def criteres():
    erreurs, donnees, edition = {}, {}, None
    if request.method == "POST":
        action = request.form.get("action", "enregistrer")
        c = _dans_entite(db.session.get(Critere, request.form.get("id", type=int) or 0))
        if action in ("supprimer", "supprimer_masse"):
            from ..services.grilles import supprimer_criteres
            if request.form.get("tout") == "1":
                ids = set(db.session.scalars(select(Critere.id).where(cond_entite(Critere.entite_id, _eid()))))
            elif action == "supprimer":
                ids = {c.id} if c else set()
            else:
                ids = {int(x) for x in request.form.getlist("ids") if x.isdigit()}
            if not ids:
                flash("Cochez au moins un critère.", "info")
                return redirect(request.path)
            definitifs, caches = supprimer_criteres(_eid(), ids, current_user.id)
            journaliser("criteres_supprimes", f"{definitifs + caches} critère(s)",
                        f"{definitifs} effacé(s), {caches} gardé(s) pour l'historique")
            db.session.commit()
            msg = f"{definitifs + caches} critère(s) supprimé(s)."
            if caches:
                msg += (f" {caches} déjà utilisé(s) dans des évaluations : retiré(s) des grilles, "
                        "conservé(s) uniquement dans l'historique des notes.")
            flash(msg, "succes")
            return redirect(request.path)
        if action == "basculer" and c:
            c.actif = not c.actif
            journaliser("critere_statut", c.libelle, "actif" if c.actif else "inactif")
            db.session.commit()
            flash(f"Critère « {c.libelle} » {'activé' if c.actif else 'désactivé'}. "
                  "Les évaluations déjà soumises ne changent pas.", "succes")
            return redirect(request.path)
        donnees = {k: (request.form.get(k) or "").strip() for k in ("libelle", "description", "poids", "ordre")}
        from ..services.grilles import masques as _masques, reprendre_masque
        homonyme = db.session.scalar(select(Critere).where(func.lower(Critere.libelle) == donnees["libelle"].lower(),
                                                           Critere.id != (c.id if c else -1),
                                                           cond_entite(Critere.entite_id, _eid())))
        if not donnees["libelle"]:
            erreurs["libelle"] = "Le libellé est obligatoire."
        elif homonyme and c is None and homonyme.id in _masques(_eid()):
            reprendre_masque(_eid(), homonyme, current_user.id)   # recréé après suppression
            c = homonyme
        elif homonyme:
            erreurs["libelle"] = "Ce critère existe déjà."
        from ..services.grilles import definir_coefficient, lire_coefficient
        poids = lire_coefficient(donnees["poids"])
        if poids is None:
            erreurs["poids"] = "Le coefficient doit être un entier de 1 à 100."
            poids = 1
        try:
            ordre = int(donnees["ordre"] or 0)
        except ValueError:
            erreurs["ordre"] = "Ordre invalide."
            ordre = 0
        if not erreurs:
            nouveau = c is None
            c = c or Critere(entite_id=_eid())
            c.libelle, c.description, c.ordre = donnees["libelle"], donnees["description"] or None, ordre
            if nouveau:
                db.session.add(c)
            db.session.flush()
            definir_coefficient(c, poids, current_user.id)
            journaliser("critere_" + ("cree" if nouveau else "modifie"), c.libelle, f"poids={poids}")
            db.session.commit()
            flash("Critère enregistré. Le nouveau poids s'applique aux évaluations à venir.", "succes")
            return redirect(request.path)
        edition = c
    elif request.args.get("modifier", type=int):
        edition = _dans_entite(db.session.get(Critere, request.args.get("modifier", type=int)) or abort(404))
        donnees = {"libelle": edition.libelle, "description": edition.description or "",
                   "poids": str(__coef(edition)), "ordre": str(edition.ordre)}
    from ..services.grilles import masques as _masques
    caches = _masques(_eid())
    elements = [x for x in db.session.scalars(select(Critere).where(cond_entite(Critere.entite_id, _eid()))
                                              .order_by(Critere.actif.desc(), Critere.ordre, Critere.id))
                if x.id not in caches]
    utilises = set(db.session.scalars(select(EvaluationNote.critere_id).distinct()))
    from ..services import grilles as G
    spe = G.specifiques(_eid())
    coefs = {c.id: G.coefficient(c) for c in elements}
    total_poids = sum(coefs[c.id] for c in elements if c.actif and c.id not in spe)
    gr = G.grilles(_eid())
    grilles_postes = sorted(((v["libelle"], len(v["criteres"])) for v in gr.values() if v["criteres"]),
                            key=lambda x: cle_texte(x[0]))
    return render_template("parametres/criteres.html", elements=elements, edition=edition, donnees=donnees,
                           erreurs=erreurs, utilises=utilises, total_poids=total_poids, specifiques=spe, coefs=coefs,
                           utilisations={c.id: G.postes_utilisant(_eid(), c.id) for c in elements},
                           grilles_postes=grilles_postes, postes=postes_actifs(_eid()))


# --- Extraction de KPI par l'IA (document / image) --------------------------
@bp.post("/criteres/extraire")
@login_required
@rh_requis
def extraire_kpi_document():
    from ..models import ImportBrouillon
    from ..services.import_ia import ErreurImport
    from ..services.kpi_ia import extraire_kpi
    f = request.files.get("fichier")
    texte = request.form.get("texte") or ""
    contenu, nom, mime = (f.read(), f.filename, f.mimetype or "") if f and f.filename else (None, "", "")
    retour = request.form.get("retour") or url_for("parametres.criteres")
    if not retour.startswith("/"):
        retour = url_for("parametres.criteres")
    if not contenu and not texte.strip():
        flash("Ajoutez un fichier, une photo ou collez un texte.", "erreur")
        return redirect(retour)
    try:
        kpis, remarques = extraire_kpi(nom, mime, contenu, texte)
    except ErreurImport as exc:
        flash(str(exc), "erreur")
        return redirect(retour)
    except Exception:
        current_app.logger.exception("Extraction KPI")
        flash("L'analyse a échoué de façon inattendue. Réessayez avec un document plus lisible.", "erreur")
        return redirect(retour)
    if not kpis:
        flash("Aucun KPI détecté dans ce document.", "erreur")
        return redirect(retour)
    # Un seul brouillon KPI par utilisateur (les brouillons d'import du personnel ne sont pas touchés)
    for b in db.session.scalars(select(ImportBrouillon).where(ImportBrouillon.utilisateur_id == current_user.id)):
        if (b.donnees or {}).get("type") == "kpi":
            db.session.delete(b)
    b = ImportBrouillon(utilisateur_id=current_user.id, entite_id=_eid(), source_nom=nom or "Texte collé",
                        donnees={"type": "kpi", "kpis": kpis, "poste": request.form.get("poste") or ""},
                        remarques=remarques)
    db.session.add(b)
    journaliser("kpi_analyse", b.source_nom, f"{len(kpis)} KPI détecté(s)")
    db.session.commit()
    return redirect(url_for("parametres.verifier_kpi", brouillon_id=b.id))


def _brouillon_kpi(brouillon_id):
    from ..models import ImportBrouillon
    b = db.session.get(ImportBrouillon, brouillon_id) or abort(404)
    if b.utilisateur_id != current_user.id or (b.donnees or {}).get("type") != "kpi":
        abort(404)
    return b


@bp.route("/criteres/extraction/<int:brouillon_id>", methods=["GET", "POST"])
@login_required
@rh_requis
def verifier_kpi(brouillon_id):
    """Vérification des KPI extraits : correction, grille de destination, puis import."""
    from ..services import grilles as G
    b = _brouillon_kpi(brouillon_id)
    eid = b.entite_id or _eid()
    postes = postes_actifs(eid)
    par_cle = {cle_texte(p): p for p in postes}
    kpis = b.donnees.get("kpis", [])
    if request.method == "POST":
        if request.form.get("action") == "abandonner":
            db.session.delete(b)
            db.session.commit()
            flash("Extraction abandonnée : rien n'a été importé.", "info")
            return redirect(url_for("parametres.criteres"))
        caches = G.masques(eid)
        existants = {cle_texte(c.libelle): c for c in db.session.scalars(
            select(Critere).where(cond_entite(Critere.entite_id, eid)))}
        ordre = (db.session.scalar(select(func.max(Critere.ordre)).where(cond_entite(Critere.entite_id, eid))) or 0)
        grilles_maj: dict[str, dict[int, int]] = {}
        crees = repris = 0
        for i in range(len(kpis)):
            if request.form.get(f"k{i}_inclure") != "1":
                continue
            lib = " ".join((request.form.get(f"k{i}_libelle") or "").split())[:120]
            if not lib:
                continue
            desc = " ".join((request.form.get(f"k{i}_description") or "").split())[:400] or None
            poids = G.lire_coefficient(request.form.get(f"k{i}_poids")) or 1
            dest = request.form.get(f"k{i}_destination") or ""
            poste = None
            if dest.startswith("poste:"):
                poste = par_cle.get(cle_texte(dest[6:]))
            elif dest.startswith("nouveau:") and dest[8:].strip():
                from ..services.organisation import poste_canonique
                poste = poste_canonique(dest[8:], True, eid)
                marquer_poste_supprime(eid, poste, False, current_user.id)
            c = existants.get(cle_texte(lib))
            if c is None:
                ordre += 1
                c = Critere(entite_id=eid, libelle=lib, description=desc, poids=min(poids, 5), ordre=ordre)
                db.session.add(c)
                db.session.flush()
                G.definir_coefficient(c, poids, current_user.id)
                existants[cle_texte(lib)] = c
                crees += 1
                if poste:
                    G.marquer_specifique(eid, c.id, True, current_user.id)
            elif c.id in caches:           # critère supprimé auparavant : il revient
                G.reprendre_masque(eid, c, current_user.id)
                c.description = desc
                G.definir_coefficient(c, poids, current_user.id)
                if poste:
                    G.marquer_specifique(eid, c.id, True, current_user.id)
                repris += 1
            elif not poste:
                c.actif = True
                G.marquer_specifique(eid, c.id, False, current_user.id)   # entre dans la grille commune
                G.definir_coefficient(c, poids, current_user.id)
            if poste:
                if poste not in grilles_maj:
                    actuelle = G.grilles(eid).get(cle_texte(poste))
                    grilles_maj[poste] = dict(actuelle["criteres"]) if actuelle else {}
                grilles_maj[poste][c.id] = poids
        for poste, crit in grilles_maj.items():
            G.definir_grille(eid, poste, crit, current_user.id)
        journaliser("kpi_importes", b.source_nom,
                    f"{crees} créé(s), {repris} repris ; grilles de poste : {', '.join(grilles_maj) or 'aucune'}")
        db.session.delete(b)
        db.session.commit()
        msg = f"KPI importés : {crees} nouveau(x) critère(s)"
        if repris:
            msg += f", {repris} repris"
        if grilles_maj:
            msg += " ; grilles mises à jour : " + ", ".join(grilles_maj)
        flash(msg + ".", "succes")
        return redirect(url_for("parametres.criteres"))
    prefill = b.donnees.get("poste") or ""
    lignes = []
    for k in kpis:
        cible = prefill or k.get("poste") or ""
        if not cible:
            dest = ""
        elif cle_texte(cible) in par_cle:
            dest = "poste:" + par_cle[cle_texte(cible)]
        else:
            dest = "nouveau:" + cible
        lignes.append({**k, "destination": dest})
    nouveaux = sorted({l["destination"][8:] for l in lignes if l["destination"].startswith("nouveau:")})
    return render_template("parametres/verifier_kpi.html", b=b, lignes=lignes, postes=postes, nouveaux=nouveaux)


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
        if action == "supprimer_masse":
            libelles = [x.strip() for x in request.form.getlist("libelles") if x.strip()]
            if request.form.get("tout") == "1":
                libelles = postes_actifs(eid) + list(db.session.scalars(select(Poste.libelle).where(
                    Poste.actif.is_(False), cond_entite(Poste.entite_id, eid))))
            if not libelles:
                flash("Cochez au moins un poste.", "info")
                return redirect(request.path)
            vider = request.form.get("occupes") == "vider"
            portes = {cle_texte(x) for x in db.session.scalars(select(Employe.poste).where(
                Employe.poste.isnot(None), cond_entite(Employe.entite_id, eid)).distinct())}
            supprimes, gardes, fiches = 0, [], 0
            for lib in dict.fromkeys(libelles):
                if cle_texte(lib) in portes and not vider:
                    gardes.append(lib)
                    continue
                fiches += _supprimer_poste(eid, lib, "")
                supprimes += 1
            journaliser("postes_supprimes", f"{supprimes} poste(s)",
                        f"{fiches} fiche(s) sans poste" if fiches else "aucune fiche modifiée")
            db.session.commit()
            msg = f"{supprimes} poste(s) supprimé(s)."
            if fiches:
                msg += f" {fiches} fiche(s) n'ont plus de poste : à renseigner."
            if gardes:
                msg += (f" {len(gardes)} conservé(s) car occupé(s) par des salariés : {', '.join(gardes[:8])}"
                        f"{'…' if len(gardes) > 8 else ''}.")
            flash(msg, "succes")
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
            n = _supprimer_poste(eid, lib, remplacement)
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
                from ..services.grilles import renommer_poste
                renommer_poste(eid, ancien, libelle, current_user.id)
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
    from ..services.grilles import grilles as _grilles
    gr = _grilles(eid)
    elements = [{"libelle": x, "actif": True, "effectif": effectifs.get(x, 0), "groupe": poste_est_groupe(x, reglage),
                 "fiches": fiches.get(cle_texte(x), 0),
                 "grille": len(gr.get(cle_texte(x), {}).get("criteres", {}))} for x in actifs] + \
               [{"libelle": x, "actif": False, "effectif": effectifs.get(x, 0), "groupe": False,
                 "fiches": fiches.get(cle_texte(x), 0)} for x in archives]
    return render_template("parametres/postes.html", elements=elements, edition=edition,
                           donnees=donnees, erreurs=erreurs, actifs=actifs)


def _supprimer_poste(eid: int, lib: str, remplacement: str) -> int:
    """Supprime un poste de l'entité ; les fiches qui le portent reçoivent `remplacement` (ou rien).

    Renvoie le nombre de fiches modifiées. Ne valide pas la transaction.
    """
    k = cle_texte(lib)
    n = 0
    for e in db.session.scalars(select(Employe).where(Employe.poste.isnot(None), cond_entite(Employe.entite_id, eid))):
        if cle_texte(e.poste) == k:
            e.poste = remplacement or None
            n += 1
    for p in db.session.scalars(select(Poste).where(cond_entite(Poste.entite_id, eid))):
        if cle_texte(p.libelle) == k:
            db.session.delete(p)
    marquer_poste_supprime(eid, lib, True, current_user.id)
    from ..services.grilles import definir_grille
    definir_grille(eid, lib, {}, current_user.id)
    return n


@bp.route("/postes/grille", methods=["GET", "POST"])
@login_required
@rh_requis
def grille_poste():
    """Grille KPI d'un poste : choix des critères (bibliothèque de l'entité) et de leurs coefficients."""
    from ..services import grilles as G
    eid = _eid()
    poste = (request.values.get("libelle") or "").strip() or abort(404)
    if cle_texte(poste) not in {cle_texte(x) for x in postes_actifs(eid)}:
        abort(404)
    url_retour = url_for("parametres.grille_poste", libelle=poste)
    erreurs, donnees = {}, {}
    if request.method == "POST":
        action = request.form.get("action", "enregistrer")
        if action == "commune":
            G.definir_grille(eid, poste, {}, current_user.id)
            journaliser("grille_poste", poste, "grille commune")
            db.session.commit()
            flash(f"« {poste} » utilise de nouveau la grille commune.", "succes")
            return redirect(url_retour)
        if action == "copier":
            source = (request.form.get("source") or "").strip()
            defn = G.grilles(eid).get(cle_texte(source))
            if not defn:
                flash("Ce poste n'a pas de grille propre à copier.", "erreur")
                return redirect(url_retour)
            G.definir_grille(eid, poste, defn["criteres"], current_user.id)
            journaliser("grille_poste", poste, f"copiée de {source}")
            db.session.commit()
            flash(f"Grille de « {source} » copiée sur « {poste} ».", "succes")
            return redirect(url_retour)
        if action == "ajouter_critere":
            donnees = {k: (request.form.get(k) or "").strip() for k in ("libelle_critere", "description", "poids")}
            lib = donnees["libelle_critere"][:120]
            homonyme = db.session.scalar(select(Critere).where(func.lower(Critere.libelle) == lib.lower(),
                                                               cond_entite(Critere.entite_id, eid))) if lib else None
            if not lib:
                erreurs["libelle_critere"] = "Le libellé est obligatoire."
            elif homonyme and homonyme.id not in G.masques(eid):
                erreurs["libelle_critere"] = "Ce critère existe déjà dans la bibliothèque : cochez-le dans la liste."
            poids = G.lire_coefficient(donnees["poids"])
            if poids is None:
                erreurs["poids"] = "Coefficient : entier de 1 à 100."
                poids = 1
            if not erreurs:
                ordre = (db.session.scalar(select(func.max(Critere.ordre)).where(
                    cond_entite(Critere.entite_id, eid))) or 0) + 1
                if homonyme:            # recréé après suppression : même ligne, historique intact
                    G.reprendre_masque(eid, homonyme, current_user.id)
                    c = homonyme
                    c.description, c.poids, c.ordre = donnees["description"][:400] or None, min(poids, 5), ordre
                else:
                    c = Critere(entite_id=eid, libelle=lib, description=donnees["description"][:400] or None,
                                poids=min(poids, 5), ordre=ordre)
                    db.session.add(c)
                db.session.flush()
                G.marquer_specifique(eid, c.id, True, current_user.id)
                # Le poste démarre de sa grille actuelle (ou de la commune) + ce nouveau critère
                actuelle, _ = G.grille_pour(eid, poste)
                G.definir_grille(eid, poste, {**{x.id: x.poids for x in actuelle}, c.id: poids}, current_user.id)
                journaliser("critere_cree", lib, f"spécifique au poste {poste}")
                db.session.commit()
                flash(f"Critère « {lib} » créé et ajouté à la grille de « {poste} ».", "succes")
                return redirect(url_retour)
        if action == "enregistrer":
            choix = {}
            for cid in request.form.getlist("inclus"):
                if not cid.isdigit():
                    continue
                p_ = G.lire_coefficient(request.form.get(f"poids_{cid}")) or 1
                choix[int(cid)] = p_
            valides = {c.id for c in db.session.scalars(select(Critere).where(
                Critere.id.in_(choix or {-1}), Critere.actif.is_(True), cond_entite(Critere.entite_id, eid)))}
            choix = {k: v for k, v in choix.items() if k in valides}
            if not choix:
                flash("Cochez au moins un critère (ou revenez à la grille commune).", "erreur")
                return redirect(url_retour)
            G.definir_grille(eid, poste, choix, current_user.id)
            journaliser("grille_poste", poste, f"{len(choix)} critère(s)")
            db.session.commit()
            flash(f"Grille de « {poste} » enregistrée : {len(choix)} critère(s). Elle s'applique aux évaluations à venir.",
                  "succes")
            return redirect(url_retour)
    propre = G.grille_du_poste(eid, poste)
    selection = {x.id: x.poids for x in (propre or G.grille_commune(eid))}
    coefs = {c.id: G.coefficient(c) for c in db.session.scalars(select(Critere).where(cond_entite(Critere.entite_id, eid)))}
    spe = G.specifiques(eid)
    bibliotheque = db.session.scalars(select(Critere).where(Critere.actif.is_(True), cond_entite(Critere.entite_id, eid))
                                      .order_by(Critere.ordre, Critere.id)).all()
    autres_grilles = sorted((v["libelle"] for k, v in G.grilles(eid).items()
                             if k != cle_texte(poste) and v["criteres"]), key=cle_texte)
    nb_salaries = sum(1 for p_ in db.session.scalars(select(Employe.poste).where(
        Employe.actif.is_(True), Employe.poste.isnot(None), cond_entite(Employe.entite_id, eid))) if cle_texte(p_) == cle_texte(poste))
    return render_template("parametres/grille_poste.html", poste=poste, propre=propre is not None,
                           selection=selection, bibliotheque=bibliotheque, specifiques=spe, coefs=coefs,
                           utilisations={c.id: G.postes_utilisant(eid, c.id) for c in bibliotheque if c.id in spe},
                           autres_grilles=autres_grilles, nb_salaries=nb_salaries, erreurs=erreurs, donnees=donnees)


@bp.post("/postes/importer")
@login_required
@rh_requis
def importer_postes():
    from ..services.postes_import import ErreurImportPostes, importer, lire_fichier, lire_texte
    eid = _eid()
    f = request.files.get("fichier")
    try:
        if f and f.filename:
            libelles = lire_fichier(f.filename, f.read())
        else:
            libelles = lire_texte(request.form.get("texte"))
        bilan = importer(eid, libelles, current_user.id, ne_garder_que=request.form.get("ne_garder_que") == "1")
    except ErreurImportPostes as exc:
        flash(str(exc), "erreur")
        return redirect(url_for("parametres.postes") + "#import")
    journaliser("postes_importes", f"{len(bilan['ajoutes'])} ajouté(s)",
                f"{bilan['lus']} lu(s), {bilan['deja']} déjà présent(s), {len(bilan['retires'])} retiré(s)")
    db.session.commit()
    msg = f"{bilan['lus']} poste(s) lu(s) : {len(bilan['ajoutes'])} ajouté(s), {bilan['deja']} déjà dans la liste."
    if bilan["retires"]:
        msg += f" {len(bilan['retires'])} poste(s) absent(s) du fichier retiré(s)."
    if bilan["gardes"]:
        msg += (f" Conservés car portés par des salariés : {', '.join(bilan['gardes'][:8])}"
                f"{'…' if len(bilan['gardes']) > 8 else ''}.")
    flash(msg, "succes")
    return redirect(url_for("parametres.postes"))


@bp.get("/postes/modele.xlsx")
@login_required
@rh_requis
def modele_postes():
    from ..services.exports import export_excel
    exemples = ["Directeur général", "Conducteur de travaux", "Chef de chantier", "Comptable", "Magasinier"]
    return export_excel("Liste des postes", ["Poste"], [[x] for x in exemples], "modele_postes")


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
