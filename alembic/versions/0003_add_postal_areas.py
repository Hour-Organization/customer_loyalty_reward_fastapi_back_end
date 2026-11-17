"""add postal areas

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Cambodia Post's provinces, districts and communes. `python -m app.cli import-postal-codes` fills them."""
    op.create_table('provinces',
    sa.Column('code', sa.String(length=6), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('name_kh', sa.String(length=255), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code')
    )
    op.create_table('districts',
    sa.Column('province_id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=6), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('name_kh', sa.String(length=255), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['province_id'], ['provinces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code')
    )
    with op.batch_alter_table('districts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_districts_province_id'), ['province_id'], unique=False)

    op.create_table('communes',
    sa.Column('district_id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=6), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('name_kh', sa.String(length=255), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['district_id'], ['districts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code')
    )
    with op.batch_alter_table('communes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_communes_district_id'), ['district_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('communes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_communes_district_id'))
    op.drop_table('communes')
    with op.batch_alter_table('districts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_districts_province_id'))
    op.drop_table('districts')
    op.drop_table('provinces')
