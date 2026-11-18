#!/usr/bin/env python3
"""Stdio JSON-RPC 2.0 MCP Server for the Jobsuche Ausbildung plugin."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from typing import Any

# Bootstrap repository src/ and .venv site-packages so `python3 mcp_server.py`
# works even when invoked with system python3 outside the virtualenv.
_PLUGIN_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PLUGIN_DIR.parents[2]
_SRC_DIR = _REPO_ROOT / "src"
if _SRC_DIR.is_dir() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

for _sp in sorted((_REPO_ROOT / ".venv" / "lib").glob("python*/site-packages"), reverse=True):
    if _sp.is_dir() and str(_sp) not in sys.path:
        sys.path.insert(0, str(_sp))

try:
    import pydantic  # noqa: F401
except ImportError:
    _venv_py = _REPO_ROOT / ".venv" / "bin" / "python3"
    if _venv_py.is_file() and Path(sys.executable).resolve() != _venv_py.resolve():
        os.execv(str(_venv_py), [str(_venv_py), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise

import yaml
from pydantic import ValidationError

from jobsuche_pkg.api_client import (
    JobsucheAPIClient,
    JobsucheAPIError,
    JobsucheNotFoundError,
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

_ALLOWED_SEARCH_MODES = {"api", "browser", "hybrid"}

_DEFAULT_FISI_KEYWORDS = [
    "Linux",
    "Debian",
    "Netzwerk",
    "Windows Server",
    "Active Directory",
    "Virtualisierung",
    "Proxmox",
    "Docker",
    "IT-Support",
    "Scripting",
    "Firewall",
]


def _load_runtime_settings() -> Settings:
    settings = get_settings()
    api_override = os.environ.get("JOBSUCHE_API_BASE_URL") or os.environ.get("API_BASE_URL")
    if api_override and api_override.strip():
        settings = settings.model_copy(update={"api_base_url": api_override.strip().rstrip("/")})
    return settings


def _fallback_score_offer(
    offer: AusbildungOffer,
    keywords: list[str] | None = None,
) -> AusbildungOffer:
    try:
        from jobsuche_pkg import sorter as _sorter  # type: ignore[import-not-found]

        if hasattr(_sorter, "score_offer"):
            return _sorter.score_offer(offer, keywords=keywords)
    except Exception:
        pass

    kw_list = keywords if keywords is not None else _DEFAULT_FISI_KEYWORDS
    haystack = " ".join(
        filter(
            None,
            [
                offer.title,
                offer.profession,
                offer.description,
                offer.requirements,
                offer.company_info,
            ],
        )
    ).lower()

    matched: list[str] = []
    for kw in kw_list:
        if kw.lower() in haystack:
            matched.append(kw)

    skill_score = min(60.0, len(matched) * 10.0)
    if "fachinformatiker" in haystack or "systemintegration" in haystack or "fisi" in haystack:
        skill_score = min(65.0, skill_score + 15.0)

    dist_score = 15.0
    if offer.distance_km is not None:
        if offer.distance_km <= 5:
            dist_score = 25.0
        elif offer.distance_km <= 15:
            dist_score = 20.0
        elif offer.distance_km <= 25:
            dist_score = 15.0
        elif offer.distance_km <= 50:
            dist_score = 5.0
        else:
            dist_score = 0.0

    date_score = 10.0 if (offer.start_date and offer.start_date.startswith("2026")) else 5.0
    total = max(0.0, min(100.0, round(skill_score + dist_score + date_score, 1)))
    return offer.model_copy(update={"score": total, "matched_skills": matched})


def _fallback_filter_offers(
    offers: list[AusbildungOffer],
    search_filter: SearchFilter,
) -> list[AusbildungOffer]:
    try:
        from jobsuche_pkg import sorter as _sorter  # type: ignore[import-not-found]

        if hasattr(_sorter, "filter_offers"):
            return _sorter.filter_offers(offers, search_filter)
    except Exception:
        pass

    result: list[AusbildungOffer] = []
    for o in offers:
        if search_filter.status is not None and o.status != search_filter.status:
            continue
        if o.distance_km is not None and o.distance_km > float(search_filter.umkreis):
            continue
        if search_filter.start_date_from and (
            not o.start_date or o.start_date < search_filter.start_date_from
        ):
            continue
        if search_filter.start_date_to and (
            not o.start_date or o.start_date > search_filter.start_date_to
        ):
            continue
        result.append(o)
    return result


def _fallback_export_offers(
    offers: list[AusbildungOffer],
    export_dir: Path,
) -> dict[str, Any]:
    try:
        from jobsuche_pkg import exporter as _exporter  # type: ignore[import-not-found]

        if hasattr(_exporter, "export_offers"):
            return _exporter.export_offers(offers, export_dir)
    except Exception:
        pass

    export_dir = Path(export_dir)
    offers_dir = export_dir / "offers"
    offers_dir.mkdir(parents=True, exist_ok=True)

    sorted_offers = sorted(
        offers,
        key=lambda x: (-float(x.score), x.distance_km if x.distance_km is not None else 9999.0, x.refnr),
    )

    for offer in sorted_offers:
        safe_slug = slugify_company(offer.company_slug or offer.company)
        safe_ref = re.sub(r"[^a-zA-Z0-9._-]+", "_", offer.refnr).strip("._-") or "offer"
        folder = offers_dir / f"{safe_slug}__{safe_ref}"
        folder.mkdir(parents=True, exist_ok=True)

        frontmatter = {
            "refnr": offer.refnr,
            "title": offer.title,
            "profession": offer.profession,
            "company": offer.company,
            "plz": offer.plz,
            "city": offer.city,
            "distance_km": offer.distance_km,
            "start_date": offer.start_date,
            "contact_name": offer.contact_name,
            "contact_email": offer.contact_email,
            "contact_phone": offer.contact_phone,
            "apply_url": offer.apply_url,
            "portal_url": offer.portal_url,
            "score": float(offer.score),
            "matched_skills": list(offer.matched_skills),
            "status": offer.status.value,
        }
        fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
        md_lines = [
            "---",
            fm_yaml,
            "---",
            "",
            f"# {offer.title} — {offer.company}",
            "",
            "## Описание и задачи",
            offer.description or "Описание не указано.",
            "",
            "## Требования к кандидату",
            offer.requirements or offer.education_required or "Требования не указаны.",
            "",
            "## О компании",
            offer.company_info or f"{offer.company} ({offer.plz or ''} {offer.city or ''})".strip(),
            "",
            "## Контакты для Bewerbung",
            f"- Контактное лицо: {offer.contact_name or 'Не указано'}",
            f"- Email: {offer.contact_email or 'Не указан'}",
            f"- Телефон: {offer.contact_phone or 'Не указан'}",
            f"- Ссылка на отклик: {offer.apply_url or offer.portal_url}",
            "",
            "## Подсказки для ИИ при генерации Anschreiben",
            f"- Ключевые навыки: {', '.join(offer.matched_skills) if offer.matched_skills else 'FISI / Systemintegration'}",
            f"- Referenznummer: {offer.refnr}",
            "",
        ]
        (folder / "offer.md").write_text("\n".join(md_lines), encoding="utf-8")
        (folder / "offer.json").write_text(
            json.dumps(offer.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    grouped: dict[str, list[AusbildungOffer]] = {}
    for offer in sorted_offers:
        slug = offer.company_slug or slugify_company(offer.company)
        grouped.setdefault(slug, []).append(offer)

    profiles: list[dict[str, Any]] = []
    for slug, comp_offers in grouped.items():
        scores = [float(o.score) for o in comp_offers]
        cities = list(dict.fromkeys(o.city for o in comp_offers if o.city))
        apply_urls = list(dict.fromkeys(o.apply_url for o in comp_offers if o.apply_url))
        profile = CompanyProfile(
            company_slug=slug,
            name=comp_offers[0].company,
            city=comp_offers[0].city,
            plz=comp_offers[0].plz,
            offer_count=len(comp_offers),
            max_score=max(scores) if scores else 0.0,
            avg_score=round(sum(scores) / len(scores), 2) if scores else 0.0,
            offers=[o.refnr for o in comp_offers],
            portal_url=comp_offers[0].portal_url,
            apply_urls=apply_urls,
            cities=cities,
        )
        profiles.append(profile.model_dump(mode="json"))

    profiles.sort(key=lambda p: (-float(p["max_score"]), -int(p["offer_count"]), p["company_slug"]))
    (export_dir / "companies_summary.json").write_text(
        json.dumps({"companies": profiles, "total_companies": len(profiles)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    idx_lines = [
        "# INDEX FOR LLM — Ausbildung Offers",
        "",
        "| Score | RefNr | Company | Title | City | Start Date | Status |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for o in sorted_offers:
        idx_lines.append(
            f"| {o.score:.1f} | {o.refnr} | {o.company} | {o.title} | {o.city or '-'} | {o.start_date or '-'} | {o.status.value} |"
        )
    idx_lines.append("")
    (export_dir / "INDEX_FOR_LLM.md").write_text("\n".join(idx_lines), encoding="utf-8")

    return {
        "exported_offers": len(sorted_offers),
        "exported_companies": len(profiles),
        "export_dir": str(export_dir),
        "index_path": str(export_dir / "INDEX_FOR_LLM.md"),
        "companies_summary_path": str(export_dir / "companies_summary.json"),
    }


TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "name": "jobsuche_search_ausbildung",
        "description": (
            "Search Bundesagentur für Arbeit (arbeitsagentur.de) for Ausbildung offers "
            "(default: Fachinformatiker/in für Systemintegration in Aachen, 25 km radius)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "was": {
                    "type": "string",
                    "description": "Profession or search query (default: Fachinformatiker/in für Systemintegration).",
                },
                "wo": {
                    "type": "string",
                    "description": "City or 5-digit PLZ (default: Aachen).",
                },
                "plz": {
                    "type": "string",
                    "description": "Optional 5-digit German postal code.",
                },
                "umkreis": {
                    "type": "integer",
                    "description": "Search radius in km (>= 0, default: 25).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of offers to return.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["api", "browser", "hybrid"],
                    "description": "Discovery mode: api (default), browser, or hybrid.",
                },
                "abdatum": {
                    "type": "string",
                    "description": "Optional start date filter (YYYY-MM-DD).",
                },
                "persist": {
                    "type": "boolean",
                    "description": "Whether to persist discovered offers into SQLite (default: true).",
                },
            },
            "additionalProperties": True,
        },
    },
    {
        "name": "jobsuche_fetch_offer_details",
        "description": "Fetch full description, requirements, and employer contact details for a Jobsuche refnr.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "refnr": {
                    "type": "string",
                    "description": "Unique Jobsuche Referenznummer (e.g. 10000-1206415041-S).",
                },
                "mode": {
                    "type": "string",
                    "enum": ["api", "browser", "hybrid"],
                    "description": "Enrichment mode: api (default), browser, or hybrid.",
                },
                "persist": {
                    "type": "boolean",
                    "description": "Whether to update the offer in SQLite if present (default: true).",
                },
            },
            "required": ["refnr"],
            "additionalProperties": True,
        },
    },
    {
        "name": "jobsuche_sync_and_export",
        "description": (
            "Synchronize Ausbildung offers from Jobsuche API into SQLite, score and group by company, "
            "and export LLM-ready Markdown/JSON packages to exports/."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "was": {
                    "type": "string",
                    "description": "Profession search term.",
                },
                "wo": {
                    "type": "string",
                    "description": "City or postal code (default: Aachen).",
                },
                "plz": {
                    "type": "string",
                    "description": "Optional 5-digit postal code.",
                },
                "umkreis": {
                    "type": "integer",
                    "description": "Radius in km (>= 0, default: 25).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional maximum offers to sync.",
                },
                "enrich_details": {
                    "type": "boolean",
                    "description": "Fetch pc/v4/jobdetails for each offer before exporting (default: true).",
                },
                "export_dir": {
                    "type": "string",
                    "description": "Optional override path for exports directory.",
                },
                "db_path": {
                    "type": "string",
                    "description": "Optional override path for SQLite database.",
                },
            },
            "additionalProperties": True,
        },
    },
    {
        "name": "jobsuche_list_offers",
        "description": "List and filter tracked Ausbildung offers stored in SQLite.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["new", "shortlisted", "drafted", "applied", "rejected", "ignored"],
                    "description": "Filter by application status.",
                },
                "company": {
                    "type": "string",
                    "description": "Filter by company name or slug.",
                },
                "city": {
                    "type": "string",
                    "description": "Filter by city name.",
                },
                "min_score": {
                    "type": "number",
                    "description": "Filter by minimum relevance score (0-100).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of offers to return.",
                },
                "db_path": {
                    "type": "string",
                    "description": "Optional override path for SQLite database.",
                },
            },
            "additionalProperties": True,
        },
    },
    {
        "name": "jobsuche_update_offer_status",
        "description": "Update an offer's application status (new, shortlisted, drafted, applied, rejected, ignored) and notes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "refnr": {
                    "type": "string",
                    "description": "Offer Referenznummer to update.",
                },
                "status": {
                    "type": "string",
                    "enum": ["new", "shortlisted", "drafted", "applied", "rejected", "ignored"],
                    "description": "New application lifecycle status.",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional notes for history tracking.",
                },
                "db_path": {
                    "type": "string",
                    "description": "Optional override path for SQLite database.",
                },
            },
            "required": ["refnr", "status"],
            "additionalProperties": True,
        },
    },
    {
        "name": "jobsuche_auth_status",
        "description": "Check Bundesagentur für Arbeit session state, credentials, and 6-digit TOTP generator readiness.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "test_totp": {
                    "type": "boolean",
                    "description": "Verify TOTP secret can generate a valid 6-digit code (default: true).",
                },
            },
            "additionalProperties": True,
        },
    },
]


def _handle_search_ausbildung(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    settings = _load_runtime_settings()
    mode = str(args.get("mode", "api") or "api").strip().lower()
    if mode not in _ALLOWED_SEARCH_MODES:
        return (
            {
                "ok": False,
                "error": f"Invalid search mode {mode!r}; expected one of {sorted(_ALLOWED_SEARCH_MODES)}",
            },
            True,
        )

    filter_kwargs: dict[str, Any] = {
        "was": args.get("was", settings.default_was),
        "wo": args.get("wo", settings.default_wo),
        "umkreis": args.get("umkreis", settings.default_umkreis),
        "angebotsart": args.get("angebotsart", settings.default_angebotsart),
    }
    for optional_key in ("plz", "abdatum", "start_date_from", "start_date_to", "limit", "page", "size"):
        if optional_key in args and args[optional_key] is not None:
            filter_kwargs[optional_key] = args[optional_key]

    try:
        search_filter = SearchFilter.model_validate(filter_kwargs)
    except (ValidationError, ValueError, TypeError) as exc:
        return ({"ok": False, "error": f"Invalid search parameters: {exc}"}, True)

    try:
        with JobsucheAPIClient(settings) as client:
            raw_offers = client.search_jobs(search_filter)
            scored = [_fallback_score_offer(o) for o in raw_offers]
    except JobsucheAPIError as exc:
        return ({"ok": False, "error": str(exc)}, True)
    except Exception as exc:
        return ({"ok": False, "error": f"Search failed: {exc}"}, True)

    if args.get("persist", True):
        db_path = Path(str(args["db_path"])) if args.get("db_path") else settings.db_path
        with JobDatabase(db_path) as db:
            counts = db.upsert_offers(scored)
            db.record_search_run(
                search_filter.model_dump(mode="json", exclude_none=True),
                total_found=len(scored),
                new_count=counts["inserted"],
            )

    return (
        {
            "ok": True,
            "mode": mode,
            "count": len(scored),
            "offers": [o.model_dump(mode="json", exclude={"raw_api", "raw_portal"}) for o in scored],
        },
        False,
    )


def _handle_fetch_offer_details(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    refnr = str(args.get("refnr") or "").strip()
    if not refnr:
        return ({"ok": False, "error": "Parameter 'refnr' is required and must be non-empty."}, True)

    mode = str(args.get("mode", "api") or "api").strip().lower()
    if mode not in _ALLOWED_SEARCH_MODES:
        return ({"ok": False, "error": f"Invalid mode {mode!r}"}, True)

    settings = _load_runtime_settings()
    db_path = Path(str(args["db_path"])) if args.get("db_path") else settings.db_path

    try:
        with JobsucheAPIClient(settings) as client:
            details = client.get_job_details(refnr)
            if not details:
                return ({"ok": False, "error": f"Offer details not found for refnr {refnr!r}"}, True)

            with JobDatabase(db_path) as db:
                existing = db.get_offer(refnr)

            if existing is not None:
                base_offer = existing
            else:
                payload_with_ref = dict(details)
                payload_with_ref.setdefault("referenznummer", refnr)
                base_offer = client._normalize_offer_item(payload_with_ref)

            scraper = None
            if mode in ("browser", "hybrid"):
                try:
                    from jobsuche_pkg.portal_scraper import PortalScraper  # type: ignore[import-not-found]

                    if settings.has_ba_credentials() or settings.storage_state_path.is_file():
                        scraper = PortalScraper(settings)
                except Exception:
                    scraper = None

            enriched = client.enrich_offer(base_offer, scraper=scraper, fetch_api_details=True)
            scored = _fallback_score_offer(enriched)

            if args.get("persist", True):
                with JobDatabase(db_path) as db:
                    db.upsert_offer(scored)
                    persisted = db.get_offer(refnr)
                    if persisted is not None:
                        scored = persisted
    except JobsucheNotFoundError as exc:
        return ({"ok": False, "error": str(exc), "refnr": refnr}, True)
    except JobsucheAPIError as exc:
        return ({"ok": False, "error": str(exc), "refnr": refnr}, True)
    except Exception as exc:
        return ({"ok": False, "error": f"Failed to fetch offer details: {exc}", "refnr": refnr}, True)

    return (
        {
            "ok": True,
            "refnr": refnr,
            "offer": scored.model_dump(mode="json", exclude={"raw_api", "raw_portal"}),
            "details": details,
        },
        False,
    )


def _handle_sync_and_export(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    settings = _load_runtime_settings()
    filter_kwargs: dict[str, Any] = {
        "was": args.get("was", settings.default_was),
        "wo": args.get("wo", settings.default_wo),
        "umkreis": args.get("umkreis", settings.default_umkreis),
        "angebotsart": args.get("angebotsart", settings.default_angebotsart),
    }
    for optional_key in ("plz", "abdatum", "start_date_from", "start_date_to", "limit", "page", "size"):
        if optional_key in args and args[optional_key] is not None:
            filter_kwargs[optional_key] = args[optional_key]

    try:
        search_filter = SearchFilter.model_validate(filter_kwargs)
    except (ValidationError, ValueError, TypeError) as exc:
        return ({"ok": False, "error": f"Invalid sync filter parameters: {exc}"}, True)

    db_path = Path(str(args["db_path"])) if args.get("db_path") else settings.db_path
    export_dir = Path(str(args["export_dir"])) if args.get("export_dir") else settings.export_dir
    enrich_details = bool(args.get("enrich_details", True))

    try:
        with JobsucheAPIClient(settings) as client:
            fetched = client.search_jobs(search_filter)
            processed: list[AusbildungOffer] = []
            for offer in fetched:
                item = client.enrich_offer(offer, fetch_api_details=True) if enrich_details else offer
                processed.append(_fallback_score_offer(item))

        with JobDatabase(db_path) as db:
            counts = db.upsert_offers(processed)
            db.record_search_run(
                search_filter.model_dump(mode="json", exclude_none=True),
                total_found=len(processed),
                new_count=counts["inserted"],
            )
            stored_offers = db.list_offers()

        filtered = _fallback_filter_offers(stored_offers, search_filter)
        export_summary = _fallback_export_offers(filtered, export_dir)
    except Exception as exc:
        return ({"ok": False, "error": f"Sync and export failed: {exc}"}, True)

    return (
        {
            "ok": True,
            "synced_count": len(processed),
            "db_counts": counts,
            "exported_count": len(filtered),
            "export_dir": str(export_dir),
            "export": export_summary,
        },
        False,
    )


def _handle_list_offers(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    settings = _load_runtime_settings()
    db_path = Path(str(args["db_path"])) if args.get("db_path") else settings.db_path

    status_arg = args.get("status")
    status_enum: ApplicationStatus | None = None
    if status_arg is not None and str(status_arg).strip():
        try:
            status_enum = ApplicationStatus(str(status_arg).strip().lower())
        except ValueError:
            return ({"ok": False, "error": f"Invalid status filter: {status_arg!r}"}, True)

    company = str(args["company"]).strip() if args.get("company") else None
    city = str(args["city"]).strip() if args.get("city") else None

    min_score: float | None = None
    if args.get("min_score") is not None:
        try:
            min_score = float(args["min_score"])
        except (TypeError, ValueError):
            return ({"ok": False, "error": f"Invalid min_score: {args.get('min_score')!r}"}, True)

    limit: int | None = None
    if args.get("limit") is not None:
        try:
            limit = int(args["limit"])
            if limit < 0:
                raise ValueError("limit must be >= 0")
        except (TypeError, ValueError) as exc:
            return ({"ok": False, "error": f"Invalid limit: {exc}"}, True)

    try:
        with JobDatabase(db_path) as db:
            offers = db.list_offers(
                status=status_enum,
                company=company,
                city=city,
                min_score=min_score,
                limit=limit,
            )
    except Exception as exc:
        return ({"ok": False, "error": f"Failed to list offers: {exc}"}, True)

    return (
        {
            "ok": True,
            "count": len(offers),
            "offers": [o.model_dump(mode="json", exclude={"raw_api", "raw_portal"}) for o in offers],
        },
        False,
    )


def _handle_update_offer_status(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    refnr = str(args.get("refnr") or "").strip()
    status_raw = str(args.get("status") or "").strip()
    if not refnr or not status_raw:
        return ({"ok": False, "error": "Both 'refnr' and 'status' are required."}, True)

    try:
        status_enum = ApplicationStatus(status_raw.lower())
    except ValueError:
        return (
            {
                "ok": False,
                "error": f"Invalid status {status_raw!r}. Must be one of {[s.value for s in ApplicationStatus]}.",
            },
            True,
        )

    notes = args.get("notes")
    settings = _load_runtime_settings()
    db_path = Path(str(args["db_path"])) if args.get("db_path") else settings.db_path

    try:
        with JobDatabase(db_path) as db:
            updated = db.update_status(refnr, status_enum, notes=notes)
    except KeyError as exc:
        return ({"ok": False, "error": f"Offer not found: {refnr} ({exc})"}, True)
    except ValueError as exc:
        return ({"ok": False, "error": str(exc)}, True)
    except Exception as exc:
        return ({"ok": False, "error": f"Failed to update offer status: {exc}"}, True)

    return (
        {
            "ok": True,
            "refnr": updated.refnr,
            "status": updated.status.value,
            "notes": updated.notes,
            "offer": updated.model_dump(mode="json", exclude={"raw_api", "raw_portal"}),
        },
        False,
    )


def _handle_auth_status(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    settings = _load_runtime_settings()
    browser_status: dict[str, Any] = {}
    try:
        from jobsuche_pkg.browser import StealthBrowserManager  # type: ignore[import-not-found]

        browser_status = StealthBrowserManager(settings).get_auth_status()
    except Exception:
        storage_exists = settings.storage_state_path.is_file()
        session_valid = False
        if storage_exists:
            try:
                raw = json.loads(settings.storage_state_path.read_text(encoding="utf-8"))
                session_valid = isinstance(raw, dict) and bool(raw.get("cookies"))
            except Exception:
                session_valid = False
        browser_status = {
            "storage_state_exists": storage_exists,
            "has_session": session_valid,
            "authenticated": session_valid,
            "session_valid": session_valid,
        }

    totp_valid = False
    totp_remaining: float | None = None
    if settings.has_totp() and args.get("test_totp", True):
        try:
            import pyotp

            secret = (settings.ba_totp_secret or "").strip().replace(" ", "")
            code = pyotp.TOTP(secret).now()
            totp_valid = bool(re.fullmatch(r"\d{6}", code))
        except Exception:
            totp_valid = False

    has_session = bool(
        browser_status.get("has_session")
        or browser_status.get("authenticated")
        or browser_status.get("session_valid")
    )
    return (
        {
            "ok": True,
            "authenticated": has_session,
            "has_session": has_session,
            "session_valid": has_session,
            "storage_state_exists": bool(browser_status.get("storage_state_exists", settings.storage_state_path.is_file())),
            "storage_state_path": str(settings.storage_state_path),
            "has_ba_credentials": settings.has_ba_credentials(),
            "has_totp": settings.has_totp(),
            "totp_valid": totp_valid,
            "totp_remaining_seconds": totp_remaining,
            "has_imap_config": settings.has_imap_config(),
        },
        False,
    )


_TOOL_HANDLERS = {
    "jobsuche_search_ausbildung": _handle_search_ausbildung,
    "jobsuche_fetch_offer_details": _handle_fetch_offer_details,
    "jobsuche_sync_and_export": _handle_sync_and_export,
    "jobsuche_list_offers": _handle_list_offers,
    "jobsuche_update_offer_status": _handle_update_offer_status,
    "jobsuche_auth_status": _handle_auth_status,
}


def _send_jsonrpc(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle_request(req: dict[str, Any]) -> None:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params")

    # Notifications have no "id" and must not receive a response
    if "id" not in req:
        return

    if method == "initialize":
        client_proto = "2024-11-05"
        if isinstance(params, dict) and isinstance(params.get("protocolVersion"), str):
            client_proto = params["protocolVersion"]
        _send_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": client_proto,
                    "capabilities": {
                        "tools": {"listChanged": False},
                    },
                    "serverInfo": {
                        "name": "jobsuche",
                        "version": "0.1.0",
                    },
                },
            }
        )
        return

    if method == "ping":
        _send_jsonrpc({"jsonrpc": "2.0", "id": req_id, "result": {}})
        return

    if method == "tools/list":
        _send_jsonrpc({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS_SCHEMA}})
        return

    if method == "resources/list":
        _send_jsonrpc({"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}})
        return

    if method == "prompts/list":
        _send_jsonrpc({"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}})
        return

    if method == "tools/call":
        if not isinstance(params, dict):
            _send_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": "Invalid params: expected object"},
                }
            )
            return

        tool_name = params.get("name")
        raw_args = params.get("arguments")
        if raw_args is None:
            tool_args: dict[str, Any] = {}
        elif isinstance(raw_args, dict):
            tool_args = raw_args
        else:
            _send_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {"ok": False, "error": "Tool arguments must be a JSON object"},
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                        "isError": True,
                    },
                }
            )
            return

        handler = _TOOL_HANDLERS.get(str(tool_name))
        if handler is None:
            _send_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {"ok": False, "error": f"Unknown tool: {tool_name!r}"},
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                        "isError": True,
                    },
                }
            )
            return

        try:
            payload, is_error = handler(tool_args)
        except Exception as exc:
            payload = {"ok": False, "error": f"Unhandled tool error: {exc}"}
            is_error = True

        _send_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(payload, ensure_ascii=False),
                        }
                    ],
                    "isError": bool(is_error),
                },
            }
        )
        return

    _send_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    )


def main() -> None:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            _send_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
            )
            continue

        if isinstance(msg, dict):
            _handle_request(msg)
        elif isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict):
                    _handle_request(item)


if __name__ == "__main__":
    main()
