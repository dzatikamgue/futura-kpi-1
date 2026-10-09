"""Multi-entités : entités, accès par entité, rattachement des données à une entité

Migration ADDITIVE pour les données :
- nouvelles tables `entites` et `acces_entites` (une seule ligne insérée : l'entité
  principale, dans la nouvelle table) ;
- nouvelles colonnes `entite_id` VIDES (NULL = entité principale) ;
- les contraintes d'unicité globales (code, nom, matricule, libellé) deviennent
  « unique par entité ». Aucune ligne existante n'est lue ni modifiée.

Revision ID: d4e9f2a6b8c1
Revises: c3d8e5f1a2b4
Create Date: 2026-10-09 12:00:00

"""
import os
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = 'd4e9f2a6b8c1'
down_revision = 'c3d8e5f1a2b4'
branch_labels = None
depends_on = None

# Les contraintes UNIQUE du schéma initial n'ont pas de nom explicite :
# PostgreSQL les nomme <table>_<colonne>_key ; sous SQLite on les nomme à la réflexion.
CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _nom_uq(table, col):
    return f"{table}_{col}_key" if op.get_bind().dialect.name == "postgresql" else f"uq_{table}_{col}"


def _rattacher(table, anciennes_uq, nouvelles_uq, fk_ondelete="RESTRICT", index_unique_a_retirer=None):
    with op.batch_alter_table(table, schema=None, naming_convention=CONVENTION) as b:
        if index_unique_a_retirer:
            b.drop_index(index_unique_a_retirer)
            b.create_index(index_unique_a_retirer, [index_unique_a_retirer.split("_", 2)[2]], unique=False)
        for col in anciennes_uq:
            b.drop_constraint(_nom_uq(table, col), type_="unique")
        b.add_column(sa.Column('entite_id', sa.Integer(), nullable=True))
        b.create_foreign_key(f"fk_{table}_entite", 'entites', ['entite_id'], ['id'], ondelete=fk_ondelete)
        b.create_index(f"ix_{table}_entite_id", ['entite_id'], unique=False)
        for nom, cols in nouvelles_uq:
            b.create_unique_constraint(nom, cols)


def upgrade():
    entites = op.create_table(
        'entites',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=12), nullable=False),
        sa.Column('nom', sa.String(length=120), nullable=False),
        sa.Column('couleur', sa.String(length=7), nullable=False),
        sa.Column('logo', sa.LargeBinary(), nullable=True),
        sa.Column('logo_mime', sa.String(length=40), nullable=True),
        sa.Column('principale', sa.Boolean(), nullable=False),
        sa.Column('actif', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    maintenant = datetime.now(timezone.utc).replace(tzinfo=None)
    nom = (os.environ.get("COMPANY_NAME") or "FUTURA").strip()[:120]
    op.bulk_insert(entites, [{"id": 1, "code": "FUT", "nom": nom, "couleur": "#213E70", "principale": True,
                              "actif": True, "created_at": maintenant, "updated_at": maintenant}])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SELECT setval(pg_get_serial_sequence('entites', 'id'), 1)")

    op.create_table(
        'acces_entites',
        sa.Column('utilisateur_id', sa.Integer(), nullable=False),
        sa.Column('entite_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.CheckConstraint("role IN ('direction','collaborateur')", name='ck_acces_role'),
        sa.ForeignKeyConstraint(['entite_id'], ['entites.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['utilisateur_id'], ['utilisateurs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('utilisateur_id', 'entite_id'),
    )

    _rattacher('departements', ['code', 'nom'],
               [("uq_departements_entite_code", ['entite_id', 'code']),
                ("uq_departements_entite_nom", ['entite_id', 'nom'])])
    _rattacher('projets', ['code'], [("uq_projets_entite_code", ['entite_id', 'code'])])
    _rattacher('criteres', ['libelle'], [("uq_criteres_entite_libelle", ['entite_id', 'libelle'])])
    _rattacher('postes', ['libelle'], [("uq_postes_entite_libelle", ['entite_id', 'libelle'])], fk_ondelete="CASCADE")
    _rattacher('employes', [], [("uq_employes_entite_matricule", ['entite_id', 'matricule'])],
               index_unique_a_retirer="ix_employes_matricule")
    with op.batch_alter_table('imports_brouillons', schema=None) as b:
        b.add_column(sa.Column('entite_id', sa.Integer(), nullable=True))
        b.create_foreign_key("fk_imports_brouillons_entite", 'entites', ['entite_id'], ['id'], ondelete='CASCADE')


def downgrade():
    with op.batch_alter_table('imports_brouillons', schema=None) as b:
        b.drop_constraint("fk_imports_brouillons_entite", type_="foreignkey")
        b.drop_column('entite_id')
    for table, uqs, anciennes in (
            ('employes', ["uq_employes_entite_matricule"], []),
            ('postes', ["uq_postes_entite_libelle"], ['libelle']),
            ('criteres', ["uq_criteres_entite_libelle"], ['libelle']),
            ('projets', ["uq_projets_entite_code"], ['code']),
            ('departements', ["uq_departements_entite_code", "uq_departements_entite_nom"], ['code', 'nom'])):
        with op.batch_alter_table(table, schema=None) as b:
            for u in uqs:
                b.drop_constraint(u, type_="unique")
            b.drop_index(f"ix_{table}_entite_id")
            b.drop_constraint(f"fk_{table}_entite", type_="foreignkey")
            b.drop_column('entite_id')
            for col in anciennes:
                b.create_unique_constraint(_nom_uq(table, col), [col])
            if table == 'employes':
                b.drop_index("ix_employes_matricule")
                b.create_index("ix_employes_matricule", ['matricule'], unique=True)
    op.drop_table('acces_entites')
    op.drop_table('entites')
