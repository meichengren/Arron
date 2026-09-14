"""Application settings: YAML configuration + .env credentials (pydantic-settings)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


class AppConfig(BaseModel):
    name: str = "Investment System"
    version: str = "1.0.0"
    host: str = "0.0.0.0"
    port: int = 8501


class DatabaseConfig(BaseModel):
    url: str = "sqlite:///data/investment.db"


class DataSourceConfig(BaseModel):
    primary: Literal["tushare", "akshare"] = "tushare"
    fallback: Literal["tushare", "akshare"] = "akshare"
    auto_failover: bool = True
    request_timeout_seconds: int = 30


class SyncConfig(BaseModel):
    market_history_years: int = 5
    financial_history_years: int = 5
    financial_freshness_days: int = 120
    market_freshness_days: int = 3


class YAMLConfig(BaseModel):
    app: AppConfig = AppConfig()
    database: DatabaseConfig = DatabaseConfig()
    data_source: DataSourceConfig = DataSourceConfig()
    sync: SyncConfig = SyncConfig()
    seed_symbols: list[str] = []


class EnvSettings(BaseSettings):
    """Credentials / environment overrides loaded from .env."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    tushare_token: str = ""
    investment_db_url: str = ""


class AppSettings:
    """Aggregated typed settings: YAML config + .env values."""

    def __init__(self, yaml_config: YAMLConfig, env: EnvSettings) -> None:
        self.yaml_config = yaml_config
        self.env = env

    @property
    def tushare_token(self) -> str:
        return self.env.tushare_token.strip()

    @property
    def database_url(self) -> str:
        if self.env.investment_db_url.strip():
            return self.env.investment_db_url.strip()
        url = self.yaml_config.database.url
        if url.startswith("sqlite:///"):
            raw = url[len("sqlite:///") :]
            p = Path(raw)
            if not p.is_absolute():
                p = PROJECT_ROOT / raw
            p.parent.mkdir(parents=True, exist_ok=True)
            return f"sqlite:///{p.as_posix()}"
        return url

    @classmethod
    def load(cls, config_dir: Path | None = None) -> "AppSettings":
        cfg_dir = config_dir or CONFIG_DIR
        app_path = cfg_dir / "app.yaml"
        raw: dict = {}
        if app_path.exists():
            with app_path.open("r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        yaml_config = YAMLConfig.model_validate(raw)
        env = EnvSettings()
        return cls(yaml_config=yaml_config, env=env)


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings.load()