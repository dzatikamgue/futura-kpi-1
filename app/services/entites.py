"""Multi-entités : entité courante, entités accessibles, rôle par entité.

Convention de stockage : `entite_id` NULL = entité principale (données antérieures
au multi-entités, jamais réécrites). Toute requête passe par `cond_entite`.
"""
from __future__ import annotations

from flask import g, session
from sqlalchemy import or_, select

from ..extensions import db
from ..models import (AccesEntite, Affectation, Departement, Employe, Entite,
                      Projet, Role, RoleEntite)

ROLE_RH = "rh"


def principale() -> Entite:
    if getattr(g, "_entite_principale", None) is None:
        e = db.session.scalar(select(Entite).where(Entite.principale.is_(True)).order_by(Entite.id))
        if e is None:  # base créée sans les migrations (tests, db.create_all) : jamais en production
            from flask import current_app
            e = Entite(code="FUT", nom=current_app.config.get("COMPANY_NAME", "FUTURA"), couleur="#213E70",
                       principale=True, actif=True)
            db.session.add(e)
            db.session.commit()
        g._entite_principale = e
    return g._entite_principale


def id_effectif(entite_id: int | None) -> int:
    """NULL → id de l'entité principale."""
    return entite_id if entite_id is not None else principale().id


def cond_entite(col, entite_id: int):
    """Condition SQL « appartient à l'entité » (gère NULL = principale)."""
    if entite_id == principale().id:
        return or_(col == entite_id, col.is_(None))
    return col == entite_id


def valeur_stockage(entite_id: int) -> int:
    """Valeur à écrire dans entite_id pour une nouvelle ligne."""
    return entite_id


def roles_par_entite(user) -> dict[int, str]:
    """{entite_id: 'rh' | 'direction' | 'collaborateur'} pour les entités actives accessibles."""
    principale()
    actives = {e.id: e for e in db.session.scalars(select(Entite).where(Entite.actif.is_(True)))}
    if user.est_rh:
        return {i: ROLE_RH for i in actives}
    roles: dict[int, str] = {}
    for a in user.acces:
        roles[a.entite_id] = a.role
    emp = user.employe
    if emp is not None:
        roles.setdefault(id_effectif(emp.entite_id), user.role if user.role != Role.RH else RoleEntite.DIRECTION)
        # N+1 désigné dans une autre entité : accès « collaborateur » limité à ses N-1
        for (eid,) in db.session.execute(
                select(Employe.entite_id).join(Affectation, Affectation.employe_id == Employe.id)
                .where(Affectation.evaluateur_id == emp.id, Affectation.actif.is_(True)).distinct()):
            roles.setdefault(id_effectif(eid), RoleEntite.COLLABORATEUR)
        # Responsable d'un département / projet : consultation de son périmètre
        for model in (Departement, Projet):
            for (eid,) in db.session.execute(select(model.entite_id).where(model.responsable_id == emp.id).distinct()):
                roles.setdefault(id_effectif(eid), RoleEntite.COLLABORATEUR)
    elif not roles:
        # Compte antérieur non lié à une fiche : entité principale avec son rôle d'origine
        roles[principale().id] = user.role
    return {i: r for i, r in roles.items() if i in actives}


def entites_accessibles(user) -> list[Entite]:
    roles = roles_par_entite(user)
    if not roles:
        return []
    return db.session.scalars(select(Entite).where(Entite.id.in_(roles.keys()))
                              .order_by(Entite.principale.desc(), Entite.nom)).all()


def entite_courante(user) -> Entite:
    """Entité de l'onglet actif (mémorisée en session), validée à chaque requête."""
    if getattr(g, "_entite_courante", None) is not None:
        return g._entite_courante
    roles = roles_par_entite(user)
    voulu = session.get("entite_id")
    if voulu not in roles:
        voulu = None
        if user.employe is not None and id_effectif(user.employe.entite_id) in roles:
            voulu = id_effectif(user.employe.entite_id)
        elif roles:
            voulu = principale().id if principale().id in roles else sorted(roles)[0]
        else:
            voulu = principale().id
        session["entite_id"] = voulu
    g._entite_courante = db.session.get(Entite, voulu)
    return g._entite_courante


# --- Charte graphique -----------------------------------------------------------
def _hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_hex(rgb):
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in rgb)


def _melange(c1, c2, t):
    return tuple(a + (b - a) * t for a, b in zip(c1, c2))


def palette(couleur: str) -> dict:
    """Variables CSS dérivées de la couleur d'accent (clair et sombre)."""
    try:
        base = _hex_rgb(couleur or "#213E70")
    except ValueError:
        base = _hex_rgb("#213E70")
    blanc, noir = (255, 255, 255), (0, 0, 0)
    r, gg, b = base
    clair = _melange(base, blanc, .45)
    return {
        "brand": _rgb_hex(base),
        "brand_hover": _rgb_hex(_melange(base, noir, .2)),
        "brand_soft": _rgb_hex(_melange(base, blanc, .92)),
        "brand_ring": f"rgba({r}, {gg}, {b}, .28)",
        "dark_brand": _rgb_hex(clair),
        "dark_brand_hover": _rgb_hex(_melange(base, blanc, .6)),
        "dark_brand_soft": _rgb_hex(_melange(base, (17, 24, 39), .8)),
        "dark_brand_ring": "rgba({}, {}, {}, .35)".format(*[round(c) for c in clair]),
    }


def couleur_dominante(contenu: bytes) -> str | None:
    """Couleur d'accent proposée à partir du logo (ignore le blanc, le noir et les gris)."""
    import colorsys
    import io

    from PIL import Image
    try:
        img = Image.open(io.BytesIO(contenu)).convert("RGBA")
    except Exception:
        return None
    img.thumbnail((96, 96))
    comptes: dict[tuple, int] = {}
    for r, gg, b, a in img.getdata():
        if a < 128:
            continue
        h, l, s = colorsys.rgb_to_hls(r / 255, gg / 255, b / 255)
        if s < .25 or l > .85 or l < .12:
            continue
        cle = (r // 24, gg // 24, b // 24)
        comptes[cle] = comptes.get(cle, 0) + 1
    if not comptes:
        return None
    r, gg, b = max(comptes, key=comptes.get)
    rgb = (r * 24 + 12, gg * 24 + 12, b * 24 + 12)
    # Assure un contraste suffisant sur fond blanc (texte des boutons en blanc)
    h, l, s = colorsys.rgb_to_hls(*[c / 255 for c in rgb])
    l = min(l, .42)
    return _rgb_hex([c * 255 for c in colorsys.hls_to_rgb(h, l, s)])
