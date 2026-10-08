"""Tests de non-régression : périmètres d'accès, flux de notation, écrans, exports.

Lancer :  pytest -q
"""
import io
import re

import pytest
from sqlalchemy import select

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import (Affectation, Critere, Employe, Evaluation, Role,
                        StatutEvaluation, Utilisateur)
from app.services.notation import (calculer_note_globale, niveau,
                                   periode_courante)

MDP = "Futura2026!"


@pytest.fixture()
def app():
    app = create_app(TestConfig)

    # Le contexte d'application reste ouvert pendant le test (pour interroger la base) :
    # Flask le réutilise alors pour chaque requête, donc on vide `g` entre deux requêtes
    # comme le ferait un vrai serveur.
    def _isoler_requete():
        from flask import g
        g.pop("_login_user", None)
        g.pop("_perimetre", None)
    # Doit passer avant les autres before_request de l'application
    app.before_request_funcs.setdefault(None, []).insert(0, _isoler_requete)

    with app.app_context():
        db.create_all()
        runner = app.test_cli_runner()
        res = runner.invoke(args=["demo", "--mdp", MDP])
        assert "Démo chargée" in res.output, res.output
        yield app
        db.session.remove()
        db.drop_all()


def login(client, email):
    r = client.post("/connexion", data={"email": email, "mot_de_passe": MDP})
    assert r.status_code == 302, r.data[:300]


def email_de(nom):
    return db.session.scalar(select(Employe.email).where(Employe.nom == nom))


# ---------------------------------------------------------------- calculs
def test_calcul_note_globale():
    assert calculer_note_globale([(100, 1), (100, 3)]) == 100.0
    assert calculer_note_globale([(60, 1)]) == 60.0
    assert calculer_note_globale([(40, 3), (80, 1)]) == 50.0  # (120+80)/4
    assert calculer_note_globale([(0, 2)]) == 0.0
    assert calculer_note_globale([]) is None
    assert niveau(80)[0] == "excellent" and niveau(49.99)[0] == "insuffisant" and niveau(0)[0] == "insuffisant"


# ---------------------------------------------------------------- pages
@pytest.mark.parametrize("nom", ["MBALLA", "NGUEMA", "FOTSO", "ESSOMBA"])
def test_toutes_les_pages_repondent(app, nom):
    c = app.test_client()
    login(c, email_de(nom))
    for url in ["/", "/evaluations/", "/suivi/", "/suivi/?vue=mensuelle", "/suivi/?vue=annuelle",
                "/personnel/", "/mot-de-passe"]:
        r = c.get(url)
        assert r.status_code == 200, (nom, url, r.status_code)
    est_rh = nom == "MBALLA"
    for url in ["/import/", "/parametres/departements", "/parametres/projets",
                "/parametres/criteres", "/parametres/utilisateurs", "/journal/", "/personnel/nouveau",
                "/parametres/postes", "/parametres/claude"]:
        assert c.get(url).status_code == (200 if est_rh else 403), (nom, url)


def test_exports(app):
    c = app.test_client()
    login(c, email_de("MBALLA"))
    for url in ["/suivi/?export=xlsx", "/suivi/?export=pdf", "/personnel/?export=xlsx",
                "/personnel/?export=pdf", "/evaluations/?export=xlsx", "/journal/?export=xlsx",
                "/import/modele.xlsx"]:
        r = c.get(url)
        assert r.status_code == 200, url
        assert len(r.data) > 1000, url


# ---------------------------------------------------------------- périmètres
def test_chef_de_projet_ne_voit_pas_l_autre_projet(app):
    c = app.test_client()
    login(c, email_de("FOTSO"))  # chef du projet BALI
    page = c.get("/suivi/?vue=annuelle").get_data(as_text=True)
    assert "ESSOMBA" in page      # projet BALI
    assert "NJOYA" not in page    # projet CHR Buea → invisible
    assert "NDJOCK" not in page   # département DAF → invisible
    # Accès direct à une évaluation hors périmètre → 403
    njoya = db.session.scalar(select(Employe).where(Employe.nom == "NJOYA"))
    ev = db.session.scalar(select(Evaluation).where(Evaluation.employe_id == njoya.id))
    if ev:
        assert c.get(f"/evaluations/{ev.id}").status_code == 403
    assert c.get(f"/personnel/{njoya.id}").status_code == 403


def test_direction_voit_tout(app):
    c = app.test_client()
    login(c, email_de("NGUEMA"))
    page = c.get("/suivi/?vue=annuelle").get_data(as_text=True)
    for nom in ("ESSOMBA", "NJOYA", "NDJOCK"):
        assert nom in page


def test_double_rattachement_cloisonne(app):
    """ESSOMBA est sur BALI (N+1 FOTSO) et en Direction technique (N+1 TCHOUA)."""
    essomba = db.session.scalar(select(Employe).where(Employe.nom == "ESSOMBA"))
    fotso = db.session.scalar(select(Employe).where(Employe.nom == "FOTSO"))
    c = app.test_client()
    login(c, fotso.email)
    page = c.get(f"/personnel/{essomba.id}").get_data(as_text=True)
    assert "Projet Immeuble BALI R+11" in page
    assert "Dépt. Direction technique" not in page  # note du dépt. invisible pour le chef de projet


def test_salarie_ne_voit_que_ses_notes_soumises(app):
    essomba = db.session.scalar(select(Employe).where(Employe.nom == "ESSOMBA"))
    a, m = periode_courante()
    aff = db.session.scalar(select(Affectation).where(Affectation.employe_id == essomba.id))
    ev = Evaluation(affectation_id=aff.id, annee=a, mois=m, statut=StatutEvaluation.BROUILLON,
                    employe_id=essomba.id, evaluateur_id=aff.evaluateur_id, projet_id=aff.projet_id,
                    departement_id=aff.departement_id)
    db.session.add(ev)
    db.session.commit()
    c = app.test_client()
    login(c, essomba.email)
    assert c.get(f"/evaluations/{ev.id}").status_code == 403  # brouillon invisible pour l'évalué


def test_auto_evaluation_interdite(app):
    fotso = db.session.scalar(select(Employe).where(Employe.nom == "FOTSO"))
    aff = db.session.scalar(select(Affectation).where(Affectation.employe_id == fotso.id))
    c = app.test_client()
    login(c, fotso.email)
    assert c.get(f"/evaluations/noter/{aff.id}").status_code == 403


# ---------------------------------------------------------------- flux de notation
def test_flux_brouillon_puis_soumission(app):
    fotso = db.session.scalar(select(Employe).where(Employe.nom == "FOTSO"))
    owona = db.session.scalar(select(Employe).where(Employe.nom == "OWONA"))
    aff = db.session.scalar(select(Affectation).where(Affectation.employe_id == owona.id))
    assert aff.evaluateur_id == fotso.id
    criteres = db.session.scalars(select(Critere).where(Critere.actif.is_(True))).all()
    a, m = periode_courante()
    c = app.test_client()
    login(c, fotso.email)
    url = f"/evaluations/noter/{aff.id}?annee={a}&mois={m}"
    assert c.get(url).status_code == 200

    # Soumission incomplète refusée
    r = c.post(url, data={"action": "soumettre", f"note_{criteres[0].id}": "70"})
    assert r.status_code == 200 and "Note obligatoire" in r.get_data(as_text=True)

    # Note 1 sans justification refusée
    # Note hors bornes refusée
    r = c.post(url, data={**{f"note_{x.id}": "75" for x in criteres}, f"note_{criteres[0].id}": "120", "action": "brouillon"})
    assert "0 à 100" in r.get_data(as_text=True)

    data = {f"note_{x.id}": "80" for x in criteres}
    data[f"note_{criteres[0].id}"] = "30"
    r = c.post(url, data={**data, "action": "soumettre"})
    assert "Justifiez" in r.get_data(as_text=True)

    # Brouillon OK
    data[f"note_{criteres[0].id}"] = "80"
    r = c.post(url, data={**data, "action": "brouillon", "commentaire": "En progrès"})
    assert r.status_code == 302
    ev = db.session.scalar(select(Evaluation).where(Evaluation.affectation_id == aff.id,
                                                    Evaluation.annee == a, Evaluation.mois == m))
    assert ev.statut == "brouillon" and ev.note_globale == 80.0

    # Soumission OK, puis plus modifiable
    r = c.post(url, data={**data, "action": "soumettre", "commentaire": "Très bon mois"})
    assert r.status_code == 302
    db.session.refresh(ev)
    assert ev.statut == "soumise" and ev.commentaire == "Très bon mois"
    assert c.get(url).status_code == 302  # redirige vers le détail

    # La RH rouvre
    rh = app.test_client()
    login(rh, email_de("MBALLA"))
    assert rh.post(f"/evaluations/{ev.id}/rouvrir", data={"motif": "Erreur"}).status_code == 302
    db.session.refresh(ev)
    assert ev.statut == "brouillon" and ev.rouverte


def test_periode_close_refusee(app):
    fotso = db.session.scalar(select(Employe).where(Employe.nom == "FOTSO"))
    owona = db.session.scalar(select(Employe).where(Employe.nom == "OWONA"))
    aff = db.session.scalar(select(Affectation).where(Affectation.employe_id == owona.id))
    a, _ = periode_courante()
    c = app.test_client()
    login(c, fotso.email)
    r = c.post(f"/evaluations/noter/{aff.id}?annee={a - 1}&mois=1", data={"action": "brouillon"})
    assert r.status_code == 302
    assert not db.session.scalar(select(Evaluation).where(Evaluation.affectation_id == aff.id,
                                                          Evaluation.annee == a - 1))


# ---------------------------------------------------------------- import
def test_import_modele_excel_sans_ia(app):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Matricule", "Nom", "Prénom", "Poste", "Email", "Téléphone", "Département", "Projet", "N+1 (nom ou matricule)"])
    ws.append(["", "ATANGANA", "Rose", "Secrétaire", "rose@x.cm", "", "Logistique", "", "FUT-004"])
    ws.append(["T-2", "BIYA", "Luc", "Chauffeur", "", "", "Logistique", "CHR Buea", "ATANGANA Rose"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    c = app.test_client()
    login(c, email_de("MBALLA"))
    r = c.post("/import/analyser", data={"fichier": (buf, "liste.xlsx")}, content_type="multipart/form-data")
    assert r.status_code == 302 and "/verifier" in r.location
    page = c.get(r.location).get_data(as_text=True)
    assert "ATANGANA" in page and "Nouveau dépt." in page and "Créer « Logistique »" in page
    bid = int(re.search(r"/import/(\d+)/", r.location).group(1))
    form = {"nb_lignes": "2", "creer_contextes": "1"}
    for i, p in enumerate([("", "ATANGANA", "Rose", "Secrétaire", "rose@x.cm", "", "Logistique", "", "FUT-004"),
                           ("T-2", "BIYA", "Luc", "Chauffeur", "", "", "Logistique", "CHR Buea", "ATANGANA Rose")]):
        form[f"r{i}_inclure"] = "1"
        for k, v in zip(["matricule", "nom", "prenom", "poste", "email", "telephone", "departement", "projet", "n_plus_1"], p):
            form[f"r{i}_{k}"] = v
    r = c.post(f"/import/{bid}/valider", data=form)
    assert r.status_code == 302
    biya = db.session.scalar(select(Employe).where(Employe.nom == "BIYA"))
    rose = db.session.scalar(select(Employe).where(Employe.nom == "ATANGANA"))
    assert rose.matricule.startswith("FUT-")
    assert len(biya.affectations) == 2
    assert all(a.evaluateur_id == rose.id for a in biya.affectations)
    # Rose a un N-1 et un e-mail : son compte est créé automatiquement, accès à remettre
    assert rose.compte and rose.compte.email == "rose@x.cm" and rose.compte.en_attente_acces
    # Les nouveaux postes rejoignent le référentiel
    from app.models import Poste
    assert db.session.scalar(select(Poste).where(Poste.libelle == "Secrétaire"))


def test_import_ia_sans_cle(app):
    c = app.test_client()
    login(c, email_de("MBALLA"))
    r = c.post("/import/analyser", data={"texte": "DUPONT Jean, maçon"}, follow_redirects=True)
    assert "Clé API Claude" in r.get_data(as_text=True)


# ---------------------------------------------------------------- sécurité
def test_blocage_apres_echecs(app):
    c = app.test_client()
    email = email_de("FOTSO")
    for _ in range(5):
        c.post("/connexion", data={"email": email, "mot_de_passe": "faux"})
    r = c.post("/connexion", data={"email": email, "mot_de_passe": MDP})
    assert r.status_code == 200 and "bloqué" in r.get_data(as_text=True)


def test_changement_mdp_force(app):
    u = Utilisateur(email="nouveau@x.cm", role=Role.COLLABORATEUR, doit_changer_mdp=True)
    u.set_password(MDP)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    login(c, "nouveau@x.cm")
    r = c.get("/")
    assert r.status_code == 302 and "/mot-de-passe" in r.location


def test_import_claude_simule(app, monkeypatch):
    """Vérifie l'appel à l'API Claude (client simulé) et le passage à l'écran de vérification."""
    import anthropic

    appels = {}

    class Bloc:
        type = "tool_use"
        input = {"personnes": [{"nom": "MVONDO", "prenom": "Éric", "poste": "Grutier", "projet": "Immeuble BALI R+11",
                                "n_plus_1": "FOTSO Jean-Marc", "confiance": "basse", "remarque": "Prénom peu lisible"}],
                 "remarques": "Photo légèrement floue"}

    class Messages:
        def create(self, **kw):
            appels.update(kw)
            return type("R", (), {"content": [Bloc()]})()

    class Client:
        def __init__(self, **kw):
            self.messages = Messages()

    monkeypatch.setattr(anthropic, "Anthropic", Client)
    app.config["ANTHROPIC_API_KEY"] = "sk-test"
    c = app.test_client()
    login(c, email_de("MBALLA"))
    photo = io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    r = c.post("/import/analyser", data={"fichier": (photo, "liste.png")}, content_type="multipart/form-data")
    assert r.status_code == 302 and "/verifier" in r.location
    assert appels["tool_choice"]["name"] == "enregistrer_personnel"
    assert appels["messages"][0]["content"][0]["type"] == "image"
    page = c.get(r.location).get_data(as_text=True)
    assert "MVONDO" in page and "Photo légèrement floue" in page and "faible confiance" in page


# ---------------------------------------------------------------- comptes automatiques, listes, clé API
def test_creation_salarie_avec_rattachement_et_poste(app):
    from app.models import Departement, Poste
    c = app.test_client()
    login(c, email_de("MBALLA"))
    tech = db.session.scalar(select(Departement).where(Departement.code == "TECH"))
    r = c.post("/personnel/nouveau", data={"nom": "Ondoa", "prenom": "Félix", "poste": "__nouveau__",
                                          "poste_nouveau": "Grutier", "departement_id": str(tech.id)})
    assert r.status_code == 302
    e = db.session.scalar(select(Employe).where(Employe.nom == "ONDOA"))
    assert e.matricule.startswith("FUT-") and e.poste == "Grutier"
    assert db.session.scalar(select(Poste).where(Poste.libelle == "Grutier"))
    # Sans N+1 choisi : le responsable du département note
    assert e.affectations[0].evaluateur_id == tech.responsable_id
    # E-mail en double refusé (c'est l'identifiant de connexion)
    r = c.post("/personnel/nouveau", data={"nom": "Doublon", "email": email_de("FOTSO")})
    assert r.status_code == 200 and "déjà utilisé" in r.get_data(as_text=True)


def test_compte_cree_quand_on_devient_n1_puis_acces(app):
    c = app.test_client()
    login(c, email_de("MBALLA"))
    c.post("/personnel/nouveau", data={"nom": "NKOA", "prenom": "Yves", "email": "Yves.Nkoa@x.cm"})
    yves = db.session.scalar(select(Employe).where(Employe.nom == "NKOA"))
    assert yves.compte is None  # pas encore de N-1 : pas de compte
    owona = db.session.scalar(select(Employe).where(Employe.nom == "OWONA"))
    aff = owona.affectations_actives[0]
    c.post(f"/personnel/affectations/{aff.id}", data={"action": "evaluateur", "evaluateur_id": str(yves.id)})
    db.session.refresh(yves)
    assert yves.compte and yves.compte.email == "yves.nkoa@x.cm" and yves.compte.en_attente_acces
    page = c.get("/parametres/utilisateurs").get_data(as_text=True)
    assert "Générer les accès" in page
    r = c.post("/parametres/utilisateurs", data={"action": "generer_attente"})
    html = r.get_data(as_text=True)
    mdp = re.search(r"yves\.nkoa@x\.cm</span></td><td><span class=\"code kbd-copy\"[^>]*>([A-Za-z0-9]{12})<", html).group(1)
    c2 = app.test_client()
    r = c2.post("/connexion", data={"email": "yves.nkoa@x.cm", "mot_de_passe": mdp})
    assert r.status_code == 302
    assert "/mot-de-passe" in c2.get("/").location
    # L'identifiant suit l'e-mail de la fiche
    r = c.post(f"/personnel/{yves.id}/modifier", data={"matricule": yves.matricule, "nom": "NKOA", "email": "y.nkoa@x.cm"})
    assert r.status_code == 302 and "/personnel/" in r.location, r.location
    db.session.refresh(yves.compte)
    assert yves.compte.email == "y.nkoa@x.cm"


def test_compte_existant_lie_par_email(app):
    u = Utilisateur(email="dg.externe@x.cm", role=Role.DIRECTION, doit_changer_mdp=False)
    u.set_password(MDP)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    login(c, email_de("MBALLA"))
    c.post("/personnel/nouveau", data={"nom": "ABOUEM", "email": "DG.Externe@x.cm"})
    e = db.session.scalar(select(Employe).where(Employe.nom == "ABOUEM"))
    db.session.refresh(u)
    assert u.employe_id == e.id and u.role == Role.DIRECTION


def test_cle_api_saisie_dans_l_application(app, monkeypatch):
    from app.models import Parametre
    from app.services import reglages
    monkeypatch.setattr(reglages, "tester_cle_api", lambda cle: (True, "ok"))
    app.config["ANTHROPIC_API_KEY"] = ""
    c = app.test_client()
    login(c, email_de("MBALLA"))
    assert "Saisir la clé API" in c.get("/import/").get_data(as_text=True)
    r = c.post("/parametres/claude", data={"action": "enregistrer", "cle": "pas-une-cle"})
    assert "sk-ant-" in r.get_data(as_text=True)
    cle = "sk-ant-api03-" + "x" * 40 + "WXYZ"
    r = c.post("/parametres/claude", data={"action": "enregistrer", "cle": cle})
    assert r.status_code == 302
    stockee = db.session.get(Parametre, reglages.CLE_API).valeur
    assert cle not in stockee  # chiffrée en base
    assert reglages.cle_api() == cle and reglages.source_cle_api() == "app"
    page = c.get("/parametres/claude").get_data(as_text=True)
    assert "WXYZ" in page and cle not in page  # affichée masquée
    assert "Saisir la clé API" not in c.get("/import/").get_data(as_text=True)
    # La variable d'environnement reste prioritaire
    app.config["ANTHROPIC_API_KEY"] = "sk-ant-env"
    assert reglages.cle_api() == "sk-ant-env"
    # Un collaborateur n'a pas accès à l'écran
    c2 = app.test_client()
    login(c2, email_de("FOTSO"))
    assert c2.get("/parametres/claude").status_code == 403
