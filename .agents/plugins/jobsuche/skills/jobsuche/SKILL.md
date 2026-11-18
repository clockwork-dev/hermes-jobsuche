---
name: jobsuche
description: >-
  Search, enrich, score, track, and export Ausbildung offers from Bundesagentur für Arbeit (arbeitsagentur.de) and generate tailored German FISI Anschreiben and Bewerbung packages from LLM-ready offer cards.
---

# Jobsuche Ausbildung & FISI Bewerbung Skill

Use this skill whenever the user wants to discover, filter, enrich, track, or export *Ausbildung* offers (especially *Fachinformatiker/in für Systemintegration — FISI* in the Aachen region) or prepare personalized German *Bewerbungsunterlagen* (*Anschreiben*, *Lebenslauf* highlights, employer comparisons).

## Available MCP Tools

1. **`jobsuche_search_ausbildung`**:
   - Searches `pc/v6/jobs` with automatic schema normalization, relevance scoring, filtering, and optional SQLite persistence.
   - Key parameters: `was` (default `"Fachinformatiker/in für Systemintegration"`), `wo` (default `"Aachen"`), `umkreis` (default `25`), `limit`, `mode` (`"api"`, `"browser"`, or `"hybrid"`).
2. **`jobsuche_fetch_offer_details`**:
   - Fetches detailed offer descriptions, required education, and employer contacts/application links by `refnr` (via `pc/v4/jobdetails/{base64(refnr)}` and optional authenticated portal scraping).
3. **`jobsuche_sync_and_export`**:
   - Runs a full search + detail enrichment + FISI relevance scoring + SQLite deduplicated upsert, then writes the LLM export tree under `exports/`:
     - `exports/INDEX_FOR_LLM.md`
     - `exports/companies_summary.json`
     - `exports/offers/<Company_Slug>__<RefNr>/offer.md`
     - `exports/offers/<Company_Slug>__<RefNr>/offer.json`
4. **`jobsuche_list_offers`**:
   - Queries tracked offers from SQLite with optional filters: `status` (`new`, `shortlisted`, `drafted`, `applied`, `rejected`, `ignored`), `company`, `city`, `min_score`, `limit`.
5. **`jobsuche_update_offer_status`**:
   - Updates an offer's `status` and `notes` by `refnr` while logging an audit entry in `offer_history`.
6. **`jobsuche_auth_status`**:
   - Checks whether BA portal credentials, TOTP 2FA (`BA_TOTP_SECRET`), IMAP fallback, and saved Playwright `storage_state.json` session are configured and valid.

## Step-by-Step Workflow

### Step 1: Synchronize & Export Current Offers
1. Call `jobsuche_sync_and_export` with `wo="Aachen"` and `umkreis=25` (or the user's target city/PLZ and radius).
2. Read `exports/INDEX_FOR_LLM.md` in a single `read_file` call to review all ranked offers sorted by FISI relevance score (`score` 0–100) and distance.
3. Read `exports/companies_summary.json` to spot employers offering multiple *Ausbildung* positions or locations.

### Step 2: Inspect High-Priority Offer Cards
1. For top-scoring offers (or offers selected by the user), read `exports/offers/<Company_Slug>__<RefNr>/offer.md`.
2. If contact details or full descriptions are missing, call `jobsuche_fetch_offer_details` with `{"refnr": "<RefNr>"}` to enrich the record.
3. Shortlist promising offers using `jobsuche_update_offer_status` with `status="shortlisted"` and concise notes explaining why the employer fits.

### Step 3: Draft a Tailored FISI Anschreiben
1. Inspect the YAML frontmatter of `offer.md` for:
   - `company`, `title`, `refnr`, `start_date`, `plz`, `city`
   - `contact_name`, `contact_email`, `contact_phone`, `apply_url`
   - `matched_skills` and `score`
2. Review the 5 Markdown sections in `offer.md`:
   - `## Описание и задачи`
   - `## Требования к кандидату`
   - `## О компании`
   - `## Контакты для Bewerbung`
   - `## Подсказки для ИИ при генерации Anschreiben`
3. Draft a DIN 5008-compliant German *Anschreiben*:
   - **Betreff**: `Bewerbung um eine Ausbildung zum Fachinformatiker für Systemintegration ab <start_date> – Referenznummer <refnr>`
   - **Anrede**: Use `contact_name` if available (`Sehr geehrte Frau ...` / `Sehr geehrter Herr ...`), otherwise `Sehr geehrte Damen und Herren,`.
   - **Body**: Match the employer's tech stack (`matched_skills`: Linux/Debian, Windows Server, Active Directory, Proxmox/Docker virtualization, networking/firewalls, scripting, IT support) with concrete examples from the candidate's homelab and system administration practice.
4. Once drafted, call `jobsuche_update_offer_status` with `status="drafted"` (and `status="applied"` once sent).
