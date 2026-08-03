from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # PostgreSQL
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str
    postgres_user: str
    postgres_password: str

    # APIs
    polygon_api_key: str
    alpha_vantage_api_key: str = ""
    openfigi_api_key: str = ""
    # FRED. Free, but not optional the way the two above are: FRED rejects an
    # unauthenticated request outright rather than serving a reduced response,
    # so src/ingestion/fred.py fails loudly at startup when this is blank.
    fred_api_key: str = ""

    # Yahoo needs no key at all — the chart endpoint is unauthenticated, which
    # is one of the reasons ADR-0006 makes it the fallback and not the primary.

    # App
    app_env: str = "development"
    log_level: str = "INFO"

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


# Single shared instance — import this everywhere
settings = Settings()