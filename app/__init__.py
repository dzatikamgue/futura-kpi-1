"""Futura Performance — application de suivi mensuel des KPI du personnel."""
import logging
import os

from flask import Flask, render_template, request
from flask_login import current_user
from flask_wtf.csrf import CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config
from .extensions import csrf, db, login_manager, migrate


def create_app(config_class=Config):
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(config_class)

    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("SECRET_KEY doit être défini en production : ajoutez la variable SECRET_KEY "
                           "(longue chaîne aléatoire) dans le service web → Variables sur Railway.")

    # Railway place l'application derrière un proxy HTTPS
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    logging.basicConfig(level=logging.INFO)

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)
    csrf.init_app(app)
    login_manager.init_app(app)

    from . import models  # noqa: F401  (enregistre les modèles pour Alembic)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(models.Utilisateur, int(user_id))

    from .blueprints import (auth, entites, evaluations, import_personnel, journal,
                             parametres, personnel, suivi, tableau_bord)
    for bp in (auth.bp, tableau_bord.bp, evaluations.bp, personnel.bp, suivi.bp,
               import_personnel.bp, parametres.bp, journal.bp, entites.bp):
        app.register_blueprint(bp)

    from .filters import register_filters
    register_filters(app)

    from .cli import register_cli
    register_cli(app)

    @app.before_request
    def forcer_changement_mdp():
        from flask import redirect, url_for
        # Seul le superadmin choisit son propre mot de passe ; les autres utilisent celui qu'il leur attribue
        if (current_user.is_authenticated and current_user.doit_changer_mdp and current_user.est_superadmin
                and request.endpoint not in ("auth.changer_mot_de_passe", "auth.logout", "static")):
            return redirect(url_for("auth.changer_mot_de_passe"))

    @app.after_request
    def entetes_securite(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'")
        if app.config["IS_PRODUCTION"]:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        # Les pages authentifiées ne doivent pas être mises en cache par un navigateur partagé
        if current_user.is_authenticated and resp.mimetype == "text/html":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(403)
    def interdit(e):
        return render_template("erreur.html", code=403, titre="Accès refusé",
                               message="Cette page ne fait pas partie de votre périmètre."), 403

    @app.errorhandler(404)
    def introuvable(e):
        return render_template("erreur.html", code=404, titre="Page introuvable",
                               message="L'élément demandé n'existe pas ou a été supprimé."), 404

    @app.errorhandler(413)
    def trop_gros(e):
        return render_template("erreur.html", code=413, titre="Fichier trop volumineux",
                               message="La taille maximale autorisée est de 10 Mo."), 413

    @app.errorhandler(CSRFError)
    def csrf_erreur(e):
        return render_template("erreur.html", code=400, titre="Session expirée",
                               message="Le formulaire a expiré. Rechargez la page et recommencez : "
                                       "votre brouillon local a été conservé."), 400

    @app.errorhandler(500)
    def erreur_serveur(e):
        db.session.rollback()
        return render_template("erreur.html", code=500, titre="Erreur inattendue",
                               message="Une erreur est survenue. L'équipe a été notifiée dans les journaux du serveur."), 500

    @app.get("/sante")
    def sante():
        """Point de contrôle pour Railway (healthcheck)."""
        db.session.execute(db.text("SELECT 1"))
        return {"statut": "ok"}

    return app
