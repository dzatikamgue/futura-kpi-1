"""Outils transverses : pagination SQL, lecture de dates."""
from __future__ import annotations

import math
from datetime import date, datetime

from sqlalchemy import func, select

from .extensions import db


class Page:
    def __init__(self, items, page, per_page, total):
        self.items = items
        self.page = page
        self.per_page = per_page
        self.total = total
        self.pages = max(1, math.ceil(total / per_page)) if per_page else 1
        self.has_prev = page > 1
        self.has_next = page < self.pages
        self.prev_num = page - 1
        self.next_num = page + 1
        self.premier = (page - 1) * per_page + 1 if total else 0
        self.dernier = min(page * per_page, total)

    def iter_pages(self, voisins=2):
        dernier = 0
        for n in range(1, self.pages + 1):
            if n <= 1 or n >= self.pages or abs(n - self.page) <= voisins:
                if dernier and n - dernier > 1:
                    yield None
                yield n
                dernier = n


def paginer(q, page: int = 1, per_page: int = 25, scalars: bool = True) -> Page:
    """Pagination SQL (LIMIT/OFFSET) d'un select, mono ou multi-entités."""
    page = max(1, page or 1)
    total = db.session.scalar(select(func.count()).select_from(q.order_by(None).subquery())) or 0
    pages = max(1, math.ceil(total / per_page))
    page = min(page, pages)
    res = db.session.execute(q.limit(per_page).offset((page - 1) * per_page))
    items = res.scalars().all() if scalars else [tuple(r) for r in res.all()]
    return Page(items, page, per_page, total)


def lire_date(valeur: str | None) -> date | None:
    """Accepte JJ/MM/AAAA ou AAAA-MM-JJ (champ date HTML)."""
    v = (valeur or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    raise ValueError("Date invalide (format JJ/MM/AAAA).")
