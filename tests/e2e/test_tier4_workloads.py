from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from tests.e2e.conftest import (
    E2EWorkspace,
    FakeClock,
    FakeHermesContext,
    FakeJobsucheRouter,
    FakePlaywrightEnv,
    PROJECT_ROOT,
    encode_refnr_b64,
    load_hermes_jobsuche_module,
    require_module,
    run_cli,
    run_mcp_stdio,
)


def _make_v6_item(
    refnr: str,
    title: str,
    company: str,
    *,
    plz: str = "52062",
    city: str = "Aachen",
    distance: int = 3,
    start_date: str = "2026-08-01",
) -> dict[str, Any]:
    return {
        "referenznummer": refnr,
        "stellenangebotsTitel": title,
        "hauptberuf": "Fachinformatiker/in - Systemintegration",
        "stellenangebotsart": "AUSBILDUNG",
        "ausbildungsart": "AUSBILDUNG",
        "firma": company,
        "stellenlokationen": [
            {
                "adresse": {
                    "plz": plz,
                    "ort": city,
                    "region": "Nordrhein-Westfalen",
                    "land": "Deutschland",
                }
            }
        ],
        "entfernung": distance,
        "eintrittszeitraum": {"von": start_date},
        "datumErsteVeroeffentlichung": "2026-02-01",
        "aenderungsdatum": "2026-02-10T09:00:00",
    }


def test_workload_01_aachen_fisi_discovery_dedup_grouping_export(
    mock_jobsuche_http: FakeJobsucheRouter,
    isolated_env: E2EWorkspace,
    settings_without_creds: Any,
    job_db: Any,
    pkg_api_client: Any,
    pkg_models: Any,
    pkg_sorter: Any,
    pkg_exporter: Any,
) -> None:
    dup_refnr = "10000-W1-DUP-1"
    mock_jobsuche_http.v6_pages = {
        1: {
            "maxErgebnisse": 5,
            "page": 1,
            "size": 3,
            "ergebnisliste": [
                _make_v6_item(
                    dup_refnr,
                    "Ausbildung FISI — Linux & Proxmox",
                    "Müller IT-Systeme GmbH",
                    distance=2,
                ),
                _make_v6_item(
                    "10000-W1-2",
                    "Azubi Systemintegration Netzwerk",
                    "Müller IT-Systeme GmbH",
                    city="Würselen",
                    plz="52146",
                    distance=8,
                ),
                _make_v6_item(
                    "10000-W1-3",
                    "Ausbildung Cloud & Docker",
                    "AixNet Systems GmbH",
                    distance=5,
                ),
            ],
        },
        2: {
            "maxErgebnisse": 5,
            "page": 2,
            "size": 3,
            "ergebnisliste": [
                _make_v6_item(
                    dup_refnr,
                    "Ausbildung FISI — Linux & Proxmox",
                    "Müller IT-Systeme GmbH",
                    distance=2,
                ),
                _make_v6_item(
                    "10000-W1-4",
                    "Ausbildung Bürokaufmann",
                    "Allgemein Handel GmbH",
                    distance=20,
                ),
            ],
        },
    }
    mock_jobsuche_http.jobdetails = {
        encode_refnr_b64(dup_refnr): {
            "referenznummer": dup_refnr,
            "stellenangebotsBeschreibung": "Linux, Debian, Proxmox, Docker, Netzwerk, Firewall, Active Directory.",
            "geforderterBildungsabschluss": "Mittlere Reife",
            "allianzpartnerUrl": "https://mueller-it.example/apply/1",
        },
        encode_refnr_b64("10000-W1-2"): {
            "referenznummer": "10000-W1-2",
            "stellenangebotsBeschreibung": "Netzwerk, Windows Server, IT-Support.",
        },
        encode_refnr_b64("10000-W1-3"): {
            "referenznummer": "10000-W1-3",
            "stellenangebotsBeschreibung": "Docker, Scripting.",
        },
        encode_refnr_b64("10000-W1-4"): {
            "referenznummer": "10000-W1-4",
            "stellenangebotsBeschreibung": "Büroorganisation und Buchhaltung.",
        },
    }

    client = pkg_api_client.JobsucheAPIClient(settings_without_creds)
    fetched = client.search_jobs(pkg_models.SearchFilter(wo="Aachen", umkreis=25, size=3))
    enriched = [client.enrich_offer(o, fetch_api_details=True) for o in fetched]
    scored = [pkg_sorter.score_offer(o) for o in enriched]

    job_db.upsert_offers(scored)
    persisted = job_db.list_offers()
    assert len(persisted) == 4

    filtered = pkg_sorter.filter_offers(persisted, pkg_models.SearchFilter(wo="Aachen", umkreis=25))
    grouped = pkg_sorter.group_by_company(filtered)
    assert "mueller-it-systeme-gmbh" in grouped
    assert len(grouped["mueller-it-systeme-gmbh"]) == 2

    top_offer = max(filtered, key=lambda o: o.score)
    assert top_offer.refnr == dup_refnr

    pkg_exporter.export_offers(filtered, isolated_env.export_dir)
    index_md = (isolated_env.export_dir / "INDEX_FOR_LLM.md").read_text(encoding="utf-8")
    assert dup_refnr in index_md
    assert (isolated_env.export_dir / "companies_summary.json").is_file()
    for o in filtered:
        folder = isolated_env.export_dir / "offers" / f"{o.company_slug}__{o.refnr}"
        assert (folder / "offer.md").is_file()
        assert (folder / "offer.json").is_file()


def test_workload_02_incremental_sync_tracks_mutations_preserves_user_status(
    mock_jobsuche_http: FakeJobsucheRouter,
    isolated_env: E2EWorkspace,
    settings_without_creds: Any,
    job_db: Any,
    pkg_api_client: Any,
    pkg_models: Any,
) -> None:
    ref_a = "10000-W2-A"
    ref_b = "10000-W2-B"
    ref_c = "10000-W2-C"
    ref_d = "10000-W2-D"

    mock_jobsuche_http.v6_pages = {
        1: {
            "maxErgebnisse": 4,
            "page": 1,
            "size": 10,
            "ergebnisliste": [
                _make_v6_item(ref_a, "FISI Day 1", "Company A", start_date="2026-08-01"),
                _make_v6_item(ref_b, "FISI Day 1 B", "Company B", start_date="2026-08-01"),
                _make_v6_item(ref_d, "FISI Day 1 D", "Company D", start_date="2026-08-01"),
                _make_v6_item("10000-W2-E", "FISI Day 1 E", "Company E", start_date="2026-08-01"),
            ],
        }
    }

    client = pkg_api_client.JobsucheAPIClient(settings_without_creds)
    day1 = client.search_jobs(pkg_models.SearchFilter(size=10))
    res1 = job_db.upsert_offers(day1)
    assert res1["inserted"] == 4
    job_db.record_search_run({"day": 1}, total_found=len(day1), new_count=res1["inserted"])

    job_db.update_status(ref_a, pkg_models.ApplicationStatus.SHORTLISTED, notes="Keep this shortlist note")
    job_db.update_status(ref_b, pkg_models.ApplicationStatus.IGNORED, notes="Not relevant")

    mock_jobsuche_http.v6_pages = {
        1: {
            "maxErgebnisse": 3,
            "page": 1,
            "size": 10,
            "ergebnisliste": [
                _make_v6_item(ref_a, "FISI Day 2 Mutated", "Company A", start_date="2026-09-01"),
                _make_v6_item(ref_b, "FISI Day 1 B", "Company B", start_date="2026-08-01"),
                _make_v6_item(ref_c, "FISI Day 2 New", "Company C", start_date="2026-08-15"),
            ],
        }
    }

    day2 = client.search_jobs(pkg_models.SearchFilter(size=10))
    res2 = job_db.upsert_offers(day2)
    assert res2 == {"inserted": 1, "updated": 1, "unchanged": 1}
    job_db.record_search_run({"day": 2}, total_found=len(day2), new_count=res2["inserted"])

    stored_a = job_db.get_offer(ref_a)
    stored_b = job_db.get_offer(ref_b)
    assert stored_a.status == pkg_models.ApplicationStatus.SHORTLISTED
    assert stored_a.notes == "Keep this shortlist note"
    assert stored_a.title == "FISI Day 2 Mutated"
    assert stored_a.start_date == "2026-09-01"
    assert stored_b.status == pkg_models.ApplicationStatus.IGNORED

    hist_a = json.dumps(job_db.get_history(ref_a), default=str)
    assert "shortlisted" in hist_a
    assert "FISI Day 2 Mutated" in hist_a
    assert "2026-09-01" in hist_a

    conn = sqlite3.connect(isolated_env.db_path)
    try:
        run_count = conn.execute("SELECT COUNT(*) FROM search_runs").fetchone()[0]
    finally:
        conn.close()
    assert run_count >= 2


def test_workload_03_hybrid_api_authenticated_portal_and_vermittlungsvorschlaege(
    mock_jobsuche_http: FakeJobsucheRouter,
    fake_playwright: FakePlaywrightEnv,
    totp_clock: FakeClock,
    isolated_env: E2EWorkspace,
    settings_with_creds: Any,
    job_db: Any,
    pkg_auth_otp: Any,
    pkg_api_client: Any,
    pkg_portal_scraper: Any,
    pkg_browser: Any,
    pkg_models: Any,
    pkg_exporter: Any,
) -> None:
    totp_clock.set(FakeClock.ROLLOVER_TS)
    code, rem = pkg_auth_otp.generate_totp(
        settings_with_creds.ba_totp_secret,
        min_remaining_seconds=3.0,
        now=totp_clock.now(),
        sleep_fn=totp_clock.sleep,
    )
    assert len(code) == 6 and rem >= 25.0

    fake_playwright.scenario = "authenticated_portal"
    scraper = pkg_portal_scraper.PortalScraper(settings_with_creds)
    login_res = scraper.login()
    assert login_res.get("authenticated") is True or login_res.get("ok") is True
    assert isolated_env.storage_state_path.is_file()

    client = pkg_api_client.JobsucheAPIClient(settings_with_creds)
    base_offers = client.search_jobs(pkg_models.SearchFilter(size=2, limit=1))
    vv_list = scraper.fetch_vermittlungsvorschlaege()
    vv_refnrs = {item["refnr"] for item in vv_list}

    enriched_offers = []
    for offer in base_offers:
        enriched = client.enrich_offer(offer, scraper=scraper, fetch_api_details=True)
        if enriched.refnr in vv_refnrs:
            enriched = enriched.model_copy(update={"is_vermittlungsvorschlag": True})
        enriched_offers.append(enriched)

    job_db.upsert_offers(enriched_offers)
    persisted = job_db.get_offer("10000-1206415041-S")
    assert persisted is not None
    assert persisted.contact_name == "Frau Anna Weber"
    assert persisted.contact_email == "karriere@mueller-it.example"
    assert persisted.contact_phone == "+49 241 123456"
    assert persisted.is_vermittlungsvorschlag is True

    pkg_exporter.export_offers(enriched_offers, isolated_env.export_dir)
    offer_json = json.loads(
        (
            isolated_env.export_dir
            / "offers"
            / f"{persisted.company_slug}__{persisted.refnr}"
            / "offer.json"
        ).read_text(encoding="utf-8")
    )
    assert offer_json["is_vermittlungsvorschlag"] is True

    # Reuse storage_state on subsequent browser manager invocation
    mgr2 = pkg_browser.StealthBrowserManager(settings_with_creds)
    for m_name in ("open_context", "new_context", "context"):
        fn = getattr(mgr2, m_name, None)
        if callable(fn):
            ctx_or_cm = fn()
            if hasattr(ctx_or_cm, "__enter__"):
                with ctx_or_cm:
                    pass
            break
    assert any(
        c.get("storage_state") is not None
        and Path(str(c.get("storage_state"))) == isolated_env.storage_state_path
        for c in fake_playwright.context_calls
    )


def test_workload_04_captcha_mid_workflow_preserves_prior_db_without_bypass(
    fake_playwright: FakePlaywrightEnv,
    isolated_env: E2EWorkspace,
    settings_with_creds: Any,
    job_db: Any,
    sample_fisi_offers: list[Any],
    pkg_browser: Any,
    pkg_portal_scraper: Any,
    pkg_exporter: Any,
) -> None:
    offers = [
        sample_fisi_offers[0],
        sample_fisi_offers[1],
        sample_fisi_offers[2],
        sample_fisi_offers[0].model_copy(update={"refnr": "10000-W4-4"}),
    ]
    scraper = pkg_portal_scraper.PortalScraper(settings_with_creds)
    captcha_error: Any = None

    for idx, offer in enumerate(offers):
        fake_playwright.scenario = "captcha_detail" if idx == 2 else "authenticated_portal"
        try:
            details = scraper.fetch_offer_details(offer.refnr)
            updated = offer.model_copy(
                update={
                    "contact_name": details.get("contact_name", offer.contact_name),
                    "contact_email": details.get("contact_email", offer.contact_email),
                }
            )
            job_db.upsert_offer(updated)
        except pkg_browser.CaptchaDetectedError as err:
            captcha_error = err
            break

    assert captcha_error is not None
    assert captcha_error.screenshot_path.is_file()
    assert captcha_error.screenshot_path.stat().st_size > 0
    assert captcha_error.screenshot_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert fake_playwright.captcha_solver_calls == 0

    saved = job_db.list_offers()
    assert len(saved) == 2
    pkg_exporter.export_offers(saved, isolated_env.export_dir)
    assert (isolated_env.export_dir / "INDEX_FOR_LLM.md").is_file()
    assert len(list((isolated_env.export_dir / "offers").glob("*/offer.md"))) == 2


def test_workload_05_cross_surface_application_funnel_cli_mcp_hermes(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    local_jobsuche_server: tuple[str, FakeJobsucheRouter],
    hermes_recording_ctx: FakeHermesContext,
    pkg_db: Any,
) -> None:
    yaml = require_module("yaml")
    base_url, _ = local_jobsuche_server
    refnr = "10000-1206415041-S"

    # 1. CLI sync -> status: new
    assert run_cli(["sync", "--wo", "Aachen", "--umkreis", "25"], workspace=isolated_env, api_base_url=base_url).returncode == 0

    # 2. Antigravity MCP -> list, update to shortlisted, sync & export
    async def _mcp_step(session: Any, init_result: Any) -> None:
        await session.call_tool("jobsuche_list_offers", {})
        r_upd = await session.call_tool(
            "jobsuche_update_offer_status",
            {"refnr": refnr, "status": "shortlisted", "notes": "Top fit"},
        )
        assert not getattr(r_upd, "isError", False)
        r_exp = await session.call_tool("jobsuche_sync_and_export", {"wo": "Aachen", "umkreis": 25})
        assert not getattr(r_exp, "isError", False)

    run_mcp_stdio(isolated_env, _mcp_step, api_base_url=base_url)

    # 3. CLI transitions shortlisted -> drafted -> applied (+ invalid status check)
    assert run_cli(["status", refnr, "drafted", "--notes", "Anschreiben drafted"], workspace=isolated_env).returncode == 0
    assert run_cli(["status", refnr, "applied", "--notes", "Sent email"], workspace=isolated_env).returncode == 0
    assert run_cli(["status", refnr, "invalid_funnel_state"], workspace=isolated_env).returncode != 0

    # 4. Hermes plugin reads status and exports LLM docs
    monkeypatch.setenv("JOBSUCHE_API_BASE_URL", base_url)
    monkeypatch.setenv("API_BASE_URL", base_url)
    plugin = load_hermes_jobsuche_module(monkeypatch, hermes_home=isolated_env.root / "hermes_home")
    plugin.register(hermes_recording_ctx)
    h_st = json.loads(hermes_recording_ctx.tools["jobsuche_status"]["handler"]({"db_path": str(isolated_env.db_path)}))
    h_exp = json.loads(
        hermes_recording_ctx.tools["jobsuche_export_llm"]["handler"](
            {"db_path": str(isolated_env.db_path), "export_dir": str(isolated_env.export_dir)}
        )
    )
    assert h_st["ok"] is True
    assert h_exp["ok"] is True

    with pkg_db.JobDatabase(isolated_env.db_path) as db:
        offer = db.get_offer(refnr)
        assert offer is not None and offer.status.value == "applied"
        hist_str = json.dumps(db.get_history(refnr), default=str)
        pos_short = hist_str.find("shortlisted")
        pos_draft = hist_str.find("drafted")
        pos_app = hist_str.find("applied")
        assert -1 not in (pos_short, pos_draft, pos_app)
        assert pos_short < pos_draft < pos_app

    md_path = next((isolated_env.export_dir / "offers").glob(f"*__{refnr}/offer.md"))
    fm = yaml.safe_load(md_path.read_text(encoding="utf-8").split("---\n", 2)[1])
    assert fm["status"] == "applied"
    jdata = json.loads(md_path.with_name("offer.json").read_text(encoding="utf-8"))
    assert jdata["status"] == "applied"


def test_workload_06_hermes_cron_monitoring_delta_telegram_digest(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    local_jobsuche_server: tuple[str, FakeJobsucheRouter],
    hermes_recording_ctx: FakeHermesContext,
) -> None:
    base_url, router = local_jobsuche_server
    monkeypatch.setenv("JOBSUCHE_API_BASE_URL", base_url)
    monkeypatch.setenv("API_BASE_URL", base_url)

    tick1_items = [
        _make_v6_item("10000-W6-1", "FISI Tick 1 A", "Company Alpha"),
        _make_v6_item("10000-W6-2", "FISI Tick 1 B", "Company Beta"),
        _make_v6_item("10000-W6-3", "FISI Tick 1 C", "Company Gamma"),
    ]
    router.v6_pages = {
        1: {"maxErgebnisse": 3, "page": 1, "size": 25, "ergebnisliste": copy.deepcopy(tick1_items)}
    }

    plugin = load_hermes_jobsuche_module(monkeypatch, hermes_home=isolated_env.root / "hermes_home")
    plugin.register(hermes_recording_ctx)
    h_monitor = hermes_recording_ctx.tools["jobsuche_monitor"]["handler"]

    r1 = json.loads(
        h_monitor({"wo": "Aachen", "umkreis": 25, "format": "telegram", "db_path": str(isolated_env.db_path)})
    )
    assert r1["ok"] is True and r1["new_count"] == 3 and r1["message"]

    # Tick 2: identical 3 items -> 0 new
    r2 = json.loads(
        h_monitor({"wo": "Aachen", "umkreis": 25, "format": "telegram", "db_path": str(isolated_env.db_path)})
    )
    assert r2["ok"] is True and r2["new_count"] == 0 and r2.get("message", "") == ""

    # Tick 3: add 1 new offer with Markdown-special characters
    special_company = "ACME_IT *Cloud* [Aachen] GmbH"
    special_title = "Azubi_FISI *Pro* [2026]"
    router.v6_pages = {
        1: {
            "maxErgebnisse": 4,
            "page": 1,
            "size": 25,
            "ergebnisliste": [
                *copy.deepcopy(tick1_items),
                _make_v6_item("10000-W6-4", special_title, special_company),
            ],
        }
    }
    r3 = json.loads(
        h_monitor({"wo": "Aachen", "umkreis": 25, "format": "telegram", "db_path": str(isolated_env.db_path)})
    )
    assert r3["ok"] is True and r3["new_count"] == 1
    msg = r3["message"]
    assert "10000-W6-4" in msg or "ACME" in msg
    assert "10000-W6-1" not in msg
    assert "\\_" in msg or "\\*" in msg or "\\[" in msg


@pytest.mark.live_api
def test_workload_07a_live_public_api_aachen_fisi_smoke(
    isolated_env: E2EWorkspace,
    settings_without_creds: Any,
    job_db: Any,
    pkg_api_client: Any,
    pkg_models: Any,
    pkg_sorter: Any,
    pkg_exporter: Any,
) -> None:
    client = pkg_api_client.JobsucheAPIClient(settings_without_creds)
    offers = client.search_jobs(
        pkg_models.SearchFilter(
            was="Fachinformatiker/in für Systemintegration",
            wo="Aachen",
            umkreis=25,
            angebotsart=4,
            size=5,
            limit=5,
        )
    )
    assert len(offers) >= 1
    scored = [pkg_sorter.score_offer(o) for o in offers]
    job_db.upsert_offers(scored)
    pkg_exporter.export_offers(scored, isolated_env.export_dir)
    assert (isolated_env.export_dir / "INDEX_FOR_LLM.md").is_file()
    assert list((isolated_env.export_dir / "offers").glob("*/offer.md"))


@pytest.mark.live_ba
def test_workload_07b_live_ba_login_if_env_present(
    pkg_config: Any,
    pkg_portal_scraper: Any,
) -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        pytest.skip(f"BA credentials absent: {env_path} does not exist")

    settings = pkg_config.get_settings(env_file=env_path)
    if not (settings.ba_username and settings.ba_password and settings.ba_totp_secret):
        pytest.skip(f"BA credentials absent in {env_path}")

    res = pkg_portal_scraper.PortalScraper(settings).login()
    assert res.get("authenticated") is True or res.get("ok") is True
