from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class BookingCreate(BaseModel):
    """DTO для создания booking (вход в service.create_booking).

    Note: client_id intentionally absent — service resolves client by telegram_id
    via _select_or_create_client(telegram_id) (booking.py:101-112).
    """

    model_config = ConfigDict(frozen=True)

    slot_id: UUID
    client_name: str = Field(min_length=1, max_length=255)
    service_title: str = Field(min_length=1, max_length=255)
    service_id: UUID | None = None


class BookingOut(BaseModel):
    """DTO результата create_booking (выход из service)."""

    model_config = ConfigDict(frozen=True)

    booking_id: UUID
    slot_id: UUID
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
