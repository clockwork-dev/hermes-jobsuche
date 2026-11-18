"""Hermes Agent native standalone v2 plugin for Bundesagentur für Arbeit Jobsuche Ausbildung automation."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parent
_SKILL_MD_PATH = _PLUGIN_ROOT / "skills" / "jobsuche" / "SKILL.md"
_SKILL_DESCRIPTION = "Search, monitor, and export BA Jobsuche Ausbildung offers."
_ALLOWED_FORMATS = ("json", "telegram", "markdown")
_ALLOWED_STATUSES = ("new", "shortlisted", "drafted", "applied", "rejected", "ignored")
_CANDIDATE_PROJECT_ROOTS = (
    _PLUGIN_ROOT,
    _PLUGIN_ROOT.parents[2] if (_PLUGIN_ROOT.parents[2] / "src" / "jobsuche_pkg").is_dir() else Path.home() / "FISI-Bewerbung",
    Path.home() / "FISI-Bewerbung",
    Path("/opt/jobsuche-test"),
)


def _ensure_jobsuche_pkg_on_path() -> None:
    """Ensure `jobsuche_pkg` and its pure-Python dependencies are importable in Hermes's runtime."""
    roots: list[Path] = []
    env_root = os.environ.get("JOBSUCHE_PROJECT_ROOT", "").strip()
    if env_root:
        roots.append(Path(env_root).expanduser())
    roots.extend(_CANDIDATE_PROJECT_ROOTS)
    try:
        cwd = Path.cwd()
        if cwd not in roots:
            roots.append(cwd)
    except OSError:
        pass

    if importlib.util.find_spec("jobsuche_pkg") is None:
        for root in roots:
            src_dir = root / "src"
            if (src_dir / "jobsuche_pkg").is_dir():
                src_str = str(src_dir)
                if src_str not in sys.path:
                    sys.path.insert(0, src_str)
                break

    if (
        importlib.util.find_spec("pydantic_settings") is None
        or importlib.util.find_spec("pyotp") is None
    ):
        for root in roots:
            venv_lib = root / ".venv" / "lib"
            if venv_lib.is_dir():
                for site_pkg in sorted(venv_lib.glob("python*/site-packages"), reverse=True):
                    site_str = str(site_pkg)
                    if site_pkg.is_dir() and site_str not in sys.path:
                        sys.path.append(site_str)


try:
    _ensure_jobsuche_pkg_on_path()
except Exception:
    pass


class PluginValidationError(ValueError):
    """Raised when tool arguments fail validation."""


def _search_properties() -> dict[str, Any]:
    return {
        "was": {
            "type": "string",
            "description": (
                "Search profession or keywords (default: 'Fachinformatiker/in für Systemintegration')."
            ),
        },
        "query": {
            "type": "string",
            "description": "Alias for 'was' search keywords.",
        },
        "wo": {
            "type": "string",
            "description": "Target city or 5-digit German postal code (default: 'Aachen').",
        },
        "city": {
            "type": "string",
            "description": "Alias for 'wo' city name.",
        },
        "plz": {
            "type": "string",
            "description": "Optional 5-digit German postal code (PLZ, e.g. '52062').",
        },
        "postal_code": {
            "type": "string",
            "description": "Alias for 'plz' 5-digit German postal code.",
        },
        "umkreis": {
            "type": "integer",
            "minimum": 0,
            "default": 25,
            "description": "Search radius in km around 'wo'/'plz' (>= 0, default: 25).",
        },
        "radius": {
            "type": "integer",
            "minimum": 0,
            "description": "Alias for 'umkreis' search radius in km (>= 0).",
        },
        "angebotsart": {
            "type": "integer",
            "minimum": 1,
            "default": 4,
            "description": "Offer type code (4 = Ausbildung / Duales Studium).",
        },
        "abdatum": {
            "type": "string",
            "description": "Optional earliest start date filter in ISO YYYY-MM-DD format.",
        },
        "start_date_from": {
            "type": "string",
            "description": "Optional earliest Ausbildungsbeginn date (YYYY-MM-DD).",
        },
        "start_date_to": {
            "type": "string",
            "description": "Optional latest Ausbildungsbeginn date (YYYY-MM-DD).",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "default": 25,
            "description": "Maximum number of offers to return (>= 1, default: 25).",
        },
        "min_score": {
            "type": "number",
            "minimum": 0,
            "maximum": 100,
            "description": "Optional minimum FISI relevance score (0-100).",
        },
        "enrich_details": {
            "type": "boolean",
            "default": False,
            "description": "Fetch full offer details from pc/v4/jobdetails for each offer.",
        },
    }


SEARCH_SCHEMA: dict[str, Any] = {
    "name": "jobsuche_search",
    "description": (
        "Search Bundesagentur für Arbeit (arbeitsagentur.de) Jobsuche for Ausbildung offers "
        "(default: Fachinformatiker/in für Systemintegration in Aachen +25 km), score relevance, "
        "group by company, and return structured JSON."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            **_search_properties(),
            "save_to_db": {
                "type": "boolean",
                "default": False,
                "description": "Optionally persist discovered offers into the SQLite database.",
            },
            "db_path": {
                "type": "string",
                "description": "Optional SQLite database path override when save_to_db is true.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}

MONITOR_SCHEMA: dict[str, Any] = {
    "name": "jobsuche_monitor",
    "description": (
        "Search Bundesagentur für Arbeit Jobsuche for Ausbildung offers, deduplicate against "
        "SQLite storage by Referenznummer (refnr), and return newly discovered offers as JSON "
        "with an optional Telegram Markdown digest."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            **_search_properties(),
            "format": {
                "type": "string",
                "enum": list(_ALLOWED_FORMATS),
                "default": "json",
                "description": "Output format: 'json' (default), 'telegram', or 'markdown'.",
            },
            "dry_run": {
                "type": "boolean",
                "default": False,
                "description": "Check against SQLite without persisting newly discovered offers.",
            },
            "db_path": {
                "type": "string",
                "description": "Optional SQLite database path override.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}

EXPORT_LLM_SCHEMA: dict[str, Any] = {
    "name": "jobsuche_export_llm",
    "description": (
        "Export stored Jobsuche Ausbildung offers from SQLite into LLM-ready Markdown "
        "(with 16-key YAML frontmatter and structured Bewerbung/Anschreiben sections) and JSON "
        "cards, plus INDEX_FOR_LLM.md and companies_summary.json."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "db_path": {
                "type": "string",
                "description": "Optional SQLite database path override.",
            },
            "export_dir": {
                "type": "string",
                "description": "Optional target directory override for exported LLM files.",
            },
            "status": {
                "type": "string",
                "enum": list(_ALLOWED_STATUSES),
                "description": "Optional lifecycle status filter (e.g. 'new', 'shortlisted', 'applied').",
            },
            "company": {
                "type": "string",
                "description": "Optional company name or slug filter.",
            },
            "city": {
                "type": "string",
                "description": "Optional city filter.",
            },
            "min_score": {
                "type": "number",
                "minimum": 0,
                "maximum": 100,
                "description": "Optional minimum relevance score filter (0-100).",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional maximum number of offers to export.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}

STATUS_SCHEMA: dict[str, Any] = {
    "name": "jobsuche_status",
    "description": (
        "Inspect or update Jobsuche SQLite database statistics, application funnel status counts "
        "(new, shortlisted, drafted, applied, rejected, ignored), company summaries, and BA "
        "authentication session status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "db_path": {
                "type": "string",
                "description": "Optional SQLite database path override.",
            },
            "refnr": {
                "type": "string",
                "description": "Optional offer Referenznummer to inspect or update.",
            },
            "status": {
                "type": "string",
                "enum": list(_ALLOWED_STATUSES),
                "description": "Optional status to filter by, or new status to set when 'refnr' is provided.",
            },
            "notes": {
                "type": "string",
                "description": "Optional notes when updating an offer's status.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _load_pkg() -> tuple[Any, Any, Any, Any, Any, Any, Any]:
    _ensure_jobsuche_pkg_on_path()
    from jobsuche_pkg import api_client, browser, config, db, exporter, models, sorter

    return config, models, db, api_client, sorter, exporter, browser


def _default_hermes_db_path(settings: Any) -> Path:
    env_db = os.environ.get("JOBSUCHE_DB_PATH", "").strip()
    if env_db:
        return Path(env_db).expanduser()

    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser() / "plugin-data" / "jobsuche" / "jobsuche.db"

    return Path(settings.db_path)


def _resolve_db_path(raw_db_path: Any, settings: Any) -> Path:
    if raw_db_path is not None:
        if not isinstance(raw_db_path, str) or not raw_db_path.strip():
            raise PluginValidationError("db_path must be a non-empty string.")
        return Path(raw_db_path.strip()).expanduser()
    return _default_hermes_db_path(settings)


def _resolve_export_dir(raw_export_dir: Any, settings: Any) -> Path:
    if raw_export_dir is not None:
        if not isinstance(raw_export_dir, str) or not raw_export_dir.strip():
            raise PluginValidationError("export_dir must be a non-empty string.")
        return Path(raw_export_dir.strip()).expanduser()
    env_exp = os.environ.get("JOBSUCHE_EXPORT_DIR", "").strip()
    if env_exp:
        return Path(env_exp).expanduser()
    return Path(settings.export_dir)


def _resolve_api_base_url(settings: Any) -> str:
    env_url = (
        os.environ.get("JOBSUCHE_API_BASE_URL", "").strip()
        or os.environ.get("API_BASE_URL", "").strip()
    )
    if env_url:
        return env_url.rstrip("/")
    return str(settings.api_base_url).rstrip("/")


def _parse_int_param(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value.strip())
        else:
            raise PluginValidationError(f"'{name}' must be an integer >= {minimum}.")
    if value < minimum:
        raise PluginValidationError(f"'{name}' must be >= {minimum} (got {value}).")
    return value


def _extract_search_filter(payload: dict[str, Any], settings: Any, models_mod: Any) -> tuple[Any, float | None, bool]:
    raw_was = payload.get("was") if payload.get("was") is not None else payload.get("query")
    if raw_was is not None and not isinstance(raw_was, str):
        raise PluginValidationError("'was' must be a string.")
    was = raw_was.strip() if isinstance(raw_was, str) and raw_was.strip() else settings.default_was

    raw_wo = payload.get("wo") if payload.get("wo") is not None else payload.get("city")
    if raw_wo is not None and not isinstance(raw_wo, str):
        raise PluginValidationError("'wo' must be a string.")

    raw_plz = payload.get("plz") if payload.get("plz") is not None else payload.get("postal_code")
    if raw_plz is not None and not isinstance(raw_plz, str):
        raise PluginValidationError("'plz' must be a 5-digit string.")
    plz = raw_plz.strip() if isinstance(raw_plz, str) and raw_plz.strip() else None

    if raw_wo is not None and raw_wo.strip():
        wo = raw_wo.strip()
    elif plz:
        wo = plz
    else:
        wo = settings.default_wo

    raw_umkreis = payload.get("umkreis") if payload.get("umkreis") is not None else payload.get("radius")
    if raw_umkreis is not None:
        umkreis = _parse_int_param(raw_umkreis, "umkreis", minimum=0)
    else:
        umkreis = int(settings.default_umkreis)

    raw_angebotsart = payload.get("angebotsart")
    if raw_angebotsart is not None:
        angebotsart = _parse_int_param(raw_angebotsart, "angebotsart", minimum=1)
    else:
        angebotsart = int(settings.default_angebotsart)

    raw_limit = payload.get("limit")
    if raw_limit is not None:
        limit = _parse_int_param(raw_limit, "limit", minimum=1)
    else:
        limit = 25

    min_score: float | None = None
    if payload.get("min_score") is not None:
        raw_ms = payload.get("min_score")
        if isinstance(raw_ms, bool) or not isinstance(raw_ms, (int, float)):
            raise PluginValidationError("'min_score' must be a number between 0 and 100.")
        min_score = float(raw_ms)
        if min_score < 0.0 or min_score > 100.0:
            raise PluginValidationError("'min_score' must be between 0 and 100.")

    enrich_details = False if payload.get("enrich_details") is None else payload.get("enrich_details")
    if not isinstance(enrich_details, bool):
        raise PluginValidationError("'enrich_details' must be a boolean.")

    try:
        sf = models_mod.SearchFilter(
            was=was,
            wo=wo,
            plz=plz,
            umkreis=umkreis,
            angebotsart=angebotsart,
            abdatum=payload.get("abdatum"),
            start_date_from=payload.get("start_date_from"),
            start_date_to=payload.get("start_date_to"),
            size=min(limit, 25),
            limit=limit,
        )
    except Exception as exc:
        raise PluginValidationError(str(exc)) from exc

    return sf, min_score, enrich_details


def _escape_telegram_markdown(text: str) -> str:
    """Escape Telegram Markdown v1 special characters in user/API strings."""
    escaped = str(text or "")
    for ch in ("\\", "_", "*", "[", "]", "`"):
        escaped = escaped.replace(ch, f"\\{ch}")
    return escaped


def format_telegram_digest(
    offers: list[Any],
    *,
    was: str,
    wo: str,
    umkreis: int,
) -> str:
    """Render newly discovered Ausbildung offers as a Telegram Markdown digest."""
    if not offers:
        return ""

    header_was = _escape_telegram_markdown(was)
    header_wo = _escape_telegram_markdown(wo)
    lines: list[str] = [
        f"🎓 *Jobsuche Ausbildung: {len(offers)} neue Angebote für \"{header_was}\"*",
        f"📍 Region: {header_wo} (+{umkreis} km)",
        "",
    ]

    for idx, offer in enumerate(offers, start=1):
        title_esc = _escape_telegram_markdown(offer.title)
        company_esc = _escape_telegram_markdown(offer.company)
        city_esc = _escape_telegram_markdown(offer.city or wo)
        refnr_esc = _escape_telegram_markdown(offer.refnr)
        dist_str = f" ({offer.distance_km:.1f} km)" if offer.distance_km is not None else ""
        start_str = f" | Start: {_escape_telegram_markdown(offer.start_date)}" if offer.start_date else ""
        skills_str = (
            f"\n   🛠 Skills: {_escape_telegram_markdown(', '.join(offer.matched_skills[:5]))}"
            if getattr(offer, "matched_skills", None)
            else ""
        )
        url = offer.apply_url or offer.portal_url
        lines.append(
            f"{idx}. *{title_esc}* — {company_esc} (`{refnr_esc}`)\n"
            f"   📍 {city_esc}{dist_str}{start_str} | Score: {offer.score:.1f}\n"
            f"   🔗 {url}{skills_str}"
        )

    return "\n".join(lines)


def _error_payload(exc: Exception, api_mod: Any = None) -> str:
    if isinstance(exc, PluginValidationError):
        return _json_dumps({"ok": False, "error_type": "validation_error", "error": str(exc)})
    if api_mod is not None and isinstance(exc, getattr(api_mod, "JobsucheAPIError", ())):
        return _json_dumps(
            {
                "ok": False,
                "error_type": "api_error",
                "status_code": getattr(exc, "status_code", None),
                "error": str(exc),
            }
        )
    if isinstance(exc, sqlite3.Error):
        return _json_dumps({"ok": False, "error_type": "database_error", "error": str(exc)})
    if isinstance(exc, ValueError):
        return _json_dumps({"ok": False, "error_type": "validation_error", "error": str(exc)})
    if isinstance(exc, OSError):
        return _json_dumps({"ok": False, "error_type": "io_error", "error": str(exc)})
    return _json_dumps({"ok": False, "error_type": "unexpected_error", "error": str(exc)})


def _handle_search(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Execute a Jobsuche Ausbildung search, score and group offers, and return JSON."""
    del kwargs
    payload = args if isinstance(args, dict) else {}
    api_mod = None
    try:
        config_mod, models_mod, db_mod, api_mod, sorter_mod, _, _ = _load_pkg()
        settings = config_mod.get_settings()
        sf, min_score, enrich_details = _extract_search_filter(payload, settings, models_mod)
        base_url = _resolve_api_base_url(settings)

        save_to_db = False if payload.get("save_to_db") is None else payload.get("save_to_db")
        if not isinstance(save_to_db, bool):
            raise PluginValidationError("'save_to_db' must be a boolean.")

        with api_mod.JobsucheAPIClient(
            settings,
            base_url=base_url,
            timeout=10.0,
            max_retries=1,
            backoff_factor=0.05,
        ) as client:
            raw_offers = client.search_jobs(sf)
            if enrich_details:
                raw_offers = [client.enrich_offer(o, fetch_api_details=True) for o in raw_offers]

        scored = [sorter_mod.score_offer(o) for o in raw_offers]
        if min_score is not None or sf.start_date_from or sf.start_date_to:
            scored = sorter_mod.filter_offers(scored, sf, min_score=min_score)
        sorted_offers = sorter_mod.sort_offers(scored)
        companies = sorter_mod.build_company_profiles(sorted_offers)

        db_stats: dict[str, int] | None = None
        if save_to_db:
            resolved_db = _resolve_db_path(payload.get("db_path"), settings)
            with db_mod.JobDatabase(resolved_db) as job_db:
                db_stats = job_db.upsert_offers(sorted_offers)
                job_db.record_search_run(
                    {"was": sf.was, "wo": sf.wo, "umkreis": sf.umkreis},
                    total_found=len(sorted_offers),
                    new_count=db_stats["inserted"],
                )

        result: dict[str, Any] = {
            "ok": True,
            "was": sf.was,
            "wo": sf.wo,
            "umkreis": sf.umkreis,
            "angebotsart": sf.angebotsart,
            "count": len(sorted_offers),
            "company_count": len(companies),
            "offers": [o.model_dump(mode="json") for o in sorted_offers],
            "listings": [o.model_dump(mode="json") for o in sorted_offers],
            "companies": [c.model_dump(mode="json") for c in companies],
        }
        if db_stats is not None:
            result["db_stats"] = db_stats
        return _json_dumps(result)
    except Exception as exc:
        return _error_payload(exc, api_mod)


def _handle_monitor(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Search Jobsuche, deduplicate offers in SQLite by refnr, and emit JSON + Telegram digest."""
    del kwargs
    payload = args if isinstance(args, dict) else {}
    api_mod = None
    try:
        config_mod, models_mod, db_mod, api_mod, sorter_mod, _, _ = _load_pkg()
        settings = config_mod.get_settings()

        fmt = "json" if payload.get("format") is None else payload.get("format")
        if not isinstance(fmt, str) or fmt not in _ALLOWED_FORMATS:
            raise PluginValidationError(
                f"Invalid format '{fmt}'. Allowed values: {list(_ALLOWED_FORMATS)}."
            )

        dry_run = False if payload.get("dry_run") is None else payload.get("dry_run")
        if not isinstance(dry_run, bool):
            raise PluginValidationError("'dry_run' must be a boolean.")

        resolved_db = _resolve_db_path(payload.get("db_path"), settings)
        sf, min_score, enrich_details = _extract_search_filter(payload, settings, models_mod)
        base_url = _resolve_api_base_url(settings)

        with api_mod.JobsucheAPIClient(
            settings,
            base_url=base_url,
            timeout=10.0,
            max_retries=1,
            backoff_factor=0.05,
        ) as client:
            raw_offers = client.search_jobs(sf)
            if enrich_details:
                raw_offers = [client.enrich_offer(o, fetch_api_details=True) for o in raw_offers]

        scored = [sorter_mod.score_offer(o) for o in raw_offers]
        if min_score is not None or sf.start_date_from or sf.start_date_to:
            scored = sorter_mod.filter_offers(scored, sf, min_score=min_score)
        sorted_offers = sorter_mod.sort_offers(scored)

        new_offers: list[Any] = []
        updated_count = 0
        unchanged_count = 0

        if dry_run:
            if resolved_db.is_file():
                with db_mod.JobDatabase(resolved_db) as job_db:
                    for offer in sorted_offers:
                        if job_db.get_offer(offer.refnr) is None:
                            new_offers.append(offer)
                        else:
                            unchanged_count += 1
            else:
                new_offers = list(sorted_offers)
        else:
            with db_mod.JobDatabase(resolved_db) as job_db:
                for offer in sorted_offers:
                    is_new, changed_fields = job_db.upsert_offer(offer)
                    if is_new:
                        new_offers.append(offer)
                    elif changed_fields:
                        updated_count += 1
                    else:
                        unchanged_count += 1
                job_db.record_search_run(
                    {"was": sf.was, "wo": sf.wo, "umkreis": sf.umkreis},
                    total_found=len(sorted_offers),
                    new_count=len(new_offers),
                )

        digest_msg = (
            format_telegram_digest(
                new_offers,
                was=sf.was,
                wo=sf.wo,
                umkreis=sf.umkreis,
            )
            if new_offers
            else ""
        )

        result: dict[str, Any] = {
            "ok": True,
            "was": sf.was,
            "wo": sf.wo,
            "umkreis": sf.umkreis,
            "format": fmt,
            "dry_run": dry_run,
            "db_path": str(resolved_db),
            "total_fetched": len(sorted_offers),
            "new_count": len(new_offers),
            "updated_count": updated_count,
            "unchanged_count": unchanged_count,
            "offers": [o.model_dump(mode="json") for o in new_offers],
            "listings": [o.model_dump(mode="json") for o in new_offers],
            "message": digest_msg,
        }
        return _json_dumps(result)
    except Exception as exc:
        return _error_payload(exc, api_mod)


def _handle_export_llm(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Export offers from SQLite into LLM-ready Markdown and JSON files."""
    del kwargs
    payload = args if isinstance(args, dict) else {}
    try:
        config_mod, models_mod, db_mod, _, sorter_mod, exporter_mod, _ = _load_pkg()
        settings = config_mod.get_settings()
        resolved_db = _resolve_db_path(payload.get("db_path"), settings)
        resolved_export_dir = _resolve_export_dir(payload.get("export_dir"), settings)

        status_filter = payload.get("status")
        if status_filter is not None:
            if not isinstance(status_filter, str) or status_filter not in _ALLOWED_STATUSES:
                raise PluginValidationError(
                    f"Invalid status '{status_filter}'. Allowed: {list(_ALLOWED_STATUSES)}."
                )
            status_enum = models_mod.ApplicationStatus(status_filter)
        else:
            status_enum = None

        min_score: float | None = None
        if payload.get("min_score") is not None:
            raw_ms = payload.get("min_score")
            if isinstance(raw_ms, bool) or not isinstance(raw_ms, (int, float)):
                raise PluginValidationError("'min_score' must be a number.")
            min_score = float(raw_ms)

        limit: int | None = None
        if payload.get("limit") is not None:
            limit = _parse_int_param(payload.get("limit"), "limit", minimum=1)

        if resolved_db.is_file():
            with db_mod.JobDatabase(resolved_db) as job_db:
                offers = job_db.list_offers(
                    status=status_enum,
                    company=payload.get("company"),
                    city=payload.get("city"),
                    min_score=min_score,
                    limit=limit,
                )
        else:
            offers = []

        scored_offers = [
            sorter_mod.score_offer(o) if (o.score == 0.0 and not o.matched_skills) else o
            for o in offers
        ]
        export_summary = exporter_mod.export_offers(scored_offers, resolved_export_dir)
        export_summary["db_path"] = str(resolved_db)
        return _json_dumps(export_summary)
    except Exception as exc:
        return _error_payload(exc)


def _handle_status(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Return or update SQLite offer funnel status counts, companies, and auth state."""
    del kwargs
    payload = args if isinstance(args, dict) else {}
    try:
        config_mod, models_mod, db_mod, _, _, _, browser_mod = _load_pkg()
        settings = config_mod.get_settings()
        resolved_db = _resolve_db_path(payload.get("db_path"), settings)

        refnr = payload.get("refnr")
        status_arg = payload.get("status")
        notes_arg = payload.get("notes")

        if status_arg is not None:
            if not isinstance(status_arg, str) or status_arg not in _ALLOWED_STATUSES:
                raise PluginValidationError(
                    f"Invalid status '{status_arg}'. Allowed: {list(_ALLOWED_STATUSES)}."
                )

        status_counts: dict[str, int] = {s: 0 for s in _ALLOWED_STATUSES}
        offers_list: list[Any] = []
        companies_list: list[Any] = []
        updated_offer: dict[str, Any] | None = None

        db_exists = resolved_db.is_file()
        if db_exists or (refnr and status_arg):
            with db_mod.JobDatabase(resolved_db) as job_db:
                if refnr and status_arg:
                    updated_obj = job_db.update_status(str(refnr), status_arg, notes=notes_arg)
                    updated_offer = updated_obj.model_dump(mode="json")

                all_offers = job_db.list_offers()
                for o in all_offers:
                    st_val = (
                        o.status.value
                        if isinstance(o.status, models_mod.ApplicationStatus)
                        else str(o.status)
                    )
                    status_counts[st_val] = status_counts.get(st_val, 0) + 1

                if status_arg and not refnr:
                    offers_list = job_db.list_offers(status=status_arg)
                elif refnr and not status_arg:
                    single = job_db.get_offer(str(refnr))
                    offers_list = [single] if single is not None else []
                else:
                    offers_list = all_offers

                companies_list = job_db.list_companies()

        auth_status = browser_mod.StealthBrowserManager(settings).get_auth_status()

        result: dict[str, Any] = {
            "ok": True,
            "db_path": str(resolved_db),
            "db_exists": resolved_db.is_file(),
            "total_offers": sum(status_counts.values()),
            "total_companies": len(companies_list),
            "status_counts": status_counts,
            **status_counts,
            "offers": [
                {
                    "refnr": o.refnr,
                    "title": o.title,
                    "company": o.company,
                    "company_slug": o.company_slug,
                    "city": o.city,
                    "score": o.score,
                    "status": (
                        o.status.value
                        if isinstance(o.status, models_mod.ApplicationStatus)
                        else str(o.status)
                    ),
                    "notes": o.notes,
                }
                for o in offers_list
            ],
            "companies": [c.model_dump(mode="json") for c in companies_list],
            "auth": auth_status,
        }
        if updated_offer is not None:
            result["updated_offer"] = updated_offer
        return _json_dumps(result)
    except Exception as exc:
        return _error_payload(exc)


def register(ctx: Any) -> None:
    """Register Jobsuche tools and bundled skill on Hermes PluginContext."""
    ctx.register_tool(
        name="jobsuche_search",
        toolset="jobsuche",
        schema=SEARCH_SCHEMA,
        handler=_handle_search,
        description=SEARCH_SCHEMA["description"],
    )
    ctx.register_tool(
        name="jobsuche_monitor",
        toolset="jobsuche",
        schema=MONITOR_SCHEMA,
        handler=_handle_monitor,
        description=MONITOR_SCHEMA["description"],
    )
    ctx.register_tool(
        name="jobsuche_export_llm",
        toolset="jobsuche",
        schema=EXPORT_LLM_SCHEMA,
        handler=_handle_export_llm,
        description=EXPORT_LLM_SCHEMA["description"],
    )
    ctx.register_tool(
        name="jobsuche_status",
        toolset="jobsuche",
        schema=STATUS_SCHEMA,
        handler=_handle_status,
        description=STATUS_SCHEMA["description"],
    )
    if _SKILL_MD_PATH.is_file():
        ctx.register_skill(
            "jobsuche",
            _SKILL_MD_PATH,
            description=_SKILL_DESCRIPTION,
            frontmatter={
                "name": "jobsuche",
                "description": _SKILL_DESCRIPTION,
                "platforms": ["linux", "macos", "windows"],
            },
        )
