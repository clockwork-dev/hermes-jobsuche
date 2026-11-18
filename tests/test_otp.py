"""Unit and integration tests for M2 (`auth_otp`, `browser`, `portal_scraper`)."""

from __future__ import annotations

import email.message
import imaplib
import json
from pathlib import Path
import re
from typing import Any

import pyotp
import pytest

from jobsuche_pkg.auth_otp import (
    OTPConfigError,
    OTPError,
    fetch_otp_via_imap,
    generate_totp,
    get_otp_code,
)
from jobsuche_pkg.browser import CaptchaDetectedError, StealthBrowserManager
from jobsuche_pkg.config import Settings, get_settings
from jobsuche_pkg.portal_scraper import PortalScraper

TEST_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def test_generate_totp_safe_window_and_freshness_wait() -> None:
    sleeps: list[float] = []
    # Safe window: 1700000020 -> 20s into 30s bucket -> 10s remaining
    code_safe, rem_safe = generate_totp(
        TEST_SECRET,
        min_remaining_seconds=3.0,
        now=1700000020.0,
        sleep_fn=sleeps.append,
    )
    assert re.fullmatch(r"\d{6}", code_safe)
    assert code_safe == pyotp.TOTP(TEST_SECRET).at(1700000020)
    assert rem_safe == pytest.approx(20.0, abs=0.5)
    assert sleeps == []

    # Rollover window: 1700000038 -> 2s remaining (< 3.0s) -> sleeps until 1700000040
    clock = {"ts": 1700000038.0}

    def fake_sleep(sec: float) -> None:
        sleeps.append(sec)
        clock["ts"] += sec

    code_next, rem_next = generate_totp(
        TEST_SECRET,
        min_remaining_seconds=3.0,
        now=clock["ts"],
        sleep_fn=fake_sleep,
    )
    assert len(sleeps) >= 1
    assert sum(sleeps) >= 2.0
    assert code_next == pyotp.TOTP(TEST_SECRET).at(1700000040)
    assert rem_next >= 25.0


def test_generate_totp_rejects_invalid_secret() -> None:
    with pytest.raises((OTPConfigError, OTPError, ValueError)):
        generate_totp("   ")
    with pytest.raises((OTPConfigError, OTPError, ValueError)):
        generate_totp("NOT_BASE32_SECRET!!!")


def test_imap_and_cli_callback_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    msg = email.message.EmailMessage()
    msg["Subject"] = "Ihr Sicherheitscode"
    msg["From"] = "noreply@arbeitsagentur.de"
    msg["To"] = "fisi@example.de"
    msg.set_content("Ihr 6-stelliger Sicherheitscode lautet: 849201\n")
    raw_bytes = msg.as_bytes()

    class DummyIMAP:
        def __init__(self, host: str, port: int = 993, timeout: float | None = None) -> None:
            self.host = host
            self.port = port

        def __enter__(self) -> DummyIMAP:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
            return ("OK", [b"Logged in"])

        def select(self, folder: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
            return ("OK", [b"1"])

        def search(self, charset: str | None, *criteria: str) -> tuple[str, list[bytes]]:
            return ("OK", [b"1"])

        def fetch(self, message_set: Any, parts: str) -> tuple[str, list[Any]]:
            return ("OK", [(b"1 (RFC822)", raw_bytes)])

        def close(self) -> tuple[str, list[bytes]]:
            return ("OK", [b"Closed"])

        def logout(self) -> tuple[str, list[bytes]]:
            return ("BYE", [b"Bye"])

    monkeypatch.setattr(imaplib, "IMAP4_SSL", DummyIMAP)

    code_imap = fetch_otp_via_imap(
        host="imap.example.de",
        port=993,
        username="fisi@example.de",
        password="secret",
    )
    assert code_imap == "849201"

    settings_cb = Settings(
        ba_totp_secret=None,
        ba_imap_host=None,
        db_path=tmp_path / "jobsuche.db",
        export_dir=tmp_path / "exports",
    )
    assert get_otp_code(settings_cb, cli_callback=lambda: "192837") == "192837"

    with pytest.raises((OTPConfigError, OTPError)):
        get_otp_code(settings_cb)


def test_stealth_browser_profile_and_captcha_guard(tmp_path: Path) -> None:
    settings = Settings(
        storage_state_path=tmp_path / "data" / "storage_state.json",
        screenshot_dir=tmp_path / "data" / "screenshots",
    )
    mgr = StealthBrowserManager(settings)
    status_before = mgr.get_auth_status()
    assert isinstance(status_before, dict)

    settings.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    settings.storage_state_path.write_text(
        json.dumps({"cookies": [{"name": "ba_session", "value": "abc"}], "origins": []}),
        encoding="utf-8",
    )
    status_after = mgr.get_auth_status()
    assert any(status_after.get(k) is True for k in ("has_session", "authenticated", "storage_state_exists", "session_valid"))

    class FakeCaptchaPage:
        url = "https://www.arbeitsagentur.de/login"

        def title(self) -> str:
            return "Sicherheitsabfrage — Bundesagentur für Arbeit"

        def content(self) -> str:
            return "<html><body><h1>Sicherheitsabfrage</h1><p>Bitte bestätigen Sie, dass Sie ein Mensch sind.</p></body></html>"

        def screenshot(self, *, path: str | Path | None = None, **kwargs: Any) -> bytes:
            data = b"\x89PNG\r\n\x1a\n"
            if path is not None:
                p = Path(path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            return data

    with pytest.raises(CaptchaDetectedError) as exc_info:
        mgr.detect_captcha_or_block(FakeCaptchaPage())
    assert exc_info.value.screenshot_path.is_file()
    assert isinstance(exc_info.value.status_info, dict)
