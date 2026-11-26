"""A jövőbeli teljesítésű tárolt számlák `future_performance` jelölést kapnak (nincs hozzájuk árfolyam).

A jelölés a státuszt nem változtatja; a számla kimarad a kimutatásból, amíg az újrafeltöltés le nem veszi.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-26 18:00:00
"""

import json
from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODE = "future_performance"
_ISSUE = {"code": _CODE, "field": "performance_date", "severity": "info"}


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, issues FROM invoices WHERE performance_date > :today"),
        {"today": date.today().isoformat()},
    ).all()
    for row_id, raw_issues in rows:
        issues = json.loads(raw_issues)
        if any(issue.get("code") == _CODE for issue in issues):
            continue
        bind.execute(
            sa.text("UPDATE invoices SET issues = :issues WHERE id = :id"),
            {"issues": json.dumps([*issues, _ISSUE]), "id": row_id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, issues FROM invoices WHERE issues LIKE :pattern"), {"pattern": f'%"{_CODE}"%'}
    ).all()
    for row_id, raw_issues in rows:
        issues = [issue for issue in json.loads(raw_issues) if issue.get("code") != _CODE]
        bind.execute(sa.text("UPDATE invoices SET issues = :issues WHERE id = :id"), {"issues": json.dumps(issues), "id": row_id})
