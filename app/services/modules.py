"""Portail des modules Futura (KPI, Flotte automobile, …) avec connexion unique.

Aucune donnée en base : la liste des modules est dans le code, leurs adresses et
secrets dans les variables d'environnement. Un module externe est actif si ces deux
variables existent :
    MODULE_<CODE>_URL     adresse de l'application (ex. https://futura-flotte-production.up.railway.app)
    MODULE_<CODE>_SECRET  secret partagé avec cette application (même valeur que son SSO_SECRET)

Connexion unique : le portail envoie l'utilisateur vers <url>/sso?jeton=… ; le jeton,
signé avec le secret partagé, contient son e-mail et expire après 60 secondes.
L'application du module retrouve son compte par l'e-mail et le connecte.
Ajouter un module : une entrée dans MODULES + les deux variables + la route /sso côté module.
"""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.parse
import urllib.request

from flask import session
from itsdangerous import URLSafeTimedSerializer

SEL = "futura-sso"
DUREE_JETON = 60          # secondes

MODULES = [
    {"code": "kpi", "nom": "Performance KPI", "icone": "star", "interne": True,
     "description": "Notation mensuelle du personnel, grilles KPI, suivi trimestriel et annuel."},
    {"code": "flotte", "nom": "Flotte automobile", "icone": "truck",
     "description": "Véhicules, chauffeurs, kilométrage, pannes et immobilisations."},
]


def _conf(code: str) -> tuple[str | None, str | None]:
    url = (os.environ.get(f"MODULE_{code.upper()}_URL") or "").strip().rstrip("/")
    secret = (os.environ.get(f"MODULE_{code.upper()}_SECRET") or "").strip()
    return (url or None, secret or None)


def externes_configures() -> list[dict]:
    return [m for m in MODULES if not m.get("interne") and all(_conf(m["code"]))]


def module(code: str) -> dict | None:
    m = next((x for x in MODULES if x["code"] == code), None)
    if m and not m.get("interne") and not all(_conf(code)):
        return None
    return m


def _signer(code: str, donnees: dict) -> str:
    _url, secret = _conf(code)
    return URLSafeTimedSerializer(secret, salt=SEL).dumps({**donnees, "m": code})


def url_ouverture(code: str, email: str) -> str:
    """Adresse de connexion unique vers le module (jeton à usage unique, 60 s)."""
    url, _s = _conf(code)
    jeton = _signer(code, {"email": email.strip().lower(), "n": secrets.token_urlsafe(12)})
    return f"{url}/sso?{urllib.parse.urlencode({'jeton': jeton})}"


def a_acces(code: str, email: str) -> bool | None:
    """Le module a-t-il un compte actif pour cet e-mail ? (None si le module ne répond pas)

    Réponse mémorisée 5 minutes dans la session pour ne pas ralentir la navigation.
    """
    cache = session.get("acces_modules") or {}
    c = cache.get(code)
    if c and time.time() - c[1] < 300:
        return c[0]
    url, _s = _conf(code)
    jeton = _signer(code, {"email": email.strip().lower(), "q": 1})
    try:
        with urllib.request.urlopen(f"{url}/sso/acces?{urllib.parse.urlencode({'jeton': jeton})}", timeout=3) as r:
            ok = bool(json.loads(r.read().decode("utf-8")).get("acces"))
    except Exception:                                   # module injoignable : on ne bloque pas le portail
        return None
    cache[code] = [ok, time.time()]
    session["acces_modules"] = cache
    return ok


def modules_pour(email: str) -> list[dict]:
    """Modules proposés à l'utilisateur : KPI + modules externes où il a un compte (ou injoignables)."""
    res = []
    for m in MODULES:
        if m.get("interne"):
            res.append({**m, "etat": "ok"})
        elif all(_conf(m["code"])):
            acces = a_acces(m["code"], email)
            if acces is not False:
                res.append({**m, "etat": "ok" if acces else "inconnu"})
    return res
