---
name: jobsuche
description: "Search, monitor, and export BA Jobsuche Ausbildung offers."
category: productivity
version: "1.0.0"
author: "Hermes Agent"
license: MIT
platforms:
  - linux
  - macos
  - windows
metadata:
  hermes:
    tags:
      - jobsuche
      - ausbildung
      - arbeitsagentur
      - fisi
      - bewerbung
    related_skills:
      - kleinanzeigen
---

# Jobsuche Ausbildung Automation

Search, monitor, score, and export *Ausbildung* offers from Bundesagentur für Arbeit (`arbeitsagentur.de` / `rest.arbeitsagentur.de`) with SQLite deduplication, company grouping, and LLM-ready Markdown/JSON cards for personalized *Anschreiben* and *Bewerbung* generation.

## When to Use

- Searching for *Ausbildung* or *Duales Studium* positions in Germany (defaulting to *Fachinformatiker/in für Systemintegration — FISI* in `Aachen` within `25` km, or any profession/city/5-digit PLZ).
- Monitoring new *Ausbildung* listings via cron (`jobsuche_monitor`) with SQLite deduplication by `refnr` (*Referenznummer*) and formatted Telegram Markdown digests.
- Exporting structured LLM-ready vacancy cards (`INDEX_FOR_LLM.md`, `companies_summary.json`, `offers/<Company_Slug>__<RefNr>/offer.md` + `offer.json`) via `jobsuche_export_llm` to draft tailored German *Anschreiben*.
- Tracking application funnel statuses (`new`, `shortlisted`, `drafted`, `applied`, `rejected`, `ignored`) and notes via `jobsuche_status`.
- Do not use to solve or bypass any CAPTCHA, Turnstile, or *Sicherheitsabfrage* challenge.

## Native Plugin Tools

When the `jobsuche` plugin is enabled in Hermes Agent, prefer calling these four native tools directly:

| Tool | Purpose | Key Parameters |
| --- | --- | --- |
| `jobsuche_search` | One-shot search, FISI relevance scoring (0–100), and company grouping | `was`, `wo`, `plz`, `umkreis` (default `25`), `angebotsart` (default `4`), `limit`, `min_score`, `enrich_details`, `save_to_db`, `db_path` |
| `jobsuche_monitor` | Incremental search + SQLite deduplication by `refnr` + optional Telegram digest | Same search parameters plus `format` (`"json"` \| `"telegram"` \| `"markdown"`), `dry_run`, `db_path` |
| `jobsuche_export_llm` | Export SQLite offers into `INDEX_FOR_LLM.md`, `companies_summary.json`, and per-offer `offer.md` + `offer.json` | `db_path`, `export_dir`, `status`, `company`, `city`, `min_score`, `limit` |
| `jobsuche_status` | Inspect or update offer funnel statuses (`new`, `shortlisted`, `drafted`, `applied`, `rejected`, `ignored`), company summaries, and BA session status | `db_path`, `refnr`, `status`, `notes` |

## CLI Fallback (via `terminal`)

When operating via the command line in the project directory (`~/FISI-Bewerbung`), invoke the installed `jobsuche` CLI from `.venv`:

```bash
.venv/bin/jobsuche search --was "Fachinformatiker/in für Systemintegration" --wo Aachen --umkreis 25 --format json
.venv/bin/jobsuche sync --wo Aachen --umkreis 25
.venv/bin/jobsuche export
.venv/bin/jobsuche companies --format json
.venv/bin/jobsuche status 10000-1206415041-S shortlisted --notes "Strong Linux & Proxmox stack"
```

## Procedure for FISI Bewerbung & Anschreiben

### 1. Discover and Synchronize Offers

Call `jobsuche_monitor` (or `jobsuche_search` with `save_to_db: true`) for the target profession and region:
- Default profession (`was`): `"Fachinformatiker/in für Systemintegration"`
- Default location (`wo`): `"Aachen"`, radius (`umkreis`): `25` km, offer type (`angebotsart`): `4` (*Ausbildung*).

### 2. Export LLM-Ready Cards

Call `jobsuche_export_llm` to generate the export tree:
1. Use `read_file` on `exports/INDEX_FOR_LLM.md` to review all offers ranked by FISI relevance score (`0–100`), distance, start date, and matched technical keywords (*Linux, Debian, Netzwerk, Windows Server, Active Directory, Virtualisierung, Proxmox, Docker, IT-Support, Scripting, Firewall*).
2. Use `read_file` on `exports/companies_summary.json` to spot employers offering multiple positions or branch locations.
3. For a shortlisted offer, use `read_file` on `exports/offers/<Company_Slug>__<RefNr>/offer.md`, which contains:
   - 16 YAML frontmatter fields (`refnr`, `title`, `profession`, `company`, `plz`, `city`, `distance_km`, `start_date`, `contact_name`, `contact_email`, `contact_phone`, `apply_url`, `portal_url`, `score`, `matched_skills`, `status`).
   - Five structured sections:
     - `## Описание и задачи`
     - `## Требования к кандидату`
     - `## О компании`
     - `## Контакты для Bewerbung`
     - `## Подсказки для ИИ при генерации Anschreiben`

### 3. Draft Personalized Anschreiben & Update Funnel Status

- Address `contact_name` directly when present (`Sehr geehrte Frau ...` / `Sehr geehrter Herr ...`), or use `Sehr geehrte Damen und Herren`.
- Reference the exact `title` and `refnr` in the subject line (`Bewerbung um einen Ausbildungsplatz als ... — Ref.-Nr. ...`).
- Align candidate hands-on strengths with `matched_skills` and company tasks.
- Update the offer lifecycle status with `jobsuche_status` (`shortlisted` -> `drafted` -> `applied`).

## Security & Anti-Bot Rules

- **Strict CAPTCHA Policy**: Never attempt to solve, click through, wait out, or bypass any CAPTCHA, Cloudflare Turnstile, or *Sicherheitsabfrage* screen. When a challenge occurs during portal scraping, the browser manager saves a diagnostic PNG screenshot and status JSON and recommends running `jobsuche login --headed` so the user can complete verification manually.

## Verification

- [ ] Search parameters validated (`umkreis >= 0`, `limit >= 1`, 5-digit PLZ when numeric).
- [ ] `jobsuche_monitor` deduplicates against SQLite and escapes Telegram Markdown special characters (`_`, `*`, `[`, `]`, `` ` ``).
- [ ] `jobsuche_export_llm` writes `INDEX_FOR_LLM.md`, `companies_summary.json`, and `offers/<Company_Slug>__<RefNr>/{offer.md,offer.json}`.
- [ ] Application status transitions recorded in SQLite via `jobsuche_status`.
