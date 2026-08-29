from datetime import date, datetime, time
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BookingCreate(BaseModel):
    """DTO для создания booking (вход в service.create_booking).

    Этап 5.8a (миграция 006): Booking.slot_id nullable → два path'а:

    1. **Legacy slot path** (`slot_id` set, `workday_id`/`start_time_local` None):
       существующий flow из /book — Slot row в DB, _select_open_slot,
       _build_start_at(slot, business_tz), UPDATE slot.status='booked'.

    2. **WorkDay path** (`workday_id` + `start_time_local` set, `slot_id` None):
       новый flow из /slots (5.8b — BookSlot30CallbackData). Slot row НЕ
       создаётся — 30-мин slot генерируется из WorkDay window на лету в /slots
       UI. start_at строится через _build_start_at_from_workday(workday,
       start_time_local: time, business_tz). Booking.slot_id = NULL.

    Validator (XOR contract): slot_id XOR (workday_id AND start_time_local).
    `start_time_local: time` (НЕ `start_at: datetime`) — соответсвует
    `_build_start_at_from_workday(workday, start_time_local: time, business_tz)`
    (booking.py:289-290), UI /slots генерирует LOCAL HH:MM → BookSlot30CallbackData
    несёт `start_time_local`. Compile-time catch на стороне contract, не silent
    runtime conversion. Critic iter 2 finding B.

    Note: client_id intentionally absent — service resolves client by telegram_id
    via _select_or_create_client(telegram_id) (booking.py:101-112).
    """

    model_config = ConfigDict(frozen=True)

    slot_id: UUID | None = None
    workday_id: UUID | None = None
    start_time_local: time | None = None
    client_name: str = Field(min_length=1, max_length=255)
    service_title: str = Field(min_length=1, max_length=255)
    service_id: UUID | None = None

    @model_validator(mode="after")
    def _validate_slot_xor_workday(self) -> "BookingCreate":
        """XOR contract: slot_id XOR (workday_id AND start_time_local).

        - Legacy slot path: slot_id set, workday_id AND start_time_local None.
        - WorkDay path: slot_id None, workday_id AND start_time_local both set.
        - Anything else → ValueError (compile-time catch on contract side).
        """
        has_slot = self.slot_id is not None
        has_workday = self.workday_id is not None and self.start_time_local is not None
        if has_slot and has_workday:
            raise ValueError(
                "BookingCreate: slot_id is mutually exclusive with "
                "(workday_id, start_time_local) — pick one path"
            )
        if not has_slot and not has_workday:
            raise ValueError(
                "BookingCreate: must provide either slot_id (legacy) "
                "or (workday_id AND start_time_local) (WorkDay path)"
            )
        # Half-state invalid: slot_id None + workday_id set + start_time_local None
        # (or vice versa) → has_workday is False → falls into "neither" branch above.
        return self


class BookingOut(BaseModel):
    """DTO результата create_booking (выход из service)."""

    model_config = ConfigDict(frozen=True)

    booking_id: UUID
    slot_id: UUID | None
    master_id: UUID
    business_id: UUID
    start_at: datetime
    end_at: datetime
    client_name_snapshot: str
    service_title_snapshot: str
    master_notification_text: str


class SlotOut(BaseModel):
    """DTO слота для handler/keyboard."""

    model_config = ConfigDict(frozen=True)

    slot_id: UUID
    slot_date: date
    slot_hour: int
    status: str
