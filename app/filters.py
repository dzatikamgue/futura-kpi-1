"""Filtres Jinja et variables globales des gabarits (formats français)."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from flask import request, url_for
from flask_login import current_user

from .services.notation import ECHELLE, MOIS, MOIS_COURTS, niveau


FUSEAU = ZoneInfo("Africa/Douala")


def date_fr(v, avec_heure=False):
    """Les dates-heures sont stockées en UTC et affichées à l'heure de Douala."""
    if not v:
        return "—"
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        v = v.astimezone(FUSEAU)
        return v.strftime("%d/%m/%Y à %H:%M" if avec_heure else "%d/%m/%Y")
    if isinstance(v, date):
        return v.strftime("%d/%m/%Y")
    return str(v)


def note_fr(v, decimales=2):
    if v is None:
        return "—"
    return f"{v:.{decimales}f}".replace(".", ",")


def nombre_fr(v):
    if v is None:
        return "—"
    return f"{v:,}".replace(",", " ")


def register_filters(app):
    app.jinja_env.filters["date_fr"] = date_fr
    app.jinja_env.filters["note"] = note_fr
    app.jinja_env.filters["nombre"] = nombre_fr
    app.jinja_env.filters["niveau"] = lambda v: niveau(v)
    app.jinja_env.filters["mois"] = lambda m: MOIS[int(m) - 1] if m else ""
    app.jinja_env.filters["mois_court"] = lambda m: MOIS_COURTS[int(m) - 1] if m else ""

    def url_tri(cle):
        """URL qui trie par `cle` en conservant les autres filtres."""
        args = request.args.to_dict()
        actuel, sens = args.get("tri"), args.get("sens", "asc")
        args["sens"] = "desc" if (actuel == cle and sens == "asc") else "asc"
        args["tri"] = cle
        args.pop("page", None)
        return url_for(request.endpoint, **(request.view_args or {}), **args)

    def url_page(page):
        args = request.args.to_dict()
        args["page"] = page
        return url_for(request.endpoint, **(request.view_args or {}), **args)

    def url_avec(**kw):
        args = request.args.to_dict()
        args.update({k: v for k, v in kw.items()})
        return url_for(request.endpoint, **(request.view_args or {}), **args)

    def logo_entite(e, blanc=False):
        """URL du logo d'une entité : logo importé, sinon logo FUTURA pour l'entité principale."""
        if e is not None and e.logo:
            return url_for("entites.logo", entite_id=e.id, v=int(e.updated_at.timestamp()))
        if e is None or e.principale:
            return url_for("static", filename="img/logo-white.svg" if blanc else "img/logo.svg")
        return None

    @app.context_processor
    def globals_gabarits():
        per = entite = None
        onglets, charte = [], None
        if current_user.is_authenticated:
            from .permissions import perimetre
            from .services.entites import entites_accessibles, palette
            per = perimetre()
            entite = per.entite
            onglets = entites_accessibles(current_user)
            charte = palette(entite.couleur if entite else None)
        return {
            "entite": entite,
            "onglets_entites": onglets,
            "charte": charte,
            "logo_entite": logo_entite,
            "app_name": app.config["APP_NAME"],
            "company_name": app.config["COMPANY_NAME"],
            "per": per,
            "url_tri": url_tri,
            "url_page": url_page,
            "url_avec": url_avec,
            "ECHELLE": ECHELLE,
            "MOIS": MOIS,
            "annee_courante": date.today().year,
        }
