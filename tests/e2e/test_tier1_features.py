from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from typing import Any

import pytest

from tests.e2e.conftest import (
    ANTIGRAVITY_PLUGIN_DIR,
    E2EWorkspace,
    FakeClock,
    FakeHermesContext,
    FakeIMAPMailbox,
    FakeJobsucheRouter,
    FakePlaywrightEnv,
    GEMINI_CONFIG_DIR,
    HERMES_AGENT_DIR,
    HERMES_PLUGIN_DIR,
    HERMES_PYTHON,
    PROJECT_ROOT,
    VALID_TEST_TOTP_SECRET,
    encode_refnr_b64,
    load_hermes_jobsuche_module,
    require_module,
    run_cli,
    run_mcp_stdio,
)


def _open_stealth_context(manager: Any, callback: Any) -> Any:
    """Exercise StealthBrowserManager via its public context lifecycle API."""
    for method_name in ("open_context", "new_context", "context", "browser_context", "session"):
        method = getattr(manager, method_name, None)
        if callable(method):
            res = method()
            if hasattr(res, "__enter__") and hasattr(res, "__exit__"):
                with res as ctx:
                    return callback(ctx)
            return callback(res)
    if hasattr(manager, "__enter__") and hasattr(manager, "__exit__"):
        with manager as ctx:
            return callback(ctx)
    raise AssertionError("StealthBrowserManager exposes no public context method or context manager")


def _extract_mcp_json(result: Any) -> dict[str, Any]:
    assert not getattr(result, "isError", False), f"Unexpected MCP error result: {result}"
    texts = [getattr(item, "text", "") for item in getattr(result, "content", [])]
    joined = "\n".join(t for t in texts if t).strip()
    assert joined, f"Empty MCP tool response content: {result}"
    return json.loads(joined)


# ===========================================================================
# Feature F1: Package Scaffolding, Metadata & CLI Entrypoint (5 tests)
# ===========================================================================


def test_t1_f1_01_pyproject_metadata_and_scripts() -> None:
    pyproject_path = PROJECT_ROOT / "pyproject.toml"
    assert pyproject_path.is_file()
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = data["project"]
    assert project["name"]
    assert project["version"]
    assert "3.13" in str(project.get("requires-python", "")) or ">=3.11" in str(project.get("requires-python", ""))
    deps_joined = " ".join(str(d).lower() for d in project.get("dependencies", []))
    for expected_dep in ("pydantic", "pyotp", "playwright", "beautifulsoup4", "pyyaml"):
        assert expected_dep in deps_joined, f"Missing dependency {expected_dep} in pyproject.toml"
    scripts = project.get("scripts", {})
    assert scripts.get("jobsuche") == "jobsuche_pkg.cli:main"


def test_t1_f1_02_required_root_files_exist() -> None:
    for filename in ("pyproject.toml", ".env.example", ".gitignore", "README.md"):
        target = PROJECT_ROOT / filename
        assert target.is_file(), f"Missing required root file: {target}"
        assert target.stat().st_size > 0, f"Root file is empty: {target}"


def test_t1_f1_03_env_example_documents_config_keys() -> None:
    env_example = PROJECT_ROOT / ".env.example"
    assert env_example.is_file()
    keys: set[str] = set()
    for line in env_example.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0].strip())
    expected_keys = {
        "BA_USERNAME",
        "BA_PASSWORD",
        "BA_TOTP_SECRET",
        "BA_IMAP_HOST",
        "JOBSUCHE_DB_PATH",
        "JOBSUCHE_EXPORT_DIR",
        "JOBSUCHE_STORAGE_STATE",
        "JOBSUCHE_DEFAULT_CITY",
        "JOBSUCHE_DEFAULT_RADIUS",
    }
    missing = expected_keys - keys
    assert not missing, f"Missing keys in .env.example: {sorted(missing)}"


def test_t1_f1_04_gitignore_excludes_runtime_and_secrets() -> None:
    gitignore = PROJECT_ROOT / ".gitignore"
    assert gitignore.is_file()
    text = gitignore.read_text(encoding="utf-8")
    for pattern in (".env", ".venv", "storage_state.json", "exports", "__pycache__"):
        assert pattern in text, f"Expected pattern {pattern!r} in .gitignore"
    assert "*.db" in text or "data/" in text


def test_t1_f1_05_editable_import_and_cli_help(isolated_env: E2EWorkspace) -> None:
    pkg = require_module("jobsuche_pkg")
    assert hasattr(pkg, "__file__")
    proc = run_cli(["--help"], workspace=isolated_env)
    assert proc.returncode == 0, f"CLI --help failed: {proc.stderr}"
    for subcmd in ("login", "search", "sync", "export", "companies", "status", "otp-test"):
        assert subcmd in proc.stdout, f"Subcommand {subcmd!r} missing from --help output"


# ===========================================================================
# Feature F2: Config & Env Loading (5 tests)
# ===========================================================================


def test_t1_f2_01_defaults_without_dotenv(isolated_env: E2EWorkspace, pkg_config: Any) -> None:
    s = pkg_config.get_settings(env_file=isolated_env.env_file)
    assert s.default_was == "Fachinformatiker/in für Systemintegration"
    assert s.default_wo == "Aachen"
    assert s.default_umkreis == 25
    assert s.api_base_url == "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
    assert s.api_key == "jobboerse-jobsuche"
    assert s.ba_imap_port == 993
    assert s.headless is True


def test_t1_f2_02_default_path_fields_are_paths(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    pkg_config: Any,
) -> None:
    for var in ("JOBSUCHE_DB_PATH", "JOBSUCHE_EXPORT_DIR", "JOBSUCHE_STORAGE_STATE", "JOBSUCHE_SCREENSHOT_DIR"):
        monkeypatch.delenv(var, raising=False)
    s = pkg_config.get_settings(env_file=isolated_env.root / "nonexistent.env")
    assert isinstance(s.db_path, Path) and s.db_path == Path("data/jobsuche.db")
    assert isinstance(s.export_dir, Path) and s.export_dir == Path("exports")
    assert isinstance(s.storage_state_path, Path) and s.storage_state_path == Path("data/storage_state.json")
    assert isinstance(s.screenshot_dir, Path) and s.screenshot_dir == Path("data/screenshots")


def test_t1_f2_03_environment_variable_overrides(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    pkg_config: Any,
) -> None:
    monkeypatch.setenv("JOBSUCHE_DEFAULT_CITY", "Köln")
    monkeypatch.setenv("JOBSUCHE_DEFAULT_RADIUS", "50")
    s = pkg_config.get_settings(env_file=isolated_env.env_file)
    assert s.default_wo == "Köln"
    assert s.default_umkreis == 50
    assert s.db_path == isolated_env.db_path
    assert s.export_dir == isolated_env.export_dir


def test_t1_f2_04_loads_explicit_dotenv_file(isolated_env: E2EWorkspace, pkg_config: Any) -> None:
    isolated_env.env_file.write_text(
        "\n".join(
            [
                "BA_USERNAME=azubi.tester@example.de",
                f"BA_TOTP_SECRET={VALID_TEST_TOTP_SECRET}",
                "JOBSUCHE_DEFAULT_CITY=Bonn",
                "JOBSUCHE_DEFAULT_RADIUS=15",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    s = pkg_config.get_settings(env_file=isolated_env.env_file)
    assert s.ba_username == "azubi.tester@example.de"
    assert s.ba_totp_secret == VALID_TEST_TOTP_SECRET
    assert s.default_wo == "Bonn"
    assert s.default_umkreis == 15


def test_t1_f2_05_process_env_overrides_dotenv_file(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    pkg_config: Any,
) -> None:
    isolated_env.env_file.write_text("JOBSUCHE_DEFAULT_CITY=Bonn\n", encoding="utf-8")
    monkeypatch.setenv("JOBSUCHE_DEFAULT_CITY", "Düsseldorf")
    s = pkg_config.get_settings(env_file=isolated_env.env_file)
    assert s.default_wo == "Düsseldorf"


# ===========================================================================
# Feature F3: Domain Data Models (5 tests)
# ===========================================================================


def test_t1_f3_01_application_status_exact_six_values(pkg_models: Any) -> None:
    ApplicationStatus = pkg_models.ApplicationStatus
    values = {member.value for member in ApplicationStatus}
    assert values == {"new", "shortlisted", "drafted", "applied", "rejected", "ignored"}
    assert isinstance(ApplicationStatus.NEW, str)


def test_t1_f3_02_search_filter_defaults(pkg_models: Any) -> None:
    f = pkg_models.SearchFilter()
    assert f.was == "Fachinformatiker/in für Systemintegration"
    assert f.wo == "Aachen"
    assert f.umkreis == 25
    assert f.angebotsart == 4
    assert f.page == 1
    assert f.size == 25


def test_t1_f3_03_offer_auto_populates_slug_and_portal_url(pkg_models: Any) -> None:
    o = pkg_models.AusbildungOffer(
        refnr="10000-1206415041-S",
        title="Azubi FISI",
        company="Müller IT GmbH",
    )
    assert o.company_slug
    assert o.company_slug.isascii()
    assert "mueller" in o.company_slug.lower()
    assert o.portal_url == "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1206415041-S"
    assert o.status == pkg_models.ApplicationStatus.NEW


def test_t1_f3_04_slugify_company_transliterates_german_umlauts(pkg_models: Any) -> None:
    slugify = pkg_models.slugify_company
    slug1 = slugify("Bäcker, Öl & Straße GmbH (Übach)")
    slug2 = slugify("Bäcker, Öl & Straße GmbH (Übach)")
    assert slug1 == slug2
    assert slug1.isascii()
    for token in ("ae", "oe", "ss", "ue"):
        assert token in slug1
    for forbidden in (" ", "&", ",", "(", ")"):
        assert forbidden not in slug1


def test_t1_f3_05_offer_and_company_profile_json_roundtrip(pkg_models: Any) -> None:
    offer = pkg_models.AusbildungOffer(
        refnr="10000-1206415041-S",
        title="Azubi FISI",
        company="AixIT GmbH",
        matched_skills=["Linux", "Debian"],
        status=pkg_models.ApplicationStatus.SHORTLISTED,
    )
    offer_rt = pkg_models.AusbildungOffer.model_validate_json(offer.model_dump_json())
    assert offer_rt == offer

    profile = pkg_models.CompanyProfile(
        company_slug=offer.company_slug,
        name="AixIT GmbH",
        city="Aachen",
        plz="52062",
        offer_count=1,
        max_score=88.0,
        avg_score=88.0,
        offers=[offer.refnr],
        apply_urls=["https://aixit.example/apply"],
        cities=["Aachen"],
    )
    profile_rt = pkg_models.CompanyProfile.model_validate_json(profile.model_dump_json())
    assert profile_rt == profile


# ===========================================================================
# Feature F4: Jobsuche REST API Client (5 tests)
# ===========================================================================


def test_t1_f4_01_search_jobs_calls_v6_with_headers_and_default_params(
    mock_jobsuche_http: FakeJobsucheRouter,
    settings_without_creds: Any,
    pkg_api_client: Any,
    pkg_models: Any,
) -> None:
    client = pkg_api_client.JobsucheAPIClient(settings_without_creds)
    offers = client.search_jobs(pkg_models.SearchFilter())
    assert len(offers) >= 1
    assert mock_jobsuche_http.requests, "Expected HTTP requests recorded by FakeJobsucheRouter"
    req = mock_jobsuche_http.requests[0]
    assert "/pc/v6/jobs" in req.url
    assert "/pc/v4/jobs" not in req.url
    assert req.headers.get("x-api-key") == "jobboerse-jobsuche"
    assert "application/json" in req.headers.get("accept", "")
    assert "user-agent" in req.headers and len(req.headers["user-agent"]) > 10
    assert req.query.get("wo") == ["Aachen"]
    assert req.query.get("umkreis") == ["25"]
    assert req.query.get("angebotsart") == ["4"]


def test_t1_f4_02_normalizes_v6_ergebnisliste_payload(
    mock_jobsuche_http: FakeJobsucheRouter,
    settings_without_creds: Any,
    pkg_api_client: Any,
    pkg_models: Any,
) -> None:
    client = pkg_api_client.JobsucheAPIClient(settings_without_creds)
    offers = client.search_jobs(pkg_models.SearchFilter(size=2, limit=2))
    assert len(offers) == 2
    assert offers[0].refnr == "10000-1206415041-S"
    assert offers[0].company == "Müller & Söhne IT-Systemhaus GmbH"
    assert offers[0].plz == "52062"
    assert offers[0].city == "Aachen"
    assert offers[0].distance_km == 2.0
    assert offers[0].start_date == "2026-08-01"
    assert offers[1].apply_url == "https://karriere.mueller-it.example/fisi-2026"


def test_t1_f4_03_normalizes_legacy_v4_stellenangebote_payload(
    mock_jobsuche_http: FakeJobsucheRouter,
    sample_v4_jobs_payload: dict[str, Any],
    settings_without_creds: Any,
    pkg_api_client: Any,
    pkg_models: Any,
) -> None:
    mock_jobsuche_http.serve_v4_on_search = True
    mock_jobsuche_http.custom_search_payload = sample_v4_jobs_payload
    client = pkg_api_client.JobsucheAPIClient(settings_without_creds)
    offers = client.search_jobs(pkg_models.SearchFilter())
    assert len(offers) == 1
    o = offers[0]
    assert o.refnr == "10000-9900011122-S"
    assert o.title == "Fachinformatiker Systemintegration (Legacy v4)"
    assert o.company == "Rheinland Cloud KG"
    assert o.plz == "52064"
    assert o.city == "Aachen"
    assert o.distance_km == 3.0
    assert o.start_date == "2026-08-01"


def test_t1_f4_04_pagination_collects_all_and_respects_limit(
    mock_jobsuche_http: FakeJobsucheRouter,
    settings_without_creds: Any,
    pkg_api_client: Any,
    pkg_models: Any,
) -> None:
    client = pkg_api_client.JobsucheAPIClient(settings_without_creds)
    all_offers = client.search_jobs(pkg_models.SearchFilter(size=2, limit=None))
    assert len(all_offers) == 3
    assert {o.refnr for o in all_offers} == {
        "10000-1206415041-S",
        "10000-1206415999-S",
        "10000-1206420001-S",
    }
    limited = client.search_jobs(pkg_models.SearchFilter(size=2, limit=2))
    assert len(limited) == 2


def test_t1_f4_05_get_job_details_base64_and_enrich_offer(
    mock_jobsuche_http: FakeJobsucheRouter,
    settings_without_creds: Any,
    pkg_api_client: Any,
    pkg_models: Any,
) -> None:
    client = pkg_api_client.JobsucheAPIClient(settings_without_creds)
    refnr = "10000-1206415041-S"
    details = client.get_job_details(refnr)
    expected_b64 = encode_refnr_b64(refnr)
    assert any(f"/pc/v4/jobdetails/{expected_b64}" in r.url for r in mock_jobsuche_http.requests)
    assert "Linux" in details.get("stellenangebotsBeschreibung", "")

    class _StubScraper:
        def fetch_offer_details(self, req_refnr: str) -> dict[str, Any]:
            assert req_refnr == refnr
            return {
                "contact_name": "Frau Anna Weber",
                "contact_email": "karriere@mueller-it.example",
                "contact_phone": "+49 241 123456",
                "apply_url": "https://karriere.mueller-it.example/apply/1206415041",
            }

    base_offer = pkg_models.AusbildungOffer(
        refnr=refnr,
        title="Ausbildung FISI",
        company="Müller & Söhne IT-Systemhaus GmbH",
    )
    enriched = client.enrich_offer(base_offer, scraper=_StubScraper(), fetch_api_details=True)
    assert "Linux" in (enriched.description or "")
    assert enriched.education_required == "Mittlere Reife / Fachabitur"
    assert enriched.contact_name == "Frau Anna Weber"
    assert enriched.contact_email == "karriere@mueller-it.example"
    assert enriched.contact_phone == "+49 241 123456"
    assert enriched.apply_url


# ===========================================================================
# Feature F5: SQLite Storage & History (5 tests)
# ===========================================================================


def test_t1_f5_01_auto_creates_nested_parent_dirs_and_schema(
    isolated_env: E2EWorkspace,
    pkg_db: Any,
) -> None:
    nested_db = isolated_env.root / "nested" / "db" / "jobsuche.db"
    assert not nested_db.parent.exists()
    with pkg_db.JobDatabase(nested_db):
        assert nested_db.is_file()
    conn = sqlite3.connect(nested_db)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"offers", "companies", "offer_history"}.issubset(tables)


def test_t1_f5_02_upsert_offers_dedup_by_refnr(
    job_db: Any,
    sample_fisi_offers: list[Any],
) -> None:
    two_offers = sample_fisi_offers[:2]
    first = job_db.upsert_offers(two_offers)
    assert first == {"inserted": 2, "updated": 0, "unchanged": 0}
    second = job_db.upsert_offers(two_offers)
    assert second == {"inserted": 0, "updated": 0, "unchanged": 2}
    assert len(job_db.list_offers()) == 2


def test_t1_f5_03_upsert_detects_field_diff_and_logs_history(
    job_db: Any,
    sample_fisi_offers: list[Any],
) -> None:
    offer = sample_fisi_offers[0]
    is_new, changed = job_db.upsert_offer(offer)
    assert is_new is True
    assert changed == []

    mutated = offer.model_copy(
        update={
            "title": "Ausbildung FISI (Aktualisiert 2026)",
            "contact_email": "azubi@mueller-it.example",
        }
    )
    is_new2, changed2 = job_db.upsert_offer(mutated)
    assert is_new2 is False
    assert "title" in changed2
    assert "contact_email" in changed2

    stored = job_db.get_offer(offer.refnr)
    assert stored is not None
    assert stored.title == "Ausbildung FISI (Aktualisiert 2026)"
    assert stored.contact_email == "azubi@mueller-it.example"

    history = job_db.get_history(offer.refnr)
    assert len(history) >= 2
    serialized_hist = json.dumps(history, default=str)
    assert "azubi@mueller-it.example" in serialized_hist


def test_t1_f5_04_update_status_and_notes_records_history(
    job_db: Any,
    sample_fisi_offers: list[Any],
    pkg_models: Any,
) -> None:
    offer = sample_fisi_offers[0]
    job_db.upsert_offer(offer)
    updated = job_db.update_status(offer.refnr, pkg_models.ApplicationStatus.SHORTLISTED, notes="Top FISI match")
    assert updated.status == pkg_models.ApplicationStatus.SHORTLISTED
    assert updated.notes == "Top FISI match"

    history = job_db.get_history(offer.refnr)
    serialized = json.dumps(history, default=str)
    assert "new" in serialized and "shortlisted" in serialized


def test_t1_f5_05_list_offers_filters_companies_and_search_runs(
    job_db: Any,
    sample_fisi_offers: list[Any],
    pkg_models: Any,
) -> None:
    scored = [
        sample_fisi_offers[0].model_copy(update={"score": 90.0}),
        sample_fisi_offers[1].model_copy(update={"score": 60.0, "status": pkg_models.ApplicationStatus.SHORTLISTED}),
        sample_fisi_offers[2].model_copy(update={"score": 45.0}),
    ]
    job_db.upsert_offers(scored)
    assert len(job_db.list_offers(status=pkg_models.ApplicationStatus.SHORTLISTED)) == 1
    assert len(job_db.list_offers(city="Aachen")) == 2
    assert len(job_db.list_offers(min_score=70.0)) == 1
    assert len(job_db.list_offers(limit=2)) == 2
    assert len(job_db.list_offers(company="Aachener Rechenzentrum")) == 1

    companies = job_db.list_companies()
    assert len(companies) == 2
    counts = {c.name: c.offer_count for c in companies}
    assert counts["Müller & Söhne IT-Systemhaus GmbH"] == 2
    assert counts["Aachener Rechenzentrum AG"] == 1

    run_id = job_db.record_search_run({"wo": "Aachen", "umkreis": 25}, total_found=3, new_count=3)
    assert isinstance(run_id, int) and run_id >= 1


# ===========================================================================
# Feature F6: 6-Digit TOTP & Fallback OTP (5 tests)
# ===========================================================================


def test_t1_f6_01_generate_totp_safe_window_no_sleep(
    totp_clock: FakeClock,
    pkg_auth_otp: Any,
) -> None:
    pyotp = require_module("pyotp")
    totp_clock.set(FakeClock.SAFE_WINDOW_TS)
    code, rem = pkg_auth_otp.generate_totp(
        VALID_TEST_TOTP_SECRET,
        now=totp_clock.now(),
        sleep_fn=totp_clock.sleep,
    )
    assert re.fullmatch(r"\d{6}", code)
    assert code == pyotp.TOTP(VALID_TEST_TOTP_SECRET).at(int(FakeClock.SAFE_WINDOW_TS))
    assert rem == pytest.approx(20.0, abs=0.2)
    assert totp_clock.sleep_calls == []


def test_t1_f6_02_generate_totp_waits_for_next_tick_under_3s(
    totp_clock: FakeClock,
    pkg_auth_otp: Any,
) -> None:
    pyotp = require_module("pyotp")
    totp_clock.set(FakeClock.ROLLOVER_TS)
    code, rem = pkg_auth_otp.generate_totp(
        VALID_TEST_TOTP_SECRET,
        min_remaining_seconds=3.0,
        now=totp_clock.now(),
        sleep_fn=totp_clock.sleep,
    )
    assert len(totp_clock.sleep_calls) >= 1
    assert sum(totp_clock.sleep_calls) >= 2.0
    assert code == pyotp.TOTP(VALID_TEST_TOTP_SECRET).at(1700000040)
    assert rem >= 25.0


def test_t1_f6_03_get_otp_code_uses_settings_totp_secret(
    settings_with_creds: Any,
    totp_clock: FakeClock,
    pkg_auth_otp: Any,
) -> None:
    pyotp = require_module("pyotp")
    totp_clock.set(FakeClock.SAFE_WINDOW_TS)
    code = pkg_auth_otp.get_otp_code(settings_with_creds)
    assert re.fullmatch(r"\d{6}", code)
    totp = pyotp.TOTP(settings_with_creds.ba_totp_secret)
    assert code in {totp.at(int(totp_clock.now())), totp.now()}


def test_t1_f6_04_imap_fallback_extracts_6_digit_code(
    mock_imap: FakeIMAPMailbox,
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    pkg_config: Any,
    pkg_auth_otp: Any,
) -> None:
    mock_imap.add_otp_email("482910")
    monkeypatch.setenv("BA_IMAP_HOST", "imap.example.de")
    monkeypatch.setenv("BA_IMAP_PORT", "993")
    monkeypatch.setenv("BA_IMAP_USER", "fisi.test@example.de")
    monkeypatch.setenv("BA_IMAP_PASSWORD", "imap-secret")
    settings_imap = pkg_config.get_settings(env_file=isolated_env.env_file)

    direct = pkg_auth_otp.fetch_otp_via_imap(
        host="imap.example.de",
        port=993,
        username="fisi.test@example.de",
        password="imap-secret",
    )
    assert direct == "482910"
    assert pkg_auth_otp.get_otp_code(settings_imap) == "482910"


def test_t1_f6_05_cli_callback_fallback_returns_6_digit_code(
    settings_without_creds: Any,
    pkg_auth_otp: Any,
) -> None:
    code = pkg_auth_otp.get_otp_code(settings_without_creds, cli_callback=lambda: "739201")
    assert code == "739201"


# ===========================================================================
# Feature F7: Stealth Playwright Profile & CAPTCHA Guard (5 tests)
# ===========================================================================


def test_t1_f7_01_context_configures_german_locale_timezone_viewport_headers(
    fake_playwright: FakePlaywrightEnv,
    settings_without_creds: Any,
    pkg_browser: Any,
) -> None:
    manager = pkg_browser.StealthBrowserManager(settings_without_creds)
    _open_stealth_context(manager, lambda ctx: None)
    assert len(fake_playwright.context_calls) >= 1
    ctx_kwargs = fake_playwright.context_calls[-1]
    assert ctx_kwargs.get("locale") == "de-DE"
    assert ctx_kwargs.get("timezone_id") == "Europe/Berlin"
    vp = ctx_kwargs.get("viewport") or {}
    assert vp.get("width", 0) >= 1280 and vp.get("height", 0) >= 720
    headers = {k.lower(): v for k, v in (ctx_kwargs.get("extra_http_headers") or {}).items()}
    assert headers.get("accept-language", "").startswith("de-DE")


def test_t1_f7_02_user_agent_rotates_realistic_linux_or_windows_desktop(
    fake_playwright: FakePlaywrightEnv,
    settings_without_creds: Any,
    pkg_browser: Any,
) -> None:
    manager = pkg_browser.StealthBrowserManager(settings_without_creds)
    uas: list[str] = []
    for _ in range(4):
        _open_stealth_context(manager, lambda ctx: None)
        uas.append(str(fake_playwright.context_calls[-1].get("user_agent", "")))
    for ua in uas:
        assert "X11; Linux x86_64" in ua or "Windows NT 10.0; Win64; x64" in ua
        assert "HeadlessChrome" not in ua
        assert "python-requests" not in ua


def test_t1_f7_03_storage_state_persisted_and_reused(
    fake_playwright: FakePlaywrightEnv,
    settings_without_creds: Any,
    isolated_env: E2EWorkspace,
    pkg_browser: Any,
) -> None:
    manager = pkg_browser.StealthBrowserManager(settings_without_creds)

    def _save(ctx: Any) -> None:
        if hasattr(manager, "save_storage_state"):
            manager.save_storage_state(ctx)
        else:
            ctx.storage_state(path=str(isolated_env.storage_state_path))

    _open_stealth_context(manager, _save)
    assert isolated_env.storage_state_path.is_file()
    parsed = json.loads(isolated_env.storage_state_path.read_text(encoding="utf-8"))
    assert len(parsed.get("cookies", [])) >= 1

    _open_stealth_context(manager, lambda ctx: None)
    storage_state_arg = fake_playwright.context_calls[-1].get("storage_state")
    assert storage_state_arg is not None
    assert Path(str(storage_state_arg)) == isolated_env.storage_state_path


def test_t1_f7_04_human_pause_sleeps_within_jitter_bounds(
    totp_clock: FakeClock,
    settings_without_creds: Any,
    pkg_browser: Any,
) -> None:
    manager = pkg_browser.StealthBrowserManager(settings_without_creds)
    manager.human_pause(min_s=0.4, max_s=0.9)
    assert len(totp_clock.sleep_calls) == 1
    assert 0.4 <= totp_clock.sleep_calls[0] <= 0.9


def test_t1_f7_05_get_auth_status_missing_vs_valid_storage_state(
    settings_without_creds: Any,
    isolated_env: E2EWorkspace,
    pkg_browser: Any,
) -> None:
    manager = pkg_browser.StealthBrowserManager(settings_without_creds)
    before = manager.get_auth_status()
    assert isinstance(before, dict)
    assert any(before.get(k) is False for k in ("has_session", "authenticated", "storage_state_exists", "session_valid"))

    isolated_env.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    isolated_env.storage_state_path.write_text(
        json.dumps({"cookies": [{"name": "ba", "value": "123"}], "origins": []}),
        encoding="utf-8",
    )
    after = manager.get_auth_status()
    assert isinstance(after, dict)
    assert any(after.get(k) is True for k in ("has_session", "authenticated", "storage_state_exists", "session_valid"))


# ===========================================================================
# Feature F8: Authenticated Portal Scraper (5 tests)
# ===========================================================================


def test_t1_f8_01_login_submits_creds_and_6_digit_totp_saves_state(
    fake_playwright: FakePlaywrightEnv,
    settings_with_creds: Any,
    totp_clock: FakeClock,
    isolated_env: E2EWorkspace,
    pkg_portal_scraper: Any,
) -> None:
    fake_playwright.scenario = "authenticated_portal"
    scraper = pkg_portal_scraper.PortalScraper(settings_with_creds)
    res = scraper.login()
    assert isinstance(res, dict)
    assert res.get("authenticated") is True or res.get("ok") is True
    assert isolated_env.storage_state_path.is_file()
    assert any(re.fullmatch(r"\d{6}", val.strip()) for _, val in fake_playwright.filled_values)


def test_t1_f8_02_login_headed_flag_sets_headless_false(
    fake_playwright: FakePlaywrightEnv,
    settings_with_creds: Any,
    totp_clock: FakeClock,
    pkg_portal_scraper: Any,
) -> None:
    fake_playwright.scenario = "authenticated_portal"
    pkg_portal_scraper.PortalScraper(settings_with_creds).login(headed=True)
    assert fake_playwright.launch_calls
    assert fake_playwright.launch_calls[-1].get("headless") is False


def test_t1_f8_03_fetch_offer_details_extracts_contacts_and_apply_url(
    fake_playwright: FakePlaywrightEnv,
    settings_with_creds: Any,
    pkg_portal_scraper: Any,
) -> None:
    fake_playwright.scenario = "authenticated_portal"
    details = pkg_portal_scraper.PortalScraper(settings_with_creds).fetch_offer_details("10000-1206415041-S")
    assert "Linux" in (details.get("description") or "")
    assert details.get("contact_name") == "Frau Anna Weber"
    assert details.get("contact_email") == "karriere@mueller-it.example"
    assert details.get("contact_phone") == "+49 241 123456"
    assert "mueller-it.example" in (details.get("apply_url") or "")


def test_t1_f8_04_fetch_vermittlungsvorschlaege_returns_proposals(
    fake_playwright: FakePlaywrightEnv,
    settings_with_creds: Any,
    pkg_portal_scraper: Any,
) -> None:
    fake_playwright.scenario = "authenticated_portal"
    vv = pkg_portal_scraper.PortalScraper(settings_with_creds).fetch_vermittlungsvorschlaege()
    assert isinstance(vv, list) and len(vv) == 2
    for row in vv:
        assert row.get("refnr")
        assert row.get("title")
        assert row.get("company")
        assert row.get("is_vermittlungsvorschlag") is True


def test_t1_f8_05_login_with_otp_callback_fallback(
    fake_playwright: FakePlaywrightEnv,
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    pkg_config: Any,
    pkg_portal_scraper: Any,
) -> None:
    fake_playwright.scenario = "authenticated_portal"
    monkeypatch.setenv("BA_USERNAME", "fisi.test@example.de")
    monkeypatch.setenv("BA_PASSWORD", "SyntheticPassword!2026")
    settings = pkg_config.get_settings(env_file=isolated_env.env_file)
    res = pkg_portal_scraper.PortalScraper(settings).login(otp_callback=lambda: "654321")
    assert res.get("authenticated") is True or res.get("ok") is True
    assert any(val == "654321" for _, val in fake_playwright.filled_values)


# ===========================================================================
# Feature F9: Company Grouping, Scoring & Filtering (6 tests)
# ===========================================================================


def test_t1_f9_01_score_offer_fisi_keywords_and_bounds(pkg_models: Any, pkg_sorter: Any) -> None:
    offer_a = pkg_models.AusbildungOffer(
        refnr="10000-1",
        title="Ausbildung FISI",
        company="AixIT GmbH",
        distance_km=3.0,
        start_date="2026-08-01",
        description="Linux, Debian, Netzwerk, Active Directory, Proxmox, Docker, Firewall",
    )
    offer_b = pkg_models.AusbildungOffer(
        refnr="10000-2",
        title="Ausbildung Bürokaufmann",
        company="Büro GmbH",
        distance_km=20.0,
        description="Buchhaltung und Telefonzentrale",
    )
    scored_a = pkg_sorter.score_offer(offer_a)
    scored_b = pkg_sorter.score_offer(offer_b)
    assert 0.0 <= scored_a.score <= 100.0
    assert 0.0 <= scored_b.score <= 100.0
    assert scored_a.score > scored_b.score
    lower_skills = {s.lower() for s in scored_a.matched_skills}
    for kw in ("linux", "debian", "netzwerk", "proxmox", "docker", "firewall"):
        assert kw in lower_skills
    assert scored_b.matched_skills == []


def test_t1_f9_02_score_offer_custom_keywords(pkg_models: Any, pkg_sorter: Any) -> None:
    offer = pkg_models.AusbildungOffer(
        refnr="10000-3",
        title="Ausbildung Cloud",
        company="Cloud AG",
        description="Wir nutzen kubernetes, ANSIBLE und Python im Alltag.",
    )
    scored = pkg_sorter.score_offer(offer, keywords=["Kubernetes", "Ansible", "Python", "Terraform"])
    assert 0.0 <= scored.score <= 100.0
    lower = {s.lower() for s in scored.matched_skills}
    assert {"kubernetes", "ansible", "python"}.issubset(lower)
    assert "terraform" not in lower


def test_t1_f9_03_group_by_company_slug(pkg_models: Any, pkg_sorter: Any) -> None:
    AusbildungOffer = pkg_models.AusbildungOffer
    offers = [
        AusbildungOffer(refnr="R1", title="T1", company="AixIT GmbH", company_slug="aixit-gmbh"),
        AusbildungOffer(refnr="R2", title="T2", company="AixIT GmbH", company_slug="aixit-gmbh"),
        AusbildungOffer(refnr="R3", title="T3", company="Regio IT", company_slug="regio-it"),
        AusbildungOffer(refnr="R4", title="T4", company="Stadtwerke Aachen", company_slug="stadtwerke-aachen"),
    ]
    grouped = pkg_sorter.group_by_company(offers)
    assert set(grouped.keys()) == {"aixit-gmbh", "regio-it", "stadtwerke-aachen"}
    assert len(grouped["aixit-gmbh"]) == 2
    assert sum(len(v) for v in grouped.values()) == 4


def test_t1_f9_04_build_company_profiles_aggregation(pkg_models: Any, pkg_sorter: Any) -> None:
    AusbildungOffer = pkg_models.AusbildungOffer
    offers = [
        AusbildungOffer(
            refnr="R1",
            title="T1",
            company="Alpha IT GmbH",
            city="Aachen",
            plz="52062",
            score=85.0,
            apply_url="https://alpha.example/1",
        ),
        AusbildungOffer(
            refnr="R2",
            title="T2",
            company="Alpha IT GmbH",
            city="Würselen",
            plz="52146",
            score=55.0,
            apply_url="https://alpha.example/2",
        ),
        AusbildungOffer(
            refnr="R3",
            title="T3",
            company="Beta IT GmbH",
            city="Aachen",
            score=40.0,
        ),
    ]
    profiles = pkg_sorter.build_company_profiles(offers)
    assert len(profiles) == 2
    assert profiles[0].name == "Alpha IT GmbH"
    assert profiles[0].offer_count == 2
    assert profiles[0].max_score == 85.0
    assert profiles[0].avg_score == pytest.approx(70.0)
    assert set(profiles[0].offers) == {"R1", "R2"}
    assert set(profiles[0].cities) == {"Aachen", "Würselen"}
    assert set(profiles[0].apply_urls) == {"https://alpha.example/1", "https://alpha.example/2"}


def test_t1_f9_05_filter_offers_by_region_radius_dates_status(pkg_models: Any, pkg_sorter: Any) -> None:
    AusbildungOffer = pkg_models.AusbildungOffer
    ApplicationStatus = pkg_models.ApplicationStatus
    SearchFilter = pkg_models.SearchFilter
    offers = [
        AusbildungOffer(
            refnr="OK-1",
            title="Match",
            company="C1",
            city="Aachen",
            plz="52062",
            distance_km=5.0,
            start_date="2026-08-01",
            status=ApplicationStatus.NEW,
        ),
        AusbildungOffer(
            refnr="OK-EDGE",
            title="Edge Radius",
            company="C2",
            city="Aachen",
            plz="52062",
            distance_km=25.0,
            start_date="2026-09-01",
            status=ApplicationStatus.NEW,
        ),
        AusbildungOffer(
            refnr="FAR",
            title="Too Far",
            company="C3",
            city="Aachen",
            plz="52062",
            distance_km=40.0,
            start_date="2026-08-01",
            status=ApplicationStatus.NEW,
        ),
        AusbildungOffer(
            refnr="LATE",
            title="Too Late",
            company="C4",
            city="Aachen",
            plz="52062",
            distance_km=5.0,
            start_date="2027-02-01",
            status=ApplicationStatus.NEW,
        ),
        AusbildungOffer(
            refnr="IGNORED",
            title="Ignored",
            company="C5",
            city="Aachen",
            plz="52062",
            distance_km=5.0,
            start_date="2026-08-01",
            status=ApplicationStatus.IGNORED,
        ),
    ]
    f = SearchFilter(
        wo="Aachen",
        umkreis=25,
        start_date_from="2026-08-01",
        start_date_to="2026-09-30",
        status=ApplicationStatus.NEW,
    )
    matched = pkg_sorter.filter_offers(offers, f)
    assert [o.refnr for o in matched] == ["OK-1", "OK-EDGE"]

    plz_matched = pkg_sorter.filter_offers(offers, SearchFilter(wo="52062", umkreis=25, status=ApplicationStatus.NEW))
    assert {o.refnr for o in plz_matched} == {"OK-1", "OK-EDGE", "LATE"}


def test_t1_f9_06_default_search_filter_aachen_25km(pkg_models: Any, pkg_sorter: Any) -> None:
    default_filter = pkg_models.SearchFilter()
    assert default_filter.wo == "Aachen"
    assert default_filter.umkreis == 25
    offers = [
        pkg_models.AusbildungOffer(refnr="IN", title="In", company="C1", city="Aachen", distance_km=10.0),
        pkg_models.AusbildungOffer(refnr="OUT", title="Out", company="C2", city="Köln", distance_km=30.0),
    ]
    res = pkg_sorter.filter_offers(offers, default_filter)
    assert [o.refnr for o in res] == ["IN"]


# ===========================================================================
# Feature F10: LLM-Ready Markdown & JSON Exporter (5 tests)
# ===========================================================================


def test_t1_f10_01_export_creates_expected_directory_structure(
    isolated_env: E2EWorkspace,
    sample_fisi_offers: list[Any],
    pkg_exporter: Any,
) -> None:
    pkg_exporter.export_offers(sample_fisi_offers, isolated_env.export_dir)
    assert (isolated_env.export_dir / "INDEX_FOR_LLM.md").is_file()
    assert (isolated_env.export_dir / "companies_summary.json").is_file()
    for o in sample_fisi_offers:
        folder = isolated_env.export_dir / "offers" / f"{o.company_slug}__{o.refnr}"
        assert (folder / "offer.md").is_file()
        assert (folder / "offer.json").is_file()


def test_t1_f10_02_offer_md_yaml_frontmatter_all_16_keys(
    isolated_env: E2EWorkspace,
    sample_fisi_offers: list[Any],
    pkg_exporter: Any,
) -> None:
    yaml = require_module("yaml")
    offer = sample_fisi_offers[0].model_copy(update={"score": 88.5, "matched_skills": ["Linux", "Debian"]})
    pkg_exporter.export_offers([offer], isolated_env.export_dir)
    md_path = isolated_env.export_dir / "offers" / f"{offer.company_slug}__{offer.refnr}" / "offer.md"
    raw = md_path.read_text(encoding="utf-8")
    assert raw.startswith("---\n")
    parts = raw.split("---\n", 2)
    assert len(parts) >= 3
    fm = yaml.safe_load(parts[1])
    expected_keys = {
        "refnr",
        "title",
        "profession",
        "company",
        "plz",
        "city",
        "distance_km",
        "start_date",
        "contact_name",
        "contact_email",
        "contact_phone",
        "apply_url",
        "portal_url",
        "score",
        "matched_skills",
        "status",
    }
    assert expected_keys.issubset(set(fm.keys()))
    assert fm["refnr"] == offer.refnr
    assert fm["company"] == offer.company
    assert fm["score"] == pytest.approx(88.5)


def test_t1_f10_03_offer_md_exact_five_russian_headings(
    isolated_env: E2EWorkspace,
    sample_fisi_offers: list[Any],
    pkg_exporter: Any,
) -> None:
    offer = sample_fisi_offers[0].model_copy(update={"matched_skills": ["Linux", "Docker"]})
    pkg_exporter.export_offers([offer], isolated_env.export_dir)
    md_path = isolated_env.export_dir / "offers" / f"{offer.company_slug}__{offer.refnr}" / "offer.md"
    text = md_path.read_text(encoding="utf-8")
    headings = [
        "## Описание и задачи",
        "## Требования к кандидату",
        "## О компании",
        "## Контакты для Bewerbung",
        "## Подсказки для ИИ при генерации Anschreiben",
    ]
    positions = [text.find(h) for h in headings]
    assert all(pos != -1 for pos in positions), f"Missing required headings in {md_path}"
    assert positions == sorted(positions)


def test_t1_f10_04_offer_json_schema_and_utf8(
    isolated_env: E2EWorkspace,
    sample_fisi_offers: list[Any],
    pkg_exporter: Any,
) -> None:
    offer = sample_fisi_offers[0]
    pkg_exporter.export_offers([offer], isolated_env.export_dir)
    json_path = isolated_env.export_dir / "offers" / f"{offer.company_slug}__{offer.refnr}" / "offer.json"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["refnr"] == offer.refnr
    assert "Müller & Söhne" in data["company"]
    assert "Straße" in (data.get("street") or "")
    assert data["portal_url"] == offer.portal_url


def test_t1_f10_05_index_for_llm_and_companies_summary(
    isolated_env: E2EWorkspace,
    sample_fisi_offers: list[Any],
    pkg_exporter: Any,
) -> None:
    scored = [
        sample_fisi_offers[2].model_copy(update={"score": 90.0}),
        sample_fisi_offers[0].model_copy(update={"score": 65.0}),
        sample_fisi_offers[1].model_copy(update={"score": 40.0}),
    ]
    pkg_exporter.export_offers(scored, isolated_env.export_dir)
    index_text = (isolated_env.export_dir / "INDEX_FOR_LLM.md").read_text(encoding="utf-8")
    pos_90 = index_text.find(scored[0].refnr)
    pos_65 = index_text.find(scored[1].refnr)
    pos_40 = index_text.find(scored[2].refnr)
    assert -1 not in (pos_90, pos_65, pos_40)
    assert pos_90 < pos_65 < pos_40

    summary = json.loads((isolated_env.export_dir / "companies_summary.json").read_text(encoding="utf-8"))
    companies = summary if isinstance(summary, list) else summary.get("companies", [])
    assert len(companies) == 2


# ===========================================================================
# Feature F11: jobsuche CLI (6 tests)
# ===========================================================================


def test_t1_f11_01_cli_help_root_and_all_7_subcommands(isolated_env: E2EWorkspace) -> None:
    root_proc = run_cli(["--help"], workspace=isolated_env)
    assert root_proc.returncode == 0
    for subcmd in ("login", "search", "sync", "export", "companies", "status", "otp-test"):
        sub_proc = run_cli([subcmd, "--help"], workspace=isolated_env)
        assert sub_proc.returncode == 0, f"`jobsuche {subcmd} --help` failed: {sub_proc.stderr}"


def test_t1_f11_02_otp_test_valid_secret(isolated_env: E2EWorkspace) -> None:
    pyotp = require_module("pyotp")
    proc = run_cli(
        ["otp-test"],
        workspace=isolated_env,
        extra_env={"BA_TOTP_SECRET": VALID_TEST_TOTP_SECRET},
    )
    assert proc.returncode == 0, f"otp-test failed: {proc.stderr}"
    match = re.search(r"\b(\d{6})\b", proc.stdout)
    assert match is not None, f"No 6-digit OTP found in stdout: {proc.stdout!r}"
    assert pyotp.TOTP(VALID_TEST_TOTP_SECRET).verify(match.group(1), valid_window=1)


def test_t1_f11_03_search_defaults_and_formats(
    isolated_env: E2EWorkspace,
    local_jobsuche_server: tuple[str, FakeJobsucheRouter],
) -> None:
    base_url, router = local_jobsuche_server
    json_proc = run_cli(["search", "--format", "json"], workspace=isolated_env, api_base_url=base_url)
    assert json_proc.returncode == 0, f"search json failed: {json_proc.stderr}"
    parsed = json.loads(json_proc.stdout)
    items = parsed if isinstance(parsed, list) else parsed.get("offers", parsed.get("results", []))
    assert len(items) >= 2
    assert any(r.query.get("wo") == ["Aachen"] and r.query.get("umkreis") == ["25"] for r in router.requests)

    table_proc = run_cli(["search", "--format", "table"], workspace=isolated_env, api_base_url=base_url)
    assert table_proc.returncode == 0

    md_proc = run_cli(["search", "--format", "markdown"], workspace=isolated_env, api_base_url=base_url)
    assert md_proc.returncode == 0
    assert "|" in md_proc.stdout or "#" in md_proc.stdout


def test_t1_f11_04_sync_export_and_companies_workflow(
    isolated_env: E2EWorkspace,
    local_jobsuche_server: tuple[str, FakeJobsucheRouter],
) -> None:
    base_url, _ = local_jobsuche_server
    sync_proc = run_cli(
        ["sync", "--was", "Fachinformatiker", "--wo", "Aachen", "--umkreis", "25"],
        workspace=isolated_env,
        api_base_url=base_url,
    )
    assert sync_proc.returncode == 0, f"sync failed: {sync_proc.stderr}"

    exp_proc = run_cli(["export"], workspace=isolated_env, api_base_url=base_url)
    assert exp_proc.returncode == 0, f"export failed: {exp_proc.stderr}"
    assert (isolated_env.export_dir / "INDEX_FOR_LLM.md").is_file()
    assert (isolated_env.export_dir / "companies_summary.json").is_file()
    assert list((isolated_env.export_dir / "offers").glob("*/offer.md"))

    comp_proc = run_cli(["companies", "--format", "json"], workspace=isolated_env, api_base_url=base_url)
    assert comp_proc.returncode == 0, f"companies failed: {comp_proc.stderr}"
    companies_data = json.loads(comp_proc.stdout)
    assert len(companies_data) >= 2


def test_t1_f11_05_status_update_persists_in_db(
    isolated_env: E2EWorkspace,
    job_db: Any,
    sample_fisi_offers: list[Any],
) -> None:
    offer = sample_fisi_offers[0].model_copy(update={"refnr": "10000-1"})
    job_db.upsert_offer(offer)
    proc = run_cli(
        ["status", "10000-1", "shortlisted", "--notes", "Top match"],
        workspace=isolated_env,
    )
    assert proc.returncode == 0, f"status command failed: {proc.stderr}"
    refreshed = job_db.get_offer("10000-1")
    assert refreshed is not None
    assert refreshed.status.value == "shortlisted"
    assert refreshed.notes == "Top match"


def test_t1_f11_06_login_headed_flag_accepted_by_parser(isolated_env: E2EWorkspace) -> None:
    proc = run_cli(["login", "--help"], workspace=isolated_env)
    assert proc.returncode == 0
    assert "--headed" in proc.stdout


# ===========================================================================
# Feature F12: Antigravity CLI Plugin & MCP Server (5 tests)
# ===========================================================================


def test_t1_f12_01_agy_plugin_validate_strict_json() -> None:
    agy_bin = shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")
    assert Path(agy_bin).is_file(), f"agy CLI not found at {agy_bin}"
    proc = subprocess.run(
        [agy_bin, "plugin", "validate", str(ANTIGRAVITY_PLUGIN_DIR), "--strict", "--json"],
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    assert proc.returncode == 0, f"agy plugin validate failed: {proc.stdout}\n{proc.stderr}"
    data = json.loads(proc.stdout)
    assert data["name"] == "jobsuche"
    components = data["components"]
    assert components["mcpServers"] >= 1
    assert components["rules"] >= 1
    assert components["skills"] >= 1
    assert data.get("findings", []) == []


def test_t1_f12_02_manifest_mcp_config_rules_and_skill_contracts() -> None:
    yaml = require_module("yaml")
    plugin_json = json.loads((ANTIGRAVITY_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    assert plugin_json["name"] == "jobsuche"
    assert plugin_json["version"]
    assert plugin_json["description"]
    assert len(plugin_json.get("suggestedPrompts", [])) <= 3

    mcp_cfg = json.loads((ANTIGRAVITY_PLUGIN_DIR / "mcp_config.json").read_text(encoding="utf-8"))
    servers = mcp_cfg["mcpServers"]
    assert len(servers) >= 1
    srv = next(iter(servers.values()))
    assert "${PLUGIN_ROOT}" in " ".join(str(a) for a in srv.get("args", []))
    assert "${PLUGIN_ROOT}" not in str(srv.get("cwd", ""))

    rules_path = ANTIGRAVITY_PLUGIN_DIR / "rules" / "AGENTS.md"
    assert rules_path.is_file()
    rules_bytes = rules_path.read_bytes()
    assert len(rules_bytes) < 24000
    assert not rules_bytes.decode("utf-8").lstrip().startswith("---")

    skill_path = ANTIGRAVITY_PLUGIN_DIR / "skills" / "jobsuche" / "SKILL.md"
    assert skill_path.is_file()
    skill_text = skill_path.read_text(encoding="utf-8")
    assert skill_text.startswith("---\n")
    fm = yaml.safe_load(skill_text.split("---\n", 2)[1])
    assert fm["name"] == "jobsuche"
    assert fm["description"]


def test_t1_f12_03_mcp_stdio_initialize_and_tools_list(
    isolated_env: E2EWorkspace,
    local_jobsuche_server: tuple[str, FakeJobsucheRouter],
) -> None:
    base_url, _ = local_jobsuche_server

    async def _check(session: Any, init_result: Any) -> tuple[Any, list[Any]]:
        tools_result = await session.list_tools()
        return init_result, tools_result.tools

    init_res, tools = run_mcp_stdio(isolated_env, _check, api_base_url=base_url)
    assert init_res.serverInfo is not None
    assert init_res.capabilities.tools is not None
    expected_tools = {
        "jobsuche_search_ausbildung",
        "jobsuche_fetch_offer_details",
        "jobsuche_sync_and_export",
        "jobsuche_list_offers",
        "jobsuche_update_offer_status",
        "jobsuche_auth_status",
    }
    tool_map = {t.name: t for t in tools}
    assert expected_tools.issubset(set(tool_map.keys()))
    for name in expected_tools:
        assert tool_map[name].inputSchema is not None


def test_t1_f12_04_mcp_stdio_tools_call_happy_paths(
    isolated_env: E2EWorkspace,
    local_jobsuche_server: tuple[str, FakeJobsucheRouter],
) -> None:
    base_url, _ = local_jobsuche_server

    async def _exercise(session: Any, init_result: Any) -> dict[str, dict[str, Any]]:
        r_search = await session.call_tool(
            "jobsuche_search_ausbildung",
            {"was": "Fachinformatiker", "wo": "Aachen", "umkreis": 25, "mode": "api"},
        )
        r_details = await session.call_tool(
            "jobsuche_fetch_offer_details",
            {"refnr": "10000-1206415041-S"},
        )
        r_sync = await session.call_tool(
            "jobsuche_sync_and_export",
            {"was": "Fachinformatiker", "wo": "Aachen", "umkreis": 25},
        )
        r_upd = await session.call_tool(
            "jobsuche_update_offer_status",
            {"refnr": "10000-1206415041-S", "status": "shortlisted", "notes": "MCP shortlisted"},
        )
        r_list = await session.call_tool(
            "jobsuche_list_offers",
            {"status": "shortlisted"},
        )
        r_auth = await session.call_tool("jobsuche_auth_status", {})
        return {
            "search": _extract_mcp_json(r_search),
            "details": _extract_mcp_json(r_details),
            "sync": _extract_mcp_json(r_sync),
            "update": _extract_mcp_json(r_upd),
            "list": _extract_mcp_json(r_list),
            "auth": _extract_mcp_json(r_auth),
        }

    out = run_mcp_stdio(
        isolated_env,
        _exercise,
        api_base_url=base_url,
        extra_env={"BA_TOTP_SECRET": VALID_TEST_TOTP_SECRET},
    )
    assert out["search"]
    assert out["details"]
    assert (isolated_env.export_dir / "INDEX_FOR_LLM.md").is_file()
    assert (isolated_env.export_dir / "companies_summary.json").is_file()
    assert "10000-1206415041-S" in json.dumps(out["list"])
    assert out["auth"]


def test_t1_f12_05_global_gemini_config_preserved_and_non_conflicting() -> None:
    mcp_cfg = json.loads((GEMINI_CONFIG_DIR / "mcp_config.json").read_text(encoding="utf-8"))
    for server_name in ("deckcraft", "gridcraft", "mailcraft", "wordcraft"):
        assert server_name in mcp_cfg["mcpServers"]
        assert mcp_cfg["mcpServers"][server_name].get("disabled") is False

    cfg = json.loads((GEMINI_CONFIG_DIR / "config.json").read_text(encoding="utf-8"))
    assert cfg["plugins"]["swarm"]["enabled"] is True

    agy_bin = shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")
    proc = subprocess.run([agy_bin, "plugin", "list", "--json"], capture_output=True, text=True, timeout=20.0)
    assert proc.returncode == 0
    agy_plugins = json.loads(proc.stdout)

    plugin_dir_linked = (GEMINI_CONFIG_DIR / "plugins" / "jobsuche").exists()
    plugins_json_path = GEMINI_CONFIG_DIR / "plugins.json"
    plugins_json_linked = False
    if plugins_json_path.is_file():
        plugins_json_text = plugins_json_path.read_text(encoding="utf-8")
        plugins_json_linked = "jobsuche" in plugins_json_text or ".agents/plugins" in plugins_json_text
    mcp_linked = "jobsuche" in mcp_cfg.get("mcpServers", {})
    agy_listed = "jobsuche" in json.dumps(agy_plugins)
    assert plugin_dir_linked or plugins_json_linked or mcp_linked or agy_listed, (
        "Expected jobsuche plugin to be safely linked/registered in ~/.gemini/config/ or `agy plugin list --json`"
    )


# ===========================================================================
# Feature F13: Hermes Agent Standalone v2 Plugin & Skill (5 tests)
# ===========================================================================


def test_t1_f13_01_hermes_plugins_validate_json() -> None:
    hermes_bin = shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")
    assert Path(hermes_bin).is_file(), f"hermes CLI not found at {hermes_bin}"
    proc = subprocess.run(
        [hermes_bin, "plugins", "validate", str(HERMES_PLUGIN_DIR), "--json"],
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    assert proc.returncode == 0, f"hermes plugins validate failed: {proc.stdout}\n{proc.stderr}"
    data = json.loads(proc.stdout)
    assert data["ok"] is True
    assert all(c.get("ok") is True for c in data.get("checks", []))
    assert data.get("isolation", {}).get("host_ready") is True


def test_t1_f13_02_hermes_plugins_doctor_ci() -> None:
    hermes_bin = shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")
    proc = subprocess.run(
        [hermes_bin, "plugins", "doctor", "--ci", str(HERMES_PLUGIN_DIR)],
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    assert proc.returncode == 0, f"hermes plugins doctor failed: {proc.stdout}\n{proc.stderr}"
    combined = proc.stdout + proc.stderr
    assert "standalone" in combined
    assert "4 tool(s)" in combined


def test_t1_f13_03_manifest_and_register_ctx_contract(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    hermes_recording_ctx: FakeHermesContext,
) -> None:
    yaml = require_module("yaml")
    manifest = yaml.safe_load((HERMES_PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 2
    assert manifest["kind"] == "standalone"
    assert manifest["name"] == "jobsuche"
    expected_tools = ["jobsuche_search", "jobsuche_monitor", "jobsuche_export_llm", "jobsuche_status"]
    assert set(manifest["provides_tools"]) == set(expected_tools)

    plugin = load_hermes_jobsuche_module(monkeypatch, hermes_home=isolated_env.root / "hermes_home")
    plugin.register(hermes_recording_ctx)

    assert set(hermes_recording_ctx.tools.keys()) == set(expected_tools)
    for name, entry in hermes_recording_ctx.tools.items():
        schema = entry["schema"]
        assert schema["name"] == name
        assert schema["description"]
        assert schema["parameters"]["type"] == "object"
    assert "jobsuche" in hermes_recording_ctx.skills
    assert hermes_recording_ctx.skills["jobsuche"]["path"].is_file()


def test_t1_f13_04_handlers_json_and_telegram_digest_happy_paths(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    local_jobsuche_server: tuple[str, FakeJobsucheRouter],
    hermes_recording_ctx: FakeHermesContext,
) -> None:
    base_url, _ = local_jobsuche_server
    monkeypatch.setenv("JOBSUCHE_API_BASE_URL", base_url)
    monkeypatch.setenv("API_BASE_URL", base_url)
    plugin = load_hermes_jobsuche_module(monkeypatch, hermes_home=isolated_env.root / "hermes_home")
    plugin.register(hermes_recording_ctx)

    h_search = hermes_recording_ctx.tools["jobsuche_search"]["handler"]
    h_monitor = hermes_recording_ctx.tools["jobsuche_monitor"]["handler"]
    h_export = hermes_recording_ctx.tools["jobsuche_export_llm"]["handler"]
    h_status = hermes_recording_ctx.tools["jobsuche_status"]["handler"]

    res_search = json.loads(h_search({"was": "Fachinformatiker", "wo": "Aachen", "umkreis": 25}))
    assert res_search["ok"] is True

    res_mon1 = json.loads(h_monitor({"format": "telegram", "db_path": str(isolated_env.db_path)}))
    assert res_mon1["ok"] is True
    assert res_mon1["new_count"] >= 1
    assert res_mon1.get("message")

    res_mon2 = json.loads(h_monitor({"format": "telegram", "db_path": str(isolated_env.db_path)}))
    assert res_mon2["ok"] is True
    assert res_mon2["new_count"] == 0

    res_exp = json.loads(
        h_export({"db_path": str(isolated_env.db_path), "export_dir": str(isolated_env.export_dir)})
    )
    assert res_exp["ok"] is True
    assert (isolated_env.export_dir / "INDEX_FOR_LLM.md").is_file()
    assert (isolated_env.export_dir / "companies_summary.json").is_file()

    res_st = json.loads(h_status({"db_path": str(isolated_env.db_path)}))
    assert res_st["ok"] is True


def test_t1_f13_05_skill_md_frontmatter_and_hermes_skill_linter() -> None:
    yaml = require_module("yaml")
    skill_dir = HERMES_PLUGIN_DIR / "skills" / "jobsuche"
    skill_md = skill_dir / "SKILL.md"
    assert skill_md.is_file()
    text = skill_md.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    fm = yaml.safe_load(text.split("---\n", 2)[1])
    assert fm["name"] == "jobsuche"
    assert len(str(fm["description"])) <= 60
    assert fm.get("metadata", {}).get("hermes", {}).get("tags")
    assert "## When to Use" in text
    for forbidden in ("README.md", ".env", ".gitignore"):
        assert not (skill_dir / forbidden).exists(), f"Forbidden file inside skill dir: {forbidden}"

    assert HERMES_PYTHON.is_file(), f"Missing Hermes python: {HERMES_PYTHON}"
    proc = subprocess.run(
        [
            str(HERMES_PYTHON),
            "-I",
            "-c",
            (
                f"import sys; sys.path.insert(0, {str(HERMES_AGENT_DIR)!r}); "
                "import hermes_bootstrap; "
                "from pathlib import Path; from tools.skill_linter import lint_skill; "
                f"findings = lint_skill(Path({str(skill_md)!r})); "
                "print(findings); raise SystemExit(1 if findings else 0)"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=20.0,
    )
    assert proc.returncode == 0, f"Hermes skill_linter reported findings: {proc.stdout}\n{proc.stderr}"
