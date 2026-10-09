"""Commandes d'administration :  flask init-admin | flask demo"""
import os
import random
import secrets
import unicodedata
from datetime import date

import click
from sqlalchemy import select

from .extensions import db
from .models import (Affectation, Critere, Departement, Employe, Entite, Evaluation,
                     EvaluationNote, Projet, Role, StatutEvaluation,
                     Utilisateur)
from .services.notation import calculer_note_globale

CRITERES_DEFAUT = [
    ("Qualité du travail", "Exactitude, finition, conformité aux plans et aux normes", 3),
    ("Respect des délais", "Tenue du planning, réactivité, livrables à l'heure", 3),
    ("Productivité", "Volume de travail réalisé au regard des objectifs", 2),
    ("Sécurité & HSE", "Port des EPI, respect des consignes, propreté du poste", 3),
    ("Assiduité & ponctualité", "Présence, respect des horaires", 2),
    ("Esprit d'équipe", "Coopération, entraide, relations avec les collègues", 1),
    ("Initiative & autonomie", "Propositions d'amélioration, capacité à résoudre seul", 1),
    ("Communication & reporting", "Clarté, remontée d'informations, rapports", 1),
]


def creer_criteres_defaut(entite_id=None):
    """Grille par défaut d'une entité (None = entité principale), seulement si elle n'en a aucune."""
    q = select(Critere.id)
    if entite_id is None:
        q = q.where(Critere.entite_id.is_(None) | Critere.entite_id.in_(
            select(Entite.id).where(Entite.principale.is_(True))))
    else:
        q = q.where(Critere.entite_id == entite_id)
    if db.session.scalar(q.limit(1)):
        return 0
    for i, (lib, desc, poids) in enumerate(CRITERES_DEFAUT, 1):
        db.session.add(Critere(libelle=lib, description=desc, poids=poids, ordre=i, entite_id=entite_id))
    return len(CRITERES_DEFAUT)


def register_cli(app):
    @app.cli.command("init-admin")
    def init_admin():
        """Crée le compte RH initial (ADMIN_EMAIL / ADMIN_PASSWORD) et la grille par défaut."""
        email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
        mdp = os.environ.get("ADMIN_PASSWORD") or ""
        n = creer_criteres_defaut()
        if n:
            click.echo(f"{n} critères KPI par défaut créés.")
        if not email:
            db.session.commit()
            click.echo("ADMIN_EMAIL non défini : aucun compte créé.")
            return
        if db.session.scalar(select(Utilisateur).where(Utilisateur.email == email)):
            db.session.commit()
            click.echo(f"Le compte {email} existe déjà (inchangé).")
            return
        if len(mdp) < 10:
            mdp = secrets.token_urlsafe(10)
            click.echo(f"Mot de passe temporaire généré : {mdp}")
        u = Utilisateur(email=email, role=Role.RH, doit_changer_mdp=True, acces_remis_le=date.today())
        u.set_password(mdp)
        db.session.add(u)
        db.session.commit()
        click.echo(f"Compte RH créé : {email} (changement de mot de passe exigé à la 1re connexion).")

    @app.cli.command("demo")
    @click.option("--mdp", default="Futura2026!", help="Mot de passe des comptes de démonstration")
    def demo(mdp):
        """Charge un jeu de données de démonstration (NE PAS utiliser en production)."""
        if db.session.scalar(select(Employe.id).limit(1)):
            click.echo("Des salariés existent déjà : démo ignorée.")
            return
        creer_criteres_defaut()
        db.session.flush()

        def emp(mat, nom, prenom, poste):
            base = unicodedata.normalize("NFKD", f"{prenom.split()[0]}.{nom}".lower())
            base = "".join(c for c in base if not unicodedata.combining(c))
            e = Employe(matricule=mat, nom=nom, prenom=prenom, poste=poste,
                        email=f"{base}@futura-demo.cm",
                        date_embauche=date(2021, random.randint(1, 12), 1))
            db.session.add(e)
            return e

        dg = emp("FUT-001", "NGUEMA", "Paul", "Directeur général")
        drh = emp("FUT-002", "MBALLA", "Christine", "Responsable RH")
        dt = emp("FUT-003", "TCHOUA", "Hervé", "Directeur technique")
        daf = emp("FUT-004", "EKANE", "Sandrine", "Directrice administrative et financière")
        cp1 = emp("FUT-010", "FOTSO", "Jean-Marc", "Chef de projet")
        cp2 = emp("FUT-011", "ABENA", "Patricia", "Cheffe de projet")
        ouvriers = [
            emp("FUT-101", "ESSOMBA", "Martin", "Conducteur de travaux"),
            emp("FUT-102", "NJOYA", "Ibrahim", "Chef de chantier"),
            emp("FUT-103", "OWONA", "Brice", "Ferrailleur"),
            emp("FUT-104", "KAMDEM", "Alain", "Maçon"),
            emp("FUT-105", "BELLO", "Aminatou", "Responsable HSE"),
            emp("FUT-106", "MANGA", "Serge", "Topographe"),
            emp("FUT-107", "NANA", "Laure", "Métreuse"),
            emp("FUT-108", "ETOA", "Rodrigue", "Coffreur"),
        ]
        compta = [emp("FUT-201", "NDJOCK", "Esther", "Comptable"),
                  emp("FUT-202", "MOUKOKO", "Didier", "Assistant comptable")]
        db.session.flush()

        d_tech = Departement(code="TECH", nom="Direction technique", responsable_id=dt.id)
        d_daf = Departement(code="DAF", nom="Administration & Finances", responsable_id=daf.id)
        d_dg = Departement(code="DG", nom="Direction générale", responsable_id=dg.id)
        p1 = Projet(code="BALI", nom="Immeuble BALI R+11", localisation="Douala, Bonapriso",
                    responsable_id=cp1.id, date_debut=date(2025, 3, 1))
        p2 = Projet(code="CHR", nom="CHR Buea", localisation="Buea", responsable_id=cp2.id,
                    date_debut=date(2025, 9, 1))
        db.session.add_all([d_tech, d_daf, d_dg, p1, p2])
        db.session.flush()

        def aff(e, dept=None, proj=None, n1=None):
            a = Affectation(employe_id=e.id, departement_id=dept.id if dept else None,
                            projet_id=proj.id if proj else None, evaluateur_id=n1.id if n1 else None)
            db.session.add(a)
            return a

        affs = [aff(drh, d_dg, n1=dg), aff(dt, d_dg, n1=dg), aff(daf, d_dg, n1=dg),
                aff(cp1, d_tech, n1=dt), aff(cp2, d_tech, n1=dt)]
        for i, o in enumerate(ouvriers):
            affs.append(aff(o, proj=p1 if i % 2 == 0 else p2, n1=cp1 if i % 2 == 0 else cp2))
        affs.append(aff(ouvriers[0], d_tech, n1=dt))  # double rattachement
        affs.append(aff(ouvriers[4], d_tech, n1=dt))
        for c in compta:
            affs.append(aff(c, d_daf, n1=daf))
        db.session.flush()

        def compte(e, role):
            u = Utilisateur(email=e.email, role=role, employe_id=e.id, doit_changer_mdp=False,
                            acces_remis_le=date.today())
            u.set_password(mdp)
            db.session.add(u)

        compte(drh, Role.RH)
        compte(dg, Role.DIRECTION)
        for e in (dt, daf, cp1, cp2, ouvriers[0]):
            compte(e, Role.COLLABORATEUR)

        criteres = db.session.scalars(select(Critere).order_by(Critere.ordre)).all()
        today = date.today()
        random.seed(42)
        for a in affs:
            base = random.uniform(52, 88)
            for m in range(1, today.month):
                notes = [max(0, min(100, round(base + random.uniform(-12, 12)))) for _ in criteres]
                ev = Evaluation(affectation_id=a.id, annee=today.year, mois=m,
                                statut=StatutEvaluation.SOUMISE, employe_id=a.employe_id,
                                evaluateur_id=a.evaluateur_id, departement_id=a.departement_id,
                                projet_id=a.projet_id,
                                commentaire=random.choice([
                                    "Bon mois, engagement constant sur le chantier.",
                                    "Doit améliorer le reporting hebdomadaire.",
                                    "Très bonne tenue des délais malgré la saison des pluies.",
                                    "Rappel effectué sur le port des EPI.", ""]))
                for c, n in zip(criteres, notes):
                    ev.notes.append(EvaluationNote(critere_id=c.id, note=n, poids=c.poids))
                ev.note_globale = calculer_note_globale([(n, c.poids) for c, n in zip(criteres, notes)])
                db.session.add(ev)
        db.session.commit()
        click.echo(f"Démo chargée. Comptes (mot de passe : {mdp}) :")
        for u in db.session.scalars(select(Utilisateur).where(Utilisateur.email.like("%futura-demo%"))):
            click.echo(f"  {u.role:<14} {u.email}")
