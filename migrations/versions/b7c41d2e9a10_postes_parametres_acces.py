"""Référentiel des postes, réglages (clé API Claude), date de remise des accès

Migration purement ADDITIVE : deux tables vides et une colonne vide.
Aucune ligne existante n'est lue ni modifiée.

Revision ID: b7c41d2e9a10
Revises: 5ae5806a2359
Create Date: 2026-10-08 15:30:00

"""
import sqlalchemy as sa
from alembic import op

revision = 'b7c41d2e9a10'
down_revision = '5ae5806a2359'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
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


def downgrade():
    with op.batch_alter_table('utilisateurs', schema=None) as batch_op:
        batch_op.drop_column('acces_remis_le')
    op.drop_table('parametres')
    op.drop_table('postes')
