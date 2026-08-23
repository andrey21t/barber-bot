"""Postgres-only EXCLUDE constraint for no-double-booking (tstzrange overlap).

revision: 002_postgres_exclude
down_revision: 001_initial
created: 2026-08-23

Cross-DB:
- Postgres: CREATE EXTENSION btree_gist + EXCLUDE USING gist (tstzrange)
  (VERIFIED btree_gist available on Render Postgres 13+ —
   https://render.com/docs/postgresql-extensions)
- SQLite: no-op (UNIQUE(slot_id) from 001_initial handles same-slot double-booking;
  SQLite не имеет EXCLUDE USING gist)

WHERE clause: status IN ('confirmed', 'transferred') — адаптировано под codebase:
- booking INSERT goes 'confirmed' (booking.py:183)
- UPDATE to 'transferred' (booking.py:571)
- UPDATE to 'cancelled' (booking.py:345) — 'cancelled' excluded из WHERE
  (отмена освобождает слот для новых overlap'ов)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "002_postgres_exclude"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        # VERIFIED btree_gist available on Render Postgres 13+
        # https://render.com/docs/postgresql-extensions
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        op.execute(
            sa.text("""
                ALTER TABLE bookings ADD CONSTRAINT no_overlap
                EXCLUDE USING gist (
                    master_id WITH =,
                    tstzrange(start_at, end_at) WITH &&
                ) WHERE (status IN ('confirmed', 'transferred'))
            """)
        )
    # SQLite — no-op (UNIQUE(slot_id) из 001 handles same-slot)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS no_overlap")
