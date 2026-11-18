from __future__ import annotations

import copy
import json
import re
import tomllib
from typing import Any

import pytest

from tests.e2e.conftest import (
    E2EWorkspace,
    FakeClock,
    FakeHermesContext,
    FakeIMAPMailbox,
    FakeJobsucheRouter,
    FakePlaywrightEnv,
    PROJECT_ROOT,
    VALID_TEST_TOTP_SECRET,
    load_hermes_jobsuche_module,
    make_v6_jobs_pages,
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


def test_t3_01_pair_f1_f2_pyproject_entrypoint_and_env_config(
    isolated_env: E2EWorkspace,
    pkg_config: Any,
) -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["scripts"]["jobsuche"] == "jobsuche_pkg.cli:main"
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "JOBSUCHE_DEFAULT_CITY" in env_example
    assert "JOBSUCHE_DEFAULT_RADIUS" in env_example

    defaults = pkg_config.get_settings(env_file=isolated_env.env_file)
    assert defaults.default_wo == "Aachen"
    assert defaults.default_umkreis == 25

    isolated_env.env_file.write_text(
        "JOBSUCHE_DEFAULT_CITY=Köln\nJOBSUCHE_DEFAULT_RADIUS=40\n",
        encoding="utf-8",
    )
    overridden = pkg_config.get_settings(env_file=isolated_env.env_file)
    assert overridden.default_wo == "Köln"
    assert overridden.default_umkreis == 40
    assert overridden.db_path == isolated_env.db_path
    assert overridden.export_dir == isolated_env.export_dir


def test_t3_02_pair_f1_f11_installed_cli_script_invocation(isolated_env: E2EWorkspace) -> None:
    help_proc = run_cli(["--help"], workspace=isolated_env)
    assert help_proc.returncode == 0
    assert "otp-test" in help_proc.stdout

    otp_ok = run_cli(
        ["otp-test"],
        workspace=isolated_env,
        extra_env={"BA_TOTP_SECRET": VALID_TEST_TOTP_SECRET},
    )
    assert otp_ok.returncode == 0
    assert re.search(r"\b\d{6}\b", otp_ok.stdout)

    otp_fail = run_cli(["otp-test"], workspace=isolated_env)
    assert otp_fail.returncode != 0


def test_t3_03_pair_f2_f4_settings_drive_api_client_search(
    local_jobsuche_server: tuple[str, FakeJobsucheRouter],
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    pkg_config: Any,
    pkg_api_client: Any,
) -> None:
    base_url, router = local_jobsuche_server
    monkeypatch.setenv("JOBSUCHE_API_BASE_URL", base_url)
    settings = pkg_config.get_settings(env_file=isolated_env.env_file).model_copy(update={"api_base_url": base_url})
    client = pkg_api_client.JobsucheAPIClient(settings)
    offers = client.search_jobs()
    assert len(offers) >= 2
    req = router.requests[0]
    assert "/pc/v6/jobs" in req.url
    assert req.headers.get("x-api-key") == "jobboerse-jobsuche"
    assert req.query.get("angebotsart") == ["4"]
    assert req.query.get("wo") == ["Aachen"]
    assert req.query.get("umkreis") == ["25"]


def test_t3_04_pair_f3_f4_models_normalize_v6_and_v4_schemas(
    mock_jobsuche_http: FakeJobsucheRouter,
    sample_v4_jobs_payload: dict[str, Any],
    settings_without_creds: Any,
    pkg_api_client: Any,
    pkg_models: Any,
) -> None:
    client = pkg_api_client.JobsucheAPIClient(settings_without_creds)
    v6_offers = client.search_jobs(pkg_models.SearchFilter(size=2, limit=2))
    assert len(v6_offers) == 2
    assert isinstance(v6_offers[0], pkg_models.AusbildungOffer)
    assert "mueller" in v6_offers[0].company_slug
    assert v6_offers[0].portal_url.endswith(v6_offers[0].refnr)

    mock_jobsuche_http.custom_search_payload = sample_v4_jobs_payload
    v4_offers = client.search_jobs(pkg_models.SearchFilter())
    assert len(v4_offers) == 1
    assert v4_offers[0].refnr == "10000-9900011122-S"
    assert v4_offers[0].company == "Rheinland Cloud KG"

    details = client.get_job_details(v6_offers[0].refnr)
    assert "Linux" in details.get("stellenangebotsBeschreibung", "")


def test_t3_05_pair_f3_f5_models_roundtrip_sqlite_and_companies(
    job_db: Any,
    sample_fisi_offers: list[Any],
    pkg_models: Any,
) -> None:
    enriched = [
        sample_fisi_offers[0].model_copy(
            update={
                "matched_skills": ["Linux", "Debian", "Proxmox"],
                "score": 91.0,
                "status": pkg_models.ApplicationStatus.SHORTLISTED,
            }
        ),
        sample_fisi_offers[1].model_copy(update={"score": 70.0}),
    ]
    assert job_db.upsert_offers(enriched) == {"inserted": 2, "updated": 0, "unchanged": 0}
    assert job_db.upsert_offers(enriched) == {"inserted": 0, "updated": 0, "unchanged": 2}

    loaded = job_db.get_offer(enriched[0].refnr)
    assert loaded is not None
    assert loaded.matched_skills == ["Linux", "Debian", "Proxmox"]
    assert loaded.status == pkg_models.ApplicationStatus.SHORTLISTED

    companies = job_db.list_companies()
    assert len(companies) == 1
    assert isinstance(companies[0], pkg_models.CompanyProfile)
    assert companies[0].offer_count == 2


def test_t3_06_pair_f4_f5_incremental_api_sync_records_offer_history(
    mock_jobsuche_http: FakeJobsucheRouter,
    settings_without_creds: Any,
    job_db: Any,
    pkg_api_client: Any,
    pkg_models: Any,
) -> None:
    client = pkg_api_client.JobsucheAPIClient(settings_without_creds)
    pass1 = client.search_jobs(pkg_models.SearchFilter(size=2, limit=2))
    res1 = job_db.upsert_offers(pass1)
    run1 = job_db.record_search_run({"pass": 1}, total_found=len(pass1), new_count=res1["inserted"])
    assert res1["inserted"] == 2
    assert run1 >= 1

    mutated_pages = copy.deepcopy(make_v6_jobs_pages())
    mutated_pages[1]["ergebnisliste"][0]["stellenangebotsTitel"] = "Ausbildung FISI — Updated 2026"
    mutated_pages[1]["ergebnisliste"][0]["aenderungsdatum"] = "2026-02-20T12:00:00"
    mutated_pages[1]["ergebnisliste"].append(mutated_pages[2]["ergebnisliste"][0])
    mock_jobsuche_http.v6_pages = mutated_pages

    pass2 = client.search_jobs(pkg_models.SearchFilter(size=3, limit=3))
    res2 = job_db.upsert_offers(pass2)
    assert res2 == {"inserted": 1, "updated": 1, "unchanged": 1}

    hist = job_db.get_history("10000-1206415041-S")
    assert len(hist) >= 1
    assert "Ausbildung FISI — Updated 2026" in json.dumps(hist, default=str, ensure_ascii=False)


def test_t3_07_pair_f5_f9_db_offers_to_sorter_scoring_and_grouping(
    job_db: Any,
    sample_fisi_offers: list[Any],
    pkg_models: Any,
    pkg_sorter: Any,
) -> None:
    far_offer = pkg_models.AusbildungOffer(
        refnr="10000-FAR-99",
        title="Bürokaufmann",
        company="FarAway AG",
        city="Düsseldorf",
        distance_km=60.0,
        description="Allgemeine Verwaltung",
    )
    job_db.upsert_offers([*sample_fisi_offers, far_offer])
    stored = job_db.list_offers()
    scored = [pkg_sorter.score_offer(o) for o in stored]
    filtered = pkg_sorter.filter_offers(scored, pkg_models.SearchFilter(wo="Aachen", umkreis=25))
    assert all(o.refnr != "10000-FAR-99" for o in filtered)

    grouped = pkg_sorter.group_by_company(filtered)
    assert len(grouped) == 2
    profiles = pkg_sorter.build_company_profiles(filtered)
    assert any(p.offer_count == 2 for p in profiles)


def test_t3_08_pair_f9_f10_sorter_pipeline_to_llm_exporter(
    isolated_env: E2EWorkspace,
    sample_fisi_offers: list[Any],
    pkg_models: Any,
    pkg_sorter: Any,
    pkg_exporter: Any,
) -> None:
    yaml = require_module("yaml")
    scored = [pkg_sorter.score_offer(o) for o in sample_fisi_offers]
    filtered = pkg_sorter.filter_offers(scored, pkg_models.SearchFilter(wo="Aachen", umkreis=25))
    profiles = pkg_sorter.build_company_profiles(filtered)
    pkg_exporter.export_offers(filtered, isolated_env.export_dir)

    index_md = (isolated_env.export_dir / "INDEX_FOR_LLM.md").read_text(encoding="utf-8")
    summary = json.loads((isolated_env.export_dir / "companies_summary.json").read_text(encoding="utf-8"))
    summary_list = summary if isinstance(summary, list) else summary.get("companies", [])
    assert len(summary_list) == len(profiles)

    for o in filtered:
        md_file = isolated_env.export_dir / "offers" / f"{o.company_slug}__{o.refnr}" / "offer.md"
        json_file = md_file.with_name("offer.json")
        assert md_file.is_file() and json_file.is_file()
        assert o.refnr in index_md
        raw = md_file.read_text(encoding="utf-8")
        fm = yaml.safe_load(raw.split("---\n", 2)[1])
        assert len(fm) >= 16
        for heading in (
            "## Описание и задачи",
            "## Требования к кандидату",
            "## О компании",
            "## Контакты для Bewerbung",
            "## Подсказки для ИИ при генерации Anschreiben",
        ):
            assert heading in raw


def test_t3_09_pair_f6_f7_totp_rollover_feeds_stealth_browser(
    totp_clock: FakeClock,
    fake_playwright: FakePlaywrightEnv,
    settings_with_creds: Any,
    isolated_env: E2EWorkspace,
    pkg_auth_otp: Any,
    pkg_browser: Any,
) -> None:
    totp_clock.set(FakeClock.ROLLOVER_TS)
    code, rem = pkg_auth_otp.generate_totp(
        settings_with_creds.ba_totp_secret,
        min_remaining_seconds=3.0,
        now=totp_clock.now(),
        sleep_fn=totp_clock.sleep,
    )
    assert re.fullmatch(r"\d{6}", code)
    assert rem >= 25.0
    assert len(totp_clock.sleep_calls) >= 1

    manager = pkg_browser.StealthBrowserManager(settings_with_creds)
    _open_stealth_context(manager, lambda ctx: ctx.storage_state(path=str(isolated_env.storage_state_path)))
    ctx_kwargs = fake_playwright.context_calls[-1]
    assert ctx_kwargs["locale"] == "de-DE"
    assert ctx_kwargs["timezone_id"] == "Europe/Berlin"
    headers = {k.lower(): v for k, v in ctx_kwargs["extra_http_headers"].items()}
    assert "de-DE" in headers["accept-language"]
    assert isolated_env.storage_state_path.is_file()


def test_t3_10_pair_f6_f8_portal_login_primary_totp_and_imap_fallback(
    fake_playwright: FakePlaywrightEnv,
    mock_imap: FakeIMAPMailbox,
    totp_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    settings_with_creds: Any,
    pkg_config: Any,
    pkg_portal_scraper: Any,
) -> None:
    fake_playwright.scenario = "authenticated_portal"
    res1 = pkg_portal_scraper.PortalScraper(settings_with_creds).login()
    assert res1.get("authenticated") is True or res1.get("ok") is True
    assert isolated_env.storage_state_path.is_file()

    # Fallback mode: no BA_TOTP_SECRET, IMAP email provides 6-digit OTP
    mock_imap.add_otp_email("591827")
    monkeypatch.delenv("BA_TOTP_SECRET", raising=False)
    imap_settings = pkg_config.get_settings(env_file=isolated_env.env_file)
    fake_playwright.filled_values.clear()
    res2 = pkg_portal_scraper.PortalScraper(imap_settings).login()
    assert res2.get("authenticated") is True or res2.get("ok") is True
    assert any(val == "591827" for _, val in fake_playwright.filled_values)


def test_t3_11_pair_f7_f8_captcha_guard_aborts_scraper_without_bypass(
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
    err = exc_info.value
    assert err.screenshot_path.is_file()
    assert err.screenshot_path.resolve().is_relative_to(isolated_env.screenshot_dir.resolve())
    assert isinstance(err.status_info, dict)
    assert fake_playwright.captcha_solver_calls == 0


def test_t3_12_pair_f8_f4_hybrid_enrich_merges_api_and_portal_vv(
    mock_jobsuche_http: FakeJobsucheRouter,
    fake_playwright: FakePlaywrightEnv,
    settings_with_creds: Any,
    pkg_api_client: Any,
    pkg_portal_scraper: Any,
    pkg_models: Any,
) -> None:
    fake_playwright.scenario = "authenticated_portal"
    client = pkg_api_client.JobsucheAPIClient(settings_with_creds)
    scraper = pkg_portal_scraper.PortalScraper(settings_with_creds)

    base_offers = client.search_jobs(pkg_models.SearchFilter(size=2, limit=1))
    assert len(base_offers) == 1
    enriched = client.enrich_offer(base_offers[0], scraper=scraper, fetch_api_details=True)
    assert "Linux" in (enriched.description or "")
    assert enriched.contact_name == "Frau Anna Weber"
    assert enriched.contact_email == "karriere@mueller-it.example"
    assert enriched.contact_phone == "+49 241 123456"
    assert enriched.apply_url

    vv = scraper.fetch_vermittlungsvorschlaege()
    assert len(vv) == 2
    assert all(item.get("is_vermittlungsvorschlag") is True for item in vv)


def test_t3_13_pair_f11_f5_f9_f10_cli_sync_status_export_pipeline(
    isolated_env: E2EWorkspace,
    local_jobsuche_server: tuple[str, FakeJobsucheRouter],
    pkg_db: Any,
) -> None:
    yaml = require_module("yaml")
    base_url, _ = local_jobsuche_server

    assert run_cli(["sync", "--wo", "Aachen", "--umkreis", "25"], workspace=isolated_env, api_base_url=base_url).returncode == 0
    refnr = "10000-1206415041-S"
    assert (
        run_cli(
            ["status", refnr, "shortlisted", "--notes", "Strong fit"],
            workspace=isolated_env,
            api_base_url=base_url,
        ).returncode
        == 0
    )
    comp_proc = run_cli(["companies", "--format", "json"], workspace=isolated_env, api_base_url=base_url)
    assert comp_proc.returncode == 0
    assert json.loads(comp_proc.stdout)
    assert run_cli(["export"], workspace=isolated_env, api_base_url=base_url).returncode == 0

    with pkg_db.JobDatabase(isolated_env.db_path) as db:
        hist = db.get_history(refnr)
        assert "shortlisted" in json.dumps(hist, default=str)

    md_files = list((isolated_env.export_dir / "offers").glob(f"*__{refnr}/offer.md"))
    assert len(md_files) == 1
    fm = yaml.safe_load(md_files[0].read_text(encoding="utf-8").split("---\n", 2)[1])
    assert fm["status"] == "shortlisted"


def test_t3_14_pair_f12_f5_f9_f10_antigravity_mcp_stdio_db_and_export(
    isolated_env: E2EWorkspace,
    local_jobsuche_server: tuple[str, FakeJobsucheRouter],
    pkg_db: Any,
) -> None:
    base_url, _ = local_jobsuche_server

    async def _run(session: Any, init_result: Any) -> dict[str, Any]:
        tools = {t.name for t in (await session.list_tools()).tools}
        r_sync = await session.call_tool("jobsuche_sync_and_export", {"wo": "Aachen", "umkreis": 25})
        r_upd = await session.call_tool(
            "jobsuche_update_offer_status",
            {"refnr": "10000-1206415041-S", "status": "shortlisted", "notes": "MCP fit"},
        )
        r_list = await session.call_tool("jobsuche_list_offers", {"status": "shortlisted"})
        r_det = await session.call_tool("jobsuche_fetch_offer_details", {"refnr": "10000-1206415041-S"})
        r_auth = await session.call_tool("jobsuche_auth_status", {})
        return {
            "tools": tools,
            "sync": json.loads(r_sync.content[0].text),
            "upd": json.loads(r_upd.content[0].text),
            "list": json.loads(r_list.content[0].text),
            "det": json.loads(r_det.content[0].text),
            "auth": json.loads(r_auth.content[0].text),
        }

    out = run_mcp_stdio(isolated_env, _run, api_base_url=base_url)
    assert len(out["tools"]) >= 6
    assert (isolated_env.export_dir / "INDEX_FOR_LLM.md").is_file()
    with pkg_db.JobDatabase(isolated_env.db_path) as db:
        offer = db.get_offer("10000-1206415041-S")
        assert offer is not None and offer.status.value == "shortlisted"
    assert "10000-1206415041-S" in json.dumps(out["list"])


def test_t3_15_pair_f13_f5_f9_f10_hermes_plugin_tools_and_telegram_digest(
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
    assert "jobsuche" in hermes_recording_ctx.skills

    m1 = json.loads(
        hermes_recording_ctx.tools["jobsuche_monitor"]["handler"](
            {"format": "telegram", "db_path": str(isolated_env.db_path)}
        )
    )
    m2 = json.loads(
        hermes_recording_ctx.tools["jobsuche_monitor"]["handler"](
            {"format": "telegram", "db_path": str(isolated_env.db_path)}
        )
    )
    exp = json.loads(
        hermes_recording_ctx.tools["jobsuche_export_llm"]["handler"](
            {"db_path": str(isolated_env.db_path), "export_dir": str(isolated_env.export_dir)}
        )
    )
    st = json.loads(
        hermes_recording_ctx.tools["jobsuche_status"]["handler"]({"db_path": str(isolated_env.db_path)})
    )
    assert m1["ok"] is True and m1["new_count"] >= 2 and m1["message"]
    assert m2["ok"] is True and m2["new_count"] == 0
    assert exp["ok"] is True and (isolated_env.export_dir / "INDEX_FOR_LLM.md").is_file()
    assert st["ok"] is True


def _status_payload_confirms_applied(payload: Any, refnr: str) -> bool:
    if isinstance(payload, dict):
        applied_val = payload.get("applied")
        if isinstance(applied_val, int) and not isinstance(applied_val, bool) and applied_val >= 1:
            return True
        if payload.get("refnr") == refnr and payload.get("status") == "applied":
            return True
        return any(_status_payload_confirms_applied(v, refnr) for v in payload.values())
    if isinstance(payload, list):
        return any(_status_payload_confirms_applied(item, refnr) for item in payload)
    return False


def test_t3_16_pair_f12_f13_f2_f3_f5_cross_agent_shared_db_consistency(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    local_jobsuche_server: tuple[str, FakeJobsucheRouter],
    hermes_recording_ctx: FakeHermesContext,
    pkg_db: Any,
) -> None:
    base_url, _ = local_jobsuche_server
    monkeypatch.setenv("JOBSUCHE_API_BASE_URL", base_url)
    monkeypatch.setenv("API_BASE_URL", base_url)

    plugin = load_hermes_jobsuche_module(monkeypatch, hermes_home=isolated_env.root / "hermes_home")
    plugin.register(hermes_recording_ctx)

    mon = json.loads(
        hermes_recording_ctx.tools["jobsuche_monitor"]["handler"](
            {"db_path": str(isolated_env.db_path)}
        )
    )
    assert mon["ok"] is True and mon["new_count"] >= 2

    async def _mcp_update(session: Any, init_result: Any) -> Any:
        return await session.call_tool(
            "jobsuche_update_offer_status",
            {"refnr": "10000-1206415041-S", "status": "applied", "notes": "Sent"},
        )

    upd_res = run_mcp_stdio(isolated_env, _mcp_update, api_base_url=base_url)
    assert not getattr(upd_res, "isError", False)

    with pkg_db.JobDatabase(isolated_env.db_path) as db:
        offer = db.get_offer("10000-1206415041-S")
        assert offer is not None and offer.status.value == "applied"

    st = json.loads(
        hermes_recording_ctx.tools["jobsuche_status"]["handler"]({"db_path": str(isolated_env.db_path)})
    )
    assert st["ok"] is True
    assert _status_payload_confirms_applied(st, "10000-1206415041-S"), (
        f"Expected jobsuche_status to report >= 1 applied offer or list applied offer, got: {st}"
    )
