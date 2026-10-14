"""Copie des postes et des grilles KPI d'une entité vers une autre.

N'ajoute que ce qui manque dans l'entité cible (les postes et critères existants sont
réutilisés, rapprochés par leur libellé). Aucune fiche salarié ni évaluation n'est touchée.
"""
from __future__ import annotations

from sqlalchemy import func, select

from ..extensions import db
from ..models import Critere, Poste
from . import grilles as G
from .entites import cond_entite
from .organisation import cle_texte, marquer_poste_supprime, postes_actifs


def copier(source_eid: int, cible_eid: int, *, postes: bool = True, commune: bool = True,
           grilles_postes: bool = True, ecraser: bool = False, utilisateur_id: int | None = None) -> dict:
    if source_eid == cible_eid:
        raise ValueError("Choisissez une autre entité que l'entité active.")
    bilan = {"postes": 0, "criteres": 0, "commune": 0, "grilles": 0, "grilles_ignorees": 0}

    # 1) Postes
    postes_cible = {cle_texte(p) for p in postes_actifs(cible_eid)}
    lignes_cible = {cle_texte(p.libelle): p for p in db.session.scalars(
        select(Poste).where(cond_entite(Poste.entite_id, cible_eid)))}

    def assurer_poste(lib: str) -> None:
        k = cle_texte(lib)
        marquer_poste_supprime(cible_eid, lib, False, utilisateur_id)
        if k in postes_cible:
            return
        ligne = lignes_cible.get(k)
        if ligne:
            ligne.actif = True
        else:
            ligne = Poste(libelle=lib, entite_id=cible_eid)
            db.session.add(ligne)
            lignes_cible[k] = ligne
        postes_cible.add(k)
        bilan["postes"] += 1

    if postes:
        for lib in postes_actifs(source_eid):
            assurer_poste(lib)

    # 2) Critères : rapprochement par libellé, création si absent
    criteres_cible = {cle_texte(c.libelle): c for c in db.session.scalars(
        select(Critere).where(cond_entite(Critere.entite_id, cible_eid)))}
    caches_cible = G.masques(cible_eid)
    spe_source = G.specifiques(source_eid)
    ordre = db.session.scalar(select(func.max(Critere.ordre)).where(cond_entite(Critere.entite_id, cible_eid))) or 0

    def critere_cible(c: Critere, coef, specifique: bool) -> Critere:
        nonlocal ordre
        k = cle_texte(c.libelle)
        cc = criteres_cible.get(k)
        if cc is None:
            ordre += 1
            cc = Critere(entite_id=cible_eid, libelle=c.libelle, description=c.description,
                         poids=min(max(int(round(coef)), 1), 5), ordre=ordre)
            db.session.add(cc)
            db.session.flush()
            G.definir_coefficient(cc, coef, utilisateur_id)
            if specifique:
                G.marquer_specifique(cible_eid, cc.id, True, utilisateur_id)
            criteres_cible[k] = cc
            bilan["criteres"] += 1
        elif cc.id in caches_cible:            # supprimé auparavant dans la cible : il revient
            G.reprendre_masque(cible_eid, cc, utilisateur_id)
            G.definir_coefficient(cc, coef, utilisateur_id)
            caches_cible.discard(cc.id)
        return cc

    # 3) Grille commune
    if commune:
        for x in G.grille_commune(source_eid):
            cc = critere_cible(x.critere, x.poids, False)
            if not cc.actif:
                cc.actif = True
            if cc.id in G.specifiques(cible_eid):
                G.marquer_specifique(cible_eid, cc.id, False, utilisateur_id)
            bilan["commune"] += 1

    # 4) Grilles des postes
    if grilles_postes:
        existantes = G.grilles(cible_eid)
        for k, defn in G.grilles(source_eid).items():
            if not defn["criteres"]:
                continue
            if k in existantes and existantes[k]["criteres"] and not ecraser:
                bilan["grilles_ignorees"] += 1
                continue
            sources = {c.id: c for c in db.session.scalars(select(Critere).where(
                Critere.id.in_(defn["criteres"]), Critere.actif.is_(True)))}
            nouvelle = {}
            for cid, coef in defn["criteres"].items():
                if cid in sources:
                    cc = critere_cible(sources[cid], coef, cid in spe_source)
                    nouvelle[cc.id] = coef
            if nouvelle:
                assurer_poste(defn["libelle"])
                G.definir_grille(cible_eid, defn["libelle"], nouvelle, utilisateur_id)
                bilan["grilles"] += 1
    return bilan
