"""Unit and integration tests for M1 (`config`, `models`, `db`, `api_client`)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

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


def test_settings_defaults_without_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in ("BA_USERNAME", "BA_PASSWORD", "BA_TOTP_SECRET", "JOBSUCHE_DB_PATH", "JOBSUCHE_EXPORT_DIR"):
        monkeypatch.delenv(key, raising=False)

    settings = get_settings(env_file=tmp_path / "nonexistent.env")
    assert isinstance(settings, Settings)
    assert settings.default_was == "Fachinformatiker/in für Systemintegration"
    assert settings.default_wo == "Aachen"
    assert settings.default_umkreis == 25
    assert settings.default_angebotsart == 4
    assert settings.api_key == "jobboerse-jobsuche"
    assert settings.has_ba_credentials() is False
    assert settings.has_totp() is False
    assert settings.has_imap_config() is False


def test_models_slugify_and_search_filter_validation() -> None:
    assert slugify_company("Müller & Söhne IT-Systemhaus GmbH") == "mueller-soehne-it-systemhaus-gmbh"
    assert slugify_company("  !!!  ") == "unbekannt"

    sf = SearchFilter(plz="52062", umkreis=25)
    assert sf.wo == "52062"
    assert sf.angebotsart == 4

    with pytest.raises(ValueError):
        SearchFilter(plz="123")

    with pytest.raises(ValueError):
        SearchFilter(start_date_from="2026-09-01", start_date_to="2026-08-01")


def test_api_client_search_v6_v4_pagination_and_enrichment() -> None:
    ref1 = "10000-1206415041-S"
    ref2 = "10000-1206415999-S"
    ref1_b64 = base64.b64encode(ref1.encode("utf-8")).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-API-Key") == "jobboerse-jobsuche"
        if request.url.path.endswith("/pc/v6/jobs"):
            page = int(request.url.params.get("page", "1"))
            if page == 1:
                return httpx.Response(
                    200,
                    json={
                        "maxErgebnisse": 2,
                        "page": 1,
                        "size": 1,
                        "ergebnisliste": [
                            {
                                "referenznummer": ref1,
                                "stellenangebotsTitel": "Ausbildung FISI 2026",
                                "hauptberuf": "Fachinformatiker/in - Systemintegration",
                                "firma": "Müller & Söhne IT GmbH",
                                "stellenlokationen": [
                                    {
                                        "adresse": {
                                            "strasse": "Theaterstr.",
                                            "hausnummer": "12",
                                            "plz": "52062",
                                            "ort": "Aachen",
                                            "region": "NRW",
                                        }
                                    }
                                ],
                                "entfernung": 2,
                                "eintrittszeitraum": {"von": "2026-08-01"},
                            }
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "maxErgebnisse": 2,
                    "page": 2,
                    "size": 1,
                    "stellenangebote": [
                        {
                            "refnr": ref2,
                            "titel": "Azubi Systemintegration (v4 schema)",
                            "beruf": "Fachinformatiker/in - Systemintegration",
                            "arbeitgeber": "Müller & Söhne IT GmbH",
                            "arbeitsort": {"plz": "52146", "ort": "Würselen", "entfernung": 8},
                            "eintrittsdatum": "2026-09-01",
                            "externeURL": "https://karriere.mueller.example/apply",
                        }
                    ],
                },
            )
        if request.url.path.endswith(f"/pc/v4/jobdetails/{ref1_b64}"):
            return httpx.Response(
                200,
                json={
                    "referenznummer": ref1,
                    "stellenangebotsBeschreibung": "Linux, Debian, Proxmox, Docker, Firewall.",
                    "geforderterBildungsabschluss": "Fachabitur",
                    "allianzpartnerUrl": "https://partner.example/apply/1",
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handler)
    with JobsucheAPIClient(transport=transport, backoff_factor=0.0) as client:
        offers = client.search_jobs(SearchFilter(was="FISI", wo="Aachen", umkreis=25, size=1, limit=2))
        assert len(offers) == 2
        assert offers[0].refnr == ref1
        assert offers[0].company_slug == "mueller-soehne-it-gmbh"
        assert offers[0].street == "Theaterstr. 12"
        assert offers[1].refnr == ref2
        assert offers[1].apply_url == "https://karriere.mueller.example/apply"

        class DummyScraper:
            def fetch_offer_details(self, r: str) -> dict[str, Any]:
                assert r == ref1
                return {
                    "contact_name": "Frau Anna Weber",
                    "contact_email": "jobs@mueller.example",
                    "contact_phone": "+49 241 99999",
                    "requirements": "Linux Kenntnisse",
                    "is_vermittlungsvorschlag": True,
                }

        enriched = client.enrich_offer(offers[0], scraper=DummyScraper(), fetch_api_details=True)
        assert "Proxmox" in (enriched.description or "")
        assert enriched.education_required == "Fachabitur"
        assert enriched.apply_url == "https://partner.example/apply/1"
        assert enriched.contact_email == "jobs@mueller.example"
        assert enriched.is_vermittlungsvorschlag is True


def test_api_client_error_handling_and_retries() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if request.url.path.endswith("/auth-error"):
            return httpx.Response(403, json={"message": "No match found"})
        if request.url.path.endswith("/rate-limit"):
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"})
        if request.url.path.endswith("/missing"):
            return httpx.Response(404, json={"message": "missing"})
        return httpx.Response(500, json={"message": "server error"})

    transport = httpx.MockTransport(handler)
    with JobsucheAPIClient(
        transport=transport,
        max_retries=1,
        backoff_factor=0.0,
        sleep_fn=lambda _: None,
    ) as client:
        with pytest.raises(JobsucheAuthError):
            client._request_json("GET", "/auth-error")
        with pytest.raises(JobsucheRateLimitError):
            client._request_json("GET", "/rate-limit")
        with pytest.raises(JobsucheNotFoundError):
            client._request_json("GET", "/missing")
        with pytest.raises(JobsucheAPIError):
            client._request_json("GET", "/server-error")


def test_sqlite_db_deduplication_history_and_company_aggregation(tmp_path: Path) -> None:
    db_file = tmp_path / "data" / "jobsuche.db"
    with JobDatabase(db_file) as db:
        offer_v1 = AusbildungOffer(
            refnr="10000-1206415041-S",
            title="Ausbildung FISI 2026",
            profession="Fachinformatiker/in - Systemintegration",
            company="Müller & Söhne IT GmbH",
            plz="52062",
            city="Aachen",
            distance_km=2.5,
            start_date="2026-08-01",
            description="Initiale Beschreibung",
            score=78.0,
            matched_skills=["Linux", "Docker"],
        )
        is_new, changed = db.upsert_offer(offer_v1)
        assert is_new is True
        assert changed == []

        # Unchanged re-upsert
        is_new2, changed2 = db.upsert_offer(offer_v1)
        assert is_new2 is False
        assert changed2 == []

        # Status update and notes
        updated_status = db.update_status("10000-1206415041-S", ApplicationStatus.SHORTLISTED, notes="Top Arbeitgeber")
        assert updated_status.status == ApplicationStatus.SHORTLISTED
        assert updated_status.notes == "Top Arbeitgeber"

        # Modified offer from API re-sync should preserve user status & notes while logging field diff
        offer_v2 = offer_v1.model_copy(
            update={
                "title": "Ausbildung FISI 2026 (Aktualisiert)",
                "description": "Erweiterte Beschreibung mit Proxmox",
                "contact_email": "ausbildung@mueller.example",
                "status": ApplicationStatus.NEW,
                "notes": None,
            }
        )
        is_new3, changed3 = db.upsert_offer(offer_v2)
        assert is_new3 is False
        assert "title" in changed3
        assert "description" in changed3
        assert "contact_email" in changed3

        stored = db.get_offer("10000-1206415041-S")
        assert stored is not None
        assert stored.status == ApplicationStatus.SHORTLISTED
        assert stored.notes == "Top Arbeitgeber"
        assert stored.contact_email == "ausbildung@mueller.example"

        history = db.get_history("10000-1206415041-S")
        history_fields = [h["field_name"] for h in history]
        assert "status" in history_fields
        assert "notes" in history_fields
        assert "title" in history_fields
        assert "description" in history_fields

        # Add second offer for same company
        offer_second = AusbildungOffer(
            refnr="10000-1206415999-S",
            title="Ausbildung FISI - Netzwerk",
            company="Müller & Söhne IT GmbH",
            plz="52146",
            city="Würselen",
            score=64.0,
        )
        batch_stats = db.upsert_offers([offer_v2, offer_second])
        assert batch_stats == {"inserted": 1, "updated": 0, "unchanged": 1}

        companies = db.list_companies()
        assert len(companies) == 1
        comp: CompanyProfile = companies[0]
        assert comp.company_slug == "mueller-soehne-it-gmbh"
        assert comp.offer_count == 2
        assert comp.max_score == 78.0
        assert set(comp.cities) == {"Aachen", "Würselen"}

        run_id = db.record_search_run({"was": "FISI", "wo": "Aachen"}, total_found=2, new_count=1)
        assert run_id >= 1
