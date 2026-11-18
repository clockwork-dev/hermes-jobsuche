"""6-digit TOTP generator and fallback OTP retrieval (IMAP + CLI callback)."""

from __future__ import annotations

import base64
import binascii
import email
from email.header import decode_header
from email.message import Message
import imaplib
import re
import time
from collections.abc import Callable
from typing import Any

import pyotp

from jobsuche_pkg.config import Settings

_SIX_DIGIT_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_BASE32_CLEAN_RE = re.compile(r"^[A-Z2-7]+=*$")


class OTPError(RuntimeError):
    """Raised when an OTP code cannot be generated, fetched, or validated."""


class OTPConfigError(OTPError, ValueError):
    """Raised when OTP configuration (TOTP secret, IMAP settings, callback) is invalid or missing."""


def _normalize_and_validate_base32(secret: str) -> str:
    if not isinstance(secret, str) or not secret.strip():
        raise OTPConfigError("TOTP secret is missing or empty")

    cleaned = re.sub(r"\s+", "", secret.strip()).upper()
    if not cleaned or not _BASE32_CLEAN_RE.match(cleaned):
        raise OTPConfigError("Invalid Base32 TOTP secret")

    unpadded = cleaned.rstrip("=")
    if not unpadded:
        raise OTPConfigError("Invalid Base32 TOTP secret")

    pad_len = (8 - (len(unpadded) % 8)) % 8
    padded = unpadded + ("=" * pad_len)
    try:
        decoded = base64.b32decode(padded, casefold=True)
    except (binascii.Error, ValueError) as exc:
        raise OTPConfigError("Invalid Base32 TOTP secret") from exc

    if not decoded:
        raise OTPConfigError("Invalid Base32 TOTP secret")
    return unpadded


def _resolve_timestamp(now: float | Callable[[], float] | None) -> float:
    if now is None:
        return float(time.time())
    if callable(now):
        return float(now())
    return float(now)


def generate_totp(
    secret: str,
    *,
    min_remaining_seconds: float = 3.0,
    now: float | Callable[[], float] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    interval: int = 30,
) -> tuple[str, float]:
    """Generate a 6-digit TOTP code and remaining validity window in seconds.

    If the remaining window in the current 30-second tick is strictly less than
    ``min_remaining_seconds``, waits until the next 30-second boundary before
    computing the code so portal submission does not race an expiring code.
    """
    normalized_secret = _normalize_and_validate_base32(secret)
    if interval <= 0:
        raise OTPConfigError("TOTP interval must be > 0")

    current_ts = _resolve_timestamp(now)
    period = float(interval)
    elapsed = current_ts % period
    remaining = period - elapsed

    if min_remaining_seconds > 0 and remaining < float(min_remaining_seconds):
        wait_seconds = max(0.0, remaining) + 0.05
        effective_sleep = sleep_fn if sleep_fn is not None else time.sleep
        effective_sleep(wait_seconds)
        if now is None:
            new_ts = float(time.time())
            current_ts = new_ts if new_ts >= current_ts + wait_seconds - 1e-6 else current_ts + wait_seconds
        elif callable(now):
            new_ts = float(now())
            current_ts = new_ts if new_ts >= current_ts + wait_seconds - 1e-6 else current_ts + wait_seconds
        else:
            current_ts = current_ts + wait_seconds
        elapsed = current_ts % period
        remaining = period - elapsed

    totp = pyotp.TOTP(normalized_secret, digits=6, interval=interval)
    code = totp.at(int(current_ts))
    if not _SIX_DIGIT_RE.fullmatch(code):
        raise OTPError(f"Generated TOTP code is not 6 digits: {code!r}")
    return code, remaining


def _decode_mime_header(raw_header: str | None) -> str:
    if not raw_header:
        return ""
    parts: list[str] = []
    for chunk, charset in decode_header(raw_header):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(str(chunk))
    return "".join(parts)


def _extract_message_text(msg: Message) -> str:
    chunks: list[str] = []
    subject = _decode_mime_header(msg.get("Subject"))
    if subject:
        chunks.append(subject)

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                charset = part.get_content_charset() or "utf-8"
                chunks.append(payload.decode(charset, errors="replace"))
            elif isinstance(payload, str):
                chunks.append(payload)
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = msg.get_content_charset() or "utf-8"
            chunks.append(payload.decode(charset, errors="replace"))
        elif isinstance(payload, str):
            chunks.append(payload)

    return "\n".join(chunks)


def fetch_otp_via_imap(
    host: str | Settings | None = None,
    port: int = 993,
    username: str | None = None,
    password: str | None = None,
    *,
    user: str | None = None,
    folder: str = "INBOX",
    settings: Settings | None = None,
    timeout: float = 15.0,
) -> str:
    """Fetch the most recent 6-digit OTP security code from an IMAP mailbox."""
    if isinstance(host, Settings) or (host is not None and hasattr(host, "ba_imap_host")):
        settings = host  # type: ignore[assignment]
        host = None

    resolved_host = host or (settings.ba_imap_host if settings is not None else None)
    resolved_port = int(port or (settings.ba_imap_port if settings is not None else 993))
    resolved_user = username or user or (settings.ba_imap_user if settings is not None else None)
    resolved_password = password or (settings.ba_imap_password if settings is not None else None)
    resolved_folder = folder or (settings.ba_imap_folder if settings is not None else "INBOX")

    if not (resolved_host and resolved_user and resolved_password):
        raise OTPConfigError("IMAP host, username, and password are required for IMAP OTP fallback")

    try:
        with imaplib.IMAP4_SSL(resolved_host, port=resolved_port, timeout=timeout) as client:
            client.login(resolved_user, resolved_password)
            client.select(resolved_folder, readonly=True)
            status, search_data = client.search(None, "ALL")
            if status != "OK" or not search_data or not search_data[0]:
                raise OTPError("No messages found in IMAP mailbox for OTP extraction")

            raw_ids = search_data[0]
            id_list = (
                raw_ids.decode("ascii", errors="ignore").split()
                if isinstance(raw_ids, bytes)
                else str(raw_ids).split()
            )
            if not id_list:
                raise OTPError("IMAP mailbox is empty; no OTP email found")

            for msg_id in reversed(id_list):
                fetch_status, msg_parts = client.fetch(msg_id, "(RFC822)")
                if fetch_status != "OK" or not msg_parts:
                    continue
                for part in msg_parts:
                    if not isinstance(part, tuple) or len(part) < 2:
                        continue
                    raw_email = part[1]
                    if not isinstance(raw_email, (bytes, bytearray)):
                        continue
                    parsed_msg = email.message_from_bytes(bytes(raw_email))
                    text_blob = _extract_message_text(parsed_msg)
                    match = _SIX_DIGIT_RE.search(text_blob)
                    if match:
                        return match.group(1)
    except OTPError:
        raise
    except Exception as exc:
        raise OTPError(f"Failed to retrieve OTP via IMAP: {exc}") from exc

    raise OTPError("No 6-digit OTP code found in IMAP messages")


def get_otp_code(
    settings: Settings | None = None,
    *,
    cli_callback: Callable[[], str] | None = None,
    otp_callback: Callable[[], str] | None = None,
    min_remaining_seconds: float = 3.0,
    now: float | Callable[[], float] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> str:
    """Resolve a 6-digit OTP code via fallback chain: TOTP -> IMAP -> CLI callback."""
    callback = cli_callback or otp_callback
    errors: list[Exception] = []

    if settings is not None and settings.ba_totp_secret:
        try:
            code, _ = generate_totp(
                settings.ba_totp_secret,
                min_remaining_seconds=min_remaining_seconds,
                now=now,
                sleep_fn=sleep_fn,
            )
            return code
        except OTPError as exc:
            errors.append(exc)
            if not (settings.has_imap_config() or callback is not None):
                raise

    if settings is not None and settings.has_imap_config():
        try:
            return fetch_otp_via_imap(settings)
        except OTPError as exc:
            errors.append(exc)
            if callback is None:
                raise

    if callback is not None:
        raw_value: Any = callback()
        if not isinstance(raw_value, str):
            raise OTPError("OTP callback did not return a string")
        cleaned_code = raw_value.strip()
        if not _SIX_DIGIT_RE.fullmatch(cleaned_code):
            raise OTPError(f"OTP callback returned invalid 6-digit code: {raw_value!r}")
        return cleaned_code

    if errors:
        raise errors[-1]
    raise OTPConfigError(
        "No OTP source configured: provide BA_TOTP_SECRET, BA_IMAP_* settings, or cli_callback"
    )
