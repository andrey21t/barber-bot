"""Add Client.telegram_username column (nullable).

Revision ID: 009_client_telegram_username
Revises: 008_status_completed_no_show
Create Date: 2026-09-13

Фича: @username клиента в списках записей admin (cmd_today, cmd_week).
Раньше @username не сохранялся в БД (читался из callback.from_user.username
живьём только для notification "Новая запись"). Теперь сохраняем в
Client.telegram_username — для рендера в списках без Bot API get_chat().

Nullable: существующие clients получают NULL, обновится при следующем
booking (no backfill — без Bot API get_chat() per client = overkill).

Downgrade is one-way door (mirror migration 007:30-33):
- DROP COLUMN telegram_username — теряет @username данные.
  Pet-project single-tenant (Екатерина, ~10-30 clients), data loss acceptable
  для rollback scenario. @username восстановится при следующих bookings.
"""

from alembic import op
import sqlalchemy as sa


revision = "009_client_telegram_username"
down_revision = "008_status_completed_no_show"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # op.add_column works on both Postgres and SQLite (3.35+).
    # No batch_alter needed — ADD COLUMN nullable is simple operation.
    op.add_column(
        "clients",
        sa.Column("telegram_username", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    # One-way door: теряет @username данные. Pet-project single-tenant,
    # data loss acceptable (восстановится при следующих bookings).
    # Mirror 007:30-33 precedent for one-way door documentation.
    # NB: op.drop_column needs SQLite >= 3.35 (DROP COLUMN added in 3.35.0).
    # macOS ships 3.39+, prod uses Postgres → safe in our stack.
    op.drop_column("clients", "telegram_username")
