"""Messagerie interne : conversations privées, groupes de département, supervision (superadmin)."""
from __future__ import annotations

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required
from sqlalchemy import select

from ..extensions import db
from ..filters import FUSEAU
from ..models import Departement, Utilisateur
from ..services import chat as C
from ..services.audit import journaliser

bp = Blueprint("chat", __name__, url_prefix="/messages")


@bp.before_request
def _actif():
    if not C.actif():
        abort(404)


def _conv_ou_404(conv_id: int, lecture_admin: bool = False):
    from ..chat_models import Conversation
    conv = db.session.get(Conversation, conv_id) or abort(404)
    membre = next((m for m in conv.membres if m.utilisateur_id == current_user.id), None)
    if membre is None and not (lecture_admin and current_user.est_superadmin):
        abort(404)                       # on ne révèle pas l'existence de la conversation
    return conv, membre


def _heure(dt) -> str:
    from datetime import datetime, timezone
    loc = dt.replace(tzinfo=timezone.utc).astimezone(FUSEAU)
    auj = datetime.now(FUSEAU).date()
    return f"{loc:%H:%M}" if loc.date() == auj else f"{loc:%d/%m %H:%M}"


def _messages_json(conv, apres: int, noms):
    from ..chat_models import Message
    q = select(Message).where(Message.conversation_id == conv.id, Message.id > apres).order_by(Message.id)
    msgs = db.session.scalars(q.limit(200)).all()
    manquants = {m.auteur_id for m in msgs} - set(noms)
    noms.update(C.comptes(manquants))
    return [{"id": m.id, "moi": m.auteur_id == current_user.id, "auteur": C.nom(noms.get(m.auteur_id)),
             "initiales": C.initiales(noms.get(m.auteur_id)), "heure": _heure(m.created_at),
             "texte": "" if m.supprime else m.texte, "supprime": m.supprime} for m in msgs]


# ------------------------------------------------------------------ pages
@bp.get("/")
@bp.get("/<int:conv_id>")
@login_required
def index(conv_id=None):
    from ..chat_models import Message
    convs = C.mes_conversations(current_user)
    non_lus = C.non_lus_par_conversation(current_user)
    ids = {m.utilisateur_id for c in convs for m in c.membres}
    conv = membre = None
    if conv_id:
        conv, membre = _conv_ou_404(conv_id)
        ids |= {m.utilisateur_id for m in conv.membres}
    noms = C.comptes(ids | {current_user.id})
    lignes = [{"conv": c, "titre": C.titre(c, current_user, noms), "non_lus": non_lus.get(c.id, 0),
               "heure": _heure(c.dernier_message_at)} for c in convs]
    messages = []
    if conv:
        messages = _messages_json(conv, 0, noms)
        dernier = max((m["id"] for m in messages), default=0)
        if membre and dernier > membre.dernier_lu_id:
            membre.dernier_lu_id = dernier
            db.session.commit()
            lignes = [{**l, "non_lus": 0} if l["conv"].id == conv.id else l for l in lignes]
    contacts = sorted(C.comptes(C.ids_contacts(current_user)).values(), key=lambda u: C.nom(u).lower())
    return render_template(
        "chat/index.html", lignes=lignes, conv=conv, messages=messages, noms=noms, contacts=contacts,
        titre_conv=C.titre(conv, current_user, noms) if conv else None,
        peut_gerer=bool(conv and C.peut_gerer(current_user, conv)),
        departements=C.departements_geres(current_user), C=C)


@bp.post("/nouvelle")
@login_required
def nouvelle_privee():
    autre = request.form.get("utilisateur_id", type=int)
    if autre not in C.ids_contacts(current_user):
        flash("Cette personne n'est pas dans vos contacts.", "erreur")
        return redirect(url_for("chat.index"))
    conv = C.conversation_privee(current_user.id, autre, current_user.id)
    db.session.commit()
    return redirect(url_for("chat.index", conv_id=conv.id))


@bp.post("/groupe")
@login_required
def creer_groupe():
    """Groupe de département : créé par la RH ou le chef du département."""
    from ..chat_models import Conversation, Membre
    deps = {d.id: d for d in C.departements_geres(current_user)}
    dep_id = request.form.get("departement_id", type=int)
    if dep_id not in deps:
        flash("Seuls la RH et le chef du département peuvent créer un groupe pour ce département.", "erreur")
        return redirect(url_for("chat.index"))
    nom = " ".join((request.form.get("nom") or "").split())[:120] or deps[dep_id].nom
    contacts = C.ids_contacts(current_user)
    ids = {int(x) for x in request.form.getlist("membres") if x.isdigit()} & contacts
    if request.form.get("tout_le_departement") == "1":
        ids |= C.comptes_du_departement(dep_id)
    ids.discard(current_user.id)
    conv = Conversation(type="groupe", nom=nom, departement_id=dep_id, entite_id=deps[dep_id].entite_id,
                        cree_par_id=current_user.id)
    conv.membres = [Membre(utilisateur_id=current_user.id, gestionnaire=True)] + [Membre(utilisateur_id=i) for i in sorted(ids)]
    db.session.add(conv)
    db.session.flush()
    journaliser("chat_groupe_cree", nom, f"{len(ids) + 1} membre(s) — département {deps[dep_id].nom}")
    db.session.commit()
    flash(f"Groupe « {nom} » créé avec {len(ids) + 1} membre(s).", "succes")
    return redirect(url_for("chat.index", conv_id=conv.id))


@bp.post("/<int:conv_id>/membres")
@login_required
def gerer_groupe(conv_id):
    from ..chat_models import Membre
    conv, _m = _conv_ou_404(conv_id, lecture_admin=True)
    if not C.peut_gerer(current_user, conv):
        abort(403)
    action = request.form.get("action")
    actuels = {m.utilisateur_id: m for m in conv.membres}
    if action == "ajouter":
        ids = {int(x) for x in request.form.getlist("membres") if x.isdigit()} & C.ids_contacts(current_user)
        if request.form.get("tout_le_departement") == "1" and conv.departement_id:
            ids |= C.comptes_du_departement(conv.departement_id)
        nouveaux = ids - set(actuels)
        for i in nouveaux:
            conv.membres.append(Membre(utilisateur_id=i))
        flash(f"{len(nouveaux)} membre(s) ajouté(s).", "succes")
    elif action == "retirer":
        uid = request.form.get("utilisateur_id", type=int)
        if uid in actuels and uid != conv.cree_par_id:
            db.session.delete(actuels[uid])
            flash("Membre retiré du groupe.", "succes")
    elif action == "renommer":
        nom = " ".join((request.form.get("nom") or "").split())[:120]
        if nom:
            conv.nom = nom
    elif action == "archiver":
        conv.archive = True
        journaliser("chat_groupe_archive", conv.nom or "Groupe", "")
        db.session.commit()
        flash("Groupe archivé.", "succes")
        return redirect(url_for("chat.index"))
    db.session.commit()
    return redirect(url_for("chat.index", conv_id=conv.id))


# ------------------------------------------------------------------ messages (AJAX + repli sans JS)
@bp.post("/<int:conv_id>/envoyer")
@login_required
def envoyer(conv_id):
    from ..chat_models import Message
    conv, membre = _conv_ou_404(conv_id)
    texte = (request.form.get("texte") or "").strip()[:C.LONGUEUR_MAX]
    ajax = request.headers.get("X-Requested-With") == "fetch"
    if not texte:
        return (jsonify(erreur="Message vide."), 400) if ajax else redirect(url_for("chat.index", conv_id=conv.id))
    msg = Message(conversation_id=conv.id, auteur_id=current_user.id, texte=texte)
    db.session.add(msg)
    db.session.flush()
    conv.dernier_message_at = msg.created_at
    membre.dernier_lu_id = msg.id
    db.session.commit()
    if ajax:
        return jsonify(ok=True)
    return redirect(url_for("chat.index", conv_id=conv.id) + "#bas")


@bp.get("/<int:conv_id>/nouveaux")
@login_required
def nouveaux(conv_id):
    conv, membre = _conv_ou_404(conv_id, lecture_admin=True)
    apres = request.args.get("apres", type=int) or 0
    msgs = _messages_json(conv, apres, {})
    if membre and msgs and msgs[-1]["id"] > membre.dernier_lu_id:
        membre.dernier_lu_id = msgs[-1]["id"]
        db.session.commit()
    return jsonify(messages=msgs, non_lus=C.total_non_lus(current_user))


@bp.post("/message/<int:message_id>/supprimer")
@login_required
def supprimer_message(message_id):
    from ..chat_models import Message
    m = db.session.get(Message, message_id) or abort(404)
    if m.auteur_id != current_user.id:
        abort(403)
    m.supprime, m.texte = True, ""
    db.session.commit()
    return redirect(url_for("chat.index", conv_id=m.conversation_id))


@bp.get("/non-lus")
@login_required
def compteur():
    return jsonify(non_lus=C.total_non_lus(current_user))


# ------------------------------------------------------------------ supervision (superadmin)
@bp.get("/supervision")
@bp.get("/supervision/<int:conv_id>")
@login_required
def supervision(conv_id=None):
    """Le superadmin peut consulter toutes les conversations (consultation journalisée)."""
    from ..chat_models import Conversation
    if not current_user.est_superadmin:
        abort(403)
    convs = db.session.scalars(select(Conversation).order_by(Conversation.dernier_message_at.desc()).limit(300)).all()
    ids = {m.utilisateur_id for c in convs for m in c.membres}
    noms = C.comptes(ids)
    lignes = [{"conv": c, "titre": c.nom if c.type == "groupe" else " ↔ ".join(C.nom(noms.get(m.utilisateur_id)) for m in c.membres),
               "membres": len(c.membres), "heure": _heure(c.dernier_message_at)} for c in convs]
    conv, messages = None, []
    if conv_id:
        conv = db.session.get(Conversation, conv_id) or abort(404)
        messages = _messages_json(conv, 0, noms)
        titre = next(l["titre"] for l in lignes if l["conv"].id == conv.id) if any(l["conv"].id == conv.id for l in lignes) else (conv.nom or "Conversation")
        journaliser("chat_consultation_admin", titre, f"{len(messages)} message(s) consulté(s)")
        db.session.commit()
    return render_template("chat/supervision.html", lignes=lignes, conv=conv, messages=messages)
