"""Messagerie interne — tables dans une base SÉPARÉE (bind « chat »).

La base KPI n'est pas modifiée : les utilisateurs et départements sont référencés par
leur identifiant (pas de clé étrangère entre les deux bases).
"""
from datetime import datetime, timezone

from .extensions import db


def _maintenant():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Conversation(db.Model):
    __bind_key__ = "chat"
    __tablename__ = "chat_conversations"

    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(10), nullable=False)               # "prive" | "groupe"
    cle = db.Column(db.String(40), unique=True)                     # "prive:<id1>-<id2>" (conversation unique)
    nom = db.Column(db.String(120))
    departement_id = db.Column(db.Integer, index=True)              # groupe d'un département (base KPI)
    entite_id = db.Column(db.Integer, index=True)
    cree_par_id = db.Column(db.Integer, nullable=False)             # utilisateur (base KPI)
    created_at = db.Column(db.DateTime, default=_maintenant, nullable=False)
    dernier_message_at = db.Column(db.DateTime, default=_maintenant, nullable=False, index=True)
    archive = db.Column(db.Boolean, default=False, nullable=False)

    membres = db.relationship("Membre", back_populates="conversation", cascade="all, delete-orphan")


class Membre(db.Model):
    __bind_key__ = "chat"
    __tablename__ = "chat_membres"
    __table_args__ = (db.UniqueConstraint("conversation_id", "utilisateur_id", name="uq_chat_membre"),)

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    utilisateur_id = db.Column(db.Integer, nullable=False, index=True)
    gestionnaire = db.Column(db.Boolean, default=False, nullable=False)
    dernier_lu_id = db.Column(db.Integer, default=0, nullable=False)
    ajoute_le = db.Column(db.DateTime, default=_maintenant, nullable=False)

    conversation = db.relationship("Conversation", back_populates="membres")


class Message(db.Model):
    __bind_key__ = "chat"
    __tablename__ = "chat_messages"
    __table_args__ = (db.Index("ix_chat_messages_conv_id", "conversation_id", "id"),)

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False)
    auteur_id = db.Column(db.Integer, nullable=False)
    texte = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=_maintenant, nullable=False)
    supprime = db.Column(db.Boolean, default=False, nullable=False)
