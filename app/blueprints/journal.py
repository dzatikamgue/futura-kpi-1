"""Journal des actions (RH uniquement)."""
from flask import Blueprint, render_template, request
from flask_login import login_required
from sqlalchemy import or_, select
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..models import JournalAction, Utilisateur
from ..permissions import rh_requis
from ..services.exports import export_excel
from ..utils import paginer

bp = Blueprint("journal", __name__, url_prefix="/journal")


@bp.get("/")
@login_required
@rh_requis
def index():
    q = (select(JournalAction).options(joinedload(JournalAction.utilisateur).joinedload(Utilisateur.employe))
         .outerjoin(Utilisateur, JournalAction.utilisateur_id == Utilisateur.id))
    if terme := (request.args.get("q") or "").strip():
        like = f"%{terme}%"
        q = q.where(or_(JournalAction.action.ilike(like), JournalAction.cible.ilike(like),
                        JournalAction.details.ilike(like), Utilisateur.email.ilike(like)))
    if action := request.args.get("action"):
        q = q.where(JournalAction.action == action)
    q = q.order_by(JournalAction.created_at.desc(), JournalAction.id.desc())
    if request.args.get("export") == "xlsx":
        lignes = [[j.created_at.strftime("%d/%m/%Y %H:%M"), j.utilisateur.email if j.utilisateur else "—",
                   j.action, j.cible or "", j.details or "", j.ip or ""] for j in db.session.scalars(q.limit(20000))]
        return export_excel("Journal des actions", ["Date (UTC)", "Utilisateur", "Action", "Cible", "Détails", "IP"],
                            lignes, "journal")
    actions = db.session.scalars(select(JournalAction.action).distinct().order_by(JournalAction.action)).all()
    return render_template("journal.html", page=paginer(q, request.args.get("page", 1, type=int), 50),
                           actions=actions)
