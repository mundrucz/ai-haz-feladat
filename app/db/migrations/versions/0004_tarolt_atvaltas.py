"""A kimutatás pénznemére váltás eredménye a számla sorában (árfolyam, összeg, kimaradás oka) és a sor revíziója.

A meglévő számlák átváltása üres marad: a következő kimutatás-lekérdezés egyszer elvégzi.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-26 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ('revision', sa.Integer(), {'server_default': '0', 'nullable': False}),
    ('report_currency', sa.String(), {'nullable': True}),
    ('rate', sa.String(), {'nullable': True}),
    ('rate_unit', sa.Integer(), {'nullable': True}),
    ('rate_date', sa.Date(), {'nullable': True}),
    ('report_amount', sa.Integer(), {'nullable': True}),
    ('report_problem', sa.String(), {'nullable': True}),
    ('report_exclusion', sa.String(), {'nullable': True}),
)


def upgrade() -> None:
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        for name, type_, options in _COLUMNS:
            batch_op.add_column(sa.Column(name, type_, **options))
        batch_op.create_index(batch_op.f('ix_invoices_report_currency'), ['report_currency'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_invoices_report_currency'))
        for name, _, _ in reversed(_COLUMNS):
            batch_op.drop_column(name)
