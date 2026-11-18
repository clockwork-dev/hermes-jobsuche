"""LLM-ready Markdown & JSON exporter for Jobsuche Ausbildung offers."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from jobsuche_pkg.models import ApplicationStatus, AusbildungOffer, slugify_company
from jobsuche_pkg.sorter import build_company_profiles, sort_offers

_SAFE_REFNR_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_FRONTMATTER_KEYS: tuple[str, ...] = (
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
)


def _sanitize_refnr_for_path(refnr: str) -> str:
    cleaned = refnr.strip()
    if _SAFE_REFNR_RE.match(cleaned) and ".." not in cleaned:
        return cleaned
    no_dots = cleaned.replace("..", "_")
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", no_dots).strip("._-")
    return sanitized or "refnr"


def _build_frontmatter_dict(offer: AusbildungOffer) -> dict[str, Any]:
    status_val = (
        offer.status.value
        if isinstance(offer.status, ApplicationStatus)
        else str(offer.status)
    )
    return {
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
        "status": status_val,
    }


def _serialize_yaml_frontmatter(data: dict[str, Any]) -> str:
    """Serialize frontmatter using single-line JSON-encoded YAML values.

    YAML 1.2 is a strict superset of JSON; single-line JSON scalars prevent
    embedded newlines, `---`, colons, quotes, or brackets in titles/companies
    from breaking `raw.split("---\\n", 2)` or `yaml.safe_load()`.
    """
    lines = ["---"]
    for key in _FRONTMATTER_KEYS:
        val = data.get(key)
        lines.append(f"{key}: {json.dumps(val, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def render_offer_markdown(offer: AusbildungOffer) -> str:
    """Render an `AusbildungOffer` as YAML-frontmatter Markdown with the 5 LLM sections."""
    fm_dict = _build_frontmatter_dict(offer)
    fm_block = _serialize_yaml_frontmatter(fm_dict)

    description_text = (
        offer.description.strip()
        if offer.description and offer.description.strip()
        else "Описание вакансии не указано в открытых данных API. См. карточку на портале BA."
    )

    req_lines: list[str] = []
    if offer.requirements and offer.requirements.strip():
        req_lines.append(offer.requirements.strip())
    if offer.education_required:
        req_lines.append(f"- **Требуемое образование**: {offer.education_required}")
    if offer.matched_skills:
        req_lines.append(f"- **Ключевые навыки (совпадения)**: {', '.join(offer.matched_skills)}")
    if not req_lines:
        req_lines.append("Специальные требования не детализированы; ориентируйтесь на профиль FISI.")
    requirements_text = "\n".join(req_lines)

    company_lines: list[str] = [f"- **Работодатель**: {offer.company}"]
    location_parts = [p for p in (offer.street, offer.plz, offer.city, offer.region) if p]
    if location_parts:
        company_lines.append(f"- **Локация**: {', '.join(location_parts)}")
    if offer.distance_km is not None:
        company_lines.append(f"- **Расстояние**: {offer.distance_km:.1f} км")
    if offer.work_mode:
        company_lines.append(f"- **Формат работы**: {offer.work_mode}")
    if offer.start_date:
        company_lines.append(f"- **Дата начала (Ausbildungsbeginn)**: {offer.start_date}")
    if offer.company_info and offer.company_info.strip():
        company_lines.append(f"\n{offer.company_info.strip()}")
    company_text = "\n".join(company_lines)

    contact_lines: list[str] = [
        f"- **Контактное лицо**: {offer.contact_name or 'Damen und Herren (не указано)'}",
        f"- **Email**: {offer.contact_email or 'не указан'}",
        f"- **Телефон**: {offer.contact_phone or 'не указан'}",
        f"- **Ссылка для отклика (Apply URL)**: {offer.apply_url or 'не указана'}",
        f"- **Карточка на портале BA**: {offer.portal_url}",
    ]
    if offer.is_vermittlungsvorschlag:
        contact_lines.append("- **Vermittlungsvorschlag**: Да (официальное направление от Agentur für Arbeit)")
    contacts_text = "\n".join(contact_lines)

    salutation = (
        f"Обращайтесь лично к контактному лицу ({offer.contact_name})."
        if offer.contact_name
        else "Используйте стандартное обращение «Sehr geehrte Damen und Herren»."
    )
    skills_hint = (
        f"Сделайте акцент на практическом опыте с: {', '.join(offer.matched_skills)}."
        if offer.matched_skills
        else "Подчеркните интерес к системному администрированию, Linux, сетям и инфраструктуре."
    )
    ai_hints_lines = [
        f"1. **Приветствие**: {salutation}",
        f"2. **Позиция и референс**: Укажите в теме письма `{offer.title}` и Referenznummer `{offer.refnr}`.",
        f"3. **Технический фокус**: {skills_hint}",
        f"4. **Мотивация к компании**: Свяжите интерес к обучению в `{offer.company}` ({offer.city or 'регион Aachen'}) с задачами из раздела описания.",
    ]
    if offer.start_date:
        ai_hints_lines.append(f"5. **Готовность к старту**: Подтвердите готовность начать обучение с `{offer.start_date}`.")
    ai_hints_text = "\n".join(ai_hints_lines)

    body_sections = [
        f"# {offer.title} — {offer.company}",
        "",
        "## Описание и задачи",
        description_text,
        "",
        "## Требования к кандидату",
        requirements_text,
        "",
        "## О компании",
        company_text,
        "",
        "## Контакты для Bewerbung",
        contacts_text,
        "",
        "## Подсказки для ИИ при генерации Anschreiben",
        ai_hints_text,
        "",
    ]
    return f"{fm_block}\n\n" + "\n".join(body_sections)


def _render_index_markdown(
    sorted_offers: list[AusbildungOffer],
    folder_by_refnr: dict[str, str],
) -> str:
    lines: list[str] = [
        "# INDEX FOR LLM — Сводный индекс вакансий Ausbildung",
        "",
        f"Всего актуальных предложений: **{len(sorted_offers)}**",
        "",
        "| # | Score | Status | Компания | Позиция | Город (км) | Старт | Навыки | Карточка (RefNr) |",
        "|---|-------|--------|----------|---------|------------|-------|--------|------------------|",
    ]

    for idx, offer in enumerate(sorted_offers, start=1):
        status_val = (
            offer.status.value
            if isinstance(offer.status, ApplicationStatus)
            else str(offer.status)
        )
        company_clean = (offer.company or "Unbekannt").replace("|", "/").replace("\n", " ")
        title_clean = (offer.title or "").replace("|", "/").replace("\n", " ")
        city_str = offer.city or "-"
        if offer.distance_km is not None:
            loc_str = f"{city_str} ({offer.distance_km:.1f} km)"
        else:
            loc_str = city_str
        start_str = offer.start_date or "-"
        skills_str = ", ".join(offer.matched_skills[:5]) if offer.matched_skills else "-"
        folder_name = folder_by_refnr[offer.refnr]
        # Note: folder_name contains offer.refnr once (or sanitized refnr); if sanitized differs,
        # include offer.refnr in label so refnr appears exactly once per row.
        if offer.refnr in folder_name:
            card_cell = f"`offers/{folder_name}/offer.md`"
        else:
            card_cell = f"`{offer.refnr}` (`offers/{folder_name}/offer.md`)"

        lines.append(
            f"| {idx} | {offer.score:.1f} | {status_val} | {company_clean} | {title_clean} "
            f"| {loc_str} | {start_str} | {skills_str} | {card_cell} |"
        )

    lines.append("")
    return "\n".join(lines)


def export_offers(
    offers: list[AusbildungOffer],
    export_dir: Path | str,
) -> dict[str, Any]:
    """Export offers into LLM-friendly Markdown/JSON folders and summary index files."""
    export_root = Path(export_dir)
    offers_root = export_root / "offers"
    offers_root.mkdir(parents=True, exist_ok=True)
    offers_root_resolved = offers_root.resolve()

    # Deduplicate offers by refnr (preserving latest instance on overwrite)
    deduped_map: dict[str, AusbildungOffer] = {}
    for offer in offers:
        deduped_map[offer.refnr] = offer

    sorted_offers = sort_offers(list(deduped_map.values()))
    profiles = build_company_profiles(sorted_offers)

    folder_by_refnr: dict[str, str] = {}
    exported_items: list[dict[str, Any]] = []

    for offer in sorted_offers:
        safe_slug = slugify_company(offer.company_slug or offer.company)
        safe_refnr = _sanitize_refnr_for_path(offer.refnr)
        folder_name = f"{safe_slug}__{safe_refnr}"
        offer_folder = (offers_root / folder_name).resolve()
        if not offer_folder.is_relative_to(offers_root_resolved):
            raise ValueError(f"Unsafe export path detected for offer {offer.refnr!r}")

        offer_folder.mkdir(parents=True, exist_ok=True)
        folder_by_refnr[offer.refnr] = folder_name

        md_path = offer_folder / "offer.md"
        json_path = offer_folder / "offer.json"

        md_content = render_offer_markdown(offer)
        md_path.write_text(md_content, encoding="utf-8")

        offer_payload = offer.model_dump(mode="json")
        street_val = offer_payload.get("street")
        if isinstance(street_val, str) and "straße" in street_val and "Straße" not in street_val:
            offer_payload["street"] = street_val.replace("straße", "Straße")
        offer_payload["status"] = (
            offer.status.value
            if isinstance(offer.status, ApplicationStatus)
            else str(offer.status)
        )
        json_path.write_text(
            json.dumps(offer_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        exported_items.append(
            {
                "refnr": offer.refnr,
                "company": offer.company,
                "company_slug": safe_slug,
                "score": float(offer.score),
                "status": offer_payload["status"],
                "offer_md": str(md_path),
                "offer_json": str(json_path),
            }
        )

    index_path = export_root / "INDEX_FOR_LLM.md"
    index_path.write_text(
        _render_index_markdown(sorted_offers, folder_by_refnr),
        encoding="utf-8",
    )

    summary_path = export_root / "companies_summary.json"
    companies_payload = [p.model_dump(mode="json") for p in profiles]
    summary_path.write_text(
        json.dumps(companies_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return {
        "ok": True,
        "export_dir": str(export_root),
        "index_path": str(index_path),
        "companies_summary_path": str(summary_path),
        "offer_count": len(sorted_offers),
        "company_count": len(profiles),
        "offers": exported_items,
    }
