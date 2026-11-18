# Jobsuche & FISI Bewerbung Workspace Rules

## 1. Scope & Core Mission
These rules govern how agents interact with the **Jobsuche Ausbildung Automation Suite** (`jobsuche_pkg`), its SQLite database (`data/jobsuche.db`), the LLM export tree (`exports/`), and the preparation of German *Bewerbungsunterlagen* (*Anschreiben* and *Lebenslauf*) for *Ausbildung zum Fachinformatiker für Systemintegration (FISI)* and related IT professions.

## 2. Search & Synchronization Defaults
- **Default Profession (`was`)**: `Fachinformatiker/in für Systemintegration`
- **Default Location (`wo`)**: `Aachen`
- **Default Radius (`umkreis`)**: `25` km (`>= 0`)
- **Default Offer Type (`angebotsart`)**: `4` (*Ausbildung / Duales Studium*)
- **REST API Endpoints**:
  - Search always uses `GET /pc/v6/jobs` with header `X-API-Key: jobboerse-jobsuche` (never `pc/v4/jobs`, which returns HTTP 403 on the live gateway).
  - Offer details use `GET /pc/v4/jobdetails/{base64(refnr)}`.

## 3. Application Status Lifecycle & Database Integrity
- Every offer is uniquely identified by its `refnr` (e.g., `10000-1206415041-S`) and grouped by deterministic ASCII `company_slug` (`ä/ö/ü/ß -> ae/oe/ue/ss`).
- Valid `ApplicationStatus` values are strictly:
  - `new` — newly discovered offer, not yet reviewed.
  - `shortlisted` — selected as a strong match for Bewerbung.
  - `drafted` — tailored *Anschreiben* / application package has been drafted.
  - `applied` — application has been submitted to the employer.
  - `rejected` — negative reply received or position closed.
  - `ignored` — not relevant or out of scope.
- Never overwrite a non-`new` status back to `new` during automated synchronization runs. Use `jobsuche_update_offer_status` (or `jobsuche status <refnr> <status> --notes ...`) to transition statuses and record an audit trail in `offer_history`.

## 4. Security, Privacy & CAPTCHA Guardrails
- **No Hardcoded Secrets**: Never hardcode `BA_USERNAME`, `BA_PASSWORD`, `BA_TOTP_SECRET`, or `BA_IMAP_PASSWORD` in code, rules, skills, or exported Markdown files.
- **No CAPTCHA Bypass**: If `arbeitsagentur.de` presents a CAPTCHA or security challenge (`Sicherheitsabfrage`), **never** attempt to solve, click through, or bypass it programmatically. Always rely on `StealthBrowserManager.detect_captcha_or_block()` to save a diagnostic screenshot and status file, and instruct the user to complete the challenge in headed mode (`jobsuche login --headed`).

## 5. Exporting & Reading LLM Offer Packages
- Before drafting an application, run `jobsuche_sync_and_export` (or read `exports/INDEX_FOR_LLM.md` and `exports/companies_summary.json`) to view ranked offers and identify companies offering multiple positions.
- Each exported offer folder `exports/offers/<Company_Slug>__<RefNr>/` contains:
  - `offer.md` with 16 YAML frontmatter keys (`refnr`, `title`, `profession`, `company`, `plz`, `city`, `distance_km`, `start_date`, `contact_name`, `contact_email`, `contact_phone`, `apply_url`, `portal_url`, `score`, `matched_skills`, `status`) and 5 structured sections:
    1. `## Описание и задачи`
    2. `## Требования к кандидату`
    3. `## О компании`
    4. `## Контакты для Bewerbung`
    5. `## Подсказки для ИИ при генерации Anschreiben`
  - `offer.json` with the full normalized `AusbildungOffer` payload.

## 6. Standards for FISI Anschreiben Generation
When generating a German *Bewerbungsschreiben (Anschreiben)* from an `offer.md` card:
- **Personalization**:
  - Address `contact_name` directly (`Sehr geehrte Frau <Nachname>,` or `Sehr geehrter Herr <Nachname>,`) whenever `contact_name` is present in the YAML frontmatter. Fall back to `Sehr geehrte Damen und Herren,` only when no contact person is listed.
  - Include the exact `title`, `start_date` (*Ausbildungsbeginn*), and `refnr` (*Referenznummer*) in the subject line (*Betreffzeile*).
- **Technical Alignment (FISI Profile)**:
  - Explicitly connect the candidate's hands-on experience (Linux/Debian administration, networking, TCP/IP, DNS/DHCP, firewalling, virtualization with Proxmox/Docker, Windows Server/Active Directory, scripting, and structured troubleshooting) to the `matched_skills` and tasks in `## Описание и задачи`.
- **Format & Tone**:
  - Follow DIN 5008 business letter conventions, concise 1-page length (3–4 focused paragraphs: motivation for the specific company, practical FISI technical foundation, soft skills/reliability, and call to action for a *Vorstellungsgespräch* or *Praktikum*).
  - After drafting the *Anschreiben*, update the offer status to `drafted` via `jobsuche_update_offer_status` with a note referencing the draft file.
