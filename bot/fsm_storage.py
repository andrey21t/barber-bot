"""PostgresStorage for aiogram FSM — persistent state across bot restarts.

Spec.md:538-547 — Render free tier restarts every 15 min → MemoryStorage loses
FSM state → user stuck mid-flow with "Что вы хотели сделать?" confusion.

Cross-DB design (deep-analysis Session 4 + code-review iter 1 fixes):
- Postgres (prod): JSONB column → atomic `data || $new::jsonb` merge in
  update_data. INSERT path uses ON CONFLICT DO NOTHING (W1 fix) to avoid
  composite-PK IntegrityError on concurrent UPDATE→None→INSERT race.
- SQLite (dev/test): JSON column → update_data falls back to read-modify-write
  (base.py default impl). Single-process test runner → race never triggered.

Race condition mitigation (code-review iter 1 W1+W4 fixes):
- Postgres JSONB `||` merge is atomic (UPDATE-UPDATE race covered).
- INSERT path after UPDATE→None uses ON CONFLICT DO NOTHING.
- main.py:99 explicitly passes events_isolation=SimpleEventIsolation() to
  Dispatcher (aiogram default is DisabledEventIsolation — verified).
  SimpleEventIsolation locks per StorageKey (per-chat) → serializes
  update_data calls for the same user (defensive for any remaining races).

Contract: aiogram BaseStorage (aiogram/fsm/storage/base.py:102-191)
5 abstract methods: set_state, get_state, set_data, get_data, close.
update_data (concrete, base.py:173-184) — read-modify-write. We override it
on Postgres for atomic JSONB merge.
set_data and update_data raise DataNotDictLikeError on non-Mapping input
(W2 fix — consistent with MemoryStorage.set_data contract).

updated_at: bumped explicitly via func.now() in Core constructs (W3 fix —
SQLAlchemy onupdate is ORM-only, doesn't trigger on pg_insert/update Core).

Engine sharing: storage uses shared async_session_factory from bot.db:22.
close() is no-op (engine disposed in main.py:_on_shutdown via bot.session.close).
"""

from collections.abc import Mapping
from typing import Any

from aiogram.exceptions import DataNotDictLikeError
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import (
    BaseStorage,
    StateType,
    StorageKey,
)
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import func

from bot.models import FsmState


class PostgresStorage(BaseStorage):
    """FSM storage backed by Postgres/SQLite via SQLAlchemy.

    Args:
        session_factory: shared async_sessionmaker (from bot.db:22).
            Storage does NOT own the engine → close() is no-op.

    Usage (bot/main.py):
        dp = Dispatcher(storage=PostgresStorage(async_session_factory))

    Note: For SQLite (dev) use MemoryStorage instead — see bot/main.py switch.
    PostgresStorage works on SQLite (cross-DB SQLAlchemy), but MemoryStorage is
    simpler for dev (no DB writes for FSM state).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def close(self) -> None:
        """No-op — engine is shared with handlers, disposed in main.py shutdown."""
        pass

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        state_value = state.state if isinstance(state, State) else state
        async with self._session_factory() as session:
            await self._upsert_state(session, key, state_value)
            await session.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._session_factory() as session:
            stmt = select(FsmState.state).where(self._where_clause(key))
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        if not isinstance(data, dict):
            msg = f"Data must be a dict or dict-like object, got {type(data).__name__}"
            raise DataNotDictLikeError(msg)
        async with self._session_factory() as session:
            await self._upsert_data(session, key, dict(data))
            await session.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._session_factory() as session:
            stmt = select(FsmState.data).where(self._where_clause(key))
            result = await session.execute(stmt)
            data = result.scalar_one_or_none()
            return dict(data) if data is not None else {}

    async def update_data(self, key: StorageKey, data: Mapping[str, Any]) -> dict[str, Any]:
        """Atomic merge on Postgres (JSONB `||`), read-modify-write on SQLite.

        Postgres: `UPDATE ... SET data = data || $new::jsonb RETURNING data`
        SQLite: SELECT data → merge in Python → UPDATE (base.py default impl).
        Single-process test runner means SQLite race is not triggered in tests.

        Contract (W2 fix): raise DataNotDictLikeError for non-Mapping input
        (consistent with MemoryStorage.set_data, base.py contract for
        FSMContext.update_data callers).
        """
        if not isinstance(data, Mapping):
            msg = f"Data must be a dict or dict-like object, got {type(data).__name__}"
            raise DataNotDictLikeError(msg)
        data_dict = dict(data)
        async with self._session_factory() as session:
            # Try Postgres JSONB merge first (dialect-specific)
            bind = session.get_bind()
            if bind.dialect.name == "postgresql":
                stmt = (
                    update(FsmState)
                    .where(self._where_clause(key))
                    .values(
                        data=FsmState.data.op("||")(data_dict),
                        updated_at=func.now(),
                    )
                    .returning(FsmState.data)
                )
                result = await session.execute(stmt)
                merged = result.scalar_one_or_none()
                if merged is None:
                    # Row doesn't exist → INSERT with ON CONFLICT DO NOTHING
                    # (W1 fix — race: two concurrent UPDATE→None → both INSERT,
                    # second would IntegrityError on composite PK conflict).
                    await self._insert_if_absent(session, key, data_dict)
                    await session.commit()
                    return data_dict.copy()
                await session.commit()
                return dict(merged)
            # SQLite fallback: read-modify-write
            select_stmt = select(FsmState.data).where(self._where_clause(key))
            current = (await session.execute(select_stmt)).scalar_one_or_none()
            current_dict = dict(current) if current is not None else {}
            current_dict.update(data_dict)
            await self._upsert_data(session, key, current_dict)
            await session.commit()
            return current_dict.copy()

    async def _upsert_state(
        self,
        session: AsyncSession,
        key: StorageKey,
        state_value: str | None,
    ) -> None:
        """UPSERT state column, preserving existing data.

        Postgres: ON CONFLICT DO UPDATE (with updated_at bump for W3 fix —
        Core constructs don't trigger ORM onupdate).
        SQLite: INSERT OR REPLACE (preserves data column via COALESCE).
        """
        where = self._where_clause(key)
        existing = (await session.execute(select(FsmState.data).where(where))).scalar_one_or_none()
        data_value = existing if existing is not None else {}
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            stmt = (
                pg_insert(FsmState)
                .values(
                    bot_id=key.bot_id,
                    chat_id=key.chat_id,
                    user_id=key.user_id,
                    destiny=key.destiny,
                    state=state_value,
                    data=data_value,
                )
                .on_conflict_do_update(
                    index_elements=["bot_id", "chat_id", "user_id", "destiny"],
                    set_={
                        "state": state_value,
                        "updated_at": func.now(),
                    },
                )
            )
            await session.execute(stmt)
        else:
            # SQLite — manual UPSERT via SELECT then INSERT/UPDATE
            existing_state = (
                await session.execute(select(FsmState).where(where))
            ).scalar_one_or_none()
            if existing_state is None:
                session.add(
                    FsmState(
                        bot_id=key.bot_id,
                        chat_id=key.chat_id,
                        user_id=key.user_id,
                        destiny=key.destiny,
                        state=state_value,
                        data={},
                    )
                )
            else:
                existing_state.state = state_value

    async def _upsert_data(
        self,
        session: AsyncSession,
        key: StorageKey,
        data: dict[str, Any],
    ) -> None:
        """UPSERT data column, preserving existing state.

        Postgres: ON CONFLICT DO UPDATE (with updated_at bump for W3 fix).
        SQLite: SELECT then INSERT/UPDATE.
        """
        where = self._where_clause(key)
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            existing_state = (
                await session.execute(select(FsmState.state).where(where))
            ).scalar_one_or_none()
            stmt = (
                pg_insert(FsmState)
                .values(
                    bot_id=key.bot_id,
                    chat_id=key.chat_id,
                    user_id=key.user_id,
                    destiny=key.destiny,
                    state=existing_state,
                    data=data,
                )
                .on_conflict_do_update(
                    index_elements=["bot_id", "chat_id", "user_id", "destiny"],
                    set_={
                        "data": data,
                        "updated_at": func.now(),
                    },
                )
            )
            await session.execute(stmt)
        else:
            existing = (await session.execute(select(FsmState).where(where))).scalar_one_or_none()
            if existing is None:
                session.add(
                    FsmState(
                        bot_id=key.bot_id,
                        chat_id=key.chat_id,
                        user_id=key.user_id,
                        destiny=key.destiny,
                        state=None,
                        data=data,
                    )
                )
            else:
                existing.data = data

    async def _insert_if_absent(
        self,
        session: AsyncSession,
        key: StorageKey,
        data: dict[str, Any],
    ) -> None:
        """INSERT with ON CONFLICT DO NOTHING (W1 fix — race in update_data INSERT path).

        Two concurrent UPDATE→None → both call _upsert_data which uses
        ON CONFLICT DO UPDATE — that's fine (one wins, the other becomes UPDATE).
        BUT update_data Postgres path calls _insert_if_absent (not _upsert_data)
        because it already knows the row is missing from UPDATE→None — and
        uses ON CONFLICT DO NOTHING to avoid IntegrityError on composite PK.

        Postgres: INSERT ... ON CONFLICT DO NOTHING.
        SQLite: INSERT OR IGNORE.
        """
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            stmt = (
                pg_insert(FsmState)
                .values(
                    bot_id=key.bot_id,
                    chat_id=key.chat_id,
                    user_id=key.user_id,
                    destiny=key.destiny,
                    state=None,
                    data=data,
                )
                .on_conflict_do_nothing(
                    index_elements=["bot_id", "chat_id", "user_id", "destiny"],
                )
            )
            await session.execute(stmt)
        else:
            # SQLite — manual check + insert (race-free in single-process tests)
            where = self._where_clause(key)
            existing = (await session.execute(select(FsmState).where(where))).scalar_one_or_none()
            if existing is None:
                session.add(
                    FsmState(
                        bot_id=key.bot_id,
                        chat_id=key.chat_id,
                        user_id=key.user_id,
                        destiny=key.destiny,
                        state=None,
                        data=data,
                    )
                )

    @staticmethod
    def _where_clause(key: StorageKey) -> Any:
        """Composite WHERE for StorageKey (PK columns)."""
        return (
            (FsmState.bot_id == key.bot_id)
            & (FsmState.chat_id == key.chat_id)
            & (FsmState.user_id == key.user_id)
            & (FsmState.destiny == key.destiny)
        )
