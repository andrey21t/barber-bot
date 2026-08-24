"""FSM state persistence — fsm_states table (spec.md:538-547 PostgresStorage).

revision: 003_fsm_storage
down_revision: 002_postgres_exclude
created: 2026-08-23

Cross-DB:
- Postgres: JSONB column for atomic `data || $new::jsonb` merge in update_data
- SQLite: JSON column (read-modify-write fallback, single-process tests)

PK = (bot_id, chat_id, user_id, destiny) — composite, matches aiogram StorageKey
(base.py:14-21). thread_id/business_connection_id excluded (NULL for simple chats).

Session 4 deep-analysis decisions:
- Race condition: Postgres JSONB merge is atomic (no SimpleEventIsolation needed)
- Engine sharing: storage uses shared async_session_factory from bot.db
- close() no-op — engine disposed in main.py:_on_shutdown
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "003_fsm_storage"
down_revision = "002_postgres_exclude"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_table(
            "fsm_states",
            sa.Column("bot_id", sa.BigInteger, primary_key=True),
            sa.Column("chat_id", sa.BigInteger, primary_key=True),
            sa.Column("user_id", sa.BigInteger, primary_key=True),
            sa.Column("destiny", sa.String(50), primary_key=True, server_default="default"),
            sa.Column("state", sa.String(255), nullable=True),
            sa.Column("data", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column(
                "updated_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.Index("idx_fsm_states_chat_user", "chat_id", "user_id"),
        )
    else:
        # SQLite — JSON column (read-modify-write fallback)
        op.create_table(
            "fsm_states",
            sa.Column("bot_id", sa.BigInteger, primary_key=True),
            sa.Column("chat_id", sa.BigInteger, primary_key=True),
            sa.Column("user_id", sa.BigInteger, primary_key=True),
            sa.Column("destiny", sa.String(50), primary_key=True, server_default="default"),
            sa.Column("state", sa.String(255), nullable=True),
            sa.Column("data", sa.JSON, nullable=False, server_default=sa.text("'{}'")),
            # DateTime(timezone=True) для cross-DB консистентности с models.py:234
            # (Postgres path использует sa.TIMESTAMP(timezone=True) на line 45)
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Index("idx_fsm_states_chat_user", "chat_id", "user_id"),
        )


def downgrade() -> None:
    op.drop_table("fsm_states")
