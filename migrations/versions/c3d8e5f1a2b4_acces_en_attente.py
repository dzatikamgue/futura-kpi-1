"""Marqueur « accès à remettre » pour les comptes créés automatiquement

Migration purement ADDITIVE : une colonne vide (NULL pour tous les comptes
existants, qui ne sont donc jamais considérés « en attente »). Aucune donnée
existante n'est modifiée.

Revision ID: c3d8e5f1a2b4
Revises: b7c41d2e9a10
Create Date: 2026-10-08 21:00:00

"""
import sqlalchemy as sa
from alembic import op

revision = 'c3d8e5f1a2b4'
down_revision = 'b7c41d2e9a10'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('utilisateurs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('acces_en_attente', sa.Boolean(), nullable=True))


def downgrade():
    with op.batch_alter_table('utilisateurs', schema=None) as batch_op:
        batch_op.drop_column('acces_en_attente')
