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
    """Portail de l'utilisateur connecté : uniquement les modules qui lui sont attribués.

    Avant connexion, aucun module n'est proposé : une seule page de connexion pour tout le monde.
    """
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    if not M.externes_configures():
        return redirect(url_for("tableau_bord.index"))
    return render_template("modules/portail.html", modules=M.modules_pour(current_user.email, current_user))


@bp.get("/<code>")
def ouvrir(code):
    m = M.module(code) or abort(404)
    if not current_user.is_authenticated:
        # Connexion unique : après la connexion, le module demandé s'ouvre (si le compte y a accès)
        return redirect(url_for("auth.login", next=url_for("modules.ouvrir", code=code)))
    if m.get("interne"):
        return redirect(url_for("tableau_bord.index"))
    if code not in {x["code"] for x in M.modules_pour(current_user.email, current_user)}:
        flash(f"Le module « {m['nom']} » ne vous est pas attribué. Demandez l'accès à l'administrateur.", "info")
        return redirect(url_for("modules.portail"))
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
    # Comptes déjà créés dans chaque module (rôle géré localement), pour les afficher et les recopier
    locaux = {m["code"]: M.comptes_module(m["code"]) for m in externes if all(M._conf(m["code"]))}

    def role_source(u, code):
        """Rôle d'un compte dans un module : celui choisi au portail, sinon celui du module lui-même."""
        r = actuel.get(u.id, {}).get(code)
        if r:
            return r
        c = (locaux.get(code) or {}).get(u.email.strip().lower())
        return c["role"] if c and c.get("actif") else None

    if request.method == "POST":
        nouveau, changes = {}, []
        for u in comptes:
            for m in externes:
                r = request.form.get(f"r_{u.id}_{m['code']}", "")
                if r and r not in m["roles"]:
                    continue
                if r:
                    nouveau.setdefault(u.id, {})[m["code"]] = r
        # Recopie : mêmes accès d'un module vers un autre pour les comptes cochés
        copie = None
        if request.form.get("action") == "copier":
            src, cible = request.form.get("source", ""), request.form.get("cible", "")
            noms = {m["code"]: m["nom"] for m in externes}
            roles_de = {m["code"]: m["roles"] for m in externes}
            ids = {int(x) for x in request.form.getlist("sel") if x.isdigit()}
            if src not in noms or cible not in noms or src == cible:
                flash("Choisissez deux modules différents pour recopier les accès.", "erreur")
                return redirect(request.path)
            if not ids:
                flash("Cochez au moins un compte à recopier.", "erreur")
                return redirect(request.path)
            copies, sans_acces, sans_equiv = [], [], []
            for u in comptes:
                if u.id not in ids:
                    continue
                r_src = nouveau.get(u.id, {}).get(src) or role_source(u, src)
                if not r_src:
                    sans_acces.append(u.nom_affiche)
                    continue
                r_cible = M.role_equivalent(r_src, cible)
                if not r_cible:
                    sans_equiv.append(f"{u.nom_affiche} ({roles_de[src].get(r_src, r_src)})")
                    continue
                nouveau.setdefault(u.id, {})[cible] = r_cible
                copies.append(u.nom_affiche)
            copie = (noms[src], noms[cible], copies, sans_acces, sans_equiv)
        for u in comptes:
            for m in externes:
                avant, apres = actuel.get(u.id, {}).get(m["code"], ""), nouveau.get(u.id, {}).get(m["code"], "")
                if avant != apres:
                    changes.append(f"{u.email} · {m['nom']} : {m['roles'].get(avant, 'aucun')} → {m['roles'].get(apres, 'aucun')}")
        # Les comptes désactivés gardent leur réglage (au cas où ils seraient réactivés)
        ids_actifs = {u.id for u in comptes}
        for uid, v in actuel.items():
            if uid not in ids_actifs:
                nouveau[uid] = v
        M.enregistrer_acces_centraux(nouveau, current_user.id)
        for c in changes:
            journaliser("acces_module", c)
        db.session.commit()
        if copie:
            src, cible, copies, sans_acces, sans_equiv = copie
            msg = f"Accès recopiés de {src} vers {cible} : {len(copies)} compte(s)."
            if sans_acces:
                msg += f" Sans accès dans {src} (non recopiés) : {', '.join(sans_acces)}."
            if sans_equiv:
                msg += f" Rôle sans équivalent dans {cible}, à choisir à la main : {', '.join(sans_equiv)}."
            flash(msg, "succes" if copies else "info")
        else:
            flash(f"Accès aux modules enregistrés ({len(changes)} changement(s)). Ils s'appliquent à la prochaine "
                  "ouverture du module depuis le portail.", "succes")
        return redirect(request.path)
    return render_template("modules/acces.html", modules=externes, comptes=comptes, actuel=actuel, locaux=locaux)


@bp.post("/api/annuaire")
@csrf.exempt                          # appel serveur à serveur, authentifié par signature HMAC
def api_annuaire():
    """Annuaire commun : un module cherche une personne dans le personnel KPI et les comptes des autres modules.

    En-têtes : X-Futura-Module, X-Futura-Horodatage, X-Futura-Signature. Corps JSON : {"q": "texte"}.
    Réponse : {"personnes": [{nom, prenom, matricule, poste, email, telephone, entite, source}]} (30 au plus).
    """
    from sqlalchemy import or_
    from ..models import Employe
    code = request.headers.get("X-Futura-Module", "")
    corps = request.get_data(cache=True)
    if not M.module(code) or M.module(code).get("interne") or not M.verifier_signature_module(
            code, request.headers.get("X-Futura-Horodatage", ""), request.headers.get("X-Futura-Signature", ""), corps):
        return jsonify(erreur="Demande non autorisée."), 403
    q = str((request.get_json(silent=True) or {}).get("q") or "").strip()[:80]
    if len(q) < 2:
        return jsonify(personnes=[])
    like = f"%{q}%"
    res, vus = [], set()
    employes = db.session.scalars(select(Employe).where(Employe.actif.is_(True), or_(
        Employe.nom.ilike(like), Employe.prenom.ilike(like), Employe.matricule.ilike(like), Employe.email.ilike(like),
        (Employe.prenom + " " + Employe.nom).ilike(like))).order_by(Employe.nom).limit(30))
    for e in employes:
        res.append(dict(nom=e.nom, prenom=e.prenom or "", matricule=e.matricule, poste=e.poste or "",
                        email=(e.email or "").lower(), telephone=e.telephone or "",
                        entite=e.entite.nom if e.entite else "", source="Performance KPI"))
        if e.email:
            vus.add(e.email.strip().lower())
    ql = q.lower()
    for m in M.externes_configures():
        if m["code"] == code or len(res) >= 30:
            continue
        for email, c in (M.comptes_module(m["code"]) or {}).items():
            if email in vus or not c.get("actif"):
                continue
            if ql in email or ql in (c.get("nom") or "").lower():
                vus.add(email)
                res.append(dict(nom=c.get("nom") or email, prenom="", matricule="", poste=m["roles"].get(c.get("role"), ""),
                                email=email, telephone="", entite="", source=m["nom"]))
    return jsonify(personnes=res[:30])


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
