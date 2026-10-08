"""Journal des actions importantes (traçabilité RH)."""
from flask import request
from flask_login import current_user

from ..extensions import db
from ..models import JournalAction


def journaliser(action: str, cible: str = "", details: str = "", commit: bool = False) -> None:
    ip = None
    try:
        # Railway place l'IP réelle dans X-Forwarded-For (ProxyFix activé)
        ip = request.remote_addr
    except RuntimeError:
        pass
    user_id = current_user.id if getattr(current_user, "is_authenticated", False) else None
    db.session.add(JournalAction(utilisateur_id=user_id, action=action,
                                 cible=(cible or "")[:200], details=details, ip=ip))
    if commit:
        db.session.commit()
