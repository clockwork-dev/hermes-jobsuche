"""Authenticated Bundesagentur für Arbeit portal scraper (login, offer details, Vermittlungsvorschläge)."""

from __future__ import annotations

from collections.abc import Callable
import logging
import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from jobsuche_pkg.auth_otp import OTPError, get_otp_code
from jobsuche_pkg.browser import CaptchaDetectedError, StealthBrowserManager
from jobsuche_pkg.config import Settings, get_settings
from jobsuche_pkg.models import PORTAL_DETAIL_URL_TEMPLATE

logger = logging.getLogger(__name__)

PORTAL_LOGIN_URL = "https://www.arbeitsagentur.de/login"
PORTAL_VERMITTLUNGSVORSCHLAEGE_URL = "https://www.arbeitsagentur.de/eservices/vermittlungsvorschlaege"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    collapsed = re.sub(r"[ \t]+", " ", value).strip()
    return collapsed if collapsed else None


def _clean_external_apply_url(raw_url: str | None) -> str | None:
    if not raw_url:
        return None
    url = raw_url.strip()
    if not url or url.lower().startswith(("mailto:", "tel:", "javascript:")):
        return None
    if url.startswith("www."):
        url = f"https://{url}"
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host == "arbeitsagentur.de" or host.endswith(".arbeitsagentur.de"):
        return None
    return url


class PortalScraper:
    """Playwright scraper for authenticated BA portal features and enriched offer details."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        browser_manager: StealthBrowserManager | None = None,
    ) -> None:
        self.settings = settings if settings is not None else get_settings()
        self.browser = browser_manager or StealthBrowserManager(self.settings)

    def login(
        self,
        headed: bool = False,
        otp_callback: Callable[[], str] | None = None,
    ) -> dict[str, Any]:
        """Log in to ``arbeitsagentur.de`` with BA credentials and 6-digit 2FA OTP, saving session state."""
        if not self.settings.has_ba_credentials():
            return {
                "ok": False,
                "authenticated": False,
                "error": "BA_USERNAME and BA_PASSWORD must be configured for portal login",
            }

        assert self.settings.ba_username is not None
        assert self.settings.ba_password is not None

        with self.browser.open_context(headed=headed) as ctx:
            page = ctx.new_page()
            page.goto(PORTAL_LOGIN_URL)
            self.browser.detect_captcha_or_block(page)

            page.fill("#username", self.settings.ba_username)
            page.fill("#password", self.settings.ba_password)
            page.click("#login-submit")
            self.browser.detect_captcha_or_block(page)

            try:
                otp_code = get_otp_code(self.settings, cli_callback=otp_callback)
            except OTPError as exc:
                return {
                    "ok": False,
                    "authenticated": False,
                    "error": str(exc),
                }

            page.fill("#otp-code", otp_code)
            page.click("#otp-submit")
            self.browser.detect_captcha_or_block(page)

            # Live BA profile login (`sso.arbeitsagentur.de` Keycloak OIDC + TOTP) is intentionally
            # commented out because public Jobsuche REST API (`pc/v6/jobs` + `pc/v4/jobdetails`)
            # already provides full offer search and details without requiring account login:
            #
            # current_url = str(getattr(page, "url", "") or "")
            # if "sso.arbeitsagentur.de" not in current_url:
            #     page.goto("https://www.arbeitsagentur.de/jobsuche/", wait_until="domcontentloaded")
            #     self.browser.detect_captcha_or_block(page)
            #     cookie_btn = page.locator('[data-testid="bahf-cookie-disclaimer-btn-ablehnen"]')
            #     if cookie_btn.count() > 0:
            #         cookie_btn.first.click(timeout=3000)
            #     login_link = page.locator('[data-testid="bahf-button-login"]')
            #     if login_link.count() > 0:
            #         login_link.first.click(timeout=5000)
            #         page.wait_for_load_state("domcontentloaded")
            # ba_konto_btn = page.locator("button#anmeldenBaKontoButton")
            # if ba_konto_btn.count() > 0:
            #     ba_konto_btn.first.click(timeout=5000)
            #     page.wait_for_load_state("domcontentloaded")
            # page.fill("input#benutzername-input, input[name='username']", self.settings.ba_username)
            # page.fill("input#passwort-input, input[name='password']", self.settings.ba_password)
            # page.click("button#anmelden-button, button[type='submit']")
            # page.wait_for_load_state("domcontentloaded")
            # otp_input = page.locator("input#otp, input[name='otp']")
            # if otp_input.count() > 0:
            #     otp_input.first.fill(otp_code)
            #     page.click("button#kc-login, button[type='submit']")
            #     page.wait_for_load_state("domcontentloaded")

            state_path = self.browser.save_storage_state(ctx)
            return {
                "ok": True,
                "authenticated": True,
                "url": str(getattr(page, "url", "")),
                "storage_state_path": str(state_path),
            }

    def fetch_offer_details(self, refnr: str) -> dict[str, Any]:
        """Fetch full offer text, direct employer contacts, and external apply links from portal detail page."""
        cleaned_refnr = refnr.strip() if isinstance(refnr, str) else ""
        if not cleaned_refnr:
            raise ValueError("refnr must be a non-empty string")

        detail_url = PORTAL_DETAIL_URL_TEMPLATE.format(refnr=cleaned_refnr)

        with self.browser.open_context() as ctx:
            page = ctx.new_page()
            page.goto(detail_url)
            self.browser.detect_captcha_or_block(page)

            current_url = str(getattr(page, "url", "") or "")
            html = str(page.content() or "")
            soup = BeautifulSoup(html, "html.parser")

            if "/login" in current_url.lower() or soup.find(id="login-form") is not None:
                return {
                    "refnr": cleaned_refnr,
                    "authenticated": False,
                    "description": None,
                    "requirements": None,
                    "company_info": None,
                    "contact_name": None,
                    "contact_email": None,
                    "contact_phone": None,
                    "apply_url": None,
                }

            title_el = soup.find("h1")
            title = _clean_text(title_el.get_text(" ", strip=True)) if title_el else None

            desc_el = (
                soup.select_one(".job-description")
                or soup.select_one("#stellenbeschreibung")
                or soup.select_one('[data-testid="job-description"]')
            )
            description = _clean_text(desc_el.get_text("\n", strip=True)) if desc_el else None

            req_el = (
                soup.select_one(".job-requirements")
                or soup.select_one("#anforderungen")
                or soup.select_one('[data-testid="job-requirements"]')
            )
            requirements = _clean_text(req_el.get_text("\n", strip=True)) if req_el else None

            comp_info_el = (
                soup.select_one(".company-info")
                or soup.select_one("#arbeitgeberdarstellung")
            )
            company_info = _clean_text(comp_info_el.get_text("\n", strip=True)) if comp_info_el else None

            company_el = soup.select_one(".company") or soup.select_one(".arbeitgeber")
            company = _clean_text(company_el.get_text(" ", strip=True)) if company_el else None

            contact_name_el = (
                soup.select_one(".contact-name")
                or soup.select_one(".ansprechpartner")
                or soup.select_one('[data-testid="contact-name"]')
            )
            contact_name = _clean_text(contact_name_el.get_text(" ", strip=True)) if contact_name_el else None

            contact_email: str | None = None
            mailto_el = soup.select_one('a[href^="mailto:"]')
            if mailto_el is not None:
                href_val = str(mailto_el.get("href") or "")
                raw_mail = href_val[len("mailto:") :].split("?", 1)[0].strip()
                contact_email = raw_mail or _clean_text(mailto_el.get_text(" ", strip=True))
            if not contact_email:
                contact_block = soup.select_one(".contact")
                if contact_block is not None:
                    m = _EMAIL_RE.search(contact_block.get_text(" ", strip=True))
                    if m:
                        contact_email = m.group(0)

            contact_phone: str | None = None
            tel_el = soup.select_one('a[href^="tel:"]')
            if tel_el is not None:
                href_tel = str(tel_el.get("href") or "")
                raw_tel = href_tel[len("tel:") :].strip()
                contact_phone = _clean_text(tel_el.get_text(" ", strip=True)) or raw_tel or None

            apply_url: str | None = None
            for selector in ("a.apply-link[href]", 'a[data-testid="apply-link"][href]', ".contact a[href]"):
                for link_el in soup.select(selector):
                    candidate = _clean_external_apply_url(str(link_el.get("href") or ""))
                    if candidate:
                        apply_url = candidate
                        break
                if apply_url:
                    break

            return {
                "refnr": cleaned_refnr,
                "authenticated": True,
                "title": title,
                "company": company,
                "description": description,
                "requirements": requirements,
                "company_info": company_info,
                "contact_name": contact_name,
                "contact_email": contact_email,
                "contact_phone": contact_phone,
                "apply_url": apply_url,
                "portal_url": detail_url,
            }

    def fetch_vermittlungsvorschlaege(self) -> list[dict[str, Any]]:
        """Scrape Vermittlungsvorschläge (placement proposals) from the authenticated portal."""
        with self.browser.open_context() as ctx:
            page = ctx.new_page()
            page.goto(PORTAL_VERMITTLUNGSVORSCHLAEGE_URL)
            self.browser.detect_captcha_or_block(page)

            html = str(page.content() or "")
            soup = BeautifulSoup(html, "html.parser")
            proposals: list[dict[str, Any]] = []

            for item_el in soup.select(".vermittlungsvorschlag"):
                refnr = _clean_text(str(item_el.get("data-refnr") or ""))
                if not refnr:
                    ref_span = item_el.select_one(".refnr")
                    if ref_span is not None:
                        refnr = _clean_text(ref_span.get_text(" ", strip=True))
                if not refnr:
                    continue

                title_el = item_el.find(["h2", "h3"]) or item_el.select_one(".title")
                title = _clean_text(title_el.get_text(" ", strip=True)) if title_el else refnr

                company_el = item_el.select_one(".company") or item_el.select_one(".arbeitgeber")
                company = _clean_text(company_el.get_text(" ", strip=True)) if company_el else "Unbekannt"

                proposals.append(
                    {
                        "refnr": refnr,
                        "title": title or refnr,
                        "company": company or "Unbekannt",
                        "portal_url": PORTAL_DETAIL_URL_TEMPLATE.format(refnr=refnr),
                        "is_vermittlungsvorschlag": True,
                    }
                )

            return proposals


__all__ = [
    "CaptchaDetectedError",
    "PortalScraper",
]
