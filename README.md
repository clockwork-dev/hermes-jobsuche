# hermes-jobsuche

An open-source **Hermes Agent native v2 plugin**, **Antigravity CLI MCP plugin**, and **Python CLI suite** (`jobsuche`) for searching, monitoring, scoring, and exporting **Ausbildung** offers from the **Bundesagentur für Arbeit (Jobsuche — `arbeitsagentur.de`)** into LLM-ready Markdown & JSON bundles.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Hermes Plugin v2](https://img.shields.io/badge/Hermes_Plugin-v2_standalone-purple.svg)](plugin.yaml)
[![Antigravity MCP](https://img.shields.io/badge/Antigravity_CLI-MCP_Plugin-teal.svg)](.agents/plugins/jobsuche/plugin.json)

---

## Features

- **Triple-Surface Architecture**:
  - **Hermes Agent Standalone v2 Plugin (`plugin.yaml` + `__init__.py`)**: Exposes `jobsuche_search`, `jobsuche_monitor`, `jobsuche_export_llm`, and `jobsuche_status` tools plus the bundled `jobsuche` skill.
  - **Antigravity CLI Plugin (`.agents/plugins/jobsuche/`)**: Bundles a stdio JSON-RPC 2.0 MCP server (`mcp_server.py`) with 6 tools (`jobsuche_search_ausbildung`, `jobsuche_fetch_offer_details`, `jobsuche_sync_and_export`, `jobsuche_list_offers`, `jobsuche_update_offer_status`, `jobsuche_auth_status`), `rules/AGENTS.md`, and `skills/jobsuche/SKILL.md`.
  - **Standalone CLI (`jobsuche`)**: 7 subcommands (`search`, `sync`, `export`, `companies`, `status`, `login`, `otp-test`) for terminal and cron workflows.
- **Fast Public REST API (`pc/v6/jobs` + `pc/v4/jobdetails`)**:
  - Queries `https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs` (`angebotsart=4` for *Ausbildung / Duales Studium*) and fetches full descriptions from `pc/v4/jobdetails/{base64(refnr)}` with zero browser overhead and no BA account required.
- **SQLite Deduplication & Audit History (`db.py`)**:
  - Tracks seen `refnr` IDs, groups offers by normalized `company_slug`, logs field changes (`title`, `start_date`, `city`, `status`) in `offer_history`, and preserves applicant triage states (`new`, `shortlisted`, `drafted`, `applied`, `rejected`, `ignored`) across re-syncs.
- **Relevance Scoring & LLM-Ready Exports (`sorter.py`, `exporter.py`)**:
  - Scores offers (`0–100`) by technical keyword alignment (e.g., *Linux, Debian, Netzwerk, Windows Server, Active Directory, Virtualisierung, Proxmox, Docker, Firewall, IT-Support*), distance (km), and start date.
  - Generates per-offer folders `exports/offers/<Company_Slug>__<RefNr>/{offer.md,offer.json}` with 16 YAML frontmatter fields and 5 structured sections, plus `exports/INDEX_FOR_LLM.md` and `exports/companies_summary.json` so AI agents can immediately draft tailored German *Anschreiben* and CVs.
- **Optional Playwright Enrichment & 6-Digit TOTP (`auth_otp.py`, `browser.py`, `portal_scraper.py`)**:
  - Supports `pyotp` 6-digit TOTP generation (with `<3s` window rollover guard) and IMAP/CLI fallback, `de-DE` stealth browser context with `storage_state.json` persistence, and strict CAPTCHA safety (never attempts to solve/bypass CAPTCHAs; saves diagnostic screenshot + JSON and alerts the user).

---

## Repository Layout

```text
.
├── .agents/
│   └── plugins/
│       └── jobsuche/                    # Antigravity CLI plugin (MCP server + rules + skill)
│           ├── assets/logo.svg
│           ├── mcp_config.json
│           ├── mcp_server.py
│           ├── plugin.json
│           ├── rules/AGENTS.md
│           └── skills/jobsuche/SKILL.md
├── plugins/
│   └── hermes/
│       └── jobsuche/                    # Hermes plugin mirror for local script installs
├── scripts/
│   ├── deploy_and_test_remote.sh        # Remote SSH/rsync test runner
│   └── install_plugins.sh               # Local installer for Antigravity & Hermes plugins
├── skills/
│   └── jobsuche/
│       └── SKILL.md                     # Bundled Hermes Agent skill
├── src/
│   └── jobsuche_pkg/
│       ├── __init__.py
│       ├── api_client.py                # Jobsuche REST API v6/v4 client
│       ├── auth_otp.py                  # 6-digit TOTP (pyotp) + IMAP/CLI fallback
│       ├── browser.py                   # Playwright context manager & CAPTCHA guard
│       ├── cli.py                       # `jobsuche` CLI entrypoint
│       ├── config.py                    # Settings & environment configuration
│       ├── db.py                        # SQLite storage, history & company profiles
│       ├── exporter.py                  # LLM-ready Markdown + JSON exporter
│       ├── models.py                    # Pydantic domain models
│       ├── portal_scraper.py            # Optional authenticated portal scraper
│       └── sorter.py                    # Relevance scoring, filtering & company grouping
├── tests/
│   ├── e2e/                             # 4-tier opaque-box E2E test suite (158 tests)
│   ├── test_api_and_db.py
│   ├── test_otp.py
│   ├── test_plugins.py
│   └── test_sorter_exporter.py
├── .env.example
├── CHANGELOG.md
├── LICENSE
├── README.md
├── __init__.py                          # Root Hermes plugin entrypoint (register(ctx))
├── plugin.yaml                          # Root Hermes plugin manifest (v2 standalone)
└── pyproject.toml
```

---

## Installation

### 1. Hermes Agent Plugin

```bash
# Install directly from GitHub
hermes plugins install clockwork-dev/hermes-jobsuche --enable --yes-deps

# Validate & inspect tools
hermes plugins validate ~/.hermes/plugins/jobsuche --json
hermes plugins doctor --ci ~/.hermes/plugins/jobsuche
```

### 2. Local Clone (Python CLI + Antigravity CLI + Hermes Agent)

```bash
git clone https://github.com/clockwork-dev/hermes-jobsuche.git
cd hermes-jobsuche

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"

# Link/install both Antigravity CLI and Hermes Agent plugins into ~/
./scripts/install_plugins.sh
```

---

## CLI Usage (`jobsuche`)

```bash
# Search Ausbildung offers (defaults: Fachinformatiker/in für Systemintegration, Aachen, 25 km)
.venv/bin/jobsuche search --was "Fachinformatiker/in für Systemintegration" --wo Aachen --umkreis 25

# Sync offers into SQLite (data/jobsuche.db) and export LLM bundles into exports/
.venv/bin/jobsuche sync --was "Fachinformatiker/in für Systemintegration" --wo Aachen --umkreis 25
.venv/bin/jobsuche export

# List grouped companies and highest-scoring offers
.venv/bin/jobsuche companies

# Update application status and notes
.venv/bin/jobsuche status 10000-1208214389-S shortlisted --notes "Linux, Windows Server, AD, Firewall, VPN"
```

---

## LLM Export Structure (`exports/`)

Running `jobsuche sync` + `jobsuche export` (or calling `jobsuche_sync_and_export` / `jobsuche_export_llm`) generates:

- `exports/INDEX_FOR_LLM.md` — Ranked Markdown table of all active offers (score, status, company, city/distance, start date, matched skills, relative path to `offer.md`).
- `exports/companies_summary.json` — Aggregated statistics per employer (`company_slug`, `offer_count`, `max_score`, `avg_score`, `offers`).
- `exports/offers/<Company_Slug>__<RefNr>/offer.md` — Token-optimized Markdown card with 16 YAML frontmatter keys (`refnr`, `title`, `profession`, `company`, `plz`, `city`, `distance_km`, `start_date`, `contact_name`, `contact_email`, `contact_phone`, `apply_url`, `portal_url`, `score`, `matched_skills`, `status`) and 5 structured sections for immediate *Anschreiben* generation.
- `exports/offers/<Company_Slug>__<RefNr>/offer.json` — Full normalized JSON payload.

---

## Testing

```bash
.venv/bin/pytest
```

---

## License

Released under the [MIT License](LICENSE).
