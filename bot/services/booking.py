"""Booking creation — Pure service layer.

Contract (MY-VIBE-RULES.md, spec.md 307-309):
- Timezone conversion: slot.slot_hour LOCAL → start_at UTC
- HTML escape: client_name_snapshot + service_title_snapshot — escape in service BEFORE INSERT
- UNIQUE(slot_id) — SQLite fallback for no-double-booking
- SQLite race protection: UPDATE slot ... WHERE status='open' + rowcount check
- WorkDay invariants (Этап 5.3, PLANS.md Gap 6): if WorkDay exists for (master_id, slot_date),
  booking [start_at, end_at] must fit inside [workday.start_time, end_time] in business tz → UTC.
  WorkDay lookup is OPTIONAL — slot-only data (legacy /addslots before /openday rollout in 5.1)
  has no WorkDay → invariant skipped (backwards compat, no BookingOutsideWorkDayError).
"""

import html
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from scheduler import remove_jobs_for_booking, schedule_for_booking
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.models import Booking, Client, NotificationLog, Service, Slot, WorkDay
from bot.schemas import BookingCreate


class SlotAlreadyBookedError(Exception):
    """UNIQUE(slot_id) violated — someone booked this slot concurrently."""


class SlotInPastError(Exception):
    """slot.start_at <= now() — booking in the past is not allowed."""


class SlotClosedError(Exception):
    """slot.status != 'open' — slot was closed by master."""


class BookingOutsideWorkDayError(Exception):
    """Booking [start_at, end_at] exceeds WorkDay window [start_time, end_time] for the date.

    Raised by create_booking (Этап 5.3) and transfer_booking (Этап 5.5) when WorkDay
    exists for (master_id, work_date) AND the requested booking range falls outside
    the workday window. Comparison in UTC:
    combine(work_date, workday.start_time/end_time, business_tz).astimezone(UTC).

    Skipped (no raise) when WorkDay not found — backwards compat for slot-only data
    created via legacy /addslots before /openday rollout (Этап 5.1).
    """


class WorkDayCapacityExceededError(Exception):
    """Multi-client capacity overflow — too many overlapping active bookings.

    Raised by create_booking and transfer_booking (Этап 5.5) when WorkDay exists for
    (master_id, work_date) AND count of overlapping active bookings
    (status IN ('confirmed','transferred') WHERE Booking.start_at < new_end_at AND
    Booking.end_at > new_start_at) >= WorkDay.max_concurrent_clients.

    Half-open overlap (start_at < new_end_at AND end_at > new_start_at) — touching
    ranges [14:00,15:00]+[15:00,16:00] do NOT overlap (boundary inclusive on start,
    exclusive on end). Equivalently tstzrange && on Postgres, but cross-DB SQL
    expression avoids dialect branching (works on SQLite + Postgres).

    Race protection: _acquire_advisory_lock (pg_advisory_xact_lock on Postgres,
    no-op on SQLite) is called BEFORE _check_multi_client_capacity to serialize
    concurrent INSERTs on same (master_id, work_date). SQLite serializes via
    DB-level lock — race tests skipif SQLite because advisory lock semantics are
    Postgres-only.
    """


@dataclass(frozen=True)
class BookingCreatedData:
    """Result of create_booking — passed to handler for Telegram I/O.

    Этап 5.8a: slot_id теперь Optional — None для WorkDay-only bookings
    (30-min slot generated from WorkDay window, no Slot row in DB).
    """

    booking_id: UUID
    slot_id: UUID | None
    master_id: UUID
    business_id: UUID
    client_id: UUID
    start_at: datetime
    end_at: datetime
    client_name_snapshot: str  # already escaped
    service_title_snapshot: str  # already escaped
    master_notification_text: str


async def _select_open_slot(session: AsyncSession, slot_id: UUID) -> Slot:
    stmt = select(Slot).where(Slot.id == slot_id)
    result = await session.execute(stmt)
    slot = result.scalar_one_or_none()
    if slot is None:
        raise SlotClosedError(f"Slot {slot_id} not found")
    if slot.status == "booked":
        raise SlotAlreadyBookedError(f"Slot {slot_id} already booked")
    if slot.status == "closed":
        raise SlotClosedError(f"Slot {slot_id} closed by master")
    return slot


async def _select_business_timezone(session: AsyncSession, business_id: UUID) -> str:
    from bot.models import Business

    stmt = select(Business.timezone).where(Business.id == business_id)
    result = await session.execute(stmt)
    tz = result.scalar_one_or_none()
    return tz or "Europe/Moscow"


async def _select_workday_for_slot(
    session: AsyncSession, master_id: UUID, work_date: date
) -> WorkDay | None:
    """Lookup WorkDay for (master_id, work_date). Returns None if not found.

    Этап 5.3 (PLANS.md Gap 6 "Invariants"): create_booking uses this to enforce
    booking [start_at, end_at] ∈ [workday.start_time, end_time]. Skipped when
    WorkDay is None — backwards compat for slot-only data (legacy /addslots before
    /openday in 5.1).

    No filter on is_active — even inactive WorkDay bounds the window (closed day =
    no slots shown in 5.6, but a stale booking request still must fit the window
    if a WorkDay record exists). This matches the "1 booking = 1 client" model
    where WorkDay is the master's declared work window for that date.
    """
    stmt = select(WorkDay).where(WorkDay.master_id == master_id, WorkDay.work_date == work_date)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _validate_booking_within_workday(
    workday: WorkDay,
    start_at: datetime,
    end_at: datetime,
    business_tz: str,
) -> None:
    """Raise BookingOutsideWorkDayError if [start_at, end_at] exceeds [start_time, end_time].

    Comparison in UTC: combine(work_date, workday.start_time/end_time, business_tz)
    .astimezone(UTC). start_at and end_at are aware UTC, built in-memory by
    _build_start_at / _build_end_at (NOT read from DB — no SQLite naive/aware concern
    at this call site). Cross-DB aware/naive handling lives in cancel_booking /
    transfer_booking where booking.start_at IS DB-read (sqlite stores naive).

    Edge: workday.end_time <= workday.start_time is blocked by CheckConstraint
    "end_time > start_time" — no defensive check needed here.

    Operators are strict (<, >): boundary inclusive — start_at == workday.start_time
    and end_at == workday.end_time are ACCEPTED (booking fills the entire window
    from start to end is a valid case). Boundary tests in test_booking_invariants.py
    lock the semantics so a regression to <=/>= is caught.
    """
    tz = ZoneInfo(business_tz)
    workday_start_utc = datetime.combine(
        workday.work_date, workday.start_time, tzinfo=tz
    ).astimezone(UTC)
    workday_end_utc = datetime.combine(workday.work_date, workday.end_time, tzinfo=tz).astimezone(
        UTC
    )
    if start_at < workday_start_utc:
        raise BookingOutsideWorkDayError(
            f"Booking start_at={start_at} < workday start {workday_start_utc} "
            f"(workday_id={workday.id}, work_date={workday.work_date})"
        )
    if end_at > workday_end_utc:
        raise BookingOutsideWorkDayError(
            f"Booking end_at={end_at} > workday end {workday_end_utc} "
            f"(workday_id={workday.id}, work_date={workday.work_date})"
        )


async def _check_multi_client_capacity(
    session: AsyncSession,
    *,
    workday_id: UUID,
    capacity: int,
    master_id: UUID,
    start_at: datetime,
    end_at: datetime,
    excluded_booking_id: UUID | None = None,
) -> None:
    """Raise WorkDayCapacityExceededError if overlapping active bookings >= capacity.

    Half-open overlap (Этап 5.5, PLANS.md Gap 4 occupancy definition):
        SELECT count(*) FROM bookings
        WHERE master_id = :master_id
          AND start_at < :new_end_at   -- touching ranges: end == next start → no overlap
          AND end_at   > :new_start_at
          AND status IN ('confirmed', 'transferred')
          AND id != :excluded_booking_id  -- optional: skip self (same-date transfer)

    Cross-DB SQL expression (no tstzrange && operator) — works on SQLite + Postgres
    without dialect branching. On Postgres this is functionally equivalent to
    tstzrange && (verified by SQL semantics: a < c AND b > d for ranges [a,b) [c,d)
    overlap ⟺ a < d AND b > c).

    Args:
        workday_id: WorkDay.id (for error message — caller captured BEFORE any
            rollback-prone operation per B1 pattern, since WorkDay instance may
            expire after session.rollback() in _select_or_create_client).
        capacity: WorkDay.max_concurrent_clients — captured by caller BEFORE any
            rollback-prone operation (B1 pattern, see create_booking:~340).
        master_id: master whose bookings to count (also captured before rollback).
        start_at, end_at: new booking range (aware UTC) — overlap window.
        excluded_booking_id: optional Booking.id to exclude from overlap count.
            Used by transfer_booking to skip the booking being transferred (its
            own OLD range overlaps with NEW range on same-date transfer, but it's
            "moving itself" — should not count). None (default) for create_booking
            (new booking has no id yet → no self to exclude).

    Note: capacity check is conservative upper-bound — count overlap >= capacity
    rejects the N+1th concurrent booking. Pet-project acceptable; documented in
    PLANS.md 5.5 known limitations.
    """
    overlap_stmt = (
        select(func.count())
        .select_from(Booking)
        .where(
            Booking.master_id == master_id,
            Booking.start_at < end_at,
            Booking.end_at > start_at,
            Booking.status.in_(("confirmed", "transferred")),
        )
    )
    if excluded_booking_id is not None:
        overlap_stmt = overlap_stmt.where(Booking.id != excluded_booking_id)
    result = await session.execute(overlap_stmt)
    overlap_count = result.scalar_one()
    if overlap_count >= capacity:
        raise WorkDayCapacityExceededError(
            f"WorkDay {workday_id} capacity {capacity} exceeded: "
            f"{overlap_count} overlapping bookings for master {master_id} "
            f"in [{start_at}, {end_at})"
        )


async def _acquire_advisory_lock(session: AsyncSession, master_id: UUID, work_date: date) -> None:
    """Postgres-only pg_advisory_xact_lock for serializable multi-client race.

    SQLite: no-op (file-based SQLite serializes writes via DB-level lock; in-memory
    SQLite — single connection — no concurrent race). Race tests skipif SQLite
    because advisory lock semantics are Postgres-only.

    Key: hashtext(master_id::text) + work_date.toordinal() — deterministic int pair.
    pg_advisory_xact_lock is transaction-scoped (auto-release on commit/rollback),
    so callers MUST hold an open transaction (create_booking / transfer_booking do
    this implicitly via session.add + commit pattern).

    Dialect check via `session.bind.dialect.name` — same pattern as
    fsm_storage.py:95 (`bind.dialect.name`).
    """
    engine = session.bind
    if engine is None or engine.dialect.name != "postgresql":
        return
    stmt = text("SELECT pg_advisory_xact_lock(hashtext(:master_id::text), :work_date_ordinal)")
    await session.execute(
        stmt,
        {
            "master_id": str(master_id),
            "work_date_ordinal": work_date.toordinal(),
        },
    )


def _build_start_at(slot: Slot, business_timezone: str) -> datetime:
    """Convert slot (LOCAL hour in business.timezone) to UTC datetime.

    DEPRECATED (Этап 5.4): kept for backwards compat with slot-based /book flow
    (create_booking / transfer_booking through BookingCreate.slot_id) until 5.8
    introduces /slots command + WorkDay-based /book. New code should use
    `_build_start_at_from_workday` which builds from WorkDay.start_time + offset
    (30-min step) instead of slot.slot_hour (int 0-23).

    Slot.slot_hour is a 0-23 LOCAL hour — does NOT support minutes. For 30-min
    step (PLANS.md Gap 1, Blocker C), use _build_start_at_from_workday which
    accepts a `time` (HH:MM) and combines with work_date.
    """
    tz = ZoneInfo(business_timezone)
    local_dt = datetime.combine(slot.slot_date, time(hour=slot.slot_hour), tzinfo=tz)
    return local_dt.astimezone(UTC)


def _build_start_at_from_workday(
    workday: WorkDay, start_time_local: time, business_timezone: str
) -> datetime:
    """Build UTC datetime from WorkDay.work_date + arbitrary LOCAL start time.

    Этап 5.4 (PLANS.md Gap 1, Blocker C): 30-мин step at /slots UI generates
    buttons T, T+30, T+60, ... from workday.start_time to end_time. /book creates
    a booking on a 30-мин window — start_at = T (LOCAL HH:MM), end_at = T +
    service.duration_minutes (may be > 30 min). This helper combines the selected
    LOCAL time with workday.work_date → UTC datetime.

    Args:
        workday: WorkDay record — provides work_date (the calendar day).
        start_time_local: LOCAL start time (HH:MM) — generated by /slots UI from
            workday.start_time + N*30min steps (see get_30min_slots_from_workday).
        business_timezone: IANA tz name (e.g. "Europe/Moscow") — LOCAL → UTC
            conversion uses this tz.

    Returns:
        Aware UTC datetime — Booking.start_at value.

    Invariant (Этап 5.3): caller's _validate_booking_within_workday checks that
    [start_at, end_at] ⊆ [workday.start_time, end_time] in UTC. This helper only
    builds start_at — end_at is built separately by _build_end_at using
    service.duration_minutes.

    NB: no check here that start_time_local ∈ [workday.start_time, end_time] —
    /slots UI already filters, and _validate_booking_within_workday is the
    authoritative invariant enforcer.
    """
    tz = ZoneInfo(business_timezone)
    local_dt = datetime.combine(workday.work_date, start_time_local, tzinfo=tz)
    return local_dt.astimezone(UTC)


def _build_end_at(
    start_at: datetime, service: Service | None, default_duration_min: int
) -> datetime:
    duration = service.duration_minutes if service is not None else default_duration_min
    return start_at + timedelta(minutes=duration)


async def _select_service(session: AsyncSession, service_id: UUID | None) -> Service | None:
    if service_id is None:
        return None
    stmt = select(Service).where(Service.id == service_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _select_or_create_client(session: AsyncSession, telegram_id: int) -> Client:
    stmt = select(Client).where(Client.telegram_id == telegram_id)
    result = await session.execute(stmt)
    client = result.scalar_one_or_none()
    if client is not None:
        return client
    client = Client(telegram_id=telegram_id)
    session.add(client)
    try:
        await session.flush()  # populate id
    except IntegrityError:
        # Race: another concurrent request created this client between our
        # SELECT and INSERT. UNIQUE(telegram_id) — see bot/models.py:84.
        # Rollback here is safe in terms of data (no uncommitted writes to lose
        # — slot was SELECT'd read-only, booking not yet added), BUT it expires
        # all attached instances (including slot from create_booking:145).
        # Callers MUST capture any slot attributes they need BEFORE calling
        # this function — see create_booking:152 (B1 fix). Accessing slot.id
        # after this rollback would trigger MissingGreenlet (sync lazy load on
        # async session).
        await session.rollback()
        result = await session.execute(stmt)
        client = result.scalar_one()
    return client


async def create_booking(
    session: AsyncSession,
    payload: BookingCreate,
    *,
    business_id: UUID,
    master_id: UUID,
    telegram_id: int,
) -> BookingCreatedData:
    """Create booking in a transaction.

    Этап 5.8a branching (миграция 006 — Booking.slot_id nullable):
      - **Legacy slot path** (`payload.slot_id` set): existing flow from /book
        — Slot row in DB, _select_open_slot, _build_start_at(slot, business_tz),
        UPDATE slot.status='booked' (rowcount check for SQLite race).
      - **WorkDay path** (`payload.workday_id` + `payload.start_time_local` set,
        `payload.slot_id` None): new flow from /slots (5.8b — BookSlot30CallbackData).
        Slot row НЕ создаётся — 30-min slot generated from WorkDay window on
        /slots UI. start_at via _build_start_at_from_workday(workday,
        start_time_local: time, business_tz). Booking.slot_id = NULL.

    Pydantic validator (BookingCreate._validate_slot_xor_workday) guarantees
    exactly one path is set — XOR contract enforced on schema side, not here.

    Steps (spec.md 192-198, both paths share):
      1. Resolve path → slot (legacy SELECT) OR workday (WorkDay get)
      2. If legacy: slot.status != 'open' → SlotClosedError / SlotAlreadyBookedError
         If workday: WorkDay None or inactive → BookingOutsideWorkDayError
      3. Build start_at (UTC):
         - Legacy: slot.slot_hour (LOCAL) → UTC via business_tz
         - WorkDay: workday.work_date + start_time_local (LOCAL HH:MM) → UTC
      4. If start_at <= now() → SlotInPastError
      5. Build end_at = start_at + service.duration_minutes (or default)
      6. WorkDay invariant (PLANS.md Gap 6):
         - Legacy path: SELECT WorkDay for (slot.master_id, slot.slot_date);
           if found: validate [start_at, end_at] ∈ [workday.start_time, end_time]
           (compared in UTC via business_tz). Outside → BookingOutsideWorkDayError.
           If not found: skip (backwards compat, slot-only data via legacy /addslots).
         - WorkDay path: WorkDay already resolved — validate directly. MANDATORY
           (workday path is the new contract, no skip case).
      7. html.escape(client_name, service_title) BEFORE INSERT
      8. INSERT booking — legacy path: UNIQUE(slot_id) dropped (миграция 006),
         guard shift на rowcount-check (booking.py:482-487) + multi-client
         capacity check (booking.py:451). WorkDay path: no UNIQUE guard.
      9. UPDATE slot.status='booked' WHERE id=? AND status='open' — LEGACY PATH
         ONLY (skip for workday-only bookings, slot_id is None).
     10. INSERT notifications_log(master_new) — UNIQUE guard
     11. Return BookingCreatedData with master_notification_text
    """
    settings = get_settings()

    # === Path branching — capture common state ===
    slot_id: UUID | None
    slot_master_id: UUID
    slot_date: date
    workday_id: UUID | None = None
    workday_capacity = 0  # sentinel — capacity check skipped when workday is None
    start_at: datetime
    end_at: datetime
    business_tz: str

    if payload.slot_id is not None:
        # === Legacy slot path (existing flow from /book, mirror) ===
        slot = await _select_open_slot(session, payload.slot_id)
        # Capture slot_id BEFORE any rollback-prone call (B1 fix).
        # `_select_or_create_client` (line ~430) may call `session.rollback()` on
        # concurrent client INSERT race → SQLAlchemy expires all attached
        # instances → subsequent `slot.id` access triggers lazy load
        # → MissingGreenlet (sync access to async session). UUID is immutable
        # and already loaded from SELECT → safe to capture here.
        slot_id = slot.id
        business_tz = await _select_business_timezone(session, business_id)
        start_at = _build_start_at(slot, business_tz)

        if start_at <= datetime.now(UTC):
            raise SlotInPastError(f"Slot {payload.slot_id} start_at={start_at} is in the past")

        service = await _select_service(session, payload.service_id)
        end_at = _build_end_at(start_at, service, settings.SERVICE_DEFAULT_DURATION_MIN)

        # WorkDay invariants (Этап 5.3, PLANS.md Gap 6): capture master_id + slot_date
        # BEFORE _select_or_create_client (rollback-prone — B1 pattern). Lookup is
        # OPTIONAL: if WorkDay exists for (master_id, slot_date), validate [start_at,
        # end_at] ∈ [workday.start_time, end_time]. If WorkDay not found (legacy slot-
        # only data created via /addslots before /openday rollout in 5.1) → skip check.
        slot_master_id = slot.master_id
        slot_date = slot.slot_date
        workday = await _select_workday_for_slot(session, slot_master_id, slot_date)
        if workday is not None:
            _validate_booking_within_workday(workday, start_at, end_at, business_tz)
            # B1 pattern (Этап 5.5): capture workday.id + max_concurrent_clients BEFORE
            # _select_or_create_client (line ~430) — that call may invoke
            # session.rollback() on concurrent client INSERT race, which expires ALL
            # attached instances (including workday). Accessing workday attributes
            # after rollback would trigger MissingGreenlet. UUID/int are immutable
            # and already loaded from SELECT → safe to capture here.
            workday_id = workday.id
            workday_capacity = workday.max_concurrent_clients
    else:
        # === WorkDay path (Этап 5.8a — new flow from /slots 5.8b) ===
        # Pydantic validator guarantees workday_id AND start_time_local both set
        # when slot_id is None (XOR contract). Defensive guard for mypy narrowing.
        workday_id_payload = payload.workday_id
        start_time_local = payload.start_time_local
        if workday_id_payload is None or start_time_local is None:
            raise RuntimeError(
                "BookingCreate validator bypassed — workday path requires "
                "both workday_id and start_time_local"
            )

        workday = await session.get(WorkDay, workday_id_payload)
        if workday is None:
            raise BookingOutsideWorkDayError(f"WorkDay {workday_id_payload} not found")
        if not workday.is_active:
            raise BookingOutsideWorkDayError(
                f"WorkDay {workday_id_payload} is closed (is_active=False) — "
                f"day was closed by master via /closeday"
            )
        business_tz = await _select_business_timezone(session, business_id)
        start_at = _build_start_at_from_workday(workday, start_time_local, business_tz)

        if start_at <= datetime.now(UTC):
            raise SlotInPastError(
                f"WorkDay {workday_id_payload} slot start_at={start_at} is in the past"
            )

        service = await _select_service(session, payload.service_id)
        end_at = _build_end_at(start_at, service, settings.SERVICE_DEFAULT_DURATION_MIN)

        # WorkDay invariants — MANDATORY on workday path (WorkDay already resolved).
        # Compare [start_at, end_at] ∈ [workday.start_time, end_time] in UTC.
        _validate_booking_within_workday(workday, start_at, end_at, business_tz)
        slot_master_id = workday.master_id
        slot_date = workday.work_date
        workday_id = workday.id
        workday_capacity = workday.max_concurrent_clients
        slot_id = None

    # HTML escape BEFORE INSERT — stored escaped, rendered without double-escape
    escaped_name = html.escape(payload.client_name, quote=False)
    escaped_service = html.escape(payload.service_title, quote=False)

    client = await _select_or_create_client(session, telegram_id)

    # Multi-client capacity check (Этап 5.5, B1 fix): acquire advisory lock + count
    # overlapping active bookings AFTER _select_or_create_client (rollback-prone
    # above) but BEFORE Booking INSERT (line below). workday_id/capacity captured
    # before the rollback-prone call (B1 pattern). Skipped when WorkDay is None
    # (backwards compat — legacy slot-only data has no capacity constraint).
    if workday_id is not None:
        await _acquire_advisory_lock(session, slot_master_id, slot_date)
        await _check_multi_client_capacity(
            session,
            workday_id=workday_id,
            capacity=workday_capacity,
            master_id=slot_master_id,
            start_at=start_at,
            end_at=end_at,
        )

    booking = Booking(
        slot_id=slot_id,
        business_id=business_id,
        master_id=master_id,
        client_id=client.id,
        service_id=payload.service_id,
        service_title_snapshot=escaped_service,
        client_name_snapshot=escaped_name,
        start_at=start_at,
        end_at=end_at,
        status="confirmed",
    )
    session.add(booking)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        if slot_id is not None:
            # Legacy path: UNIQUE(slot_id) was the guard (pre-миграция 006);
            # post-006 it's dropped, but IntegrityError here can still happen
            # for other constraints. Preserve legacy error message for tests.
            raise SlotAlreadyBookedError(
                f"Slot {slot_id} already booked (UNIQUE constraint)"
            ) from exc
        # WorkDay path: UNIQUE guard dropped (миграция 006) — IntegrityError here
        # is unexpected (CheckConstraint end_at > start_at enforced by service,
        # FK violations shouldn't happen since workday_id is resolved above).
        # Map to SlotAlreadyBookedError for user-facing consistency ("слот занят").
        raise SlotAlreadyBookedError(
            f"WorkDay {workday_id} booking flush failed (IntegrityError)"
        ) from exc

    # SQLite race protection: UPDATE ... WHERE status='open' + rowcount check
    # Этап 5.8a: LEGACY PATH ONLY — workday-only bookings (slot_id is None)
    # have no Slot row to UPDATE (slot generated on-the-fly from WorkDay window).
    # Race protection for workday path: multi-client capacity check above
    # (acquire_advisory_lock + count overlapping active bookings) handles
    # concurrent workday bookings — no Slot.status transition needed.
    if slot_id is not None:
        upd = update(Slot).where(Slot.id == slot_id, Slot.status == "open").values(status="booked")
        res = await session.execute(upd)
        if cast("CursorResult[Any]", res).rowcount == 0:
            # Lost race: slot was closed/booked between SELECT and UPDATE
            await session.rollback()
            raise SlotAlreadyBookedError(
                f"Slot {slot_id} was taken/closed between SELECT and UPDATE"
            )

    # notifications_log idempotency — UNIQUE(booking_id, kind)
    # SAVEPOINT isolates log_entry INSERT from main transaction (booking + slot):
    # if IntegrityError (already logged — idempotent path), only savepoint rolls
    # back, main transaction (booking + slot) survives and commits below.
    # NOTE: session.add(log_entry) MUST go INSIDE begin_nested() — otherwise
    # log_entry remains in session.new after savepoint rollback and outer commit
    # attempts re-INSERT → IntegrityError unhandled → whole outer tx rolls back.
    log_entry = NotificationLog(booking_id=booking.id, kind="master_new")
    try:
        async with session.begin_nested():
            session.add(log_entry)
            await session.flush()
    except IntegrityError:
        # Already logged — idempotent, OK. Savepoint rolled back, log_entry expunged.
        pass

    await session.commit()

    # Format notification text for master (rendered in HTML parse mode, no re-escape needed)
    local_time = start_at.astimezone(ZoneInfo(business_tz))
    formatted_time = local_time.strftime("%d %B %Y, %H:%M")
    master_text = f"Новая запись:\n📅 {formatted_time}\n👤 {escaped_name}\n💇 {escaped_service}"

    return BookingCreatedData(
        booking_id=booking.id,
        slot_id=slot_id,
        master_id=master_id,
        business_id=business_id,
        client_id=client.id,
        start_at=start_at,
        end_at=end_at,
        client_name_snapshot=escaped_name,
        service_title_snapshot=escaped_service,
        master_notification_text=master_text,
    )


# ============================================================
# Cancel booking (spec.md 41, 298, 317, 405-407)
# ============================================================


class BookingNotFoundError(Exception):
    """Booking not found OR not owned by this client.

    Defense-in-depth: same error for "no such booking" and "someone else's booking"
    (avoids leaking existence of bookings the caller doesn't own).
    """


class BookingAlreadyCancelledError(Exception):
    """Booking status is already 'cancelled' — idempotent retry / double-click / race."""


class CancelTooLateError(Exception):
    """now >= start_at - CANCEL_MIN_HOURS — cancellation window expired (spec.md 406)."""


@dataclass(frozen=True)
class CancelResult:
    """Result of cancel_booking — passed to handler for Telegram I/O.

    Mirrors BookingCreatedData: snapshots are already html.escape()'d in DB
    (no re-escape needed when rendering master notification in HTML parse mode).

    Этап 5.8a: slot_id теперь Optional — None для WorkDay-only bookings
    (cancel path skips Slot UPDATE when slot_id is None — booking.py:646).
    Critic iter 2 finding A.
    """

    booking_id: UUID
    slot_id: UUID | None
    master_id: UUID
    business_id: UUID
    client_name_snapshot: str
    service_title_snapshot: str
    start_at: datetime
    master_notification_text: str


async def cancel_booking(
    session: AsyncSession,
    booking_id: UUID,
    client_id: UUID,
    scheduler: AsyncIOScheduler,
    *,
    now_utc: datetime | None = None,  # must be tz-aware UTC (datetime.now(UTC))
) -> CancelResult:
    """Cancel booking (spec.md 41, 298, 317, 405-407).

    Atomic within one transaction (booking UPDATE + slot UPDATE + NotificationLog INSERT).
    `remove_jobs_for_booking` is called AFTER commit — if commit fails, jobs remain
    (booking still active, reminders still needed). If commit succeeds, jobs are
    obsolete (booking cancelled) and removed idempotently (scheduler.remove_job
    uses suppress(Exception) — see scheduler.py:90-95).

    `now_utc` injected for tests (production uses datetime.now(UTC)).

    Steps:
      1. SELECT booking WHERE id=? AND client_id=? (ownership + existence check)
      2. If booking is None → BookingNotFoundError (covers not-found AND not-owner)
      3. If booking.status == 'cancelled' → BookingAlreadyCancelledError
      4. If now >= start_at - CANCEL_MIN_HOURS → CancelTooLateError
      5. UPDATE booking SET status='cancelled' WHERE id=? AND client_id=?
         AND status IN ('confirmed','transferred') — rowcount check (race protection)
      6. UPDATE slot SET status='open' WHERE id=booking.slot_id (slot was 'booked')
      7. INSERT notifications_log(master_cancel) — UNIQUE guard, SAVEPOINT idempotency
      8. Build CancelResult (booking object alive — fixture uses expire_on_commit=False)
      9. commit
     10. remove_jobs_for_booking(scheduler, booking_id) — AFTER commit, idempotent
     11. Return CancelResult (handler sends master notification + client confirmation)
    """
    settings = get_settings()
    ref = now_utc or datetime.now(UTC)

    # Step 1-2: SELECT booking with ownership filter (covers not-found and not-owner)
    stmt = select(Booking).where(Booking.id == booking_id, Booking.client_id == client_id)
    booking = (await session.execute(stmt)).scalar_one_or_none()
    if booking is None:
        raise BookingNotFoundError(f"Booking {booking_id} not found for client {client_id}")

    # Step 3: already cancelled — fast path, no UPDATE needed
    if booking.status == "cancelled":
        raise BookingAlreadyCancelledError(f"Booking {booking_id} already cancelled")

    # Step 4: 24h rule (spec.md 406). start_at - 24h is the deadline; past → refuse.
    # Cross-DB aware-aware comparison: on SQLite booking.start_at is naive (DateTime
    # variant strips tzinfo on bind/result — verified empirically 2026-08-23), on
    # Postgres it's aware UTC (TIMESTAMPTZ + asyncpg). Inject tzinfo=UTC on DB-read
    # side so naive (SQLite) becomes aware UTC — no-op on Postgres where already aware.
    # ref is aware UTC (caller injects datetime.now(UTC) or test passes aware).
    cancel_deadline = booking.start_at.replace(tzinfo=UTC) - timedelta(
        hours=settings.CANCEL_MIN_HOURS
    )
    if ref >= cancel_deadline:
        raise CancelTooLateError(
            f"now={ref} >= cancel_deadline={cancel_deadline} for booking {booking_id}"
        )

    # Step 5: UPDATE booking SET status='cancelled' + rowcount check (race protection).
    # WHERE clause includes status IN (...) so a concurrent cancel/transfer
    # between SELECT and UPDATE results in rowcount=0 → BookingAlreadyCancelledError.
    upd_b = (
        update(Booking)
        .where(
            Booking.id == booking_id,
            Booking.client_id == client_id,
            Booking.status.in_(("confirmed", "transferred")),
        )
        .values(status="cancelled")
    )
    res_b = await session.execute(upd_b)
    if cast("CursorResult[Any]", res_b).rowcount == 0:
        await session.rollback()
        raise BookingAlreadyCancelledError(
            f"Booking {booking_id} status changed between SELECT and UPDATE"
        )

    # Step 6: UPDATE slot SET status='open' — slot was 'booked' (close_slot refuses
    # to close a booked slot, so during the lifetime of a booking slot.status is
    # always 'booked'). Releasing to 'open' makes the slot available for new bookings.
    #
    # Этап 5.8a: skip UPDATE Slot для workday-only bookings (slot_id is None) —
    # booking не имеет Slot row (30-min slot generated from WorkDay window на
    # /slots UI), releasing slot is no-op. WorkDay не имеет "open/closed"
    # status per slot — capacity check на /slots UI re-computes на каждый SELECT.
    if booking.slot_id is not None:
        upd_s = update(Slot).where(Slot.id == booking.slot_id).values(status="open")
        await session.execute(upd_s)

    # Step 7: NotificationLog master_cancel — SAVEPOINT idempotency (booking.py:198-212
    # pattern). If IntegrityError (already logged — duplicate retry), only the
    # savepoint rolls back, main transaction survives.
    log_entry = NotificationLog(booking_id=booking.id, kind="master_cancel")
    try:
        async with session.begin_nested():
            session.add(log_entry)
            await session.flush()
    except IntegrityError:
        # Already logged — idempotent. Savepoint rolled back, log_entry expunged.
        pass

    # Step 8: Build master notification text BEFORE commit (booking object alive —
    # test fixture uses expire_on_commit=False; production engine default also
    # safe because we read snapshots into the dataclass here, not after commit).
    business_tz = await _select_business_timezone(session, booking.business_id)
    # booking.start_at is naive UTC (SQLite stores naive); explicitly mark as UTC before
    # astimezone — otherwise Python interprets naive as system-local TZ (Mac default
    # Europe/Moscow would render wrong time in master notification; Render TZ=UTC is
    # correct by accident). Mirror of transfer_booking fix at line 644.
    local_time = booking.start_at.replace(tzinfo=UTC).astimezone(ZoneInfo(business_tz))
    formatted_time = local_time.strftime("%d %B %Y, %H:%M")
    master_text = (
        f"Отмена:\n"
        f"📅 {formatted_time}\n"
        f"👤 {booking.client_name_snapshot}\n"
        f"💇 {booking.service_title_snapshot}"
    )
    result = CancelResult(
        booking_id=booking.id,
        slot_id=booking.slot_id,
        master_id=booking.master_id,
        business_id=booking.business_id,
        client_name_snapshot=booking.client_name_snapshot,
        service_title_snapshot=booking.service_title_snapshot,
        start_at=booking.start_at,
        master_notification_text=master_text,
    )

    # Step 9: commit booking + slot UPDATE + NotificationLog INSERT atomically
    await session.commit()

    # Step 10: scheduler cleanup AFTER commit (atomic: commit fail → jobs remain,
    # booking still active). remove_jobs_for_booking uses suppress(Exception)
    # internally — idempotent if jobs already removed (Урок 2.4 retry scenario).
    remove_jobs_for_booking(scheduler, booking_id)

    # Step 11: return result (handler sends master notification + client confirmation)
    return result


# ============================================================
# Transfer booking (spec.md 41, 318, 408-409 — Блок 3 часть 3)
# ============================================================


class SlotNotAvailableError(Exception):
    """New slot is not 'open' (closed, booked, or not found) — transfer rejected."""


class BookingAlreadyTransferredError(Exception):
    """Concurrent transfer race: booking.start_at changed between SELECT and UPDATE.

    Raised when `UPDATE booking SET ... WHERE id=? AND client_id=? AND status IN
    ('confirmed','transferred') AND start_at=?` returns rowcount=0 — another
    concurrent transfer updated start_at between our SELECT and UPDATE. Distinct
    from BookingAlreadyCancelledError (which fires on a stale SELECT'd status).
    """


@dataclass(frozen=True)
class TransferResult:
    """Result of transfer_booking — passed to handler for Telegram I/O.

    Mirrors CancelResult: snapshots already escaped in DB (no re-escape needed).
    Carries both old and new start_at so the handler can render "X → Y" in
    master and client messages (spec.md 318: "Перенос: ... → ...").

    Этап 5.8a: old_slot_id Optional — workday-only bookings (slot_id=None)
    поднимают NotImplementedError на transfer (booking.py:817 guard, до build
    TransferResult). Тип defensive — happy path всегда non-None.
    """

    booking_id: UUID
    old_slot_id: UUID | None
    new_slot_id: UUID
    master_id: UUID
    business_id: UUID
    client_name_snapshot: str
    service_title_snapshot: str
    old_start_at: datetime
    new_start_at: datetime
    master_notification_text: str


async def transfer_booking(
    session: AsyncSession,
    booking_id: UUID,
    new_slot_id: UUID,
    client_id: UUID,
    scheduler: AsyncIOScheduler,
    *,
    now_utc: datetime | None = None,
) -> TransferResult:
    """Transfer booking to a new slot (spec.md 41, 318, 408-409).

    Atomic within one transaction:
      - UPDATE booking SET status='transferred', slot_id, start_at, end_at
      - UPDATE old slot SET status='open' (release old)
      - UPDATE new slot SET status='booked' WHERE status='open' (race protection)
      - INSERT notifications_log(master_transfer) — SAVEPOINT idempotency

    Scheduler side-effects (AFTER commit, like cancel_booking):
      - remove_jobs_for_booking(scheduler, booking_id) — old reminder jobs
      - schedule_for_booking(scheduler, booking_id, new_start_at) — new jobs
        (replace_existing=True, idempotent if job already exists)

    Race protection (deep-analysis Pass 3, [blocker] finding):
    Concurrent transfer callbacks on same booking → both SELECTs see same
    booking.start_at; winner UPDATEs (status, slot_id, start_at, end_at); loser's
    UPDATE WHERE start_at=<old value from SELECT> no longer matches (winner
    already changed start_at) → rowcount=0 → BookingAlreadyTransferredError.
    This is the invariant that distinguishes transfer from cancel: cancel uses
    status IN ('confirmed','transferred') alone (idempotent on double-cancel
    because second UPDATE WHERE status='cancelled' returns 0), but transfer
    needs the start_at pin because status stays IN ('confirmed','transferred')
    after the winner's UPDATE.

    Re-transfer (status='transferred' → transfer again) is allowed: the
    status IN ('confirmed','transferred') clause accepts 'transferred' too.

    `now_utc` injected for tests (production uses datetime.now(UTC)).

    Steps:
      1. SELECT booking WHERE id=? AND client_id=? (ownership + existence)
      2. If None → BookingNotFoundError
      3. If status='cancelled' → BookingAlreadyCancelledError (defensive)
      4. If now >= start_at - CANCEL_MIN_HOURS → CancelTooLateError (24h rule)
      5. SELECT new slot WHERE id=? AND status='open' (race protection)
         - If closed/not found → SlotNotAvailableError
         - If status='booked' → SlotAlreadyBookedError (reuse)
      6. Build new_start_at (UTC) + new_end_at (default duration)
      7. If new_start_at <= now → SlotInPastError
      8. UPDATE booking SET status='transferred', slot_id, start_at, end_at
         WHERE id=? AND client_id=? AND status IN ('confirmed','transferred')
         AND start_at=<captured at SELECT> — rowcount check (concurrent race)
      9. UPDATE old slot SET status='open' WHERE id=<old slot_id>
     10. UPDATE new slot SET status='booked' WHERE id=? AND status='open'
         — rowcount check (race protection, like create_booking:184-196)
     11. INSERT NotificationLog(master_transfer) — SAVEPOINT idempotency
     12. Build TransferResult (old + new start_at for "X → Y" message)
     13. commit
     14. remove_jobs_for_booking + schedule_for_booking (AFTER commit)
     15. Return TransferResult
    """
    settings = get_settings()
    ref = now_utc or datetime.now(UTC)

    # Step 1-3: SELECT booking with ownership + status check.
    stmt_b = select(Booking).where(Booking.id == booking_id, Booking.client_id == client_id)
    booking = (await session.execute(stmt_b)).scalar_one_or_none()
    if booking is None:
        raise BookingNotFoundError(f"Booking {booking_id} not found for client {client_id}")
    if booking.status == "cancelled":
        raise BookingAlreadyCancelledError(f"Booking {booking_id} is cancelled, cannot transfer")

    # Этап 5.8a guard: workday-only bookings (slot_id is None) не могут
    # переноситься через transfer_booking — это slot-based API (требует
    # new_slot_id, _select_open_slot, UPDATE Slot.status). WorkDay transfer
    # = 5.9 scope (admin_move_booking — отдельный сервис без 24h rule и без
    # client_id pin). User-facing behavior: handler ловит NotImplementedError
    # (5.8b — добавим в transfer_slot_cb except list) ИЛИ mybookings_keyboard
    # hide-transfer-button для workday-only bookings (5.8b — Gap 5).
    if booking.slot_id is None:
        raise NotImplementedError(
            f"WorkDay transfer is 5.9 scope — admin_move_booking. "
            f"Booking {booking_id} has slot_id=None (WorkDay-only)"
        )

    # Step 4: 24h rule (same as cancel_booking). Cross-DB aware-aware comparison:
    # old_start_at is naive on SQLite / aware UTC on Postgres. Inject tzinfo=UTC
    # so naive becomes aware (no-op on Postgres). ref is aware UTC (caller injects).
    # Capture OLD slot_id and start_at BEFORE step 8 UPDATE booking — SQLAlchemy
    # auto-mutates booking.slot_id and booking.start_at after UPDATE (synchronize_session
    # default). Without this capture, step 9 would release the NEW slot (already 'booked'
    # from step 10) and TransferResult.old_slot_id would carry the new_slot_id.
    old_slot_id = booking.slot_id
    old_start_at = booking.start_at  # naive UTC (captured for race-protected UPDATE)
    cancel_deadline = old_start_at.replace(tzinfo=UTC) - timedelta(hours=settings.CANCEL_MIN_HOURS)
    if ref >= cancel_deadline:
        raise CancelTooLateError(
            f"now={ref} >= transfer_deadline={cancel_deadline} for booking {booking_id}"
        )

    # Step 5: SELECT new slot — re-use create_booking's _select_open_slot helper.
    # It raises SlotClosedError (slot not found OR closed) or SlotAlreadyBookedError
    # (slot already booked). Caller (handler) maps these to user-facing messages.
    new_slot = await _select_open_slot(session, new_slot_id)

    # Step 6: Build new_start_at (UTC) from new_slot.slot_hour (LOCAL in business tz).
    business_tz = await _select_business_timezone(session, booking.business_id)
    new_start_at = _build_start_at(new_slot, business_tz)

    # Step 7: new_start_at must be in the future (re-use SlotInPastError).
    # Uses real datetime.now(UTC), NOT injected `ref` — semantic: the new slot
    # must be after the REAL current time (booking a slot in the past relative
    # to now is always wrong, regardless of `ref` which only controls the 24h
    # rule check for the OLD booking's start_at). Mixing ref here would break
    # tests that inject ref in far past to bypass 24h rule (e.g. test_slot_in_past).
    if new_start_at <= datetime.now(UTC):
        raise SlotInPastError(f"New slot {new_slot_id} start_at={new_start_at} is in the past")

    # Look up service for end_at duration (booking.service_id may be None — fallback to default).
    service = await _select_service(session, booking.service_id)
    new_end_at = _build_end_at(new_start_at, service, settings.SERVICE_DEFAULT_DURATION_MIN)

    # Multi-client capacity check on NEW slot (Этап 5.5, B2 fix): SELECT WorkDay
    # for (new_slot.master_id, new_slot.slot_date). Validate + acquire advisory
    # lock + count overlapping active bookings BEFORE UPDATE booking (line 833).
    # Without this, a concurrent create_booking on the new slot could slip through
    # the EXCLUDE drop (migration 005) and double-book the slot window.
    # Backwards compat: no WorkDay → skip (legacy slot-only data).
    new_slot_master_id = new_slot.master_id
    new_slot_date = new_slot.slot_date
    new_workday = await _select_workday_for_slot(session, new_slot_master_id, new_slot_date)
    if new_workday is not None:
        _validate_booking_within_workday(new_workday, new_start_at, new_end_at, business_tz)
        new_workday_id = new_workday.id
        new_workday_capacity = new_workday.max_concurrent_clients
        await _acquire_advisory_lock(session, new_slot_master_id, new_slot_date)
        await _check_multi_client_capacity(
            session,
            workday_id=new_workday_id,
            capacity=new_workday_capacity,
            master_id=new_slot_master_id,
            start_at=new_start_at,
            end_at=new_end_at,
            excluded_booking_id=booking_id,
        )

    # Step 8: UPDATE booking with start_at-in-WHERE for concurrent-transfer race protection.
    # Loser's WHERE clause `start_at = <old_start_at captured at SELECT>` fails after
    # winner's UPDATE changed start_at → rowcount=0 → BookingAlreadyTransferredError.
    upd_b = (
        update(Booking)
        .where(
            Booking.id == booking_id,
            Booking.client_id == client_id,
            Booking.status.in_(("confirmed", "transferred")),
            Booking.start_at == old_start_at,  # race-protection pin (Pass 3 [blocker] finding)
        )
        .values(
            status="transferred",
            slot_id=new_slot.id,
            start_at=new_start_at,  # aware UTC; SQLAlchemy variant strips tzinfo on SQLite bind
            end_at=new_end_at,
        )
    )
    try:
        res_b = await session.execute(upd_b)
    except IntegrityError as exc:
        # EXCLUDE constraint (Postgres): new tstzrange(start_at, end_at) overlaps
        # another active booking (confirmed/transferred) for same master.
        # SQLite не имеет EXCLUDE — UNIQUE(slot_id) handles only same-slot case,
        # но это безопасно (SQLite dev, надёжность через UNIQUE + service-layer checks).
        # Map to existing SlotAlreadyBookedError — пользователь видит "слот занят".
        await session.rollback()
        raise SlotAlreadyBookedError(
            f"Transfer to slot {new_slot.id} overlaps existing booking (EXCLUDE constraint)"
        ) from exc
    if cast("CursorResult[Any]", res_b).rowcount == 0:
        # rowcount=0 means WHERE clause didn't match — three possible causes:
        #   1. Concurrent transfer (winner changed start_at) → BookingAlreadyTransferredError
        #   2. Concurrent cancel (winner changed status to 'cancelled') →
        #      BookingAlreadyCancelledError
        #   3. Concurrent booking deletion (rare in MVP — no DELETE in current code)
        # Re-SELECT to disambiguate: if status='cancelled' → cancel race; otherwise
        # (status='transferred' OR booking gone) → transfer race. Without this
        # re-check, concurrent cancel would surface as "Запись уже перенесена" —
        # misleading (user sees the booking is gone, not transferred).
        await session.rollback()
        recheck = await session.execute(select(Booking.status).where(Booking.id == booking_id))
        current_status = recheck.scalar_one_or_none()
        if current_status == "cancelled":
            raise BookingAlreadyCancelledError(
                f"Booking {booking_id} was cancelled by concurrent request"
            )
        raise BookingAlreadyTransferredError(
            f"Booking {booking_id} start_at changed between SELECT and UPDATE "
            "(concurrent transfer — winner's UPDATE already committed)"
        )

    # Step 9: Release OLD slot → 'open' (idempotent: if new_slot == old_slot,
    # step 10 below re-bumps it back to 'booked'). Use captured old_slot_id —
    # booking.slot_id was mutated by step 8 UPDATE to new_slot.id.
    upd_old_slot = update(Slot).where(Slot.id == old_slot_id).values(status="open")
    await session.execute(upd_old_slot)

    # Step 10: Book NEW slot — SQLite race protection (UPDATE WHERE status='open' + rowcount).
    # Pattern from create_booking:184-196. If slot was taken between SELECT (step 5)
    # and UPDATE (here) → rowcount=0 → rollback → SlotAlreadyBookedError.
    upd_new_slot = (
        update(Slot).where(Slot.id == new_slot.id, Slot.status == "open").values(status="booked")
    )
    res_new_slot = await session.execute(upd_new_slot)
    if cast("CursorResult[Any]", res_new_slot).rowcount == 0:
        await session.rollback()
        # Use function parameter `new_slot_id` (immutable UUID), NOT `new_slot.id`.
        # After session.rollback() above, `new_slot` instance attributes are
        # expired → `new_slot.id` would trigger lazy load → MissingGreenlet
        # (B3 fix — same pattern as B1 in create_booking).
        raise SlotAlreadyBookedError(
            f"New slot {new_slot_id} was taken/closed between SELECT and UPDATE"
        )

    # Step 11: NotificationLog master_transfer — SAVEPOINT idempotency (booking.py:198-212 pattern).
    log_entry = NotificationLog(booking_id=booking.id, kind="master_transfer")
    try:
        async with session.begin_nested():
            session.add(log_entry)
            await session.flush()
    except IntegrityError:
        # Already logged — idempotent. Savepoint rolled back, log_entry expunged.
        pass

    # Step 12: Build master notification text "Перенос: <old> → <new>" (spec.md 318).
    # Use snapshots (already html.escape'd in DB) for client/service lines.
    # old_start_at is naive UTC (SQLite stores naive); explicitly mark as UTC before
    # astimezone — otherwise Python interprets naive as system-local TZ (Mac default
    # Europe/Moscow would render wrong old time; Render TZ=UTC is correct by accident).
    # new_start_at is already aware UTC (built by _build_start_at).
    old_local_time = old_start_at.replace(tzinfo=UTC).astimezone(ZoneInfo(business_tz))
    new_local_time = new_start_at.astimezone(ZoneInfo(business_tz))
    old_formatted = old_local_time.strftime("%d %B %Y, %H:%M")
    new_formatted = new_local_time.strftime("%d %B %Y, %H:%M")
    master_text = (
        f"Перенос:\n"
        f"📅 {old_formatted} → {new_formatted}\n"
        f"👤 {booking.client_name_snapshot}\n"
        f"💇 {booking.service_title_snapshot}"
    )
    result = TransferResult(
        booking_id=booking.id,
        old_slot_id=old_slot_id,
        new_slot_id=new_slot.id,
        master_id=booking.master_id,
        business_id=booking.business_id,
        client_name_snapshot=booking.client_name_snapshot,
        service_title_snapshot=booking.service_title_snapshot,
        # Store old_start_at as aware UTC (replace tzinfo) so handler's
        # result.old_start_at.astimezone(...) is correct on all system TZs.
        old_start_at=old_start_at.replace(tzinfo=UTC),
        new_start_at=new_start_at,
        master_notification_text=master_text,
    )

    # Step 13: commit booking UPDATE + 2 slot UPDATEs + NotificationLog atomically.
    await session.commit()

    # Step 14: scheduler side-effects AFTER commit (atomic: commit fail → jobs remain,
    # booking still active at OLD start_at with old reminders). remove_jobs_for_booking
    # uses suppress(Exception) internally — idempotent. schedule_for_booking uses
    # replace_existing=True — idempotent (safe to call after remove_jobs).
    remove_jobs_for_booking(scheduler, booking_id)
    schedule_for_booking(scheduler, booking_id, new_start_at)

    # Step 15: return result (handler sends master notification + client confirmation).
    return result
