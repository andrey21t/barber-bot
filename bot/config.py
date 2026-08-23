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

    @property
    def async_database_url(self) -> str:
        """Convert Render's plain `postgresql://` / `postgres://` → asyncpg format.

        Render Postgres `fromDatabase` env var provides `postgresql://user:pass@host:port/db`
        (НЕ asyncpg). asyncpg driver требует `postgresql+asyncpg://`.
        """
        if self.DATABASE_URL.startswith(("postgresql://", "postgres://")):
            return self.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1).replace(
                "postgres://", "postgresql+asyncpg://", 1
            )
        return self.DATABASE_URL

    @property
    def sync_database_url(self) -> str:
        """Derive sync URL for SQLAlchemyJobStore (psycopg2).

        Render НЕ поддерживает env var interpolation в render.yaml — поэтому
        DATABASE_URL_SYNC задаём через property, не через render.yaml envVars.
        """
        if self.DATABASE_URL_SYNC:
            return self.DATABASE_URL_SYNC
        for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
            if self.DATABASE_URL.startswith(prefix):
                return self.DATABASE_URL.replace(prefix, "postgresql+psycopg2://", 1)
        # SQLite fallback (dev)
        return self.DATABASE_URL.replace("sqlite+aiosqlite://", "sqlite://", 1)


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from .env
