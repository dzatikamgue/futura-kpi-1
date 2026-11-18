"""Import du fichier Excel de suivi des visas et cartes de séjour.

Format attendu (celui du fichier « SUIVI DES CARTES DE SEJOUR ET VISA ») : une ligne d'en-tête avec
NATIONALITE · NOMS · DOCUMENTS ADMINISTRATIF · VALIDITE · NIVEAU DE TRAITEMENT (ordre libre, la dernière
colonne est facultative). La nationalité peut être une cellule fusionnée sur un groupe de lignes.

Aucune valeur n'est devinée : une ligne sans nom, sans type de document reconnu ou sans date de fin
lisible est signalée et n'est pas importée. La date de début, absente du fichier, reste vide.
"""
import re
import unicodedata
from datetime import date, datetime

MOIS_EN = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MOIS_FR = {"janv": 1, "fevr": 2, "fev": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6, "juil": 7, "aout": 8, "sept": 9,
           "oct": 10, "nov": 11, "dec": 12}


def _norme(t):
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", t).strip()


def _annee(a):
    a = int(a)
    return a + 2000 if a < 100 else a


def lire_date(v):
    """Cellule Excel -> date. Les textes sont lus jour/mois/année (format du fichier). None si illisible."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    t = _norme(v)
    if not t:
        return None
    m = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", t)
    if m:
        try:
            return date(_annee(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})[\s\-/.]*([a-z]+)\.?[\s\-/.]*(\d{2,4})", t)
    if m:
        mois = MOIS_EN.get(m.group(2)[:3]) or MOIS_FR.get(m.group(2)) or MOIS_FR.get(m.group(2)[:4])
        if mois:
            try:
                return date(_annee(m.group(3)), mois, int(m.group(1)))
            except ValueError:
                return None
    return None


def type_document(texte):
    t = _norme(texte)
    if "carte" in t or "sejour" in t:
        return "carte_sejour"
    if "visa" in t:
        return "visa"
    return None


ENTETES = {
    "nationalite": ("nationalite",),
    "nom": ("noms", "nom", "nom et prenom", "noms et prenoms"),
    "document": ("documents administratif", "documents administratifs", "document administratif", "document", "documents"),
    "validite": ("validite", "date de validite", "expiration", "date d'expiration", "fin de validite"),
    "traitement": ("niveau de traitement", "traitement", "observation", "observations"),
}


def _trouver_entete(ws):
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 30)):
        cols = {}
        for c in row:
            n = _norme(c.value)
            for cle, noms in ENTETES.items():
                if n in noms and cle not in cols:
                    cols[cle] = c.column
        if {"nom", "document", "validite"} <= cols.keys():
            return row[0].row, cols
    return None, None


def analyser(fichier):
    """Lit le classeur. Retourne (lignes, erreur_globale).

    Chaque ligne : dict(feuille, ligne, nom, nationalite, type_doc, categorie, expiration (iso), note, probleme).
    """
    from openpyxl import load_workbook
    try:
        wb = load_workbook(fichier, data_only=True)
    except Exception:
        return [], "Fichier illisible : envoyez le classeur Excel (.xlsx) d'origine."
    lignes = []
    for ws in wb.worksheets:
        h, cols = _trouver_entete(ws)
        if not h:
            continue
        # Cellules fusionnées (ex. nationalité écrite une fois pour un groupe) : valeur recopiée sur tout le groupe
        fusion = {}
        for rng in ws.merged_cells.ranges:
            v = ws.cell(rng.min_row, rng.min_col).value
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    fusion[(r, c)] = v

        def val(r, cle):
            c = cols.get(cle)
            if not c:
                return None
            return fusion.get((r, c), ws.cell(r, c).value)

        for r in range(h + 1, ws.max_row + 1):
            nom = re.sub(r"\s+", " ", str(val(r, "nom") or "")).strip()
            doc_txt = re.sub(r"\s+", " ", str(val(r, "document") or "")).strip()
            brut = val(r, "validite")
            if not nom and not doc_txt and brut in (None, ""):
                continue
            exp = lire_date(brut)
            t = type_document(doc_txt)
            probleme = None
            if not nom:
                probleme = "Nom manquant"
            elif not t:
                probleme = f"Type de document non reconnu : « {doc_txt or 'vide'} »"
            elif not exp:
                probleme = f"Date de validité illisible : « {brut if brut not in (None, '') else 'vide'} »"
            nat = re.sub(r"\s+", " ", str(val(r, "nationalite") or "")).strip()
            categorie = doc_txt if _norme(doc_txt) not in ("visa", "carte de sejour") else ""
            lignes.append(dict(feuille=ws.title, ligne=r, nom=nom, nationalite=nat.capitalize() if nat.isupper() else nat,
                               type_doc=t, categorie=categorie[:120], expiration=exp.isoformat() if exp else None,
                               note=re.sub(r"\s+", " ", str(val(r, "traitement") or "")).strip()[:255],
                               probleme=probleme))
    if not lignes:
        return [], ("Aucune ligne trouvée : le fichier doit avoir une ligne d'en-tête avec les colonnes "
                    "NOMS, DOCUMENTS ADMINISTRATIF et VALIDITE.")
    return lignes, None


def cle_nom(nom):
    return _norme(nom)
