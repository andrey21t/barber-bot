from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки проекта (spec.md 290-305).

    DATABASE_URL_SYNC — пусто в dev (MemoryJobStore), заполняется в проде для SQLAlchemyJobStore.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    BOT_TOKEN: str
    DATABASE_URL: str = "sqlite+aiosqlite:///./barber.db"
    DATABASE_URL_SYNC: str = ""  # sync engine для SQLAlchemyJobStore (psycopg2)
    ADMIN_ID: int
    TIMEZONE: str = "Europe/Moscow"
    REMINDER_24H_BEFORE: int = 24
    REMINDER_1H_BEFORE: int = 1
    CANCEL_MIN_HOURS: int = 24
    MISFIRE_GRACE_TIME: int = 3600  # Render free tier sleep 15 мин = 900 сек → 3600 сек запас
    SERVICE_DEFAULT_DURATION_MIN: int = 60
    MAX_BOOKING_DAYS_AHEAD: int = 60  # aiogram_calendar range (today..today+N days)


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from .env
