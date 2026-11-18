# Changelog

All notable changes to this project will be documented in this file.

## [1.0.0] - 2026-11-18

### Added
- Core `jobsuche_pkg` Python package supporting Bundesagentur für Arbeit Jobsuche REST API (`pc/v6/jobs` search and `pc/v4/jobdetails/{base64(refnr)}` details) with SQLite deduplication and change history tracking (`db.py`).
- Relevance scoring (`0–100`), filtering, and company grouping (`sorter.py`) with LLM-ready Markdown and JSON bundle exporter (`exporter.py` -> `INDEX_FOR_LLM.md`, `companies_summary.json`, and `offers/<Company_Slug>__<RefNr>/{offer.md,offer.json}`).
- `jobsuche` CLI (`cli.py`) with `search`, `sync`, `export`, `companies`, `status`, `login`, and `otp-test` subcommands.
- Optional Playwright browser manager (`browser.py`), 6-digit TOTP generator with `<3s` rollover guard (`auth_otp.py`), and strict non-bypassing CAPTCHA detection (`CaptchaDetectedError`).
- Native Antigravity CLI plugin (`.agents/plugins/jobsuche/`) with stdio JSON-RPC 2.0 MCP server (`mcp_server.py`, 6 tools), `rules/AGENTS.md`, and `skills/jobsuche/SKILL.md`.
- Native Hermes Agent standalone v2 plugin (`plugin.yaml`, `__init__.py` `register(ctx)` with 4 tools and `jobsuche` skill, JSON and Telegram Markdown digest support).
- Comprehensive 4-tier E2E and unit test suite (`171` tests).
