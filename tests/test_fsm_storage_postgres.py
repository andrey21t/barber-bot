"""Postgres path SQL construction tests for PostgresStorage.

Purpose (S1 from code-review, Finding 7 coverage):
Тесты в test_fsm_storage.py идут на SQLite (conftest.py:40). Все
`if self._dialect_name == "postgresql"` branches в fsm_storage.py НЕ
выполняются в тех тестах. Этот файл покрывает Postgres branches через
SQL construction unit-testing:

1. Instantiate PostgresStorage with explicit `dialect_name="postgresql"`
   (no real Postgres connection needed — we only compile SQL strings).
2. Mock session.execute to intercept the SQLAlchemy statement.
3. Compile statement with postgresql dialect to get actual SQL.
4. Assert SQL contains expected Postgres-specific constructs (ON CONFLICT,
   excluded, JSONB ||, RETURNING).

What we DO catch:
- Wrong API usage (e.g. insert_stmt.excluded.data)
- Missing ON CONFLICT clause
- Missing RETURNING in update_data
- JSONB merge operator `||` correctly placed
- Single-statement count (no extra SELECT for _upsert_state after Finding 6)

What we DON'T catch (Honest limitation, needs testcontainers or Render smoke):
- Runtime JSONB `||` semantics (shallow merge, new wins on conflict)
- ON CONFLICT target matching (PK index lookup)
- RETURNING row actually populated by Postgres
- Race conditions under real concurrent transactions
"""

from __future__ import annotations

import re
from contextlib import AbstractAsyncContextManager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.storage.base import DEFAULT_DESTINY, StorageKey
from bot.fsm_storage import PostgresStorage
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession


def _compile_sql(stmt: Any) -> str:
    """Compile SQLAlchemy statement to Postgres SQL string for assertions.

    Without literal_binds (JSON values have no literal renderer — CompileError).
    Placeholders (%s) are fine — we assert on SQL keywords, not bind values.
    """
    return str(stmt.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]


def _set_columns(sql: str) -> set[str]:
    """Extract column names from `DO UPDATE SET col1 = ..., col2 = ...` clause.

    Returns set of column names that are explicitly assigned in SET.
    Used to verify which columns a statement modifies — robust against column
    ordering and whitespace variations (F2 fix: replaces substring checks that
    were tautologically True because of `.replace(" ", "")` removing the space
    in `"set data"` search string, and because `data` is unquoted non-reserved
    word so `"set\\"data\\""` never matches).

    Example:
        `INSERT INTO t ... DO UPDATE SET state = %s, updated_at = now() RETURNING data`
        → {"state", "updated_at"}
    """
    set_match = re.search(
        r"DO UPDATE SET\s+(.+?)(?:\s+RETURNING\s+.+|$)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    if set_match is None:
        return set()
    set_clause = set_match.group(1)
    # Split on commas at top level (paren-aware to avoid splitting inside `(a || b)`)
    columns: set[str] = set()
    depth = 0
    current = ""
    for char in set_clause:
        if char == "(":
            depth += 1
            current += char
        elif char == ")":
            depth -= 1
            current += char
        elif char == "," and depth == 0:
            columns.add(_first_token(current))
            current = ""
        else:
            current += char
    if current.strip():
        columns.add(_first_token(current))
    return columns


def _first_token(assignment: str) -> str:
    """Extract column name from `col = expr` (or `table.col = expr`) assignment."""
    m = re.match(r"\s*(?:[\w]+\.)?(\w+)\s*=", assignment)
    return m.group(1) if m else ""


class _FakeSessionFactory:
    """Fake async_sessionmaker that yields mock session.

    Mocks `async with session_factory() as session:` pattern. Returns the
    same mock_session every call so tests can inspect call_args on it.

    `kw` attribute mimics async_sessionmaker.kw (dict with 'bind' key) —
    used by PostgresStorage.__init__ to infer dialect. Defaults to {} so
    tests requiring no-bind raise path can override.
    """

    def __init__(self, mock_session: AsyncMock) -> None:
        self._mock_session = mock_session
        self.kw: dict[str, Any] = {}

    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]:
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=self._mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=None)
        return mock_cm


@pytest.fixture
def key() -> StorageKey:
    return StorageKey(bot_id=1, chat_id=2, user_id=3, destiny=DEFAULT_DESTINY)


@pytest.fixture
def mock_session() -> AsyncMock:
    """Mock AsyncSession — execute() returns Mock result with scalar helpers."""
    session = AsyncMock()
    # execute returns object with .scalar_one(), .scalar_one_or_none(), .scalars()
    result = MagicMock()
    result.scalar_one.return_value = {"mock": "merged"}
    result.scalar_one_or_none.return_value = None  # for SELECT-state in _upsert_data
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result
    session.commit = AsyncMock()
    return session


@pytest.fixture
def pg_storage(mock_session: AsyncMock) -> PostgresStorage:
    """PostgresStorage wired to fake session_factory with postgresql dialect.

    No real Postgres connection — tests inspect SQL construction only.
    """
    fake_factory = _FakeSessionFactory(mock_session)
    return PostgresStorage(fake_factory, dialect_name="postgresql")  # type: ignore[arg-type]


# ============================================================
# Init / dialect inference
# ============================================================


def test_init_raises_when_no_bind_and_no_dialect() -> None:
    """Without bind engine AND explicit dialect_name, raise ValueError."""
    fake_factory = _FakeSessionFactory(AsyncMock())
    # kw={} by default — no bind key (mimics broken session_factory)
    with pytest.raises(ValueError, match="Cannot infer dialect"):
        PostgresStorage(fake_factory, dialect_name=None)  # type: ignore[arg-type]


def test_init_explicit_dialect_skips_bind_lookup() -> None:
    """Explicit dialect_name bypasses session_factory.kw["bind"] lookup."""
    fake_factory = _FakeSessionFactory(AsyncMock())
    # kw={} by default — broken session_factory, but explicit dialect wins
    storage = PostgresStorage(fake_factory, dialect_name="postgresql")  # type: ignore[arg-type]
    assert storage._dialect_name == "postgresql"


# ============================================================
# update_data Postgres — Finding 7 single atomic JSONB merge
# ============================================================


@pytest.mark.asyncio
async def test_update_data_postgres_uses_single_atomic_statement(
    pg_storage: PostgresStorage,
    mock_session: AsyncMock,
    key: StorageKey,
) -> None:
    """update_data Postgres = ONE execute call (no SELECT before pg_insert).

    Finding 7: replaced 2-step UPDATE→None→INSERT ON CONFLICT DO NOTHING with
    single atomic pg_insert.on_conflict_do_update. Asserts only one execute
    call (the old code had 1 UPDATE + potentially 1 INSERT = 2).
    """
    await pg_storage.update_data(key, {"new": "data"})

    assert mock_session.execute.await_count == 1
    assert mock_session.commit.await_count == 1


@pytest.mark.asyncio
async def test_update_data_postgres_sql_contains_on_conflict_do_update(
    pg_storage: PostgresStorage,
    mock_session: AsyncMock,
    key: StorageKey,
) -> None:
    """update_data Postgres SQL contains ON CONFLICT ... DO UPDATE for composite PK."""
    await pg_storage.update_data(key, {"new": "data"})

    stmt = mock_session.execute.call_args[0][0]
    sql = _compile_sql(stmt)
    assert "ON CONFLICT" in sql
    assert "DO UPDATE" in sql
    # Composite PK columns in conflict target
    assert "bot_id" in sql
    assert "chat_id" in sql
    assert "user_id" in sql
    assert "destiny" in sql


@pytest.mark.asyncio
async def test_update_data_postgres_sql_uses_jsonb_merge_via_excluded(
    pg_storage: PostgresStorage,
    mock_session: AsyncMock,
    key: StorageKey,
) -> None:
    """update_data Postgres SQL merges existing JSONB with new via `|| excluded.data`.

    Critical check (Finding 7): SQL must contain `fsm_states.data || excluded.data`
    (or equivalent string form) — atomic JSONB merge in single statement with
    correct ORDER of operands:
    - existing.data first → new keys overwrite existing on conflict (correct)
    - excluded.data first → existing overwrites new (BUG — user's new data lost)

    W3 fix: assertions verify the full merge expression as a substring, not
    independent `excluded` + `||` + `data` substrings that pass even when the
    operand order is wrong (e.g. `excluded.data || fsm_states.data`).
    """
    await pg_storage.update_data(key, {"new": "data"})

    stmt = mock_session.execute.call_args[0][0]
    sql = _compile_sql(stmt)
    sql_compact = " ".join(sql.split())  # normalize whitespace
    # Verify exact merge expression: existing.data || excluded.data (correct order)
    assert "fsm_states.data || excluded.data" in sql_compact, (
        f"Expected JSONB merge `fsm_states.data || excluded.data` "
        f"(existing first, new overwrites), got: {sql_compact}"
    )


@pytest.mark.asyncio
async def test_update_data_postgres_sql_has_returning(
    pg_storage: PostgresStorage,
    mock_session: AsyncMock,
    key: StorageKey,
) -> None:
    """update_data Postgres SQL has RETURNING clause for atomic result."""
    await pg_storage.update_data(key, {"new": "data"})

    stmt = mock_session.execute.call_args[0][0]
    sql = _compile_sql(stmt)
    assert "RETURNING" in sql.upper()


@pytest.mark.asyncio
async def test_update_data_postgres_returns_merged_dict(
    pg_storage: PostgresStorage,
    mock_session: AsyncMock,
    key: StorageKey,
) -> None:
    """update_data returns dict copy of merged result (not reference to storage).

    F1 fix: removed `or True` that made assertion tautologically True (no-op).
    Real assertion: mutate result, second update_data call should NOT contain
    the mutation — proves dict() defensive copy in fsm_storage.py:174.
    """
    result = await pg_storage.update_data(key, {"new": "data"})

    assert isinstance(result, dict)
    # mock_session.execute.return_value.scalar_one returns {"mock": "merged"}
    assert result == {"mock": "merged"}
    # Mutating result must not affect next call (defensive copy via dict())
    result["extra"] = "value"
    second_result = await pg_storage.update_data(key, {"x": 1})
    assert "extra" not in second_result


# ============================================================
# set_state Postgres — Finding 6 no SELECT before pg_insert
# ============================================================


@pytest.mark.asyncio
async def test_set_state_postgres_uses_single_statement_no_select(
    pg_storage: PostgresStorage,
    mock_session: AsyncMock,
    key: StorageKey,
) -> None:
    """set_state Postgres = ONE execute call (no SELECT data before pg_insert).

    Finding 6: removed redundant SELECT data before pg_insert.on_conflict_do_update.
    PostgreSQL preserves columns NOT in SET clause — data column untouched on
    UPDATE. INSERT new row uses data={} (server_default).
    """
    await pg_storage.set_state(key, "BookingStates:entering_name")

    assert mock_session.execute.await_count == 1
    assert mock_session.commit.await_count == 1


@pytest.mark.asyncio
async def test_set_state_postgres_sql_contains_on_conflict_do_update(
    pg_storage: PostgresStorage,
    mock_session: AsyncMock,
    key: StorageKey,
) -> None:
    """set_state Postgres SQL uses ON CONFLICT DO UPDATE on composite PK."""
    await pg_storage.set_state(key, "BookingStates:entering_name")

    stmt = mock_session.execute.call_args[0][0]
    sql = _compile_sql(stmt)
    assert "ON CONFLICT" in sql
    assert "DO UPDATE" in sql


@pytest.mark.asyncio
async def test_set_state_postgres_sql_updates_only_state_and_updated_at(
    pg_storage: PostgresStorage,
    mock_session: AsyncMock,
    key: StorageKey,
) -> None:
    """set_state Postgres SQL SET clause contains only state + updated_at (not data).

    Critical (Finding 6): data column must NOT be in SET — preserves existing
    data on UPDATE. INSERT path uses data={} (server_default).

    F2 fix: replaced tautologically-True substring checks (`"set data"` with
    space searched in string where spaces were removed; `'set"data"'` with
    quotes for unquoted column `data`) with `_set_columns` regex parser that
    extracts actual column names from SET clause regardless of order/whitespace.
    """
    await pg_storage.set_state(key, "BookingStates:entering_name")

    stmt = mock_session.execute.call_args[0][0]
    sql = _compile_sql(stmt)
    set_cols = _set_columns(sql)

    # state IS in SET (it's the column we want to update)
    assert "state" in set_cols, f"Expected state in SET, got: {set_cols}"
    # data MUST NOT be in SET (preserves existing data column — Finding 6)
    assert "data" not in set_cols, (
        f"data column in SET — Finding 6 regression (data should be preserved): {set_cols}"
    )
    # updated_at IS in SET (explicit bump, ORM onupdate doesn't fire on Core)
    assert "updated_at" in set_cols, f"Expected updated_at in SET, got: {set_cols}"


# ============================================================
# set_data Postgres — _upsert_data has SELECT state + pg_insert
# ============================================================


@pytest.mark.asyncio
async def test_set_data_postgres_uses_select_then_pg_insert(
    pg_storage: PostgresStorage,
    mock_session: AsyncMock,
    key: StorageKey,
) -> None:
    """set_data Postgres = TWO execute calls: SELECT state + pg_insert.

    _upsert_data Postgres path: SELECT existing state → INSERT with that
    state value (preserved on UPDATE via not-in-SET). 2 queries — pre-existing
    observation (not changed by Findings 4-7 refactor). Verified count = 2.
    """
    await pg_storage.set_data(key, {"k": "v"})

    assert mock_session.execute.await_count == 2
    assert mock_session.commit.await_count == 1
    # First execute = SELECT (state lookup)
    first_stmt = mock_session.execute.call_args_list[0][0][0]
    first_sql = _compile_sql(first_stmt)
    assert "SELECT" in first_sql.upper()
    # Second execute = INSERT ... ON CONFLICT
    second_stmt = mock_session.execute.call_args_list[1][0][0]
    second_sql = _compile_sql(second_stmt)
    assert "ON CONFLICT" in second_sql
    assert "DO UPDATE" in second_sql


@pytest.mark.asyncio
async def test_set_data_postgres_sql_updates_only_data_and_updated_at(
    pg_storage: PostgresStorage,
    mock_session: AsyncMock,
    key: StorageKey,
) -> None:
    """set_data Postgres SQL SET clause contains only data + updated_at (not state).

    W2 fix: replaced partial substring check (`"setstate"` only caught state
    as FIRST column) with `_set_columns` regex parser — catches state in SET
    regardless of column position.
    """
    await pg_storage.set_data(key, {"k": "v"})

    # Second execute is the pg_insert.on_conflict_do_update
    stmt = mock_session.execute.call_args_list[1][0][0]
    sql = _compile_sql(stmt)
    assert "ON CONFLICT" in sql
    assert "DO UPDATE" in sql
    set_cols = _set_columns(sql)
    # data IS in SET (it's the column we want to replace)
    assert "data" in set_cols, f"Expected data in SET, got: {set_cols}"
    # state MUST NOT be in SET (preserves existing state column)
    assert "state" not in set_cols, (
        f"state column in SET — set_data should not touch state: {set_cols}"
    )
    # updated_at IS in SET (explicit bump, ORM onupdate doesn't fire on Core)
    assert "updated_at" in set_cols, f"Expected updated_at in SET, got: {set_cols}"


# ============================================================
# get_state / get_data Postgres — generic SELECT
# ============================================================


@pytest.mark.asyncio
async def test_get_state_postgres_executes_select(
    pg_storage: PostgresStorage,
    mock_session: AsyncMock,
    key: StorageKey,
) -> None:
    """get_state Postgres = single SELECT on FsmState.state."""
    await pg_storage.get_state(key)

    assert mock_session.execute.await_count == 1
    stmt = mock_session.execute.call_args[0][0]
    sql = _compile_sql(stmt)
    assert "SELECT" in sql.upper()
    assert "FROM" in sql.upper()


@pytest.mark.asyncio
async def test_get_data_postgres_executes_select(
    pg_storage: PostgresStorage,
    mock_session: AsyncMock,
    key: StorageKey,
) -> None:
    """get_data Postgres = single SELECT on FsmState.data."""
    await pg_storage.get_data(key)

    assert mock_session.execute.await_count == 1
    stmt = mock_session.execute.call_args[0][0]
    sql = _compile_sql(stmt)
    assert "SELECT" in sql.upper()


# ============================================================
# close — no-op (covered already on SQLite, explicit Postgres check)
# ============================================================


@pytest.mark.asyncio
async def test_close_postgres_is_noop(pg_storage: PostgresStorage) -> None:
    """close() is no-op — engine shared with handlers, disposed in main.py."""
    # Should not raise, should not call any session method
    await pg_storage.close()
