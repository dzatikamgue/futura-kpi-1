"""Portail des modules : page d'accueil après connexion quand plusieurs modules existent."""
from flask import Blueprint, abort, redirect, render_template, url_for
from flask_login import current_user

from ..extensions import db
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
