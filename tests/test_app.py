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
        for k in ("_login_user", "_perimetre", "_entite_courante", "_entite_principale", "_carte_projets", "_ids_groupe", "_parents_dep", "_reglages_grilles", "_postes_entites"):
            g.pop(k, None)
    # Doit passer avant les autres before_request de l'application
    app.before_request_funcs.setdefault(None, []).insert(0, _isoler_requete)

    with app.app_context():
        db.create_all()
        # Comme la migration multi-entités : l'entité principale existe d'emblée
        from app.models import Entite
        db.session.add(Entite(code="FUT", nom="FUTURA", couleur="#213E70", principale=True, actif=True))
        db.session.commit()
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
    assert niveau(90)[0] == "excellent" and niveau(89.99)[0] == "tres-bien"
    assert niveau(49.99)[0] == "insuffisant" and niveau(0)[0] == "insuffisant"


# ---------------------------------------------------------------- pages
@pytest.mark.parametrize("nom", ["MBALLA", "NGUEMA", "FOTSO", "ESSOMBA"])
def test_toutes_les_pages_repondent(app, nom):
    c = app.test_client()
    login(c, email_de(nom))
    for url in ["/", "/evaluations/", "/suivi/", "/suivi/?vue=mensuelle", "/suivi/?vue=annuelle",
                "/personnel/"]:
        r = c.get(url)
        assert r.status_code == 200, (nom, url, r.status_code)
    est_rh = nom == "MBALLA"
    for url in ["/import/", "/parametres/departements", "/parametres/projets",
                "/parametres/criteres", "/parametres/utilisateurs", "/journal/", "/personnel/nouveau",
                "/parametres/postes", "/parametres/claude"]:
        assert c.get(url).status_code == (200 if est_rh else 403), (nom, url)
    # Les mots de passe sont attribués par le superadmin : personne d'autre ne change le sien
    assert c.get("/mot-de-passe").status_code == 403


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
    # Projet : N+1 indiqué dans le fichier ; département (nouveau, sans responsable) : vide
    assert next(a for a in biya.affectations if a.projet_id).evaluateur_id == rose.id
    assert next(a for a in biya.affectations if a.departement_id).evaluateur_id is None
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


def test_mots_de_passe_attribues_par_le_superadmin(app):
    # Un utilisateur ordinaire n'est jamais forcé à changer, et ne peut pas changer son mot de passe
    u = Utilisateur(email="nouveau@x.cm", role=Role.COLLABORATEUR, doit_changer_mdp=True)
    u.set_password(MDP)
    db.session.add(u)
    sa = Utilisateur(email="super@x.cm", role=Role.COLLABORATEUR, doit_changer_mdp=True)
    sa.set_password(MDP)
    db.session.add(sa)
    db.session.commit()
    c = app.test_client()
    login(c, "nouveau@x.cm")
    assert c.get("/").status_code == 200
    assert c.get("/mot-de-passe").status_code == 403
    # Le superadmin, lui, choisit son propre mot de passe
    c2 = app.test_client()
    login(c2, "super@x.cm")
    r = c2.get("/")
    assert r.status_code == 302 and "/mot-de-passe" in r.location
    # La RH ne peut pas attribuer de mot de passe ; le superadmin oui
    rh = app.test_client()
    login(rh, email_de("MBALLA"))
    assert rh.post("/parametres/utilisateurs", data={"action": "definir_mdp", "id": u.id,
                                                    "mot_de_passe": "Choisi12345"}).status_code == 403
    c2.post("/mot-de-passe", data={"actuel": MDP, "nouveau": "SuperAdmin2026", "confirmation": "SuperAdmin2026"})
    r = c2.post("/parametres/utilisateurs", data={"action": "definir_mdp", "id": u.id, "mot_de_passe": "Choisi12345"})
    assert r.status_code == 200 and "Choisi12345" in r.get_data(as_text=True)
    c3 = app.test_client()
    assert c3.post("/connexion", data={"email": "nouveau@x.cm", "mot_de_passe": "Choisi12345"}).status_code == 302
    assert c3.get("/").status_code == 200  # pas de changement imposé


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
    # Département : le N+1 est automatiquement le responsable du département
    assert e.affectations[0].evaluateur_id == tech.responsable_id
    r = c.post("/personnel/nouveau", data={"nom": "Mvogo", "departement_id": str(tech.id),
                                          "evaluateur_id": str(db.session.scalar(select(Employe.id).where(Employe.nom == "FOTSO")))})
    mvogo = db.session.scalar(select(Employe).where(Employe.nom == "MVOGO"))
    assert mvogo.affectations[0].evaluateur_id == tech.responsable_id   # le choix manuel ne s'applique pas
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
    assert "seul le superadmin" in page  # la RH voit l'attente mais n'attribue pas
    assert c.post("/parametres/utilisateurs", data={"action": "generer_attente"}).status_code == 403
    sa = Utilisateur(email="super@x.cm", role=Role.COLLABORATEUR, doit_changer_mdp=False)
    sa.set_password(MDP)
    db.session.add(sa)
    db.session.commit()
    csa = app.test_client()
    login(csa, "super@x.cm")
    r = csa.post("/parametres/utilisateurs", data={"action": "generer_attente"})
    html = r.get_data(as_text=True)
    mdp = re.search(r"yves\.nkoa@x\.cm</span></td><td><span class=\"code kbd-copy\"[^>]*>([A-Za-z0-9]{12})<", html).group(1)
    c2 = app.test_client()
    r = c2.post("/connexion", data={"email": "yves.nkoa@x.cm", "mot_de_passe": mdp})
    assert r.status_code == 302
    assert c2.get("/").status_code == 200  # mot de passe attribué : pas de changement imposé
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


def test_erreurs_claude_expliquees():
    """Un compte sans crédit renvoie 400 : le message doit donner la vraie cause, pas « image plus nette »."""
    from app.services.import_ia import traduire_erreur_api

    class Exc:
        status_code = 400
        def __init__(self, message):
            self.message = message
    credit = traduire_erreur_api(Exc("Your credit balance is too low to access the Anthropic API."))
    assert "crédit" in credit and "Billing" in credit
    modele = traduire_erreur_api(Exc("model: claude-xyz not found"))
    assert "ANTHROPIC_MODEL" in modele
    autre = traduire_erreur_api(Exc("messages.0.content: something unexpected"))
    assert "something unexpected" in autre and "code 400" in autre


def test_repli_tool_choice_refuse(app, monkeypatch):
    """Si le modèle refuse l'outil forcé (400), l'import réessaie en mode auto."""
    import anthropic

    class Refus(anthropic.BadRequestError):
        def __init__(self, message):  # pas de vraie réponse HTTP dans le test
            Exception.__init__(self, message)
            self.message, self.status_code = message, 400
            self.body = {"error": {"type": "invalid_request_error", "message": message}}

    appels = []

    class Bloc:
        type = "tool_use"
        input = {"personnes": [{"nom": "TEST", "prenom": "Un", "confiance": "haute"}]}

    class Messages:
        def create(self, **kw):
            appels.append(kw["tool_choice"]["type"])
            if kw["tool_choice"]["type"] == "tool":
                raise Refus("tool_choice: forced tool use is not compatible with thinking")
            return type("R", (), {"content": [Bloc()]})()

    class Client:
        def __init__(self, **kw):
            self.messages = Messages()

    monkeypatch.setattr(anthropic, "Anthropic", Client)
    app.config["ANTHROPIC_API_KEY"] = "sk-test"
    c = app.test_client()
    login(c, email_de("MBALLA"))
    r = c.post("/import/analyser", data={"texte": "TEST Un"}, content_type="multipart/form-data")
    assert r.status_code == 302 and "/verifier" in r.location
    assert appels == ["tool", "auto"]


# ---------------------------------------------------------------- seul le N+1 désigné note
def test_seul_le_n1_designe_note(app):
    from app.models import Projet
    bali = db.session.scalar(select(Projet).where(Projet.code == "BALI"))
    fotso = db.session.scalar(select(Employe).where(Employe.nom == "FOTSO"))   # chef du projet BALI
    tchoua = db.session.scalar(select(Employe).where(Employe.nom == "TCHOUA"))  # hors du projet
    owona = db.session.scalar(select(Employe).where(Employe.nom == "OWONA"))
    aff = next(a for a in owona.affectations if a.projet_id == bali.id)
    aff.evaluateur_id = tchoua.id   # la RH désigne quelqu'un d'un autre périmètre
    db.session.commit()
    from app.services.notation import periode_courante
    an, mo = periode_courante()

    # Le chef de projet voit la ligne mais ne peut pas noter
    c = app.test_client()
    login(c, fotso.email)
    page = c.get(f"/evaluations/?annee={an}&mois={mo}").get_data(as_text=True)
    assert "OWONA" in page
    assert f"/evaluations/noter/{aff.id}" not in page
    assert c.get(f"/evaluations/noter/{aff.id}?annee={an}&mois={mo}").status_code == 403

    # Le N+1 désigné note, même s'il n'est pas sur le projet
    u = Utilisateur(email="herve.tchoua.n1@x.cm", role=Role.COLLABORATEUR, doit_changer_mdp=False)
    if not tchoua.compte:
        u.employe_id = tchoua.id
        u.set_password(MDP)
        db.session.add(u)
        db.session.commit()
    c2 = app.test_client()
    login(c2, tchoua.compte.email)
    assert c2.get(f"/evaluations/noter/{aff.id}?annee={an}&mois={mo}").status_code == 200


def test_aucune_donnee_modifiee_sans_action_rh(app):
    """init-admin (lancé à chaque démarrage) ne lie ni ne crée aucun compte, ne touche à aucune fiche."""
    from app.models import Poste
    u = Utilisateur(email="rh.initial@x.cm", role=Role.RH, doit_changer_mdp=False)
    u.set_password(MDP)
    db.session.add(u)
    e = Employe(matricule="X-1", nom="ZOA", prenom="", email="rh.initial@x.cm", poste="ingenieur")
    db.session.add(e)
    db.session.commit()
    avant = [(x.id, x.email, x.employe_id, x.role, x.password_hash) for x in db.session.scalars(select(Utilisateur))]
    nb_postes = db.session.scalar(select(db.func.count(Poste.id)))
    import os
    os.environ["ADMIN_EMAIL"] = "rh.initial@x.cm"
    try:
        res = app.test_cli_runner().invoke(args=["init-admin"])
    finally:
        os.environ.pop("ADMIN_EMAIL")
    assert "inchangé" in res.output
    db.session.expire_all()
    apres = [(x.id, x.email, x.employe_id, x.role, x.password_hash) for x in db.session.scalars(select(Utilisateur))]
    assert avant == apres
    assert db.session.get(Employe, e.id).poste == "ingenieur"
    assert db.session.scalar(select(db.func.count(Poste.id))) == nb_postes
    # Les comptes antérieurs ne sont jamais « en attente » : « Générer les accès » ne les touche pas
    assert not any(x.en_attente_acces for x in db.session.scalars(select(Utilisateur)))


# ---------------------------------------------------------------- multi-entités
def _png(couleur=(200, 30, 40)):
    from PIL import Image
    buf = io.BytesIO()
    img = Image.new("RGBA", (120, 60), (255, 255, 255, 0))
    for x in range(20, 100):
        for y in range(10, 50):
            img.putpixel((x, y), couleur + (255,))
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


def _creer_entite(c, nom="Société Test", code="STE"):
    r = c.post("/parametres/entites", data={"nom": nom, "code": code, "couleur": "#213E70", "couleur_auto": "1",
                                            "logo": (_png(), "logo.png")}, content_type="multipart/form-data")
    assert r.status_code == 302, r.get_data(as_text=True)[-2000:]
    from app.models import Entite
    return db.session.scalar(select(Entite).where(Entite.code == code))


def test_creation_entite_avec_logo_et_grille(app):
    from app.models import Critere
    c = app.test_client()
    login(c, email_de("MBALLA"))
    ent = _creer_entite(c)
    assert ent.logo and ent.logo_mime == "image/png"
    assert ent.couleur != "#213E70"  # couleur déduite du logo (rouge)
    assert int(ent.couleur[1:3], 16) > int(ent.couleur[5:7], 16)
    assert db.session.scalar(select(db.func.count(Critere.id)).where(Critere.entite_id == ent.id)) == 8
    # On bascule dans l'espace de la nouvelle entité ; les onglets apparaissent
    page = c.get("/").get_data(as_text=True)
    assert "entite-tabs" in page and "Société Test" in page and f"/entite/{ent.id}/logo" in page
    assert c.get(f"/entite/{ent.id}/logo").status_code == 200
    # SVG refusé (risque XSS)
    r = c.post("/parametres/entites", data={"nom": "X", "code": "X", "logo": (io.BytesIO(b"<svg/>"), "l.svg")},
               content_type="multipart/form-data")
    assert "PNG, JPG ou WEBP" in r.get_data(as_text=True)


def test_seuls_rh_et_superadmin_creent_des_entites(app):
    c = app.test_client()
    login(c, email_de("NGUEMA"))  # Direction
    assert c.get("/parametres/entites").status_code == 403
    u = Utilisateur(email="super@x.cm", role=Role.COLLABORATEUR, doit_changer_mdp=False)  # superadmin via config
    u.set_password(MDP)
    db.session.add(u)
    db.session.commit()
    c2 = app.test_client()
    login(c2, "super@x.cm")
    assert c2.get("/parametres/entites").status_code == 200
    # La RH ne peut pas nommer un autre compte RH ; le superadmin oui
    fotso = db.session.scalar(select(Utilisateur).where(Utilisateur.email == email_de("FOTSO")))
    c3 = app.test_client()
    login(c3, email_de("MBALLA"))
    c3.post("/parametres/utilisateurs", data={"action": "role", "id": fotso.id, "role": "rh"})
    db.session.refresh(fotso)
    assert fotso.role == Role.COLLABORATEUR
    c2.post("/parametres/utilisateurs", data={"action": "role", "id": fotso.id, "role": "rh"})
    db.session.refresh(fotso)
    assert fotso.role == Role.RH


def test_cloisonnement_et_role_par_entite(app):
    from app.models import AccesEntite, Departement
    c = app.test_client()
    login(c, email_de("MBALLA"))
    ent = _creer_entite(c)
    # Organisation et personnel de la nouvelle entité (l'onglet actif est STE)
    c.post("/parametres/departements", data={"code": "DAF", "nom": "Administration & Finances"})  # même code qu'à FUTURA
    dep = db.session.scalar(select(Departement).where(Departement.entite_id == ent.id))
    assert dep is not None
    tchoua = db.session.scalar(select(Employe).where(Employe.nom == "TCHOUA"))
    c.post("/personnel/nouveau", data={"nom": "ONANA", "matricule": "FUT-001", "departement_id": str(dep.id),
                                      "evaluateur_id": str(tchoua.id)})  # matricule déjà pris à FUTURA : autorisé ici
    onana = db.session.scalar(select(Employe).where(Employe.nom == "ONANA"))
    assert onana.entite_id == ent.id and onana.matricule == "FUT-001"
    # La liste du personnel de l'onglet STE ne montre que STE
    page = c.get("/personnel/").get_data(as_text=True)
    assert "ONANA" in page and "ESSOMBA" not in page
    # La Direction de FUTURA ne voit pas STE
    c2 = app.test_client()
    login(c2, email_de("NGUEMA"))
    assert "ONANA" not in c2.get("/personnel/").get_data(as_text=True)
    assert c2.get(f"/personnel/{onana.id}").status_code == 403
    assert c2.get(f"/entite/{ent.id}").status_code == 403

    # Rôle par entité : FOTSO, collaborateur à FUTURA, devient Direction à STE
    fotso_u = db.session.scalar(select(Utilisateur).where(Utilisateur.email == email_de("FOTSO")))
    c.post(f"/parametres/utilisateurs/{fotso_u.id}/acces", data={f"role_{ent.id}": "direction"})
    assert db.session.get(AccesEntite, (fotso_u.id, ent.id)).role == "direction"
    c3 = app.test_client()
    login(c3, fotso_u.email)
    assert "ESSOMBA" in c3.get("/personnel/").get_data(as_text=True)  # FUTURA : son périmètre (chantier BALI)
    assert "NDJOCK" not in c3.get("/personnel/").get_data(as_text=True)  # FUTURA : pas tout le personnel
    c3.get(f"/entite/{ent.id}")
    page = c3.get("/personnel/").get_data(as_text=True)
    assert "ONANA" in page and "ESSOMBA" not in page  # STE : Direction, voit tout STE


def test_n1_dans_une_autre_entite(app):
    from app.models import Critere, Departement
    from app.services.notation import periode_courante
    c = app.test_client()
    login(c, email_de("MBALLA"))
    ent = _creer_entite(c)
    tchoua = db.session.scalar(select(Employe).where(Employe.nom == "TCHOUA"))
    # TCHOUA (FUTURA, direction du groupe) dirige le département de STE : il en note les membres
    c.post("/parametres/departements", data={"code": "CHT", "nom": "Chantiers", "responsable_id": str(tchoua.id)})
    dep = db.session.scalar(select(Departement).where(Departement.entite_id == ent.id))
    c.post("/personnel/nouveau", data={"nom": "ONANA", "departement_id": str(dep.id), "evaluateur_id": str(tchoua.id)})
    c.post("/personnel/nouveau", data={"nom": "BEKONO", "departement_id": str(dep.id)})
    onana = db.session.scalar(select(Employe).where(Employe.nom == "ONANA"))
    aff = onana.affectations[0]
    # Compte de TCHOUA (N+1 désigné, de FUTURA)
    if not tchoua.compte:
        u = Utilisateur(email="tchoua.n1@x.cm", role=Role.COLLABORATEUR, employe_id=tchoua.id, doit_changer_mdp=False)
        u.set_password(MDP)
        db.session.add(u)
        db.session.commit()
    c2 = app.test_client()
    login(c2, tchoua.compte.email)
    page = c2.get("/").get_data(as_text=True)
    assert "Société Test" in page  # onglet obtenu automatiquement
    c2.get(f"/entite/{ent.id}")
    an, mo = periode_courante()
    camp = c2.get(f"/evaluations/?annee={an}&mois={mo}").get_data(as_text=True)
    assert "ONANA" in camp and "BEKONO" in camp  # responsable du département : il note tous ses membres
    r = c2.get(f"/evaluations/noter/{aff.id}?annee={an}&mois={mo}")
    assert r.status_code == 200
    # La grille utilisée est celle de STE
    crit_ste = db.session.scalars(select(Critere).where(Critere.entite_id == ent.id)).all()
    form = {"action": "soumettre", "commentaire": "Bon début"}
    for cr in crit_ste:
        form[f"note_{cr.id}"] = "75"
    r = c2.post(f"/evaluations/noter/{aff.id}?annee={an}&mois={mo}", data=form)
    assert r.status_code == 302
    ev = db.session.scalar(select(Evaluation).where(Evaluation.affectation_id == aff.id))
    assert ev.est_soumise and ev.note_globale == 75 and {n.critere_id for n in ev.notes} == {x.id for x in crit_ste}


def test_page_connexion_liste_les_entreprises(app):
    c = app.test_client()
    login(c, email_de("MBALLA"))
    ent = _creer_entite(c)
    anonyme = app.test_client()
    page = anonyme.get("/connexion").get_data(as_text=True)
    assert "Choisissez votre entreprise" in page and "FUTURA" in page and "Société Test" in page
    assert f"/entite/{ent.id}/logo" in page
    assert anonyme.get(f"/entite/{ent.id}/logo").status_code == 200  # logo visible avant connexion
    # Connexion en choisissant l'entreprise : on arrive dans son espace
    rh = app.test_client()
    rh.post("/connexion", data={"email": email_de("MBALLA"), "mot_de_passe": MDP, "entite": ent.id})
    assert "Les évaluations de" in rh.get("/").get_data(as_text=True)
    assert "chez Société Test" in rh.get("/").get_data(as_text=True)
    # Sans accès à cette entreprise : message clair, espace habituel
    d = app.test_client()
    r = d.post("/connexion", data={"email": email_de("NGUEMA"), "mot_de_passe": MDP, "entite": ent.id},
               follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "pas accès à Société Test" in html and "chez FUTURA" in html


# ---------------------------------------------------------------- sélection et suppression
def test_selection_desactiver_puis_reactiver(app):
    c = app.test_client()
    login(c, email_de("MBALLA"))
    ids = [db.session.scalar(select(Employe.id).where(Employe.nom == n)) for n in ("OWONA", "KAMDEM")]
    page = c.get("/personnel/").get_data(as_text=True)
    assert "data-selection" in page and "Supprimer…" in page
    c.post("/personnel/selection", data={"action": "desactiver", "ids": [str(i) for i in ids]})
    assert all(not db.session.get(Employe, i).actif for i in ids)
    c.post("/personnel/selection", data={"action": "reactiver", "ids": [str(i) for i in ids]})
    assert all(db.session.get(Employe, i).actif for i in ids)
    # Un collaborateur n'a pas accès à la sélection
    c2 = app.test_client()
    login(c2, email_de("FOTSO"))
    assert c2.post("/personnel/selection", data={"action": "desactiver", "ids": [str(ids[0])]}).status_code == 403


def test_suppression_individuelle_et_impacts(app):
    from app.models import JournalAction
    c = app.test_client()
    login(c, email_de("MBALLA"))
    essomba = db.session.scalar(select(Employe).where(Employe.nom == "ESSOMBA"))  # a un compte et des notes
    eid, uid = essomba.id, essomba.compte.id
    nb_evals = db.session.scalar(select(db.func.count(Evaluation.id)).where(Evaluation.employe_id == eid))
    assert nb_evals > 0
    page = c.get(f"/personnel/{eid}/supprimer").get_data(as_text=True)
    assert "Supprimer 1 fiche" in page and f"{nb_evals} évaluation(s) reçue(s)" in page
    # Sans le mot de confirmation : rien n'est supprimé
    c.post("/personnel/supprimer", data={"ids": [str(eid)], "confirmation": "oui"})
    assert db.session.get(Employe, eid) is not None
    r = c.post("/personnel/supprimer", data={"ids": [str(eid)], "confirmation": "supprimer"})
    assert r.status_code == 302
    db.session.expire_all()
    assert db.session.get(Employe, eid) is None
    assert db.session.get(Utilisateur, uid) is None
    assert db.session.scalar(select(db.func.count(Evaluation.id)).where(Evaluation.employe_id == eid)) == 0
    assert db.session.scalar(select(JournalAction).where(JournalAction.action == "employe_supprime"))
    # Les autres salariés et leurs notes sont intacts
    assert db.session.scalar(select(db.func.count(Evaluation.id))) > 0


def test_suppression_en_masse_par_filtre_et_protections(app):
    c = app.test_client()
    login(c, email_de("MBALLA"))
    mballa = db.session.scalar(select(Employe).where(Employe.nom == "MBALLA"))
    # « Tous les résultats » du filtre : uniquement les fiches correspondantes, sans la sienne
    r = c.post("/personnel/selection", data={"action": "supprimer", "tout": "1", "filtres": "q=NDJOCK"})
    page = r.get_data(as_text=True)
    assert "NDJOCK" in page and "ESSOMBA" not in page
    r = c.post("/personnel/selection", data={"action": "supprimer", "ids": [str(mballa.id)]})
    assert r.status_code == 302  # sa propre fiche est protégée : rien à supprimer
    ndjock = db.session.scalar(select(Employe.id).where(Employe.nom == "NDJOCK"))
    moukoko = db.session.scalar(select(Employe.id).where(Employe.nom == "MOUKOKO"))
    c.post("/personnel/supprimer", data={"ids": [str(ndjock), str(moukoko), str(mballa.id)], "confirmation": "SUPPRIMER"})
    db.session.expire_all()
    assert db.session.get(Employe, ndjock) is None and db.session.get(Employe, moukoko) is None
    assert db.session.get(Employe, mballa.id) is not None


# ---------------------------------------------------------------- personnel groupe et listes de rattachement
def test_direction_et_rh_visibles_dans_toutes_les_entites(app):
    c = app.test_client()
    login(c, email_de("MBALLA"))
    ent = _creer_entite(c)  # on bascule dans l'espace de la nouvelle entité
    page = c.get("/personnel/").get_data(as_text=True)
    # Directeur général, directeurs, responsable RH : visibles ; ouvriers : non
    for nom in ("NGUEMA", "TCHOUA", "MBALLA"):
        assert nom in page, nom
    assert "ESSOMBA" not in page and "Groupe, rattaché à FUTURA" in page
    # Non sélectionnables pour les actions groupées depuis une autre entité
    nguema = db.session.scalar(select(Employe).where(Employe.nom == "NGUEMA"))
    r = c.post("/personnel/selection", data={"action": "desactiver", "ids": [str(nguema.id)]})
    assert db.session.get(Employe, nguema.id).actif
    # Le poste peut être retiré de la liste « groupe » (écran Postes)
    c.post("/parametres/postes", data={"action": "groupe", "libelle": "Directeur technique"})
    assert "TCHOUA" not in c.get("/personnel/").get_data(as_text=True)
    # … et un poste ordinaire peut y être ajouté
    c.post("/parametres/postes", data={"action": "groupe", "libelle": "Topographe"})
    assert "MANGA" in c.get("/personnel/").get_data(as_text=True)
    # La Direction de la nouvelle entité peut ouvrir la fiche du DG du groupe
    assert c.get(f"/personnel/{nguema.id}").status_code == 200


def test_modifier_rattachement_par_listes(app):
    from app.models import Departement, Projet
    c = app.test_client()
    login(c, email_de("MBALLA"))
    owona = db.session.scalar(select(Employe).where(Employe.nom == "OWONA"))
    chr_ = db.session.scalar(select(Projet).where(Projet.code == "CHR"))
    tech = db.session.scalar(select(Departement).where(Departement.code == "TECH"))
    tchoua = db.session.scalar(select(Employe.id).where(Employe.nom == "TCHOUA"))
    page = c.get(f"/personnel/{owona.id}/modifier").get_data(as_text=True)
    assert 'name="departement_id"' in page and 'name="projet_id"' in page and 'name="evaluateur_id"' in page
    ancien = next(a for a in owona.affectations_actives if a.projet_id)
    r = c.post(f"/personnel/{owona.id}/modifier", data={
        "matricule": owona.matricule, "nom": owona.nom, "prenom": owona.prenom, "poste": owona.poste,
        "departement_id": str(tech.id), "projet_id": str(chr_.id), "evaluateur_id": str(tchoua)})
    assert r.status_code == 302
    db.session.refresh(owona)
    actives = owona.affectations_actives
    assert {(a.departement_id, a.projet_id) for a in actives} >= {(tech.id, None), (None, chr_.id)}
    assert all(a.evaluateur_id == tchoua for a in actives)
    if ancien.projet_id != chr_.id:
        assert not ancien.actif  # ancienne affectation clôturée, historique conservé
    # Fiche : listes séparées Département / Projet
    fiche = c.get(f"/personnel/{owona.id}").get_data(as_text=True)
    assert 'id="aff-dep"' in fiche and 'id="aff-proj"' in fiche
    daf = db.session.scalar(select(Departement).where(Departement.code == "DAF"))
    c.post(f"/personnel/{owona.id}/affectations", data={"departement_id": str(daf.id), "evaluateur_id": str(tchoua)})
    db.session.refresh(owona)
    assert any(a.departement_id == daf.id for a in owona.affectations_actives)


def test_filtre_par_poste(app):
    c = app.test_client()
    login(c, email_de("MBALLA"))
    page = c.get("/personnel/").get_data(as_text=True)
    assert 'id="flt-poste"' in page and ">Topographe<" in page
    page = c.get("/personnel/?poste=Topographe").get_data(as_text=True)
    assert "MANGA" in page and "ESSOMBA" not in page
    # Combinable avec la sélection « tous les résultats » et les exports
    r = c.get("/personnel/?poste=Topographe&export=xlsx")
    assert r.status_code == 200


def test_projet_rattache_departement_et_note_unique(app):
    """Un salarié dans un projet ET un département n'a qu'une ligne de notation (le projet)."""
    from app.models import Departement, Parametre, Projet
    from app.services.notation import periode_courante
    an, mo = periode_courante()
    bali = db.session.scalar(select(Projet).where(Projet.code == "BALI"))
    tech = db.session.scalar(select(Departement).where(Departement.code == "TECH"))
    essomba = db.session.scalar(select(Employe).where(Employe.nom == "ESSOMBA"))
    tchoua = db.session.scalar(select(Employe).where(Employe.nom == "TCHOUA"))  # chef du dépt TECH
    tech.responsable_id = tchoua.id
    db.session.commit()

    c = app.test_client()
    login(c, email_de("MBALLA"))   # RH
    page = c.get("/parametres/projets").get_data(as_text=True)
    assert 'name="departement_id"' in page and "À rattacher" in page
    # Projet sans département refusé, puis rattaché à TECH
    base = {"id": bali.id, "code": bali.code, "nom": bali.nom}
    r = c.post("/parametres/projets", data=base)
    assert r.status_code == 200 and "département de rattachement" in r.get_data(as_text=True)
    r = c.post("/parametres/projets", data={**base, "departement_id": str(tech.id)})
    assert r.status_code == 302
    p = db.session.get(Parametre, "projets_departements")
    assert p and str(bali.id) in p.valeur
    assert bali.nom in c.get("/parametres/departements").get_data(as_text=True)

    # Campagne vue par la RH : ESSOMBA une seule fois
    campagne = c.get(f"/evaluations/?annee={an}&mois={mo}&par_page=200").get_data(as_text=True)
    lignes_essomba = [a for a in essomba.affectations_actives]
    assert any(a.projet_id for a in lignes_essomba) and any(a.departement_id for a in lignes_essomba)
    assert campagne.count(f'href="/personnel/{essomba.id}" style="color:inherit"') == 1
    assert "Projet Immeuble BALI R+11" in campagne

    # Le N+1 de l'affectation département ne peut plus la noter (déjà notée via le projet)
    aff_dep = next(a for a in lignes_essomba if a.departement_id)
    from app.permissions import Perimetre
    n1 = aff_dep.evaluateur
    if n1 and n1.compte:
        from flask import g
        g.pop("_carte_projets", None)
        assert not Perimetre(n1.compte).peut_noter(aff_dep)

    # Le chef du département voit les salariés du projet rattaché
    u = tchoua.compte
    if not u:
        u = Utilisateur(email="tchoua.dep@x.cm", role=Role.COLLABORATEUR, doit_changer_mdp=False, employe_id=tchoua.id)
        u.set_password(MDP)
        db.session.add(u)
        db.session.commit()
    c2 = app.test_client()
    login(c2, u.email)
    assert "ESSOMBA" in c2.get("/suivi/?vue=annuelle").get_data(as_text=True)
    fiche = c.get(f"/personnel/{essomba.id}").get_data(as_text=True)
    assert "Non notée" in fiche


def test_salarie_sans_projet_note_dans_son_departement(app):
    from app.models import Departement
    from app.services.notation import periode_courante
    an, mo = periode_courante()
    ndjock = db.session.scalar(select(Employe).where(Employe.nom == "NDJOCK"))  # dépt DAF, sans projet
    aff = next(a for a in ndjock.affectations_actives if a.departement_id)
    assert not any(a.projet_id for a in ndjock.affectations_actives)
    c = app.test_client()
    login(c, email_de("MBALLA"))
    campagne = c.get(f"/evaluations/?annee={an}&mois={mo}&par_page=200").get_data(as_text=True)
    assert "NDJOCK" in campagne


def test_changement_de_projet_avec_nouveau_n1(app):
    from app.models import Projet
    c = app.test_client()
    login(c, email_de("MBALLA"))
    owona = db.session.scalar(select(Employe).where(Employe.nom == "OWONA"))
    chr_ = db.session.scalar(select(Projet).where(Projet.code == "CHR"))
    njoya = db.session.scalar(select(Employe).where(Employe.nom == "NJOYA"))
    ancienne = next(a for a in owona.affectations_actives if a.projet_id and a.projet_id != chr_.id)
    nb_evals = db.session.scalar(select(db.func.count(Evaluation.id)).where(Evaluation.affectation_id == ancienne.id))
    fiche = c.get(f"/personnel/{owona.id}").get_data(as_text=True)
    assert "Changer de projet" in fiche
    url = f"/personnel/affectations/{ancienne.id}/changer-projet"
    # Le N+1 est obligatoire
    c.post(url, data={"projet_id": str(chr_.id)})
    db.session.refresh(ancienne)
    assert ancienne.actif
    r = c.post(url, data={"projet_id": str(chr_.id), "evaluateur_id": str(njoya.id)})
    assert r.status_code == 302
    db.session.refresh(owona)
    db.session.refresh(ancienne)
    assert not ancienne.actif
    nouvelle = next(a for a in owona.affectations_actives if a.projet_id == chr_.id)
    assert nouvelle.evaluateur_id == njoya.id
    # Historique conservé
    assert db.session.scalar(select(db.func.count(Evaluation.id)).where(Evaluation.affectation_id == ancienne.id)) == nb_evals


def test_changement_de_projet_groupe(app):
    from app.models import Projet
    c = app.test_client()
    login(c, email_de("MBALLA"))
    chr_ = db.session.scalar(select(Projet).where(Projet.code == "CHR"))
    fotso = db.session.scalar(select(Employe).where(Employe.nom == "FOTSO"))
    gens = [db.session.scalar(select(Employe).where(Employe.nom == n)) for n in ("ESSOMBA", "OWONA")]
    page = c.post("/personnel/selection", data={"action": "changer_projet", "ids": [str(e.id) for e in gens]})
    assert page.status_code == 200 and "Nouveau rattachement" in page.get_data(as_text=True)
    r = c.post("/personnel/changer-projet", data={"ids": [str(e.id) for e in gens], "projet_id": str(chr_.id),
                                                  "evaluateur_id": str(fotso.id)})
    assert r.status_code == 302
    for e in gens:
        db.session.refresh(e)
        projets = [a for a in e.affectations_actives if a.projet_id]
        assert [a.projet_id for a in projets] == [chr_.id]
        assert projets[0].evaluateur_id == fotso.id


def test_salarie_multi_entites(app):
    """Un salarié de FUTURA travaille aussi pour une autre entité : noté dans chacune."""
    from app.models import Departement, Projet
    from app.services.notation import periode_courante
    an, mo = periode_courante()
    c = app.test_client()
    login(c, email_de("MBALLA"))
    ent = _creer_entite(c)
    c.get(f"/entite/{ent.id}")
    c.post("/parametres/departements", data={"code": "CHT", "nom": "Chantiers"})
    dep = db.session.scalar(select(Departement).where(Departement.entite_id == ent.id))
    c.post("/parametres/projets", data={"code": "PSTE", "nom": "Projet Société Test", "departement_id": str(dep.id)})
    proj = db.session.scalar(select(Projet).where(Projet.entite_id == ent.id))
    assert proj is not None
    essomba = db.session.scalar(select(Employe).where(Employe.nom == "ESSOMBA"))  # FUTURA, projet BALI
    tchoua = db.session.scalar(select(Employe).where(Employe.nom == "TCHOUA"))
    fiche = c.get(f"/personnel/{essomba.id}").get_data(as_text=True)
    assert "Projet Société Test" in fiche   # contextes des autres entités proposés
    r = c.post(f"/personnel/{essomba.id}/affectations", data={"projet_id": str(proj.id), "evaluateur_id": str(tchoua.id)})
    assert r.status_code == 302
    db.session.refresh(essomba)
    assert any(a.projet_id == proj.id for a in essomba.affectations_actives)
    assert any(a.projet_id and a.projet_id != proj.id for a in essomba.affectations_actives)  # BALI continue

    # Dans l'entité Société Test : visible dans le personnel et la campagne, une seule ligne
    liste = c.get("/personnel/").get_data(as_text=True)
    assert "ESSOMBA" in liste and "Multi-entités" in liste
    camp = c.get(f"/evaluations/?annee={an}&mois={mo}&par_page=200").get_data(as_text=True)
    assert camp.count(f'href="/personnel/{essomba.id}" style="color:inherit"') == 1
    assert "Projet Société Test" in camp and "BALI" not in camp
    # Dans FUTURA : toujours noté sur BALI, sans la ligne de Société Test
    principale = db.session.scalar(select(db.text("id FROM entites WHERE principale")))
    c.get(f"/entite/{principale}")
    camp = c.get(f"/evaluations/?annee={an}&mois={mo}&par_page=200").get_data(as_text=True)
    assert "Projet Immeuble BALI R+11" in camp and "Projet Société Test" not in camp


def test_affectation_en_masse_depuis_le_projet(app):
    from app.models import Departement, Projet
    c = app.test_client()
    login(c, email_de("MBALLA"))
    chr_ = db.session.scalar(select(Projet).where(Projet.code == "CHR"))
    daf = db.session.scalar(select(Departement).where(Departement.code == "DAF"))
    fotso = db.session.scalar(select(Employe).where(Employe.nom == "FOTSO"))
    gens = [db.session.scalar(select(Employe).where(Employe.nom == n)) for n in ("ESSOMBA", "OWONA", "MANGA")]
    assert "Équipe" in c.get("/parametres/projets").get_data(as_text=True)
    page = c.get(f"/personnel/equipe/projet/{chr_.id}").get_data(as_text=True)
    assert "Ajouter des salariés" in page and "data-candidat" in page
    url = f"/personnel/equipe/projet/{chr_.id}/ajouter"
    # N+1 obligatoire
    c.post(url, data={"ids": [str(e.id) for e in gens]})
    db.session.refresh(gens[0])
    assert not any(a.projet_id == chr_.id for a in gens[0].affectations_actives)
    r = c.post(url, data={"ids": [str(e.id) for e in gens], "evaluateur_id": str(fotso.id), "remplacer": "1"})
    assert r.status_code == 302
    for e in gens:
        db.session.refresh(e)
        projets = [a for a in e.affectations_actives if a.projet_id]
        assert [a.projet_id for a in projets] == [chr_.id] and projets[0].evaluateur_id == fotso.id
    # Les membres apparaissent ; changement de N+1 et retrait groupés
    page = c.get(f"/personnel/equipe/projet/{chr_.id}").get_data(as_text=True)
    assert all(e.nom in page.split("Ajouter des salariés")[0] for e in gens)
    affs = [next(a for a in e.affectations_actives if a.projet_id == chr_.id) for e in gens]
    njoya = db.session.scalar(select(Employe.id).where(Employe.nom == "NJOYA"))
    c.post(f"/personnel/equipe/projet/{chr_.id}/membres",
           data={"action": "n1", "evaluateur_id": str(njoya), "aff_ids": [str(a.id) for a in affs[:2]]})
    c.post(f"/personnel/equipe/projet/{chr_.id}/membres", data={"action": "retirer", "aff_ids": [str(affs[2].id)]})
    for a in affs:
        db.session.refresh(a)
    assert affs[0].evaluateur_id == njoya and affs[1].evaluateur_id == njoya
    assert not affs[2].actif
    # Département sans remplacement : l'affectation s'ajoute
    r = c.post(f"/personnel/equipe/departement/{daf.id}/ajouter",
               data={"ids": [str(gens[0].id)], "evaluateur_id": str(fotso.id)})
    db.session.refresh(gens[0])
    assert any(a.departement_id == daf.id for a in gens[0].affectations_actives)
    assert c.get("/personnel/equipe/autre/1").status_code == 404


def test_suppression_de_poste(app):
    from app.services.organisation import postes_actifs
    from app.services.entites import principale
    c = app.test_client()
    login(c, email_de("MBALLA"))
    eid = principale().id
    # Poste de la liste par défaut, porté par personne : supprimé directement
    assert "Magasinier" in postes_actifs(eid)
    r = c.post("/parametres/postes", data={"action": "supprimer", "libelle": "Magasinier"})
    assert r.status_code == 302
    page = c.get("/parametres/postes").get_data(as_text=True)
    assert "<strong>Magasinier</strong>" not in page and "supprimé" in page
    assert "Magasinier" not in postes_actifs(eid)
    # Poste porté par des fiches : remplacé par un autre
    manga = db.session.scalar(select(Employe).where(Employe.nom == "MANGA"))
    assert manga.poste == "Topographe"
    conf = c.get("/parametres/postes/supprimer?libelle=Topographe").get_data(as_text=True)
    assert "MANGA" in conf and 'name="remplacement"' in conf
    c.post("/parametres/postes", data={"action": "supprimer", "libelle": "Topographe", "remplacement": "Technicien"})
    db.session.refresh(manga)
    assert manga.poste == "Technicien" and "Topographe" not in postes_actifs(eid)
    # Recréé ensuite : il revient
    c.post("/parametres/postes", data={"libelle": "Magasinier"})
    assert "Magasinier" in postes_actifs(eid)


def test_import_liste_de_postes(app):
    import io as _io
    from app.services.organisation import postes_actifs
    from app.services.entites import principale
    c = app.test_client()
    login(c, email_de("MBALLA"))
    eid = principale().id
    # Le modèle téléchargé se réimporte tel quel
    modele = c.get("/parametres/postes/modele.xlsx")
    assert modele.status_code == 200
    c.post("/parametres/postes", data={"action": "supprimer", "libelle": "Magasinier"})
    r = c.post("/parametres/postes/importer", data={"fichier": (_io.BytesIO(modele.data), "modele_postes.xlsx")},
               content_type="multipart/form-data")
    assert r.status_code == 302
    assert "Magasinier" in postes_actifs(eid)
    # Texte collé (doublons et en-tête ignorés)
    c.post("/parametres/postes/importer", data={"texte": "Poste\nGrutier\ngrutier\nOpérateur drone"})
    actifs = postes_actifs(eid)
    assert "Grutier" in actifs and "Opérateur drone" in actifs
    # CSV avec une colonne « Fonction » ; ne garder que la liste
    csv_ = "Nom;Fonction\nA;Grutier\nB;Topographe\n".encode()
    c.post("/parametres/postes/importer", data={"fichier": (_io.BytesIO(csv_), "p.csv"), "ne_garder_que": "1"},
           content_type="multipart/form-data")
    actifs = postes_actifs(eid)
    assert "Grutier" in actifs and "Topographe" in actifs
    assert "Magasinier" not in actifs          # absent du fichier et porté par personne
    assert "Maçon" in actifs                   # porté par un salarié : conservé
    assert "Opérateur drone" not in actifs
    page = c.get("/parametres/postes").get_data(as_text=True)
    assert "Importer une liste" in page


def test_sous_departements(app):
    from app.models import Departement, Parametre
    from app.services.notation import periode_courante
    an, mo = periode_courante()
    c = app.test_client()
    login(c, email_de("MBALLA"))
    tech = db.session.scalar(select(Departement).where(Departement.code == "TECH"))
    tchoua = db.session.scalar(select(Employe).where(Employe.nom == "TCHOUA"))
    tech.responsable_id = tchoua.id
    db.session.commit()
    page = c.get("/parametres/departements").get_data(as_text=True)
    assert 'name="parent_id"' in page
    r = c.post("/parametres/departements", data={"code": "METH", "nom": "Méthodes", "parent_id": str(tech.id)})
    assert r.status_code == 302
    meth = db.session.scalar(select(Departement).where(Departement.code == "METH"))
    assert str(meth.id) in db.session.get(Parametre, "departements_parents").valeur
    # Pas de cycle : TECH ne peut pas dépendre de son sous-département
    r = c.post("/parametres/departements", data={"id": tech.id, "code": tech.code, "nom": tech.nom, "parent_id": str(meth.id)})
    assert r.status_code == 200 and "sous-départements" in r.get_data(as_text=True)
    # Un salarié du sous-département est visible par le chef du département parent
    c.post("/personnel/nouveau", data={"nom": "BIYA", "departement_id": str(meth.id),
                                        "evaluateur_id": str(db.session.scalar(select(Employe.id).where(Employe.nom == "FOTSO")))})
    u = tchoua.compte
    if not u:
        u = Utilisateur(email="tchoua.sd@x.cm", role=Role.COLLABORATEUR, doit_changer_mdp=False, employe_id=tchoua.id)
        u.set_password(MDP)
        db.session.add(u)
        db.session.commit()
    c2 = app.test_client()
    login(c2, u.email)
    camp = c2.get(f"/evaluations/?annee={an}&mois={mo}").get_data(as_text=True)
    assert "BIYA" in camp
    # Filtre du personnel par département parent : inclut le sous-département
    liste = c.get(f"/personnel/?departement={tech.id}").get_data(as_text=True)
    assert "BIYA" in liste
    page = c.get("/parametres/departements").get_data(as_text=True)
    assert "└" in page


def test_suppression_de_postes_en_masse(app):
    from app.services.organisation import postes_actifs
    from app.services.entites import principale
    c = app.test_client()
    login(c, email_de("MBALLA"))
    eid = principale().id
    page = c.get("/parametres/postes").get_data(as_text=True)
    assert 'form="form-postes-masse"' in page and "Supprimer tous les postes" in page
    # Sélection : un poste libre et un poste occupé (conservé par défaut)
    c.post("/parametres/postes", data={"action": "supprimer_masse", "libelles": ["Chauffeur", "Maçon"]})
    actifs = postes_actifs(eid)
    assert "Chauffeur" not in actifs and "Maçon" in actifs
    # Tout supprimer en conservant les postes occupés
    c.post("/parametres/postes", data={"action": "supprimer_masse", "tout": "1", "occupes": "garder"})
    occupes = {p for p in db.session.scalars(select(Employe.poste).where(Employe.poste.isnot(None)))}
    assert set(postes_actifs(eid)) == occupes
    # Tout supprimer, y compris les postes occupés : les fiches n'ont plus de poste
    c.post("/parametres/postes", data={"action": "supprimer_masse", "tout": "1", "occupes": "vider"})
    assert postes_actifs(eid) == []
    assert db.session.scalar(select(db.func.count(Employe.id)).where(Employe.poste.isnot(None))) == 0


def test_grille_kpi_par_poste(app):
    from app.models import Critere, Parametre
    from app.services.notation import periode_courante
    an, mo = periode_courante()
    c = app.test_client()
    login(c, email_de("MBALLA"))
    page = c.get("/parametres/postes").get_data(as_text=True)
    assert "Grille KPI : commune" in page
    url = "/parametres/postes/grille?libelle=Topographe"
    page = c.get(url).get_data(as_text=True)
    assert "Utilise la grille commune" in page and 'name="inclus"' in page
    criteres = db.session.scalars(select(Critere).where(Critere.actif.is_(True))).all()
    # Grille propre : 2 critères communs (dont un avec coefficient 5) + 1 critère spécifique
    c.post(url, data={"libelle": "Topographe", "action": "enregistrer",
                      "inclus": [str(criteres[0].id), str(criteres[1].id)], f"poids_{criteres[0].id}": "5",
                      f"poids_{criteres[1].id}": "1"})
    c.post(url, data={"libelle": "Topographe", "action": "ajouter_critere",
                      "libelle_critere": "Précision des relevés", "poids": "3"})
    spe = db.session.scalar(select(Critere).where(Critere.libelle == "Précision des relevés"))
    assert spe is not None
    assert "grilles_postes" in {p.cle for p in db.session.scalars(select(Parametre))}
    page = c.get("/parametres/postes").get_data(as_text=True)
    assert "propre (3 critères)" in page
    # Le critère spécifique n'entre pas dans la grille commune
    page = c.get("/parametres/criteres").get_data(as_text=True)
    assert "Précision des relevés" not in page and "Grilles par poste" in page   # grille commune seulement
    page = c.get("/parametres/criteres?vue=tous").get_data(as_text=True)
    assert "Spécifique : Topographe" in page
    # MANGA (Topographe) est noté avec la grille du poste
    manga = db.session.scalar(select(Employe).where(Employe.nom == "MANGA"))
    aff = next(a for a in manga.affectations_actives if a.evaluateur_id)
    n1 = aff.evaluateur
    if not n1.compte:
        u = Utilisateur(email="n1.grille@x.cm", role=Role.COLLABORATEUR, doit_changer_mdp=False, employe_id=n1.id)
        u.set_password(MDP)
        db.session.add(u)
        db.session.commit()
    c2 = app.test_client()
    login(c2, n1.compte.email)
    page = c2.get(f"/evaluations/noter/{aff.id}?annee={an}&mois={mo}").get_data(as_text=True)
    assert "Poste : Topographe" in page and "Précision des relevés" in page
    assert criteres[2].libelle not in page
    form = {"action": "soumettre", f"note_{criteres[0].id}": "100", f"note_{criteres[1].id}": "50",
            f"commentaire_{criteres[1].id}": "Doit progresser",
            f"note_{spe.id}": "80"}
    r = c2.post(f"/evaluations/noter/{aff.id}?annee={an}&mois={mo}", data=form)
    assert r.status_code == 302
    ev = db.session.scalar(select(Evaluation).where(Evaluation.affectation_id == aff.id,
                                                    Evaluation.annee == an, Evaluation.mois == mo))
    assert ev.est_soumise and ev.note_globale == round((100 * 5 + 50 * 1 + 80 * 3) / 9, 2)
    # Un autre poste reste sur la grille commune (sans le critère spécifique)
    owona = db.session.scalar(select(Employe).where(Employe.nom == "OWONA"))
    aff_o = next(a for a in owona.affectations_actives if a.evaluateur_id)
    from app.services.grilles import grille_pour
    from app.services.entites import entite_affectation
    lignes, propre = grille_pour(entite_affectation(aff_o), owona.poste)
    assert not propre and spe.id not in {x.id for x in lignes}
    # Retour à la grille commune
    c.post(url, data={"libelle": "Topographe", "action": "commune"})
    assert "Utilise la grille commune" in c.get(url).get_data(as_text=True)


def test_suppression_des_criteres_kpi(app):
    from app.models import Critere, EvaluationNote
    from app.services.notation import periode_courante
    from app.cli import creer_criteres_defaut
    an, mo = periode_courante()
    c = app.test_client()
    login(c, email_de("MBALLA"))
    crits = db.session.scalars(select(Critere).order_by(Critere.ordre)).all()
    utilise = db.session.scalar(select(EvaluationNote.critere_id).limit(1))
    page = c.get("/parametres/criteres").get_data(as_text=True)
    assert "Tout supprimer (grille vide)" in page
    # Un critère jamais utilisé : effacé
    c.post("/parametres/criteres", data={"libelle": "Critère temporaire", "poids": "1", "ordre": "9"})
    libre = db.session.scalar(select(Critere).where(Critere.libelle == "Critère temporaire"))
    c.post("/parametres/criteres", data={"action": "supprimer", "id": libre.id})
    assert db.session.get(Critere, libre.id) is None
    # Tout supprimer : grille vide, l'historique reste lisible
    c.post("/parametres/criteres", data={"action": "supprimer_masse", "tout": "1"})
    page = c.get("/parametres/criteres").get_data(as_text=True)
    assert "Grille vide" in page
    if utilise:
        assert db.session.get(Critere, utilise) is not None   # gardé pour les notes passées
        ev = db.session.scalar(select(Evaluation).join(EvaluationNote).where(EvaluationNote.critere_id == utilise))
        assert c.get(f"/evaluations/{ev.id}").status_code == 200
    # Pas de grille par défaut recréée au redémarrage
    assert creer_criteres_defaut() == 0
    # Notation impossible tant que la grille est vide
    manga = db.session.scalar(select(Employe).where(Employe.nom == "MANGA"))
    aff = next(a for a in manga.affectations_actives if a.evaluateur_id)
    n1 = aff.evaluateur
    if not n1.compte:
        u = Utilisateur(email="n1.vide@x.cm", role=Role.COLLABORATEUR, doit_changer_mdp=False, employe_id=n1.id)
        u.set_password(MDP)
        db.session.add(u)
        db.session.commit()
    c2 = app.test_client()
    login(c2, n1.compte.email)
    r = c2.get(f"/evaluations/noter/{aff.id}?annee={an}&mois={mo}")
    assert r.status_code == 302
    # Nouveau critère (même libellé qu'un critère supprimé déjà utilisé : réutilisé)
    lib = db.session.get(Critere, utilise).libelle if utilise else "Qualité du travail"
    r = c.post("/parametres/criteres", data={"libelle": lib, "poids": "2", "ordre": "1"})
    assert r.status_code == 302
    assert lib in c.get("/parametres/criteres").get_data(as_text=True)
    assert c2.get(f"/evaluations/noter/{aff.id}?annee={an}&mois={mo}").status_code == 200


def test_organigramme(app):
    from app.models import Departement
    c = app.test_client()
    login(c, email_de("MBALLA"))
    tech = db.session.scalar(select(Departement).where(Departement.code == "TECH"))
    c.post("/parametres/departements", data={"code": "METH", "nom": "Méthodes", "parent_id": str(tech.id)})
    page = c.get("/parametres/organigramme").get_data(as_text=True)
    assert "Organigramme" in page and "Méthodes" in page and "Direction technique" in page
    # Le sous-département est dans la branche de son parent
    org = page[page.index('class="org"'):]
    assert org.index("Direction technique") < org.index("Méthodes")
    # Direction : accès en lecture ; collaborateur : refusé
    c2 = app.test_client()
    login(c2, email_de("NGUEMA"))
    assert c2.get("/parametres/organigramme").status_code == 200
    c3 = app.test_client()
    login(c3, email_de("FOTSO"))   # chef de projet : pas d'accès
    assert c3.get("/parametres/organigramme").status_code == 403


def test_suppression_de_departements(app):
    from app.models import Affectation, Departement, Projet
    from app.services.hierarchie import parent_de, supprimes
    from app.services.projets import departement_du_projet
    c = app.test_client()
    login(c, email_de("MBALLA"))
    tech = db.session.scalar(select(Departement).where(Departement.code == "TECH"))
    bali = db.session.scalar(select(Projet).where(Projet.code == "BALI"))
    c.post("/parametres/projets", data={"id": bali.id, "code": bali.code, "nom": bali.nom, "departement_id": str(tech.id)})
    c.post("/parametres/departements", data={"code": "METH", "nom": "Méthodes", "parent_id": str(tech.id)})
    c.post("/parametres/departements", data={"code": "VIDE", "nom": "Département vide"})
    meth = db.session.scalar(select(Departement).where(Departement.code == "METH"))
    vide = db.session.scalar(select(Departement).where(Departement.code == "VIDE"))
    # Département sans historique : effacé
    page = c.get(f"/parametres/departements/supprimer?ids={vide.id}").get_data(as_text=True)
    assert "Ce qui va se passer" in page
    c.post("/parametres/departements/supprimer", data={"ids": [str(vide.id)]})
    assert db.session.get(Departement, vide.id) is None
    # Département avec historique : retiré des listes, affectations clôturées, historique conservé
    nb_evals = db.session.scalar(select(db.func.count(Evaluation.id)))
    c.post("/parametres/departements/supprimer", data={"ids": [str(tech.id)]})
    assert tech.id in supprimes()
    assert not db.session.scalar(select(Affectation.id).where(Affectation.departement_id == tech.id,
                                                              Affectation.actif.is_(True)).limit(1))
    assert db.session.scalar(select(db.func.count(Evaluation.id))) == nb_evals
    assert parent_de(meth.id) is None and departement_du_projet(bali.id) is None
    page = c.get("/parametres/departements").get_data(as_text=True)
    assert "Direction technique" not in page.split("Nouveau département")[0]
    # Recréé sous le même code : la ligne revient
    c.post("/parametres/departements", data={"code": "TECH", "nom": "Direction technique"})
    db.session.refresh(tech)
    assert tech.actif and tech.id not in supprimes()


def test_responsable_de_departement_note_ses_membres(app):
    from app.models import Departement
    from app.services.organisation import synchroniser_n1_departements
    c = app.test_client()
    login(c, email_de("MBALLA"))
    daf = db.session.scalar(select(Departement).where(Departement.code == "DAF"))
    fotso = db.session.scalar(select(Employe).where(Employe.nom == "FOTSO"))
    # Nouveau responsable : il devient le N+1 de tous les membres du département
    c.post("/parametres/departements", data={"id": daf.id, "code": daf.code, "nom": daf.nom,
                                             "responsable_id": str(fotso.id)})
    membres = db.session.scalars(select(Affectation).where(Affectation.departement_id == daf.id,
                                                           Affectation.actif.is_(True))).all()
    assert membres and all(a.evaluateur_id == fotso.id for a in membres if a.employe_id != fotso.id)
    # Le N+1 d'une affectation département ne se modifie pas à la main
    a = next(x for x in membres if x.employe_id != fotso.id)
    tchoua = db.session.scalar(select(Employe.id).where(Employe.nom == "TCHOUA"))
    c.post(f"/personnel/affectations/{a.id}", data={"action": "evaluateur", "evaluateur_id": str(tchoua)})
    db.session.refresh(a)
    assert a.evaluateur_id == fotso.id
    # Pas de responsable : N+1 vide
    c.post("/parametres/departements", data={"id": daf.id, "code": daf.code, "nom": daf.nom, "responsable_id": ""})
    db.session.refresh(a)
    assert a.evaluateur_id is None
    # La synchronisation (lancée au démarrage) est idempotente
    synchroniser_n1_departements()
    db.session.commit()
    db.session.refresh(a)
    assert a.evaluateur_id is None


def test_sans_chef_le_chef_du_departement_mere_note(app):
    from app.models import Departement
    c = app.test_client()
    login(c, email_de("MBALLA"))
    tech = db.session.scalar(select(Departement).where(Departement.code == "TECH"))   # chef : TCHOUA
    c.post("/parametres/departements", data={"code": "METH", "nom": "Méthodes", "parent_id": str(tech.id)})
    meth = db.session.scalar(select(Departement).where(Departement.code == "METH"))
    c.post("/parametres/departements", data={"code": "BET", "nom": "Bureau d'études", "parent_id": str(meth.id)})
    bet = db.session.scalar(select(Departement).where(Departement.code == "BET"))
    c.post("/personnel/nouveau", data={"nom": "ESSAMA", "departement_id": str(bet.id)})
    a = db.session.scalar(select(Employe).where(Employe.nom == "ESSAMA")).affectations[0]
    # BET et Méthodes sans chef : on remonte jusqu'au chef de la Direction technique
    assert a.evaluateur_id == tech.responsable_id
    # Méthodes reçoit un chef : il devient le N+1 des membres de BET
    fotso = db.session.scalar(select(Employe).where(Employe.nom == "FOTSO"))
    c.post("/parametres/departements", data={"id": meth.id, "code": "METH", "nom": "Méthodes",
                                             "responsable_id": str(fotso.id), "parent_id": str(tech.id)})
    db.session.refresh(a)
    assert a.evaluateur_id == fotso.id


def test_rattacher_a_une_autre_entite_avec_poste_et_notation(app):
    from app.models import Departement
    from app.services.notation import periode_courante
    an, mo = periode_courante()
    c = app.test_client()
    login(c, email_de("MBALLA"))
    ent = _creer_entite(c)
    c.get(f"/entite/{ent.id}")
    essomba = db.session.scalar(select(Employe).where(Employe.nom == "ESSOMBA"))   # FUTURA, Conducteur de travaux
    # Responsable possible dans STE même s'il vient de FUTURA
    page = c.get("/parametres/departements").get_data(as_text=True)
    assert f'value="{essomba.id}"' in page
    c.post("/parametres/departements", data={"code": "CHT", "nom": "Chantiers", "responsable_id": str(essomba.id)})
    dep = db.session.scalar(select(Departement).where(Departement.entite_id == ent.id))
    c.post("/personnel/nouveau", data={"nom": "ONANA", "departement_id": str(dep.id)})
    onana = db.session.scalar(select(Employe).where(Employe.nom == "ONANA"))
    assert onana.affectations[0].evaluateur_id == essomba.id          # il note dans STE
    # Rattachement de ESSOMBA à STE avec un poste propre
    fiche = c.get(f"/personnel/{essomba.id}?entite={ent.id}").get_data(as_text=True)
    assert "Rattacher à Société Test" in fiche
    r = c.post(f"/personnel/{essomba.id}/entites", data={"entite_id": ent.id, "action": "rattacher",
                                                        "poste": "__nouveau__", "poste_nouveau": "Chef de chantier",
                                                        "departement_id": str(dep.id)})
    assert r.status_code == 302
    liste = c.get("/personnel/").get_data(as_text=True)                # entité active : STE
    ligne = liste[liste.index("<strong>ESSOMBA"):][:2500]
    assert "Chef de chantier" in ligne
    fiche = c.get(f"/personnel/{essomba.id}").get_data(as_text=True)
    assert "Société Test" in fiche and "Origine" in fiche
    # Dans FUTURA, son poste reste celui de sa fiche
    principale = db.session.scalar(select(db.text("id FROM entites WHERE principale")))
    c.get(f"/entite/{principale}")
    liste = c.get("/personnel/").get_data(as_text=True)
    assert "Conducteur de travaux" in liste[liste.index("<strong>ESSOMBA"):][:2500]


def test_extraction_kpi_par_ia(app, monkeypatch):
    import io as _io
    from app.models import Critere
    from app.services import kpi_ia
    from app.services.grilles import grille_pour, specifiques
    from app.services.entites import principale
    def faux_claude(outil, consigne, nom, mime, contenu, texte=""):
        assert outil["name"] == "enregistrer_kpi" and contenu
        return {"kpis": [
            {"libelle": "Respect des délais", "description": "Livrables à l'heure", "poids": 5,
             "ponderation_document": "30 %", "poste": "", "confiance": "haute"},
            {"libelle": "Précision des relevés", "description": "Écarts < 2 cm", "poids": 3,
             "poste": "Topographe", "confiance": "haute"},
            {"libelle": "Implantation des ouvrages", "poids": 2, "poste": "Géomètre", "confiance": "moyenne"},
        ], "remarques": "Fiche KPI 2026"}
    monkeypatch.setattr(kpi_ia, "appeler_claude", faux_claude)
    c = app.test_client()
    login(c, email_de("MBALLA"))
    assert "Extraire les KPI d'un document" in c.get("/parametres/criteres").get_data(as_text=True)
    r = c.post("/parametres/criteres/extraire", data={"fichier": (_io.BytesIO(b"%PDF-1.4 x"), "kpi.pdf")},
               content_type="multipart/form-data")
    assert r.status_code == 302 and "/criteres/extraction/" in r.location
    page = c.get(r.location).get_data(as_text=True)
    assert "Respect des délais" in page and "Nouveau poste : Géomètre" in page and "30 %" in page
    assert 'value="poste:Topographe" selected' in page
    r = c.post(r.location, data={
        "action": "importer",
        "k0_inclure": "1", "k0_libelle": "Respect des délais", "k0_description": "Livrables à l'heure", "k0_poids": "5", "k0_destination": "",
        "k1_inclure": "1", "k1_libelle": "Précision des relevés", "k1_description": "Écarts < 2 cm", "k1_poids": "3", "k1_destination": "poste:Topographe",
        "k2_inclure": "1", "k2_libelle": "Implantation des ouvrages", "k2_description": "", "k2_poids": "2", "k2_destination": "nouveau:Géomètre"})
    assert r.status_code == 302
    eid = principale().id
    precis = db.session.scalar(select(Critere).where(Critere.libelle == "Précision des relevés"))
    assert precis and precis.id in specifiques(eid)
    topo, propre = grille_pour(eid, "Topographe")
    assert propre and [x.libelle for x in topo] == ["Précision des relevés"]
    geo, propre = grille_pour(eid, "Géomètre")
    assert propre and geo[0].libelle == "Implantation des ouvrages"
    # « Respect des délais » existait déjà : réutilisé, pas dupliqué
    assert db.session.scalar(select(db.func.count(Critere.id)).where(Critere.libelle == "Respect des délais")) == 1
    commune, _ = grille_pour(eid, "Maçon")
    assert "Précision des relevés" not in {x.libelle for x in commune}



def test_commentaire_obligatoire_sous_60(app):
    from app.models import Critere
    from app.services.notation import periode_courante
    an, mo = periode_courante()
    manga = db.session.scalar(select(Employe).where(Employe.nom == "MANGA"))
    aff = next(a for a in manga.affectations_actives if a.evaluateur_id)
    n1 = aff.evaluateur
    if not n1.compte:
        u = Utilisateur(email="n1.seuil@x.cm", role=Role.COLLABORATEUR, doit_changer_mdp=False, employe_id=n1.id)
        u.set_password(MDP)
        db.session.add(u)
        db.session.commit()
    c = app.test_client()
    login(c, n1.compte.email)
    crits = db.session.scalars(select(Critere).where(Critere.actif.is_(True))).all()
    form = {"action": "soumettre", **{f"note_{x.id}": "80" for x in crits}}
    form[f"note_{crits[0].id}"] = "59"
    r = c.post(f"/evaluations/noter/{aff.id}?annee={an}&mois={mo}", data=form)
    assert r.status_code == 200 and "Justifiez une note inférieure à 60" in r.get_data(as_text=True)
    form[f"commentaire_{crits[0].id}"] = "Retards répétés"
    r = c.post(f"/evaluations/noter/{aff.id}?annee={an}&mois={mo}", data=form)
    assert r.status_code == 302


def test_coefficients_libres(app):
    from app.models import Critere
    from app.services.grilles import grille_pour
    from app.services.entites import principale
    c = app.test_client()
    login(c, email_de("MBALLA"))
    eid = principale().id
    # Grille commune : coefficient 30 (pourcentage)
    r = c.post("/parametres/criteres", data={"libelle": "Rendement", "poids": "30", "ordre": "20"})
    assert r.status_code == 302
    crit = db.session.scalar(select(Critere).where(Critere.libelle == "Rendement"))
    lignes, _ = grille_pour(eid, "Maçon")
    assert next(x.poids for x in lignes if x.id == crit.id) == 30
    assert "×30" in c.get("/parametres/criteres").get_data(as_text=True)
    # Hors limites refusé
    r = c.post("/parametres/criteres", data={"libelle": "Autre", "poids": "150", "ordre": "21"})
    assert r.status_code == 200 and "1 à 100" in r.get_data(as_text=True)
    # Grille d'un poste : coefficients libres
    url = "/parametres/postes/grille?libelle=Topographe"
    c.post(url, data={"libelle": "Topographe", "action": "enregistrer", "inclus": [str(crit.id)],
                      f"poids_{crit.id}": "45"})
    lignes, propre = grille_pour(eid, "Topographe")
    assert propre and lignes[0].poids == 45


def test_coefficients_decimaux(app):
    from app.models import Critere
    from app.services.grilles import grille_pour
    from app.services.entites import principale
    from app.services.notation import periode_courante
    an, mo = periode_courante()
    c = app.test_client()
    login(c, email_de("MBALLA"))
    eid = principale().id
    crits = db.session.scalars(select(Critere).where(Critere.actif.is_(True)).order_by(Critere.ordre)).all()
    # Grille du poste Topographe : coefficients décimaux (virgule ou point)
    url = "/parametres/postes/grille?libelle=Topographe"
    c.post(url, data={"libelle": "Topographe", "action": "enregistrer", "inclus": [str(crits[0].id), str(crits[1].id)],
                      f"poids_{crits[0].id}": "1,5", f"poids_{crits[1].id}": "2.25"})
    lignes, propre = grille_pour(eid, "Topographe")
    assert propre and sorted(x.poids for x in lignes) == [1.5, 2.25]
    assert "1,5" in c.get(url).get_data(as_text=True)
    # Notation de MANGA (Topographe) : note globale pondérée avec les décimales
    manga = db.session.scalar(select(Employe).where(Employe.nom == "MANGA"))
    aff = next(a for a in manga.affectations_actives if a.evaluateur_id)
    n1 = aff.evaluateur
    if not n1.compte:
        u = Utilisateur(email="n1.dec@x.cm", role=Role.COLLABORATEUR, doit_changer_mdp=False, employe_id=n1.id)
        u.set_password(MDP)
        db.session.add(u)
        db.session.commit()
    c2 = app.test_client()
    login(c2, n1.compte.email)
    r = c2.post(f"/evaluations/noter/{aff.id}?annee={an}&mois={mo}",
                data={"action": "soumettre", f"note_{crits[0].id}": "100", f"note_{crits[1].id}": "70"})
    assert r.status_code == 302
    ev = db.session.scalar(select(Evaluation).where(Evaluation.affectation_id == aff.id,
                                                    Evaluation.annee == an, Evaluation.mois == mo))
    assert ev.note_globale == round((100 * 1.5 + 70 * 2.25) / 3.75, 2)
    detail = c2.get(f"/evaluations/{ev.id}").get_data(as_text=True)
    assert "×1,5" in detail and "×2,25" in detail
    # Grille commune : coefficient décimal ; invalide refusé
    c.post("/parametres/criteres", data={"libelle": "Rendement", "poids": "0,75", "ordre": "30"})
    assert "×0,75" in c.get("/parametres/criteres").get_data(as_text=True)
    r = c.post("/parametres/criteres", data={"libelle": "Autre", "poids": "abc", "ordre": "31"})
    assert r.status_code == 200 and "Coefficient invalide" in r.get_data(as_text=True)



def test_page_poste_affiche_seulement_ses_kpi(app):
    from app.models import Critere
    c = app.test_client()
    login(c, email_de("MBALLA"))
    crits = db.session.scalars(select(Critere).where(Critere.actif.is_(True)).order_by(Critere.ordre)).all()
    url = "/parametres/postes/grille?libelle=Topographe"
    # Sans grille propre : aucun KPI coché, la bibliothèque est repliée en dessous
    page = c.get(url).get_data(as_text=True)
    assert "Aucun KPI propre à ce poste" in page and "Ajouter des KPI existants" in page
    assert page.count("checked data-inclus") == 0
    c.post(url, data={"libelle": "Topographe", "action": "enregistrer", "inclus": [str(crits[0].id)],
                      f"poids_{crits[0].id}": "2"})
    page = c.get(url).get_data(as_text=True)
    principal = page[page.index("KPI du poste"):page.index("Ajouter des KPI existants")]
    assert crits[0].libelle in principal and crits[1].libelle not in principal
    # Partir de la grille commune
    c.post("/parametres/postes/grille?libelle=Maçon", data={"libelle": "Maçon", "action": "copier_commune"})
    from app.services.grilles import grille_pour
    from app.services.entites import principale
    lignes, propre = grille_pour(principale().id, "Maçon")
    assert propre and len(lignes) == len(crits)


def test_copier_postes_et_kpi_vers_une_autre_entite(app):
    from app.models import Critere
    from app.services.grilles import grille_pour, specifiques
    from app.services.organisation import postes_actifs
    from app.services.entites import principale
    c = app.test_client()
    login(c, email_de("MBALLA"))
    fut = principale().id
    # Dans FUTURA : grille propre au poste Topographe avec un KPI spécifique décimal
    crits = db.session.scalars(select(Critere).where(Critere.actif.is_(True)).order_by(Critere.ordre)).all()
    url = "/parametres/postes/grille?libelle=Topographe"
    c.post(url, data={"libelle": "Topographe", "action": "enregistrer", "inclus": [str(crits[0].id)],
                      f"poids_{crits[0].id}": "2,5"})
    c.post(url, data={"libelle": "Topographe", "action": "ajouter_critere", "libelle_critere": "Précision des relevés",
                      "poids": "3"})
    ent = _creer_entite(c)            # STE démarre avec la grille par défaut
    c.get(f"/entite/{ent.id}")
    page = c.get("/parametres/postes").get_data(as_text=True)
    assert "Copier depuis une autre entité" in page
    r = c.post("/parametres/copier-entite", data={"source_id": fut, "postes": "1", "commune": "1",
                                                   "grilles_postes": "1", "retour": "/parametres/postes"})
    assert r.status_code == 302
    assert "Topographe" in postes_actifs(ent.id)
    lignes, propre = grille_pour(ent.id, "Topographe")
    assert propre and sorted((x.libelle, x.poids) for x in lignes) == sorted(
        [(crits[0].libelle, 2.5), ("Précision des relevés", 3)])
    spe = db.session.scalar(select(Critere).where(Critere.entite_id == ent.id, Critere.libelle == "Précision des relevés"))
    assert spe and spe.id in specifiques(ent.id)
    # Les critères de STE restent ceux de STE (pas de partage de lignes entre entités)
    assert all(x.critere.entite_id == ent.id for x in lignes)
    # Recopier sans « remplacer » : la grille déjà définie est conservée
    r = c.post("/parametres/copier-entite", data={"source_id": fut, "grilles_postes": "1", "retour": "/parametres/postes"})
    assert "conservée" in c.get("/parametres/postes").get_data(as_text=True)
    # Pas de copie vers soi-même
    r = c.post("/parametres/copier-entite", data={"source_id": ent.id, "postes": "1", "retour": "/parametres/postes"})
    assert "Choisissez" in c.get("/parametres/postes").get_data(as_text=True)


def _compte(nom, email):
    e = db.session.scalar(select(Employe).where(Employe.nom == nom))
    if not e.compte:
        u = Utilisateur(email=email, role=Role.COLLABORATEUR, doit_changer_mdp=False, employe_id=e.id)
        u.set_password(MDP)
        db.session.add(u)
        db.session.commit()
    return e


def test_membre_voit_ses_collegues_sans_leurs_notes(app):
    ndjock = _compte("NDJOCK", "ndjock.v@x.cm")              # membre du département DAF
    moukoko = db.session.scalar(select(Employe).where(Employe.nom == "MOUKOKO"))   # même département
    essomba = db.session.scalar(select(Employe).where(Employe.nom == "ESSOMBA"))   # autre département
    c = app.test_client()
    login(c, ndjock.compte.email)
    liste = c.get("/personnel/").get_data(as_text=True)
    assert "MOUKOKO" in liste and "ESSOMBA" not in liste
    assert c.get(f"/personnel/{moukoko.id}").status_code == 200
    assert c.get(f"/personnel/{essomba.id}").status_code == 403
    ev = db.session.scalar(select(Evaluation).where(Evaluation.employe_id == moukoko.id))
    assert ev is not None
    if ev:
        assert c.get(f"/evaluations/{ev.id}").status_code == 403          # pas les notes des collègues


def test_chef_voit_toute_sa_branche_et_les_notes(app):
    from app.models import Departement
    tchoua = _compte("TCHOUA", "tchoua.v@x.cm")              # chef de la Direction technique
    essomba = db.session.scalar(select(Employe).where(Employe.nom == "ESSOMBA"))   # membre TECH, noté sur BALI
    c = app.test_client()
    login(c, email_de("MBALLA"))
    tech = db.session.scalar(select(Departement).where(Departement.code == "TECH"))
    c.post("/parametres/departements", data={"code": "METH", "nom": "Méthodes", "parent_id": str(tech.id)})
    meth = db.session.scalar(select(Departement).where(Departement.code == "METH"))
    c.post("/personnel/nouveau", data={"nom": "BIYONG", "departement_id": str(meth.id)})
    c2 = app.test_client()
    login(c2, tchoua.compte.email)
    liste = c2.get("/personnel/").get_data(as_text=True)
    assert "BIYONG" in liste and "ESSOMBA" in liste                # sous-département compris
    ev = db.session.scalar(select(Evaluation).where(Evaluation.employe_id == essomba.id,
                                                    Evaluation.evaluateur_id != tchoua.id))
    assert ev is not None
    if ev:                                                         # noté par un autre N+1 sur un projet
        assert c2.get(f"/evaluations/{ev.id}").status_code == 200


def test_collaborateur_chef_dans_deux_entites(app):
    from app.models import Departement
    from app.services.notation import periode_courante
    an, mo = periode_courante()
    essomba = _compte("ESSOMBA", "essomba.v@x.cm")
    c = app.test_client()
    login(c, email_de("MBALLA"))
    daf = db.session.scalar(select(Departement).where(Departement.code == "DAF"))
    c.post("/parametres/departements", data={"id": daf.id, "code": daf.code, "nom": daf.nom,
                                             "responsable_id": str(essomba.id)})
    ent = _creer_entite(c)
    c.get(f"/entite/{ent.id}")
    c.post("/parametres/departements", data={"code": "CHT", "nom": "Chantiers", "responsable_id": str(essomba.id)})
    dep = db.session.scalar(select(Departement).where(Departement.entite_id == ent.id))
    c.post("/personnel/nouveau", data={"nom": "ONANA", "departement_id": str(dep.id)})
    c2 = app.test_client()
    login(c2, essomba.compte.email)
    accueil = c2.get("/").get_data(as_text=True)
    assert "Société Test" in accueil                                   # onglet de la 2e entité
    camp = c2.get(f"/evaluations/?annee={an}&mois={mo}").get_data(as_text=True)
    assert "NDJOCK" in camp                                            # il note la DAF dans FUTURA
    c2.get(f"/entite/{ent.id}")
    camp = c2.get(f"/evaluations/?annee={an}&mois={mo}").get_data(as_text=True)
    assert "ONANA" in camp                                             # et les Chantiers de STE
    assert "ONANA" in c2.get("/personnel/").get_data(as_text=True)
