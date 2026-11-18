"""Unit and integration tests for M3 (`sorter`, `exporter`, `cli`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from jobsuche_pkg.exporter import export_offers
from jobsuche_pkg.models import ApplicationStatus, AusbildungOffer, SearchFilter
from jobsuche_pkg.sorter import (
    build_company_profiles,
    filter_offers,
    group_by_company,
    score_offer,
)


def _sample_offers() -> list[AusbildungOffer]:
    return [
        AusbildungOffer(
            refnr="10000-1206415041-S",
            title="Ausbildung Fachinformatiker/in - Systemintegration (m/w/d) 2026",
            profession="Fachinformatiker/in - Systemintegration",
            company="Müller & Söhne IT-Systemhaus GmbH",
            plz="52062",
            city="Aachen",
            street="Theaterstraße 12",
            region="Nordrhein-Westfalen",
            distance_km=2.0,
            start_date="2026-08-01",
            description=(
                "Praxisnahe Ausbildung mit Linux, Debian, Windows Server, Active Directory, "
                "Virtualisierung (Proxmox, Docker), Netzwerk, Firewall und IT-Support."
            ),
            requirements="Mittlere Reife oder Fachabitur, Begeisterung für IT-Infrastruktur.",
            company_info="Systemhaus im Herzen von Aachen.",
            contact_name="Frau Anna Weber",
            contact_email="karriere@mueller-it.example",
            contact_phone="+49 241 123456",
            apply_url="https://karriere.mueller-it.example/apply/1",
        ),
        AusbildungOffer(
            refnr="10000-1206415999-S",
            title="Azubi FISI — Netzwerk & Scripting",
            profession="Fachinformatiker/in - Systemintegration",
            company="Müller & Söhne IT-Systemhaus GmbH",
            plz="52146",
            city="Würselen",
            distance_km=9.0,
            start_date="2026-09-01",
            description="Linux, Scripting, Firewall und Netzwerkbetrieb.",
            apply_url="https://karriere.mueller-it.example/apply/2",
        ),
        AusbildungOffer(
            refnr="10000-1206420001-S",
            title="Ausbildung Systemintegration (Cloud & Proxmox)",
            profession="Fachinformatiker/in - Systemintegration",
            company="Aachener Rechenzentrum AG",
            plz="52074",
            city="Aachen",
            distance_km=4.0,
            start_date="2026-08-15",
            description="Clusterbetrieb mit Proxmox, Docker und Debian Linux.",
            apply_url="https://arz.example/apply",
        ),
    ]


def test_sorter_scoring_grouping_and_filtering() -> None:
    offers = [score_offer(o) for o in _sample_offers()]
    assert all(0.0 <= o.score <= 100.0 for o in offers)
    assert offers[0].score > offers[1].score
    skills_lower = {s.lower() for s in offers[0].matched_skills}
    for kw in ("linux", "debian", "proxmox", "docker", "firewall"):
        assert kw in skills_lower

    grouped = group_by_company(offers)
    assert "mueller-soehne-it-systemhaus-gmbh" in grouped
    assert len(grouped["mueller-soehne-it-systemhaus-gmbh"]) == 2
    assert len(grouped["aachener-rechenzentrum-ag"]) == 1

    profiles = build_company_profiles(offers)
    assert len(profiles) == 2
    by_slug = {p.company_slug: p for p in profiles}
    mueller = by_slug["mueller-soehne-it-systemhaus-gmbh"]
    assert mueller.offer_count == 2
    assert mueller.max_score == pytest.approx(max(offers[0].score, offers[1].score))
    assert set(mueller.cities) == {"Aachen", "Würselen"}

    sf = SearchFilter(
        wo="Aachen",
        umkreis=25,
        start_date_from="2026-08-01",
        start_date_to="2026-08-31",
        status=ApplicationStatus.NEW,
    )
    filtered = filter_offers(offers, sf)
    assert {o.refnr for o in filtered} == {"10000-1206415041-S", "10000-1206420001-S"}


def test_exporter_writes_markdown_frontmatter_sections_and_indices(tmp_path: Path) -> None:
    export_dir = tmp_path / "exports"
    offers = [score_offer(o) for o in _sample_offers()]
    summary = export_offers(offers, export_dir)
    assert isinstance(summary, dict)

    index_md = export_dir / "INDEX_FOR_LLM.md"
    companies_json = export_dir / "companies_summary.json"
    assert index_md.is_file()
    assert companies_json.is_file()

    for offer in offers:
        folder = export_dir / "offers" / f"{offer.company_slug}__{offer.refnr}"
        md_file = folder / "offer.md"
        json_file = folder / "offer.json"
        assert md_file.is_file()
        assert json_file.is_file()

        md_text = md_file.read_text(encoding="utf-8")
        assert md_text.startswith("---\n")
        parts = md_text.split("---\n", 2)
        fm = yaml.safe_load(parts[1])
        required_keys = {
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
        assert required_keys.issubset(set(fm.keys()))

        headings = [
            "## Описание и задачи",
            "## Требования к кандидату",
            "## О компании",
            "## Контакты для Bewerbung",
            "## Подсказки для ИИ при генерации Anschreiben",
        ]
        positions = [md_text.find(h) for h in headings]
        assert all(p != -1 for p in positions)
        assert positions == sorted(positions)

        card = json.loads(json_file.read_text(encoding="utf-8"))
        assert card["refnr"] == offer.refnr
        assert card["company"] == offer.company

    companies_payload = json.loads(companies_json.read_text(encoding="utf-8"))
    companies_list = (
        companies_payload
        if isinstance(companies_payload, list)
        else companies_payload.get("companies", [])
    )
    assert len(companies_list) == 2
