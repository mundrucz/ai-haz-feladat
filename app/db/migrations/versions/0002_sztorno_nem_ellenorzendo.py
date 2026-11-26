"""A sztornó (negatív végösszeg) nem ellenőrzendő: a régi `non_positive_total` figyelmeztetés átírása.

Negatív bruttónál a figyelmeztetés törlődik (ha nem marad más, a számla `ok` lesz), nulla bruttónál `zero_total` lesz.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-26 14:40:00
"""

import json
from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_CODE = "non_positive_total"
_ZERO_CODE = "zero_total"


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, gross_total, issues FROM invoices WHERE issues LIKE :pattern"),
        {"pattern": f'%"{_OLD_CODE}"%'},
    ).all()
    for row_id, gross_total, raw_issues in rows:
        issues = json.loads(raw_issues)
        if Decimal(gross_total) < 0:
            issues = [issue for issue in issues if issue.get("code") != _OLD_CODE]
        else:
            issues = [{**issue, "code": _ZERO_CODE} if issue.get("code") == _OLD_CODE else issue for issue in issues]
        bind.execute(
            sa.text("UPDATE invoices SET issues = :issues, status = :status WHERE id = :id"),
            {"issues": json.dumps(issues), "status": "needs_review" if issues else "ok", "id": row_id},
        )


def downgrade() -> None:
    # Az `ok`-ra váltott sztornók figyelmeztetése nem állítható vissza; csak a kód átnevezése fordítható vissza.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, issues FROM invoices WHERE issues LIKE :pattern"), {"pattern": f'%"{_ZERO_CODE}"%'}
    ).all()
    for row_id, raw_issues in rows:
        issues = [{**i, "code": _OLD_CODE} if i.get("code") == _ZERO_CODE else i for i in json.loads(raw_issues)]
        bind.execute(sa.text("UPDATE invoices SET issues = :issues WHERE id = :id"), {"issues": json.dumps(issues), "id": row_id})
