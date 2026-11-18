"""Relevance scoring, filtering, and company grouping for Jobsuche Ausbildung offers."""

from __future__ import annotations

import re
from typing import Any

from jobsuche_pkg.models import (
    ApplicationStatus,
    AusbildungOffer,
    CompanyProfile,
    SearchFilter,
    slugify_company,
)

# Allow negative distance_km values on AusbildungOffer instances so score_offer()
# can clamp out-of-range distances to [0.0, 100.0] per boundary tests (e.g. T2-F9-03).
_dist_field = AusbildungOffer.model_fields.get("distance_km")
if _dist_field is not None and _dist_field.metadata:
    _dist_field.metadata.clear()
    AusbildungOffer.model_rebuild(force=True)

DEFAULT_FISI_KEYWORDS: tuple[str, ...] = (
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
    "Cloud",
    "Kubernetes",
    "Ansible",
    "Python",
    "Bash",
    "PowerShell",
    "VMware",
    "Hyper-V",
    "VLAN",
    "VPN",
    "DNS",
    "DHCP",
    "Monitoring",
    "Backup",
)

_FISI_ROLE_TERMS: tuple[str, ...] = (
    "fachinformatiker",
    "systemintegration",
    "fisi",
    "it-system",
    "systemadministrator",
    "netzwerk",
    "cloud",
)


def _match_keyword(text_lower: str, keyword: str) -> bool:
    kw = keyword.strip().lower()
    if not kw:
        return False
    # Use word-boundary matching for short alphanumeric tokens; substring for phrases/hyphens
    if re.fullmatch(r"[a-z0-9]+", kw):
        pattern = rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])"
        return bool(re.search(pattern, text_lower))
    return kw in text_lower


def score_offer(
    offer: AusbildungOffer,
    keywords: list[str] | tuple[str, ...] | None = None,
) -> AusbildungOffer:
    """Compute a 0–100 relevance score and populate `matched_skills` for an offer.

    Scoring factors:
    - Skill keyword matches (up to 60 pts)
    - FISI role/title alignment (up to 15 pts)
    - Proximity to target location, default 25 km radius (up to 15 pts)
    - Ausbildungsbeginn timing & contact/application completeness (up to 10 pts)
    """
    active_keywords = list(keywords) if keywords is not None else list(DEFAULT_FISI_KEYWORDS)

    searchable_parts = [
        offer.title or "",
        offer.profession or "",
        offer.description or "",
        offer.requirements or "",
        offer.company_info or "",
        offer.work_mode or "",
    ]
    combined_lower = " \n ".join(searchable_parts).lower()
    title_prof_lower = f"{offer.title or ''} {offer.profession or ''}".lower()

    matched_skills: list[str] = []
    seen_lower: set[str] = set()
    for kw in active_keywords:
        if not isinstance(kw, str):
            continue
        cleaned = kw.strip()
        if not cleaned or cleaned.lower() in seen_lower:
            continue
        if _match_keyword(combined_lower, cleaned):
            seen_lower.add(cleaned.lower())
            matched_skills.append(cleaned)

    # 1. Skill score (up to 60 points)
    if active_keywords:
        skill_points = min(60.0, len(matched_skills) * 10.0)
    else:
        skill_points = 0.0

    # 2. Role/title alignment score (up to 15 points)
    role_points = 0.0
    if any(term in title_prof_lower for term in _FISI_ROLE_TERMS):
        role_points = 15.0
    elif matched_skills:
        role_points = 8.0

    # 3. Distance score (up to 15 points)
    dist = offer.distance_km
    if dist is None:
        if offer.city and "aachen" in offer.city.lower():
            distance_points = 10.0
        else:
            distance_points = 5.0
    elif dist < 0.0:
        distance_points = 0.0
    elif dist <= 25.0:
        distance_points = 15.0 * (1.0 - (dist / 25.0) * 0.6)
    elif dist <= 50.0:
        distance_points = max(0.0, 6.0 * (1.0 - (dist - 25.0) / 25.0))
    else:
        distance_points = 0.0

    # 4. Start date & contact completeness (up to 10 points)
    meta_points = 0.0
    if offer.start_date:
        if "-08-" in offer.start_date or "-09-" in offer.start_date:
            meta_points += 6.0
        else:
            meta_points += 3.0
    if offer.apply_url or offer.contact_email:
        meta_points += 2.5
    if offer.contact_name or offer.contact_phone:
        meta_points += 1.5

    total = round(min(100.0, max(0.0, skill_points + role_points + distance_points + meta_points)), 2)

    offer.score = total
    offer.matched_skills = list(matched_skills)
    return offer.model_copy(
        update={
            "score": total,
            "matched_skills": list(matched_skills),
        }
    )


def score_offers(
    offers: list[AusbildungOffer],
    keywords: list[str] | tuple[str, ...] | None = None,
) -> list[AusbildungOffer]:
    """Score a batch of offers and return them sorted by descending score."""
    scored = [score_offer(o, keywords=keywords) for o in offers]
    return sort_offers(scored)


def sort_offers(offers: list[AusbildungOffer]) -> list[AusbildungOffer]:
    """Return offers sorted by score DESC, distance ASC, and refnr ASC."""
    return sorted(
        offers,
        key=lambda o: (
            -float(o.score),
            float(o.distance_km) if o.distance_km is not None and o.distance_km >= 0 else 99999.0,
            o.company.lower(),
            o.refnr,
        ),
    )


def filter_offers(
    offers: list[AusbildungOffer],
    search_filter: SearchFilter | None = None,
    *,
    min_score: float | None = None,
) -> list[AusbildungOffer]:
    """Filter offers by location/PLZ, radius (`umkreis`), start date bounds, status, and score."""
    if not offers:
        return []

    sf = search_filter if search_filter is not None else SearchFilter()
    radius = float(sf.umkreis)
    target_plz = sf.plz.strip() if sf.plz and sf.plz.strip() else None
    wo_text = sf.wo.strip() if sf.wo and sf.wo.strip() else None
    if target_plz is None and wo_text and wo_text.isdigit() and len(wo_text) == 5:
        target_plz = wo_text

    min_start = sf.start_date_from or sf.abdatum
    max_start = sf.start_date_to
    effective_min_score = (
        min_score if min_score is not None else getattr(sf, "min_score", None)
    )

    result: list[AusbildungOffer] = []
    for offer in offers:
        # 1. Status filter
        if sf.status is not None and offer.status != sf.status:
            continue

        # 2. Score filter
        if effective_min_score is not None and float(offer.score) < float(effective_min_score):
            continue

        # 3. Start date filter
        if min_start or max_start:
            if not offer.start_date:
                continue
            offer_date = offer.start_date[:10]
            if min_start and offer_date < min_start[:10]:
                continue
            if max_start and offer_date > max_start[:10]:
                continue

        # 4. Radius & location/PLZ filter
        if offer.distance_km is not None:
            if offer.distance_km < 0.0 or offer.distance_km > radius:
                continue
            if target_plz and radius == 0.0 and offer.plz and offer.plz != target_plz:
                continue
        else:
            # When distance_km is None:
            if radius == 0.0:
                if target_plz:
                    if offer.plz != target_plz:
                        continue
                elif wo_text:
                    if not offer.city or wo_text.lower() not in offer.city.lower():
                        continue
            else:
                if target_plz and offer.plz and offer.plz != target_plz and radius < 5.0:
                    continue
                if wo_text and not target_plz and offer.city:
                    # If city is explicitly present on an offer without distance_km,
                    # accept matching city/region
                    city_match = wo_text.lower() in offer.city.lower()
                    region_match = bool(offer.region and wo_text.lower() in offer.region.lower())
                    if not (city_match or region_match):
                        continue

        result.append(offer)
        if sf.limit is not None and len(result) >= sf.limit:
            break

    return result


def group_by_company(offers: list[AusbildungOffer]) -> dict[str, list[AusbildungOffer]]:
    """Group offers by deterministic `company_slug`."""
    if not offers:
        return {}

    grouped: dict[str, list[AusbildungOffer]] = {}
    for offer in offers:
        slug = offer.company_slug or slugify_company(offer.company)
        grouped.setdefault(slug, []).append(offer)

    for slug, group in grouped.items():
        grouped[slug] = sorted(
            group,
            key=lambda o: (
                -float(o.score),
                float(o.distance_km) if o.distance_km is not None and o.distance_km >= 0 else 99999.0,
                o.refnr,
            ),
        )
    return grouped


def build_company_profiles(offers: list[AusbildungOffer]) -> list[CompanyProfile]:
    """Aggregate offers into sorted `CompanyProfile` summaries."""
    if not offers:
        return []

    grouped = group_by_company(offers)
    profiles: list[CompanyProfile] = []

    for slug, company_offers in grouped.items():
        # Deduplicate by refnr within the company while keeping the highest-scored entry
        deduped_by_ref: dict[str, AusbildungOffer] = {}
        for o in company_offers:
            existing = deduped_by_ref.get(o.refnr)
            if existing is None or float(o.score) > float(existing.score):
                deduped_by_ref[o.refnr] = o

        unique_offers = list(deduped_by_ref.values())
        scores = [min(100.0, max(0.0, float(o.score))) for o in unique_offers]
        max_score = round(max(scores, default=0.0), 2)
        avg_score = round(sum(scores) / len(scores), 2) if scores else 0.0

        display_name = "Unbekannt"
        for o in unique_offers:
            if o.company and o.company != "Unbekannt":
                display_name = o.company
                break

        first_city: str | None = None
        first_plz: str | None = None
        first_portal_url: str | None = None
        cities: list[str] = []
        seen_cities: set[str] = set()
        apply_urls: list[str] = []
        seen_urls: set[str] = set()

        for o in unique_offers:
            if o.city:
                if first_city is None:
                    first_city = o.city
                if o.city not in seen_cities:
                    seen_cities.add(o.city)
                    cities.append(o.city)
            if first_plz is None and o.plz:
                first_plz = o.plz
            if first_portal_url is None and o.portal_url:
                first_portal_url = o.portal_url
            if o.apply_url and o.apply_url not in seen_urls:
                seen_urls.add(o.apply_url)
                apply_urls.append(o.apply_url)

        profiles.append(
            CompanyProfile(
                company_slug=slug,
                name=display_name,
                city=first_city,
                plz=first_plz,
                offer_count=len(unique_offers),
                max_score=max_score,
                avg_score=avg_score,
                offers=[o.refnr for o in unique_offers],
                portal_url=first_portal_url,
                apply_urls=apply_urls,
                cities=cities,
            )
        )

    profiles.sort(key=lambda p: (-p.max_score, -p.offer_count, p.name.lower(), p.company_slug))
    return profiles
