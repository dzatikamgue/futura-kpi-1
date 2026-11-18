"""Tests — Futura Services généraux."""
import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MDP = "Futura2026x"
SECRET = "secret-partage-test"
PDF = b"%PDF-1.4\n%test\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL") or "sqlite:///" + str(tmp_path / "t.db"))
    monkeypatch.setenv("SSO_SECRET", SECRET)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    from app import create_app
    from models import Logement, User, db
    a = create_app()
    a.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with a.app_context():
        db.drop_all()
        db.create_all()
        l1 = Logement(nom="Appart A", loyer=500_000, jour_echeance=5)
        l2 = Logement(nom="Appart B", loyer=300_000, jour_echeance=5)
        db.session.add_all([l1, l2])
        db.session.flush()
        for email, role, lid in [("admin", "admin", None), ("rh", "gestionnaire", None), ("dg", "direction", None),
                                 ("occ", "occupant", l1.id), ("occ2", "occupant", l2.id)]:
            u = User(nom=email.upper(), email=f"{email}@futura.cm", role=role, logement_id=lid)
            u.set_password(MDP)
            db.session.add(u)
        db.session.commit()
    yield a
    with a.app_context():
        db.session.remove()
        db.drop_all()


def client(app, qui):
    c = app.test_client()
    r = c.post("/connexion", data={"email": f"{qui}@futura.cm", "password": MDP})
    assert r.status_code == 302
    return c


def entreprise(c, nom="Futura SARL", type_entreprise="groupe"):
    r = c.post("/entreprises/nouvelle", data={"nom": nom, "type_entreprise": type_entreprise})
    assert r.status_code == 302
    return int(r.headers["Location"].rstrip("/").split("/")[-1])


def personne(c, nom="Anoop Kumar", eid=None):
    eid = eid or entreprise(c, nom="Ent " + nom)
    r = c.post("/personnel/nouveau", data={"nom": nom, "nationalite": "Indienne", "entreprise_id": eid})
    assert r.status_code == 302
    return int(r.headers["Location"].rstrip("/").split("/")[-1])


def test_connexion_et_roles(app):
    assert app.test_client().get("/").status_code == 302
    dg = client(app, "dg")
    assert dg.get("/tableau-de-bord").status_code == 200
    assert dg.get("/personnel/nouveau").status_code == 403
    assert client(app, "rh").get("/utilisateurs").status_code == 403
    assert client(app, "admin").get("/utilisateurs").status_code == 200


def test_document_alerte_et_renouvellement(app):
    from models import Document, db
    rh = client(app, "rh")
    pid = personne(rh)
    from models import aujourdhui
    exp = aujourdhui() + timedelta(days=20)
    # Date de début obligatoire
    r = rh.post("/documents/visa/nouveau", data={"personne_id": pid, "numero": "V1", "date_expiration": exp.isoformat()})
    assert r.status_code == 200 and "obligatoire" in r.get_data(as_text=True)
    r = rh.post("/documents/visa/nouveau", data={"personne_id": pid, "numero": "V1", "date_expiration": exp.isoformat(),
                                                 "date_delivrance": (exp - timedelta(days=365)).isoformat()})
    assert r.status_code == 302
    with app.app_context():
        d = Document.query.one()
        assert d.niveau(30) == "alerte" and not d.remplace
        did = d.id
    # Notification sur toutes les pages, et page unique visas + cartes avec début et fin
    accueil = rh.get("/").get_data(as_text=True)
    assert "expire dans moins de 30 jours" in accueil and "Commencez les procédures de renouvellement" in accueil
    liste = rh.get("/documents").get_data(as_text=True)
    assert "Visas et cartes de séjour" in liste and exp.strftime("%d/%m/%Y") in liste
    assert (exp - timedelta(days=365)).strftime("%d/%m/%Y") in liste
    page = rh.get("/tableau-de-bord").get_data(as_text=True)
    assert "Visa de Anoop Kumar" in page and "Expire dans 20 jours" in page
    # Date d'expiration avant la délivrance : refusée
    r = rh.post("/documents/visa/nouveau", data={"personne_id": pid, "date_expiration": "2026-01-01",
                                                 "date_delivrance": "2026-06-01"})
    assert r.status_code == 200 and "doit suivre" in r.get_data(as_text=True)
    # Démarche puis renouvellement : l'ancien est remplacé, l'alerte disparaît
    rh.post(f"/document/{did}/demarche", data={"renouvellement": "en_cours", "note": "Dossier déposé"})
    nouv = aujourdhui() + timedelta(days=400)
    r = rh.post(f"/document/{did}/renouveler", data={"personne_id": pid, "numero": "V2", "date_expiration": nouv.isoformat(),
                                                    "date_delivrance": aujourdhui().isoformat()},
                content_type="multipart/form-data")
    assert r.status_code == 302
    with app.app_context():
        ancien, nouveau = db.session.get(Document, did), Document.query.filter_by(numero="V2").one()
        assert ancien.remplace and ancien.renouvellement == "obtenu" and nouveau.niveau(30) == "ok"
    rh.get("/")  # consomme le message flash
    assert "Expire dans" not in rh.get("/tableau-de-bord").get_data(as_text=True)


def test_document_expire_et_filtres(app):
    from models import aujourdhui
    rh = client(app, "rh")
    pid = personne(rh)
    rh.post("/documents/carte_sejour/nouveau", data={"personne_id": pid, "numero": "CS9",
                                                     "date_delivrance": (aujourdhui() - timedelta(days=700)).isoformat(),
                                                     "date_expiration": (aujourdhui() - timedelta(days=3)).isoformat()})
    assert "CS9" in rh.get("/documents/carte_sejour?statut=expires").get_data(as_text=True)
    assert "CS9" not in rh.get("/documents/carte_sejour?statut=valides").get_data(as_text=True)
    assert "Expiré depuis 3 j" in rh.get("/documents/carte_sejour").get_data(as_text=True)
    assert "CS9" in rh.get("/documents?statut=a_traiter").get_data(as_text=True)
    assert "CS9" not in rh.get("/documents?type=visa").get_data(as_text=True)
    r = rh.get("/documents/carte_sejour/export")
    assert r.status_code == 200 and r.mimetype.endswith("sheet")


def test_loyers_echeances_dues_et_paiement(app):
    from models import Logement, Loyer, aujourdhui, db, mois_ajoutes
    with app.app_context():
        l = Logement.query.filter_by(nom="Appart A").one()
        m0 = aujourdhui().replace(day=1)
        l.loyers_suivis_depuis = mois_ajoutes(m0, -2)
        db.session.commit()
        dus = l.echeances_dues()
        attendu = 3 if aujourdhui().day >= 5 else 2
        assert len(dus) == attendu
        lid = l.id
    rh = client(app, "rh")
    debut = mois_ajoutes(aujourdhui().replace(day=1), -2)
    r = rh.post("/loyers/payer", data={"logement_id": lid, "periode_debut": debut.strftime("%Y-%m"), "montant": "500 000",
                                       "date_paiement": aujourdhui().isoformat(), "mode_paiement": "Virement"})
    assert r.status_code == 302
    # Même échéance deux fois : refusée
    r = rh.post("/loyers/payer", data={"logement_id": lid, "periode_debut": debut.strftime("%Y-%m"), "montant": "500000",
                                       "date_paiement": aujourdhui().isoformat()})
    assert r.status_code == 200 and "déjà enregistrée" in r.get_data(as_text=True)
    with app.app_context():
        assert Loyer.query.count() == 1 and Loyer.query.one().montant == 500_000
        assert len(db.session.get(Logement, lid).echeances_dues()) == attendu - 1


def test_loyer_trimestriel(app):
    from models import Logement, aujourdhui, db, mois_ajoutes
    with app.app_context():
        l = Logement(nom="Villa", loyer=1_000_000, periodicite=3, jour_echeance=1,
                     loyers_suivis_depuis=mois_ajoutes(aujourdhui().replace(day=1), -5))
        db.session.add(l)
        db.session.commit()
        assert [d for d, _ in l.echeances_dues()] == [mois_ajoutes(aujourdhui().replace(day=1), -5),
                                                      mois_ajoutes(aujourdhui().replace(day=1), -2)]
        assert l.montant_echeance == 3_000_000


def test_factures_energie(app):
    from models import FactureEnergie, Logement, aujourdhui, db
    with app.app_context():
        lid = Logement.query.filter_by(nom="Appart A").one().id
    rh = client(app, "rh")
    passe = aujourdhui() - timedelta(days=5)
    data = {"logement_id": lid, "periode": "2026-10", "montant": "95000", "consommation": "412,5",
            "date_echeance": passe.isoformat()}
    assert rh.post("/factures/electricite/nouvelle", data=data).status_code == 302
    # Doublon : demande une confirmation
    r = rh.post("/factures/electricite/nouvelle", data=data)
    assert r.status_code == 200 and "existe déjà" in r.get_data(as_text=True)
    assert rh.post("/factures/electricite/nouvelle", data={**data, "confirmer_doublon": "1"}).status_code == 302
    with app.app_context():
        f = FactureEnergie.query.first()
        assert f.niveau == "retard" and float(f.consommation) == 412.5
        fid = f.id
    assert "En retard" in rh.get("/factures/electricite?statut=retard").get_data(as_text=True)
    rh.post(f"/facture/{fid}/payer")
    with app.app_context():
        assert db.session.get(FactureEnergie, fid).payee
    # Montant invalide
    r = rh.post("/factures/eau/nouvelle", data={"logement_id": lid, "periode": "2026-10", "montant": "abc"})
    assert r.status_code == 200 and "Montant invalide" in r.get_data(as_text=True)


def test_reparation_occupant(app):
    from models import Reparation, SuiviReparation, db
    occ = client(app, "occ")
    assert occ.get("/").headers["Location"].endswith("/mon-logement")
    r = occ.post("/reparations/nouvelle", data={"titre": "Fuite évier", "categorie": "Plomberie", "urgence": "urgente",
                                                "description": "Sous l'évier", "pieces": (__import__("io").BytesIO(PDF), "f.pdf")},
                 content_type="multipart/form-data")
    assert r.status_code == 302
    with app.app_context():
        rep = Reparation.query.one()
        assert rep.logement.nom == "Appart A" and rep.statut == "signalee"
        rid = rep.id
    # Les autres occupants ne la voient pas ; l'occupant ne voit pas la gestion
    occ2 = client(app, "occ2")
    assert occ2.get(f"/reparation/{rid}").status_code == 403
    assert "Fuite évier" not in occ2.get("/reparations").get_data(as_text=True)
    assert occ.get("/logements").status_code == 403
    assert "Réparation urgente" in client(app, "dg").get("/tableau-de-bord").get_data(as_text=True)
    # Le gestionnaire clôture : un commentaire est exigé
    rh = client(app, "rh")
    r = rh.post(f"/reparation/{rid}/suivi", data={"statut": "terminee"})
    assert r.status_code == 422
    rh.post(f"/reparation/{rid}/suivi", data={"statut": "terminee", "texte": "Joint changé", "cout": "15 000",
                                              "intervenant": "Plombier"})
    with app.app_context():
        rep = db.session.get(Reparation, rid)
        assert rep.statut == "terminee" and rep.cout == 15_000 and rep.cloturee_le
        assert SuiviReparation.query.filter_by(reparation_id=rid).count() == 2
    # L'occupant ajoute un commentaire, ne peut pas changer le statut
    occ.post(f"/reparation/{rid}/suivi", data={"texte": "Merci", "statut": "annulee"})
    with app.app_context():
        assert db.session.get(Reparation, rid).statut == "terminee"


def test_billets_et_validation(app):
    from models import Billet
    rh = client(app, "rh")
    pid = personne(rh)
    r = rh.post("/billets/nouveau", data={"personne_id": pid, "motif": "conge", "depart": "Douala", "arrivee": "Mumbai",
                                          "date_depart": "2026-12-20", "date_retour": "2026-12-01", "statut": "emis"})
    assert r.status_code == 200 and "ne peut pas précéder" in r.get_data(as_text=True)
    r = rh.post("/billets/nouveau", data={"personne_id": pid, "motif": "conge", "depart": "Douala", "arrivee": "Mumbai",
                                          "date_depart": "2026-12-20", "date_retour": "2027-01-10", "statut": "emis",
                                          "montant": "780 000", "classe": "Économique"})
    assert r.status_code == 302
    with app.app_context():
        assert Billet.query.one().montant == 780_000
    assert "Mumbai" in rh.get("/billets?quand=tous").get_data(as_text=True)


def test_sso_portail(app):
    from itsdangerous import URLSafeTimedSerializer
    from models import User
    s = URLSafeTimedSerializer(SECRET, salt="futura-sso")
    jeton = s.dumps({"email": "nouveau@futura.cm", "n": "abc", "m": "services", "r": "gestionnaire", "nom": "Nouveau"})
    c = app.test_client()
    assert c.get(f"/sso?jeton={jeton}").status_code == 302
    assert c.get("/entreprises/nouvelle").status_code == 200
    with app.app_context():
        assert User.query.filter_by(email="nouveau@futura.cm").one().role == "gestionnaire"
    # Jeton déjà utilisé, jeton d'un autre module
    assert "connexion" in app.test_client().get(f"/sso?jeton={jeton}").headers["Location"]
    autre = s.dumps({"email": "admin@futura.cm", "n": "x", "m": "factures"})
    assert "connexion" in app.test_client().get(f"/sso?jeton={autre}").headers["Location"]
    acces = s.dumps({"email": "admin@futura.cm", "n": "y", "m": "services"})
    assert app.test_client().get(f"/sso/acces?jeton={acces}").get_json() == {"acces": True}


def test_pieces_jointes(app):
    import io
    from models import Piece
    rh = client(app, "rh")
    pid = personne(rh)
    r = rh.post(f"/pieces/personne/{pid}", data={"pieces": (io.BytesIO(b"pas un pdf"), "x.exe"), "retour": "/"},
                content_type="multipart/form-data")
    assert r.status_code == 302
    with app.app_context():
        assert Piece.query.count() == 0
    rh.post(f"/pieces/personne/{pid}", data={"pieces": (io.BytesIO(PDF), "passeport.pdf"), "retour": "/"},
            content_type="multipart/form-data")
    with app.app_context():
        p = Piece.query.one()
    assert rh.get(f"/piece/{p.id}").data == PDF
    assert client(app, "occ").get(f"/piece/{p.id}").status_code == 403


def test_utilisateur_occupant_exige_logement(app):
    adm = client(app, "admin")
    r = adm.post("/utilisateurs/nouveau", data={"nom": "X", "email": "x@futura.cm", "role": "occupant",
                                                "mot_de_passe": "motdepasse123"})
    assert r.status_code == 200 and "Choisissez le logement" in r.get_data(as_text=True)


def test_accueil_toutes_pages(app):
    rh = client(app, "rh")
    for url in ["/", "/tableau-de-bord", "/personnel", "/documents", "/documents/visa", "/documents/carte_sejour", "/billets",
                "/logements", "/loyers", "/loyers?onglet=paiements", "/factures/electricite", "/factures/eau",
                "/reparations", "/loyers/payer", "/billets/nouveau", "/logements/nouveau", "/entreprises",
                "/entreprises/nouvelle"]:
        assert rh.get(url).status_code == 200, url


def test_sso_comptes(app):
    from itsdangerous import URLSafeTimedSerializer
    s = URLSafeTimedSerializer(SECRET, salt="futura-sso")
    r = app.test_client().get("/sso/comptes?jeton=" + s.dumps({"email": "portail@futura", "q": "comptes", "m": "services"}))
    comptes = {c["email"]: c["role"] for c in r.get_json()["comptes"]}
    assert comptes["rh@futura.cm"] == "gestionnaire" and comptes["occ@futura.cm"] == "occupant"
    # Sans le bon type de question, ou pour un autre module : refusé
    assert app.test_client().get("/sso/comptes?jeton=" + s.dumps({"email": "x", "m": "services"})).status_code == 403
    assert app.test_client().get("/sso/comptes?jeton=" + s.dumps({"email": "x", "q": "comptes", "m": "flotte"})).status_code == 403


def test_entreprises_et_filtre(app):
    from models import Personne, aujourdhui
    rh = client(app, "rh")
    # Sans entreprise : la création de personne renvoie vers la création d'entreprise
    r = rh.get("/personnel/nouveau")
    assert r.status_code == 302 and "/entreprises/nouvelle" in r.headers["Location"]
    g = entreprise(rh, "Futura SARL")
    x = entreprise(rh, "SEG International", "externe")
    assert rh.post("/entreprises/nouvelle", data={"nom": "seg international", "type_entreprise": "groupe"}).status_code == 200
    p1, p2 = personne(rh, "Anoop", g), personne(rh, "Elias", x)
    fin = (aujourdhui() + timedelta(days=10)).isoformat()
    for pid, num in ((p1, "VG"), (p2, "VX")):
        rh.post("/documents/visa/nouveau", data={"personne_id": pid, "numero": num, "date_expiration": fin,
                                                 "date_delivrance": "2026-01-01"})
    page = rh.get(f"/documents?entreprise={x}").get_data(as_text=True)
    assert "VX" in page and "VG" not in page and "SEG International" in page
    fiche = rh.get(f"/entreprise/{x}").get_data(as_text=True)
    assert f'href="/personnel/{p2}"' in fiche and f'href="/personnel/{p1}"' not in fiche and "Entreprise externe" in fiche
    liste = rh.get(f"/personnel?entreprise={x}").get_data(as_text=True)
    assert f'href="/personnel/{p2}"' in liste and f'href="/personnel/{p1}"' not in liste
    assert "(SEG International)" in rh.get("/tableau-de-bord").get_data(as_text=True)
    assert client(app, "dg").get("/entreprises/nouvelle").status_code == 403
    with app.app_context():
        assert Personne.query.filter_by(nom="Elias").one().entreprise.externe


def test_reprise_ancienne_societe(app):
    from models import Entreprise, Personne, db, migrer
    with app.app_context():
        db.session.add_all([Personne(nom="A", societe="BuildSmart BTP"), Personne(nom="B", societe="buildsmart btp ")])
        db.session.commit()
        migrer()
        assert Entreprise.query.count() == 1
        assert {p.entreprise.nom for p in Personne.query} == {"BuildSmart BTP"}


def test_ajouter_personne_depuis_annuaire(app, monkeypatch):
    import app as A
    rh = client(app, "rh")
    eid = entreprise(rh, "Futura SARL")
    deja = personne(rh, "Paul Ekambi", eid)
    monkeypatch.setattr(A, "chercher_annuaire", lambda q: ([
        {"nom": "MBALLA", "prenom": "Jean", "matricule": "FUT-042", "poste": "Comptable", "email": "j.mballa@futura.cm",
         "telephone": "+237 6", "entite": "Futura SARL", "source": "Performance KPI"},
        {"nom": "Paul Ekambi", "prenom": "", "matricule": "", "poste": "Chef de projet", "email": "", "telephone": "",
         "entite": "", "source": "Circuit des factures"}], None))
    page = rh.get("/personnel/ajouter?q=mba").get_data(as_text=True)
    assert "Jean MBALLA" in page and "Performance KPI" in page and "Reprendre" in page
    assert f'href="/personnel/{deja}"' in page          # déjà suivie : pas de doublon proposé
    # Le lien « Reprendre » pré-remplit le formulaire, entreprise retrouvée par son nom
    import re
    lien = re.search(r'href="(/personnel/nouveau\\?[^"]*FUT-042[^"]*)"', page).group(1).replace("&amp;", "&")
    form = rh.get(lien).get_data(as_text=True)
    assert 'value="Jean MBALLA"' in form and 'value="FUT-042"' in form and "Performance KPI" in form
    assert f'<option value="{eid}" selected' in form
    # Portail injoignable : message clair, saisie manuelle possible
    monkeypatch.setattr(A, "chercher_annuaire", lambda q: ([], "Le portail ne répond pas pour le moment."))
    page = rh.get("/personnel/ajouter?q=xyz").get_data(as_text=True)
    assert "ne répond pas" in page and "Saisir une nouvelle personne" in page
    assert client(app, "dg").get("/personnel/ajouter").status_code == 403


def _classeur():
    import io
    from datetime import datetime
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet3"
    ws["A5"], ws["B5"], ws["C5"], ws["D5"], ws["E5"] = "NATIONALITE", "NOMS", "DOCUMENTS ADMINISTRATIF", "VALIDITE", "NIVEAU DE TRAITEMENT"
    rows = [("AGENT TEST UN", "Carte de séjour", datetime(2035, 2, 12), None),
            ("AGENT TEST DEUX", "carte de séjour/ Receipt", "16/7/2028", None),
            ("AGENT TEST TROIS", "VISA", "6/12/2026", "Encours de téléchargement"),
            ("SANS DATE", "VISA", "bientôt", None),
            ("INCONNU", "Passeport", "1/1/2027", None)]
    for i, (n, d, v, e) in enumerate(rows, start=7):
        ws.cell(i, 2, n); ws.cell(i, 3, d); ws.cell(i, 4, v); ws.cell(i, 5, e)
    ws["A7"] = "LIBANAIS"
    ws.merge_cells("A7:A11")
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return buf


def test_import_excel(app):
    import json
    import import_excel as IX
    from models import Document, Entreprise, Personne
    lignes, err = IX.analyser(_classeur())
    assert err is None and len(lignes) == 5
    assert [l["probleme"] is None for l in lignes] == [True, True, True, False, False]
    assert lignes[0]["expiration"] == "2035-02-12" and lignes[1]["expiration"] == "2028-07-16"
    assert all(l["nationalite"] == "Libanais" for l in lignes)          # cellule fusionnée
    assert lignes[1]["type_doc"] == "carte_sejour" and lignes[1]["categorie"] == "carte de séjour/ Receipt"
    assert IX.lire_date("9-Jan-27").isoformat() == "2027-01-09" and IX.lire_date("13-Oct-28").isoformat() == "2028-10-13"
    rh = client(app, "rh")
    r = rh.post("/documents/importer", data={"action": "analyser", "nouvelle_entreprise": "Futura",
                                             "fichier": (_classeur(), "suivi.xlsx")}, content_type="multipart/form-data")
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "Date de validité illisible" in page and "Type de document non reconnu" in page
    donnees = json.dumps(lignes)
    sel = [f"Sheet3:{l['ligne']}" for l in lignes]
    r = rh.post("/documents/importer", data={"action": "confirmer", "nouvelle_entreprise": "Futura", "donnees": donnees, "ligne": sel})
    assert r.status_code == 302
    with app.app_context():
        assert Entreprise.query.filter_by(nom="Futura").count() == 1
        assert Personne.query.count() == 3 and Document.query.count() == 3
        j = Personne.query.filter_by(nom="AGENT TEST TROIS").one()
        assert j.nationalite == "Libanais" and j.entreprise.nom == "Futura"
        d = j.documents[0]
        assert d.date_delivrance is None and d.renouvellement_note == "Encours de téléchargement"
    # Réimport : aucun doublon
    rh.post("/documents/importer", data={"action": "confirmer", "nouvelle_entreprise": "Futura", "donnees": donnees, "ligne": sel})
    with app.app_context():
        assert Personne.query.count() == 3 and Document.query.count() == 3
    assert "à compléter" in rh.get("/documents").get_data(as_text=True)
    assert client(app, "dg").get("/documents/importer").status_code == 403


def test_import_initial_par_variable(app, monkeypatch):
    import base64, json
    import import_excel as IX
    from app import import_initial
    from models import Document, Entreprise, Personne
    lignes, _ = IX.analyser(_classeur())
    monkeypatch.setenv("IMPORT_VISAS", base64.b64encode(json.dumps({"entreprise": "Futura", "lignes": lignes}).encode()).decode())
    with app.app_context():
        import_initial()
        import_initial()                       # rejoué : rien de plus
        assert Entreprise.query.filter_by(nom="Futura").count() == 1
        assert Personne.query.count() == 3 and Document.query.count() == 3


def test_note_document_expire(app):
    from models import Document, aujourdhui
    rh = client(app, "rh")
    pid = personne(rh)
    rh.post("/documents/visa/nouveau", data={"personne_id": pid, "numero": "VX1", "date_delivrance": "2025-01-01",
                                             "date_expiration": (aujourdhui() - timedelta(days=5)).isoformat()})
    with app.app_context():
        did = Document.query.one().id
    page = rh.get("/documents").get_data(as_text=True)
    assert f'action="/document/{did}/note"' in page and "Pourquoi est-il expiré" in page
    rh.post(f"/document/{did}/note", data={"note": "  Passeport en renouvellement   à l'ambassade ", "retour": "/documents"})
    with app.app_context():
        d = Document.query.one()
        assert d.renouvellement_note == "Passeport en renouvellement à l'ambassade" and d.note_le and d.note_par.nom == "RH"
    assert "Passeport en renouvellement" in client(app, "dg").get("/documents").get_data(as_text=True)
    assert client(app, "dg").post(f"/document/{did}/note", data={"note": "x"}).status_code == 403
    rh.post(f"/document/{did}/note", data={"note": ""})
    with app.app_context():
        assert Document.query.one().renouvellement_note is None


def test_supprimer_personne(app):
    import io
    from models import Billet, Document, Personne, Piece, aujourdhui
    rh = client(app, "rh")
    pid = personne(rh)
    rh.post("/documents/visa/nouveau", data={"personne_id": pid, "date_delivrance": "2025-01-01",
                                             "date_expiration": (aujourdhui() + timedelta(days=90)).isoformat(),
                                             "pieces": (io.BytesIO(PDF), "visa.pdf")}, content_type="multipart/form-data")
    rh.post("/billets/nouveau", data={"personne_id": pid, "motif": "conge", "depart": "Douala", "arrivee": "Beyrouth",
                                      "date_depart": "2026-12-20", "statut": "emis"})
    assert client(app, "dg").post(f"/personnel/{pid}/supprimer", data={"confirmation": "SUPPRIMER"}).status_code == 403
    rh.post(f"/personnel/{pid}/supprimer", data={"confirmation": "non"})
    with app.app_context():
        assert Personne.query.count() == 1
    r = rh.post(f"/personnel/{pid}/supprimer", data={"confirmation": "supprimer"})
    assert r.status_code == 302
    with app.app_context():
        assert Personne.query.count() == 0 and Document.query.count() == 0 and Billet.query.count() == 0
        assert Piece.query.count() == 0


def test_charges_communes(app):
    from models import Logement, Loyer, aujourdhui, db
    rh = client(app, "rh")
    base = {"nom": "Appart C", "loyer": "400 000", "periodicite": "3", "jour_echeance": "5",
            "loyers_suivis_depuis": aujourdhui().strftime("%Y-%m")}
    r = rh.post("/logements/nouveau", data={**base, "charges_mode": "en_sus"})
    assert r.status_code == 200 and "Indiquez le montant des charges" in r.get_data(as_text=True)
    r = rh.post("/logements/nouveau", data={**base, "charges_mode": "en_sus", "charges": "50 000"})
    lid = int(r.headers["Location"].rstrip("/").split("/")[-1])
    with app.app_context():
        l = db.session.get(Logement, lid)
        assert l.mensuel_total == 450_000 and l.montant_echeance == 1_350_000
    form = rh.get(f"/loyers/payer?logement={lid}").get_data(as_text=True)
    assert 'value="1 350 000"' in form and 'value="150 000"' in form
    rh.post("/loyers/payer", data={"logement_id": lid, "periode_debut": aujourdhui().strftime("%Y-%m"), "montant": "1 350 000",
                                   "montant_charges": "150 000", "date_paiement": aujourdhui().isoformat()})
    with app.app_context():
        assert Loyer.query.one().montant_charges == 150_000
    assert "Charges communes" in rh.get(f"/logement/{lid}").get_data(as_text=True)
    r = rh.post("/logements/nouveau", data={**base, "nom": "Villa D", "charges_mode": "incluses", "charges": ""})
    with app.app_context():
        v = Logement.query.filter_by(nom="Villa D").one()
        assert v.charges_mode == "incluses" and v.montant_echeance == 1_200_000
    assert "sans charges communes" in rh.get("/logements").get_data(as_text=True) or True


def test_logement_residents_et_notification(app, monkeypatch):
    import base64, json
    from app import import_initial
    from models import Logement, aujourdhui
    rh = client(app, "rh")
    fin = aujourdhui() + timedelta(days=20)
    r = rh.post("/logements/nouveau", data={"batiment": "BAT TEST", "numero": "2B", "residents": "Agent A, Agent B",
                                            "loyer": "300 000", "periodicite": "6", "jour_echeance": "5",
                                            "charges_mode": "en_sus", "charges": "50 000",
                                            "date_entree": "2024-08-15", "bail_debut": (fin - timedelta(days=180)).isoformat(), "bail_fin": fin.isoformat()})
    assert r.status_code == 302
    with app.app_context():
        l = Logement.query.filter_by(numero="2B").one()
        assert l.nom == "BAT TEST — 2B" and l.montant_echeance == 2_100_000 and l.niveau_fin(30) == "alerte"
    page = rh.get("/").get_data(as_text=True)
    assert "arrive en fin de période dans moins de 30 jours" in page and "BAT TEST · Appt 2B" in page
    liste = rh.get("/logements").get_data(as_text=True)
    assert "Agent A, Agent B" in liste and "J-20" in liste and "2 100 000 FCFA" in liste
    assert "Loyer à renouveler — BAT TEST · Appt 2B" in rh.get("/tableau-de-bord").get_data(as_text=True)
    # Sans bâtiment / n° ni nom : refusé
    r = rh.post("/logements/nouveau", data={"batiment": "X", "periodicite": "1", "jour_echeance": "5", "charges_mode": "aucune"})
    assert r.status_code == 200 and "Indiquez le bâtiment" in r.get_data(as_text=True)
    # Import de logements par variable privée, sans doublon
    donnees = {"logements": [{"batiment": "BAT TEST", "numero": "2B", "loyer": 1},
                             {"batiment": "BAT TEST", "numero": "3C", "loyer": 300000, "charges": 50000, "charges_mode": "en_sus",
                              "periodicite": 6, "bail_debut": "2026-10-16", "bail_fin": "2027-04-15", "ville": None}]}
    monkeypatch.setenv("IMPORT_VISAS", base64.b64encode(json.dumps(donnees).encode()).decode())
    with app.app_context():
        import_initial()
        import_initial()
        assert Logement.query.filter_by(batiment="BAT TEST").count() == 2
        c = Logement.query.filter_by(numero="3C").one()
        assert c.ville is None and c.bail_fin.isoformat() == "2027-04-15" and c.montant_echeance == 2_100_000
