"""Entités du groupe : changement d'onglet, logo, création / modification (RH et superadmins),
accès des comptes par entité avec un rôle propre à chacune."""
import hashlib

from flask import (Blueprint, abort, flash, make_response, redirect,
                   render_template, request, session, url_for)
from flask_login import current_user, login_required
from sqlalchemy import func, select

from ..extensions import db
from ..models import (AccesEntite, Employe, Entite, Role, RoleEntite,
                      Utilisateur)
from ..permissions import perimetre, rh_requis
from ..services.audit import journaliser
from ..services.entites import (cond_entite, couleur_dominante, id_effectif,
                                roles_par_entite)

bp = Blueprint("entites", __name__)

TYPES_LOGO = {"image/png", "image/jpeg", "image/webp"}
TAILLE_LOGO_MAX = 1024 * 1024  # 1 Mo


@bp.get("/entite/<int:entite_id>")
@login_required
def basculer(entite_id):
    """Onglet : passe à une autre entité (uniquement parmi celles accessibles)."""
    if entite_id not in roles_par_entite(current_user):
        abort(403)
    session["entite_id"] = entite_id
    return redirect(url_for("tableau_bord.index"))


@bp.get("/entite/<int:entite_id>/logo")
@login_required
def logo(entite_id):
    e = db.session.get(Entite, entite_id) or abort(404)
    if not e.logo:
        abort(404)
    etag = hashlib.sha1(e.logo).hexdigest()[:16]
    if request.if_none_match and etag in request.if_none_match:
        return "", 304
    resp = make_response(e.logo)
    resp.headers["Content-Type"] = e.logo_mime or "image/png"
    resp.headers["Cache-Control"] = "private, max-age=86400"
    resp.headers["Content-Security-Policy"] = "default-src 'none'"
    resp.set_etag(etag)
    return resp


def _lire_logo(erreurs):
    f = request.files.get("logo")
    if not f or not f.filename:
        return None, None
    contenu = f.read()
    if f.mimetype not in TYPES_LOGO:
        erreurs["logo"] = "Format accepté : PNG, JPG ou WEBP (le SVG n'est pas accepté pour des raisons de sécurité)."
        return None, None
    if len(contenu) > TAILLE_LOGO_MAX:
        erreurs["logo"] = "Logo trop lourd : 1 Mo maximum."
        return None, None
    try:
        from PIL import Image
        import io
        Image.open(io.BytesIO(contenu)).verify()
    except Exception:
        erreurs["logo"] = "Ce fichier n'est pas une image valide."
        return None, None
    return contenu, f.mimetype


@bp.route("/parametres/entites", methods=["GET", "POST"])
@login_required
@rh_requis
def gestion():
    erreurs, donnees, edition = {}, {}, None
    if request.method == "POST":
        action = request.form.get("action", "enregistrer")
        e = db.session.get(Entite, request.form.get("id", type=int) or 0)
        if action == "basculer" and e:
            if e.principale:
                flash("L'entité principale ne peut pas être archivée.", "erreur")
            else:
                e.actif = not e.actif
                journaliser("entite_statut", e.nom, "active" if e.actif else "archivée")
                db.session.commit()
                flash(f"Entité « {e.nom} » {'réactivée' if e.actif else 'archivée'}.", "succes")
            return redirect(request.path)
        if action == "retirer_logo" and e:
            e.logo, e.logo_mime = None, None
            journaliser("entite_logo_retire", e.nom)
            db.session.commit()
            flash("Logo retiré.", "succes")
            return redirect(url_for("entites.gestion", modifier=e.id))

        donnees = {k: (request.form.get(k) or "").strip() for k in ("nom", "code", "couleur")}
        code = "".join(c for c in donnees["code"].upper() if c.isalnum())[:12]
        if not donnees["nom"]:
            erreurs["nom"] = "Le nom est obligatoire."
        elif db.session.scalar(select(Entite.id).where(func.lower(Entite.nom) == donnees["nom"].lower(),
                                                       Entite.id != (e.id if e else -1))):
            erreurs["nom"] = "Une entité porte déjà ce nom."
        if not code:
            erreurs["code"] = "Le code est obligatoire (lettres et chiffres, ex. BSB)."
        elif db.session.scalar(select(Entite.id).where(Entite.code == code, Entite.id != (e.id if e else -1))):
            erreurs["code"] = "Ce code est déjà utilisé."
        couleur = donnees["couleur"]
        if couleur and not (len(couleur) == 7 and couleur.startswith("#")
                            and all(c in "0123456789abcdefABCDEF" for c in couleur[1:])):
            erreurs["couleur"] = "Couleur invalide (format #RRGGBB)."
        contenu, mime = _lire_logo(erreurs)
        if not erreurs:
            nouveau = e is None
            e = e or Entite(principale=False, actif=True)
            e.nom, e.code = donnees["nom"], code
            if contenu:
                e.logo, e.logo_mime = contenu, mime
            # Charte : couleur choisie, sinon proposée automatiquement à partir du logo
            if request.form.get("couleur_auto") == "1" and contenu:
                couleur = couleur_dominante(contenu) or couleur
            e.couleur = (couleur or e.couleur or "#213E70").upper()
            if nouveau:
                db.session.add(e)
                db.session.flush()
                from ..cli import creer_criteres_defaut
                creer_criteres_defaut(e.id)  # chaque entité démarre avec sa propre grille KPI
            journaliser("entite_" + ("creee" if nouveau else "modifiee"), e.nom, e.code)
            db.session.commit()
            if nouveau:
                session["entite_id"] = e.id
                flash(f"Entité « {e.nom} » créée avec la grille KPI par défaut. Vous êtes maintenant dans "
                      "son espace : ajoutez ses départements, projets et son personnel.", "succes")
                return redirect(url_for("tableau_bord.index"))
            flash(f"Entité « {e.nom} » enregistrée.", "succes")
            return redirect(request.path)
        edition = e
    elif request.args.get("modifier", type=int):
        edition = db.session.get(Entite, request.args.get("modifier", type=int)) or abort(404)
        donnees = {"nom": edition.nom, "code": edition.code, "couleur": edition.couleur}

    entites = db.session.scalars(select(Entite).order_by(Entite.actif.desc(), Entite.principale.desc(),
                                                         Entite.nom)).all()
    effectifs = {e.id: db.session.scalar(select(func.count(Employe.id)).where(
        Employe.actif.is_(True), cond_entite(Employe.entite_id, e.id))) for e in entites}
    return render_template("parametres/entites.html", entites=entites, effectifs=effectifs,
                           edition=edition, donnees=donnees, erreurs=erreurs)


@bp.route("/parametres/utilisateurs/<int:utilisateur_id>/acces", methods=["GET", "POST"])
@login_required
@rh_requis
def acces(utilisateur_id):
    """Entités accessibles à un compte, avec le rôle dans chacune."""
    u = db.session.get(Utilisateur, utilisateur_id) or abort(404)
    if u.est_rh:
        flash("Ce compte est RH (ou superadmin) : il voit déjà toutes les entités.", "info")
        return redirect(url_for("parametres.utilisateurs"))
    entites = db.session.scalars(select(Entite).where(Entite.actif.is_(True))
                                 .order_by(Entite.principale.desc(), Entite.nom)).all()
    rattachement = id_effectif(u.employe.entite_id) if u.employe else None
    if request.method == "POST":
        actuels = {a.entite_id: a for a in u.acces}
        changements = []
        for ent in entites:
            role = request.form.get(f"role_{ent.id}", "")
            a = actuels.get(ent.id)
            if role in RoleEntite.LIBELLES:
                if a and a.role != role:
                    changements.append(f"{ent.code}:{a.role}→{role}")
                    a.role = role
                elif not a:
                    u.acces.append(AccesEntite(entite_id=ent.id, role=role))
                    changements.append(f"{ent.code}:+{role}")
            elif a:
                u.acces.remove(a)
                changements.append(f"{ent.code}:retiré")
        journaliser("compte_acces_entites", u.email, ", ".join(changements) or "aucun changement")
        db.session.commit()
        flash(f"Accès de {u.nom_affiche} mis à jour.", "succes")
        return redirect(url_for("parametres.utilisateurs"))
    roles_effectifs = roles_par_entite(u)
    explicites = {a.entite_id: a.role for a in u.acces}
    return render_template("parametres/acces.html", u=u, entites=entites, explicites=explicites,
                           roles_effectifs=roles_effectifs, rattachement=rattachement,
                           roles=RoleEntite.LIBELLES)
