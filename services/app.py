"""Futura Services généraux — personnel (visas, cartes de séjour, billets d'avion) et logements
(loyers, électricité, eau, réparations).

Module du portail Futura (connexion unique /sso). Les gestionnaires (RH / services généraux) saisissent,
la direction consulte, l'occupant d'un logement y signale les réparations.

Lancement local :  flask --app app run --debug
Production      :  gunicorn "app:create_app()"
"""
import io
import os
import re
import secrets as _secrets
import time
from collections import defaultdict
from datetime import date, timedelta
from functools import wraps

import click
from flask import (Flask, abort, flash, has_request_context, redirect, render_template, request,
                   send_file, session, url_for)
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from werkzeug.middleware.proxy_fix import ProxyFix

import formulaires as F
from models import (ALERTE_BAIL_JOURS, ALERTE_DOC_JOURS, CATEGORIES_REPARATION, FOURNISSEURS, MIMES_AUTORISES,
                    MOTIFS_BILLET, OBJETS_PIECE, PERIODICITES, RENOUVELLEMENT, REPARATIONS_OUVERTES, ROLES,
                    STATUTS_BILLET, STATUTS_REPARATION, TYPES_DOCUMENT, TYPES_ENERGIE, UNITES, URGENCES,
                    Billet, Document, FactureEnergie, Journal, Logement, Loyer, Parametre, Personne, Piece,
                    Reparation, SuiviReparation, User, aujourdhui, db, migrer, mois_ajoutes, now)

csrf = CSRFProtect()
login_manager = LoginManager()
PAR_PAGE = 30
TAILLE_MAX_PIECE = 10 * 1024 * 1024
NB_MAX_PIECES = 6
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
        "novembre", "décembre"]
MOIS_COURTS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]

# Sous-modules affichés à l'accueil (code, titre, icône, couleur, famille)
SOUS_MODULES = {
    "documents": dict(titre="Visas et cartes de séjour", icone="id", famille="Personnel",
                      description="Visa et carte de séjour en cours de chaque personne : début, fin, alerte 30 jours "
                                  "avant l'expiration et suivi du renouvellement."),
    "billets": dict(titre="Billets d'avion", icone="plane", famille="Personnel",
                    description="Billets des employés : congés, missions, prises de poste et coûts."),
    "loyers": dict(titre="Loyers", icone="key", famille="Logements",
                   description="Échéances de loyer par logement : payées, à venir, en retard."),
    "electricite": dict(titre="Électricité", icone="bolt", famille="Logements",
                        description="Factures ENEO par logement : consommation, montants, paiements."),
    "eau": dict(titre="Eau", icone="drop", famille="Logements",
                description="Factures CAMWATER par logement : consommation, montants, paiements."),
    "reparations": dict(titre="Réparations", icone="wrench", famille="Logements",
                        description="Pannes signalées par les occupants, suivi des travaux et coûts."),
}


# --------------------------------------------------------------------------- #
#  Application
# --------------------------------------------------------------------------- #

def create_app():
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)

    prod = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("PRODUCTION"))
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        if prod:
            raise RuntimeError("Variable d'environnement SECRET_KEY manquante.")
        secret = "dev-uniquement-ne-jamais-utiliser-en-production"

    db_url = os.environ.get("DATABASE_URL") or "sqlite:///" + os.path.join(app.instance_path, "services.db")
    for prefixe in ("postgres://", "postgresql://"):
        if db_url.startswith(prefixe):
            db_url = "postgresql+psycopg://" + db_url[len(prefixe):]
            break

    app.config.update(
        SECRET_KEY=secret,
        SQLALCHEMY_DATABASE_URI=db_url,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=prod,
        REMEMBER_COOKIE_SECURE=prod,
        REMEMBER_COOKIE_HTTPONLY=True,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        WTF_CSRF_TIME_LIMIT=None,
        MAX_CONTENT_LENGTH=NB_MAX_PIECES * TAILLE_MAX_PIECE + 1024 * 1024,
    )
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "login"
    login_manager.login_message = "Veuillez vous connecter pour accéder à cette page."
    login_manager.login_message_category = "info"

    with app.app_context():
        try:
            migrer()
            bootstrap_admin()
        except IntegrityError:
            db.session.rollback()

    register_filters(app)
    register_routes(app)
    register_cli(app)
    return app


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


def bootstrap_admin():
    email, mdp = os.environ.get("ADMIN_EMAIL"), os.environ.get("ADMIN_PASSWORD")
    if email and mdp and not User.query.first():
        u = User(nom=os.environ.get("ADMIN_NOM", "Administrateur"), email=email.lower().strip(), role="admin")
        u.set_password(mdp)
        db.session.add(u)
        db.session.commit()


# --------------------------------------------------------------------------- #
#  Utilitaires
# --------------------------------------------------------------------------- #

def roles(*autorises):
    def deco(f):
        @wraps(f)
        @login_required
        def wrapper(*a, **k):
            if current_user.role not in autorises:
                abort(403)
            return f(*a, **k)
        return wrapper
    return deco


gestion = roles("admin", "gestionnaire")
lecture = roles("admin", "gestionnaire", "direction")


def journaliser(action, cible="", details=""):
    connecte = has_request_context() and current_user and current_user.is_authenticated
    db.session.add(Journal(user_id=current_user.id if connecte else None, action=action,
                           cible=(cible or "")[:160], details=details))


def parametre(cle, defaut):
    p = db.session.get(Parametre, cle)
    try:
        return int(p.valeur) if p else defaut
    except ValueError:
        return defaut


def seuil_docs():
    return parametre("alerte_documents_jours", ALERTE_DOC_JOURS)


def seuil_bail():
    return parametre("alerte_bail_jours", ALERTE_BAIL_JOURS)


def ou_404(modele, oid):
    return db.session.get(modele, oid) or abort(404)


def options_personnes():
    return [(p.id, p.nom + (f" · {p.societe}" if p.societe else ""))
            for p in Personne.query.filter_by(actif=True).order_by(Personne.nom)]


def options_logements(tous=False):
    q = Logement.query if tous else Logement.query.filter_by(actif=True)
    return [(l.id, l.libelle) for l in q.order_by(Logement.nom)]


def type_reel(contenu):
    if contenu[:5] == b"%PDF-":
        return "application/pdf"
    if contenu[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if contenu[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if contenu[:4] == b"RIFF" and contenu[8:12] == b"WEBP":
        return "image/webp"
    return None


def lire_pieces(champ="pieces"):
    """Fichiers envoyés, contrôlés par leur contenu. Retourne (liste de Piece sans objet, erreur)."""
    fichiers = [x for x in request.files.getlist(champ) if x and x.filename]
    if len(fichiers) > NB_MAX_PIECES:
        return [], f"{NB_MAX_PIECES} fichiers maximum par envoi."
    pieces = []
    for fx in fichiers:
        contenu = fx.read()
        if not contenu:
            return [], f"« {fx.filename} » est vide."
        if len(contenu) > TAILLE_MAX_PIECE:
            return [], f"« {fx.filename} » dépasse 10 Mo. Réduisez la qualité du scan ou de la photo."
        mime = type_reel(contenu)
        if not mime:
            return [], f"« {fx.filename} » n'est ni un PDF ni une image (JPG, PNG, WEBP)."
        nom = re.sub(r"[^\w.\- ()]", "_", os.path.basename(fx.filename))[:200] or f"piece.{MIMES_AUTORISES[mime]}"
        pieces.append(Piece(nom_fichier=nom, mime=mime, taille=len(contenu), contenu=contenu,
                            ajoute_par_id=current_user.id))
    return pieces, None


def rattacher(pieces, objet, oid):
    for p in pieces:
        p.objet, p.objet_id = objet, oid
        db.session.add(p)


def pieces_de(objet, oid):
    return Piece.query.filter_by(objet=objet, objet_id=oid).order_by(Piece.ajoute_le).all()


def supprimer_pieces(objet, oid):
    Piece.query.filter_by(objet=objet, objet_id=oid).delete(synchronize_session=False)


def _milliers(n):
    return f"{int(n):,}".replace(",", " ")


def mois_libelle(d, court=False):
    if not d:
        return "—"
    return f"{(MOIS_COURTS if court else MOIS)[d.month - 1]} {d.year}"


def periode_libelle(debut, nb_mois):
    if not nb_mois or nb_mois == 1:
        return mois_libelle(debut)
    fin = mois_ajoutes(debut, nb_mois - 1)
    return f"{mois_libelle(debut, True)} → {mois_libelle(fin, True)}"


def args_page():
    try:
        return max(1, int(request.args.get("page", 1)))
    except ValueError:
        return 1


def recherche():
    return (request.args.get("q") or "").strip()


def exporter_excel(nom, titre, entetes, lignes, formats=None, largeurs=None, total_col=None):
    """Classeur Excel propre : titre, en-têtes au bleu Futura, filtres, volets figés, total."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = titre[:31]
    ws.append([f"Futura — {titre}"])
    ws["A1"].font = Font(bold=True, size=14, color="1F3F7F")
    ws.append([f"Export du {now():%d/%m/%Y à %H:%M} par {current_user.nom}"])
    ws["A2"].font = Font(italic=True, color="5B6370")
    ws.append(entetes)
    for c in ws[3]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3F7F")
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for l in lignes:
        ws.append(list(l))
    derniere = ws.max_row
    if total_col is not None and lignes:
        lettre = get_column_letter(total_col + 1)
        ligne = ["TOTAL"] + [""] * (len(entetes) - 1)
        ligne[total_col] = f"=SUM({lettre}4:{lettre}{derniere})"
        ws.append(ligne)
        for c in ws[ws.max_row]:
            c.font = Font(bold=True)
    formats = formats or {}
    for row in ws.iter_rows(min_row=4, max_row=ws.max_row):
        for i, fmt in formats.items():
            row[i].number_format = {"fcfa": '#,##0 "FCFA"', "date": "DD/MM/YYYY", "dateheure": "DD/MM/YYYY HH:MM",
                                    "nombre": "#,##0"}.get(fmt, fmt)
    for i, larg in enumerate(largeurs or [18] * len(entetes), 1):
        ws.column_dimensions[get_column_letter(i)].width = larg
    ws.freeze_panes = "B4"
    ws.auto_filter.ref = f"A3:{get_column_letter(len(entetes))}{max(derniere, 3)}"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    journaliser("Export Excel", titre, f"{len(lignes)} ligne(s)")
    db.session.commit()
    return send_file(buf, as_attachment=True, download_name=f"{nom}_{now():%Y%m%d_%H%M}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# --------------------------------------------------------------------------- #
#  Calculs : documents, loyers, factures, réparations
# --------------------------------------------------------------------------- #

def documents_en_vigueur(type_doc=None):
    q = Document.query.options(selectinload(Document.personne)).join(Personne) \
        .filter(Document.remplace.is_(False), Personne.actif.is_(True))
    if type_doc:
        q = q.filter(Document.type_doc == type_doc)
    return q


def stats_documents(type_doc):
    s = seuil_docs()
    docs = documents_en_vigueur(type_doc).all()
    niv = [d.niveau(s) for d in docs]
    return dict(total=len(docs), valides=niv.count("ok"), alerte=niv.count("alerte") + niv.count("critique"),
                expires=niv.count("expire"))


def loyers_dus(jusqu_au=None):
    """Toutes les échéances de loyer échues et non payées : liste de dict, plus ancienne d'abord."""
    res = []
    for l in Logement.query.options(selectinload(Logement.loyers)).filter_by(actif=True):
        for debut, limite in l.echeances_dues(jusqu_au):
            res.append(dict(logement=l, debut=debut, limite=limite, montant=l.montant_echeance,
                            jours=(aujourdhui() - limite).days, periode=periode_libelle(debut, l.periodicite)))
    return sorted(res, key=lambda x: x["limite"])


def loyers_a_venir(jours=31):
    res = []
    for l in Logement.query.options(selectinload(Logement.loyers)).filter_by(actif=True):
        p = l.prochaine_echeance()
        if p and (p[1] - aujourdhui()).days <= jours:
            res.append(dict(logement=l, debut=p[0], limite=p[1], montant=l.montant_echeance,
                            jours=(p[1] - aujourdhui()).days, periode=periode_libelle(p[0], l.periodicite)))
    return sorted(res, key=lambda x: x["limite"])


def factures_impayees(type_energie=None):
    q = FactureEnergie.query.options(selectinload(FactureEnergie.logement)) \
        .filter(FactureEnergie.date_paiement.is_(None))
    if type_energie:
        q = q.filter(FactureEnergie.type_energie == type_energie)
    return q


def visibles_reparations():
    q = Reparation.query.options(selectinload(Reparation.logement), selectinload(Reparation.signale_par))
    if current_user.occupant:
        q = q.filter(Reparation.logement_id == (current_user.logement_id or -1))
    return q


def peut_voir_reparation(r):
    return not current_user.occupant or r.logement_id == current_user.logement_id


def couts_mensuels(nb=6):
    """Dépenses des `nb` derniers mois, par poste (loyers payés, électricité, eau, réparations, billets)."""
    debut = mois_ajoutes(aujourdhui().replace(day=1), -(nb - 1))
    mois = [mois_ajoutes(debut, i) for i in range(nb)]
    data = {m: defaultdict(int) for m in mois}

    def ajouter(d, poste, montant):
        if d and montant:
            m = d.replace(day=1)
            if m in data:
                data[m][poste] += montant

    for l in Loyer.query.filter(Loyer.date_paiement >= debut):
        ajouter(l.date_paiement, "loyers", l.montant)
    for f in FactureEnergie.query.filter(FactureEnergie.periode >= debut):
        ajouter(f.periode, f.type_energie, f.montant)
    for r in Reparation.query.filter(Reparation.cout.isnot(None)):
        ajouter(r.date_intervention or (r.cloturee_le.date() if r.cloturee_le else r.signale_le.date()),
                "reparations", r.cout)
    for b in Billet.query.filter(Billet.date_depart >= debut, Billet.statut != "annule"):
        ajouter(b.date_depart, "billets", b.montant)
    return [dict(mois=m, libelle=mois_libelle(m, True), postes=dict(data[m]), total=sum(data[m].values()))
            for m in mois]


def a_surveiller():
    """Liste unique des points d'attention, du plus grave au moins grave."""
    s, items = seuil_docs(), []
    for d in documents_en_vigueur().filter(Document.date_expiration <= aujourdhui() + timedelta(days=s)) \
            .order_by(Document.date_expiration):
        j = d.jours_restants
        items.append(dict(niveau="critique" if j <= 15 else "alerte", icone="id" if d.type_doc == "carte_sejour" else "stamp",
                          titre=f"{d.type_libelle} de {d.personne.nom}",
                          texte=(f"Expiré depuis {-j} jour{'s' if -j > 1 else ''}" if j < 0 else
                                 "Expire aujourd'hui" if j == 0 else f"Expire dans {j} jour{'s' if j > 1 else ''}")
                          + f" ({d.date_expiration:%d/%m/%Y})"
                          + (f" · {RENOUVELLEMENT[d.renouvellement]}" if d.renouvellement else " · commencer les procédures de renouvellement"),
                          lien=url_for("document_fiche", did=d.id), tri=j))
    for p in Personne.query.filter(Personne.actif.is_(True), Personne.passeport_expiration.isnot(None),
                                   Personne.passeport_expiration <= aujourdhui() + timedelta(days=s)):
        j = (p.passeport_expiration - aujourdhui()).days
        items.append(dict(niveau="critique" if j <= 15 else "alerte", icone="user", titre=f"Passeport de {p.nom}",
                          texte=(f"Expiré depuis {-j} j" if j < 0 else f"Expire dans {j} j")
                          + f" ({p.passeport_expiration:%d/%m/%Y})",
                          lien=url_for("personne_fiche", pid=p.id), tri=j))
    for e in loyers_dus():
        items.append(dict(niveau="critique" if e["jours"] > 15 else "alerte", icone="key",
                          titre=f"Loyer impayé — {e['logement'].libelle}",
                          texte=f"{e['periode']} · {_milliers(e['montant'])} FCFA · échu depuis {e['jours']} j",
                          lien=url_for("loyers"), tri=-e["jours"]))
    for f in factures_impayees().filter(FactureEnergie.date_echeance < aujourdhui()):
        j = (aujourdhui() - f.date_echeance).days
        items.append(dict(niveau="critique" if j > 15 else "alerte", icone="bolt" if f.type_energie == "electricite" else "drop",
                          titre=f"Facture {f.type_libelle.lower()} en retard — {f.logement.libelle}",
                          texte=f"{mois_libelle(f.periode)} · {_milliers(f.montant)} FCFA · échéance dépassée de {j} j",
                          lien=url_for("factures", type_energie=f.type_energie, statut="retard"), tri=-j))
    for r in Reparation.query.options(selectinload(Reparation.logement)) \
            .filter(Reparation.statut.in_(REPARATIONS_OUVERTES)):
        if r.urgence == "urgente" or r.jours_ouverte > 7:
            items.append(dict(niveau="critique" if r.urgence == "urgente" else "alerte", icone="wrench",
                              titre=f"{'Réparation urgente' if r.urgence == 'urgente' else 'Réparation en attente'} — {r.logement.libelle}",
                              texte=f"{r.titre} · {r.statut_libelle} · ouverte depuis {r.jours_ouverte} j",
                              lien=url_for("reparation_fiche", rid=r.id), tri=-r.jours_ouverte))
    for l in Logement.query.filter(Logement.actif.is_(True), Logement.bail_fin.isnot(None),
                                   Logement.bail_fin <= aujourdhui() + timedelta(days=seuil_bail())):
        j = (l.bail_fin - aujourdhui()).days
        items.append(dict(niveau="critique" if j <= 15 else "alerte", icone="home", titre=f"Fin de bail — {l.libelle}",
                          texte=(f"Bail échu depuis {-j} j" if j < 0 else f"Le bail se termine dans {j} j")
                          + f" ({l.bail_fin:%d/%m/%Y})", lien=url_for("logement_fiche", lid=l.id), tri=j))
    items.sort(key=lambda x: (x["niveau"] != "critique", x["tri"]))
    return items


# --------------------------------------------------------------------------- #
#  Filtres et contexte des gabarits
# --------------------------------------------------------------------------- #

def register_filters(app):
    app.jinja_env.globals["timedelta_jours"] = lambda n: timedelta(days=n)
    app.jinja_env.filters["fcfa"] = lambda v: "—" if v is None else f"{_milliers(v)} FCFA"
    app.jinja_env.filters["nombre"] = lambda v: "" if v is None else _milliers(v)
    app.jinja_env.filters["compact"] = lambda v: "0" if not v else (
        f"{v / 1e6:.1f} M".replace(".", ",").replace(",0 M", " M") if v >= 1e6 else f"{round(v / 1e3)} k")
    app.jinja_env.filters["date"] = lambda d: d.strftime("%d/%m/%Y") if d else "—"
    app.jinja_env.filters["dateheure"] = lambda d: d.strftime("%d/%m/%Y %H:%M") if d else "—"
    app.jinja_env.filters["mois"] = mois_libelle
    app.jinja_env.filters["taille"] = lambda o: f"{o / 1024:.0f} Ko" if o < 1024 * 1024 else f"{o / 1048576:.1f} Mo".replace(".", ",")
    app.jinja_env.filters["decimal"] = lambda v: "—" if v is None else f"{v:,.2f}".replace(",", " ").replace(".", ",").rstrip("0").rstrip(",")

    @app.context_processor
    def contexte():
        def url_avec(**kw):
            args = request.args.to_dict()
            args.update(kw)
            return url_for(request.endpoint, **(request.view_args or {}), **args)
        nb_alertes, docs_notif = 0, None
        if current_user.is_authenticated and not current_user.occupant and request.endpoint not in ("static", None):
            nb_alertes = len(a_surveiller())
            proches = documents_en_vigueur().filter(
                Document.date_expiration <= aujourdhui() + timedelta(days=seuil_docs())).all()
            if proches:
                docs_notif = dict(total=len(proches), expires=sum(1 for d in proches if d.jours_restants < 0),
                                  a_lancer=sum(1 for d in proches if d.renouvellement in ("", "a_lancer")),
                                  premier=min(proches, key=lambda d: d.date_expiration))
        return dict(ROLES=ROLES, SOUS_MODULES=SOUS_MODULES, TYPES_DOCUMENT=TYPES_DOCUMENT,
                    RENOUVELLEMENT=RENOUVELLEMENT, MOTIFS_BILLET=MOTIFS_BILLET, STATUTS_BILLET=STATUTS_BILLET,
                    TYPES_ENERGIE=TYPES_ENERGIE, UNITES=UNITES, FOURNISSEURS=FOURNISSEURS,
                    STATUTS_REPARATION=STATUTS_REPARATION, URGENCES=URGENCES, PERIODICITES=PERIODICITES,
                    PORTAIL_URL=(os.environ.get("PORTAIL_URL") or "").strip() or None,
                    url_avec=url_avec, maintenant=now(), aujourdhui=aujourdhui(), seuil=seuil_docs(),
                    nb_alertes=nb_alertes, docs_notif=docs_notif, periode_libelle=periode_libelle, options_de=F.options_de)


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #

_echecs = {}
_jetons_utilises = {}


def register_routes(app):

    @app.after_request
    def entetes_securite(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        if not getattr(resp, "piece_jointe", False):
            resp.headers["X-Frame-Options"] = "DENY"
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; "
                "script-src 'self'; form-action 'self'; frame-ancestors 'none'")
        return resp

    @app.errorhandler(CSRFError)
    def csrf_erreur(e):
        flash("Votre session a expiré. Renvoyez le formulaire.", "erreur")
        return redirect(request.referrer or url_for("accueil"))

    @app.errorhandler(403)
    def interdit(e):
        return render_template("erreur.html", code=403, message="Vous n'avez pas les droits pour cette action."), 403

    @app.errorhandler(404)
    def introuvable(e):
        return render_template("erreur.html", code=404, message="Cette page ou cet élément n'existe pas."), 404

    @app.errorhandler(413)
    def trop_gros(e):
        flash(f"Fichiers trop volumineux : {NB_MAX_PIECES} fichiers de 10 Mo maximum.", "erreur")
        return redirect(request.referrer or url_for("accueil"))

    @app.errorhandler(500)
    def erreur_serveur(e):
        db.session.rollback()
        return render_template("erreur.html", code=500,
                               message="Une erreur inattendue s'est produite. Réessayez dans un instant."), 500

    @app.route("/sante")
    def sante():
        db.session.execute(db.text("SELECT 1"))
        return {"statut": "ok"}

    # ---------------- Authentification ---------------- #

    @app.route("/connexion", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("accueil"))
        email = ""
        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()
            cle = f"{request.remote_addr}|{email}"
            nb, debut = _echecs.get(cle, (0, time.time()))
            if nb >= 5 and time.time() - debut < 600:
                flash("Trop de tentatives. Réessayez dans 10 minutes.", "erreur")
                return render_template("login.html", email=email), 429
            u = User.query.filter_by(email=email).first()
            if u and u.actif and u.check_password(request.form.get("password", "")):
                _echecs.pop(cle, None)
                login_user(u, remember=bool(request.form.get("souvenir")))
                u.derniere_connexion = now()
                journaliser("Connexion", u.email)
                db.session.commit()
                suite = request.args.get("next", "")
                if not suite.startswith("/") or suite.startswith("//"):
                    suite = url_for("accueil")
                return redirect(suite)
            _echecs[cle] = (nb + 1, debut if nb else time.time())
            flash("Identifiant ou mot de passe incorrect.", "erreur")
        return render_template("login.html", email=email)

    def _sso_lire(jeton):
        from itsdangerous import BadData, URLSafeTimedSerializer
        secret = os.environ.get("SSO_SECRET")
        if not secret:
            abort(404)
        try:
            d = URLSafeTimedSerializer(secret, salt="futura-sso").loads(jeton or "", max_age=60)
        except BadData:
            return None
        return d if isinstance(d, dict) and d.get("m") == "services" and d.get("email") else None

    @app.route("/sso")
    def sso():
        """Connexion unique depuis le portail Futura (jeton signé, 60 s, usage unique)."""
        d = _sso_lire(request.args.get("jeton"))
        if not d or not d.get("n"):
            flash("Lien de connexion expiré : rouvrez le module depuis le portail.", "erreur")
            return redirect(url_for("login"))
        t = time.time()
        for k in [k for k, v in _jetons_utilises.items() if t - v > 120]:
            _jetons_utilises.pop(k, None)
        if d["n"] in _jetons_utilises:
            flash("Lien de connexion déjà utilisé : rouvrez le module depuis le portail.", "erreur")
            return redirect(url_for("login"))
        _jetons_utilises[d["n"]] = t
        email = d["email"].strip().lower()
        u = User.query.filter(func.lower(User.email) == email).first()
        role = d.get("r")
        if role in ROLES:      # accès attribué depuis le portail : le portail fait foi
            if not u:
                u = User(email=email, nom=(d.get("nom") or email.split("@")[0])[:120], role=role, actif=True)
                u.set_password(_secrets.token_urlsafe(32))
                db.session.add(u)
                db.session.flush()
                journaliser("Compte créé depuis le portail", u.email, ROLES[role])
            elif u.role != role or not u.actif:
                journaliser("Accès mis à jour depuis le portail", u.email,
                            f"{ROLES.get(u.role, u.role)} → {ROLES[role]}")
                u.role, u.actif = role, True
        if not u or not u.actif:
            return render_template("erreur.html", code=403, message=(
                "Vous n'avez pas de compte actif dans Services généraux. Demandez à l'administrateur "
                "de vous créer un accès avec votre adresse e-mail.")), 403
        if current_user.is_authenticated and current_user.id != u.id:
            logout_user()
        login_user(u)
        u.derniere_connexion = now()
        journaliser("Connexion (portail Futura)", u.email)
        db.session.commit()
        return redirect(url_for("accueil"))

    @app.route("/sso/acces")
    def sso_acces():
        d = _sso_lire(request.args.get("jeton"))
        if not d:
            return {"erreur": "jeton invalide"}, 403
        u = User.query.filter(func.lower(User.email) == d["email"].strip().lower()).first()
        return {"acces": bool(u and u.actif)}

    @app.route("/sso/comptes")
    def sso_comptes():
        """Le portail lit les comptes de ce module et leur rôle (question signée), pour recopier les accès."""
        d = _sso_lire(request.args.get("jeton"))
        if not d or d.get("q") != "comptes":
            return {"erreur": "jeton invalide"}, 403
        return {"comptes": [{"email": u.email.lower(), "nom": u.nom, "role": u.role, "actif": bool(u.actif)}
                            for u in User.query.order_by(User.email)]}

    @app.route("/deconnexion", methods=["POST"])
    @login_required
    def logout():
        logout_user()
        flash("Vous êtes déconnecté.", "info")
        return redirect(url_for("login"))

    # ---------------- Accueil : les 7 sous-modules ---------------- #

    @app.route("/")
    @login_required
    def accueil():
        if current_user.occupant:
            return redirect(url_for("mon_logement"))
        cartes = {}
        s = stats_documents(None)
        sv, sc = stats_documents("visa"), stats_documents("carte_sejour")
        cartes["documents"] = dict(stats=[(s["valides"], "valides", ""), (s["alerte"], "à renouveler", "alerte"),
                                          (s["expires"], "expirés", "ko")],
                                   pied=f"{sv['total']} visa(s) · {sc['total']} carte(s) de séjour · alerte {seuil_docs()} j avant la fin",
                                   alerte=s["alerte"] + s["expires"], lien=url_for("documents"))
        a_venir = Billet.query.filter(Billet.date_depart >= aujourdhui(), Billet.statut.in_(("reserve", "emis")))
        debut_annee = aujourdhui().replace(month=1, day=1)
        ce_mois = Billet.query.filter(Billet.date_depart >= aujourdhui().replace(day=1),
                                      Billet.date_depart < mois_ajoutes(aujourdhui().replace(day=1), 1),
                                      Billet.statut != "annule").count()
        cout_an = db.session.query(func.coalesce(func.sum(Billet.montant), 0)) \
            .filter(Billet.date_depart >= debut_annee, Billet.statut != "annule").scalar()
        prochains = a_venir.filter(Billet.date_depart <= aujourdhui() + timedelta(days=14)).count()
        cartes["billets"] = dict(stats=[(a_venir.count(), "à venir", ""), (ce_mois, "ce mois-ci", ""),
                                        (prochains, "sous 14 jours", "alerte" if prochains else "")],
                                 pied=f"{_milliers(cout_an)} FCFA dépensés en {aujourdhui().year}",
                                 alerte=0, info=prochains, lien=url_for("billets"))
        dus = loyers_dus()
        nb_logements = Logement.query.filter_by(actif=True).count()
        cartes["loyers"] = dict(stats=[(nb_logements, "logements", ""), (len(dus), "en retard", "ko"),
                                       (len(loyers_a_venir()), "dus sous 30 j", "")],
                                pied=f"{_milliers(sum(e['montant'] for e in dus))} FCFA d'impayés",
                                alerte=len(dus), lien=url_for("loyers"))
        for code in ("electricite", "eau"):
            imp = factures_impayees(code).all()
            retard = [f for f in imp if f.niveau == "retard"]
            mois_c = FactureEnergie.query.filter(FactureEnergie.type_energie == code,
                                                 FactureEnergie.periode == mois_ajoutes(aujourdhui().replace(day=1), -1)).count()
            cartes[code] = dict(stats=[(len(imp), "à payer", ""), (len(retard), "en retard", "ko"),
                                       (mois_c, "le mois dernier", "")],
                                pied=f"{_milliers(sum(f.montant for f in imp))} FCFA à payer",
                                alerte=len(retard), lien=url_for("factures", type_energie=code))
        ouvertes = Reparation.query.filter(Reparation.statut.in_(REPARATIONS_OUVERTES))
        urgentes = ouvertes.filter(Reparation.urgence == "urgente").count()
        terminees = Reparation.query.filter(Reparation.statut == "terminee",
                                            Reparation.cloturee_le >= now().replace(day=1, hour=0, minute=0, second=0)).count()
        nouvelles = Reparation.query.filter_by(statut="signalee").count()
        cartes["reparations"] = dict(stats=[(ouvertes.count(), "en cours", ""), (urgentes, "urgentes", "ko"),
                                            (terminees, "terminées ce mois", "ok")],
                                     pied=f"{nouvelles} nouvelle{'s' if nouvelles > 1 else ''} demande{'s' if nouvelles > 1 else ''} à prendre en charge",
                                     alerte=urgentes, info=nouvelles, lien=url_for("reparations"))
        familles = {}
        for code, m in SOUS_MODULES.items():
            familles.setdefault(m["famille"], []).append(dict(m, code=code, **cartes[code]))
        return render_template("accueil.html", familles=familles)

    @app.route("/tableau-de-bord")
    @lecture
    def tableau_de_bord():
        items = a_surveiller()
        couts = couts_mensuels(6)
        max_total = max([c["total"] for c in couts] + [1])
        dus = loyers_dus()
        imp = factures_impayees().all()
        s_visa, s_carte = stats_documents("visa"), stats_documents("carte_sejour")  # noqa
        kpis = dict(
            docs=s_visa["alerte"] + s_visa["expires"] + s_carte["alerte"] + s_carte["expires"],
            docs_expires=s_visa["expires"] + s_carte["expires"],
            loyers=sum(e["montant"] for e in dus), loyers_nb=len(dus),
            factures=sum(f.montant for f in imp), factures_nb=len(imp),
            reparations=Reparation.query.filter(Reparation.statut.in_(REPARATIONS_OUVERTES)).count(),
            reparations_urgentes=Reparation.query.filter(Reparation.statut.in_(REPARATIONS_OUVERTES),
                                                         Reparation.urgence == "urgente").count(),
        )
        billets = Billet.query.options(selectinload(Billet.personne)) \
            .filter(Billet.date_depart >= aujourdhui(), Billet.statut.in_(("reserve", "emis"))) \
            .order_by(Billet.date_depart).limit(6).all()
        postes = [("loyers", "Loyers"), ("electricite", "Électricité"), ("eau", "Eau"),
                  ("reparations", "Réparations"), ("billets", "Billets d'avion")]
        return render_template("tableau_bord.html", items=items, couts=couts, max_total=max_total, kpis=kpis,
                               billets=billets, postes=postes, a_venir=loyers_a_venir(15))

    # ---------------- Personnel ---------------- #

    @app.route("/personnel")
    @lecture
    def personnel():
        q = Personne.query.options(selectinload(Personne.logement), selectinload(Personne.documents))
        if request.args.get("statut") != "tous":
            q = q.filter(Personne.actif.is_(request.args.get("statut") != "inactifs"))
        r = recherche()
        if r:
            like = f"%{r}%"
            q = q.filter(or_(Personne.nom.ilike(like), Personne.matricule.ilike(like), Personne.poste.ilike(like),
                             Personne.societe.ilike(like), Personne.nationalite.ilike(like)))
        page = q.order_by(Personne.nom).paginate(page=args_page(), per_page=PAR_PAGE, error_out=False)
        return render_template("personnel.html", page=page)

    def _doc_courant(p, type_doc):
        return next((d for d in p.documents if d.type_doc == type_doc and not d.remplace), None)
    app.jinja_env.globals["doc_courant"] = _doc_courant

    def formulaire_personne(p=None):
        champs = F.champs_personne(options_logements(tous=True))
        if p is not None:
            champs.append(F.C("actif", "Toujours dans l'entreprise", "case"))
        if request.method == "POST":
            val, err, f = F.lire(champs)
            if not err:
                if p is None:
                    p = Personne(cree_par_id=current_user.id)
                    db.session.add(p)
                F.appliquer(champs, p, val)
                db.session.flush()
                journaliser("Personne enregistrée", p.nom)
                db.session.commit()
                flash(f"Fiche de {p.nom} enregistrée.", "succes")
                return redirect(url_for("personne_fiche", pid=p.id))
        else:
            f, err = F.depuis_objet(champs, p), {}
        return render_template("formulaire.html", champs=champs, f=f, err=err, objet=p,
                               titre="Modifier la fiche" if p else "Nouvelle personne",
                               ariane=[("Personnel", url_for("personnel"))],
                               retour=url_for("personne_fiche", pid=p.id) if p else url_for("personnel"))

    @app.route("/personnel/nouveau", methods=["GET", "POST"])
    @gestion
    def personne_nouvelle():
        return formulaire_personne()

    @app.route("/personnel/<int:pid>/modifier", methods=["GET", "POST"])
    @gestion
    def personne_modifier(pid):
        return formulaire_personne(ou_404(Personne, pid))

    @app.route("/personnel/<int:pid>")
    @lecture
    def personne_fiche(pid):
        p = ou_404(Personne, pid)
        cout_billets = sum(b.montant or 0 for b in p.billets if b.statut != "annule")
        return render_template("personne_fiche.html", p=p, pieces=pieces_de("personne", p.id),
                               cout_billets=cout_billets)

    @app.route("/personnel/export")
    @lecture
    def personnel_export():
        lignes = []
        for p in Personne.query.options(selectinload(Personne.documents), selectinload(Personne.logement)) \
                .order_by(Personne.nom):
            v, c = _doc_courant(p, "visa"), _doc_courant(p, "carte_sejour")
            lignes.append([p.nom, p.matricule, p.poste, p.societe, p.nationalite, p.telephone, p.email,
                           p.passeport_numero, p.passeport_expiration, v.date_expiration if v else None,
                           c.date_expiration if c else None, p.logement.libelle if p.logement else "",
                           "Oui" if p.actif else "Non"])
        return exporter_excel("personnel", "Personnel", [
            "Nom", "Matricule", "Poste", "Société", "Nationalité", "Téléphone", "E-mail", "Passeport",
            "Passeport expire le", "Visa expire le", "Carte de séjour expire le", "Logement", "Actif"],
            lignes, formats={8: "date", 9: "date", 10: "date"},
            largeurs=[28, 12, 22, 20, 14, 16, 26, 14, 14, 14, 14, 28, 8])

    # ---------------- Visas et cartes de séjour ---------------- #

    def type_doc_ou_404(type_doc):
        if type_doc not in TYPES_DOCUMENT and type_doc != "tous":
            abort(404)
        return type_doc

    def requete_documents(type_doc):
        s = seuil_docs()
        q = Document.query.options(selectinload(Document.personne)).join(Personne)
        if type_doc in TYPES_DOCUMENT:
            q = q.filter(Document.type_doc == type_doc)
        statut = request.args.get("statut", "en_vigueur")
        j = aujourdhui()
        if statut != "historique":
            q = q.filter(Document.remplace.is_(False), Personne.actif.is_(True))
        if statut == "a_renouveler":
            q = q.filter(Document.date_expiration >= j, Document.date_expiration <= j + timedelta(days=s))
        elif statut == "a_traiter":
            q = q.filter(Document.date_expiration <= j + timedelta(days=s))
        elif statut == "expires":
            q = q.filter(Document.date_expiration < j)
        elif statut == "valides":
            q = q.filter(Document.date_expiration > j + timedelta(days=s))
        if request.args.get("renouvellement") in RENOUVELLEMENT:
            q = q.filter(Document.renouvellement == request.args["renouvellement"])
        r = recherche()
        if r:
            like = f"%{r}%"
            q = q.filter(or_(Personne.nom.ilike(like), Document.numero.ilike(like), Document.categorie.ilike(like),
                             Personne.societe.ilike(like)))
        return q.order_by(Document.date_expiration.asc(), Document.id)

    @app.route("/documents")
    @app.route("/documents/<type_doc>")
    @lecture
    def documents(type_doc="tous"):
        type_doc = request.args.get("type") or type_doc
        type_doc_ou_404(type_doc)
        page = requete_documents(type_doc).paginate(page=args_page(), per_page=PAR_PAGE, error_out=False)
        t = type_doc if type_doc in TYPES_DOCUMENT else None
        return render_template("documents.html", page=page, type_doc=type_doc, stats=stats_documents(t),
                               nb_type={k: stats_documents(k)["total"] for k in TYPES_DOCUMENT},
                               module=SOUS_MODULES["documents"])

    @app.route("/documents/export")
    @app.route("/documents/<type_doc>/export")
    @lecture
    def documents_export(type_doc="tous"):
        type_doc = request.args.get("type") or type_doc
        type_doc_ou_404(type_doc)
        s = seuil_docs()
        etats = {"ok": "Valide", "alerte": "À renouveler", "critique": "À renouveler d'urgence", "expire": "Expiré",
                 "remplace": "Remplacé"}
        lignes = [[d.personne.nom, d.personne.societe, d.type_libelle, d.numero, d.categorie, d.pays, d.date_delivrance,
                   d.date_expiration, d.jours_restants, etats[d.niveau(s)], RENOUVELLEMENT.get(d.renouvellement, ""),
                   d.renouvellement_note, d.cout] for d in requete_documents(type_doc)]
        return exporter_excel("visas_cartes_sejour", "Visas et cartes de séjour", [
            "Personne", "Société", "Type", "Numéro", "Catégorie", "Pays", "Début", "Fin", "Jours restants",
            "État", "Renouvellement", "Note", "Coût"], lignes, formats={6: "date", 7: "date", 12: "fcfa"},
            largeurs=[28, 18, 16, 16, 22, 12, 12, 12, 10, 22, 22, 32, 14], total_col=12)

    def formulaire_document(type_doc, d=None, renouveler=None):
        champs = F.champs_document(options_personnes)
        lib = TYPES_DOCUMENT[type_doc]
        if request.method == "POST":
            val, err, f = F.lire(champs)
            if val.get("date_delivrance") and val.get("date_expiration") and val["date_expiration"] < val["date_delivrance"]:
                err["date_expiration"] = "La date d'expiration doit suivre la date de délivrance."
            pieces, e = lire_pieces()
            if e:
                err["pieces"] = e
            if not err:
                nouveau = d is None
                if nouveau:
                    d = Document(type_doc=type_doc, cree_par_id=current_user.id)
                    db.session.add(d)
                F.appliquer(champs, d, val)
                d.renouvellement = val.get("renouvellement") or ""
                db.session.flush()
                if nouveau:
                    # Le nouveau document remplace les plus anciens du même type pour cette personne
                    anciens = Document.query.filter(Document.personne_id == d.personne_id,
                                                    Document.type_doc == type_doc, Document.id != d.id,
                                                    Document.remplace.is_(False),
                                                    Document.date_expiration <= d.date_expiration).all()
                    for a in anciens:
                        a.remplace = True
                        a.renouvellement = "obtenu"
                rattacher(pieces, "document", d.id)
                journaliser(f"{lib} {'enregistré' if nouveau else 'modifié'}", d.personne.nom,
                            f"expire le {d.date_expiration:%d/%m/%Y}")
                db.session.commit()
                flash(f"{lib} de {d.personne.nom} enregistré{'e' if type_doc == 'carte_sejour' else ''}.", "succes")
                return redirect(url_for("document_fiche", did=d.id))
        else:
            err = {}
            if renouveler is not None:
                f = F.depuis_objet(champs, None, personne_id=renouveler.personne_id, categorie=renouveler.categorie or "",
                                   pays=renouveler.pays or "", date_delivrance=aujourdhui().strftime("%Y-%m-%d"))
            else:
                f = F.depuis_objet(champs, d)
                if d is None and request.args.get("personne", "").isdigit():
                    f["personne_id"] = request.args["personne"]
        titre = (f"Renouveler : {lib.lower()} de {renouveler.personne.nom}" if renouveler else
                 f"Modifier {lib.lower()}" if d else f"Nouveau{'lle' if type_doc == 'carte_sejour' else ''} {lib.lower()}")
        return render_template("formulaire.html", champs=champs, f=f, err=err, objet=d, titre=titre, pieces_form=True,
                               aide=("Saisissez le nouveau document : l'ancien passera automatiquement à « remplacé »."
                                     if renouveler else None),
                               ariane=[("Visas et cartes de séjour", url_for("documents"))],
                               retour=url_for("document_fiche", did=(d or renouveler).id) if (d or renouveler)
                               else url_for("documents"))

    @app.route("/documents/<type_doc>/nouveau", methods=["GET", "POST"])
    @gestion
    def document_nouveau(type_doc):
        return formulaire_document(type_doc_ou_404(type_doc))

    @app.route("/document/<int:did>")
    @lecture
    def document_fiche(did):
        d = ou_404(Document, did)
        historique = Document.query.filter(Document.personne_id == d.personne_id, Document.type_doc == d.type_doc) \
            .order_by(Document.date_expiration.desc()).all()
        return render_template("document_fiche.html", d=d, historique=historique, pieces=pieces_de("document", d.id),
                               module=SOUS_MODULES["documents"])

    @app.route("/document/<int:did>/modifier", methods=["GET", "POST"])
    @gestion
    def document_modifier(did):
        d = ou_404(Document, did)
        return formulaire_document(d.type_doc, d)

    @app.route("/document/<int:did>/renouveler", methods=["GET", "POST"])
    @gestion
    def document_renouveler(did):
        d = ou_404(Document, did)
        return formulaire_document(d.type_doc, renouveler=d)

    @app.route("/document/<int:did>/demarche", methods=["POST"])
    @gestion
    def document_demarche(did):
        """Mise à jour rapide de l'état du renouvellement (depuis la liste ou la fiche)."""
        d = ou_404(Document, did)
        etat = request.form.get("renouvellement", "")
        if etat not in RENOUVELLEMENT:
            abort(400)
        d.renouvellement = etat
        note = (request.form.get("note") or "").strip()[:255]
        if note:
            d.renouvellement_note = note
        journaliser("Renouvellement mis à jour", f"{d.type_libelle} · {d.personne.nom}", RENOUVELLEMENT[etat])
        db.session.commit()
        flash(f"Renouvellement : {RENOUVELLEMENT[etat].lower()}.", "succes")
        return redirect(request.form.get("retour") if (request.form.get("retour") or "").startswith("/")
                        else url_for("document_fiche", did=d.id))

    @app.route("/document/<int:did>/supprimer", methods=["POST"])
    @gestion
    def document_supprimer(did):
        d = ou_404(Document, did)
        type_doc, nom = d.type_doc, d.personne.nom
        supprimer_pieces("document", d.id)
        db.session.delete(d)
        journaliser(f"{TYPES_DOCUMENT[type_doc]} supprimé", nom)
        db.session.commit()
        flash("Document supprimé.", "succes")
        return redirect(url_for("documents"))

    # ---------------- Billets d'avion ---------------- #

    def requete_billets():
        q = Billet.query.options(selectinload(Billet.personne)).join(Personne)
        quand = request.args.get("quand", "a_venir")
        if quand == "a_venir":
            q = q.filter(Billet.date_depart >= aujourdhui(), Billet.statut != "annule")
        elif quand == "passes":
            q = q.filter(Billet.date_depart < aujourdhui())
        if request.args.get("motif") in MOTIFS_BILLET:
            q = q.filter(Billet.motif == request.args["motif"])
        if (request.args.get("annee") or "").isdigit():
            a = int(request.args["annee"])
            q = q.filter(Billet.date_depart >= date(a, 1, 1), Billet.date_depart <= date(a, 12, 31))
        if (request.args.get("personne") or "").isdigit():
            q = q.filter(Billet.personne_id == int(request.args["personne"]))
        r = recherche()
        if r:
            like = f"%{r}%"
            q = q.filter(or_(Personne.nom.ilike(like), Billet.arrivee.ilike(like), Billet.depart.ilike(like),
                             Billet.compagnie.ilike(like), Billet.reservation.ilike(like)))
        return q.order_by(Billet.date_depart.asc() if quand == "a_venir" else Billet.date_depart.desc(), Billet.id)

    @app.route("/billets")
    @lecture
    def billets():
        q = requete_billets()
        total = sum(b.montant or 0 for b in q if b.statut != "annule")
        page = q.paginate(page=args_page(), per_page=PAR_PAGE, error_out=False)
        annees = sorted({b.date_depart.year for b in Billet.query.with_entities(Billet.date_depart)}
                        | {aujourdhui().year}, reverse=True)
        return render_template("billets.html", page=page, total=total, annees=annees, module=SOUS_MODULES["billets"])

    @app.route("/billets/export")
    @lecture
    def billets_export():
        lignes = [[b.personne.nom, b.personne.societe, MOTIFS_BILLET.get(b.motif, b.motif), b.depart, b.arrivee,
                   b.date_depart, b.date_retour, b.compagnie, b.reservation, b.classe,
                   STATUTS_BILLET.get(b.statut, b.statut), b.montant] for b in requete_billets()]
        return exporter_excel("billets", "Billets d'avion", [
            "Voyageur", "Société", "Motif", "Départ", "Destination", "Date de départ", "Date de retour", "Compagnie",
            "PNR", "Classe", "Statut", "Prix"], lignes, formats={5: "date", 6: "date", 11: "fcfa"},
            largeurs=[26, 18, 16, 14, 16, 13, 13, 18, 10, 12, 14, 14], total_col=11)

    def formulaire_billet(b=None):
        champs = F.champs_billet(options_personnes)
        if request.method == "POST":
            val, err, f = F.lire(champs)
            if val.get("date_retour") and val.get("date_depart") and val["date_retour"] < val["date_depart"]:
                err["date_retour"] = "Le retour ne peut pas précéder le départ."
            pieces, e = lire_pieces()
            if e:
                err["pieces"] = e
            if not err:
                nouveau = b is None
                if nouveau:
                    b = Billet(cree_par_id=current_user.id)
                    db.session.add(b)
                F.appliquer(champs, b, val)
                db.session.flush()
                rattacher(pieces, "billet", b.id)
                journaliser(f"Billet {'enregistré' if nouveau else 'modifié'}", b.personne.nom,
                            f"{b.trajet} le {b.date_depart:%d/%m/%Y}")
                db.session.commit()
                flash(f"Billet de {b.personne.nom} enregistré.", "succes")
                return redirect(url_for("billet_fiche", bid=b.id))
        else:
            f, err = F.depuis_objet(champs, b), {}
            if b is None and request.args.get("personne", "").isdigit():
                f["personne_id"] = request.args["personne"]
        compagnies = [c for (c,) in db.session.query(Billet.compagnie).filter(Billet.compagnie.isnot(None))
                      .distinct().order_by(Billet.compagnie)]
        return render_template("formulaire.html", champs=champs, f=f, err=err, objet=b, pieces_form=True,
                               titre="Modifier le billet" if b else "Nouveau billet d'avion",
                               listes={"compagnies": compagnies or ["Air France", "Brussels Airlines", "Ethiopian Airlines",
                                                                    "Turkish Airlines", "Emirates", "Royal Air Maroc",
                                                                    "Camair-Co", "ASKY"]},
                               ariane=[("Billets d'avion", url_for("billets"))],
                               retour=url_for("billet_fiche", bid=b.id) if b else url_for("billets"))

    @app.route("/billets/nouveau", methods=["GET", "POST"])
    @gestion
    def billet_nouveau():
        return formulaire_billet()

    @app.route("/billet/<int:bid>")
    @lecture
    def billet_fiche(bid):
        b = ou_404(Billet, bid)
        return render_template("billet_fiche.html", b=b, pieces=pieces_de("billet", b.id),
                               module=SOUS_MODULES["billets"])

    @app.route("/billet/<int:bid>/modifier", methods=["GET", "POST"])
    @gestion
    def billet_modifier(bid):
        return formulaire_billet(ou_404(Billet, bid))

    @app.route("/billet/<int:bid>/supprimer", methods=["POST"])
    @gestion
    def billet_supprimer(bid):
        b = ou_404(Billet, bid)
        supprimer_pieces("billet", b.id)
        journaliser("Billet supprimé", b.personne.nom, b.trajet)
        db.session.delete(b)
        db.session.commit()
        flash("Billet supprimé.", "succes")
        return redirect(url_for("billets"))

    # ---------------- Logements ---------------- #

    @app.route("/logements")
    @lecture
    def logements():
        q = Logement.query.options(selectinload(Logement.occupants), selectinload(Logement.loyers),
                                   selectinload(Logement.reparations))
        if request.args.get("statut") != "tous":
            q = q.filter(Logement.actif.is_(request.args.get("statut") != "inactifs"))
        if request.args.get("ville"):
            q = q.filter(Logement.ville == request.args["ville"])
        r = recherche()
        if r:
            like = f"%{r}%"
            q = q.filter(or_(Logement.nom.ilike(like), Logement.quartier.ilike(like), Logement.bailleur.ilike(like),
                             Logement.adresse.ilike(like)))
        liste = q.order_by(Logement.ville, Logement.nom).all()
        return render_template("logements.html", liste=liste,
                               total_loyers=sum(l.loyer or 0 for l in liste if l.actif))

    def formulaire_logement(l=None):
        champs = F.champs_logement()
        if l is not None:
            champs.append(F.C("actif", "Logement toujours loué par l'entreprise", "case"))
        if request.method == "POST":
            val, err, f = F.lire(champs)
            if val.get("bail_debut") and val.get("bail_fin") and val["bail_fin"] < val["bail_debut"]:
                err["bail_fin"] = "La fin du bail doit suivre son début."
            if val.get("loyer") and not val.get("loyers_suivis_depuis"):
                val["loyers_suivis_depuis"] = aujourdhui().replace(day=1)
            if not err:
                nouveau = l is None
                if nouveau:
                    l = Logement()
                    db.session.add(l)
                F.appliquer(champs, l, val)
                l.jour_echeance = val.get("jour_echeance") or 5
                l.periodicite = val.get("periodicite") or 1
                db.session.flush()
                journaliser(f"Logement {'créé' if nouveau else 'modifié'}", l.nom)
                db.session.commit()
                flash(f"Logement « {l.nom} » enregistré.", "succes")
                return redirect(url_for("logement_fiche", lid=l.id))
        else:
            f, err = F.depuis_objet(champs, l), {}
            if l is None:
                f["loyers_suivis_depuis"] = aujourdhui().strftime("%Y-%m")
        return render_template("formulaire.html", champs=champs, f=f, err=err, objet=l,
                               titre="Modifier le logement" if l else "Nouveau logement",
                               ariane=[("Logements", url_for("logements"))],
                               retour=url_for("logement_fiche", lid=l.id) if l else url_for("logements"))

    @app.route("/logements/nouveau", methods=["GET", "POST"])
    @gestion
    def logement_nouveau():
        return formulaire_logement()

    @app.route("/logement/<int:lid>/modifier", methods=["GET", "POST"])
    @gestion
    def logement_modifier(lid):
        return formulaire_logement(ou_404(Logement, lid))

    @app.route("/logement/<int:lid>")
    @lecture
    def logement_fiche(lid):
        l = ou_404(Logement, lid)
        debut_an = aujourdhui().replace(month=1, day=1)
        couts = dict(
            loyers=sum(x.montant for x in l.loyers if x.date_paiement >= debut_an),
            electricite=sum(x.montant for x in l.factures if x.type_energie == "electricite" and x.periode >= debut_an),
            eau=sum(x.montant for x in l.factures if x.type_energie == "eau" and x.periode >= debut_an),
            reparations=sum(x.cout or 0 for x in l.reparations if x.signale_le.date() >= debut_an),
        )
        return render_template("logement_fiche.html", l=l, couts=couts, dus=l.echeances_dues(),
                               prochaine=l.prochaine_echeance(), pieces=pieces_de("logement", l.id),
                               comptes=User.query.filter_by(logement_id=l.id, actif=True).all())

    # ---------------- Loyers ---------------- #

    @app.route("/loyers")
    @lecture
    def loyers():
        onglet = request.args.get("onglet", "suivi")
        q = Loyer.query.options(selectinload(Loyer.logement), selectinload(Loyer.cree_par))
        if (request.args.get("logement") or "").isdigit():
            q = q.filter(Loyer.logement_id == int(request.args["logement"]))
        page = q.order_by(Loyer.date_paiement.desc(), Loyer.id.desc()) \
            .paginate(page=args_page(), per_page=PAR_PAGE, error_out=False)
        debut_an = aujourdhui().replace(month=1, day=1)
        paye_an = db.session.query(func.coalesce(func.sum(Loyer.montant), 0)).filter(Loyer.date_paiement >= debut_an).scalar()
        return render_template("loyers.html", onglet=onglet, dus=loyers_dus(), a_venir=loyers_a_venir(45), page=page,
                               paye_an=paye_an, logements=options_logements(tous=True), module=SOUS_MODULES["loyers"])

    @app.route("/loyers/export")
    @lecture
    def loyers_export():
        lignes = [[x.logement.nom, x.logement.quartier, x.logement.bailleur, periode_libelle(x.periode_debut, x.mois),
                   x.montant, x.date_paiement, x.mode_paiement, x.reference, x.cree_par.nom if x.cree_par else ""]
                  for x in Loyer.query.options(selectinload(Loyer.logement)).order_by(Loyer.date_paiement.desc())]
        return exporter_excel("loyers", "Loyers payés", [
            "Logement", "Quartier", "Bailleur", "Période", "Montant", "Payé le", "Mode", "Référence", "Saisi par"],
            lignes, formats={4: "fcfa", 5: "date"}, largeurs=[28, 18, 24, 24, 16, 12, 14, 18, 20], total_col=4)

    @app.route("/loyers/payer", methods=["GET", "POST"])
    @gestion
    def loyer_payer():
        champs = F.champs_loyer(options_logements)
        if request.method == "POST":
            val, err, f = F.lire(champs)
            l = db.session.get(Logement, val["logement_id"]) if val.get("logement_id") else None
            if l and val.get("periode_debut"):
                if Loyer.query.filter_by(logement_id=l.id, periode_debut=val["periode_debut"]).first():
                    err["periode_debut"] = "Cette échéance est déjà enregistrée comme payée pour ce logement."
            pieces, e = lire_pieces()
            if e:
                err["pieces"] = e
            if not err:
                x = Loyer(cree_par_id=current_user.id, mois=l.periodicite or 1)
                F.appliquer(champs, x, val)
                db.session.add(x)
                db.session.flush()
                rattacher(pieces, "loyer", x.id)
                if not l.loyers_suivis_depuis or x.periode_debut < l.loyers_suivis_depuis:
                    l.loyers_suivis_depuis = x.periode_debut
                journaliser("Loyer payé", l.nom, f"{periode_libelle(x.periode_debut, x.mois)} · {_milliers(x.montant)} FCFA")
                db.session.commit()
                flash(f"Paiement du loyer enregistré : {l.nom}, {periode_libelle(x.periode_debut, x.mois)}.", "succes")
                return redirect(request.args.get("retour") if (request.args.get("retour") or "").startswith("/")
                                else url_for("loyers"))
        else:
            err = {}
            f = F.depuis_objet(champs, None, date_paiement=aujourdhui().strftime("%Y-%m-%d"))
            lid = request.args.get("logement", "")
            if lid.isdigit() and (l := db.session.get(Logement, int(lid))):
                f["logement_id"] = lid
                f["montant"] = _milliers(l.montant_echeance) if l.loyer else ""
                p = request.args.get("periode") or ((l.echeances_dues() or [None])[0] or l.prochaine_echeance() or [None])[0]
                if isinstance(p, date):
                    p = p.strftime("%Y-%m")
                f["periode_debut"] = p or ""
        return render_template("formulaire.html", champs=champs, f=f, err=err, objet=None, pieces_form=True,
                               titre="Enregistrer un paiement de loyer", bouton="Enregistrer le paiement",
                               aide="Pour un loyer trimestriel ou annuel, indiquez le premier mois de la période payée.",
                               ariane=[("Loyers", url_for("loyers"))], retour=url_for("loyers"))

    @app.route("/loyer/<int:xid>/supprimer", methods=["POST"])
    @gestion
    def loyer_supprimer(xid):
        x = ou_404(Loyer, xid)
        supprimer_pieces("loyer", x.id)
        journaliser("Paiement de loyer supprimé", x.logement.nom, periode_libelle(x.periode_debut, x.mois))
        db.session.delete(x)
        db.session.commit()
        flash("Paiement supprimé : l'échéance redevient due.", "succes")
        return redirect(request.referrer if (request.referrer or "").startswith(request.host_url) else url_for("loyers"))

    @app.route("/loyer/<int:xid>/pieces")
    @lecture
    def loyer_pieces(xid):
        x = ou_404(Loyer, xid)
        p = pieces_de("loyer", x.id)
        if not p:
            abort(404)
        return redirect(url_for("piece_voir", pid=p[0].id))

    # ---------------- Factures d'électricité et d'eau ---------------- #

    def type_energie_ou_404(t):
        if t not in TYPES_ENERGIE:
            abort(404)
        return t

    def requete_factures(type_energie):
        q = FactureEnergie.query.options(selectinload(FactureEnergie.logement)).join(Logement) \
            .filter(FactureEnergie.type_energie == type_energie)
        statut = request.args.get("statut", "a_payer")
        if statut == "a_payer":
            q = q.filter(FactureEnergie.date_paiement.is_(None))
        elif statut == "retard":
            q = q.filter(FactureEnergie.date_paiement.is_(None), FactureEnergie.date_echeance < aujourdhui())
        elif statut == "payees":
            q = q.filter(FactureEnergie.date_paiement.isnot(None))
        if (request.args.get("logement") or "").isdigit():
            q = q.filter(FactureEnergie.logement_id == int(request.args["logement"]))
        r = recherche()
        if r:
            like = f"%{r}%"
            q = q.filter(or_(Logement.nom.ilike(like), Logement.quartier.ilike(like), FactureEnergie.numero.ilike(like)))
        return q.order_by(FactureEnergie.periode.desc(), Logement.nom)

    @app.route("/factures/<type_energie>")
    @lecture
    def factures(type_energie):
        type_energie_ou_404(type_energie)
        q = requete_factures(type_energie)
        total = db.session.query(func.coalesce(func.sum(FactureEnergie.montant), 0)) \
            .filter(FactureEnergie.id.in_(q.with_entities(FactureEnergie.id))).scalar()
        page = q.paginate(page=args_page(), per_page=PAR_PAGE, error_out=False)
        imp = factures_impayees(type_energie).all()
        # Logements actifs sans facture saisie pour le mois dernier
        mois_dernier = mois_ajoutes(aujourdhui().replace(day=1), -1)
        avec = {lid for (lid,) in db.session.query(FactureEnergie.logement_id)
                .filter_by(type_energie=type_energie, periode=mois_dernier)}
        manquantes = [l for l in Logement.query.filter_by(actif=True).order_by(Logement.nom) if l.id not in avec]
        return render_template("factures.html", page=page, type_energie=type_energie, total=total,
                               module=SOUS_MODULES[type_energie], logements=options_logements(tous=True),
                               a_payer=sum(f.montant for f in imp),
                               en_retard=sum(1 for f in imp if f.niveau == "retard"), nb_a_payer=len(imp),
                               manquantes=manquantes, mois_dernier=mois_dernier)

    @app.route("/factures/<type_energie>/export")
    @lecture
    def factures_export(type_energie):
        type_energie_ou_404(type_energie)
        lignes = [[f.logement.nom, f.logement.quartier, mois_libelle(f.periode), f.numero,
                   float(f.consommation) if f.consommation is not None else None, f.montant, f.date_echeance,
                   f.date_paiement, {"payee": "Payée", "retard": "En retard", "a_payer": "À payer"}[f.niveau],
                   f.mode_paiement, f.reference] for f in requete_factures(type_energie)]
        lib = TYPES_ENERGIE[type_energie]
        return exporter_excel(f"factures_{type_energie}", f"Factures {lib.lower()}", [
            "Logement", "Quartier", "Mois", "N° facture", f"Consommation ({UNITES[type_energie]})", "Montant",
            "Échéance", "Payée le", "État", "Mode", "Référence"], lignes,
            formats={4: "#,##0.00", 5: "fcfa", 6: "date", 7: "date"},
            largeurs=[28, 18, 16, 16, 14, 16, 12, 12, 12, 14, 18], total_col=5)

    def formulaire_facture(type_energie, fe=None):
        champs = F.champs_facture(options_logements, type_energie)
        lib = TYPES_ENERGIE[type_energie]
        if request.method == "POST":
            val, err, f = F.lire(champs)
            if val.get("logement_id") and val.get("periode"):
                doublon = FactureEnergie.query.filter_by(logement_id=val["logement_id"], type_energie=type_energie,
                                                         periode=val["periode"])
                if fe is not None:
                    doublon = doublon.filter(FactureEnergie.id != fe.id)
                if doublon.first() and not request.form.get("confirmer_doublon"):
                    err["periode"] = ("Une facture existe déjà pour ce logement et ce mois. "
                                      "Cochez la case ci-dessous si c'est bien une seconde facture.")
            pieces, e = lire_pieces()
            if e:
                err["pieces"] = e
            if not err:
                nouveau = fe is None
                if nouveau:
                    fe = FactureEnergie(type_energie=type_energie, cree_par_id=current_user.id)
                    db.session.add(fe)
                F.appliquer(champs, fe, val)
                db.session.flush()
                rattacher(pieces, "facture", fe.id)
                journaliser(f"Facture {lib.lower()} {'enregistrée' if nouveau else 'modifiée'}", fe.logement.nom,
                            f"{mois_libelle(fe.periode)} · {_milliers(fe.montant)} FCFA")
                db.session.commit()
                flash(f"Facture {lib.lower()} enregistrée : {fe.logement.nom}, {mois_libelle(fe.periode)}.", "succes")
                return redirect(url_for("factures", type_energie=type_energie,
                                        statut="payees" if fe.payee else "a_payer"))
        else:
            err = {}
            f = F.depuis_objet(champs, fe)
            if fe is None:
                f["periode"] = mois_ajoutes(aujourdhui().replace(day=1), -1).strftime("%Y-%m")
                if (request.args.get("logement") or "").isdigit():
                    f["logement_id"] = request.args["logement"]
        return render_template("formulaire.html", champs=champs, f=f, err=err, objet=fe, pieces_form=True,
                               doublon=bool(err.get("periode") and "existe déjà" in err.get("periode", "")),
                               titre=f"Modifier la facture {lib.lower()}" if fe else f"Nouvelle facture {lib.lower()}",
                               aide=f"Fournisseur : {FOURNISSEURS[type_energie]}. Joignez la facture scannée ou photographiée.",
                               ariane=[(lib, url_for("factures", type_energie=type_energie))],
                               retour=url_for("factures", type_energie=type_energie))

    @app.route("/factures/<type_energie>/nouvelle", methods=["GET", "POST"])
    @gestion
    def facture_nouvelle(type_energie):
        return formulaire_facture(type_energie_ou_404(type_energie))

    @app.route("/facture/<int:fid>/modifier", methods=["GET", "POST"])
    @gestion
    def facture_modifier(fid):
        fe = ou_404(FactureEnergie, fid)
        return formulaire_facture(fe.type_energie, fe)

    @app.route("/facture/<int:fid>/payer", methods=["POST"])
    @gestion
    def facture_payer(fid):
        fe = ou_404(FactureEnergie, fid)
        try:
            d = F.lire_date(request.form.get("date_paiement")) or aujourdhui()
        except ValueError:
            d = aujourdhui()
        fe.date_paiement = d
        fe.mode_paiement = (request.form.get("mode_paiement") or fe.mode_paiement or "")[:30] or None
        fe.reference = (request.form.get("reference") or fe.reference or "")[:80] or None
        journaliser(f"Facture {fe.type_libelle.lower()} payée", fe.logement.nom, mois_libelle(fe.periode))
        db.session.commit()
        flash(f"Facture {fe.type_libelle.lower()} de {fe.logement.nom} marquée payée.", "succes")
        return redirect(request.referrer if (request.referrer or "").startswith(request.host_url)
                        else url_for("factures", type_energie=fe.type_energie))

    @app.route("/facture/<int:fid>/supprimer", methods=["POST"])
    @gestion
    def facture_supprimer(fid):
        fe = ou_404(FactureEnergie, fid)
        t = fe.type_energie
        supprimer_pieces("facture", fe.id)
        journaliser(f"Facture {fe.type_libelle.lower()} supprimée", fe.logement.nom, mois_libelle(fe.periode))
        db.session.delete(fe)
        db.session.commit()
        flash("Facture supprimée.", "succes")
        return redirect(url_for("factures", type_energie=t))

    @app.route("/facture/<int:fid>/piece")
    @lecture
    def facture_piece(fid):
        p = pieces_de("facture", ou_404(FactureEnergie, fid).id)
        return redirect(url_for("piece_voir", pid=p[0].id)) if p else abort(404)

    # ---------------- Réparations ---------------- #

    def requete_reparations():
        q = visibles_reparations().join(Logement)
        statut = request.args.get("statut", "ouvertes")
        if statut == "ouvertes":
            q = q.filter(Reparation.statut.in_(REPARATIONS_OUVERTES))
        elif statut in STATUTS_REPARATION:
            q = q.filter(Reparation.statut == statut)
        if request.args.get("urgence") == "urgente":
            q = q.filter(Reparation.urgence == "urgente")
        if (request.args.get("logement") or "").isdigit():
            q = q.filter(Reparation.logement_id == int(request.args["logement"]))
        if request.args.get("categorie") in CATEGORIES_REPARATION:
            q = q.filter(Reparation.categorie == request.args["categorie"])
        r = recherche()
        if r:
            like = f"%{r}%"
            q = q.filter(or_(Reparation.titre.ilike(like), Reparation.description.ilike(like),
                             Logement.nom.ilike(like), Reparation.intervenant.ilike(like)))
        return q.order_by((Reparation.urgence == "urgente").desc(), Reparation.signale_le.desc())

    @app.route("/reparations")
    @login_required
    def reparations():
        q = requete_reparations()
        page = q.paginate(page=args_page(), per_page=PAR_PAGE, error_out=False)
        base = visibles_reparations()
        compte = {s: base.filter(Reparation.statut == s).count() for s in STATUTS_REPARATION}
        cout = db.session.query(func.coalesce(func.sum(Reparation.cout), 0)) \
            .filter(Reparation.id.in_(q.with_entities(Reparation.id))).scalar()
        return render_template("reparations.html", page=page, compte=compte, cout=cout,
                               logements=options_logements(tous=True), module=SOUS_MODULES["reparations"],
                               CATEGORIES=CATEGORIES_REPARATION)

    @app.route("/reparations/export")
    @lecture
    def reparations_export():
        lignes = [[r.reference, r.logement.nom, r.titre, r.categorie, URGENCES.get(r.urgence), r.statut_libelle,
                   r.signale_le, r.signale_par.nom if r.signale_par else "", r.intervenant, r.date_intervention, r.cout,
                   r.cloturee_le, r.jours_ouverte] for r in requete_reparations()]
        return exporter_excel("reparations", "Réparations", [
            "Réf.", "Logement", "Problème", "Catégorie", "Urgence", "Statut", "Signalée le", "Signalée par",
            "Intervenant", "Intervention le", "Coût", "Clôturée le", "Jours"], lignes,
            formats={6: "dateheure", 9: "date", 10: "fcfa", 11: "dateheure"},
            largeurs=[14, 26, 36, 16, 10, 16, 16, 20, 22, 13, 14, 16, 8], total_col=10)

    @app.route("/reparations/nouvelle", methods=["GET", "POST"])
    @login_required
    def reparation_nouvelle():
        if current_user.role == "direction":
            abort(403)
        occ = current_user.occupant
        if occ and not current_user.logement_id:
            flash("Votre compte n'est rattaché à aucun logement : contactez le gestionnaire.", "erreur")
            return redirect(url_for("mon_logement"))
        champs = F.champs_reparation(None if occ else options_logements)
        if request.method == "POST":
            val, err, f = F.lire(champs)
            pieces, e = lire_pieces()
            if e:
                err["pieces"] = e
            if not err:
                r = Reparation(signale_par_id=current_user.id,
                               logement_id=current_user.logement_id if occ else val["logement_id"])
                F.appliquer(champs, r, val)
                db.session.add(r)
                db.session.flush()
                db.session.add(SuiviReparation(reparation_id=r.id, user_id=current_user.id, statut="signalee",
                                               texte="Demande signalée."))
                rattacher(pieces, "reparation", r.id)
                journaliser("Réparation signalée", r.logement.nom, r.titre)
                db.session.commit()
                flash("Demande de réparation envoyée. Le gestionnaire la voit dès maintenant.", "succes")
                return redirect(url_for("reparation_fiche", rid=r.id))
        else:
            f, err = F.depuis_objet(champs, None), {}
            if (request.args.get("logement") or "").isdigit():
                f["logement_id"] = request.args["logement"]
        return render_template("formulaire.html", champs=champs, f=f, err=err, objet=None, pieces_form=True,
                               titre="Signaler un problème", bouton="Envoyer la demande", photos=True,
                               aide=(f"Logement : {current_user.logement.libelle}" if occ else None),
                               ariane=[("Réparations", url_for("reparations"))], retour=url_for("reparations"))

    @app.route("/reparation/<int:rid>")
    @login_required
    def reparation_fiche(rid):
        r = ou_404(Reparation, rid)
        if not peut_voir_reparation(r):
            abort(403)
        champs = F.champs_suivi()
        f = F.depuis_objet(champs, r, texte="")
        return render_template("reparation_fiche.html", r=r, pieces=pieces_de("reparation", r.id), champs=champs,
                               f=f, err={}, module=SOUS_MODULES["reparations"])

    @app.route("/reparation/<int:rid>/suivi", methods=["POST"])
    @login_required
    def reparation_suivi(rid):
        r = ou_404(Reparation, rid)
        if not peut_voir_reparation(r) or current_user.role == "direction":
            abort(403)
        pieces, e = lire_pieces()
        if current_user.gere:
            champs = F.champs_suivi()
            val, err, f = F.lire(champs)
            if e:
                err["pieces"] = e
            if val.get("statut") == "terminee" and not val.get("texte") and r.statut != "terminee":
                err["texte"] = "Indiquez ce qui a été fait pour clôturer la réparation."
            if err:
                return render_template("reparation_fiche.html", r=r, pieces=pieces_de("reparation", r.id),
                                       champs=champs, f=f, err=err, module=SOUS_MODULES["reparations"]), 422
            ancien = r.statut
            for k in ("intervenant", "date_intervention", "cout"):
                setattr(r, k, val.get(k))
            if val["statut"] != ancien:
                r.statut, r.statut_depuis = val["statut"], now()
                r.cloturee_le = now() if val["statut"] in ("terminee", "annulee") else None
            if val["statut"] != ancien or val.get("texte") or pieces:
                db.session.add(SuiviReparation(reparation_id=r.id, user_id=current_user.id,
                                               statut=val["statut"] if val["statut"] != ancien else None,
                                               texte=val.get("texte") or (f"{len(pieces)} photo(s) / document(s) ajouté(s)." if pieces else None)))
            journaliser("Réparation mise à jour", r.reference,
                        f"{STATUTS_REPARATION[ancien]} → {r.statut_libelle}" if r.statut != ancien else "détails / commentaire")
        else:
            # Occupant : commentaire et photos uniquement
            texte = (request.form.get("texte") or "").strip()[:4000]
            if e or (not texte and not pieces):
                flash(e or "Écrivez un commentaire ou ajoutez une photo.", "erreur")
                return redirect(url_for("reparation_fiche", rid=r.id) + "#suivi")
            db.session.add(SuiviReparation(reparation_id=r.id, user_id=current_user.id,
                                           texte=texte or f"{len(pieces)} photo(s) ajoutée(s)."))
        rattacher(pieces, "reparation", r.id)
        db.session.commit()
        flash("Suivi enregistré.", "succes")
        return redirect(url_for("reparation_fiche", rid=r.id))

    @app.route("/mon-logement")
    @login_required
    def mon_logement():
        if not current_user.occupant:
            return redirect(url_for("accueil"))
        l = current_user.logement
        liste = visibles_reparations().order_by(Reparation.signale_le.desc()).all() if l else []
        return render_template("mon_logement.html", l=l, liste=liste)

    # ---------------- Pièces jointes ---------------- #

    def objet_piece(objet, oid):
        modele = {"personne": Personne, "document": Document, "billet": Billet, "logement": Logement,
                  "loyer": Loyer, "facture": FactureEnergie, "reparation": Reparation}.get(objet)
        return ou_404(modele, oid) if modele else abort(404)

    def peut_voir_piece(p):
        if not current_user.occupant:
            return True
        return p.objet == "reparation" and (r := db.session.get(Reparation, p.objet_id)) and peut_voir_reparation(r)

    @app.route("/pieces/<objet>/<int:oid>", methods=["POST"])
    @gestion
    def piece_ajouter(objet, oid):
        if objet not in OBJETS_PIECE:
            abort(404)
        objet_piece(objet, oid)
        pieces, e = lire_pieces()
        if e or not pieces:
            flash(e or "Choisissez au moins un fichier.", "erreur")
        else:
            rattacher(pieces, objet, oid)
            journaliser("Pièce ajoutée", f"{objet} {oid}", ", ".join(p.nom_fichier for p in pieces))
            db.session.commit()
            flash(f"{len(pieces)} pièce(s) ajoutée(s).", "succes")
        retour = request.form.get("retour", "")
        return redirect(retour if retour.startswith("/") and not retour.startswith("//") else url_for("accueil"))

    @app.route("/piece/<int:pid>")
    @login_required
    def piece_voir(pid):
        p = ou_404(Piece, pid)
        if not peut_voir_piece(p):
            abort(403)
        resp = send_file(io.BytesIO(p.contenu), mimetype=p.mime, download_name=p.nom_fichier,
                         as_attachment=request.args.get("telecharger") == "1", max_age=0)
        resp.headers["Cache-Control"] = "private, no-store"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        if not p.est_pdf:
            resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
        resp.piece_jointe = True
        return resp

    @app.route("/piece/<int:pid>/supprimer", methods=["POST"])
    @gestion
    def piece_supprimer(pid):
        p = ou_404(Piece, pid)
        journaliser("Pièce supprimée", f"{p.objet} {p.objet_id}", p.nom_fichier)
        db.session.delete(p)
        db.session.commit()
        flash("Pièce supprimée.", "succes")
        retour = request.form.get("retour", "")
        return redirect(retour if retour.startswith("/") and not retour.startswith("//") else url_for("accueil"))

    # ---------------- Administration ---------------- #

    @app.route("/utilisateurs")
    @roles("admin")
    def utilisateurs():
        liste = User.query.options(selectinload(User.logement)).order_by(User.actif.desc(), User.nom).all()
        return render_template("utilisateurs.html", liste=liste)

    def formulaire_utilisateur(u=None):
        champs = F.champs_utilisateur(lambda: options_logements(tous=True), creation=u is None)
        if u is not None:
            champs.append(F.C("actif", "Compte actif", "case"))
        if request.method == "POST":
            val, err, f = F.lire(champs)
            if val.get("email"):
                autre = User.query.filter(func.lower(User.email) == val["email"])
                if u is not None:
                    autre = autre.filter(User.id != u.id)
                if autre.first():
                    err["email"] = "Un compte existe déjà avec cette adresse."
            if val.get("mot_de_passe") and len(val["mot_de_passe"]) < 10:
                err["mot_de_passe"] = "10 caractères minimum."
            if val.get("role") == "occupant" and not val.get("logement_id"):
                err["logement_id"] = "Choisissez le logement de l'occupant."
            if u is not None and u.id == current_user.id and (val.get("role") != "admin" or not val.get("actif")):
                err["role"] = "Vous ne pouvez pas retirer vos propres droits d'administrateur."
            if not err:
                nouveau = u is None
                if nouveau:
                    u = User()
                    db.session.add(u)
                F.appliquer(champs, u, val, exclure=("mot_de_passe",))
                if val["role"] != "occupant":
                    u.logement_id = None
                if val.get("mot_de_passe"):
                    u.set_password(val["mot_de_passe"])
                db.session.flush()
                journaliser(f"Compte {'créé' if nouveau else 'modifié'}", u.email, ROLES[u.role])
                db.session.commit()
                flash(f"Compte de {u.nom} enregistré.", "succes")
                return redirect(url_for("utilisateurs"))
        else:
            f, err = F.depuis_objet(champs, u), {}
            f["mot_de_passe"] = ""
        return render_template("formulaire.html", champs=champs, f=f, err=err, objet=u,
                               titre="Modifier le compte" if u else "Nouveau compte",
                               ariane=[("Utilisateurs", url_for("utilisateurs"))], retour=url_for("utilisateurs"),
                               aide="Avec le portail Futura, les accès se donnent plutôt depuis le portail "
                                    "(même connexion pour tous les modules).")

    @app.route("/utilisateurs/nouveau", methods=["GET", "POST"])
    @roles("admin")
    def utilisateur_nouveau():
        return formulaire_utilisateur()

    @app.route("/utilisateur/<int:uid>/modifier", methods=["GET", "POST"])
    @roles("admin")
    def utilisateur_modifier(uid):
        return formulaire_utilisateur(ou_404(User, uid))

    @app.route("/parametres", methods=["GET", "POST"])
    @roles("admin")
    def parametres():
        champs = [F.C("alerte_documents_jours", "Alerte avant l'expiration d'un visa, d'une carte de séjour ou d'un passeport",
                      "entier", requis=True, min=7, max=365, aide="En jours. Par défaut : 30 jours."),
                  F.C("alerte_bail_jours", "Alerte avant la fin d'un bail", "entier", requis=True, min=7, max=365,
                      aide="En jours.")]
        if request.method == "POST":
            val, err, f = F.lire(champs)
            if not err:
                for k, v in val.items():
                    p = db.session.get(Parametre, k) or Parametre(cle=k, valeur="")
                    p.valeur, p.updated_by_id = str(v), current_user.id
                    db.session.add(p)
                journaliser("Réglages des alertes modifiés", "", ", ".join(f"{k} = {v}" for k, v in val.items()))
                db.session.commit()
                flash("Réglages enregistrés.", "succes")
                return redirect(url_for("parametres"))
        else:
            f, err = {"alerte_documents_jours": seuil_docs(), "alerte_bail_jours": seuil_bail()}, {}
        return render_template("formulaire.html", champs=champs, f=f, err=err, objet=None, titre="Réglages des alertes",
                               ariane=[("Administration", None)], retour=url_for("tableau_de_bord"))

    @app.route("/journal")
    @roles("admin")
    def journal():
        page = Journal.query.options(selectinload(Journal.user)).order_by(Journal.date.desc()) \
            .paginate(page=args_page(), per_page=50, error_out=False)
        return render_template("journal.html", page=page)


# --------------------------------------------------------------------------- #
#  Commandes
# --------------------------------------------------------------------------- #

def register_cli(app):

    @app.cli.command("creer-admin")
    @click.argument("email")
    @click.argument("nom")
    @click.password_option()
    def creer_admin(email, nom, password):
        """Crée (ou réactive) un compte administrateur."""
        u = User.query.filter_by(email=email.lower()).first() or User(email=email.lower())
        u.nom, u.role, u.actif = nom, "admin", True
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        click.echo(f"Administrateur {u.email} prêt.")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Données de démonstration (mot de passe de tous les comptes : Futura2026)."""
        from demo import charger
        charger()
        click.echo("Données de démonstration chargées. Mot de passe : Futura2026")
