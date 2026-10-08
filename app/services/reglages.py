"""Réglages saisis dans l'application.

La clé API Claude peut venir :
  1. de la variable d'environnement ANTHROPIC_API_KEY (prioritaire, ex. Railway) ;
  2. sinon de l'écran « Clé API Claude » (RH), stockée CHIFFRÉE en base.
Le chiffrement (Fernet) utilise une clé dérivée de SECRET_KEY : si SECRET_KEY
change, la clé stockée devient illisible et doit être ressaisie.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

from ..extensions import db
from ..models import Parametre

CLE_API = "anthropic_api_key"


def _fernet() -> Fernet:
    secret = current_app.config["SECRET_KEY"].encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"futura-reglages:" + secret).digest()))


def _cle_stockee() -> tuple[str | None, bool]:
    """(clé déchiffrée, illisible?)"""
    p = db.session.get(Parametre, CLE_API)
    if not p:
        return None, False
    try:
        return _fernet().decrypt(p.valeur.encode()).decode(), False
    except InvalidToken:
        return None, True


def source_cle_api() -> str | None:
    """'env', 'app' ou None."""
    if current_app.config.get("ANTHROPIC_API_KEY"):
        return "env"
    cle, _ = _cle_stockee()
    return "app" if cle else None


def cle_api() -> str:
    env = current_app.config.get("ANTHROPIC_API_KEY") or ""
    if env:
        return env
    cle, _ = _cle_stockee()
    return cle or ""


def etat_cle_api() -> dict:
    env = current_app.config.get("ANTHROPIC_API_KEY") or ""
    cle, illisible = _cle_stockee()
    p = db.session.get(Parametre, CLE_API)
    active = env or cle or ""
    return {
        "source": "env" if env else ("app" if cle else None),
        "masquee": masquer(active) if active else "",
        "illisible": illisible,
        "maj_le": p.updated_at if p else None,
        "maj_par": p.updated_by.nom_affiche if p and p.updated_by else None,
    }


def masquer(cle: str) -> str:
    return f"{cle[:7]}…{cle[-4:]}" if len(cle) > 14 else "••••"


def enregistrer_cle_api(cle: str, utilisateur_id: int | None) -> None:
    jeton = _fernet().encrypt(cle.encode()).decode()
    p = db.session.get(Parametre, CLE_API)
    if p:
        p.valeur, p.updated_by_id = jeton, utilisateur_id
    else:
        db.session.add(Parametre(cle=CLE_API, valeur=jeton, updated_by_id=utilisateur_id))


def supprimer_cle_api() -> None:
    p = db.session.get(Parametre, CLE_API)
    if p:
        db.session.delete(p)


def tester_cle_api(cle: str) -> tuple[bool, str]:
    """Vérifie la clé avec un vrai appel minimal (même format que l'import).

    Un simple contrôle de la clé ne suffit pas : un compte sans crédit passe ce contrôle
    mais échoue à l'import. Le coût de ce test est négligeable (quelques jetons).
    """
    import anthropic

    from .import_ia import OUTIL, detail_erreur_api, traduire_erreur_api
    modele = current_app.config["ANTHROPIC_MODEL"]
    try:
        client = anthropic.Anthropic(api_key=cle, timeout=30.0, max_retries=0)
        client.messages.create(
            model=modele, max_tokens=64, tools=[OUTIL], tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": "Test de connexion : réponds simplement « ok »."}])
        return True, f"Connexion réussie : la clé est valide, le compte a du crédit et le modèle {modele} répond."
    except anthropic.AuthenticationError:
        return False, "Clé refusée par Anthropic : vérifiez qu'elle est complète et active."
    except anthropic.PermissionDeniedError as exc:
        return False, "Clé valide mais sans accès à ce modèle : " + detail_erreur_api(exc)[:200]
    except anthropic.NotFoundError:
        return False, (f"Clé valide, mais le modèle « {modele} » est introuvable. "
                       "Corrigez la variable ANTHROPIC_MODEL sur Railway ou supprimez-la.")
    except anthropic.RateLimitError:
        return True, "Clé valide (le service est momentanément saturé, réessayez plus tard)."
    except anthropic.APIConnectionError:
        return False, "Le serveur n'arrive pas à joindre Anthropic. Réessayez plus tard."
    except anthropic.APIStatusError as exc:
        return False, traduire_erreur_api(exc)
