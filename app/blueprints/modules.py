"""Portail des modules : page d'accueil après connexion quand plusieurs modules existent."""
from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import csrf, db
from ..services.audit import journaliser
from ..services import modules as M

bp = Blueprint("modules", __name__, url_prefix="/modules")


@bp.get("/")
def portail():
    """Page d'accueil publique (avant connexion) ou portail de l'utilisateur connecté."""
    if not M.externes_configures():
        return redirect(url_for("tableau_bord.index"))
    if not current_user.is_authenticated:
        modules = [m for m in M.MODULES if m.get("interne") or M.module(m["code"])]
        return render_template("modules/accueil.html", modules=modules)
    return render_template("modules/portail.html", modules=M.modules_pour(current_user.email))


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
    return redirect(M.url_ouverture(code, current_user.email))


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
