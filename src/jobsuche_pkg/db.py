"""SQLite persistence, deduplication, and audit history for Jobsuche offers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jobsuche_pkg.models import (
    ApplicationStatus,
    AusbildungOffer,
    CompanyProfile,
    slugify_company,
)

# Column -> AusbildungOffer attribute name for history diffing
_TRACKED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("title", "title"),
    ("profession", "profession"),
    ("company_name", "company"),
    ("plz", "plz"),
    ("city", "city"),
    ("street", "street"),
    ("region", "region"),
    ("distance_km", "distance_km"),
    ("start_date", "start_date"),
    ("published_date", "published_date"),
    ("modified_date", "modified_date"),
    ("education_required", "education_required"),
    ("work_mode", "work_mode"),
    ("description", "description"),
    ("requirements", "requirements"),
    ("company_info", "company_info"),
    ("contact_name", "contact_name"),
    ("contact_email", "contact_email"),
    ("contact_phone", "contact_phone"),
    ("apply_url", "apply_url"),
    ("portal_url", "portal_url"),
    ("is_vermittlungsvorschlag", "is_vermittlungsvorschlag"),
    ("status", "status"),
    ("notes", "notes"),
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS companies (
    company_slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT,
    plz TEXT,
    website TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offers (
    refnr TEXT PRIMARY KEY,
    company_slug TEXT NOT NULL REFERENCES companies(company_slug),
    title TEXT NOT NULL,
    profession TEXT,
    company_name TEXT NOT NULL,
    plz TEXT,
    city TEXT,
    street TEXT,
    region TEXT,
    distance_km REAL,
    start_date TEXT,
    published_date TEXT,
    modified_date TEXT,
    education_required TEXT,
    work_mode TEXT,
    description TEXT,
    requirements TEXT,
    company_info TEXT,
    contact_name TEXT,
    contact_email TEXT,
    contact_phone TEXT,
    apply_url TEXT,
    portal_url TEXT NOT NULL,
    is_vermittlungsvorschlag INTEGER NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0.0,
    matched_skills_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT,
    content_hash TEXT,
    raw_api_json TEXT,
    raw_portal_json TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offer_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    refnr TEXT NOT NULL REFERENCES offers(refnr) ON DELETE CASCADE,
    changed_at TEXT NOT NULL,
    field_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT
);

CREATE TABLE IF NOT EXISTS search_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_hash TEXT NOT NULL,
    params_json TEXT NOT NULL,
    total_found INTEGER NOT NULL,
    new_count INTEGER NOT NULL,
    ran_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_offers_company_slug ON offers(company_slug);
CREATE INDEX IF NOT EXISTS idx_offers_status ON offers(status);
CREATE INDEX IF NOT EXISTS idx_offers_city ON offers(city);
CREATE INDEX IF NOT EXISTS idx_offers_score ON offers(score);
CREATE INDEX IF NOT EXISTS idx_offer_history_refnr ON offer_history(refnr);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_diff_value(col: str, value: Any) -> str | None:
    if value is None:
        return None
    if col == "is_vermittlungsvorschlag":
        return "1" if bool(value) else "0"
    if col == "distance_km":
        return f"{float(value):.6g}"
    if isinstance(value, ApplicationStatus):
        return value.value
    text = str(value).strip()
    return text if text else None


def _compute_content_hash(values: dict[str, Any]) -> str:
    canonical = {col: _normalize_diff_value(col, values.get(col)) for col, _ in _TRACKED_COLUMNS}
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_dumps_or_none(data: dict[str, Any] | None) -> str | None:
    if data is None:
        return None
    return json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)


def _json_loads_or_none(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    loaded = json.loads(raw)
    return loaded if isinstance(loaded, dict) else None


class JobDatabase:
    """SQLite storage with refnr deduplication, company aggregation, and change history."""

    def __init__(self, db_path: str | Path) -> None:
        self._is_memory = str(db_path) == ":memory:"
        self.db_path: Path | str = ":memory:" if self._is_memory else Path(db_path)
        if not self._is_memory:
            assert isinstance(self.db_path, Path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn: sqlite3.Connection | None = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.create_function(
            "PY_LOWER",
            1,
            lambda s: s.lower() if isinstance(s, str) else s,
            deterministic=True,
        )
        self._conn.execute("PRAGMA foreign_keys = ON;")
        if not self._is_memory:
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA synchronous = NORMAL;")
        self._init_schema()

    def __enter__(self) -> JobDatabase:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying SQLite connection idempotently."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("JobDatabase connection is closed")
        return self._conn

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(_SCHEMA_SQL)

    def _upsert_company(
        self,
        *,
        company_slug: str,
        name: str,
        city: str | None,
        plz: str | None,
        website: str | None,
        now: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO companies (company_slug, name, city, plz, website, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company_slug) DO UPDATE SET
                name = CASE
                    WHEN excluded.name != 'Unbekannt' THEN excluded.name
                    ELSE companies.name
                END,
                city = COALESCE(excluded.city, companies.city),
                plz = COALESCE(excluded.plz, companies.plz),
                website = COALESCE(excluded.website, companies.website),
                updated_at = excluded.updated_at
            """,
            (company_slug, name, city, plz, website, now, now),
        )

    def upsert_offer(self, offer: AusbildungOffer) -> tuple[bool, list[str]]:
        """Insert or update an offer, returning `(is_new, changed_fields)`.

        On re-sync of an existing `refnr`:
        - Preserves user `status` when incoming is `new` and DB is non-`new`.
        - Preserves user `notes` when incoming is `None`.
        - Preserves previously enriched non-null fields when incoming is `None`.
        - Preserves `score` and `matched_skills` when incoming is unscored.
        - Logs all tracked field changes into `offer_history`.
        """
        now = _utc_now()
        company_slug = offer.company_slug or slugify_company(offer.company)
        incoming_raw_api = _json_dumps_or_none(offer.raw_api)
        incoming_raw_portal = _json_dumps_or_none(offer.raw_portal)
        incoming_skills_json = json.dumps(offer.matched_skills, ensure_ascii=False)

        with self.conn:
            existing = self.conn.execute(
                "SELECT * FROM offers WHERE refnr = ?",
                (offer.refnr,),
            ).fetchone()

            if existing is None:
                self._upsert_company(
                    company_slug=company_slug,
                    name=offer.company,
                    city=offer.city,
                    plz=offer.plz,
                    website=offer.apply_url,
                    now=now,
                )
                values = {
                    "title": offer.title,
                    "profession": offer.profession,
                    "company_name": offer.company,
                    "plz": offer.plz,
                    "city": offer.city,
                    "street": offer.street,
                    "region": offer.region,
                    "distance_km": offer.distance_km,
                    "start_date": offer.start_date,
                    "published_date": offer.published_date,
                    "modified_date": offer.modified_date,
                    "education_required": offer.education_required,
                    "work_mode": offer.work_mode,
                    "description": offer.description,
                    "requirements": offer.requirements,
                    "company_info": offer.company_info,
                    "contact_name": offer.contact_name,
                    "contact_email": offer.contact_email,
                    "contact_phone": offer.contact_phone,
                    "apply_url": offer.apply_url,
                    "portal_url": offer.portal_url,
                    "is_vermittlungsvorschlag": int(offer.is_vermittlungsvorschlag),
                    "status": offer.status.value,
                    "notes": offer.notes,
                }
                content_hash = _compute_content_hash(values)
                self.conn.execute(
                    """
                    INSERT INTO offers (
                        refnr, company_slug, title, profession, company_name,
                        plz, city, street, region, distance_km,
                        start_date, published_date, modified_date,
                        education_required, work_mode, description, requirements, company_info,
                        contact_name, contact_email, contact_phone, apply_url, portal_url,
                        is_vermittlungsvorschlag, score, matched_skills_json, status, notes,
                        content_hash, raw_api_json, raw_portal_json,
                        first_seen_at, last_seen_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?
                    )
                    """,
                    (
                        offer.refnr,
                        company_slug,
                        offer.title,
                        offer.profession,
                        offer.company,
                        offer.plz,
                        offer.city,
                        offer.street,
                        offer.region,
                        offer.distance_km,
                        offer.start_date,
                        offer.published_date,
                        offer.modified_date,
                        offer.education_required,
                        offer.work_mode,
                        offer.description,
                        offer.requirements,
                        offer.company_info,
                        offer.contact_name,
                        offer.contact_email,
                        offer.contact_phone,
                        offer.apply_url,
                        offer.portal_url,
                        int(offer.is_vermittlungsvorschlag),
                        float(offer.score),
                        incoming_skills_json,
                        offer.status.value,
                        offer.notes,
                        content_hash,
                        incoming_raw_api,
                        incoming_raw_portal,
                        now,
                        now,
                        now,
                    ),
                )
                return True, []

            # Merge-preserving update for existing offers
            effective_status = (
                existing["status"]
                if (
                    offer.status == ApplicationStatus.NEW
                    and existing["status"] != ApplicationStatus.NEW.value
                )
                else offer.status.value
            )
            effective_notes = existing["notes"] if offer.notes is None else offer.notes

            effective_company = (
                existing["company_name"]
                if (offer.company == "Unbekannt" and existing["company_name"] != "Unbekannt")
                else offer.company
            )
            effective_slug = (
                existing["company_slug"]
                if (offer.company == "Unbekannt" and existing["company_name"] != "Unbekannt")
                else company_slug
            )

            effective: dict[str, Any] = {
                "title": offer.title,
                "profession": offer.profession if offer.profession is not None else existing["profession"],
                "company_name": effective_company,
                "plz": offer.plz if offer.plz is not None else existing["plz"],
                "city": offer.city if offer.city is not None else existing["city"],
                "street": offer.street if offer.street is not None else existing["street"],
                "region": offer.region if offer.region is not None else existing["region"],
                "distance_km": (
                    offer.distance_km if offer.distance_km is not None else existing["distance_km"]
                ),
                "start_date": offer.start_date if offer.start_date is not None else existing["start_date"],
                "published_date": (
                    offer.published_date
                    if offer.published_date is not None
                    else existing["published_date"]
                ),
                "modified_date": (
                    offer.modified_date
                    if offer.modified_date is not None
                    else existing["modified_date"]
                ),
                "education_required": (
                    offer.education_required
                    if offer.education_required is not None
                    else existing["education_required"]
                ),
                "work_mode": offer.work_mode if offer.work_mode is not None else existing["work_mode"],
                "description": (
                    offer.description if offer.description is not None else existing["description"]
                ),
                "requirements": (
                    offer.requirements if offer.requirements is not None else existing["requirements"]
                ),
                "company_info": (
                    offer.company_info if offer.company_info is not None else existing["company_info"]
                ),
                "contact_name": (
                    offer.contact_name if offer.contact_name is not None else existing["contact_name"]
                ),
                "contact_email": (
                    offer.contact_email if offer.contact_email is not None else existing["contact_email"]
                ),
                "contact_phone": (
                    offer.contact_phone if offer.contact_phone is not None else existing["contact_phone"]
                ),
                "apply_url": offer.apply_url if offer.apply_url is not None else existing["apply_url"],
                "portal_url": offer.portal_url or existing["portal_url"],
                "is_vermittlungsvorschlag": int(
                    bool(existing["is_vermittlungsvorschlag"]) or offer.is_vermittlungsvorschlag
                ),
                "status": effective_status,
                "notes": effective_notes,
            }

            if offer.score == 0.0 and not offer.matched_skills and float(existing["score"]) > 0.0:
                effective_score = float(existing["score"])
                effective_skills_json = existing["matched_skills_json"]
            else:
                effective_score = float(offer.score)
                effective_skills_json = incoming_skills_json

            effective_raw_api = (
                incoming_raw_api if incoming_raw_api is not None else existing["raw_api_json"]
            )
            effective_raw_portal = (
                incoming_raw_portal if incoming_raw_portal is not None else existing["raw_portal_json"]
            )

            changed_fields: list[str] = []
            for col, field_name in _TRACKED_COLUMNS:
                old_norm = _normalize_diff_value(col, existing[col])
                new_norm = _normalize_diff_value(col, effective[col])
                if old_norm != new_norm:
                    changed_fields.append(field_name)
                    self.conn.execute(
                        """
                        INSERT INTO offer_history (refnr, changed_at, field_name, old_value, new_value)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (offer.refnr, now, field_name, old_norm, new_norm),
                    )

            metadata_changed = (
                float(existing["score"]) != effective_score
                or existing["matched_skills_json"] != effective_skills_json
                or existing["raw_api_json"] != effective_raw_api
                or existing["raw_portal_json"] != effective_raw_portal
                or existing["company_slug"] != effective_slug
            )
            effective_updated_at = now if (changed_fields or metadata_changed) else existing["updated_at"]
            content_hash = _compute_content_hash(effective)

            self._upsert_company(
                company_slug=effective_slug,
                name=effective_company,
                city=effective["city"],
                plz=effective["plz"],
                website=effective["apply_url"],
                now=now,
            )

            self.conn.execute(
                """
                UPDATE offers SET
                    company_slug = ?,
                    title = ?,
                    profession = ?,
                    company_name = ?,
                    plz = ?,
                    city = ?,
                    street = ?,
                    region = ?,
                    distance_km = ?,
                    start_date = ?,
                    published_date = ?,
                    modified_date = ?,
                    education_required = ?,
                    work_mode = ?,
                    description = ?,
                    requirements = ?,
                    company_info = ?,
                    contact_name = ?,
                    contact_email = ?,
                    contact_phone = ?,
                    apply_url = ?,
                    portal_url = ?,
                    is_vermittlungsvorschlag = ?,
                    score = ?,
                    matched_skills_json = ?,
                    status = ?,
                    notes = ?,
                    content_hash = ?,
                    raw_api_json = ?,
                    raw_portal_json = ?,
                    last_seen_at = ?,
                    updated_at = ?
                WHERE refnr = ?
                """,
                (
                    effective_slug,
                    effective["title"],
                    effective["profession"],
                    effective["company_name"],
                    effective["plz"],
                    effective["city"],
                    effective["street"],
                    effective["region"],
                    effective["distance_km"],
                    effective["start_date"],
                    effective["published_date"],
                    effective["modified_date"],
                    effective["education_required"],
                    effective["work_mode"],
                    effective["description"],
                    effective["requirements"],
                    effective["company_info"],
                    effective["contact_name"],
                    effective["contact_email"],
                    effective["contact_phone"],
                    effective["apply_url"],
                    effective["portal_url"],
                    effective["is_vermittlungsvorschlag"],
                    effective_score,
                    effective_skills_json,
                    effective["status"],
                    effective["notes"],
                    content_hash,
                    effective_raw_api,
                    effective_raw_portal,
                    now,
                    effective_updated_at,
                    offer.refnr,
                ),
            )
            return False, changed_fields

    def upsert_offers(self, offers: list[AusbildungOffer]) -> dict[str, int]:
        """Batch upsert offers and return inserted/updated/unchanged counts."""
        counts = {"inserted": 0, "updated": 0, "unchanged": 0}
        for offer in offers:
            is_new, changed_fields = self.upsert_offer(offer)
            if is_new:
                counts["inserted"] += 1
            elif changed_fields:
                counts["updated"] += 1
            else:
                counts["unchanged"] += 1
        return counts

    def _row_to_offer(self, row: sqlite3.Row) -> AusbildungOffer:
        skills_raw = row["matched_skills_json"]
        matched_skills = json.loads(skills_raw) if skills_raw else []
        return AusbildungOffer(
            refnr=row["refnr"],
            title=row["title"],
            profession=row["profession"],
            company=row["company_name"],
            company_slug=row["company_slug"],
            plz=row["plz"],
            city=row["city"],
            street=row["street"],
            region=row["region"],
            distance_km=row["distance_km"],
            start_date=row["start_date"],
            published_date=row["published_date"],
            modified_date=row["modified_date"],
            education_required=row["education_required"],
            work_mode=row["work_mode"],
            description=row["description"],
            requirements=row["requirements"],
            company_info=row["company_info"],
            contact_name=row["contact_name"],
            contact_email=row["contact_email"],
            contact_phone=row["contact_phone"],
            apply_url=row["apply_url"],
            portal_url=row["portal_url"],
            is_vermittlungsvorschlag=bool(row["is_vermittlungsvorschlag"]),
            score=float(row["score"]),
            matched_skills=matched_skills if isinstance(matched_skills, list) else [],
            status=ApplicationStatus(row["status"]),
            notes=row["notes"],
            raw_api=_json_loads_or_none(row["raw_api_json"]),
            raw_portal=_json_loads_or_none(row["raw_portal_json"]),
        )

    def get_offer(self, refnr: str) -> AusbildungOffer | None:
        """Retrieve a single offer by `refnr`, or `None` if not present."""
        row = self.conn.execute(
            "SELECT * FROM offers WHERE refnr = ?",
            (refnr,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_offer(row)

    def list_offers(
        self,
        *,
        status: ApplicationStatus | str | None = None,
        company: str | None = None,
        city: str | None = None,
        min_score: float | None = None,
        limit: int | None = None,
    ) -> list[AusbildungOffer]:
        """Query stored offers using safe parameterized filters."""
        clauses: list[str] = ["1=1"]
        params: list[Any] = []

        if status is not None:
            status_val = ApplicationStatus(status).value
            clauses.append("status = ?")
            params.append(status_val)

        if company is not None and company.strip():
            term = company.strip()
            escaped = f"%{_escape_like(term.lower())}%"
            clauses.append(
                "(PY_LOWER(company_slug) = ? OR PY_LOWER(company_slug) LIKE ? ESCAPE '\\' OR PY_LOWER(company_name) LIKE ? ESCAPE '\\')"
            )
            params.extend([term.lower(), escaped, escaped])

        if city is not None and city.strip():
            escaped_city = f"%{_escape_like(city.strip().lower())}%"
            clauses.append("PY_LOWER(city) LIKE ? ESCAPE '\\'")
            params.append(escaped_city)

        if min_score is not None:
            clauses.append("score >= ?")
            params.append(float(min_score))

        sql = (
            f"SELECT * FROM offers WHERE {' AND '.join(clauses)} "
            "ORDER BY score DESC, updated_at DESC, refnr ASC"
        )
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be >= 0")
            sql += " LIMIT ?"
            params.append(int(limit))

        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_offer(r) for r in rows]

    def update_status(
        self,
        refnr: str,
        status: ApplicationStatus | str,
        notes: str | None = None,
    ) -> AusbildungOffer:
        """Update an offer's status (and optionally notes) and log to `offer_history`."""
        new_status = ApplicationStatus(status)
        now = _utc_now()

        with self.conn:
            existing = self.conn.execute(
                "SELECT status, notes FROM offers WHERE refnr = ?",
                (refnr,),
            ).fetchone()
            if existing is None:
                raise KeyError(f"Offer not found: {refnr}")

            changed = False
            if existing["status"] != new_status.value:
                changed = True
                self.conn.execute(
                    """
                    INSERT INTO offer_history (refnr, changed_at, field_name, old_value, new_value)
                    VALUES (?, ?, 'status', ?, ?)
                    """,
                    (refnr, now, existing["status"], new_status.value),
                )

            effective_notes = existing["notes"]
            if notes is not None:
                cleaned_notes = notes.strip() if isinstance(notes, str) else str(notes)
                effective_notes = cleaned_notes if cleaned_notes else None
                if effective_notes != existing["notes"]:
                    changed = True
                    self.conn.execute(
                        """
                        INSERT INTO offer_history (refnr, changed_at, field_name, old_value, new_value)
                        VALUES (?, ?, 'notes', ?, ?)
                        """,
                        (refnr, now, existing["notes"], effective_notes),
                    )

            if changed:
                self.conn.execute(
                    """
                    UPDATE offers SET status = ?, notes = ?, updated_at = ?
                    WHERE refnr = ?
                    """,
                    (new_status.value, effective_notes, now, refnr),
                )

        updated = self.get_offer(refnr)
        assert updated is not None
        return updated

    def get_history(self, refnr: str) -> list[dict[str, Any]]:
        """Return chronological change-history entries for `refnr`."""
        rows = self.conn.execute(
            """
            SELECT id, refnr, changed_at, field_name, old_value, new_value
            FROM offer_history
            WHERE refnr = ?
            ORDER BY changed_at ASC, id ASC
            """,
            (refnr,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_companies(self) -> list[CompanyProfile]:
        """Aggregate stored offers into sorted `CompanyProfile` objects."""
        rows = self.conn.execute(
            """
            SELECT
                c.company_slug,
                c.name AS company_name,
                c.city AS company_city,
                c.plz AS company_plz,
                c.website AS company_website,
                o.refnr,
                o.city AS offer_city,
                o.plz AS offer_plz,
                o.score,
                o.portal_url,
                o.apply_url
            FROM companies c
            INNER JOIN offers o ON o.company_slug = c.company_slug
            ORDER BY c.company_slug ASC, o.score DESC, o.refnr ASC
            """
        ).fetchall()

        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            slug = row["company_slug"]
            entry = grouped.setdefault(
                slug,
                {
                    "company_slug": slug,
                    "name": row["company_name"],
                    "company_city": row["company_city"],
                    "company_plz": row["company_plz"],
                    "company_website": row["company_website"],
                    "offer_rows": [],
                },
            )
            if row["refnr"] is not None:
                entry["offer_rows"].append(row)

        profiles: list[CompanyProfile] = []
        for entry in grouped.values():
            offer_rows = entry["offer_rows"]
            if not offer_rows:
                continue
            scores = [float(r["score"]) for r in offer_rows]
            offer_refs = [str(r["refnr"]) for r in offer_rows]

            cities: list[str] = []
            seen_cities: set[str] = set()
            apply_urls: list[str] = []
            seen_urls: set[str] = set()
            first_city: str | None = None
            first_plz: str | None = None
            first_portal_url: str | None = None

            for r in offer_rows:
                c_val = r["offer_city"]
                if c_val:
                    if first_city is None:
                        first_city = c_val
                    if c_val not in seen_cities:
                        seen_cities.add(c_val)
                        cities.append(c_val)
                if first_plz is None and r["offer_plz"]:
                    first_plz = r["offer_plz"]
                if first_portal_url is None and r["portal_url"]:
                    first_portal_url = r["portal_url"]
                u_val = r["apply_url"]
                if u_val and u_val not in seen_urls:
                    seen_urls.add(u_val)
                    apply_urls.append(u_val)

            if not cities and entry["company_city"]:
                cities.append(entry["company_city"])

            profiles.append(
                CompanyProfile(
                    company_slug=entry["company_slug"],
                    name=entry["name"],
                    city=first_city or entry["company_city"],
                    plz=first_plz or entry["company_plz"],
                    offer_count=len(offer_rows),
                    max_score=max(scores, default=0.0),
                    avg_score=round(sum(scores) / len(scores), 2) if scores else 0.0,
                    offers=offer_refs,
                    portal_url=first_portal_url or entry["company_website"],
                    apply_urls=apply_urls,
                    cities=cities,
                )
            )

        profiles.sort(key=lambda p: (-p.max_score, -p.offer_count, p.name.lower()))
        return profiles

    def record_search_run(self, params: dict[str, Any], total_found: int, new_count: int) -> int:
        """Persist a search execution record and return its row ID."""
        params_json = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
        query_hash = hashlib.sha256(params_json.encode("utf-8")).hexdigest()[:16]
        ran_at = _utc_now()
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO search_runs (query_hash, params_json, total_found, new_count, ran_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (query_hash, params_json, int(total_found), int(new_count), ran_at),
            )
            assert cursor.lastrowid is not None
            return int(cursor.lastrowid)
