"""Messagerie interne : contacts, droits, conversations, messages non lus.

Les messages sont dans une base séparée (bind « chat », voir chat_models.py) ;
les personnes et départements viennent de la base KPI, en lecture seule.
"""
from __future__ import annotations

from flask import current_app, g
from sqlalchemy import func, or_, select

from ..extensions import db
from ..models import Affectation, Departement, Employe, Utilisateur

LONGUEUR_MAX = 2000


def actif() -> bool:
    return bool(current_app.config.get("CHAT_ACTIF"))


# ------------------------------------------------------------------ contacts
def ids_contacts(user) -> set[int]:
    """Comptes (Utilisateur.id) avec qui l'utilisateur peut discuter.

    RH / superadmin : tout le monde. Sinon : les personnes des entités auxquelles il a accès
    (y compris multi-entités) et le personnel groupe.
    """
    if getattr(g, "_chat_contacts", None) is not None:
        return g._chat_contacts
    q = select(Utilisateur.id).join(Employe, Employe.id == Utilisateur.employe_id).where(
        Utilisateur.actif.is_(True), Employe.actif.is_(True))
    if not user.est_rh:
        from .entites import cond_entite, ids_employes_affectes, roles_par_entite
        from .transverses import ids_personnel_groupe
        conds = [Employe.id.in_(ids_personnel_groupe() or {-1})]
        for eid in roles_par_entite(user):
            conds += [cond_entite(Employe.entite_id, eid), Employe.id.in_(ids_employes_affectes(eid))]
        q = q.where(or_(*conds))
    g._chat_contacts = set(db.session.scalars(q)) - {user.id}
    return g._chat_contacts


def comptes(ids) -> dict[int, Utilisateur]:
    ids = set(ids)
    if not ids:
        return {}
    return {u.id: u for u in db.session.scalars(select(Utilisateur).where(Utilisateur.id.in_(ids)))}


def nom(u: Utilisateur | None) -> str:
    if u is None:
        return "Compte supprimé"
    return u.employe.nom_complet if u.employe else u.email


def initiales(u: Utilisateur | None) -> str:
    if u is not None and u.employe:
        return u.employe.initiales
    return (u.email[:2] if u else "?").upper()


# ------------------------------------------------------------------ groupes de département
def departements_geres(user) -> list[Departement]:
    """Départements pour lesquels l'utilisateur peut créer un groupe : tous (RH) ou ceux qu'il dirige
    (avec leurs sous-départements)."""
    q = select(Departement).where(Departement.actif.is_(True)).order_by(Departement.nom)
    if user.est_rh:
        from .entites import cond_entite, roles_par_entite
        eids = list(roles_par_entite(user))
        return db.session.scalars(q.where(or_(*[cond_entite(Departement.entite_id, e) for e in eids]))).all() if eids else []
    if not user.employe:
        return []
    from .hierarchie import branche
    ids = set()
    for d in db.session.scalars(select(Departement.id).where(Departement.responsable_id == user.employe_id,
                                                             Departement.actif.is_(True))):
        ids |= branche(d)
    return db.session.scalars(q.where(Departement.id.in_(ids or {-1}))).all()


def comptes_du_departement(departement_id: int) -> set[int]:
    """Comptes des membres du département et de ses sous-départements (chefs compris)."""
    from .hierarchie import branche
    deps = branche(departement_id)
    emp = set(db.session.scalars(select(Affectation.employe_id).where(
        Affectation.departement_id.in_(deps), Affectation.actif.is_(True))))
    emp |= {r for r in db.session.scalars(select(Departement.responsable_id).where(
        Departement.id.in_(deps), Departement.responsable_id.isnot(None)))}
    if not emp:
        return set()
    return set(db.session.scalars(select(Utilisateur.id).where(
        Utilisateur.employe_id.in_(emp), Utilisateur.actif.is_(True))))


def peut_gerer(user, conv) -> bool:
    """Gérer un groupe (membres, nom, archivage) : RH, créateur, gestionnaire, chef du département."""
    if conv.type != "groupe":
        return False
    if user.est_rh or conv.cree_par_id == user.id:
        return True
    if any(m.utilisateur_id == user.id and m.gestionnaire for m in conv.membres):
        return True
    return bool(conv.departement_id and any(d.id == conv.departement_id for d in departements_geres(user)))


# ------------------------------------------------------------------ conversations
def mes_conversations(user):
    from ..chat_models import Conversation, Membre, Message
    convs = db.session.scalars(
        select(Conversation).join(Membre, Membre.conversation_id == Conversation.id)
        .where(Membre.utilisateur_id == user.id, Conversation.archive.is_(False))
        .order_by(Conversation.dernier_message_at.desc())).unique().all()
    return convs


def non_lus_par_conversation(user) -> dict[int, int]:
    from ..chat_models import Membre, Message
    rows = db.session.execute(
        select(Membre.conversation_id, func.count(Message.id))
        .join(Message, Message.conversation_id == Membre.conversation_id)
        .where(Membre.utilisateur_id == user.id, Message.id > Membre.dernier_lu_id,
               Message.auteur_id != user.id, Message.supprime.is_(False))
        .group_by(Membre.conversation_id)).all()
    return {c: n for c, n in rows}


def total_non_lus(user) -> int:
    """Badge du menu. Ne doit jamais casser une page : 0 en cas de souci avec la base du chat."""
    if not actif():
        return 0
    try:
        return sum(non_lus_par_conversation(user).values())
    except Exception:  # base du chat momentanément indisponible
        current_app.logger.warning("Messagerie : comptage des non-lus impossible", exc_info=True)
        db.session.rollback()
        return 0


def titre(conv, user, noms: dict[int, Utilisateur]) -> str:
    if conv.type == "groupe":
        return conv.nom or "Groupe"
    autre = next((m.utilisateur_id for m in conv.membres if m.utilisateur_id != user.id), None)
    return nom(noms.get(autre))


def conversation_privee(user_a: int, user_b: int, createur: int):
    """Retrouve ou crée la conversation unique entre deux personnes."""
    from ..chat_models import Conversation, Membre
    a, b = sorted((user_a, user_b))
    cle = f"prive:{a}-{b}"
    conv = db.session.scalar(select(Conversation).where(Conversation.cle == cle))
    if conv is None:
        conv = Conversation(type="prive", cle=cle, cree_par_id=createur)
        conv.membres = [Membre(utilisateur_id=a), Membre(utilisateur_id=b)]
        db.session.add(conv)
        db.session.flush()
    elif conv.archive:
        conv.archive = False
    return conv
