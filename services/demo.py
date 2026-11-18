"""Données de démonstration (flask --app app seed-demo). Ne pas lancer sur la base de production."""
from datetime import timedelta

from models import (Billet, Document, FactureEnergie, Logement, Loyer, Personne, Reparation, SuiviReparation, User,
                    aujourdhui, db, mois_ajoutes, now)

MDP = "Futura2026"


def charger():
    j = aujourdhui()
    m0 = j.replace(day=1)

    def compte(email, nom, role, logement=None):
        u = User.query.filter_by(email=email).first() or User(email=email)
        u.nom, u.role, u.actif, u.logement = nom, role, True, logement
        u.set_password(MDP)
        db.session.add(u)
        return u

    admin = compte("admin@futura.cm", "Administrateur", "admin")
    compte("rh@futura.cm", "Carine Mbarga (RH)", "gestionnaire")
    compte("dg@futura.cm", "Direction générale", "direction")

    L = []
    for nom, quartier, loyer, per, occ_nom in [
        ("Appartement A2 — Résidence Bali", "Bonapriso", 650_000, 1, "Anoop Kumar"),
        ("Villa Les Palmiers", "Bonanjo", 1_200_000, 3, "Elias Nayef"),
        ("Studio Akwa 4B", "Akwa", 250_000, 1, "Imad Obeid"),
        ("Appartement Kotto C1", "Kotto", 400_000, 1, None),
    ]:
        l = Logement(nom=nom, quartier=quartier, ville="Douala", type_logement="Villa" if "Villa" in nom else "Appartement",
                     bailleur="SCI " + quartier, bailleur_telephone="+237 6 99 00 00 00", loyer=loyer, periodicite=per,
                     jour_echeance=5, caution=loyer * 3, bail_debut=j - timedelta(days=400),
                     bail_fin=j + timedelta(days=40 if quartier == "Akwa" else 400),
                     loyers_suivis_depuis=mois_ajoutes(m0, -5), compteur_electricite="ENEO-" + quartier[:3].upper() + "-0412",
                     compteur_eau="CDE-" + quartier[:3].upper() + "-77")
        db.session.add(l)
        L.append(l)
    db.session.flush()

    P = []
    for nom, poste, nat, l, v_j, c_j in [
        ("Anoop Kumar", "Ingénieur MEP", "Indienne", L[0], 25, 200),
        ("Elias Nayef", "Directeur de projet", "Libanaise", L[1], -6, 50),
        ("Imad Obeid", "Architecte", "Libanaise", L[2], 300, 12),
        ("Mathavan R.", "Conducteur de travaux", "Indienne", None, 120, 380),
    ]:
        p = Personne(nom=nom, poste=poste, societe="Futura SARL", nationalite=nat, logement=l,
                     passeport_numero="P" + str(abs(hash(nom)) % 10_000_000), passeport_expiration=j + timedelta(days=900),
                     telephone="+237 6 77 00 00 00", cree_par_id=admin.id)
        db.session.add(p)
        db.session.flush()
        db.session.add(Document(personne=p, type_doc="visa", numero="V-" + str(p.id).zfill(5), categorie="Visa de travail",
                                date_delivrance=j + timedelta(days=v_j - 365), date_expiration=j + timedelta(days=v_j),
                                renouvellement="en_cours" if v_j < 0 else "", cout=150_000))
        db.session.add(Document(personne=p, type_doc="carte_sejour", numero="CS-" + str(p.id).zfill(6),
                                categorie="Carte de résident", date_delivrance=j + timedelta(days=c_j - 730),
                                date_expiration=j + timedelta(days=c_j), cout=250_000))
        P.append(p)

    for p, dest, dd, motif, prix, st in [
        (P[0], "Mumbai", 12, "conge", 780_000, "emis"), (P[1], "Beyrouth", -40, "conge", 920_000, "utilise"),
        (P[2], "Paris", 30, "mission", 650_000, "reserve"), (P[3], "Chennai", 75, "conge", 810_000, "emis"),
    ]:
        db.session.add(Billet(personne=p, depart="Douala", arrivee=dest, date_depart=j + timedelta(days=dd),
                              date_retour=j + timedelta(days=dd + 30), motif=motif, montant=prix, statut=st,
                              compagnie="Ethiopian Airlines" if dest != "Paris" else "Air France",
                              reservation="PNR" + str(dd).replace("-", "M"), classe="Économique"))

    # Loyers : tout payé sauf le mois en cours de Kotto et deux mois du studio
    for l in L:
        d = l.loyers_suivis_depuis
        while d <= m0:
            impaye = (l is L[2] and d >= mois_ajoutes(m0, -1)) or (l is L[3] and d == m0)
            if not impaye:
                db.session.add(Loyer(logement=l, periode_debut=d, mois=l.periodicite, montant=l.montant_echeance,
                                     date_paiement=d.replace(day=4), mode_paiement="Virement", reference="VIR-" + d.strftime("%y%m")))
            d = mois_ajoutes(d, l.periodicite)

    for i, l in enumerate(L):
        for k in range(1, 5):
            per = mois_ajoutes(m0, -k)
            for t, base in (("electricite", 85_000), ("eau", 18_000)):
                paye = k > 1 or (i % 2 == 0)
                db.session.add(FactureEnergie(logement=l, type_energie=t, periode=per, numero=f"{t[:3].upper()}{per:%y%m}{i}",
                                              consommation=(420 + 37 * i + 11 * k) if t == "electricite" else (14 + i + k),
                                              montant=base + 7_500 * i + 3_000 * k,
                                              date_echeance=mois_ajoutes(per, 1).replace(day=15),
                                              date_paiement=mois_ajoutes(per, 1).replace(day=10) if paye else None,
                                              mode_paiement="Mobile money" if paye else None))

    occ = compte("occupant@futura.cm", "Anoop Kumar", "occupant", L[0])
    db.session.flush()
    for l, titre, cat, urg, st, cout, jours in [
        (L[0], "Fuite d'eau sous l'évier de la cuisine", "Plomberie", "urgente", "signalee", None, 1),
        (L[1], "Climatiseur du salon ne refroidit plus", "Climatisation", "normale", "en_cours", 85_000, 9),
        (L[2], "Serrure de la porte d'entrée bloquée", "Serrurerie", "normale", "terminee", 25_000, 20),
        (L[0], "Prise électrique de la chambre grillée", "Électricité", "normale", "prise_en_charge", None, 3),
    ]:
        r = Reparation(logement=l, titre=titre, categorie=cat, urgence=urg, statut=st,
                       description="Constaté par l'occupant. Photos jointes si possible.",
                       signale_le=now() - timedelta(days=jours), statut_depuis=now() - timedelta(days=max(0, jours - 2)),
                       signale_par_id=occ.id if l is L[0] else admin.id, cout=cout,
                       intervenant="Ets Ngando & Fils" if st != "signalee" else None,
                       cloturee_le=now() - timedelta(days=2) if st == "terminee" else None,
                       date_intervention=j - timedelta(days=3) if st in ("en_cours", "terminee") else None)
        db.session.add(r)
        db.session.flush()
        db.session.add(SuiviReparation(reparation_id=r.id, user_id=r.signale_par_id, statut="signalee",
                                       texte="Demande signalée.", date=r.signale_le))
        if st != "signalee":
            db.session.add(SuiviReparation(reparation_id=r.id, user_id=admin.id, statut=st,
                                           texte="Artisan contacté, intervention planifiée.", date=r.statut_depuis))
    db.session.commit()
