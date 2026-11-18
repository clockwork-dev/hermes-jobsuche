"""Stealth Playwright browser context manager and strict non-bypassing CAPTCHA guard."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import random
import time
from typing import Any

from jobsuche_pkg.config import Settings, get_settings

logger = logging.getLogger(__name__)

DESKTOP_USER_AGENTS: tuple[str, ...] = (
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) "
        "Gecko/20100101 Firefox/133.0"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) "
        "Gecko/20100101 Firefox/133.0"
    ),
)

_CAPTCHA_MARKERS: tuple[str, ...] = (
    "sicherheitsabfrage",
    "bitte bestätigen sie, dass sie ein mensch sind",
    "captcha.arbeitsagentur.de",
    "g-recaptcha",
    "hcaptcha",
    "cf-turnstile",
)


class CaptchaDetectedError(RuntimeError):
    """Raised when a CAPTCHA or anti-bot wall is encountered.

    Never attempts to solve or bypass the challenge; records screenshot and
    status metadata so the operator can re-run in headed mode (`--headed`).
    """

    def __init__(
        self,
        message: str,
        *,
        screenshot_path: Path | str,
        status_info: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.screenshot_path = Path(screenshot_path)
        self.status_info: dict[str, Any] = dict(status_info or {})


class StealthBrowserManager:
    """Manages stealth Playwright browser contexts, session state, and CAPTCHA detection."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        headless: bool | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.settings = settings if settings is not None else get_settings()
        self._headless_override = headless
        self._sleep_fn = sleep_fn
        self._ua_index = 0
        self._active_cm: Any = None

    def next_user_agent(self) -> str:
        """Return the next desktop Linux/Windows User-Agent in rotation."""
        ua = DESKTOP_USER_AGENTS[self._ua_index % len(DESKTOP_USER_AGENTS)]
        self._ua_index += 1
        return ua

    def human_pause(self, min_s: float = 0.4, max_s: float = 1.8) -> float:
        """Sleep for a jittered human-like interval between ``min_s`` and ``max_s``."""
        low = float(min_s)
        high = float(max_s)
        if low < 0.0 or high < 0.0:
            raise ValueError("human_pause bounds must be >= 0.0")
        if low > high:
            raise ValueError("min_s must be <= max_s")
        delay = random.uniform(low, high) if high > low else low
        effective_sleep = self._sleep_fn if self._sleep_fn is not None else time.sleep
        effective_sleep(delay)
        return delay

    @staticmethod
    def _read_valid_storage_state(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        cookies = parsed.get("cookies")
        origins = parsed.get("origins")
        if cookies is not None and not isinstance(cookies, list):
            return None
        if origins is not None and not isinstance(origins, list):
            return None
        return parsed

    def get_auth_status(self) -> dict[str, Any]:
        """Inspect persisted storage_state.json and credential configuration without network I/O."""
        state_path = Path(self.settings.storage_state_path)
        exists = state_path.is_file()
        parsed = self._read_valid_storage_state(state_path)
        cookies = parsed.get("cookies", []) if isinstance(parsed, dict) else []
        origins = parsed.get("origins", []) if isinstance(parsed, dict) else []
        has_valid_session = bool(parsed is not None and (len(cookies) > 0 or len(origins) > 0))

        return {
            "authenticated": has_valid_session,
            "has_session": has_valid_session,
            "session_valid": has_valid_session,
            "storage_state_exists": exists and parsed is not None,
            "storage_state_path": str(state_path),
            "cookie_count": len(cookies) if isinstance(cookies, list) else 0,
            "has_credentials": self.settings.has_ba_credentials(),
            "has_totp": self.settings.has_totp(),
            "has_imap": self.settings.has_imap_config(),
        }

    def save_storage_state(self, context: Any, path: Path | str | None = None) -> Path:
        """Persist browser context cookies and origins to ``storage_state.json``."""
        target = Path(path) if path is not None else Path(self.settings.storage_state_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(target))
        return target

    @contextmanager
    def open_context(
        self,
        *,
        headed: bool | None = None,
        headless: bool | None = None,
    ) -> Iterator[Any]:
        """Open a configured stealth Playwright browser context and clean up on exit."""
        from playwright import sync_api

        if headed is not None:
            effective_headless = not bool(headed)
        elif headless is not None:
            effective_headless = bool(headless)
        elif self._headless_override is not None:
            effective_headless = bool(self._headless_override)
        else:
            effective_headless = bool(self.settings.headless)

        user_agent = self.next_user_agent()
        context_kwargs: dict[str, Any] = {
            "user_agent": user_agent,
            "locale": "de-DE",
            "timezone_id": "Europe/Berlin",
            "viewport": {"width": 1920, "height": 1080},
            "extra_http_headers": {
                "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        }

        state_path = Path(self.settings.storage_state_path)
        if self._read_valid_storage_state(state_path) is not None:
            context_kwargs["storage_state"] = str(state_path)

        with sync_api.sync_playwright() as pw:
            if self.settings.browser_cdp_url:
                browser = pw.chromium.connect_over_cdp(self.settings.browser_cdp_url)
            else:
                launch_kwargs: dict[str, Any] = {"headless": effective_headless}
                if self.settings.browser_executable_path:
                    launch_kwargs["executable_path"] = self.settings.browser_executable_path
                browser = pw.chromium.launch(**launch_kwargs)

            context = browser.new_context(**context_kwargs)
            try:
                yield context
            finally:
                try:
                    context.close()
                finally:
                    browser.close()

    new_context = open_context
    context = open_context
    browser_context = open_context
    session = open_context

    def __enter__(self) -> Any:
        self._active_cm = self.open_context()
        return self._active_cm.__enter__()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._active_cm is not None:
            try:
                self._active_cm.__exit__(exc_type, exc_val, exc_tb)
            finally:
                self._active_cm = None

    def detect_captcha_or_block(
        self,
        page: Any,
        *,
        notify_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any] | None:
        """Inspect ``page`` for CAPTCHA / Sicherheitsabfrage challenges.

        Never attempts to solve, click, or bypass any challenge. When detected,
        captures a PNG screenshot, writes a status JSON report proposing headed
        mode (`--headed`), optionally notifies the caller, and raises
        :class:`CaptchaDetectedError`.
        """
        title_text = ""
        html_text = ""
        page_url = str(getattr(page, "url", "") or "")

        try:
            raw_title = page.title() if callable(getattr(page, "title", None)) else ""
            title_text = str(raw_title or "")
        except Exception:
            title_text = ""

        try:
            raw_html = page.content() if callable(getattr(page, "content", None)) else ""
            html_text = str(raw_html or "")
        except Exception:
            html_text = ""

        combined_lower = f"{title_text}\n{html_text}\n{page_url}".lower()
        detected = any(marker in combined_lower for marker in _CAPTCHA_MARKERS)

        if not detected:
            return None

        screenshot_dir = Path(self.settings.screenshot_dir)
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        ts_slug = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        screenshot_path = screenshot_dir / f"captcha_{ts_slug}_{ int(time.time() * 1000) % 100000 }.png"

        img_bytes = page.screenshot(path=str(screenshot_path))
        if not screenshot_path.is_file() and isinstance(img_bytes, (bytes, bytearray)):
            screenshot_path.write_bytes(bytes(img_bytes))

        status_json_path = screenshot_dir / "captcha_status.json"
        status_info: dict[str, Any] = {
            "captcha": True,
            "captcha_detected": True,
            "authenticated": False,
            "ok": False,
            "url": page_url,
            "title": title_text,
            "screenshot_path": str(screenshot_path),
            "status_json_path": str(status_json_path),
            "headed_fallback": True,
            "recommended_action": "Run `jobsuche login --headed` to resolve the challenge manually in a visible browser.",
            "message": (
                f"CAPTCHA / Sicherheitsabfrage detected at {page_url or 'portal'}. "
                f"Screenshot saved to {screenshot_path}. Switch to headed mode (`--headed`) for manual resolution."
            ),
        }
        status_json_path.write_text(
            json.dumps(status_info, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        if notify_callback is not None:
            notify_callback(status_info)

        raise CaptchaDetectedError(
            status_info["message"],
            screenshot_path=screenshot_path,
            status_info=status_info,
        )
