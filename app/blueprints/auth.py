"""Connexion, déconnexion, changement de mot de passe."""
from datetime import timedelta
from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import select

from ..extensions import db
from ..models import Utilisateur, utcnow
from ..services.audit import journaliser

bp = Blueprint("auth", __name__)

MAX_ECHECS = 5
DUREE_BLOCAGE = timedelta(minutes=15)


def valider_mot_de_passe(mdp: str) -> str | None:
    if len(mdp) < 10:
        return "Le mot de passe doit contenir au moins 10 caractères."
    if not any(c.isdigit() for c in mdp) or not any(c.isalpha() for c in mdp):
        return "Le mot de passe doit contenir des lettres et au moins un chiffre."
    return None


def _url_sure(cible: str | None) -> bool:
    if not cible:
        return False
    p = urlparse(cible)
    return not p.netloc and not p.scheme and cible.startswith("/")


@bp.route("/connexion", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("tableau_bord.index"))
    erreur = None
    email = ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        mdp = request.form.get("mot_de_passe") or ""
        u = db.session.scalar(select(Utilisateur).where(Utilisateur.email == email))
        maintenant = utcnow()
        if u and u.bloque_jusqua and u.bloque_jusqua > maintenant:
            erreur = "Trop de tentatives. Compte temporairement bloqué, réessayez dans 15 minutes."
        elif u and u.actif and u.check_password(mdp):
            u.echecs_connexion = 0
            u.bloque_jusqua = None
            u.derniere_connexion = maintenant
            login_user(u, remember=bool(request.form.get("se_souvenir")))
            journaliser("connexion", u.email)
            db.session.commit()
            suivant = request.args.get("next")
            return redirect(suivant if _url_sure(suivant) else url_for("tableau_bord.index"))
        else:
            if u:
                u.echecs_connexion = (u.echecs_connexion or 0) + 1
                if u.echecs_connexion >= MAX_ECHECS:
                    u.bloque_jusqua = maintenant + DUREE_BLOCAGE
                    u.echecs_connexion = 0
                journaliser("connexion_echec", email)
                db.session.commit()
            # Message identique que le compte existe ou non (pas d'énumération)
            erreur = erreur or "Adresse e-mail ou mot de passe incorrect."
    return render_template("auth/login.html", erreur=erreur, email=email)


@bp.post("/deconnexion")
@login_required
def logout():
    journaliser("deconnexion", current_user.email, commit=True)
    logout_user()
    flash("Vous êtes déconnecté.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/mot-de-passe", methods=["GET", "POST"])
@login_required
def changer_mot_de_passe():
    erreurs = {}
    if request.method == "POST":
        actuel = request.form.get("actuel") or ""
        nouveau = request.form.get("nouveau") or ""
        confirmation = request.form.get("confirmation") or ""
        if not current_user.check_password(actuel):
            erreurs["actuel"] = "Mot de passe actuel incorrect."
        msg = valider_mot_de_passe(nouveau)
        if msg:
            erreurs["nouveau"] = msg
        elif nouveau == actuel:
            erreurs["nouveau"] = "Choisissez un mot de passe différent de l'actuel."
        if nouveau != confirmation:
            erreurs["confirmation"] = "Les deux mots de passe ne correspondent pas."
        if not erreurs:
            current_user.set_password(nouveau)
            current_user.doit_changer_mdp = False
            journaliser("mot_de_passe_modifie", current_user.email)
            db.session.commit()
            flash("Mot de passe mis à jour.", "succes")
            return redirect(url_for("tableau_bord.index"))
    return render_template("auth/mot_de_passe.html", erreurs=erreurs)
