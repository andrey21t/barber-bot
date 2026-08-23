"""Tests for PostgresStorage — FSM state persistence across bot restarts.

Coverage:
- 5 abstract methods of aiogram BaseStorage (set_state, get_state, set_data,
  get_data, close)
- update_data atomic merge (Postgres JSONB `||` on prod, read-modify-write on
  SQLite dev)
- Composite PK (bot_id, chat_id, user_id, destiny) — different keys isolated
- set_state(None) clears state but preserves data
- get_data on missing key returns {} (not None, not error)
- set_data with non-dict raises DataNotDictLikeError
- Cross-session persistence: state survives session_factory close (mimics
  bot restart — same engine, new session)

Tests run on SQLite (in-memory, conftest.py:40) — PostgresStorage uses
SQLite JSON path (read-modify-write). On Postgres (smoke against Render)
JSONB merge path would be exercised. Cross-DB logic tested via dialect
branching in fsm_storage.py.
"""

import pytest
import pytest_asyncio
from aiogram.exceptions import DataNotDictLikeError
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import DEFAULT_DESTINY, StorageKey
from bot.fsm_storage import PostgresStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture
def key() -> StorageKey:
    """Default StorageKey for tests (bot_id=1, chat_id=2, user_id=3)."""
    return StorageKey(bot_id=1, chat_id=2, user_id=3, destiny=DEFAULT_DESTINY)


@pytest.fixture
def another_key() -> StorageKey:
    """Different StorageKey for isolation tests."""
    return StorageKey(bot_id=1, chat_id=99, user_id=99, destiny=DEFAULT_DESTINY)


@pytest_asyncio.fixture
async def storage(
    session_factory: async_sessionmaker[AsyncSession],
) -> PostgresStorage:
    """Fresh PostgresStorage per test (uses shared session_factory from conftest)."""
    return PostgresStorage(session_factory)


@pytest.mark.asyncio
async def test_set_and_get_state(
    storage: PostgresStorage,
    key: StorageKey,
) -> None:
    """set_state stores state string, get_state returns it."""
    await storage.set_state(key, "BookingStates:selecting_date")
    assert await storage.get_state(key) == "BookingStates:selecting_date"


@pytest.mark.asyncio
async def test_set_state_with_state_object(
    storage: PostgresStorage,
    key: StorageKey,
) -> None:
    """set_state accepts aiogram State object (extracts .state string).

    State("foo").state returns "@:foo" (aiogram's internal representation
    with group prefix). PostgresStorage stores exactly that string, get_state
    returns it as-is. MemoryStorage has the same contract (memory.py:45).
    """
    state = State("BookingStates:entering_name")
    await storage.set_state(key, state)
    assert await storage.get_state(key) == state.state


@pytest.mark.asyncio
async def test_set_state_none_clears_state(
    storage: PostgresStorage,
    key: StorageKey,
) -> None:
    """set_state(None) clears state but preserves data (MemoryStorage contract)."""
    await storage.set_state(key, "BookingStates:entering_name")
    await storage.set_data(key, {"slot_id": "abc-123"})
    await storage.set_state(key, None)
    assert await storage.get_state(key) is None
    assert await storage.get_data(key) == {"slot_id": "abc-123"}


@pytest.mark.asyncio
async def test_get_state_missing_key_returns_none(
    storage: PostgresStorage,
    key: StorageKey,
) -> None:
    """get_state on missing key returns None (no row)."""
    assert await storage.get_state(key) is None


@pytest.mark.asyncio
async def test_set_and_get_data(
    storage: PostgresStorage,
    key: StorageKey,
) -> None:
    """set_data replaces data, get_data returns copy."""
    await storage.set_data(key, {"name": "Иван", "phone": "+79991234567"})
    data = await storage.get_data(key)
    assert data == {"name": "Иван", "phone": "+79991234567"}
    # Returned dict is a copy — mutating doesn't affect storage
    data["name"] = "changed"
    assert (await storage.get_data(key))["name"] == "Иван"


@pytest.mark.asyncio
async def test_get_data_missing_key_returns_empty_dict(
    storage: PostgresStorage,
    key: StorageKey,
) -> None:
    """get_data on missing key returns {} (MemoryStorage contract — defaultdict)."""
    data = await storage.get_data(key)
    assert data == {}


@pytest.mark.asyncio
async def test_set_data_replaces_not_merges(
    storage: PostgresStorage,
    key: StorageKey,
) -> None:
    """set_data replaces entire dict, not merge (MemoryStorage contract)."""
    await storage.set_data(key, {"a": 1, "b": 2})
    await storage.set_data(key, {"c": 3})
    assert await storage.get_data(key) == {"c": 3}


@pytest.mark.asyncio
async def test_set_data_non_dict_raises(
    storage: PostgresStorage,
    key: StorageKey,
) -> None:
    """set_data with non-dict raises DataNotDictLikeError (MemoryStorage contract)."""
    with pytest.raises(DataNotDictLikeError):
        await storage.set_data(key, "not a dict")  # type: ignore[arg-type]
    with pytest.raises(DataNotDictLikeError):
        await storage.set_data(key, ["list", "not", "dict"])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_update_data_non_dict_raises(
    storage: PostgresStorage,
    key: StorageKey,
) -> None:
    """update_data with non-Mapping raises DataNotDictLikeError (W2 fix).

    MemoryStorage.set_data contract: non-dict → DataNotDictLikeError.
    PostgresStorage.update_data must follow the same contract (previously
    raised TypeError from dict(non-dict-pairs), now raises DataNotDictLikeError).
    """
    with pytest.raises(DataNotDictLikeError):
        await storage.update_data(key, "not a mapping")  # type: ignore[arg-type]
    with pytest.raises(DataNotDictLikeError):
        await storage.update_data(key, ["list", "not", "mapping"])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_update_data_merges_partial(
    storage: PostgresStorage,
    key: StorageKey,
) -> None:
    """update_data merges partial data into existing dict."""
    await storage.set_data(key, {"name": "Иван"})
    result = await storage.update_data(key, {"phone": "+7999"})
    assert result == {"name": "Иван", "phone": "+7999"}
    assert await storage.get_data(key) == {"name": "Иван", "phone": "+7999"}


@pytest.mark.asyncio
async def test_update_data_missing_key_creates_row(
    storage: PostgresStorage,
    key: StorageKey,
) -> None:
    """update_data on missing key creates row with merged data."""
    result = await storage.update_data(key, {"slot_id": "abc-123"})
    assert result == {"slot_id": "abc-123"}
    assert await storage.get_data(key) == {"slot_id": "abc-123"}


@pytest.mark.asyncio
async def test_keys_are_isolated(
    storage: PostgresStorage,
    key: StorageKey,
    another_key: StorageKey,
) -> None:
    """State for one key does not affect another (composite PK)."""
    await storage.set_state(key, "BookingStates:selecting_date")
    await storage.set_data(key, {"slot": "first"})
    await storage.set_state(another_key, "BookingStates:entering_name")
    await storage.set_data(another_key, {"slot": "second"})

    assert await storage.get_state(key) == "BookingStates:selecting_date"
    assert await storage.get_data(key) == {"slot": "first"}
    assert await storage.get_state(another_key) == "BookingStates:entering_name"
    assert await storage.get_data(another_key) == {"slot": "second"}


@pytest.mark.asyncio
async def test_close_is_noop(storage: PostgresStorage) -> None:
    """close() is no-op — engine shared with handlers, disposed in main.py."""
    # Should not raise, should not dispose engine
    await storage.close()
    # Verify storage still works after close
    key = StorageKey(bot_id=1, chat_id=1, user_id=1)
    await storage.set_state(key, "test")
    assert await storage.get_state(key) == "test"


@pytest.mark.asyncio
async def test_persistence_across_sessions(
    session_factory: async_sessionmaker[AsyncSession],
    key: StorageKey,
) -> None:
    """State persists across session_factory close (mimics bot restart).

    New PostgresStorage instance with same session_factory should see state
    written by previous instance — this is the whole point of PostgresStorage
    (survives Render free tier restarts).
    """
    storage1 = PostgresStorage(session_factory)
    await storage1.set_state(key, "BookingStates:entering_name")
    await storage1.set_data(key, {"slot_id": "abc-123"})

    # Simulate bot restart — new storage instance, same session_factory
    storage2 = PostgresStorage(session_factory)
    assert await storage2.get_state(key) == "BookingStates:entering_name"
    assert await storage2.get_data(key) == {"slot_id": "abc-123"}


@pytest.mark.asyncio
async def test_update_data_returns_new_data(
    storage: PostgresStorage,
    key: StorageKey,
) -> None:
    """update_data returns new merged data (base.py:184 contract)."""
    await storage.set_data(key, {"a": 1})
    result = await storage.update_data(key, {"b": 2})
    assert result == {"a": 1, "b": 2}
    # Returned data is a copy — mutating doesn't affect storage
    result["c"] = 3
    assert "c" not in await storage.get_data(key)


# ============================================================
# Storage value types — JSON-serializable only
# ============================================================


@pytest.mark.asyncio
async def test_storage_handles_iso_datetime_string(
    storage: PostgresStorage,
    key: StorageKey,
) -> None:
    """ISO datetime string (session_timeout.py:61 pattern) is JSON-serializable."""
    iso_str = "2026-08-23T14:30:00+00:00"
    await storage.set_data(key, {"last_message_at": iso_str})
    assert await storage.get_data(key) == {"last_message_at": iso_str}


@pytest.mark.asyncio
async def test_storage_handles_uuid_as_string(
    storage: PostgresStorage,
    key: StorageKey,
) -> None:
    """UUID as string (client.py:239,698 pattern) is JSON-serializable.

    Existing handlers already str() the UUID — no custom encoder needed.
    This test documents the contract: storage does NOT auto-convert UUID.
    """
    uuid_str = "550e8400-e29b-41d4-a716-446655440000"
    await storage.update_data(key, {"slot_id": uuid_str})
    assert await storage.get_data(key) == {"slot_id": uuid_str}
