"""Portail des modules : page d'accueil après connexion quand plusieurs modules existent."""
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select

from ..extensions import csrf, db
from ..services.audit import journaliser
from ..services import modules as M
from ..permissions import rh_requis

bp = Blueprint("modules", __name__, url_prefix="/modules")


@bp.get("/")
def portail():
    """Page d'accueil publique (avant connexion) ou portail de l'utilisateur connecté."""
    if not M.externes_configures():
        return redirect(url_for("tableau_bord.index"))
    if not current_user.is_authenticated:
        modules = [m for m in M.MODULES if m.get("interne") or M.module(m["code"])]
        return render_template("modules/accueil.html", modules=modules)
    return render_template("modules/portail.html", modules=M.modules_pour(current_user.email, current_user))


@bp.get("/<code>")
def ouvrir(code):
    m = M.module(code) or abort(404)
    if not current_user.is_authenticated:
        # Pas encore connecté : page de connexion du module choisi
        if m.get("interne"):
            return redirect(url_for("auth.login", module=code))
        return redirect(M.url_connexion(code))
    if m.get("interne"):
        return redirect(url_for("tableau_bord.index"))
    journaliser("ouverture_module", current_user.email, m["nom"])
    db.session.commit()
    return redirect(M.url_ouverture(code, current_user.email, M.role_central(code, current_user),
                                    current_user.nom_affiche))


@bp.route("/acces", methods=["GET", "POST"])
@login_required
@rh_requis
def acces():
    """Accès centralisés : le rôle de chaque compte dans chaque module, choisi ici une fois pour toutes."""
    from ..models import Utilisateur
    externes = [m for m in M.MODULES if not m.get("interne") and m.get("roles")]
    comptes = list(db.session.scalars(select(Utilisateur).where(Utilisateur.actif.is_(True))
                                      .order_by(Utilisateur.email)))
    actuel = M.acces_centraux()
    if request.method == "POST":
        nouveau, changes = {}, []
        for u in comptes:
            for m in externes:
                r = request.form.get(f"r_{u.id}_{m['code']}", "")
                if r and r not in m["roles"]:
                    continue
                if r:
                    nouveau.setdefault(u.id, {})[m["code"]] = r
                avant = actuel.get(u.id, {}).get(m["code"], "")
                if avant != r:
                    changes.append(f"{u.email} · {m['nom']} : {m['roles'].get(avant, 'aucun')} → {m['roles'].get(r, 'aucun')}")
        # Les comptes désactivés gardent leur réglage (au cas où ils seraient réactivés)
        ids_actifs = {u.id for u in comptes}
        for uid, v in actuel.items():
            if uid not in ids_actifs:
                nouveau[uid] = v
        M.enregistrer_acces_centraux(nouveau, current_user.id)
        for c in changes:
            journaliser("acces_module", c)
        db.session.commit()
        flash(f"Accès aux modules enregistrés ({len(changes)} changement(s)). Ils s'appliquent à la prochaine "
              "ouverture du module depuis le portail.", "succes")
        return redirect(request.path)
    return render_template("modules/acces.html", modules=externes, comptes=comptes, actuel=actuel)


@bp.post("/api/ia")
@csrf.exempt                          # appel serveur à serveur, authentifié par signature HMAC
def api_ia():
    """Lecture d'images/PDF par Claude pour un module (ex. Flotte), avec la clé de l'application KPI.

    En-têtes : X-Futura-Module, X-Futura-Horodatage, X-Futura-Signature.
    Corps JSON : {"outil": {...}, "blocs": [...]} → {"entree": {...}} ou {"erreur": "..."}.
    """
    code = request.headers.get("X-Futura-Module", "")
    corps = request.get_data(cache=True)
    if not M.module(code) or M.module(code).get("interne") or not M.verifier_signature_module(
            code, request.headers.get("X-Futura-Horodatage", ""), request.headers.get("X-Futura-Signature", ""), corps):
        return jsonify(erreur="Demande non autorisée."), 403
    donnees = request.get_json(silent=True) or {}
    outil, blocs = donnees.get("outil"), donnees.get("blocs")
    if not isinstance(outil, dict) or not isinstance(blocs, list) or not blocs or len(blocs) > 12:
        return jsonify(erreur="Demande incomplète."), 400
    if any(not isinstance(b, dict) or b.get("type") not in ("image", "document", "text") for b in blocs):
        return jsonify(erreur="Contenu non pris en charge."), 400
    from ..services.import_ia import ErreurImport, appeler_claude_blocs
    try:
        entree = appeler_claude_blocs(outil, blocs)
    except ErreurImport as exc:
        return jsonify(erreur=str(exc)), 422
    journaliser("lecture_ia_module", M.module(code)["nom"], f"{len(blocs)} élément(s)")
    db.session.commit()
    return jsonify(entree=entree)
