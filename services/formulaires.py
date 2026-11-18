"""Formulaires déclaratifs : description des champs, lecture et validation (messages en français).

Chaque champ : dict(nom, label, type, requis, options, aide, plein, min, max, defaut).
Types : texte, email, tel, zone, date, mois, montant, entier, decimal, choix, case.
`options` : liste de (valeur, libellé) ou fonction qui la renvoie.
"""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import request

from models import (CATEGORIES_REPARATION, CHARGES, MODES_PAIEMENT, CLASSES, MODES_PAIEMENT, MOTIFS_BILLET, PERIODICITES, RENOUVELLEMENT,
                    ROLES, STATUTS_BILLET, STATUTS_REPARATION, TYPES_LOGEMENT, URGENCES, VILLES)

class Invalide(ValueError):
    """Erreur de saisie avec un message destiné à l'utilisateur."""


RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def C(nom, label, type="texte", requis=False, **kw):
    return dict(nom=nom, label=label, type=type, requis=requis, **kw)


def paires(d):
    return list(d.items()) if isinstance(d, dict) else [(x, x) for x in d]


def lire_date(valeur):
    valeur = (valeur or "").strip()
    if not valeur:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(valeur, fmt).date()
        except ValueError:
            pass
    raise ValueError


def lire_mois(valeur):
    """'2026-11' ou '11/2026' -> date(2026, 11, 1)."""
    valeur = (valeur or "").strip()
    if not valeur:
        return None
    for fmt in ("%Y-%m", "%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(valeur, fmt).date().replace(day=1)
        except ValueError:
            pass
    raise ValueError


def lire_entier(valeur):
    """'1 250 000' -> 1250000 ; '' -> None ; texte invalide -> ValueError."""
    v = re.sub(r"[\s  .,]|FCFA|XAF", "", (valeur or "").upper())
    if not v:
        return None
    if not v.isdigit():
        raise ValueError
    return int(v)


def options_de(ch):
    o = ch.get("options") or []
    return o() if callable(o) else o


def lire(champs):
    """Lit request.form selon `champs`. Retourne (valeurs typées, erreurs, valeurs brutes pour réaffichage)."""
    val, err, brut = {}, {}, {}
    for ch in champs:
        n, t = ch["nom"], ch["type"]
        if t == "case":
            val[n] = brut[n] = bool(request.form.get(n))
            continue
        s = (request.form.get(n) or "").strip()
        brut[n] = s
        if not s:
            val[n] = None
            if ch["requis"]:
                err[n] = "Ce champ est obligatoire."
            continue
        try:
            if t in ("texte", "tel", "zone"):
                lim = 4000 if t == "zone" else ch.get("max_long", 160)
                if len(s) > lim:
                    raise Invalide(f"{lim} caractères maximum.")
                val[n] = s
            elif t == "email":
                if not RE_EMAIL.match(s):
                    raise Invalide("Adresse e-mail invalide (ex. nom@futura.cm).")
                val[n] = s.lower()
            elif t == "date":
                val[n] = lire_date(s)
            elif t == "mois":
                val[n] = lire_mois(s)
            elif t in ("montant", "entier"):
                v = lire_entier(s)
                if "min" in ch and v < ch["min"] or "max" in ch and v > ch["max"]:
                    raise Invalide(f"Valeur attendue entre {ch.get('min', 0)} et {ch.get('max')}.")
                val[n] = v
            elif t == "decimal":
                v = Decimal(s.replace(" ", "").replace(" ", "").replace(",", "."))
                if v < 0:
                    raise Invalide("La valeur doit être positive.")
                val[n] = v
            elif t == "choix":
                cles = {str(k) for k, _ in options_de(ch)}
                if s not in cles:
                    raise Invalide("Choisissez une valeur dans la liste.")
                val[n] = int(s) if ch.get("entier") else s
        except (ValueError, InvalidOperation) as e:
            msg = str(e) if isinstance(e, Invalide) else {
                "date": "Date invalide (JJ/MM/AAAA).", "mois": "Mois invalide.",
                "montant": "Montant invalide : chiffres uniquement (ex. 250 000).",
                "entier": "Nombre entier attendu.", "decimal": "Nombre attendu (ex. 125,5)."}.get(t, "Valeur invalide.")
            err[n] = msg
    return val, err, brut


def depuis_objet(champs, obj, **extra):
    """Valeurs d'un enregistrement existant, au format attendu par les champs HTML."""
    f = {}
    for ch in champs:
        v = getattr(obj, ch["nom"], None) if obj is not None else None
        if v is None:
            v = ch.get("defaut", "")
            v = v() if callable(v) else v
        if isinstance(v, date) and ch["type"] == "mois":
            v = v.strftime("%Y-%m")
        elif isinstance(v, date):
            v = v.strftime("%Y-%m-%d")
        elif ch["type"] == "montant" and isinstance(v, int):
            v = f"{v:,}".replace(",", " ")
        elif isinstance(v, Decimal):
            v = f"{v:f}".rstrip("0").rstrip(".").replace(".", ",")
        f[ch["nom"]] = v
    f.update(extra)
    return f


def appliquer(champs, obj, val, exclure=()):
    for ch in champs:
        if ch["nom"] not in exclure and ch["nom"] in val:
            setattr(obj, ch["nom"], val[ch["nom"]])


# --------------------------------------------------------------------------- #
#  Description des formulaires
# --------------------------------------------------------------------------- #

def champs_personne(logements, entreprises):
    return [
        C("nom", "Nom et prénom", requis=True, attrs='autocomplete="off" autofocus'),
        C("entreprise_id", "Entreprise", "choix", requis=True, options=entreprises, entier=True,
          aide="Entreprise du groupe ou entreprise externe pour laquelle la RH gère le dossier."),
        C("matricule", "Matricule", max_long=40),
        C("poste", "Poste / fonction"),
        C("nationalite", "Nationalité", max_long=80),
        C("telephone", "Téléphone", "tel", max_long=40),
        C("email", "E-mail", "email"),
        C("passeport_numero", "N° de passeport", max_long=40),
        C("passeport_expiration", "Expiration du passeport", "date",
          aide="Une alerte s'affiche aussi avant l'expiration du passeport."),
        C("logement_id", "Logement occupé", "choix", options=logements, entier=True, vide="— Aucun —"),
        C("notes", "Notes", "zone", plein=True),
    ]


def champs_entreprise():
    return [
        C("nom", "Nom de l'entreprise", requis=True, attrs="autofocus", aide="Ex. Futura SARL, BuildSmart BTP, SEG International"),
        C("type_entreprise", "Type", "choix", requis=True, vide=None, defaut="groupe",
          options=[("groupe", "Entreprise du groupe"), ("externe", "Entreprise externe (la RH gère ses dossiers)")]),
        C("sigle", "Sigle", max_long=20, aide="Facultatif. Ex. FUT, BSB"),
        C("contact_nom", "Personne à contacter"),
        C("contact_telephone", "Téléphone du contact", "tel", max_long=40),
        C("contact_email", "E-mail du contact", "email"),
        C("adresse", "Adresse", max_long=255, plein=True),
        C("notes", "Notes", "zone", plein=True, aide="Ex. conditions de refacturation des frais de visa."),
    ]


def champs_document(personnes):
    return [
        C("personne_id", "Personne", "choix", requis=True, options=personnes, entier=True, recherche=True),
        C("numero", "Numéro", max_long=60),
        C("categorie", "Catégorie", aide="Ex. visa de travail, visa d'affaires, carte de résident"),
        C("pays", "Pays de délivrance", max_long=80, defaut="Cameroun"),
        C("date_delivrance", "Date de début de validité", "date", requis=True),
        C("date_expiration", "Date de fin de validité", "date", requis=True,
          aide="Une notification apparaît 30 jours avant cette date pour lancer le renouvellement."),
        C("renouvellement", "Renouvellement", "choix", options=paires(RENOUVELLEMENT), vide=None),
        C("renouvellement_note", "Note sur le renouvellement", max_long=255,
          aide="Ex. dossier déposé à la DGSN le 12/11, récépissé n°…"),
        C("cout", "Coût", "montant", aide="En FCFA (frais de visa, timbres…)"),
        C("notes", "Notes", "zone", plein=True),
    ]


def champs_billet(personnes):
    return [
        C("personne_id", "Voyageur", "choix", requis=True, options=personnes, entier=True, recherche=True),
        C("motif", "Motif", "choix", requis=True, options=paires(MOTIFS_BILLET), defaut="conge"),
        C("depart", "Ville de départ", requis=True, max_long=80, defaut="Douala"),
        C("arrivee", "Destination", requis=True, max_long=80),
        C("date_depart", "Date de départ", "date", requis=True),
        C("date_retour", "Date de retour", "date", aide="Vide pour un aller simple."),
        C("compagnie", "Compagnie", max_long=80, liste="compagnies"),
        C("reservation", "Référence de réservation (PNR)", max_long=40),
        C("classe", "Classe", "choix", options=paires(CLASSES), vide=None, defaut="Économique"),
        C("montant", "Prix du billet", "montant", aide="En FCFA"),
        C("statut", "Statut", "choix", requis=True, options=paires(STATUTS_BILLET), vide=None, defaut="emis"),
        C("notes", "Notes", "zone", plein=True),
    ]


def champs_logement():
    return [
        C("batiment", "Nom du bâtiment", aide="Ex. ENERGY CLUB", attrs="autofocus", liste="batiments"),
        C("numero", "N° d'appartement", max_long=30, aide="Ex. 1A (étage / bloc)"),
        C("residents", "Résident(s)", "zone", plein=True, aide="Noms des personnes logées, séparés par des virgules."),
        C("date_entree", "Date d'entrée", "date"),
        C("nom", "Nom du logement", aide="Facultatif si le bâtiment et le n° d'appartement sont remplis."),
        C("type_logement", "Type", "choix", options=paires(TYPES_LOGEMENT), vide=None, defaut="Appartement"),
        C("ville", "Ville", "choix", options=paires(VILLES), vide="— Non précisée —", defaut="Douala"),
        C("quartier", "Quartier", max_long=120),
        C("adresse", "Adresse / repère", max_long=255),
        C("bailleur", "Bailleur"),
        C("bailleur_telephone", "Téléphone du bailleur", "tel", max_long=40),
        C("loyer", "Loyer mensuel", "montant", aide="En FCFA, par mois, hors charges payées à part."),
        C("charges_mode", "Charges communes", "choix", requis=True, vide=None, defaut="aucune", options=paires(CHARGES),
          aide="Gardiennage, entretien des parties communes, eau commune, groupe électrogène…"),
        C("charges", "Charges communes par mois", "montant",
          aide="En plus du loyer : montant ajouté à chaque échéance. Incluses : part du loyer (facultatif, pour information)."),
        C("periodicite", "Paiement du loyer", "choix", options=paires(PERIODICITES), entier=True, vide=None, defaut=1),
        C("jour_echeance", "Jour d'échéance", "entier", min=1, max=28, defaut=5,
          aide="Jour du mois où le loyer est dû (1 à 28)."),
        C("caution", "Caution versée", "montant"),
        C("bail_debut", "Période payée : début", "date", aide="Dernière période de loyer réglée."),
        C("bail_fin", "Période payée : fin", "date",
          aide="Payé jusqu'à cette date. Une notification apparaît 1 mois avant pour renouveler."),
        C("prochaine_debut", "Prochaine période (non payée) : début", "date"),
        C("prochaine_fin", "Prochaine période (non payée) : fin", "date"),
        C("montant_periode", "Montant TTC d'une période", "montant",
          aide="Tel que facturé (loyer + charges, caution éventuelle). Vide : calculé loyer + charges × périodicité."),
        C("mode_paiement", "Mode de paiement", max_long=30, liste="modes", aide="Ex. Espèces, Versement bancaire, Virement"),
        C("loyers_suivis_depuis", "Suivre les loyers à partir de", "mois",
          aide="Première échéance suivie. Les mois précédents ne sont pas comptés comme impayés."),
        C("compteur_electricite", "N° compteur / contrat ENEO", max_long=40),
        C("compteur_eau", "N° compteur / contrat CAMWATER", max_long=40),
        C("notes", "Notes", "zone", plein=True),
    ]


def champs_loyer(logements):
    return [
        C("logement_id", "Logement", "choix", requis=True, options=logements, entier=True, recherche=True),
        C("periode_debut", "Échéance payée (mois)", "mois", requis=True),
        C("montant", "Montant payé (total)", "montant", requis=True, aide="Loyer + charges communes payées à part."),
        C("montant_charges", "dont charges communes", "montant", aide="Pour un logement avec charges en plus du loyer."),
        C("date_paiement", "Payé le", "date", requis=True),
        C("mode_paiement", "Mode de paiement", "choix", options=paires(MODES_PAIEMENT)),
        C("reference", "Référence (n° de chèque, virement…)", max_long=80),
        C("notes", "Notes", max_long=255, plein=True),
    ]


def champs_facture(logements, type_energie):
    unite = "kWh" if type_energie == "electricite" else "m³"
    return [
        C("logement_id", "Logement", "choix", requis=True, options=logements, entier=True, recherche=True),
        C("periode", "Mois facturé", "mois", requis=True),
        C("numero", "N° de facture", max_long=60),
        C("consommation", f"Consommation ({unite})", "decimal"),
        C("montant", "Montant", "montant", requis=True),
        C("date_echeance", "À payer avant le", "date"),
        C("date_paiement", "Payée le", "date", aide="Laisser vide si la facture n'est pas encore payée."),
        C("mode_paiement", "Mode de paiement", "choix", options=paires(MODES_PAIEMENT)),
        C("reference", "Référence du paiement", max_long=80),
        C("notes", "Notes", max_long=255, plein=True),
    ]


def champs_reparation(logements=None):
    ch = [
        C("titre", "Problème constaté", requis=True, aide="Ex. Fuite d'eau sous l'évier de la cuisine",
          attrs="autofocus"),
        C("categorie", "Catégorie", "choix", requis=True, options=paires(CATEGORIES_REPARATION)),
        C("urgence", "Urgence", "choix", requis=True, options=paires(URGENCES), vide=None, defaut="normale"),
        C("description", "Description", "zone", requis=True, plein=True,
          aide="Où, depuis quand, ce qui a été constaté. Ajoutez des photos si possible."),
    ]
    if logements is not None:
        ch.insert(0, C("logement_id", "Logement", "choix", requis=True, options=logements, entier=True,
                       recherche=True))
    return ch


def champs_suivi():
    return [
        C("statut", "Nouveau statut", "choix", requis=True, options=paires(STATUTS_REPARATION), vide=None),
        C("intervenant", "Intervenant (artisan, entreprise)"),
        C("date_intervention", "Date d'intervention", "date"),
        C("cout", "Coût de la réparation", "montant"),
        C("texte", "Commentaire", "zone", plein=True),
    ]


def champs_utilisateur(logements, creation):
    return [
        C("nom", "Nom complet", requis=True, attrs="autofocus"),
        C("email", "E-mail (identifiant)", "email", requis=True),
        C("role", "Rôle", "choix", requis=True, options=paires(ROLES)),
        C("logement_id", "Logement (pour un occupant)", "choix", options=logements, entier=True,
          vide="— Aucun —", aide="L'occupant ne voit que ce logement et y signale les réparations."),
        C("mot_de_passe", "Mot de passe" if creation else "Nouveau mot de passe", requis=creation,
          aide="10 caractères minimum." + ("" if creation else " Laisser vide pour ne pas le changer."),
          attrs='autocomplete="new-password"'),
    ]
