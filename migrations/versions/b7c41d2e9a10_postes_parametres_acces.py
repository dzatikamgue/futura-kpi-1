"""Référentiel des postes, réglages (clé API Claude), suivi de remise des accès

Revision ID: b7c41d2e9a10
Revises: 5ae5806a2359
Create Date: 2026-10-08 15:30:00

"""
import unicodedata
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = 'b7c41d2e9a10'
down_revision = '5ae5806a2359'
branch_labels = None
depends_on = None


def upgrade():
    postes = op.create_table(
        'postes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('libelle', sa.String(length=120), nullable=False),
        sa.Column('actif', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('libelle'),
    )
    op.create_table(
        'parametres',
        sa.Column('cle', sa.String(length=60), nullable=False),
        sa.Column('valeur', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('updated_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['updated_by_id'], ['utilisateurs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('cle'),
    )
    with op.batch_alter_table('utilisateurs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('acces_remis_le', sa.DateTime(), nullable=True))

    # Les comptes existants ont déjà reçu leurs accès
    op.execute("UPDATE utilisateurs SET acces_remis_le = created_at")

    # Le référentiel reprend les postes déjà saisis sur les fiches. Les variantes
    # (casse, accents : « ingenieur » / « Ingénieur ») sont fusionnées.
    def cle(txt):
        txt = unicodedata.normalize("NFKD", txt.lower())
        return " ".join("".join(c for c in txt if not unicodedata.combining(c)).split())

    conn = op.get_bind()
    canon, lignes = {}, []
    maintenant = datetime.now(timezone.utc).replace(tzinfo=None)
    brutes = [r[0] for r in conn.execute(sa.text(
        "SELECT poste, COUNT(*) FROM employes WHERE poste IS NOT NULL GROUP BY poste ORDER BY COUNT(*) DESC, poste"))]
    for libelle in brutes:
        lib = " ".join((libelle or "").split())[:120]
        if not lib:
            continue
        k = cle(lib)
        if k not in canon:
            canon[k] = lib
            lignes.append({"libelle": lib, "actif": True, "created_at": maintenant, "updated_at": maintenant})
        if libelle != canon[k]:
            conn.execute(sa.text("UPDATE employes SET poste = :c WHERE poste = :b"), {"c": canon[k], "b": libelle})
    if lignes:
        op.bulk_insert(postes, lignes)


def downgrade():
    with op.batch_alter_table('utilisateurs', schema=None) as batch_op:
        batch_op.drop_column('acces_remis_le')
    op.drop_table('parametres')
    op.drop_table('postes')
