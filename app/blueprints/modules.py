"""Portail des modules : page d'accueil après connexion quand plusieurs modules existent."""
from flask import Blueprint, abort, redirect, render_template, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..services.audit import journaliser
from ..services import modules as M

bp = Blueprint("modules", __name__, url_prefix="/modules")


@bp.get("/")
@login_required
def portail():
    if not M.externes_configures():
        return redirect(url_for("tableau_bord.index"))
    return render_template("modules/portail.html", modules=M.modules_pour(current_user.email))


@bp.get("/<code>")
@login_required
def ouvrir(code):
    m = M.module(code) or abort(404)
    if m.get("interne"):
        return redirect(url_for("tableau_bord.index"))
    journaliser("ouverture_module", current_user.email, m["nom"])
    db.session.commit()
    return redirect(M.url_ouverture(code, current_user.email))
