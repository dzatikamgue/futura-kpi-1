"""Modèle de données Futura Performance.

Principe central : un salarié (Employe) a une ou plusieurs AFFECTATIONS,
chacune rattachée soit à un département, soit à un projet, avec un N+1
(evaluateur). Chaque mois, le N+1 produit une EVALUATION par affectation.
Le contexte (département / projet) de l'évaluation détermine qui peut la voir.
"""
from __future__ import annotations

from datetime import datetime, timezone

from flask_login import UserMixin
from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Horodatage:
    """Champs de traçabilité communs."""
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Comptes utilisateurs
# ---------------------------------------------------------------------------
class Role:
    RH = "rh"                    # administration complète, voit tout
    DIRECTION = "direction"      # voit tout, note ses N-1
    COLLABORATEUR = "collaborateur"  # note ses N-1, voit son périmètre

    LIBELLES = {
        RH: "Ressources humaines",
        DIRECTION: "Direction",
        COLLABORATEUR: "Collaborateur",
    }
    TOUS = (RH, DIRECTION, COLLABORATEUR)


class Utilisateur(UserMixin, Horodatage, db.Model):
    __tablename__ = "utilisateurs"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(160), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=Role.COLLABORATEUR)
    employe_id = db.Column(db.Integer, db.ForeignKey("employes.id", ondelete="SET NULL"), unique=True)
    actif = db.Column(db.Boolean, nullable=False, default=True)
    doit_changer_mdp = db.Column(db.Boolean, nullable=False, default=True)
    derniere_connexion = db.Column(db.DateTime)
    echecs_connexion = db.Column(db.Integer, nullable=False, default=0)
    bloque_jusqua = db.Column(db.DateTime)
    # Date à laquelle la RH a généré le mot de passe à transmettre (None = accès pas encore remis)
    acces_remis_le = db.Column(db.DateTime)

    employe = db.relationship("Employe", back_populates="compte", foreign_keys=[employe_id])

    __table_args__ = (CheckConstraint(f"role IN ('{Role.RH}','{Role.DIRECTION}','{Role.COLLABORATEUR}')",
                                      name="ck_utilisateur_role"),)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self):  # utilisé par Flask-Login
        return self.actif

    @property
    def est_rh(self) -> bool:
        return self.role == Role.RH

    @property
    def voit_tout(self) -> bool:
        return self.role in (Role.RH, Role.DIRECTION)

    @property
    def nom_affiche(self) -> str:
        return self.employe.nom_complet if self.employe else self.email

    @property
    def role_libelle(self) -> str:
        return Role.LIBELLES.get(self.role, self.role)

    @property
    def en_attente_acces(self) -> bool:
        """Compte créé automatiquement dont le mot de passe n'a pas encore été remis."""
        return self.actif and self.acces_remis_le is None and self.derniere_connexion is None


# ---------------------------------------------------------------------------
# Organisation
# ---------------------------------------------------------------------------
class Departement(Horodatage, db.Model):
    __tablename__ = "departements"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    nom = db.Column(db.String(120), unique=True, nullable=False)
    responsable_id = db.Column(db.Integer, db.ForeignKey("employes.id", ondelete="SET NULL"))
    actif = db.Column(db.Boolean, nullable=False, default=True)

    responsable = db.relationship("Employe", foreign_keys=[responsable_id])

    def __str__(self):
        return self.nom


class Projet(Horodatage, db.Model):
    __tablename__ = "projets"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    nom = db.Column(db.String(160), nullable=False)
    localisation = db.Column(db.String(160))
    responsable_id = db.Column(db.Integer, db.ForeignKey("employes.id", ondelete="SET NULL"))
    date_debut = db.Column(db.Date)
    date_fin = db.Column(db.Date)
    actif = db.Column(db.Boolean, nullable=False, default=True)

    responsable = db.relationship("Employe", foreign_keys=[responsable_id])

    def __str__(self):
        return self.nom


class Poste(Horodatage, db.Model):
    """Référentiel des postes (liste déroulante). Employe.poste en garde le libellé."""
    __tablename__ = "postes"

    id = db.Column(db.Integer, primary_key=True)
    libelle = db.Column(db.String(120), unique=True, nullable=False)
    actif = db.Column(db.Boolean, nullable=False, default=True)

    def __str__(self):
        return self.libelle


class Employe(Horodatage, db.Model):
    __tablename__ = "employes"

    id = db.Column(db.Integer, primary_key=True)
    matricule = db.Column(db.String(30), unique=True, nullable=False, index=True)
    nom = db.Column(db.String(80), nullable=False, index=True)
    prenom = db.Column(db.String(80), nullable=False, default="")
    poste = db.Column(db.String(120))
    email = db.Column(db.String(160))
    telephone = db.Column(db.String(40))
    date_embauche = db.Column(db.Date)
    actif = db.Column(db.Boolean, nullable=False, default=True)
    # L'auteur des créations/modifications est tracé dans JournalAction
    # (évite une dépendance circulaire employes <-> utilisateurs).

    affectations = db.relationship("Affectation", back_populates="employe",
                                   foreign_keys="Affectation.employe_id",
                                   cascade="all, delete-orphan")
    compte = db.relationship("Utilisateur", back_populates="employe", uselist=False,
                             foreign_keys="Utilisateur.employe_id")

    @property
    def nom_complet(self) -> str:
        return f"{self.nom.upper()} {self.prenom}".strip()

    @property
    def initiales(self) -> str:
        p = (self.prenom or " ")[0]
        return (self.nom[:1] + p).upper().strip()

    @property
    def affectations_actives(self):
        return [a for a in self.affectations if a.actif]

    def __str__(self):
        return self.nom_complet


class Affectation(Horodatage, db.Model):
    """Rattachement d'un salarié à UN département OU UN projet, avec son N+1."""
    __tablename__ = "affectations"

    id = db.Column(db.Integer, primary_key=True)
    employe_id = db.Column(db.Integer, db.ForeignKey("employes.id", ondelete="CASCADE"), nullable=False, index=True)
    departement_id = db.Column(db.Integer, db.ForeignKey("departements.id", ondelete="CASCADE"), index=True)
    projet_id = db.Column(db.Integer, db.ForeignKey("projets.id", ondelete="CASCADE"), index=True)
    evaluateur_id = db.Column(db.Integer, db.ForeignKey("employes.id", ondelete="SET NULL"), index=True)
    actif = db.Column(db.Boolean, nullable=False, default=True)

    employe = db.relationship("Employe", back_populates="affectations", foreign_keys=[employe_id])
    evaluateur = db.relationship("Employe", foreign_keys=[evaluateur_id])
    departement = db.relationship("Departement")
    projet = db.relationship("Projet")

    __table_args__ = (
        CheckConstraint("(departement_id IS NULL) <> (projet_id IS NULL)", name="ck_affectation_un_contexte"),
        CheckConstraint("evaluateur_id IS NULL OR evaluateur_id <> employe_id", name="ck_affectation_pas_auto"),
    )

    @property
    def type_contexte(self) -> str:
        return "departement" if self.departement_id else "projet"

    @property
    def contexte(self):
        return self.departement or self.projet

    @property
    def contexte_libelle(self) -> str:
        c = self.contexte
        prefixe = "Dépt." if self.departement_id else "Projet"
        return f"{prefixe} {c.nom}" if c else "—"


# ---------------------------------------------------------------------------
# Grille KPI et évaluations
# ---------------------------------------------------------------------------
class Critere(Horodatage, db.Model):
    __tablename__ = "criteres"

    id = db.Column(db.Integer, primary_key=True)
    libelle = db.Column(db.String(120), nullable=False, unique=True)
    description = db.Column(db.String(400))
    poids = db.Column(db.Integer, nullable=False, default=1)
    ordre = db.Column(db.Integer, nullable=False, default=0)
    actif = db.Column(db.Boolean, nullable=False, default=True)

    __table_args__ = (CheckConstraint("poids BETWEEN 1 AND 5", name="ck_critere_poids"),)


class StatutEvaluation:
    BROUILLON = "brouillon"
    SOUMISE = "soumise"
    LIBELLES = {BROUILLON: "Brouillon", SOUMISE: "Soumise"}


class Evaluation(Horodatage, db.Model):
    __tablename__ = "evaluations"

    id = db.Column(db.Integer, primary_key=True)
    affectation_id = db.Column(db.Integer, db.ForeignKey("affectations.id", ondelete="CASCADE"), nullable=False)
    annee = db.Column(db.Integer, nullable=False)
    mois = db.Column(db.Integer, nullable=False)
    statut = db.Column(db.String(12), nullable=False, default=StatutEvaluation.BROUILLON)
    note_globale = db.Column(db.Float)  # sur 100, recalculée à chaque enregistrement
    commentaire = db.Column(db.Text)
    axes_amelioration = db.Column(db.Text)
    rouverte = db.Column(db.Boolean, nullable=False, default=False)
    soumise_le = db.Column(db.DateTime)

    # Colonnes dénormalisées pour filtrer vite sur des milliers de lignes
    employe_id = db.Column(db.Integer, db.ForeignKey("employes.id", ondelete="CASCADE"), nullable=False)
    evaluateur_id = db.Column(db.Integer, db.ForeignKey("employes.id", ondelete="SET NULL"))
    departement_id = db.Column(db.Integer, db.ForeignKey("departements.id", ondelete="SET NULL"))
    projet_id = db.Column(db.Integer, db.ForeignKey("projets.id", ondelete="SET NULL"))

    created_by_id = db.Column(db.Integer, db.ForeignKey("utilisateurs.id", ondelete="SET NULL"))
    updated_by_id = db.Column(db.Integer, db.ForeignKey("utilisateurs.id", ondelete="SET NULL"))

    affectation = db.relationship("Affectation")
    employe = db.relationship("Employe", foreign_keys=[employe_id])
    evaluateur = db.relationship("Employe", foreign_keys=[evaluateur_id])
    departement = db.relationship("Departement")
    projet = db.relationship("Projet")
    created_by = db.relationship("Utilisateur", foreign_keys=[created_by_id])
    updated_by = db.relationship("Utilisateur", foreign_keys=[updated_by_id])
    notes = db.relationship("EvaluationNote", back_populates="evaluation",
                            cascade="all, delete-orphan", order_by="EvaluationNote.id")

    __table_args__ = (
        UniqueConstraint("affectation_id", "annee", "mois", name="uq_evaluation_periode"),
        CheckConstraint("mois BETWEEN 1 AND 12", name="ck_evaluation_mois"),
        CheckConstraint("statut IN ('brouillon','soumise')", name="ck_evaluation_statut"),
        Index("ix_evaluation_periode", "annee", "mois"),
        Index("ix_evaluation_employe_periode", "employe_id", "annee", "mois"),
        Index("ix_evaluation_dept", "departement_id", "annee"),
        Index("ix_evaluation_projet", "projet_id", "annee"),
    )

    @property
    def est_soumise(self) -> bool:
        return self.statut == StatutEvaluation.SOUMISE

    @property
    def contexte_libelle(self) -> str:
        if self.departement:
            return f"Dépt. {self.departement.nom}"
        if self.projet:
            return f"Projet {self.projet.nom}"
        return "—"


class EvaluationNote(db.Model):
    __tablename__ = "evaluation_notes"

    id = db.Column(db.Integer, primary_key=True)
    evaluation_id = db.Column(db.Integer, db.ForeignKey("evaluations.id", ondelete="CASCADE"), nullable=False, index=True)
    critere_id = db.Column(db.Integer, db.ForeignKey("criteres.id", ondelete="RESTRICT"), nullable=False)
    note = db.Column(db.Integer, nullable=False)  # 0 à 100
    poids = db.Column(db.Integer, nullable=False, default=1)  # copie du poids au moment de la notation
    commentaire = db.Column(db.String(500))

    evaluation = db.relationship("Evaluation", back_populates="notes")
    critere = db.relationship("Critere")

    __table_args__ = (
        UniqueConstraint("evaluation_id", "critere_id", name="uq_note_critere"),
        CheckConstraint("note BETWEEN 0 AND 100", name="ck_note_bornes"),
    )


# ---------------------------------------------------------------------------
# Import assisté et journal
# ---------------------------------------------------------------------------
class ImportBrouillon(db.Model):
    """Résultat d'extraction Claude en attente de validation par la RH."""
    __tablename__ = "imports_brouillons"

    id = db.Column(db.Integer, primary_key=True)
    utilisateur_id = db.Column(db.Integer, db.ForeignKey("utilisateurs.id", ondelete="CASCADE"), nullable=False)
    source_nom = db.Column(db.String(200))
    donnees = db.Column(db.JSON, nullable=False)
    remarques = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)


class Parametre(db.Model):
    """Réglages modifiables depuis l'application (ex. clé API Claude, stockée chiffrée)."""
    __tablename__ = "parametres"

    cle = db.Column(db.String(60), primary_key=True)
    valeur = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("utilisateurs.id", ondelete="SET NULL"))

    updated_by = db.relationship("Utilisateur")


class JournalAction(db.Model):
    __tablename__ = "journal_actions"

    id = db.Column(db.Integer, primary_key=True)
    utilisateur_id = db.Column(db.Integer, db.ForeignKey("utilisateurs.id", ondelete="SET NULL"), index=True)
    action = db.Column(db.String(60), nullable=False, index=True)
    cible = db.Column(db.String(200))
    details = db.Column(db.Text)
    ip = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    utilisateur = db.relationship("Utilisateur")
