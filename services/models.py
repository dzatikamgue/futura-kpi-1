"""Modèle de données — Futura Services généraux.

Deux familles de sous-modules :
  Personnel : visas, cartes de séjour (table `documents`), billets d'avion (`billets`)
  Logements : loyers (`loyers` = paiements ; les échéances dues sont calculées), factures d'électricité
              et d'eau (`factures_energie`), réparations (`reparations` + `reparation_suivis`)

Une personne peut habiter un logement ; un compte « occupant » est rattaché à un logement et
y signale les réparations. Pièces jointes (scans, photos) stockées en base (`pieces`).
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()
TZ = ZoneInfo("Africa/Douala")


def now():
    """Heure locale de Douala, stockée sans fuseau."""
    return datetime.now(TZ).replace(tzinfo=None, microsecond=0)


def aujourdhui():
    return now().date()


# --------------------------------------------------------------------------- #
#  Listes de référence
# --------------------------------------------------------------------------- #

ROLES = {
    "admin": "Administrateur",
    "gestionnaire": "Gestionnaire (RH / services généraux)",
    "direction": "Direction (consultation)",
    "occupant": "Occupant d'un logement",
}
ROLES_GESTION = ("admin", "gestionnaire")

TYPES_DOCUMENT = {"visa": "Visa", "carte_sejour": "Carte de séjour"}
RENOUVELLEMENT = {"": "Aucune démarche", "a_lancer": "À lancer", "en_cours": "Dossier déposé, en cours",
                  "obtenu": "Renouvelé"}

MOTIFS_BILLET = {"arrivee": "Prise de poste", "conge": "Congé", "mission": "Mission",
                 "retour_definitif": "Retour définitif", "famille": "Famille / rapprochement", "autre": "Autre"}
STATUTS_BILLET = {"reserve": "Réservé", "emis": "Émis", "utilise": "Voyage effectué", "annule": "Annulé"}
CLASSES = ["Économique", "Premium", "Affaires", "Première"]

TYPES_LOGEMENT = ["Appartement", "Studio", "Villa", "Chambre", "Autre"]
VILLES = ["Douala", "Yaoundé", "Kribi", "Bafoussam", "Autre"]
PERIODICITES = {1: "Mensuel", 3: "Trimestriel", 6: "Semestriel", 12: "Annuel"}
MODES_PAIEMENT = ["Virement", "Chèque", "Espèces", "Mobile money", "Prélèvement"]

TYPES_ENERGIE = {"electricite": "Électricité", "eau": "Eau"}
FOURNISSEURS = {"electricite": "ENEO", "eau": "CAMWATER"}
UNITES = {"electricite": "kWh", "eau": "m³"}

CATEGORIES_REPARATION = ["Plomberie", "Électricité", "Climatisation", "Menuiserie / portes", "Serrurerie",
                         "Peinture / murs", "Électroménager", "Toiture / étanchéité", "Nuisibles", "Autre"]
URGENCES = {"normale": "Normale", "urgente": "Urgente"}
STATUTS_REPARATION = {"signalee": "Signalée", "prise_en_charge": "Prise en charge", "en_cours": "Travaux en cours",
                      "terminee": "Terminée", "annulee": "Annulée"}
REPARATIONS_OUVERTES = ("signalee", "prise_en_charge", "en_cours")

MIMES_AUTORISES = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
ALERTE_DOC_JOURS = 60          # alerte avant l'expiration d'un visa / d'une carte de séjour (réglable)
ALERTE_BAIL_JOURS = 60         # alerte avant la fin d'un bail

SQL_ROLES = "role IN (" + ",".join(f"'{r}'" for r in ROLES) + ")"


def mois_ajoutes(d, n):
    """Date + n mois (jour ramené au dernier jour du mois si besoin)."""
    m = d.month - 1 + n
    a, m = d.year + m // 12, m % 12 + 1
    import calendar
    return date(a, m, min(d.day, calendar.monthrange(a, m)[1]))


class Tracable:
    created_at = db.Column(db.DateTime, nullable=False, default=now)
    updated_at = db.Column(db.DateTime, nullable=False, default=now, onupdate=now)


# --------------------------------------------------------------------------- #
#  Comptes
# --------------------------------------------------------------------------- #

class User(UserMixin, db.Model):
    __tablename__ = "utilisateurs"
    __table_args__ = (db.CheckConstraint(SQL_ROLES, name="ck_role"),)

    id = db.Column(db.Integer, primary_key=True)
    nom = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="direction")
    actif = db.Column(db.Boolean, nullable=False, default=True)
    logement_id = db.Column(db.Integer, db.ForeignKey("logements.id", ondelete="SET NULL"))
    derniere_connexion = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=now)

    logement = db.relationship("Logement", foreign_keys=[logement_id])

    def set_password(self, mdp):
        self.password_hash = generate_password_hash(mdp)

    def check_password(self, mdp):
        return check_password_hash(self.password_hash, mdp)

    @property
    def is_active(self):
        return self.actif

    @property
    def role_libelle(self):
        return ROLES.get(self.role, self.role)

    @property
    def gere(self):
        """Peut saisir et modifier (gestionnaire ou administrateur)."""
        return self.role in ROLES_GESTION

    @property
    def occupant(self):
        return self.role == "occupant"


# --------------------------------------------------------------------------- #
#  Personnel
# --------------------------------------------------------------------------- #

class Personne(Tracable, db.Model):
    __tablename__ = "personnes"

    id = db.Column(db.Integer, primary_key=True)
    nom = db.Column(db.String(160), nullable=False, index=True)
    matricule = db.Column(db.String(40))
    poste = db.Column(db.String(120))
    societe = db.Column(db.String(120))
    nationalite = db.Column(db.String(80))
    telephone = db.Column(db.String(40))
    email = db.Column(db.String(160))
    passeport_numero = db.Column(db.String(40))
    passeport_expiration = db.Column(db.Date)
    logement_id = db.Column(db.Integer, db.ForeignKey("logements.id", ondelete="SET NULL"), index=True)
    actif = db.Column(db.Boolean, nullable=False, default=True)
    notes = db.Column(db.Text)
    cree_par_id = db.Column(db.Integer, db.ForeignKey("utilisateurs.id"))

    logement = db.relationship("Logement", back_populates="occupants")
    documents = db.relationship("Document", back_populates="personne", cascade="all, delete-orphan",
                                order_by="Document.date_expiration.desc()")
    billets = db.relationship("Billet", back_populates="personne", cascade="all, delete-orphan",
                              order_by="Billet.date_depart.desc()")


class Document(Tracable, db.Model):
    """Visa ou carte de séjour. Le plus récent d'un même type pour une personne est le document en vigueur."""
    __tablename__ = "documents"
    __table_args__ = (db.CheckConstraint("type_doc IN ('visa', 'carte_sejour')", name="ck_type_doc"),)

    id = db.Column(db.Integer, primary_key=True)
    personne_id = db.Column(db.Integer, db.ForeignKey("personnes.id", ondelete="CASCADE"), nullable=False, index=True)
    type_doc = db.Column(db.String(20), nullable=False, index=True)
    numero = db.Column(db.String(60))
    categorie = db.Column(db.String(120))        # visa d'affaires, de travail, carte de résident…
    pays = db.Column(db.String(80), default="Cameroun")
    date_delivrance = db.Column(db.Date)
    date_expiration = db.Column(db.Date, nullable=False, index=True)
    renouvellement = db.Column(db.String(20), nullable=False, default="")
    renouvellement_note = db.Column(db.String(255))
    remplace = db.Column(db.Boolean, nullable=False, default=False)   # renouvelé : un document plus récent existe
    cout = db.Column(db.BigInteger)
    notes = db.Column(db.Text)
    cree_par_id = db.Column(db.Integer, db.ForeignKey("utilisateurs.id"))

    personne = db.relationship("Personne", back_populates="documents")

    @property
    def jours_restants(self):
        return (self.date_expiration - aujourdhui()).days

    def niveau(self, seuil=ALERTE_DOC_JOURS):
        """ok / alerte (dans le délai d'alerte) / critique (≤ 15 j) / expire / remplace."""
        if self.remplace:
            return "remplace"
        j = self.jours_restants
        if j < 0:
            return "expire"
        if j <= 15:
            return "critique"
        if j <= seuil:
            return "alerte"
        return "ok"

    @property
    def type_libelle(self):
        return TYPES_DOCUMENT.get(self.type_doc, self.type_doc)


class Billet(Tracable, db.Model):
    __tablename__ = "billets"

    id = db.Column(db.Integer, primary_key=True)
    personne_id = db.Column(db.Integer, db.ForeignKey("personnes.id", ondelete="CASCADE"), nullable=False, index=True)
    motif = db.Column(db.String(20), nullable=False, default="conge")
    depart = db.Column(db.String(80), nullable=False)
    arrivee = db.Column(db.String(80), nullable=False)
    date_depart = db.Column(db.Date, nullable=False, index=True)
    date_retour = db.Column(db.Date)
    compagnie = db.Column(db.String(80))
    reservation = db.Column(db.String(40))        # PNR
    classe = db.Column(db.String(20), default="Économique")
    montant = db.Column(db.BigInteger)
    statut = db.Column(db.String(20), nullable=False, default="emis")
    notes = db.Column(db.Text)
    cree_par_id = db.Column(db.Integer, db.ForeignKey("utilisateurs.id"))

    personne = db.relationship("Personne", back_populates="billets")

    @property
    def trajet(self):
        return f"{self.depart} → {self.arrivee}" + (" → " + self.depart if self.date_retour else "")

    @property
    def a_venir(self):
        return self.statut in ("reserve", "emis") and self.date_depart >= aujourdhui()


# --------------------------------------------------------------------------- #
#  Logements
# --------------------------------------------------------------------------- #

class Logement(Tracable, db.Model):
    __tablename__ = "logements"

    id = db.Column(db.Integer, primary_key=True)
    nom = db.Column(db.String(120), nullable=False)
    type_logement = db.Column(db.String(30), default="Appartement")
    ville = db.Column(db.String(40), default="Douala")
    quartier = db.Column(db.String(120))
    adresse = db.Column(db.String(255))
    bailleur = db.Column(db.String(160))
    bailleur_telephone = db.Column(db.String(40))
    loyer = db.Column(db.BigInteger)                     # montant par mois
    periodicite = db.Column(db.Integer, nullable=False, default=1)   # mois couverts par échéance
    jour_echeance = db.Column(db.Integer, nullable=False, default=5)
    caution = db.Column(db.BigInteger)
    bail_debut = db.Column(db.Date)
    bail_fin = db.Column(db.Date)
    loyers_suivis_depuis = db.Column(db.Date)            # première échéance suivie dans l'application
    compteur_electricite = db.Column(db.String(40))
    compteur_eau = db.Column(db.String(40))
    actif = db.Column(db.Boolean, nullable=False, default=True)
    notes = db.Column(db.Text)

    occupants = db.relationship("Personne", back_populates="logement", order_by="Personne.nom")
    loyers = db.relationship("Loyer", back_populates="logement", cascade="all, delete-orphan",
                             order_by="Loyer.periode_debut.desc()")
    factures = db.relationship("FactureEnergie", back_populates="logement", cascade="all, delete-orphan",
                               order_by="FactureEnergie.periode.desc()")
    reparations = db.relationship("Reparation", back_populates="logement", cascade="all, delete-orphan",
                                  order_by="Reparation.signale_le.desc()")

    @property
    def montant_echeance(self):
        return (self.loyer or 0) * (self.periodicite or 1)

    @property
    def libelle(self):
        return f"{self.nom} · {self.quartier}" if self.quartier else self.nom

    def echeances_dues(self, jusqu_au=None):
        """Échéances de loyer échues (date ≤ jusqu_au) non couvertes par un paiement : [(début, date limite)]."""
        if not self.actif or not self.loyer or not self.loyers_suivis_depuis:
            return []
        jusqu_au = jusqu_au or aujourdhui()
        payees = {l.periode_debut for l in self.loyers}
        p = self.periodicite or 1
        d = self.loyers_suivis_depuis.replace(day=1)
        res = []
        for _ in range(240):
            limite = d.replace(day=min(max(self.jour_echeance or 1, 1), 28))
            if limite > jusqu_au:
                break
            if self.bail_fin and d > self.bail_fin:
                break
            if d not in payees:
                res.append((d, limite))
            d = mois_ajoutes(d, p)
        return res

    def prochaine_echeance(self):
        """Prochaine échéance (non encore échue ni payée)."""
        if not self.actif or not self.loyer or not self.loyers_suivis_depuis:
            return None
        payees = {l.periode_debut for l in self.loyers}
        d = self.loyers_suivis_depuis.replace(day=1)
        for _ in range(400):
            limite = d.replace(day=min(max(self.jour_echeance or 1, 1), 28))
            if self.bail_fin and d > self.bail_fin:
                return None
            if limite > aujourdhui() and d not in payees:
                return d, limite
            d = mois_ajoutes(d, self.periodicite or 1)
        return None


class Loyer(Tracable, db.Model):
    """Paiement d'une échéance de loyer (période commençant le 1er du mois `periode_debut`)."""
    __tablename__ = "loyers"
    __table_args__ = (db.UniqueConstraint("logement_id", "periode_debut", name="uq_loyer_periode"),)

    id = db.Column(db.Integer, primary_key=True)
    logement_id = db.Column(db.Integer, db.ForeignKey("logements.id", ondelete="CASCADE"), nullable=False, index=True)
    periode_debut = db.Column(db.Date, nullable=False)
    mois = db.Column(db.Integer, nullable=False, default=1)
    montant = db.Column(db.BigInteger, nullable=False)
    date_paiement = db.Column(db.Date, nullable=False)
    mode_paiement = db.Column(db.String(30))
    reference = db.Column(db.String(80))
    notes = db.Column(db.String(255))
    cree_par_id = db.Column(db.Integer, db.ForeignKey("utilisateurs.id"))

    logement = db.relationship("Logement", back_populates="loyers")
    cree_par = db.relationship("User")

    @property
    def periode_fin(self):
        return mois_ajoutes(self.periode_debut, self.mois or 1)


class FactureEnergie(Tracable, db.Model):
    """Facture d'électricité (ENEO) ou d'eau (CAMWATER) d'un logement."""
    __tablename__ = "factures_energie"
    __table_args__ = (db.CheckConstraint("type_energie IN ('electricite', 'eau')", name="ck_type_energie"),)

    id = db.Column(db.Integer, primary_key=True)
    logement_id = db.Column(db.Integer, db.ForeignKey("logements.id", ondelete="CASCADE"), nullable=False, index=True)
    type_energie = db.Column(db.String(20), nullable=False, index=True)
    numero = db.Column(db.String(60))
    periode = db.Column(db.Date, nullable=False)          # 1er jour du mois facturé
    consommation = db.Column(db.Numeric(12, 2))
    montant = db.Column(db.BigInteger, nullable=False)
    date_echeance = db.Column(db.Date)
    date_paiement = db.Column(db.Date)
    mode_paiement = db.Column(db.String(30))
    reference = db.Column(db.String(80))
    notes = db.Column(db.String(255))
    cree_par_id = db.Column(db.Integer, db.ForeignKey("utilisateurs.id"))

    logement = db.relationship("Logement", back_populates="factures")

    @property
    def payee(self):
        return self.date_paiement is not None

    @property
    def niveau(self):
        if self.payee:
            return "payee"
        if self.date_echeance and self.date_echeance < aujourdhui():
            return "retard"
        return "a_payer"

    @property
    def type_libelle(self):
        return TYPES_ENERGIE.get(self.type_energie, self.type_energie)


class Reparation(Tracable, db.Model):
    __tablename__ = "reparations"

    id = db.Column(db.Integer, primary_key=True)
    logement_id = db.Column(db.Integer, db.ForeignKey("logements.id", ondelete="CASCADE"), nullable=False, index=True)
    titre = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text)
    categorie = db.Column(db.String(40), default="Autre")
    urgence = db.Column(db.String(10), nullable=False, default="normale")
    statut = db.Column(db.String(20), nullable=False, default="signalee", index=True)
    statut_depuis = db.Column(db.DateTime, nullable=False, default=now)
    signale_le = db.Column(db.DateTime, nullable=False, default=now)
    signale_par_id = db.Column(db.Integer, db.ForeignKey("utilisateurs.id"))
    intervenant = db.Column(db.String(160))
    date_intervention = db.Column(db.Date)
    cout = db.Column(db.BigInteger)
    cloturee_le = db.Column(db.DateTime)

    logement = db.relationship("Logement", back_populates="reparations")
    signale_par = db.relationship("User")
    suivis = db.relationship("SuiviReparation", back_populates="reparation", cascade="all, delete-orphan",
                             order_by="SuiviReparation.date")

    @property
    def reference(self):
        return f"REP-{self.signale_le:%Y}-{self.id:04d}"

    @property
    def ouverte(self):
        return self.statut in REPARATIONS_OUVERTES

    @property
    def statut_libelle(self):
        return STATUTS_REPARATION.get(self.statut, self.statut)

    @property
    def jours_ouverte(self):
        fin = self.cloturee_le or now()
        return max(0, (fin - self.signale_le).days)


class SuiviReparation(db.Model):
    """Historique d'une réparation : changements de statut et commentaires."""
    __tablename__ = "reparation_suivis"

    id = db.Column(db.Integer, primary_key=True)
    reparation_id = db.Column(db.Integer, db.ForeignKey("reparations.id", ondelete="CASCADE"), nullable=False, index=True)
    date = db.Column(db.DateTime, nullable=False, default=now)
    user_id = db.Column(db.Integer, db.ForeignKey("utilisateurs.id"))
    statut = db.Column(db.String(20))          # nouveau statut (None = simple commentaire)
    texte = db.Column(db.Text)

    reparation = db.relationship("Reparation", back_populates="suivis")
    user = db.relationship("User")


# --------------------------------------------------------------------------- #
#  Pièces jointes, réglages, journal
# --------------------------------------------------------------------------- #

OBJETS_PIECE = ("personne", "document", "billet", "logement", "loyer", "facture", "reparation")


class Piece(db.Model):
    __tablename__ = "pieces"

    id = db.Column(db.Integer, primary_key=True)
    objet = db.Column(db.String(20), nullable=False)
    objet_id = db.Column(db.Integer, nullable=False)
    nom_fichier = db.Column(db.String(255), nullable=False)
    mime = db.Column(db.String(60), nullable=False)
    taille = db.Column(db.Integer, nullable=False)
    contenu = db.deferred(db.Column(db.LargeBinary, nullable=False))
    ajoute_le = db.Column(db.DateTime, nullable=False, default=now)
    ajoute_par_id = db.Column(db.Integer, db.ForeignKey("utilisateurs.id"))

    ajoute_par = db.relationship("User")
    __table_args__ = (db.Index("ix_pieces_objet", "objet", "objet_id"),)

    @property
    def est_pdf(self):
        return self.mime == "application/pdf"

    @property
    def est_image(self):
        return self.mime.startswith("image/")


class Parametre(db.Model):
    __tablename__ = "parametres"
    cle = db.Column(db.String(60), primary_key=True)
    valeur = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=now, onupdate=now)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("utilisateurs.id"))


class Journal(db.Model):
    __tablename__ = "journal"
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, nullable=False, default=now, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("utilisateurs.id"))
    action = db.Column(db.String(120), nullable=False)
    cible = db.Column(db.String(160))
    details = db.Column(db.Text)
    user = db.relationship("User")


def migrer():
    """Crée les tables manquantes. Ne supprime ni ne modifie aucune donnée (rejouable à chaque démarrage).

    Pour une future colonne : l'ajouter dans COLONNES_AJOUTEES (ALTER TABLE … ADD COLUMN si absente).
    """
    from sqlalchemy import inspect, text
    db.create_all()
    COLONNES_AJOUTEES = {}
    insp = inspect(db.engine)
    with db.engine.begin() as cx:
        for table, colonnes in COLONNES_AJOUTEES.items():
            existantes = {c["name"] for c in insp.get_columns(table)}
            for nom, ddl in colonnes:
                if nom not in existantes:
                    cx.execute(text(f"ALTER TABLE {table} ADD COLUMN {nom} {ddl}"))
