"""Configuration loading for the Jobsuche Ausbildung automation suite."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_OPTIONAL_STR_FIELDS = {
    "ba_username",
    "ba_password",
    "ba_totp_secret",
    "ba_imap_host",
    "ba_imap_user",
    "ba_imap_password",
    "browser_executable_path",
    "browser_cdp_url",
}

_DEFAULTED_FIELDS = {
    "ba_imap_port",
    "ba_imap_folder",
    "db_path",
    "export_dir",
    "storage_state_path",
    "screenshot_dir",
    "default_was",
    "default_wo",
    "default_umkreis",
    "default_angebotsart",
    "api_base_url",
    "api_key",
    "headless",
}


class Settings(BaseSettings):
    """Application settings loaded from `.env` and environment variables.

    Missing `.env` files or missing `BA_*` portal credentials never fail
    initialization, allowing unauthenticated REST API searches and exports.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
    )

    # Bundesagentur fuer Arbeit portal credentials (optional for public API search)
    ba_username: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BA_USERNAME", "ba_username"),
    )
    ba_password: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BA_PASSWORD", "ba_password"),
    )
    ba_totp_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BA_TOTP_SECRET", "ba_totp_secret"),
    )

    # Optional IMAP fallback for email OTP codes
    ba_imap_host: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BA_IMAP_HOST", "ba_imap_host"),
    )
    ba_imap_port: int = Field(
        default=993,
        ge=1,
        le=65535,
        validation_alias=AliasChoices("BA_IMAP_PORT", "ba_imap_port"),
    )
    ba_imap_user: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BA_IMAP_USER", "ba_imap_user"),
    )
    ba_imap_password: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BA_IMAP_PASSWORD", "ba_imap_password"),
    )
    ba_imap_folder: str = Field(
        default="INBOX",
        validation_alias=AliasChoices("BA_IMAP_FOLDER", "ba_imap_folder"),
    )

    # Local storage & export paths
    db_path: Path = Field(
        default=Path("data/jobsuche.db"),
        validation_alias=AliasChoices("JOBSUCHE_DB_PATH", "db_path"),
    )
    export_dir: Path = Field(
        default=Path("exports"),
        validation_alias=AliasChoices("JOBSUCHE_EXPORT_DIR", "export_dir"),
    )
    storage_state_path: Path = Field(
        default=Path("data/storage_state.json"),
        validation_alias=AliasChoices("JOBSUCHE_STORAGE_STATE", "storage_state_path"),
    )
    screenshot_dir: Path = Field(
        default=Path("data/screenshots"),
        validation_alias=AliasChoices("JOBSUCHE_SCREENSHOT_DIR", "screenshot_dir"),
    )

    # Search & REST API defaults
    default_was: str = Field(
        default="Fachinformatiker/in für Systemintegration",
        validation_alias=AliasChoices("JOBSUCHE_DEFAULT_WAS", "default_was"),
    )
    default_wo: str = Field(
        default="Aachen",
        validation_alias=AliasChoices("JOBSUCHE_DEFAULT_CITY", "JOBSUCHE_DEFAULT_WO", "default_wo"),
    )
    default_umkreis: int = Field(
        default=25,
        ge=0,
        validation_alias=AliasChoices(
            "JOBSUCHE_DEFAULT_RADIUS", "JOBSUCHE_DEFAULT_UMKREIS", "default_umkreis"
        ),
    )
    default_angebotsart: int = Field(
        default=4,
        ge=1,
        validation_alias=AliasChoices("JOBSUCHE_DEFAULT_ANGEBOTSART", "default_angebotsart"),
    )
    api_base_url: str = Field(
        default="https://rest.arbeitsagentur.de/jobboerse/jobsuche-service",
        validation_alias=AliasChoices("JOBSUCHE_API_BASE_URL", "api_base_url"),
    )
    api_key: str = Field(
        default="jobboerse-jobsuche",
        validation_alias=AliasChoices("JOBSUCHE_API_KEY", "api_key"),
    )

    # Browser automation settings
    browser_executable_path: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BROWSER_EXECUTABLE_PATH", "browser_executable_path"),
    )
    browser_cdp_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BROWSER_CDP_URL", "browser_cdp_url"),
    )
    headless: bool = Field(
        default=True,
        validation_alias=AliasChoices("HEADLESS", "headless"),
    )

    @field_validator(*sorted(_OPTIONAL_STR_FIELDS), mode="before")
    @classmethod
    def _empty_optional_str_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped if stripped else None
        return value

    @field_validator(*sorted(_DEFAULTED_FIELDS), mode="before")
    @classmethod
    def _blank_str_to_default(cls, value: Any, info: ValidationInfo) -> Any:
        if isinstance(value, str) and not value.strip():
            field_info = cls.model_fields.get(info.field_name or "")
            if field_info is not None:
                return field_info.default
        return value

    @field_validator("api_base_url", mode="after")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    def has_ba_credentials(self) -> bool:
        """Return True if BA portal username and password are both configured."""
        return bool(self.ba_username and self.ba_password)

    def has_totp(self) -> bool:
        """Return True if a TOTP secret is configured."""
        return bool(self.ba_totp_secret)

    def has_imap_config(self) -> bool:
        """Return True if IMAP OTP fallback settings are configured."""
        return bool(self.ba_imap_host and self.ba_imap_user and self.ba_imap_password)

    def ensure_directories(self) -> None:
        """Create configured storage, export, and screenshot directories on demand."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)


def get_settings(env_file: Path | str | None = None) -> Settings:
    """Load fresh Settings without caching so env overrides and tests take effect.

    Nonexistent `.env` paths are ignored silently by pydantic-settings.
    """
    if env_file is None or (isinstance(env_file, str) and not env_file.strip()):
        return Settings()
    return Settings(_env_file=env_file)
