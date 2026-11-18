"""Core package for Bundesagentur für Arbeit Jobsuche Ausbildung automation."""

from jobsuche_pkg.api_client import (
    JobsucheAPIClient,
    JobsucheAPIError,
    JobsucheAuthError,
    JobsucheNotFoundError,
    JobsucheRateLimitError,
)
from jobsuche_pkg.config import Settings, get_settings
from jobsuche_pkg.db import JobDatabase
from jobsuche_pkg.models import (
    ApplicationStatus,
    AusbildungOffer,
    CompanyProfile,
    SearchFilter,
    slugify_company,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "ApplicationStatus",
    "AusbildungOffer",
    "CompanyProfile",
    "JobDatabase",
    "JobsucheAPIClient",
    "JobsucheAPIError",
    "JobsucheAuthError",
    "JobsucheNotFoundError",
    "JobsucheRateLimitError",
    "SearchFilter",
    "Settings",
    "get_settings",
    "slugify_company",
]
