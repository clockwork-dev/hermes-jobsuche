"""Domain data models for the Jobsuche Ausbildung automation suite."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PORTAL_DETAIL_URL_TEMPLATE = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"

_GERMAN_UMLAUT_MAP = str.maketrans(
    {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
        "Ä": "Ae",
        "Ö": "Oe",
        "Ü": "Ue",
        "ẞ": "Ss",
    }
)
_PLZ_5_RE = re.compile(r"^\d{5}$")


class ApplicationStatus(str, Enum):
    """Lifecycle status of an Ausbildung offer."""

    NEW = "new"
    SHORTLISTED = "shortlisted"
    DRAFTED = "drafted"
    APPLIED = "applied"
    REJECTED = "rejected"
    IGNORED = "ignored"


def slugify_company(name: str) -> str:
    """Create a deterministic, ASCII, filesystem-safe slug for a company name.

    Transliterates German umlauts and Eszett (`ä/ö/ü/ß -> ae/oe/ue/ss`), folds
    other Latin diacritics via NFKD, replaces non-alphanumeric spans with `-`,
    and falls back to `"unbekannt"` when no alphanumeric characters remain.
    """
    if not isinstance(name, str) or not name.strip():
        return "unbekannt"

    transliterated = name.translate(_GERMAN_UMLAUT_MAP).lower()
    ascii_text = (
        unicodedata.normalize("NFKD", transliterated)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    slug = slug[:100].rstrip("-")
    return slug or "unbekannt"


class SearchFilter(BaseModel):
    """Validated query and filter parameters for Jobsuche searches."""

    model_config = ConfigDict(extra="ignore")

    was: str = "Fachinformatiker/in für Systemintegration"
    wo: str = "Aachen"
    plz: str | None = None
    umkreis: int = Field(default=25, ge=0)
    angebotsart: int = Field(default=4, ge=1)
    abdatum: str | None = None
    veroeffentlichtseit: int | None = Field(default=None, ge=0)
    page: int = Field(default=1, ge=1)
    size: int = Field(default=25, ge=1)
    limit: int | None = Field(default=None, ge=0)
    start_date_from: str | None = None
    start_date_to: str | None = None
    status: ApplicationStatus | None = None

    @field_validator("was", "wo", mode="before")
    @classmethod
    def _strip_search_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("wo", mode="after")
    @classmethod
    def _validate_numeric_wo_plz(cls, value: str) -> str:
        if value.isdigit() and not _PLZ_5_RE.match(value):
            raise ValueError("Invalid German PLZ in 'wo'; expected 5 digits")
        return value

    @field_validator("plz", mode="before")
    @classmethod
    def _validate_plz(cls, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            if not _PLZ_5_RE.match(stripped):
                raise ValueError("Invalid German PLZ; expected 5 digits")
            return stripped
        raise ValueError("PLZ must be a 5-digit string")

    @field_validator("abdatum", "start_date_from", "start_date_to", mode="before")
    @classmethod
    def _validate_iso_date(cls, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            return date.fromisoformat(stripped).isoformat()
        raise ValueError("Date filter must be an ISO YYYY-MM-DD string")

    @model_validator(mode="after")
    def _normalize_location_and_dates(self) -> SearchFilter:
        if self.plz and ("wo" not in self.model_fields_set or not self.wo.strip()):
            self.wo = self.plz
        if self.start_date_from and self.start_date_to and self.start_date_from > self.start_date_to:
            raise ValueError("start_date_from must be <= start_date_to")
        return self


class AusbildungOffer(BaseModel):
    """Normalized representation of a single Jobsuche Ausbildung offer."""

    model_config = ConfigDict(extra="ignore")

    refnr: str
    title: str
    profession: str | None = None
    company: str
    company_slug: str = ""
    plz: str | None = None
    city: str | None = None
    street: str | None = None
    region: str | None = None
    distance_km: float | None = Field(default=None, ge=0.0)
    start_date: str | None = None
    published_date: str | None = None
    modified_date: str | None = None
    education_required: str | None = None
    work_mode: str | None = None
    description: str | None = None
    requirements: str | None = None
    company_info: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    apply_url: str | None = None
    portal_url: str = ""
    is_vermittlungsvorschlag: bool = False
    score: float = Field(default=0.0, ge=0.0)
    matched_skills: list[str] = Field(default_factory=list)
    status: ApplicationStatus = ApplicationStatus.NEW
    notes: str | None = None
    raw_api: dict[str, Any] | None = None
    raw_portal: dict[str, Any] | None = None

    @field_validator("refnr", "title", mode="before")
    @classmethod
    def _require_non_empty_str(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Field must be a non-empty string")
        return value.strip()

    @field_validator("company", mode="before")
    @classmethod
    def _normalize_company(cls, value: Any) -> str:
        if value is None:
            return "Unbekannt"
        if isinstance(value, str):
            stripped = value.strip()
            return stripped if stripped else "Unbekannt"
        raise ValueError("company must be a string or None")

    @field_validator(
        "profession",
        "plz",
        "city",
        "street",
        "region",
        "start_date",
        "published_date",
        "modified_date",
        "education_required",
        "work_mode",
        "description",
        "requirements",
        "company_info",
        "contact_name",
        "contact_email",
        "contact_phone",
        "apply_url",
        "notes",
        mode="before",
    )
    @classmethod
    def _clean_optional_str(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped if stripped else None
        return str(value).strip() or None

    @field_validator("company_slug", "portal_url", mode="before")
    @classmethod
    def _clean_derived_str(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()

    @model_validator(mode="after")
    def _populate_derived_fields(self) -> AusbildungOffer:
        if not self.company_slug:
            self.company_slug = slugify_company(self.company)
        if not self.portal_url:
            self.portal_url = PORTAL_DETAIL_URL_TEMPLATE.format(refnr=self.refnr)

        deduped_skills: list[str] = []
        seen: set[str] = set()
        for skill in self.matched_skills:
            cleaned = skill.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                deduped_skills.append(cleaned)
        self.matched_skills = deduped_skills
        return self


class CompanyProfile(BaseModel):
    """Aggregated company-level summary across one or more Ausbildung offers."""

    model_config = ConfigDict(extra="ignore")

    company_slug: str = ""
    name: str
    city: str | None = None
    plz: str | None = None
    offer_count: int = Field(default=0, ge=0)
    max_score: float = 0.0
    avg_score: float = 0.0
    offers: list[str] = Field(default_factory=list)
    portal_url: str | None = None
    apply_urls: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)

    @field_validator("name", mode="before")
    @classmethod
    def _normalize_name(cls, value: Any) -> str:
        if value is None:
            return "Unbekannt"
        if isinstance(value, str):
            stripped = value.strip()
            return stripped if stripped else "Unbekannt"
        raise ValueError("name must be a string")

    @field_validator("company_slug", mode="before")
    @classmethod
    def _clean_slug(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @model_validator(mode="after")
    def _populate_slug(self) -> CompanyProfile:
        if not self.company_slug:
            self.company_slug = slugify_company(self.name)
        return self
