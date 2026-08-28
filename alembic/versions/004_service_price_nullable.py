"""Service.price → nullable (убираем цену из FSM создания услуги).

revision: 004_service_price_nullable
down_revision: 003_fsm_storage
created: 2026-08-28

Причина (Session 5.9): пользователь решил убрать шаг цены из FSM добавления
услуги. Екатерина (мастер) озвучивает цену отдельно в чате, клиенту через
бота цена не показывается (grep client.py по price пусто). Шаг в FSM — лишний.

Migration: price → nullable=True ( reversible, цена остаётся в БД как
опциональное поле — если в будущем Екатерина захочет показывать прайс
через бота, поле уже есть).

Downgrade: BACK to NOT NULL. Если есть NULL значения — сначала update на 0
(defense, чтобы не упасть на ALTER).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "004_service_price_nullable"
down_revision = "003_fsm_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER COLUMN price nullable=True.
    # SQLite не умеет ALTER COLUMN directly — нужен batch_alter_table (creates
    # temp table, copies data, drops old, renames). Postgres умеет ALTER COLUMN
    # напрямую, но batch_alter_table работает и для Postgres (просто медленнее).
    # Используем batch для cross-DB совместимости (тесты на SQLite, prod на Postgres).
    with op.batch_alter_table("services") as batch_op:
        batch_op.alter_column(
            "price",
            existing_type=sa.Numeric(10, 2),
            nullable=True,
        )


def downgrade() -> None:
    # Defense: если есть NULL values — обновим на 0 ПЕРЕД ALTER NOT NULL.
    op.execute("UPDATE services SET price = 0 WHERE price IS NULL")
    with op.batch_alter_table("services") as batch_op:
        batch_op.alter_column(
            "price",
            existing_type=sa.Numeric(10, 2),
            nullable=False,
        )
