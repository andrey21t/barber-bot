"""Add 'admin_completed','admin_no_show' to ck_notifications_kind + add ck_booking_status.

Revision ID: 008_status_completed_no_show
Revises: 007_notif_log_client_moved
Create Date: 2026-09-12

B.3 — 4 статуса booking. Adds 2 new terminal statuses 'completed' and
'no_show' to the booking lifecycle, with admin UI transitions from
'confirmed'/'transferred' (existing active statuses).

Two CHECK constraint changes:

1. Extend ``ck_notifications_kind`` to 8 kinds (add 'admin_completed',
   'admin_no_show' for the new ``transition_booking_status`` audit log).
   Mirrors migration 007 pattern (DROP + ADD on Postgres, batch_alter_table
   on SQLite).

2. Add new ``ck_booking_status`` on ``bookings`` table (5 statuses: confirmed,
   cancelled, completed, no_show, transferred). Currently no CHECK exists —
   status column is ``String(20)`` with default='confirmed' but no DB-level
   enforcement. This closes the gap (INV-2 in deep-analysis Pass 3).

Cross-DB strategy (mirror migrations 006 + 007):
- Postgres: direct DROP + ADD CONSTRAINT (notifications_log), direct ADD
  CONSTRAINT (bookings — new constraint).
- SQLite: batch_alter_table (copy-rewrite via temp table).

NB: ``conftest.py:42-44`` uses ``Base.metadata.create_all`` (NOT alembic)
in tests — so ``models.py`` ``__table_args__`` CHECK constraints are enforced
in tests, alembic constraints in prod only. To keep dev/prod parity
(deep-analysis iter 3 GAP-NEW-2 finding), this migration AND ``models.py``
must add the same ``ck_booking_status`` constraint in parallel. Same for
the extended ``ck_notifications_kind``.

Downgrade is one-way door (mirror 007:82):
- DELETE FROM notifications_log WHERE kind IN ('admin_completed','admin_no_show')
  — pet-project single-tenant, audit log loss acceptable для rollback.
- Revert ck_notifications_kind to pre-008 (6 kinds WITH 'client_moved' —
  mirrors 007's _NEW_CHECK).
- DROP ck_booking_status (no re-add — pre-008 state = no constraint).
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "008_status_completed_no_show"
down_revision = "007_notif_log_client_moved"
branch_labels = None
depends_on = None


# Post-008 ck_notifications_kind — explicitly ALL 8 kinds (deep-analysis iter 3
# GAP-F/G: must spell client_moved too, otherwise upgrade fails on existing
# admin_move rows).
_NEW_NOTIF_CHECK = (
    "kind IN ("
    "'remind_24h','remind_1h','master_new','master_cancel','master_transfer',"
    "'client_moved','admin_completed','admin_no_show'"
    ")"
)
# Pre-008 ck_notifications_kind — equal to 007's _NEW_CHECK (6 kinds WITH
# client_moved). Downgrade reverts to this.
_OLD_NOTIF_CHECK = (
    "kind IN ("
    "'remind_24h','remind_1h','master_new','master_cancel','master_transfer',"
    "'client_moved'"
    ")"
)
_NOTIF_CONSTRAINT_NAME = "ck_notifications_kind"

# New ck_booking_status — ALL 5 statuses (existing 3 + 2 new terminal).
_BOOKING_STATUS_CHECK = (
    "status IN ('confirmed','cancelled','completed','no_show','transferred')"
)
_BOOKING_STATUS_NAME = "ck_booking_status"


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1. Extend ck_notifications_kind (mirror 007 pattern).
    if dialect == "postgresql":
        op.execute(
            f"ALTER TABLE notifications_log DROP CONSTRAINT IF EXISTS {_NOTIF_CONSTRAINT_NAME}"
        )
        op.create_check_constraint(
            _NOTIF_CONSTRAINT_NAME,
            "notifications_log",
            _NEW_NOTIF_CHECK,
        )
    else:
        # SQLite: batch_alter_table (copy-rewrite via temp table).
        with op.batch_alter_table("notifications_log") as batch_op:
            batch_op.drop_constraint(_NOTIF_CONSTRAINT_NAME, type_="check")
            batch_op.create_check_constraint(_NOTIF_CONSTRAINT_NAME, _NEW_NOTIF_CHECK)

    # 2. Add ck_booking_status (new constraint — no prior constraint to drop).
    if dialect == "postgresql":
        op.create_check_constraint(
            _BOOKING_STATUS_NAME,
            "bookings",
            _BOOKING_STATUS_CHECK,
        )
    else:
        with op.batch_alter_table("bookings") as batch_op:
            batch_op.create_check_constraint(_BOOKING_STATUS_NAME, _BOOKING_STATUS_CHECK)


def downgrade() -> None:
    # One-way door: rows with new kinds violate re-added old CHECK. DELETE
    # first (pet-project single-tenant, audit log data loss acceptable для
    # rollback scenario — admin complete/no_show actions lost from log,
    # bookings themselves remain with status='completed'/'no_show', but
    # ck_booking_status is dropped next so they're not constrained).
    op.execute(
        "DELETE FROM notifications_log WHERE kind IN ('admin_completed', 'admin_no_show')"
    )

    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1. Revert ck_notifications_kind to pre-008 (6 kinds WITH client_moved).
    if dialect == "postgresql":
        op.execute(
            f"ALTER TABLE notifications_log DROP CONSTRAINT IF EXISTS {_NOTIF_CONSTRAINT_NAME}"
        )
        op.create_check_constraint(
            _NOTIF_CONSTRAINT_NAME,
            "notifications_log",
            _OLD_NOTIF_CHECK,
        )
    else:
        with op.batch_alter_table("notifications_log") as batch_op:
            batch_op.drop_constraint(_NOTIF_CONSTRAINT_NAME, type_="check")
            batch_op.create_check_constraint(_NOTIF_CONSTRAINT_NAME, _OLD_NOTIF_CHECK)

    # 2. Drop ck_booking_status (no re-add — pre-008 state = no constraint).
    if dialect == "postgresql":
        op.execute(f"ALTER TABLE bookings DROP CONSTRAINT IF EXISTS {_BOOKING_STATUS_NAME}")
    else:
        with op.batch_alter_table("bookings") as batch_op:
            batch_op.drop_constraint(_BOOKING_STATUS_NAME, type_="check")
