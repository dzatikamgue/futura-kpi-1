"""Extraction de KPI (critères d'évaluation) depuis un document ou une image, via l'API Claude.

Rien n'est écrit en base à l'extraction : le résultat est proposé à la RH, qui le corrige,
choisit la grille de destination (commune ou poste) et valide.
"""
from __future__ import annotations

from flask import current_app

from .import_ia import appeler_claude

OUTIL_KPI = {
    "name": "enregistrer_kpi",
    "description": "Enregistre les indicateurs clés de performance (critères d'évaluation) extraits du document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kpis": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "libelle": {"type": "string", "description": "Intitulé court du critère (moins de 120 caractères)"},
                        "description": {"type": "string",
                                        "description": "Ce qui est évalué, indicateurs, objectifs chiffrés (moins de 400 caractères)"},
                        "poids": {"type": "number", "minimum": 0.01, "maximum": 100,
                                  "description": "Coefficient : la pondération du document telle quelle (ex. 30 pour 30 %), sinon 1 à 5 selon l'importance"},
                        "ponderation_document": {"type": "string",
                                                 "description": "Pondération telle qu'écrite dans le document (ex. « 20 % »), sinon vide"},
                        "poste": {"type": "string",
                                  "description": "Poste / fonction concerné si le document distingue les postes, sinon vide"},
                        "confiance": {"type": "string", "enum": ["haute", "moyenne", "basse"]},
                    },
                    "required": ["libelle", "poids", "confiance"],
                },
            },
            "remarques": {"type": "string", "description": "Remarques générales sur le document"},
        },
        "required": ["kpis"],
    },
}

CONSIGNE_KPI = (
    "Tu aides le service RH de l'entreprise {entreprise} (Cameroun) à définir la grille d'évaluation mensuelle "
    "de son personnel : chaque critère sera noté de 0 à 100 par le N+1, la note globale étant la moyenne pondérée. "
    "Extrais du document TOUS les KPI / critères d'évaluation / indicateurs de performance et appelle l'outil "
    "enregistrer_kpi. Règles :\n"
    "- Un critère = une compétence ou un résultat évaluable chaque mois (ex. « Respect des délais »).\n"
    "- Libellé court et clair en français ; mets les détails, indicateurs et objectifs chiffrés dans la description.\n"
    "- Pondération : si le document donne des pourcentages ou des points, utilise-les tels quels comme "
    "coefficient (30 % → 30) et recopie la pondération d'origine ; sinon 1 par défaut, "
    "3 pour un critère présenté comme essentiel.\n"
    "- Si le document attribue des KPI à des postes / fonctions précis, indique le poste pour chacun ; "
    "un même KPI valable pour plusieurs postes peut être répété avec chaque poste.\n"
    "- N'invente aucun critère absent du document ; ignore titres, totaux, signatures.\n"
    "- confiance='basse' si la lecture est douteuse."
)


def extraire_kpi(nom_fichier: str, type_mime: str, contenu: bytes | None, texte: str = ""):
    """Renvoie (kpis, remarques). kpis : [{libelle, description, poids, ponderation, poste, confiance}]."""
    data = appeler_claude(OUTIL_KPI, CONSIGNE_KPI.format(entreprise=current_app.config["COMPANY_NAME"]),
                          nom_fichier, type_mime, contenu, texte)
    kpis = []
    for k in data.get("kpis", []):
        lib = " ".join(str(k.get("libelle") or "").split())[:120]
        if not lib:
            continue
        try:
            from .grilles import lire_coefficient
            poids = lire_coefficient(k.get("poids")) or 1
        except (TypeError, ValueError):
            poids = 1
        kpis.append({"libelle": lib, "description": " ".join(str(k.get("description") or "").split())[:400],
                     "poids": poids, "ponderation": str(k.get("ponderation_document") or "").strip()[:40],
                     "poste": " ".join(str(k.get("poste") or "").split())[:120],
                     "confiance": k.get("confiance") or "moyenne"})
    return kpis, str(data.get("remarques") or "")
