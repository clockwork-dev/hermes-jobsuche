from __future__ import annotations

import json
from pathlib import Path
import re
import socket
from typing import Any

import pytest

from tests.e2e.conftest import (
    E2EWorkspace,
    FakeClock,
    FakeHermesContext,
    FakeIMAPMailbox,
    FakeJobsucheRouter,
    FakePlaywrightEnv,
    HERMES_CONFIG_PATH,
    PROJECT_ROOT,
    VALID_TEST_TOTP_SECRET,
    load_hermes_jobsuche_module,
    require_module,
    run_cli,
    run_mcp_stdio,
)


def _open_stealth_context(manager: Any, callback: Any) -> Any:
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


def _mcp_result_indicates_error(result: Any) -> bool:
    if getattr(result, "isError", False):
        return True
    texts = [getattr(item, "text", "") for item in getattr(result, "content", [])]
    joined = "\n".join(t for t in texts if t).strip()
    if not joined:
        return False
    try:
        parsed = json.loads(joined)
        if isinstance(parsed, dict) and parsed.get("ok") is False:
            return True
        if isinstance(parsed, dict) and ("error" in parsed or "errors" in parsed):
            return True
    except json.JSONDecodeError:
        if any(tok in joined.lower() for tok in ("error", "invalid", "not found", "missing")):
            return True
    return False


# ===========================================================================
# Feature F1: Package Scaffolding & CLI Entrypoint Boundaries (5 tests)
# ===========================================================================


def test_t2_f1_01_env_example_contains_no_real_secrets() -> None:
    env_example = PROJECT_ROOT / ".env.example"
    assert env_example.is_file()
    secret_keys = {"BA_USERNAME", "BA_PASSWORD", "BA_TOTP_SECRET", "BA_IMAP_PASSWORD"}
    for line in env_example.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, v = stripped.split("=", 1)
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        if key in secret_keys and val:
            lower = val.lower()
            assert any(
                marker in lower for marker in ("example", "your_", "placeholder", "changeme", "xxx", "demo", "<")
            ), f"Suspicious non-placeholder credential value in .env.example for {key}: {val!r}"


def test_t2_f1_02_source_has_no_hardcoded_ba_credentials() -> None:
    pkg_dir = PROJECT_ROOT / "src" / "jobsuche_pkg"
    assert pkg_dir.is_dir()
    py_files = list(pkg_dir.glob("*.py"))
    assert py_files
    for py_file in py_files:
        text = py_file.read_text(encoding="utf-8")
        assert not re.search(r'BA_PASSWORD\s*=\s*["\'][^"\']{4,}["\']', text), f"Hardcoded password in {py_file}"
        assert not re.search(r'BA_TOTP_SECRET\s*=\s*["\'][A-Z2-7]{16,}["\']', text), f"Hardcoded TOTP in {py_file}"


def test_t2_f1_03_readme_documents_setup_and_commands() -> None:
    readme = PROJECT_ROOT / "README.md"
    assert readme.is_file()
    text = readme.read_text(encoding="utf-8")
    assert ".venv" in text or "pip install" in text
    assert ".env" in text
    assert "Aachen" in text or "jobsuche" in text
    for subcmd in ("search", "sync", "export"):
        assert subcmd in text, f"README.md missing CLI command documentation for {subcmd!r}"


def test_t2_f1_04_cli_unknown_subcommand_exits_nonzero(isolated_env: E2EWorkspace) -> None:
    proc = run_cli(["unknown_cmd_xyz"], workspace=isolated_env)
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "Traceback (most recent call last)" not in combined
    assert combined.strip()


def test_t2_f1_05_imports_have_no_filesystem_side_effects(isolated_env: E2EWorkspace) -> None:
    for mod in ("config", "models", "db", "api_client", "auth_otp", "browser", "portal_scraper"):
        require_module(f"jobsuche_pkg.{mod}")
    assert not isolated_env.data_dir.exists()
    assert not isolated_env.export_dir.exists()


# ===========================================================================
# Feature F2: Config & Env Loading Boundaries (5 tests)
# ===========================================================================


def test_t2_f2_01_missing_ba_credentials_safe(settings_without_creds: Any) -> None:
    assert settings_without_creds.ba_username is None
    assert settings_without_creds.ba_password is None
    assert settings_without_creds.ba_totp_secret is None


def test_t2_f2_02_empty_string_credentials_do_not_crash(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    pkg_config: Any,
) -> None:
    monkeypatch.setenv("BA_USERNAME", "")
    monkeypatch.setenv("BA_PASSWORD", "")
    monkeypatch.setenv("BA_TOTP_SECRET", "")
    monkeypatch.setenv("BROWSER_EXECUTABLE_PATH", "")
    s = pkg_config.get_settings(env_file=isolated_env.env_file)
    assert not s.ba_username
    assert not s.ba_password
    assert not s.ba_totp_secret
    assert not s.browser_executable_path


def test_t2_f2_03_invalid_integer_radius_rejected(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    pkg_config: Any,
) -> None:
    pydantic = require_module("pydantic")
    monkeypatch.setenv("JOBSUCHE_DEFAULT_RADIUS", "not_a_number")
    with pytest.raises((pydantic.ValidationError, ValueError)):
        pkg_config.get_settings(env_file=isolated_env.env_file)


def test_t2_f2_04_invalid_imap_port_rejected(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    pkg_config: Any,
) -> None:
    pydantic = require_module("pydantic")
    monkeypatch.setenv("BA_IMAP_PORT", "invalid_port")
    with pytest.raises((pydantic.ValidationError, ValueError)):
        pkg_config.get_settings(env_file=isolated_env.env_file)


def test_t2_f2_05_zero_radius_and_nested_nonexistent_paths_allowed(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    pkg_config: Any,
) -> None:
    nested = isolated_env.root / "a" / "b" / "c.db"
    monkeypatch.setenv("JOBSUCHE_DEFAULT_RADIUS", "0")
    monkeypatch.setenv("JOBSUCHE_DB_PATH", str(nested))
    s = pkg_config.get_settings(env_file=isolated_env.env_file)
    assert s.default_umkreis == 0
    assert isinstance(s.db_path, Path) and s.db_path == nested


# ===========================================================================
# Feature F3: Domain Data Models Boundaries (5 tests)
# ===========================================================================


def test_t2_f3_01_slugify_company_empty_and_symbols_fallback(pkg_models: Any) -> None:
    for bad in ("", "   ", "---///!!!"):
        assert pkg_models.slugify_company(bad) == "unbekannt"


def test_t2_f3_02_slugify_company_prevents_path_traversal(pkg_models: Any) -> None:
    for raw in ("../../etc/passwd", "ACME/../../secret", "..\\..\\win\\system32"):
        slug = pkg_models.slugify_company(raw)
        assert slug
        assert "/" not in slug
        assert "\\" not in slug
        assert ".." not in slug


def test_t2_f3_03_search_filter_negative_umkreis_rejected(pkg_models: Any) -> None:
    pydantic = require_module("pydantic")
    assert pkg_models.SearchFilter(umkreis=0).umkreis == 0
    with pytest.raises((pydantic.ValidationError, ValueError)):
        pkg_models.SearchFilter(umkreis=-1)


def test_t2_f3_04_offer_invalid_status_rejected(pkg_models: Any) -> None:
    pydantic = require_module("pydantic")
    with pytest.raises((pydantic.ValidationError, ValueError)):
        pkg_models.AusbildungOffer(
            refnr="R-1",
            title="T",
            company="C",
            status="invalid_status",
        )


def test_t2_f3_05_offer_missing_required_fields_and_mutable_default_isolation(pkg_models: Any) -> None:
    pydantic = require_module("pydantic")
    with pytest.raises((pydantic.ValidationError, ValueError, TypeError)):
        pkg_models.AusbildungOffer(title="No refnr", company="Co")
    with pytest.raises((pydantic.ValidationError, ValueError, TypeError)):
        pkg_models.AusbildungOffer(refnr="R-1", company="Co")

    o1 = pkg_models.AusbildungOffer(refnr="R-1", title="T1", company="C1")
    o2 = pkg_models.AusbildungOffer(refnr="R-2", title="T2", company="C2")
    o1.matched_skills.append("Linux")
    assert o2.matched_skills == []


# ===========================================================================
# Feature F4: Jobsuche REST API Client Boundaries (5 tests)
# ===========================================================================


def test_t2_f4_01_empty_search_results_returns_empty_list(
    mock_jobsuche_http: FakeJobsucheRouter,
    settings_without_creds: Any,
    pkg_api_client: Any,
    pkg_models: Any,
) -> None:
    client = pkg_api_client.JobsucheAPIClient(settings_without_creds)
    res = client.search_jobs(pkg_models.SearchFilter(was="__NONE__"))
    assert res == []


def test_t2_f4_02_partial_offer_item_missing_optional_nested_fields(
    mock_jobsuche_http: FakeJobsucheRouter,
    settings_without_creds: Any,
    pkg_api_client: Any,
    pkg_models: Any,
) -> None:
    mock_jobsuche_http.custom_search_payload = {
        "maxErgebnisse": 1,
        "page": 1,
        "size": 25,
        "ergebnisliste": [
            {
                "referenznummer": "10000-PARTIAL-1",
                "stellenangebotsTitel": "Ausbildung IT",
                "firma": "Minimal GmbH",
            }
        ],
    }
    client = pkg_api_client.JobsucheAPIClient(settings_without_creds)
    offers = client.search_jobs(pkg_models.SearchFilter())
    assert len(offers) == 1
    assert offers[0].refnr == "10000-PARTIAL-1"
    assert offers[0].plz is None
    assert offers[0].city is None
    assert offers[0].distance_km is None


def test_t2_f4_03_http_403_handled_without_unhandled_crash(
    mock_jobsuche_http: FakeJobsucheRouter,
    settings_without_creds: Any,
    pkg_api_client: Any,
    pkg_models: Any,
) -> None:
    mock_jobsuche_http.status_overrides["search"] = 403
    client = pkg_api_client.JobsucheAPIClient(settings_without_creds)
    try:
        res = client.search_jobs(pkg_models.SearchFilter())
        assert res == []
    except Exception as exc:
        assert not isinstance(exc, (json.JSONDecodeError, KeyError, IndexError, AttributeError))


def test_t2_f4_04_http_404_job_details_handled_gracefully(
    mock_jobsuche_http: FakeJobsucheRouter,
    settings_without_creds: Any,
    pkg_api_client: Any,
    pkg_models: Any,
) -> None:
    client = pkg_api_client.JobsucheAPIClient(settings_without_creds)
    try:
        details = client.get_job_details("10000-UNKNOWN-S")
        assert details == {} or not details
    except Exception as exc:
        assert not isinstance(exc, (json.JSONDecodeError, KeyError, IndexError))

    offer = pkg_models.AusbildungOffer(
        refnr="10000-UNKNOWN-S",
        title="Existing Title",
        company="Existing Co",
        description="Preserved description",
    )
    enriched = client.enrich_offer(offer, fetch_api_details=True)
    assert enriched.title == "Existing Title"
    assert enriched.description == "Preserved description"


def test_t2_f4_05_network_timeout_and_non_json_502_handled(
    mock_jobsuche_http: FakeJobsucheRouter,
    settings_without_creds: Any,
    pkg_api_client: Any,
    pkg_models: Any,
) -> None:
    client = pkg_api_client.JobsucheAPIClient(settings_without_creds)

    mock_jobsuche_http.simulate_timeout = True
    with pytest.raises(Exception) as exc_to:
        client.search_jobs(pkg_models.SearchFilter())
    assert not isinstance(exc_to.value, (json.JSONDecodeError, KeyError))

    mock_jobsuche_http.simulate_timeout = False
    mock_jobsuche_http.malformed_body = b"<html>502 Bad Gateway</html>"
    with pytest.raises(Exception) as exc_502:
        client.search_jobs(pkg_models.SearchFilter())
    assert not isinstance(exc_502.value, (json.JSONDecodeError, KeyError))


# ===========================================================================
# Feature F5: SQLite Storage & History Boundaries (5 tests)
# ===========================================================================


def test_t2_f5_01_get_missing_offer_and_history_safe(job_db: Any) -> None:
    assert job_db.get_offer("MISSING-1") is None
    assert job_db.get_history("MISSING-1") == []


def test_t2_f5_02_update_status_missing_refnr_raises(job_db: Any, pkg_models: Any) -> None:
    with pytest.raises((KeyError, ValueError)):
        job_db.update_status("MISSING-REF", pkg_models.ApplicationStatus.APPLIED)


def test_t2_f5_03_update_status_invalid_status_rejected(
    job_db: Any,
    sample_fisi_offers: list[Any],
    pkg_models: Any,
) -> None:
    offer = sample_fisi_offers[0]
    job_db.upsert_offer(offer)
    with pytest.raises((ValueError, KeyError)):
        job_db.update_status(offer.refnr, "not_a_valid_status")
    stored = job_db.get_offer(offer.refnr)
    assert stored is not None
    assert stored.status == pkg_models.ApplicationStatus.NEW


def test_t2_f5_04_sql_injection_payloads_safe(
    job_db: Any,
    sample_fisi_offers: list[Any],
) -> None:
    offer = sample_fisi_offers[0]
    job_db.upsert_offer(offer)
    res = job_db.list_offers(city="Aachen'; DROP TABLE offers; --")
    assert res == []
    evil_note = "'); DELETE FROM offers; --"
    updated = job_db.update_status(offer.refnr, "shortlisted", notes=evil_note)
    assert updated.notes == evil_note
    assert job_db.get_offer(offer.refnr) is not None


def test_t2_f5_05_empty_db_queries_and_reopen_after_context_exit(
    isolated_env: E2EWorkspace,
    sample_fisi_offers: list[Any],
    pkg_db: Any,
) -> None:
    with pkg_db.JobDatabase(isolated_env.db_path) as db1:
        assert db1.list_offers() == []
        assert db1.list_companies() == []
        assert db1.upsert_offers([]) == {"inserted": 0, "updated": 0, "unchanged": 0}
        db1.upsert_offer(sample_fisi_offers[0])

    with pkg_db.JobDatabase(isolated_env.db_path) as db2:
        stored = db2.get_offer(sample_fisi_offers[0].refnr)
        assert stored is not None
        assert stored.title == sample_fisi_offers[0].title


# ===========================================================================
# Feature F6: 6-Digit TOTP & Fallback OTP Boundaries (5 tests)
# ===========================================================================


def test_t2_f6_01_totp_exact_3s_threshold_boundary(
    totp_clock: FakeClock,
    pkg_auth_otp: Any,
) -> None:
    pyotp = require_module("pyotp")
    secret = VALID_TEST_TOTP_SECRET
    totp = pyotp.TOTP(secret)

    # 3.01s remaining (1700000010 + 26.99) -> NO sleep, current tick
    totp_clock.set(1700000036.99)
    code_ok, _ = pkg_auth_otp.generate_totp(
        secret,
        min_remaining_seconds=3.0,
        now=totp_clock.now(),
        sleep_fn=totp_clock.sleep,
    )
    assert totp_clock.sleep_calls == []
    assert code_ok == totp.at(1700000036)

    # 2.99s remaining (1700000010 + 27.01) -> MUST sleep & roll to next tick (1700000040)
    totp_clock.set(1700000037.01)
    code_roll, rem_roll = pkg_auth_otp.generate_totp(
        secret,
        min_remaining_seconds=3.0,
        now=totp_clock.now(),
        sleep_fn=totp_clock.sleep,
    )
    assert len(totp_clock.sleep_calls) >= 1
    assert code_roll == totp.at(1700000040)
    assert rem_roll >= 25.0


def test_t2_f6_02_missing_or_empty_totp_secret_raises(
    totp_clock: FakeClock,
    pkg_auth_otp: Any,
) -> None:
    for bad in ("", "   "):
        with pytest.raises((pkg_auth_otp.OTPConfigError, ValueError)):
            pkg_auth_otp.generate_totp(bad, now=totp_clock.now(), sleep_fn=totp_clock.sleep)


def test_t2_f6_03_invalid_base32_secret_raises(
    totp_clock: FakeClock,
    pkg_auth_otp: Any,
) -> None:
    with pytest.raises((pkg_auth_otp.OTPConfigError, pkg_auth_otp.OTPError, ValueError)):
        pkg_auth_otp.generate_totp("INVALID!SECRET#123", now=totp_clock.now(), sleep_fn=totp_clock.sleep)


def test_t2_f6_04_no_totp_no_imap_no_callback_raises(
    settings_without_creds: Any,
    pkg_auth_otp: Any,
) -> None:
    with pytest.raises((pkg_auth_otp.OTPConfigError, pkg_auth_otp.OTPError)):
        pkg_auth_otp.get_otp_code(settings_without_creds, cli_callback=None)


def test_t2_f6_05_imap_missing_code_and_invalid_callback_rejected(
    mock_imap: FakeIMAPMailbox,
    settings_without_creds: Any,
    pkg_auth_otp: Any,
) -> None:
    mock_imap.add_plain_email("Newsletter", "Willkommen bei der Bundesagentur für Arbeit!")
    with pytest.raises(pkg_auth_otp.OTPError):
        pkg_auth_otp.fetch_otp_via_imap(
            host="imap.example.de",
            port=993,
            username="fisi@example.de",
            password="pw",
        )

    with pytest.raises((pkg_auth_otp.OTPError, pkg_auth_otp.OTPConfigError, ValueError)):
        pkg_auth_otp.get_otp_code(settings_without_creds, cli_callback=lambda: "12ab")


# ===========================================================================
# Feature F7: Stealth Playwright Profile & CAPTCHA Guard Boundaries (5 tests)
# ===========================================================================


def test_t2_f7_01_captcha_detection_saves_screenshot_status_and_raises_no_bypass(
    fake_playwright: FakePlaywrightEnv,
    settings_without_creds: Any,
    isolated_env: E2EWorkspace,
    pkg_browser: Any,
) -> None:
    fake_playwright.scenario = "captcha_login"
    manager = pkg_browser.StealthBrowserManager(settings_without_creds)
    page = fake_playwright.make_page()
    page.goto("https://www.arbeitsagentur.de/login")

    with pytest.raises(pkg_browser.CaptchaDetectedError) as exc_info:
        manager.detect_captcha_or_block(page)

    err = exc_info.value
    assert err.screenshot_path.is_file()
    assert err.screenshot_path.resolve().is_relative_to(isolated_env.screenshot_dir.resolve())
    assert err.screenshot_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert isinstance(err.status_info, dict)
    assert "headed" in json.dumps(err.status_info).lower() or err.status_info.get("captcha") is True
    assert fake_playwright.captcha_solver_calls == 0


def test_t2_f7_02_clean_page_triggers_no_captcha_error(
    fake_playwright: FakePlaywrightEnv,
    settings_without_creds: Any,
    isolated_env: E2EWorkspace,
    pkg_browser: Any,
) -> None:
    fake_playwright.scenario = "authenticated_portal"
    manager = pkg_browser.StealthBrowserManager(settings_without_creds)
    page = fake_playwright.make_page()
    page.goto("https://www.arbeitsagentur.de/mein-arbeitsmarkt")
    assert manager.detect_captcha_or_block(page) is None
    assert not list(isolated_env.screenshot_dir.glob("*.png")) if isolated_env.screenshot_dir.exists() else True


def test_t2_f7_03_corrupted_storage_state_json_handled_gracefully(
    fake_playwright: FakePlaywrightEnv,
    settings_without_creds: Any,
    isolated_env: E2EWorkspace,
    pkg_browser: Any,
) -> None:
    isolated_env.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    isolated_env.storage_state_path.write_text("{broken json", encoding="utf-8")
    manager = pkg_browser.StealthBrowserManager(settings_without_creds)
    status = manager.get_auth_status()
    assert isinstance(status, dict)
    assert not any(status.get(k) is True for k in ("has_session", "authenticated", "session_valid"))
    _open_stealth_context(manager, lambda ctx: None)


def test_t2_f7_04_human_pause_zero_or_inverted_bounds(
    totp_clock: FakeClock,
    settings_without_creds: Any,
    pkg_browser: Any,
) -> None:
    manager = pkg_browser.StealthBrowserManager(settings_without_creds)
    manager.human_pause(0.0, 0.0)
    try:
        manager.human_pause(1.0, 0.5)
    except ValueError:
        pass
    assert all(d >= 0.0 for d in totp_clock.sleep_calls)


def test_t2_f7_05_headless_false_and_executable_path_forwarded(
    fake_playwright: FakePlaywrightEnv,
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    pkg_config: Any,
    pkg_browser: Any,
) -> None:
    monkeypatch.setenv("BROWSER_EXECUTABLE_PATH", "/usr/bin/chromium")
    settings = pkg_config.get_settings(env_file=isolated_env.env_file).model_copy(
        update={"headless": False, "browser_executable_path": "/usr/bin/chromium"}
    )
    manager = pkg_browser.StealthBrowserManager(settings)
    _open_stealth_context(manager, lambda ctx: None)
    assert fake_playwright.launch_calls
    last_launch = fake_playwright.launch_calls[-1]
    assert last_launch.get("headless") is False
    assert last_launch.get("executable_path") == "/usr/bin/chromium"


# ===========================================================================
# Feature F8: Authenticated Portal Scraper Boundaries (5 tests)
# ===========================================================================


def test_t2_f8_01_login_without_credentials_fails_gracefully(
    fake_playwright: FakePlaywrightEnv,
    settings_without_creds: Any,
    pkg_portal_scraper: Any,
) -> None:
    scraper = pkg_portal_scraper.PortalScraper(settings_without_creds)
    try:
        res = scraper.login()
        assert isinstance(res, dict)
        assert res.get("authenticated") is False or res.get("ok") is False
    except Exception as exc:
        assert not isinstance(exc, (AttributeError, TypeError, KeyError))


def test_t2_f8_02_login_captcha_aborts_saves_screenshot_no_bypass(
    fake_playwright: FakePlaywrightEnv,
    settings_with_creds: Any,
    totp_clock: FakeClock,
    isolated_env: E2EWorkspace,
    pkg_browser: Any,
    pkg_portal_scraper: Any,
) -> None:
    fake_playwright.scenario = "captcha_login"
    scraper = pkg_portal_scraper.PortalScraper(settings_with_creds)
    try:
        res = scraper.login()
        assert isinstance(res, dict)
        assert res.get("captcha_detected") is True or res.get("authenticated") is False
        assert res.get("screenshot_path")
        assert Path(res["screenshot_path"]).is_file()
    except pkg_browser.CaptchaDetectedError as err:
        assert err.screenshot_path.is_file()
    assert fake_playwright.captcha_solver_calls == 0


def test_t2_f8_03_fetch_offer_details_unauthenticated_fails_gracefully(
    fake_playwright: FakePlaywrightEnv,
    settings_without_creds: Any,
    pkg_portal_scraper: Any,
) -> None:
    fake_playwright.scenario = "unauthenticated"
    scraper = pkg_portal_scraper.PortalScraper(settings_without_creds)
    try:
        res = scraper.fetch_offer_details("10000-1206415041-S")
        assert isinstance(res, dict)
        assert not res.get("contact_email")
    except Exception as exc:
        assert not isinstance(exc, AttributeError)


def test_t2_f8_04_fetch_offer_details_captcha_wall_aborts(
    fake_playwright: FakePlaywrightEnv,
    settings_with_creds: Any,
    isolated_env: E2EWorkspace,
    pkg_browser: Any,
    pkg_portal_scraper: Any,
) -> None:
    fake_playwright.scenario = "captcha_detail"
    scraper = pkg_portal_scraper.PortalScraper(settings_with_creds)
    with pytest.raises(pkg_browser.CaptchaDetectedError) as exc_info:
        scraper.fetch_offer_details("10000-1206415041-S")
    assert exc_info.value.screenshot_path.is_file()
    assert exc_info.value.screenshot_path.resolve().is_relative_to(isolated_env.screenshot_dir.resolve())


def test_t2_f8_05_fetch_offer_details_missing_optional_contacts(
    fake_playwright: FakePlaywrightEnv,
    settings_with_creds: Any,
    pkg_portal_scraper: Any,
) -> None:
    fake_playwright.scenario = "missing_optional_contacts"
    details = pkg_portal_scraper.PortalScraper(settings_with_creds).fetch_offer_details("10000-1206415041-S")
    assert "Linux" in (details.get("description") or "")
    assert details.get("contact_email") is None
    assert details.get("contact_phone") is None
    assert details.get("apply_url") is None


# ===========================================================================
# Feature F9: Company Grouping, Scoring & Filtering Boundaries (6 tests)
# ===========================================================================


def test_t2_f9_01_empty_offers_list(pkg_models: Any, pkg_sorter: Any) -> None:
    assert pkg_sorter.filter_offers([], pkg_models.SearchFilter()) == []
    assert pkg_sorter.group_by_company([]) == {}
    assert pkg_sorter.build_company_profiles([]) == []


def test_t2_f9_02_zero_km_radius_boundary(pkg_models: Any, pkg_sorter: Any) -> None:
    AusbildungOffer = pkg_models.AusbildungOffer
    offers = [
        AusbildungOffer(refnr="ZERO", title="Zero", company="C1", city="Aachen", distance_km=0.0),
        AusbildungOffer(refnr="TINY", title="Tiny", company="C2", city="Aachen", distance_km=0.001),
        AusbildungOffer(refnr="HALF", title="Half", company="C3", city="Aachen", distance_km=0.5),
        AusbildungOffer(refnr="NONE", title="None", company="C4", city="Aachen", distance_km=None),
    ]
    res = pkg_sorter.filter_offers(offers, pkg_models.SearchFilter(wo="Aachen", umkreis=0))
    refnrs = {o.refnr for o in res}
    assert "ZERO" in refnrs
    assert "TINY" not in refnrs
    assert "HALF" not in refnrs


def test_t2_f9_03_extreme_distances_and_score_clamping(pkg_models: Any, pkg_sorter: Any) -> None:
    AusbildungOffer = pkg_models.AusbildungOffer
    o1 = AusbildungOffer(
        refnr="MAX",
        title="Linux Debian Netzwerk Windows Server Active Directory",
        company="C1",
        distance_km=0.0,
        start_date="2026-08-01",
        description=" ".join(
            ["Linux Debian Netzwerk Windows Server Active Directory Virtualisierung Proxmox Docker IT-Support Scripting Firewall"]
            * 5
        ),
    )
    o2 = AusbildungOffer(refnr="FAR", title="Other", company="C2", distance_km=99999.0, description="Nothing")
    o3 = AusbildungOffer(refnr="NEG", title="Neg", company="C3", distance_km=-5.0, description="Linux")
    for o in (o1, o2, o3):
        scored = pkg_sorter.score_offer(o)
        assert 0.0 <= scored.score <= 100.0


def test_t2_f9_04_missing_dates_plz_city_and_texts(pkg_models: Any, pkg_sorter: Any) -> None:
    o = pkg_models.AusbildungOffer(
        refnr="SPARSE-1",
        title="Azubi",
        company="Sparse GmbH",
        plz=None,
        city=None,
        region=None,
        start_date=None,
        distance_km=None,
        description=None,
        requirements=None,
    )
    scored = pkg_sorter.score_offer(o)
    assert 0.0 <= scored.score <= 100.0
    assert scored.company_slug

    filtered_with_date = pkg_sorter.filter_offers(
        [scored],
        pkg_models.SearchFilter(wo="Aachen", umkreis=25, start_date_from="2026-08-01"),
    )
    assert filtered_with_date == []
    assert len(pkg_sorter.group_by_company([scored])) == 1
    assert len(pkg_sorter.build_company_profiles([scored])) == 1


def test_t2_f9_05_duplicate_offers_and_umlaut_company_slugs(pkg_models: Any, pkg_sorter: Any) -> None:
    AusbildungOffer = pkg_models.AusbildungOffer
    offers = [
        AusbildungOffer(refnr="10000-1", title="T1", company="Müller & Söhne GmbH", score=80.0),
        AusbildungOffer(refnr="10000-1", title="T1", company="Mueller & Soehne GmbH", score=60.0),
    ]
    assert offers[0].company_slug == offers[1].company_slug
    grouped = pkg_sorter.group_by_company(offers)
    assert len(grouped) == 1
    profiles = pkg_sorter.build_company_profiles(offers)
    assert len(profiles) == 1
    assert 0.0 <= profiles[0].avg_score <= profiles[0].max_score <= 100.0


def test_t2_f9_06_start_date_inclusive_boundaries(pkg_models: Any, pkg_sorter: Any) -> None:
    AusbildungOffer = pkg_models.AusbildungOffer
    offers = [
        AusbildungOffer(refnr="D-BEFORE", title="T", company="C", city="Aachen", distance_km=5.0, start_date="2026-07-31"),
        AusbildungOffer(refnr="D-START", title="T", company="C", city="Aachen", distance_km=5.0, start_date="2026-08-01"),
        AusbildungOffer(refnr="D-END", title="T", company="C", city="Aachen", distance_km=5.0, start_date="2026-09-01"),
        AusbildungOffer(refnr="D-AFTER", title="T", company="C", city="Aachen", distance_km=5.0, start_date="2026-09-02"),
    ]
    matched = pkg_sorter.filter_offers(
        offers,
        pkg_models.SearchFilter(
            wo="Aachen",
            umkreis=25,
            start_date_from="2026-08-01",
            start_date_to="2026-09-01",
        ),
    )
    assert [o.refnr for o in matched] == ["D-START", "D-END"]


# ===========================================================================
# Feature F10: LLM-Ready Exporter Boundaries (5 tests)
# ===========================================================================


def test_t2_f10_01_export_empty_offers_list(isolated_env: E2EWorkspace, pkg_exporter: Any) -> None:
    pkg_exporter.export_offers([], isolated_env.export_dir)
    assert (isolated_env.export_dir / "INDEX_FOR_LLM.md").is_file()
    summary = json.loads((isolated_env.export_dir / "companies_summary.json").read_text(encoding="utf-8"))
    companies = summary if isinstance(summary, list) else summary.get("companies", [])
    assert companies == []


def test_t2_f10_02_umlauts_and_path_traversal_chars_in_company_refnr(
    isolated_env: E2EWorkspace,
    pkg_models: Any,
    pkg_exporter: Any,
) -> None:
    offer = pkg_models.AusbildungOffer(
        refnr="../etc/10000-99/evil",
        title="Ausbildung FISI",
        company="Müller & Söhne / Co. KG (Aachen)",
    )
    pkg_exporter.export_offers([offer], isolated_env.export_dir)
    offers_root = (isolated_env.export_dir / "offers").resolve()
    md_files = list(offers_root.rglob("offer.md"))
    json_files = list(offers_root.rglob("offer.json"))
    assert len(md_files) == 1
    assert len(json_files) == 1
    assert md_files[0].resolve().is_relative_to(offers_root)
    assert json_files[0].resolve().is_relative_to(offers_root)


def test_t2_f10_03_missing_optional_contact_and_text_fields(
    isolated_env: E2EWorkspace,
    pkg_models: Any,
    pkg_exporter: Any,
) -> None:
    yaml = require_module("yaml")
    offer = pkg_models.AusbildungOffer(
        refnr="10000-NONE-1",
        title="Ausbildung FISI",
        company="Minimal GmbH",
        contact_name=None,
        contact_email=None,
        contact_phone=None,
        apply_url=None,
        distance_km=None,
        start_date=None,
        description=None,
        requirements=None,
        company_info=None,
        matched_skills=[],
    )
    pkg_exporter.export_offers([offer], isolated_env.export_dir)
    md_path = next((isolated_env.export_dir / "offers").rglob("offer.md"))
    text = md_path.read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---\n", 2)[1])
    assert fm["refnr"] == "10000-NONE-1"
    for heading in (
        "## Описание и задачи",
        "## Требования к кандидату",
        "## О компании",
        "## Контакты для Bewerbung",
        "## Подсказки для ИИ при генерации Anschreiben",
    ):
        assert heading in text


def test_t2_f10_04_overwrite_idempotency_and_status_update(
    isolated_env: E2EWorkspace,
    sample_fisi_offers: list[Any],
    pkg_models: Any,
    pkg_exporter: Any,
) -> None:
    yaml = require_module("yaml")
    two = sample_fisi_offers[:2]
    pkg_exporter.export_offers(two, isolated_env.export_dir)

    updated = [
        two[0].model_copy(update={"status": pkg_models.ApplicationStatus.APPLIED, "score": 95.0}),
        two[1],
    ]
    pkg_exporter.export_offers(updated, isolated_env.export_dir)

    md_path = isolated_env.export_dir / "offers" / f"{two[0].company_slug}__{two[0].refnr}" / "offer.md"
    fm = yaml.safe_load(md_path.read_text(encoding="utf-8").split("---\n", 2)[1])
    assert fm["status"] == "applied"
    assert fm["score"] == pytest.approx(95.0)

    json_path = isolated_env.export_dir / "offers" / f"{two[0].company_slug}__{two[0].refnr}" / "offer.json"
    jdata = json.loads(json_path.read_text(encoding="utf-8"))
    assert jdata["status"] == "applied"

    index_text = (isolated_env.export_dir / "INDEX_FOR_LLM.md").read_text(encoding="utf-8")
    assert index_text.count(two[0].refnr) == 1
    assert index_text.count(two[1].refnr) == 1


def test_t2_f10_05_malformed_yaml_special_chars_in_title_and_company(
    isolated_env: E2EWorkspace,
    pkg_models: Any,
    pkg_exporter: Any,
) -> None:
    yaml = require_module("yaml")
    special_title = 'Ausbildung: IT-Support #1 — "Pro" {Linux} & [Netzwerk]\n---\n- item'
    special_company = 'A: B "C" GmbH'
    offer = pkg_models.AusbildungOffer(
        refnr="10000-YAML-1",
        title=special_title,
        company=special_company,
    )
    pkg_exporter.export_offers([offer], isolated_env.export_dir)
    md_path = next((isolated_env.export_dir / "offers").rglob("offer.md"))
    raw = md_path.read_text(encoding="utf-8")
    parts = raw.split("---\n", 2)
    fm = yaml.safe_load(parts[1])
    assert fm["title"] == special_title
    assert fm["company"] == special_company


# ===========================================================================
# Feature F11: jobsuche CLI Boundaries (6 tests)
# ===========================================================================


def test_t2_f11_01_missing_dotenv_file_does_not_break_offline_commands(
    isolated_env: E2EWorkspace,
    local_jobsuche_server: tuple[str, FakeJobsucheRouter],
) -> None:
    base_url, _ = local_jobsuche_server
    assert not isolated_env.env_file.exists()
    assert run_cli(["companies", "--format", "json"], workspace=isolated_env).returncode == 0
    assert run_cli(["export"], workspace=isolated_env).returncode == 0
    assert run_cli(["search", "--format", "json"], workspace=isolated_env, api_base_url=base_url).returncode == 0


def test_t2_f11_02_otp_test_missing_or_invalid_secret_exits_nonzero(isolated_env: E2EWorkspace) -> None:
    p_missing = run_cli(["otp-test"], workspace=isolated_env, extra_env={"BA_TOTP_SECRET": ""})
    assert p_missing.returncode != 0
    assert "Traceback (most recent call last)" not in (p_missing.stdout + p_missing.stderr)

    p_invalid = run_cli(["otp-test"], workspace=isolated_env, extra_env={"BA_TOTP_SECRET": "NOT_BASE32!!!"})
    assert p_invalid.returncode != 0
    assert "Traceback (most recent call last)" not in (p_invalid.stdout + p_invalid.stderr)


def test_t2_f11_03_status_missing_refnr_exits_nonzero(isolated_env: E2EWorkspace) -> None:
    proc = run_cli(["status", "99999-MISSING", "applied"], workspace=isolated_env)
    assert proc.returncode != 0
    assert (proc.stdout + proc.stderr).strip()


def test_t2_f11_04_status_invalid_enum_exits_nonzero(
    isolated_env: E2EWorkspace,
    job_db: Any,
    sample_fisi_offers: list[Any],
) -> None:
    offer = sample_fisi_offers[0].model_copy(update={"refnr": "10000-1"})
    job_db.upsert_offer(offer)
    proc = run_cli(["status", "10000-1", "totally_invalid_status"], workspace=isolated_env)
    assert proc.returncode != 0
    assert job_db.get_offer("10000-1").status.value == "new"


def test_t2_f11_05_login_missing_credentials_exits_nonzero_without_hanging(isolated_env: E2EWorkspace) -> None:
    proc = run_cli(["login"], workspace=isolated_env, timeout=10.0)
    assert proc.returncode != 0
    assert (proc.stdout + proc.stderr).strip()


def test_t2_f11_06_invalid_format_or_negative_umkreis_exits_nonzero(isolated_env: E2EWorkspace) -> None:
    assert run_cli(["search", "--format", "xml"], workspace=isolated_env).returncode != 0
    assert run_cli(["search", "--umkreis", "-5"], workspace=isolated_env).returncode != 0


# ===========================================================================
# Feature F12: Antigravity CLI Plugin & MCP Server Boundaries (5 tests)
# ===========================================================================


def test_t2_f12_01_mcp_fetch_offer_details_missing_refnr(
    isolated_env: E2EWorkspace,
    local_jobsuche_server: tuple[str, FakeJobsucheRouter],
) -> None:
    base_url, _ = local_jobsuche_server

    async def _run(session: Any, init_result: Any) -> tuple[Any, Any]:
        err_res = await session.call_tool("jobsuche_fetch_offer_details", {"refnr": "NOPE-404"})
        alive_res = await session.list_tools()
        return err_res, alive_res

    err_res, alive_res = run_mcp_stdio(isolated_env, _run, api_base_url=base_url)
    assert _mcp_result_indicates_error(err_res)
    assert len(alive_res.tools) >= 6


def test_t2_f12_02_mcp_update_status_invalid_status_and_missing_refnr(
    isolated_env: E2EWorkspace,
    job_db: Any,
    sample_fisi_offers: list[Any],
) -> None:
    job_db.upsert_offer(sample_fisi_offers[0].model_copy(update={"refnr": "10000-1"}))

    async def _run(session: Any, init_result: Any) -> tuple[Any, Any]:
        r1 = await session.call_tool("jobsuche_update_offer_status", {"refnr": "NOPE", "status": "shortlisted"})
        r2 = await session.call_tool("jobsuche_update_offer_status", {"refnr": "10000-1", "status": "bogus"})
        return r1, r2

    r1, r2 = run_mcp_stdio(isolated_env, _run)
    assert _mcp_result_indicates_error(r1)
    assert _mcp_result_indicates_error(r2)


def test_t2_f12_03_mcp_search_invalid_arguments(
    isolated_env: E2EWorkspace,
    local_jobsuche_server: tuple[str, FakeJobsucheRouter],
) -> None:
    base_url, _ = local_jobsuche_server

    async def _run(session: Any, init_result: Any) -> tuple[Any, Any, Any]:
        r1 = await session.call_tool("jobsuche_search_ausbildung", {"umkreis": -10})
        r2 = await session.call_tool("jobsuche_search_ausbildung", {"mode": "unknown_mode"})
        r_alive = await session.list_tools()
        return r1, r2, r_alive

    r1, r2, r_alive = run_mcp_stdio(isolated_env, _run, api_base_url=base_url)
    assert _mcp_result_indicates_error(r1)
    assert _mcp_result_indicates_error(r2)
    assert len(r_alive.tools) >= 6


def test_t2_f12_04_mcp_auth_status_without_credentials_or_session(isolated_env: E2EWorkspace) -> None:
    async def _run(session: Any, init_result: Any) -> Any:
        return await session.call_tool("jobsuche_auth_status", {})

    res = run_mcp_stdio(isolated_env, _run)
    assert not getattr(res, "isError", False)
    payload = json.loads("\n".join(getattr(c, "text", "") for c in res.content))
    serialized = json.dumps(payload).lower()
    assert "false" in serialized


def test_t2_f12_05_mcp_unknown_tool_and_invalid_jsonrpc_params(isolated_env: E2EWorkspace) -> None:
    async def _run(session: Any, init_result: Any) -> tuple[bool, Any]:
        errored = False
        try:
            res = await session.call_tool("jobsuche_does_not_exist", {})
            errored = _mcp_result_indicates_error(res)
        except Exception:
            errored = True
        alive = await session.list_tools()
        return errored, alive

    errored, alive = run_mcp_stdio(isolated_env, _run)
    assert errored is True
    assert len(alive.tools) >= 6


# ===========================================================================
# Feature F13: Hermes Agent Standalone v2 Plugin Boundaries (5 tests)
# ===========================================================================


def test_t2_f13_01_search_and_monitor_invalid_args_return_error_json(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    hermes_recording_ctx: FakeHermesContext,
) -> None:
    plugin = load_hermes_jobsuche_module(monkeypatch, hermes_home=isolated_env.root / "hermes_home")
    plugin.register(hermes_recording_ctx)
    h_search = hermes_recording_ctx.tools["jobsuche_search"]["handler"]
    h_monitor = hermes_recording_ctx.tools["jobsuche_monitor"]["handler"]

    for payload in (
        h_monitor({"format": "xml"}),
        h_search({"umkreis": -1}),
        h_search({"limit": "not-an-int"}),
        h_monitor({"db_path": "   "}),
    ):
        data = json.loads(payload)
        assert data["ok"] is False
        assert data.get("error") or data.get("error_type")


def test_t2_f13_02_export_and_status_empty_or_missing_db(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    hermes_recording_ctx: FakeHermesContext,
) -> None:
    plugin = load_hermes_jobsuche_module(monkeypatch, hermes_home=isolated_env.root / "hermes_home")
    plugin.register(hermes_recording_ctx)
    h_export = hermes_recording_ctx.tools["jobsuche_export_llm"]["handler"]
    h_status = hermes_recording_ctx.tools["jobsuche_status"]["handler"]

    missing_db = str(isolated_env.root / "nonexistent" / "empty.db")
    exp_res = json.loads(h_export({"db_path": missing_db, "export_dir": str(isolated_env.export_dir)}))
    assert isinstance(exp_res, dict) and "ok" in exp_res

    st_res = json.loads(h_status({"db_path": missing_db}))
    assert isinstance(st_res, dict) and "ok" in st_res


def test_t2_f13_03_monitor_telegram_zero_new_offers_and_dry_run(
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
    h_monitor = hermes_recording_ctx.tools["jobsuche_monitor"]["handler"]

    dry = json.loads(h_monitor({"format": "telegram", "dry_run": True, "db_path": str(isolated_env.db_path)}))
    assert dry["ok"] is True
    assert dry["new_count"] >= 2

    first = json.loads(h_monitor({"format": "telegram", "dry_run": False, "db_path": str(isolated_env.db_path)}))
    assert first["ok"] is True
    assert first["new_count"] >= 2
    assert first.get("message")

    second = json.loads(h_monitor({"format": "telegram", "dry_run": False, "db_path": str(isolated_env.db_path)}))
    assert second["ok"] is True
    assert second["new_count"] == 0
    assert second.get("message", "") == ""


def test_t2_f13_04_api_unreachable_returns_structured_error_json(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    hermes_recording_ctx: FakeHermesContext,
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]

    closed_url = f"http://127.0.0.1:{closed_port}"
    monkeypatch.setenv("JOBSUCHE_API_BASE_URL", closed_url)
    monkeypatch.setenv("API_BASE_URL", closed_url)
    plugin = load_hermes_jobsuche_module(monkeypatch, hermes_home=isolated_env.root / "hermes_home")
    plugin.register(hermes_recording_ctx)

    s_res = json.loads(hermes_recording_ctx.tools["jobsuche_search"]["handler"]({"wo": "Aachen"}))
    assert s_res["ok"] is False
    assert s_res.get("error")

    m_res = json.loads(hermes_recording_ctx.tools["jobsuche_monitor"]["handler"]({"wo": "Aachen"}))
    assert m_res["ok"] is False
    assert m_res.get("error")


def test_t2_f13_05_hermes_config_preserved_and_isolated_probe(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    hermes_recording_ctx: FakeHermesContext,
) -> None:
    yaml = require_module("yaml")
    cfg = yaml.safe_load(HERMES_CONFIG_PATH.read_text(encoding="utf-8"))
    enabled = cfg.get("plugins", {}).get("enabled", [])
    assert "antigravity-oauth" in enabled
    assert "kleinanzeigen" in enabled
    assert "jobsuche" in enabled

    fake_hermes_home = isolated_env.root / "isolated_hermes_home"
    plugin = load_hermes_jobsuche_module(monkeypatch, hermes_home=fake_hermes_home)
    plugin.register(hermes_recording_ctx)
    assert len(hermes_recording_ctx.tools) == 4
