# hermes-jobsuche

Native [Hermes Agent](https://github.com/NousResearch/hermes-agent) standalone v2 plugin and bundled skill for searching, monitoring, scoring, and exporting **Ausbildung** offers from **Bundesagentur für Arbeit (Jobsuche — `arbeitsagentur.de`)**.

## Features

- **Hybrid REST API + Optional Authenticated Portal Scraping**:
  - Queries the live `pc/v6/jobs` search endpoint (`X-API-Key: jobboerse-jobsuche`, `angebotsart=4` for *Ausbildung / Duales Studium*) and `pc/v4/jobdetails/{base64(refnr)}` detail endpoint.
  - Supports authenticated portal enrichment (`PortalScraper` + `StealthBrowserManager`) with 6-digit TOTP (`pyotp`) and IMAP/CLI fallback.
- **SQLite Deduplication & Audit History**:
  - Tracks offers by `refnr` (*Referenznummer*), aggregates employers into `companies`, logs field diffs and status transitions (`new`, `shortlisted`, `drafted`, `applied`, `rejected`, `ignored`) in `offer_history`, and records `search_runs`.
- **FISI Relevance Scoring & Company Grouping**:
  - Scores offers (`0–100`) by technical keywords (*Linux, Debian, Netzwerk, Windows Server, Active Directory, Virtualisierung, Proxmox, Docker, IT-Support, Scripting, Firewall*), proximity (default `Aachen` + `25` km), and start date.
- **LLM-Ready Export**:
  - Generates `INDEX_FOR_LLM.md`, `companies_summary.json`, and `offers/<Company_Slug>__<RefNr>/{offer.md,offer.json}` with 16 YAML frontmatter keys and 5 structured Russian/German sections for *Anschreiben* generation.
- **Cron-Friendly Telegram Monitoring**:
  - `jobsuche_monitor` emits newly discovered offers as JSON and escaped Telegram Markdown digests (returning an empty message when `new_count == 0`).

## Plugin Layout

```text
~/.hermes/plugins/jobsuche/
├── plugin.yaml                  # Hermes standalone plugin manifest v2
├── __init__.py                  # Plugin entrypoint: register(ctx) for 4 tools + skill
├── README.md                    # Documentation
└── skills/
    └── jobsuche/
        └── SKILL.md             # Bundled Hermes Agent skill instructions
```

## Registered Tools

| Tool Name | Description |
| --- | --- |
| `jobsuche_search` | Search BA Jobsuche for *Ausbildung* offers, score FISI relevance, and group by company |
| `jobsuche_monitor` | Deduplicate offers against SQLite and return only new offers as JSON + Telegram digest |
| `jobsuche_export_llm` | Export stored SQLite offers into `INDEX_FOR_LLM.md`, `companies_summary.json`, and per-offer `offer.md` / `offer.json` |
| `jobsuche_status` | Query or update offer statuses (`new`, `shortlisted`, `drafted`, `applied`, `rejected`, `ignored`), company summaries, and BA auth state |

## Validation & Usage

```bash
hermes plugins validate ~/.hermes/plugins/jobsuche --json
hermes plugins doctor --ci ~/.hermes/plugins/jobsuche
hermes plugins enable jobsuche
```
