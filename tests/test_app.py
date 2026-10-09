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
        for k in ("_login_user", "_perimetre", "_entite_courante", "_entite_principale"):
            g.pop(k, None)
    # Doit passer avant les autres before_request de l'application
    app.before_request_funcs.setdefault(None, []).insert(0, _isoler_requete)

    with app.app_context():
        db.create_all()
        # Comme la migration multi-entités : l'entité principale existe d'emblée
        from app.models import Entite
        db.session.add(Entite(id=1, code="FUT", nom="FUTURA", couleur="#213E70", principale=True, actif=True))
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
    assert niveau(80)[0] == "excellent" and niveau(49.99)[0] == "insuffisant" and niveau(0)[0] == "insuffisant"


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
    # Sans N+1 choisi par la RH : personne n'est désigné (aucune déduction automatique)
    assert e.affectations[0].evaluateur_id is None
    r = c.post("/personnel/nouveau", data={"nom": "Mvogo", "departement_id": str(tech.id),
                                          "evaluateur_id": str(db.session.scalar(select(Employe.id).where(Employe.nom == "FOTSO")))})
    mvogo = db.session.scalar(select(Employe).where(Employe.nom == "MVOGO"))
    assert mvogo.affectations[0].evaluateur.nom == "FOTSO"
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
    c.post("/parametres/departements", data={"code": "CHT", "nom": "Chantiers"})
    dep = db.session.scalar(select(Departement).where(Departement.entite_id == ent.id))
    tchoua = db.session.scalar(select(Employe).where(Employe.nom == "TCHOUA"))
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
    assert "ONANA" in camp and "BEKONO" not in camp  # ne voit que la personne qu'il note
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
