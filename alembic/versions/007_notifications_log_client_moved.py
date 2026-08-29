"""Add 'client_moved' to notifications_log.ck_notifications_kind

Revision ID: 007_notif_log_client_moved
Revises: 006_booking_slot_nullable
Create Date: 2026-08-29

Этап 5.9 (/movslot + admin_move_booking). Новый kind 'client_moved' — audit
log для admin-initiated booking move (уведомление КЛИЕНТУ, не мастеру).
Существующие kinds: remind_24h | remind_1h | master_new | master_cancel |
master_transfer. 'client_moved' — для admin_move_booking (Booking.status
остаётся 'transferred', но audit log отличает admin action от client
transfer через 'master_transfer').

Cross-DB strategy (mirror migration 006):
- Postgres: ALTER TABLE ... DROP CONSTRAINT ck_notifications_kind + ALTER TABLE
  ... ADD CONSTRAINT ck_notifications_kind CHECK (kind IN (... + 'client_moved')).
- SQLite: batch_alter_table (не умеет ALTER COLUMN напрямую, copy-rewrite через
  temp table). SQLAlchemy 2.0 handles cross-DB CHECK constraint drop+recreate.

NB: PLANS.md originally reserved 007 for "drop table slots" (Plan of Work п.13,
Session 5.20 finding E). Renamed to 008 (drop table slots, after smoke-test 007
on prod ~1 неделя). 007 = NotificationLog CHECK now — data-preserving, safe to
deploy independent of 008. PLANS.md docstrings updated in this commit.

NB2 (fix 2026-08-29): revision_id укорочен с "007_notifications_log_client_moved"
(34 chars, не влезал в alembic_version.version_num VARCHAR(32) —
StringDataRightTruncation на prod deploy) до "007_notif_log_client_moved"
(26 chars). Конвенция: все revision_ids ≤32 chars (mirror 001-006).

Downgrade is one-way door (mirror migration 006:97):
- DELETE FROM notifications_log WHERE kind='client_moved' — removes audit logs
  of admin_move actions (pet-project single-tenant, data loss acceptable для
  rollback scenario).
- Then re-add old CHECK constraint (without 'client_moved').
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "007_notif_log_client_moved"
down_revision = "006_booking_slot_nullable"
branch_labels = None
depends_on = None


# Old + new CHECK constraint definitions (Postgres + SQLite via batch_alter).
_OLD_CHECK = (
    "kind IN ('remind_24h','remind_1h','master_new','master_cancel','master_transfer')"
)
_NEW_CHECK = (
    "kind IN ("
    "'remind_24h','remind_1h','master_new','master_cancel','master_transfer',"
    "'client_moved'"
    ")"
)
_CONSTRAINT_NAME = "ck_notifications_kind"


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        # Postgres: direct DROP + ADD CONSTRAINT.
        op.execute(
            f"ALTER TABLE notifications_log DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}"
        )
        op.create_check_constraint(
            _CONSTRAINT_NAME,
            "notifications_log",
            _NEW_CHECK,
        )
    else:
        # SQLite: batch_alter_table (copy-rewrite через temp table).
        # SQLAlchemy handles CHECK constraint drop+recreate within batch.
        with op.batch_alter_table("notifications_log") as batch_op:
            batch_op.drop_constraint(_CONSTRAINT_NAME, type_="check")
            batch_op.create_check_constraint(_CONSTRAINT_NAME, _NEW_CHECK)


def downgrade() -> None:
    # One-way door: rows with kind='client_moved' violate re-added old CHECK.
    # DELETE them first (pet-project single-tenant, audit log data loss
    # acceptable для rollback scenario — admin_move actions lost from log,
    # bookings themselves remain with status='transferred').
    op.execute("DELETE FROM notifications_log WHERE kind = 'client_moved'")

    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.execute(
            f"ALTER TABLE notifications_log DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}"
        )
        op.create_check_constraint(
            _CONSTRAINT_NAME,
            "notifications_log",
            _OLD_CHECK,
        )
    else:
        with op.batch_alter_table("notifications_log") as batch_op:
            batch_op.drop_constraint(_CONSTRAINT_NAME, type_="check")
            batch_op.create_check_constraint(_CONSTRAINT_NAME, _OLD_CHECK)
