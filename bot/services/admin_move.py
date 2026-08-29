"""admin_move_booking — admin-initiated booking move (Этап 5.9, PLANS.md Gap 5).

Admin (мастер Екатерина) переносит ЛЮБОЙ booking через /today → [🔄 Перенести]
кнопка → calendar → 30-min slot picker → confirm. Distinct от transfer_booking
(client-initiated transfer):

- SKIPS 24h rule (admin может перенести даже за 5 минут до начала).
- SKIPS client_id pin (admin переносит ЛЮБОЙ booking, не только свой).
- Notification goes to CLIENT (не мастеру) — kind='client_moved'.
- Destination is WorkDay-based (30-min step from workday window), NOT legacy
  Slot-based. Legacy booking.slot_id → released to 'open' + booking.slot_id=NULL
  (unified workday-based destination, design decision Q4=A).

Mirror transfer_booking (booking.py:841-1113) structure:
  - Race protection: UPDATE WHERE start_at=old_start_at (Pass 3 [blocker] finding).
  - Postgres EXCLUDE → SlotAlreadyBookedError (IntegrityError mapping).
  - rowcount=0 → re-SELECT for cancel vs transfer race disambiguation.
  - NotificationLog SAVEPOINT idempotency (UNIQUE(booking_id, kind) guard).
  - Scheduler side-effects AFTER commit (remove_jobs + schedule_for_booking).

3 edge cases (deep-analysis iter 2):
  - Past time (new_start_at <= now) → REJECT with SlotInPastError (real now,
    NOT injected `ref` — same rationale as transfer_booking:955).
  - Inactive WorkDay (is_active=False) → REJECT with WorkDayInactiveError.
  - Already-past booking (booking.start_at <= now) → ALLOW (scheduler's
    misfire_grace_time handles past time gracefully — verified scheduler.py:226).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.models import Booking, Client, NotificationLog, WorkDay
from bot.services.booking import (
    BookingAlreadyCancelledError,
    BookingAlreadyTransferredError,
    BookingNotFoundError,
    SlotAlreadyBookedError,
    SlotInPastError,
    _acquire_advisory_lock,
    _build_end_at,
    _build_start_at_from_workday,
    _check_multi_client_capacity,
    _select_business_timezone,
    _select_service,
    _validate_booking_within_workday,
)

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler


class WorkDayNotFoundError(Exception):
    """Raised when new_workday_id does not exist in DB (admin picked a date without WorkDay)."""


class WorkDayInactiveError(Exception):
    """Raised when destination WorkDay exists but is_active=False (closed day).

    Distinct from BookingOutsideWorkDayError (window bounds violation) — inactive
    means the master explicitly closed the day; the window bounds may still be
    valid but booking is rejected by policy.
    """


@dataclass
class AdminMoveResult:
    """Result of admin_move_booking — handler builds client notification from this.

    All datetimes are aware UTC (SQLite naive replaced with tzinfo=UTC at capture).
    """

    booking_id: UUID
    old_start_at: datetime  # aware UTC (replace tzinfo on SQLite naive)
    new_start_at: datetime  # aware UTC (built by _build_start_at_from_workday)
    client_telegram_id: int | None  # None if Client row missing — handler skips send
    client_name_snapshot: str
    service_title_snapshot: str
    master_id: UUID
    business_id: UUID
    business_timezone: str
    old_slot_id: UUID | None  # legacy slot released? None if workday-only source
    notification_logged: bool  # True if new NotificationLog row, False if idempotent skip


async def admin_move_booking(
    session: AsyncSession,
    booking_id: UUID,
    new_workday_id: UUID,
    new_start_at_local: time,
    scheduler: "AsyncIOScheduler",
) -> AdminMoveResult:
    """Move booking to a new WorkDay-based slot (admin action, no 24h rule, no client_id pin).

    Atomic within one transaction:
      - UPDATE booking SET status='transferred', slot_id=NULL, start_at, end_at
      - UPDATE old slot SET status='open' (release legacy slot, IF booking.slot_id is not None)
      - INSERT notifications_log(client_moved) — SAVEPOINT idempotency

    Scheduler side-effects (AFTER commit, mirror transfer_booking:1109-1110):
      - remove_jobs_for_booking(scheduler, booking_id) — old reminder jobs
      - schedule_for_booking(scheduler, booking_id, new_start_at) — new jobs
        (replace_existing=True, idempotent if job already exists; misfire_grace_time
        handles past time gracefully)

    Race protection (mirror transfer_booking:995):
      Concurrent admin_move callbacks on same booking → both SELECTs see same
      booking.start_at; winner UPDATEs; loser's UPDATE WHERE start_at=<old value>
      no longer matches → rowcount=0 → BookingAlreadyTransferredError.

    Past-time REJECT for new_start_at uses real `datetime.now(UTC)`, NOT injected
    `now_utc` param (unlike transfer_booking which has `now_utc` for 24h rule —
    admin_move skip'ает 24h rule → parameter не нужен). Same rationale as
    transfer_booking:955 (mixing ref would break tests that inject ref in far
    past to bypass 24h rule).

    Already-past booking ALLOWED by design (admin can move booking that already
    started — scheduler misfire_grace_time handles old jobs gracefully).

    Steps:
      1. SELECT booking WHERE id=? (NO client_id pin — admin can move ANY booking)
      2. If None → BookingNotFoundError
      3. If status='cancelled' → BookingAlreadyCancelledError (defensive)
      4. Capture old_slot_id + old_start_at BEFORE step 12 UPDATE (mirror
         transfer_booking:932 — SQLAlchemy mutates booking.slot_id and
         booking.start_at after UPDATE, breaking step 13 release)
      5. SELECT new_workday WHERE id=?
      6. If None → WorkDayNotFoundError
      7. If not is_active → WorkDayInactiveError (closed day, policy reject)
      8. SELECT business_tz for booking.business_id
      9. Build new_start_at (UTC) via _build_start_at_from_workday
     10. SELECT service for end_at duration (booking.service_id may be None)
     11. Build new_end_at (default duration fallback)
     12. _validate_booking_within_workday — bounds check [start_at, end_at] ⊆
         [workday.start_time, end_time] in UTC
     13. If new_start_at <= datetime.now(UTC) → SlotInPastError (real now, not ref)
     14. _acquire_advisory_lock on (master_id, new_workday.work_date) — Postgres-only
         race serialization for concurrent create_booking on same workday
     15. _check_multi_client_capacity with excluded_booking_id=booking_id (mirror
         transfer_booking:983 — self-overlap on same-date move)
     16. UPDATE booking SET status='transferred', slot_id=NULL, start_at, end_at
         WHERE id=? AND status IN ('confirmed','transferred') AND start_at=old_start_at
         — rowcount check (race protection)
     17. On IntegrityError → SlotAlreadyBookedError (Postgres EXCLUDE constraint)
     18. On rowcount=0 → re-SELECT for cancel vs transfer race disambiguation
     19. If old_slot_id is not None: UPDATE old slot SET status='open' (release legacy)
     20. INSERT NotificationLog('client_moved') — SAVEPOINT idempotency
     21. SELECT client.telegram_id for handler (None if Client row missing —
         handler skips notification send, log still recorded)
     22. Build AdminMoveResult
     23. commit (atomic: booking UPDATE + slot release + NotificationLog)
     24. remove_jobs_for_booking + schedule_for_booking (AFTER commit)
     25. return result
    """
    settings = get_settings()

    # Step 1-3: SELECT booking (NO client_id pin — admin can move ANY booking).
    stmt_b = select(Booking).where(Booking.id == booking_id)
    booking = (await session.execute(stmt_b)).scalar_one_or_none()
    if booking is None:
        raise BookingNotFoundError(f"Booking {booking_id} not found")
    if booking.status == "cancelled":
        raise BookingAlreadyCancelledError(f"Booking {booking_id} is cancelled, cannot move")

    # Step 4: Capture OLD slot_id + start_at BEFORE UPDATE.
    # Mirror transfer_booking:932 rationale: SQLAlchemy mutates booking.slot_id
    # and booking.start_at after UPDATE (synchronize_session default). Without
    # capture, step 19 would release the NEW slot (already NULL) and result
    # old_slot_id would carry None even for legacy slot-based bookings.
    old_slot_id = booking.slot_id  # UUID | None (None for workday-only source)
    old_start_at = booking.start_at  # naive UTC on SQLite (captured for race-protected UPDATE)

    # Step 5-7: SELECT new_workday + is_active check.
    stmt_w = select(WorkDay).where(WorkDay.id == new_workday_id)
    new_workday = (await session.execute(stmt_w)).scalar_one_or_none()
    if new_workday is None:
        raise WorkDayNotFoundError(f"WorkDay {new_workday_id} not found")
    if not new_workday.is_active:
        raise WorkDayInactiveError(
            f"WorkDay {new_workday_id} is inactive (closed day) — cannot move booking there"
        )

    # Step 8-11: Build new_start_at + new_end_at (mirror transfer_booking:946-960).
    business_tz = await _select_business_timezone(session, booking.business_id)
    new_start_at = _build_start_at_from_workday(new_workday, new_start_at_local, business_tz)
    service = await _select_service(session, booking.service_id)
    new_end_at = _build_end_at(new_start_at, service, settings.SERVICE_DEFAULT_DURATION_MIN)

    # Step 12: Validate [new_start_at, new_end_at] ⊆ [workday.start_time, end_time] in UTC.
    _validate_booking_within_workday(new_workday, new_start_at, new_end_at, business_tz)

    # Step 13: Past-time REJECT for new_start_at (real now, NOT injected ref).
    # Mirror transfer_booking:955 rationale: mixing ref here would break tests
    # that inject ref in far past to bypass the past-time check.
    if new_start_at <= datetime.now(UTC):
        raise SlotInPastError(
            f"New slot start_at={new_start_at} is in the past (admin_move)"
        )

    # Step 14: Acquire advisory lock on (master_id, new_workday.work_date).
    # Postgres-only — no-op on SQLite (file-based serializes writes via DB lock).
    await _acquire_advisory_lock(session, booking.master_id, new_workday.work_date)

    # Step 15: Multi-client capacity check (exclude self — same-date move overlaps
    # with own OLD range, should not count against capacity).
    await _check_multi_client_capacity(
        session,
        workday_id=new_workday.id,
        capacity=new_workday.max_concurrent_clients,
        master_id=booking.master_id,
        start_at=new_start_at,
        end_at=new_end_at,
        excluded_booking_id=booking_id,
    )

    # Step 16: UPDATE booking with start_at-in-WHERE for concurrent-move race protection.
    # Mirror transfer_booking:989-1003. booking.slot_id → NULL (unified workday-based
    # destination — design decision Q4=A, legacy slot_id is dropped).
    upd_b = (
        update(Booking)
        .where(
            Booking.id == booking_id,
            Booking.status.in_(("confirmed", "transferred")),
            Booking.start_at == old_start_at,  # race-protection pin
        )
        .values(
            status="transferred",
            slot_id=None,  # unified workday-based destination
            start_at=new_start_at,
            end_at=new_end_at,
        )
    )
    notification_logged = True
    try:
        res_b = await session.execute(upd_b)
    except IntegrityError as exc:
        # Postgres EXCLUDE constraint: new tstzrange overlaps existing active booking.
        # SQLite has no EXCLUDE — UNIQUE(slot_id) is irrelevant (slot_id=NULL now),
        # service-layer checks (capacity + advisory lock) are the safety net.
        await session.rollback()
        raise SlotAlreadyBookedError(
            f"admin_move to workday {new_workday.id} overlaps existing booking "
            f"(EXCLUDE constraint or capacity race)"
        ) from exc
    if cast("CursorResult[Any]", res_b).rowcount == 0:
        # rowcount=0 — race: concurrent move/cancel changed booking between SELECT and UPDATE.
        # Mirror transfer_booking:1016-1036 disambiguation: re-SELECT for cancel vs transfer.
        await session.rollback()
        recheck = await session.execute(select(Booking.status).where(Booking.id == booking_id))
        current_status = recheck.scalar_one_or_none()
        if current_status == "cancelled":
            raise BookingAlreadyCancelledError(
                f"Booking {booking_id} was cancelled by concurrent request"
            )
        raise BookingAlreadyTransferredError(
            f"Booking {booking_id} start_at changed between SELECT and UPDATE "
            "(concurrent admin_move — winner's UPDATE already committed)"
        )

    # Step 19: Release OLD legacy slot IF booking.slot_id was not None.
    # Workday-only source (slot_id is None) — no slot to release. Legacy slot-based
    # source — UPDATE slot SET status='open' (mirror transfer_booking:1041).
    if old_slot_id is not None:
        from bot.models import Slot

        upd_old_slot = update(Slot).where(Slot.id == old_slot_id).values(status="open")
        await session.execute(upd_old_slot)

    # Step 20: NotificationLog('client_moved') — SAVEPOINT idempotency.
    # Mirror transfer_booking:1062-1069 pattern (NOT log_notification from
    # notifications.py — that helper commits, breaking atomicity with booking UPDATE).
    log_entry = NotificationLog(booking_id=booking.id, kind="client_moved")
    try:
        async with session.begin_nested():
            session.add(log_entry)
            await session.flush()
    except IntegrityError:
        # Already logged — idempotent. Savepoint rolled back, log_entry expunged.
        notification_logged = False

    # Step 21: SELECT client.telegram_id for handler notification send.
    # None if Client row missing (rare — Client row created at booking time and
    # CASCADE-deleted only if booking is deleted, which is not a current code path).
    # Handler checks result.client_telegram_id is None → skip bot.send_message.
    client_stmt = select(Client).where(Client.id == booking.client_id)
    client = (await session.execute(client_stmt)).scalar_one_or_none()
    client_telegram_id = client.telegram_id if client is not None else None

    # Step 22: Build result.
    result = AdminMoveResult(
        booking_id=booking.id,
        # Store old_start_at as aware UTC (replace tzinfo) so handler's
        # result.old_start_at.astimezone(...) is correct on all system TZs
        # (mirror transfer_booking:1097).
        old_start_at=old_start_at.replace(tzinfo=UTC),
        new_start_at=new_start_at,
        client_telegram_id=client_telegram_id,
        client_name_snapshot=booking.client_name_snapshot,
        service_title_snapshot=booking.service_title_snapshot,
        master_id=booking.master_id,
        business_id=booking.business_id,
        business_timezone=business_tz,
        old_slot_id=old_slot_id,
        notification_logged=notification_logged,
    )

    # Step 23: commit booking UPDATE + slot release + NotificationLog atomically.
    await session.commit()

    # Step 24: scheduler side-effects AFTER commit (mirror transfer_booking:1109-1110).
    # remove_jobs_for_booking uses suppress(Exception) — idempotent. schedule_for_booking
    # uses replace_existing=True — idempotent. misfire_grace_time handles past time
    # (already-past booking move is allowed by design).
    from scheduler import remove_jobs_for_booking, schedule_for_booking

    remove_jobs_for_booking(scheduler, booking_id)
    schedule_for_booking(scheduler, booking_id, new_start_at)

    # Step 25: return result.
    return result
