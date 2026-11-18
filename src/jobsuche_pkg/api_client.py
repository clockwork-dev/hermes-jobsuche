"""Hybrid Jobsuche REST API client (`pc/v6/jobs` + `pc/v4/jobdetails`) and enrichment."""

from __future__ import annotations

import base64
import logging
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from jobsuche_pkg.config import Settings
from jobsuche_pkg.models import AusbildungOffer, SearchFilter, slugify_company

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class JobsucheAPIError(RuntimeError):
    """Base exception for Jobsuche REST API errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class JobsucheAuthError(JobsucheAPIError):
    """Raised when the Jobsuche API returns HTTP 401 or 403."""


class JobsucheNotFoundError(JobsucheAPIError):
    """Raised when the requested offer or endpoint returns HTTP 404."""


class JobsucheRateLimitError(JobsucheAPIError):
    """Raised when HTTP 429 persists after exhausting retries."""


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    text = str(value).strip()
    return text if text else None


def _first_non_empty_str(values: Any) -> str | None:
    if isinstance(values, list):
        for item in values:
            cleaned = _clean_str(item)
            if cleaned:
                return cleaned
    return None


_NON_HTTP_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d+(?:[/?#]|$))")


def _normalize_external_url(raw_url: Any) -> str | None:
    url = _clean_str(raw_url)
    if not url:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 or ch.isspace() for ch in url):
        return None
    lower_url = url.lower()
    if lower_url.startswith("http://"):
        url = f"http://{url[7:]}"
    elif lower_url.startswith("https://"):
        url = f"https://{url[8:]}"
    else:
        if _NON_HTTP_SCHEME_RE.match(url):
            return None
        authority_prefix = re.split(r"[/?#]", url, maxsplit=1)[0]
        if lower_url.startswith("www.") or "." in authority_prefix:
            url = f"https://{url.lstrip('/')}"
        else:
            return None
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return None
    host = (hostname or "").lower().rstrip(".")
    if not host or host == "arbeitsagentur.de" or host.endswith(".arbeitsagentur.de"):
        return None
    return url


def _extract_apply_url(item: dict[str, Any]) -> str | None:
    for key in ("externeURL", "externeUrl", "bewerbungsUrl", "bewerbungsurl"):
        normalized = _normalize_external_url(item.get(key))
        if normalized:
            return normalized
    return _normalize_external_url(item.get("allianzpartnerUrl"))


def _extract_location(item: dict[str, Any]) -> dict[str, Any]:
    """Extract normalized location fields from either live v6 or legacy v4 payloads."""
    adresse: dict[str, Any] = {}
    top_loc: dict[str, Any] = {}

    stellenlokationen = item.get("stellenlokationen")
    if isinstance(stellenlokationen, list) and stellenlokationen:
        first_loc = stellenlokationen[0]
        if isinstance(first_loc, dict):
            top_loc = first_loc
            nested_addr = first_loc.get("adresse")
            if isinstance(nested_addr, dict):
                adresse = nested_addr
            else:
                adresse = first_loc
    elif isinstance(item.get("arbeitsort"), dict):
        adresse = item["arbeitsort"]
        top_loc = adresse
    elif isinstance(item.get("arbeitsorte"), list) and item["arbeitsorte"]:
        first_ao = item["arbeitsorte"][0]
        if isinstance(first_ao, dict):
            adresse = first_ao
            top_loc = first_ao

    strasse = _clean_str(adresse.get("strasse"))
    hausnummer = _clean_str(adresse.get("hausnummer"))
    if strasse and hausnummer and not strasse.endswith(hausnummer):
        street: str | None = f"{strasse} {hausnummer}"
    else:
        street = strasse

    raw_distance = item.get("entfernung")
    if raw_distance is None:
        raw_distance = top_loc.get("entfernung")
    if raw_distance is None:
        raw_distance = adresse.get("entfernung")

    distance_km: float | None = None
    if raw_distance is not None and not isinstance(raw_distance, bool):
        try:
            dist_val = float(raw_distance)
            if dist_val >= 0.0:
                distance_km = dist_val
        except (TypeError, ValueError):
            distance_km = None

    return {
        "plz": _clean_str(adresse.get("plz")),
        "city": _clean_str(adresse.get("ort")),
        "street": street,
        "region": _clean_str(adresse.get("region")),
        "distance_km": distance_km,
    }


def _extract_work_mode(item: dict[str, Any]) -> str | None:
    parts: list[str] = []
    if item.get("arbeitszeitVollzeit") is True:
        parts.append("Vollzeit")
    teilzeit_keys = (
        "arbeitszeitTeilzeitFlexibel",
        "arbeitszeitTeilzeitVormittag",
        "arbeitszeitTeilzeitNachmittag",
        "arbeitszeitTeilzeitAbend",
    )
    if any(item.get(k) is True for k in teilzeit_keys):
        parts.append("Teilzeit")
    if item.get("homeofficemoeglich") is True:
        parts.append("Homeoffice möglich")
    if parts:
        return ", ".join(parts)

    legacy = item.get("arbeitszeit")
    if isinstance(legacy, list):
        joined = ", ".join(s for s in (_clean_str(x) for x in legacy) if s)
        return joined or None
    return _clean_str(legacy)


def _extract_contacts(item: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    kontakt = item.get("angebotskontakt")
    if not isinstance(kontakt, dict):
        return None, None, None

    full_name = _clean_str(kontakt.get("name"))
    if not full_name:
        anrede = _clean_str(kontakt.get("anrede"))
        vorname = _clean_str(kontakt.get("vorname"))
        nachname = _clean_str(kontakt.get("nachname"))
        name_parts = [p for p in (anrede, vorname, nachname) if p]
        full_name = " ".join(name_parts) if name_parts else None

    email = _clean_str(kontakt.get("email") or kontakt.get("eMail"))
    phone = _clean_str(
        kontakt.get("telefon") or kontakt.get("telefonnummer") or kontakt.get("mobil")
    )
    return full_name, email, phone


def _extract_start_date(item: dict[str, Any]) -> str | None:
    eintritt = item.get("eintrittszeitraum")
    if isinstance(eintritt, dict):
        von = _clean_str(eintritt.get("von"))
        if von:
            return von
    return _clean_str(item.get("eintrittsdatum"))


def _extract_published_date(item: dict[str, Any]) -> str | None:
    direct = _clean_str(item.get("datumErsteVeroeffentlichung"))
    if direct:
        return direct
    zeitraum = item.get("veroeffentlichungszeitraum")
    if isinstance(zeitraum, dict):
        von = _clean_str(zeitraum.get("von"))
        if von:
            return von
    return _clean_str(item.get("aktuelleVeroeffentlichungsdatum"))


class JobsucheAPIClient:
    """Client for Bundesagentur für Arbeit Jobsuche REST API with hybrid enrichment."""

    DEFAULT_BASE_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
    DEFAULT_API_KEY = "jobboerse-jobsuche"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        resolved_base = (
            base_url
            or (settings.api_base_url if settings is not None else None)
            or self.DEFAULT_BASE_URL
        )
        resolved_key = (
            api_key
            or (settings.api_key if settings is not None else None)
            or self.DEFAULT_API_KEY
        )
        self.base_url = resolved_base.rstrip("/")
        self.api_key = resolved_key
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff_factor = max(0.0, float(backoff_factor))
        self._sleep = sleep_fn
        self._owns_client = client is None

        default_headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "jobsuche-pkg/0.1 (+https://www.arbeitsagentur.de)",
        }
        if client is not None:
            self._client = client
        else:
            self._client = httpx.Client(
                base_url=self.base_url,
                headers=default_headers,
                timeout=self.timeout,
                transport=transport,
            )

    def __enter__(self) -> JobsucheAPIClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP client if owned by this instance."""
        if self._owns_client:
            self._client.close()

    def _build_url(self, path: str) -> str:
        clean_path = path if path.startswith("/") else f"/{path}"
        return f"{self.base_url}{clean_path}"

    def _parse_payload(self, response: httpx.Response) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def _compute_retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    seconds = float(retry_after.strip())
                    if seconds >= 0:
                        return min(seconds, 60.0)
                except ValueError:
                    pass
        return self.backoff_factor * (2**attempt)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._build_url(path)
        headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
        }
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    self._sleep(self._compute_retry_delay(None, attempt))
                    continue
                raise JobsucheAPIError(f"Request failed after retries: {exc}") from exc

            payload = self._parse_payload(response)
            status = response.status_code

            if status in (401, 403):
                raise JobsucheAuthError(
                    f"Authentication/API key rejected (HTTP {status})",
                    status_code=status,
                    payload=payload,
                )
            if status == 404:
                raise JobsucheNotFoundError(
                    f"Resource not found (HTTP 404): {path}",
                    status_code=status,
                    payload=payload,
                )
            if status in _RETRYABLE_STATUS_CODES:
                if attempt < self.max_retries:
                    self._sleep(self._compute_retry_delay(response, attempt))
                    continue
                if status == 429:
                    raise JobsucheRateLimitError(
                        "Rate limit exceeded (HTTP 429)",
                        status_code=status,
                        payload=payload,
                    )
                raise JobsucheAPIError(
                    f"Upstream server error (HTTP {status})",
                    status_code=status,
                    payload=payload,
                )
            if status >= 400:
                raise JobsucheAPIError(
                    f"API request error (HTTP {status})",
                    status_code=status,
                    payload=payload,
                )

            if not isinstance(payload, dict):
                raise JobsucheAPIError(
                    "Expected JSON object in API response",
                    status_code=status,
                    payload=payload,
                )
            return payload

        raise JobsucheAPIError(f"Unexpected request failure: {last_exc}")

    def _resolve_filter(
        self,
        search_filter: SearchFilter | None,
        kwargs: dict[str, Any],
    ) -> SearchFilter:
        raw_plz = kwargs.get("plz")
        has_plz = isinstance(raw_plz, str) and bool(raw_plz.strip())
        raw_wo = kwargs.get("wo")
        has_explicit_wo = raw_wo is not None and (
            not isinstance(raw_wo, str) or bool(raw_wo.strip())
        )

        if search_filter is not None:
            if not kwargs:
                return search_filter
            merged = search_filter.model_dump()
            if has_plz and not has_explicit_wo and (
                "wo" not in search_filter.model_fields_set
                or (search_filter.plz is not None and search_filter.wo == search_filter.plz)
            ):
                merged.pop("wo", None)
            merged.update(kwargs)
            if has_plz and raw_wo is None and "wo" in kwargs:
                merged.pop("wo", None)
            return SearchFilter.model_validate(merged)

        init_data: dict[str, Any] = {}
        if self.settings is not None:
            init_data = {
                "was": self.settings.default_was,
                "umkreis": self.settings.default_umkreis,
                "angebotsart": self.settings.default_angebotsart,
            }
            if not (has_plz and not has_explicit_wo):
                init_data["wo"] = self.settings.default_wo
        init_data.update(kwargs)
        if has_plz and raw_wo is None and "wo" in kwargs:
            init_data.pop("wo", None)
        return SearchFilter.model_validate(init_data)

    @staticmethod
    def _build_search_params(
        search_filter: SearchFilter,
        *,
        page: int,
        size: int,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "angebotsart": search_filter.angebotsart,
            "page": page,
            "size": size,
        }
        if search_filter.was and search_filter.was.strip():
            params["was"] = search_filter.was.strip()
        if search_filter.wo and search_filter.wo.strip():
            params["wo"] = search_filter.wo.strip()
            params["umkreis"] = search_filter.umkreis
        if search_filter.abdatum:
            params["abdatum"] = search_filter.abdatum
        if search_filter.veroeffentlichtseit is not None:
            params["veroeffentlichtseit"] = search_filter.veroeffentlichtseit
        return params

    @staticmethod
    def _normalize_offer_item(item: dict[str, Any]) -> AusbildungOffer:
        refnr = _clean_str(item.get("referenznummer") or item.get("refnr"))
        if not refnr:
            raise ValueError("Job item missing referenznummer/refnr")

        title = (
            _clean_str(item.get("stellenangebotsTitel"))
            or _clean_str(item.get("titel"))
            or _clean_str(item.get("title"))
            or _clean_str(item.get("hauptberuf"))
            or _clean_str(item.get("beruf"))
            or refnr
        )
        profession = (
            _clean_str(item.get("hauptberuf"))
            or _clean_str(item.get("beruf"))
            or _first_non_empty_str(item.get("alleBerufe"))
        )
        company = (
            _clean_str(item.get("firma"))
            or _clean_str(item.get("arbeitgeber"))
            or _clean_str(item.get("arbeitgeberdarstellung"))
            or "Unbekannt"
        )
        location = _extract_location(item)
        contact_name, contact_email, contact_phone = _extract_contacts(item)

        return AusbildungOffer(
            refnr=refnr,
            title=title,
            profession=profession,
            company=company,
            plz=location["plz"],
            city=location["city"],
            street=location["street"],
            region=location["region"],
            distance_km=location["distance_km"],
            start_date=_extract_start_date(item),
            published_date=_extract_published_date(item),
            modified_date=_clean_str(
                item.get("aenderungsdatum") or item.get("modifikationsTimestamp")
            ),
            education_required=_clean_str(item.get("geforderterBildungsabschluss")),
            work_mode=_extract_work_mode(item),
            description=_clean_str(
                item.get("stellenangebotsBeschreibung")
                or item.get("stellenbeschreibung")
                or item.get("beschreibung")
            ),
            contact_name=contact_name,
            contact_email=contact_email,
            contact_phone=contact_phone,
            apply_url=_extract_apply_url(item),
            raw_api=dict(item),
        )

    def search_jobs(
        self,
        search_filter: SearchFilter | None = None,
        **kwargs: Any,
    ) -> list[AusbildungOffer]:
        """Search Jobsuche `pc/v6/jobs` with pagination and v6/v4 normalization."""
        resolved = self._resolve_filter(search_filter, kwargs)
        if resolved.limit is not None and resolved.limit <= 0:
            return []

        page_size = (
            min(resolved.size, resolved.limit)
            if resolved.limit is not None
            else resolved.size
        )
        page_size = max(1, page_size)

        offers: list[AusbildungOffer] = []
        seen_refnrs: set[str] = set()
        total_seen_raw = 0
        current_page = resolved.page

        while True:
            params = self._build_search_params(resolved, page=current_page, size=page_size)
            payload = self._request_json("GET", "/pc/v6/jobs", params=params)

            raw_items = payload.get("ergebnisliste")
            if raw_items is None:
                raw_items = payload.get("stellenangebote")
            if not isinstance(raw_items, list) or not raw_items:
                break

            total_seen_raw += len(raw_items)
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    continue
                try:
                    offer = self._normalize_offer_item(raw_item)
                except ValueError as exc:
                    logger.warning("Skipping malformed jobsuche item: %s", exc)
                    continue
                if offer.refnr in seen_refnrs:
                    continue
                seen_refnrs.add(offer.refnr)
                offers.append(offer)
                if resolved.limit is not None and len(offers) >= resolved.limit:
                    return offers[: resolved.limit]

            max_results = payload.get("maxErgebnisse")
            if isinstance(max_results, int) and total_seen_raw >= max_results:
                break
            if len(raw_items) < page_size:
                break

            current_page += 1

        return offers[: resolved.limit] if resolved.limit is not None else offers

    def get_job_details(self, refnr: str) -> dict[str, Any]:
        """Fetch raw detail JSON from `pc/v4/jobdetails/{base64(refnr)}`."""
        cleaned = refnr.strip() if isinstance(refnr, str) else ""
        if not cleaned:
            raise ValueError("refnr must be a non-empty string")
        encoded = base64.b64encode(cleaned.encode("utf-8")).decode("ascii")
        return self._request_json("GET", f"/pc/v4/jobdetails/{encoded}")

    def enrich_offer(
        self,
        offer: AusbildungOffer,
        scraper: Any | None = None,
        fetch_api_details: bool = True,
    ) -> AusbildungOffer:
        """Enrich an offer with `pc/v4/jobdetails` and/or an authenticated portal scraper."""
        updated = offer.model_copy(deep=True)

        if fetch_api_details:
            try:
                details = self.get_job_details(updated.refnr)
            except JobsucheNotFoundError:
                logger.warning("Job details not found for refnr=%s (404)", updated.refnr)
            else:
                merged_raw = dict(updated.raw_api or {})
                merged_raw["details"] = details
                updated.raw_api = merged_raw

                detail_company = _clean_str(
                    details.get("firma") or details.get("arbeitgeber")
                )
                if updated.company == "Unbekannt" and detail_company:
                    updated.company = detail_company
                    updated.company_slug = slugify_company(detail_company)

                detail_desc = _clean_str(
                    details.get("stellenangebotsBeschreibung")
                    or details.get("stellenbeschreibung")
                    or details.get("beschreibung")
                )
                if detail_desc and (
                    not updated.description or len(detail_desc) > len(updated.description)
                ):
                    updated.description = detail_desc

                detail_reqs = _clean_str(details.get("anforderungen"))
                if detail_reqs and not updated.requirements:
                    updated.requirements = detail_reqs

                detail_edu = _clean_str(details.get("geforderterBildungsabschluss"))
                if detail_edu and not updated.education_required:
                    updated.education_required = detail_edu

                detail_work_mode = _extract_work_mode(details)
                if detail_work_mode and not updated.work_mode:
                    updated.work_mode = detail_work_mode

                detail_start = _extract_start_date(details)
                if detail_start and not updated.start_date:
                    updated.start_date = detail_start

                detail_pub = _extract_published_date(details)
                if detail_pub and not updated.published_date:
                    updated.published_date = detail_pub

                detail_mod = _clean_str(
                    details.get("aenderungsdatum") or details.get("modifikationsTimestamp")
                )
                if detail_mod and not updated.modified_date:
                    updated.modified_date = detail_mod

                detail_apply = _extract_apply_url(details)
                if detail_apply and not updated.apply_url:
                    updated.apply_url = detail_apply

                c_name, c_email, c_phone = _extract_contacts(details)
                if c_name and not updated.contact_name:
                    updated.contact_name = c_name
                if c_email and not updated.contact_email:
                    updated.contact_email = c_email
                if c_phone and not updated.contact_phone:
                    updated.contact_phone = c_phone

        if scraper is not None:
            portal_data = scraper.fetch_offer_details(updated.refnr)
            if isinstance(portal_data, dict):
                updated.raw_portal = dict(portal_data)

                portal_company = _clean_str(portal_data.get("company"))
                if updated.company == "Unbekannt" and portal_company:
                    updated.company = portal_company
                    updated.company_slug = slugify_company(portal_company)

                for attr in (
                    "description",
                    "requirements",
                    "company_info",
                    "contact_name",
                    "contact_email",
                    "contact_phone",
                    "education_required",
                    "work_mode",
                    "start_date",
                ):
                    incoming_val = _clean_str(portal_data.get(attr))
                    if incoming_val:
                        setattr(updated, attr, incoming_val)

                portal_apply_url = _normalize_external_url(portal_data.get("apply_url"))
                if portal_apply_url:
                    updated.apply_url = portal_apply_url

                if portal_data.get("is_vermittlungsvorschlag"):
                    updated.is_vermittlungsvorschlag = True

        return updated
