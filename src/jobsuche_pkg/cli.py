"""Command-line interface (`jobsuche`) for the Jobsuche Ausbildung automation suite."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
from pathlib import Path
import sys
import time
from typing import Any

from jobsuche_pkg.api_client import JobsucheAPIClient, JobsucheAPIError
from jobsuche_pkg.config import Settings, get_settings
from jobsuche_pkg.db import JobDatabase
from jobsuche_pkg.exporter import export_offers
from jobsuche_pkg.models import (
    ApplicationStatus,
    AusbildungOffer,
    CompanyProfile,
    SearchFilter,
)
from jobsuche_pkg.sorter import filter_offers, score_offer, sort_offers


def _format_offers_table(offers: list[AusbildungOffer]) -> str:
    if not offers:
        return "No offers found."
    header = f"{'RefNr':<22} | {'Score':>5} | {'Status':<11} | {'City (km)':<18} | {'Company':<28} | Title"
    sep = "-" * len(header)
    rows = [header, sep]
    for o in offers:
        city_part = o.city or "-"
        if o.distance_km is not None:
            loc = f"{city_part} ({o.distance_km:.0f}km)"
        else:
            loc = city_part
        status_str = o.status.value if isinstance(o.status, ApplicationStatus) else str(o.status)
        rows.append(
            f"{o.refnr[:22]:<22} | {o.score:5.1f} | {status_str:<11} | "
            f"{loc[:18]:<18} | {o.company[:28]:<28} | {o.title}"
        )
    return "\n".join(rows)


def _format_offers_markdown(offers: list[AusbildungOffer]) -> str:
    lines = [
        "# Jobsuche Ausbildung Offers",
        "",
        "| RefNr | Score | Status | City | Company | Title | Start Date |",
        "|-------|-------|--------|------|---------|-------|------------|",
    ]
    for o in offers:
        status_str = o.status.value if isinstance(o.status, ApplicationStatus) else str(o.status)
        city_str = o.city or "-"
        if o.distance_km is not None:
            city_str = f"{city_str} ({o.distance_km:.1f} km)"
        company_clean = o.company.replace("|", "/")
        title_clean = o.title.replace("|", "/")
        lines.append(
            f"| {o.refnr} | {o.score:.1f} | {status_str} | {city_str} | "
            f"{company_clean} | {title_clean} | {o.start_date or '-'} |"
        )
    return "\n".join(lines)


def _format_companies_table(companies: list[CompanyProfile]) -> str:
    if not companies:
        return "No companies found."
    header = f"{'Slug':<28} | {'Offers':>6} | {'Max':>5} | {'Avg':>5} | {'City':<16} | Name"
    sep = "-" * len(header)
    rows = [header, sep]
    for c in companies:
        rows.append(
            f"{c.company_slug[:28]:<28} | {c.offer_count:6d} | {c.max_score:5.1f} | "
            f"{c.avg_score:5.1f} | {(c.city or '-')[:16]:<16} | {c.name}"
        )
    return "\n".join(rows)


def _format_companies_markdown(companies: list[CompanyProfile]) -> str:
    lines = [
        "# Companies Summary",
        "",
        "| Slug | Name | Offers | Max Score | Avg Score | Cities |",
        "|------|------|--------|-----------|-----------|--------|",
    ]
    for c in companies:
        cities_str = ", ".join(c.cities) if c.cities else (c.city or "-")
        lines.append(
            f"| {c.company_slug} | {c.name.replace('|', '/')} | {c.offer_count} | "
            f"{c.max_score:.1f} | {c.avg_score:.1f} | {cities_str} |"
        )
    return "\n".join(lines)


def _generate_totp_fallback(
    secret: str,
    *,
    min_remaining_seconds: float = 3.0,
) -> tuple[str, float]:
    """Generate a 6-digit TOTP code via `jobsuche_pkg.auth_otp` or direct `pyotp`."""
    try:
        from jobsuche_pkg import auth_otp  # type: ignore[attr-defined]

        if hasattr(auth_otp, "generate_totp"):
            res = auth_otp.generate_totp(secret, min_remaining_seconds=min_remaining_seconds)
            if isinstance(res, tuple) and len(res) >= 2:
                return str(res[0]), float(res[1])
            return str(res), 30.0
    except ImportError:
        pass

    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("Missing or empty BA_TOTP_SECRET")

    cleaned = secret.strip().replace(" ", "").upper()
    padding = (-len(cleaned)) % 8
    padded = cleaned + ("=" * padding)
    try:
        decoded = base64.b32decode(padded, casefold=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Invalid Base32 TOTP secret: {exc}") from exc
    if not decoded:
        raise ValueError("Decoded TOTP secret is empty")

    import pyotp

    totp = pyotp.TOTP(cleaned)
    now_ts = time.time()
    remaining = 30.0 - (now_ts % 30.0)
    if remaining < min_remaining_seconds:
        time.sleep(remaining + 0.05)
        now_ts = time.time()
        remaining = 30.0 - (now_ts % 30.0)
    return totp.at(int(now_ts)), remaining


def _cmd_login(args: argparse.Namespace, settings: Settings) -> int:
    if not settings.has_ba_credentials():
        print(
            "Error: Missing BA_USERNAME or BA_PASSWORD in environment or .env file.",
            file=sys.stderr,
        )
        return 1

    try:
        from jobsuche_pkg.portal_scraper import PortalScraper
    except ImportError as exc:
        print(f"Error: portal_scraper module unavailable: {exc}", file=sys.stderr)
        return 1

    scraper = PortalScraper(settings)
    result = scraper.login(headed=bool(args.headed))
    ok = bool(isinstance(result, dict) and (result.get("authenticated") or result.get("ok")))
    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        if ok:
            print(f"Login successful. Session saved to {settings.storage_state_path}")
        else:
            print(f"Login failed: {result}", file=sys.stderr)
    return 0 if ok else 1


def _cmd_search(args: argparse.Namespace, settings: Settings) -> int:
    sf = SearchFilter(
        was=args.was if args.was is not None else settings.default_was,
        wo=args.wo if args.wo is not None else settings.default_wo,
        plz=args.plz,
        umkreis=args.umkreis if args.umkreis is not None else settings.default_umkreis,
        angebotsart=args.angebotsart if args.angebotsart is not None else settings.default_angebotsart,
        abdatum=args.abdatum,
        start_date_from=args.start_date_from,
        start_date_to=args.start_date_to,
        size=args.size,
        limit=args.limit,
    )

    with JobsucheAPIClient(settings) as client:
        raw_offers = client.search_jobs(sf)
        if args.details:
            raw_offers = [client.enrich_offer(o, fetch_api_details=True) for o in raw_offers]

    scored = [score_offer(o) for o in raw_offers]
    filtered = filter_offers(scored, sf, min_score=args.min_score)
    sorted_list = sort_offers(filtered)

    if args.save:
        db_path = Path(args.db_path) if args.db_path else settings.db_path
        with JobDatabase(db_path) as db:
            counts = db.upsert_offers(sorted_list)
            db.record_search_run(
                sf.model_dump(mode="json"),
                total_found=len(sorted_list),
                new_count=counts["inserted"],
            )

    if args.format == "json":
        payload = [o.model_dump(mode="json") for o in sorted_list]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif args.format == "markdown":
        print(_format_offers_markdown(sorted_list))
    else:
        print(_format_offers_table(sorted_list))
    return 0


def _cmd_sync(args: argparse.Namespace, settings: Settings) -> int:
    sf = SearchFilter(
        was=args.was if args.was is not None else settings.default_was,
        wo=args.wo if args.wo is not None else settings.default_wo,
        plz=args.plz,
        umkreis=args.umkreis if args.umkreis is not None else settings.default_umkreis,
        angebotsart=args.angebotsart if args.angebotsart is not None else settings.default_angebotsart,
        abdatum=args.abdatum,
        start_date_from=args.start_date_from,
        start_date_to=args.start_date_to,
        size=args.size,
        limit=args.limit,
    )

    scraper: Any = None
    if args.browser:
        try:
            from jobsuche_pkg.portal_scraper import PortalScraper

            scraper = PortalScraper(settings)
        except ImportError:
            scraper = None

    with JobsucheAPIClient(settings) as client:
        fetched = client.search_jobs(sf)
        enriched: list[AusbildungOffer] = []
        for offer in fetched:
            if not args.no_details or scraper is not None:
                try:
                    offer = client.enrich_offer(
                        offer,
                        scraper=scraper,
                        fetch_api_details=not args.no_details,
                    )
                except JobsucheAPIError:
                    pass
            enriched.append(score_offer(offer))

    db_path = Path(args.db_path) if args.db_path else settings.db_path
    with JobDatabase(db_path) as db:
        counts = db.upsert_offers(enriched)
        run_id = db.record_search_run(
            sf.model_dump(mode="json"),
            total_found=len(enriched),
            new_count=counts["inserted"],
        )
        stored_offers = db.list_offers()

    export_summary: dict[str, Any] | None = None
    if args.export:
        export_dir = Path(args.export_dir) if args.export_dir else settings.export_dir
        export_summary = export_offers(stored_offers, export_dir)

    summary = {
        "ok": True,
        "run_id": run_id,
        "fetched": len(enriched),
        "inserted": counts["inserted"],
        "updated": counts["updated"],
        "unchanged": counts["unchanged"],
        "db_path": str(db_path),
    }
    if export_summary is not None:
        summary["export"] = export_summary

    if args.format == "json":
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(
            f"Synced {len(enriched)} offer(s) into {db_path} "
            f"(inserted={counts['inserted']}, updated={counts['updated']}, unchanged={counts['unchanged']})."
        )
    return 0


def _cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    db_path = Path(args.db_path) if args.db_path else settings.db_path
    export_dir = Path(args.export_dir) if args.export_dir else settings.export_dir

    status_filter: ApplicationStatus | None = None
    if args.status:
        status_filter = ApplicationStatus(args.status)

    offers: list[AusbildungOffer] = []
    if db_path.exists():
        with JobDatabase(db_path) as db:
            offers = db.list_offers(
                status=status_filter,
                company=args.company,
                city=args.city,
                min_score=args.min_score,
                limit=args.limit,
            )

    scored_offers: list[AusbildungOffer] = []
    for offer in offers:
        if offer.score == 0.0 and not offer.matched_skills:
            offer = score_offer(offer)
        scored_offers.append(offer)

    if args.wo is not None or args.umkreis is not None:
        sf = SearchFilter(
            wo=args.wo if args.wo is not None else settings.default_wo,
            umkreis=args.umkreis if args.umkreis is not None else settings.default_umkreis,
            status=status_filter,
        )
        scored_offers = filter_offers(scored_offers, sf, min_score=args.min_score)

    result = export_offers(scored_offers, export_dir)
    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            f"Exported {result['offer_count']} offer(s) across {result['company_count']} companies "
            f"to {result['export_dir']}."
        )
    return 0


def _cmd_companies(args: argparse.Namespace, settings: Settings) -> int:
    db_path = Path(args.db_path) if args.db_path else settings.db_path
    companies: list[CompanyProfile] = []
    if db_path.exists():
        with JobDatabase(db_path) as db:
            companies = db.list_companies()

    if args.format == "json":
        print(json.dumps([c.model_dump(mode="json") for c in companies], indent=2, ensure_ascii=False))
    elif args.format == "markdown":
        print(_format_companies_markdown(companies))
    else:
        print(_format_companies_table(companies))
    return 0


def _cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    db_path = Path(args.db_path) if args.db_path else settings.db_path

    if args.refnr and args.status:
        new_status = ApplicationStatus(args.status)
        with JobDatabase(db_path) as db:
            updated = db.update_status(args.refnr, new_status, notes=args.notes)
        if args.format == "json":
            print(json.dumps(updated.model_dump(mode="json"), indent=2, ensure_ascii=False))
        else:
            print(f"Updated {updated.refnr} -> status={updated.status.value}")
        return 0

    if args.refnr and not args.status:
        with JobDatabase(db_path) as db:
            offer = db.get_offer(args.refnr)
        if offer is None:
            print(f"Error: Offer not found: {args.refnr}", file=sys.stderr)
            return 1
        if args.format == "json":
            print(json.dumps(offer.model_dump(mode="json"), indent=2, ensure_ascii=False))
        else:
            print(f"{offer.refnr}: status={offer.status.value} notes={offer.notes or ''}")
        return 0

    # No refnr provided -> print summary of statuses in DB
    counts = {s.value: 0 for s in ApplicationStatus}
    total = 0
    if db_path.exists():
        with JobDatabase(db_path) as db:
            for o in db.list_offers():
                counts[o.status.value] = counts.get(o.status.value, 0) + 1
                total += 1
    payload = {"ok": True, "total": total, "by_status": counts, **counts}
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        parts = ", ".join(f"{k}={v}" for k, v in counts.items())
        print(f"Total offers: {total} ({parts})")
    return 0


def _cmd_otp_test(args: argparse.Namespace, settings: Settings) -> int:
    secret = args.secret if args.secret is not None else settings.ba_totp_secret
    if not secret or not str(secret).strip():
        print("Error: BA_TOTP_SECRET is not configured.", file=sys.stderr)
        return 1

    code, remaining = _generate_totp_fallback(
        str(secret),
        min_remaining_seconds=float(args.min_remaining),
    )
    if args.format == "json":
        print(
            json.dumps(
                {"ok": True, "code": code, "remaining_seconds": round(remaining, 2)},
                indent=2,
            )
        )
    else:
        print(f"TOTP Code: {code} (valid for {remaining:.1f}s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser for the `jobsuche` CLI."""
    parser = argparse.ArgumentParser(
        prog="jobsuche",
        description="Bundesagentur für Arbeit Jobsuche Ausbildung automation CLI.",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Optional path to .env configuration file.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. login
    p_login = subparsers.add_parser("login", help="Authenticate with the BA portal using Playwright & OTP.")
    p_login.add_argument("--headed", action="store_true", help="Launch browser in headed (visible) mode.")
    p_login.add_argument("--format", choices=["text", "json"], default="text", help="Output format.")

    # 2. search
    p_search = subparsers.add_parser("search", help="Search Ausbildung offers via the Jobsuche REST API.")
    p_search.add_argument("--was", default=None, help="Search query / profession title.")
    p_search.add_argument("--wo", default=None, help="City or 5-digit PLZ (default: Aachen).")
    p_search.add_argument("--plz", default=None, help="Explicit 5-digit German postal code.")
    p_search.add_argument("--umkreis", "--radius", dest="umkreis", type=int, default=None, help="Search radius in km (default: 25).")
    p_search.add_argument("--angebotsart", type=int, default=None, help="Offer type (default: 4 for Ausbildung).")
    p_search.add_argument("--abdatum", default=None, help="Earliest start date (YYYY-MM-DD).")
    p_search.add_argument("--start-date-from", default=None, help="Filter start_date >= YYYY-MM-DD.")
    p_search.add_argument("--start-date-to", default=None, help="Filter start_date <= YYYY-MM-DD.")
    p_search.add_argument("--size", type=int, default=2, help="Page size.")
    p_search.add_argument("--limit", type=int, default=25, help="Maximum offers to return.")
    p_search.add_argument("--min-score", type=float, default=None, help="Minimum relevance score (0-100).")
    p_search.add_argument("--details", "--enrich", dest="details", action="store_true", help="Fetch full job details.")
    p_search.add_argument("--save", action="store_true", help="Persist results into SQLite database.")
    p_search.add_argument("--db-path", default=None, help="Override SQLite database path.")
    p_search.add_argument("--format", choices=["table", "json", "markdown"], default="table", help="Output format.")

    # 3. sync
    p_sync = subparsers.add_parser("sync", help="Search, enrich, score, and persist offers into SQLite.")
    p_sync.add_argument("--was", default=None, help="Search query / profession title.")
    p_sync.add_argument("--wo", default=None, help="City or 5-digit PLZ (default: Aachen).")
    p_sync.add_argument("--plz", default=None, help="Explicit 5-digit German postal code.")
    p_sync.add_argument("--umkreis", "--radius", dest="umkreis", type=int, default=None, help="Search radius in km (default: 25).")
    p_sync.add_argument("--angebotsart", type=int, default=None, help="Offer type (default: 4 for Ausbildung).")
    p_sync.add_argument("--abdatum", default=None, help="Earliest start date (YYYY-MM-DD).")
    p_sync.add_argument("--start-date-from", default=None, help="Filter start_date >= YYYY-MM-DD.")
    p_sync.add_argument("--start-date-to", default=None, help="Filter start_date <= YYYY-MM-DD.")
    p_sync.add_argument("--size", type=int, default=2, help="Page size.")
    p_sync.add_argument("--limit", type=int, default=None, help="Maximum offers to sync.")
    p_sync.add_argument("--no-details", action="store_true", help="Skip fetching pc/v4/jobdetails per offer.")
    p_sync.add_argument("--browser", action="store_true", help="Also enrich offers via authenticated browser session.")
    p_sync.add_argument("--export", action="store_true", help="Also export LLM documents after syncing.")
    p_sync.add_argument("--db-path", default=None, help="Override SQLite database path.")
    p_sync.add_argument("--export-dir", default=None, help="Override export directory path.")
    p_sync.add_argument("--format", choices=["text", "json"], default="text", help="Output format.")

    # 4. export
    p_export = subparsers.add_parser("export", help="Export stored offers into LLM-ready Markdown and JSON files.")
    p_export.add_argument("--export-dir", "--output", dest="export_dir", default=None, help="Target export directory.")
    p_export.add_argument("--db-path", default=None, help="SQLite database path.")
    p_export.add_argument("--wo", default=None, help="Optional location filter.")
    p_export.add_argument("--umkreis", type=int, default=None, help="Optional radius filter in km.")
    p_export.add_argument("--city", default=None, help="Optional city substring filter.")
    p_export.add_argument("--company", default=None, help="Optional company filter.")
    p_export.add_argument("--status", default=None, help="Optional status filter.")
    p_export.add_argument("--min-score", type=float, default=None, help="Minimum score filter.")
    p_export.add_argument("--limit", type=int, default=None, help="Maximum offers to export.")
    p_export.add_argument("--format", choices=["text", "json"], default="text", help="Output format.")

    # 5. companies
    p_companies = subparsers.add_parser("companies", help="List aggregated company profiles from SQLite.")
    p_companies.add_argument("--db-path", default=None, help="SQLite database path.")
    p_companies.add_argument("--format", choices=["table", "json", "markdown"], default="table", help="Output format.")

    # 6. status
    p_status = subparsers.add_parser("status", help="Inspect or update offer application status.")
    p_status.add_argument("refnr", nargs="?", default=None, help="Offer reference number (RefNr).")
    p_status.add_argument("status", nargs="?", default=None, help="New status (new, shortlisted, drafted, applied, rejected, ignored).")
    p_status.add_argument("--notes", default=None, help="Optional note to attach to the status change.")
    p_status.add_argument("--db-path", default=None, help="SQLite database path.")
    p_status.add_argument("--format", choices=["text", "json"], default="text", help="Output format.")

    # 7. otp-test
    p_otp = subparsers.add_parser("otp-test", help="Test 6-digit TOTP code generation.")
    p_otp.add_argument("--secret", default=None, help="Optional Base32 secret override.")
    p_otp.add_argument("--min-remaining", type=float, default=3.0, help="Minimum remaining seconds in 30s TOTP window.")
    p_otp.add_argument("--format", choices=["text", "json"], default="text", help="Output format.")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for the `jobsuche` CLI command."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
        if argv is None:
            raise
        return code

    try:
        settings = get_settings(env_file=args.env_file)
        subcmd = args.subcommand
        if subcmd == "login":
            return _cmd_login(args, settings)
        if subcmd == "search":
            return _cmd_search(args, settings)
        if subcmd == "sync":
            return _cmd_sync(args, settings)
        if subcmd == "export":
            return _cmd_export(args, settings)
        if subcmd == "companies":
            return _cmd_companies(args, settings)
        if subcmd == "status":
            return _cmd_status(args, settings)
        if subcmd == "otp-test":
            return _cmd_otp_test(args, settings)
        print(f"Error: Unknown subcommand: {subcmd}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
