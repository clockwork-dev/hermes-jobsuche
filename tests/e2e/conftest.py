from __future__ import annotations

import asyncio
import base64
import copy
import email.message
import http.client
import http.server
import imaplib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from types import ModuleType
from typing import TYPE_CHECKING, Any, Callable
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT_STR = str(PROJECT_ROOT)
if _REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, _REPO_ROOT_STR)
_HOME_DIR = Path.home()
ANTIGRAVITY_PLUGIN_DIR = PROJECT_ROOT / ".agents" / "plugins" / "jobsuche"
HERMES_PLUGIN_DIR = (
    _HOME_DIR / ".hermes" / "plugins" / "jobsuche"
    if (_HOME_DIR / ".hermes" / "plugins" / "jobsuche").is_dir()
    else PROJECT_ROOT / "plugins" / "hermes" / "jobsuche"
)
GEMINI_CONFIG_DIR = _HOME_DIR / ".gemini" / "config"
HERMES_CONFIG_PATH = _HOME_DIR / ".hermes" / "config.yaml"
HERMES_AGENT_DIR = _HOME_DIR / ".hermes" / "hermes-agent"
VALID_TEST_TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def _resolve_hermes_python() -> Path:
    launcher = HERMES_AGENT_DIR / ".hermes" / "bin" / "hermes"
    if launcher.is_file():
        try:
            text = launcher.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"^\s*exec\s+(\S+)", text, re.MULTILINE)
            if m:
                candidate = Path(m.group(1).strip("\"'"))
                if candidate.is_file():
                    return candidate
        except OSError:
            pass
    tools_dir = _HOME_DIR / ".hermes" / "tools"
    if tools_dir.is_dir():
        for candidate in sorted(tools_dir.glob("python-*/bin/python3"), reverse=True):
            if candidate.is_file():
                return candidate
    return _HOME_DIR / ".hermes" / "tools" / "python-3.14.9+20261021-linux-x64" / "bin" / "python3"


HERMES_PYTHON = _resolve_hermes_python()
HERMES_VENV_PYTHON = HERMES_PYTHON

_PNG_SIGNATURE_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

_ENV_PREFIXES_TO_CLEAR = ("BA_", "JOBSUCHE_", "IMAP_", "BROWSER_")
_ENV_EXACT_TO_CLEAR = ("API_BASE_URL",)


def require_module(dotted_name: str) -> ModuleType:
    """Lazy-import a module at test execution time; fails honestly with ModuleNotFoundError."""
    return importlib.import_module(dotted_name)


@dataclass(frozen=True)
class E2EWorkspace:
    root: Path
    data_dir: Path
    db_path: Path
    export_dir: Path
    storage_state_path: Path
    screenshot_dir: Path
    env_file: Path


@pytest.fixture
def e2e_workspace(tmp_path: Path) -> E2EWorkspace:
    return E2EWorkspace(
        root=tmp_path,
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "jobsuche.db",
        export_dir=tmp_path / "exports",
        storage_state_path=tmp_path / "data" / "storage_state.json",
        screenshot_dir=tmp_path / "data" / "screenshots",
        env_file=tmp_path / ".env",
    )


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, e2e_workspace: E2EWorkspace) -> E2EWorkspace:
    for key in list(os.environ.keys()):
        if key.startswith(_ENV_PREFIXES_TO_CLEAR) or key in _ENV_EXACT_TO_CLEAR:
            monkeypatch.delenv(key, raising=False)

    monkeypatch.chdir(e2e_workspace.root)
    monkeypatch.setenv("JOBSUCHE_DB_PATH", str(e2e_workspace.db_path))
    monkeypatch.setenv("JOBSUCHE_EXPORT_DIR", str(e2e_workspace.export_dir))
    monkeypatch.setenv("JOBSUCHE_STORAGE_STATE", str(e2e_workspace.storage_state_path))
    monkeypatch.setenv("JOBSUCHE_SCREENSHOT_DIR", str(e2e_workspace.screenshot_dir))
    return e2e_workspace


def build_clean_subprocess_env(
    workspace: E2EWorkspace,
    *,
    api_base_url: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_ENV_PREFIXES_TO_CLEAR) and k not in _ENV_EXACT_TO_CLEAR
    }
    env["JOBSUCHE_DB_PATH"] = str(workspace.db_path)
    env["JOBSUCHE_EXPORT_DIR"] = str(workspace.export_dir)
    env["JOBSUCHE_STORAGE_STATE"] = str(workspace.storage_state_path)
    env["JOBSUCHE_SCREENSHOT_DIR"] = str(workspace.screenshot_dir)
    if api_base_url is not None:
        env["JOBSUCHE_API_BASE_URL"] = api_base_url
        env["API_BASE_URL"] = api_base_url
    if extra:
        env.update(extra)
    return env


def resolve_cli_command() -> list[str]:
    venv_jobsuche = Path(sys.executable).parent / "jobsuche"
    if venv_jobsuche.is_file():
        return [str(venv_jobsuche)]
    project_venv_jobsuche = PROJECT_ROOT / ".venv" / "bin" / "jobsuche"
    if project_venv_jobsuche.is_file():
        return [str(project_venv_jobsuche)]
    return [sys.executable, "-m", "jobsuche_pkg.cli"]


def run_cli(
    args: list[str],
    *,
    workspace: E2EWorkspace,
    api_base_url: str | None = None,
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float = 20.0,
) -> subprocess.CompletedProcess[str]:
    cmd = [*resolve_cli_command(), *args]
    env = build_clean_subprocess_env(workspace, api_base_url=api_base_url, extra=extra_env)
    return subprocess.run(
        cmd,
        cwd=str(cwd or workspace.root),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Lazy module fixtures (execute-time only; never skipped on ImportError)
# ---------------------------------------------------------------------------


@pytest.fixture
def pkg_config() -> ModuleType:
    return require_module("jobsuche_pkg.config")


@pytest.fixture
def pkg_models() -> ModuleType:
    return require_module("jobsuche_pkg.models")


@pytest.fixture
def pkg_db() -> ModuleType:
    return require_module("jobsuche_pkg.db")


@pytest.fixture
def pkg_api_client() -> ModuleType:
    return require_module("jobsuche_pkg.api_client")


@pytest.fixture
def pkg_auth_otp() -> ModuleType:
    return require_module("jobsuche_pkg.auth_otp")


@pytest.fixture
def pkg_browser() -> ModuleType:
    return require_module("jobsuche_pkg.browser")


@pytest.fixture
def pkg_portal_scraper() -> ModuleType:
    return require_module("jobsuche_pkg.portal_scraper")


@pytest.fixture
def pkg_sorter() -> ModuleType:
    return require_module("jobsuche_pkg.sorter")


@pytest.fixture
def pkg_exporter() -> ModuleType:
    return require_module("jobsuche_pkg.exporter")


@pytest.fixture
def pkg_cli() -> ModuleType:
    return require_module("jobsuche_pkg.cli")


@pytest.fixture
def settings_without_creds(isolated_env: E2EWorkspace, pkg_config: ModuleType) -> Any:
    return pkg_config.get_settings(env_file=isolated_env.env_file)


@pytest.fixture
def settings_with_creds(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: E2EWorkspace,
    pkg_config: ModuleType,
) -> Any:
    monkeypatch.setenv("BA_USERNAME", "fisi.test@example.de")
    monkeypatch.setenv("BA_PASSWORD", "SyntheticPassword!2026")
    monkeypatch.setenv("BA_TOTP_SECRET", VALID_TEST_TOTP_SECRET)
    monkeypatch.setenv("BA_IMAP_HOST", "imap.example.de")
    monkeypatch.setenv("BA_IMAP_PORT", "993")
    monkeypatch.setenv("BA_IMAP_USER", "fisi.test@example.de")
    monkeypatch.setenv("BA_IMAP_PASSWORD", "imap-secret")
    return pkg_config.get_settings(env_file=isolated_env.env_file)


@pytest.fixture
def job_db(isolated_env: E2EWorkspace, pkg_db: ModuleType) -> Iterator[Any]:
    with pkg_db.JobDatabase(isolated_env.db_path) as db:
        yield db


# ---------------------------------------------------------------------------
# Layer 2: Synthetic Jobsuche REST Payloads (v6, v4, jobdetails)
# ---------------------------------------------------------------------------


def make_v6_jobs_pages() -> dict[int, dict[str, Any]]:
    return {
        1: {
            "maxErgebnisse": 3,
            "page": 1,
            "size": 2,
            "woOutput": {
                "bereinigterOrt": "Aachen",
                "suchmodus": "UMKREISSUCHE",
                "koordinaten": [{"lat": 50.7711907, "lon": 6.0957494}],
            },
            "facetten": {"befristung": {}, "externestellenboersen": {}},
            "ergebnisliste": [
                {
                    "referenznummer": "10000-1206415041-S",
                    "stellenangebotsTitel": "Ausbildung Fachinformatiker/in - Systemintegration (m/w/d) 2026",
                    "hauptberuf": "Fachinformatiker/in - Systemintegration",
                    "stellenangebotsart": "AUSBILDUNG",
                    "ausbildungsart": "AUSBILDUNG",
                    "firma": "Müller & Söhne IT-Systemhaus GmbH",
                    "stellenlokationen": [
                        {
                            "adresse": {
                                "strasse": "Theaterstraße",
                                "hausnummer": "12",
                                "plz": "52062",
                                "ort": "Aachen",
                                "region": "Nordrhein-Westfalen",
                                "land": "Deutschland",
                            },
                            "breite": 50.775,
                            "laenge": 6.084,
                        }
                    ],
                    "entfernung": 2,
                    "eintrittszeitraum": {"von": "2026-08-01"},
                    "datumErsteVeroeffentlichung": "2026-02-01",
                    "aenderungsdatum": "2026-02-10T09:00:00",
                    "ausbildungsverguetungJahr1": 1150,
                    "homeofficemoeglich": True,
                },
                {
                    "referenznummer": "10000-1206415999-S",
                    "stellenangebotsTitel": "Azubi FISI — Linux & Netzwerkbetrieb",
                    "hauptberuf": "Fachinformatiker/in - Systemintegration",
                    "stellenangebotsart": "AUSBILDUNG",
                    "ausbildungsart": "AUSBILDUNG",
                    "firma": "Müller & Söhne IT-Systemhaus GmbH",
                    "stellenlokationen": [
                        {
                            "adresse": {
                                "strasse": "Kaiserstraße",
                                "hausnummer": "5",
                                "plz": "52146",
                                "ort": "Würselen",
                                "region": "Nordrhein-Westfalen",
                                "land": "Deutschland",
                            }
                        }
                    ],
                    "entfernung": 9,
                    "eintrittszeitraum": {"von": "2026-09-01"},
                    "datumErsteVeroeffentlichung": "2026-02-03",
                    "aenderungsdatum": "2026-02-11T10:30:00",
                    "externeURL": "https://karriere.mueller-it.example/fisi-2026",
                },
            ],
        },
        2: {
            "maxErgebnisse": 3,
            "page": 2,
            "size": 2,
            "woOutput": {
                "bereinigterOrt": "Aachen",
                "suchmodus": "UMKREISSUCHE",
                "koordinaten": [{"lat": 50.7711907, "lon": 6.0957494}],
            },
            "facetten": {"befristung": {}, "externestellenboersen": {}},
            "ergebnisliste": [
                {
                    "referenznummer": "10000-1206420001-S",
                    "stellenangebotsTitel": "Ausbildung Systemintegration (Cloud & Proxmox)",
                    "hauptberuf": "Fachinformatiker/in - Systemintegration",
                    "stellenangebotsart": "AUSBILDUNG",
                    "ausbildungsart": "AUSBILDUNG",
                    "firma": "Aachener Rechenzentrum AG",
                    "stellenlokationen": [
                        {
                            "adresse": {
                                "plz": "52074",
                                "ort": "Aachen",
                                "region": "Nordrhein-Westfalen",
                                "land": "Deutschland",
                            }
                        }
                    ],
                    "entfernung": 4,
                    "eintrittszeitraum": {"von": "2026-08-15"},
                    "datumErsteVeroeffentlichung": "2026-02-05",
                    "aenderungsdatum": "2026-02-12T08:15:00",
                }
            ],
        },
    }


def make_v4_jobs_payload() -> dict[str, Any]:
    return {
        "maxErgebnisse": 1,
        "page": 1,
        "size": 25,
        "stellenangebote": [
            {
                "refnr": "10000-9900011122-S",
                "titel": "Fachinformatiker Systemintegration (Legacy v4)",
                "beruf": "Fachinformatiker/in - Systemintegration",
                "arbeitgeber": "Rheinland Cloud KG",
                "arbeitsort": {
                    "plz": "52064",
                    "ort": "Aachen",
                    "strasse": "Krefelder Str. 10",
                    "region": "Nordrhein-Westfalen",
                    "entfernung": 3,
                },
                "eintrittsdatum": "2026-08-01",
                "aktuelleVeroeffentlichungsdatum": "2026-02-01",
                "modifikationsTimestamp": "2026-02-09T12:00:00",
            }
        ],
    }


def encode_refnr_b64(refnr: str) -> str:
    return base64.b64encode(refnr.encode("utf-8")).decode("ascii")


def make_jobdetails_payloads() -> dict[str, dict[str, Any]]:
    details_by_refnr: dict[str, dict[str, Any]] = {
        "10000-1206415041-S": {
            "referenznummer": "10000-1206415041-S",
            "stellenangebotsTitel": "Ausbildung Fachinformatiker/in - Systemintegration (m/w/d) 2026",
            "firma": "Müller & Söhne IT-Systemhaus GmbH",
            "stellenangebotsBeschreibung": (
                "Du betreust Linux (Debian), Windows Server, Active Directory, "
                "Virtualisierung mit Proxmox und Docker, sowie Netzwerk/Firewall und IT-Support."
            ),
            "geforderterBildungsabschluss": "Mittlere Reife / Fachabitur",
            "allianzpartnerName": "Azubiyo",
            "allianzpartnerUrl": "https://partner.example/apply/1206415041",
            "eintrittszeitraum": {"von": "2026-08-01"},
        },
        "10000-1206415999-S": {
            "referenznummer": "10000-1206415999-S",
            "stellenangebotsTitel": "Azubi FISI — Linux & Netzwerkbetrieb",
            "firma": "Müller & Söhne IT-Systemhaus GmbH",
            "stellenangebotsBeschreibung": (
                "Praxisnahe Ausbildung im Bereich Linux, Scripting, Firewall und Netzwerk."
            ),
            "geforderterBildungsabschluss": "Fachhochschulreife",
            "allianzpartnerName": "Ausbildung.de",
            "allianzpartnerUrl": "https://karriere.mueller-it.example/fisi-2026",
            "eintrittszeitraum": {"von": "2026-09-01"},
        },
        "10000-1206420001-S": {
            "referenznummer": "10000-1206420001-S",
            "stellenangebotsTitel": "Ausbildung Systemintegration (Cloud & Proxmox)",
            "firma": "Aachener Rechenzentrum AG",
            "stellenangebotsBeschreibung": (
                "Betrieb hochverfügbarer Cluster mit Proxmox, Docker, Debian Linux und Firewall-Systemen."
            ),
            "geforderterBildungsabschluss": "Abitur / Fachabitur",
            "allianzpartnerName": "CareerPortal",
            "allianzpartnerUrl": "https://arz.example/apply/1206420001",
            "eintrittszeitraum": {"von": "2026-08-15"},
        },
        "10000-9900011122-S": {
            "referenznummer": "10000-9900011122-S",
            "stellenangebotsTitel": "Fachinformatiker Systemintegration (Legacy v4)",
            "firma": "Rheinland Cloud KG",
            "stellenangebotsBeschreibung": "Cloud-Infrastruktur, Linux, Netzwerk und IT-Support.",
            "geforderterBildungsabschluss": "Mittlere Reife",
            "allianzpartnerUrl": "https://rheinland-cloud.example/apply",
            "eintrittszeitraum": {"von": "2026-08-01"},
        },
    }
    return {encode_refnr_b64(refnr): payload for refnr, payload in details_by_refnr.items()}


@pytest.fixture
def sample_v6_jobs_pages() -> dict[int, dict[str, Any]]:
    return copy.deepcopy(make_v6_jobs_pages())


@pytest.fixture
def sample_v4_jobs_payload() -> dict[str, Any]:
    return copy.deepcopy(make_v4_jobs_payload())


@pytest.fixture
def sample_jobdetails_payloads() -> dict[str, dict[str, Any]]:
    return copy.deepcopy(make_jobdetails_payloads())


@pytest.fixture
def sample_fisi_offers(pkg_models: ModuleType) -> list[Any]:
    AusbildungOffer = pkg_models.AusbildungOffer
    return [
        AusbildungOffer(
            refnr="10000-1206415041-S",
            title="Ausbildung Fachinformatiker/in - Systemintegration (m/w/d) 2026",
            profession="Fachinformatiker/in - Systemintegration",
            company="Müller & Söhne IT-Systemhaus GmbH",
            plz="52062",
            city="Aachen",
            street="Theaterstraße 12",
            region="Nordrhein-Westfalen",
            distance_km=2.0,
            start_date="2026-08-01",
            published_date="2026-02-01",
            education_required="Mittlere Reife / Fachabitur",
            description=(
                "Du betreust Linux (Debian), Windows Server, Active Directory, "
                "Virtualisierung mit Proxmox und Docker, sowie Netzwerk/Firewall und IT-Support."
            ),
            requirements="Interesse an Linux, Netzwerktechnik, Scripting und IT-Support.",
            company_info="Mittelständisches IT-Systemhaus in Aachen.",
            contact_name="Frau Anna Weber",
            contact_email="karriere@mueller-it.example",
            contact_phone="+49 241 123456",
            apply_url="https://partner.example/apply/1206415041",
        ),
        AusbildungOffer(
            refnr="10000-1206415999-S",
            title="Azubi FISI — Linux & Netzwerkbetrieb",
            profession="Fachinformatiker/in - Systemintegration",
            company="Müller & Söhne IT-Systemhaus GmbH",
            plz="52146",
            city="Würselen",
            street="Kaiserstraße 5",
            region="Nordrhein-Westfalen",
            distance_km=9.0,
            start_date="2026-09-01",
            published_date="2026-02-03",
            description="Linux, Scripting, Firewall und Netzwerkbetrieb.",
            apply_url="https://karriere.mueller-it.example/fisi-2026",
        ),
        AusbildungOffer(
            refnr="10000-1206420001-S",
            title="Ausbildung Systemintegration (Cloud & Proxmox)",
            profession="Fachinformatiker/in - Systemintegration",
            company="Aachener Rechenzentrum AG",
            plz="52074",
            city="Aachen",
            region="Nordrhein-Westfalen",
            distance_km=4.0,
            start_date="2026-08-15",
            published_date="2026-02-05",
            description="Proxmox, Docker, Debian Linux, Firewall.",
            apply_url="https://arz.example/apply/1206420001",
        ),
    ]


# ---------------------------------------------------------------------------
# Layer 3: HTTP Transport Mock & Loopback HTTP Server
# ---------------------------------------------------------------------------


@dataclass
class RecordedRequest:
    method: str
    url: str
    headers: dict[str, str]
    query: dict[str, list[str]]


class FakeJobsucheRouter:
    def __init__(
        self,
        v6_pages: dict[int, dict[str, Any]],
        v4_payload: dict[str, Any],
        jobdetails: dict[str, dict[str, Any]],
    ) -> None:
        self.v6_pages = v6_pages
        self.v4_payload = v4_payload
        self.jobdetails = jobdetails
        self.requests: list[RecordedRequest] = []
        self.status_overrides: dict[str, int] = {}
        self.simulate_timeout: bool = False
        self.serve_v4_on_search: bool = False
        self.malformed_body: bytes | None = None
        self.custom_search_payload: dict[str, Any] | None = None

    def handle(self, method: str, url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        norm_headers = {str(k).lower(): str(v) for k, v in headers.items()}
        self.requests.append(RecordedRequest(method=method.upper(), url=url, headers=norm_headers, query=query))

        if self.simulate_timeout:
            raise TimeoutError("Simulated Jobsuche API timeout")

        if self.malformed_body is not None:
            return (502, {"Content-Type": "text/html; charset=utf-8"}, self.malformed_body)

        path = parsed.path.rstrip("/")

        if path.endswith("/pc/v6/jobs"):
            if "search" in self.status_overrides:
                code = self.status_overrides["search"]
                return (
                    code,
                    {"Content-Type": "application/json"},
                    json.dumps({"message": f"HTTP {code} override"}).encode("utf-8"),
                )
            if self.custom_search_payload is not None:
                return (
                    200,
                    {"Content-Type": "application/json"},
                    json.dumps(self.custom_search_payload).encode("utf-8"),
                )
            was = (query.get("was") or [""])[0]
            if was == "__NONE__":
                empty_payload = {
                    "maxErgebnisse": 0,
                    "page": 1,
                    "size": int((query.get("size") or ["25"])[0]),
                    "ergebnisliste": [],
                }
                return (200, {"Content-Type": "application/json"}, json.dumps(empty_payload).encode("utf-8"))

            page_num = int((query.get("page") or ["1"])[0])
            payload = self.v6_pages.get(
                page_num,
                {
                    "maxErgebnisse": self.v6_pages.get(1, {}).get("maxErgebnisse", 0),
                    "page": page_num,
                    "size": int((query.get("size") or ["25"])[0]),
                    "ergebnisliste": [],
                },
            )
            return (200, {"Content-Type": "application/json"}, json.dumps(payload).encode("utf-8"))

        if path.endswith("/pc/v4/jobs"):
            if "search_v4" in self.status_overrides:
                code = self.status_overrides["search_v4"]
                return (
                    code,
                    {"Content-Type": "application/json"},
                    json.dumps({"message": f"HTTP {code} override"}).encode("utf-8"),
                )
            if self.serve_v4_on_search:
                return (200, {"Content-Type": "application/json"}, json.dumps(self.v4_payload).encode("utf-8"))
            return (
                403,
                {"Content-Type": "application/json"},
                json.dumps({"message": "No match found for request"}).encode("utf-8"),
            )

        if "/pc/v4/jobdetails/" in path:
            if "details" in self.status_overrides:
                code = self.status_overrides["details"]
                return (
                    code,
                    {"Content-Type": "application/json"},
                    json.dumps({"message": f"HTTP {code} override"}).encode("utf-8"),
                )
            hash_id = path.rsplit("/pc/v4/jobdetails/", 1)[-1].strip("/")
            if hash_id in self.jobdetails:
                return (
                    200,
                    {"Content-Type": "application/json"},
                    json.dumps(self.jobdetails[hash_id]).encode("utf-8"),
                )
            return (
                404,
                {"Content-Type": "application/json"},
                json.dumps({"code": "STELLENANGEBOT_NICHT_GEFUNDEN"}).encode("utf-8"),
            )

        return (404, {"Content-Type": "application/json"}, json.dumps({"error": "Not Found"}).encode("utf-8"))


class _FakeUrllibRawResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int, headers: dict[str, str]) -> None:
        super().__init__(body)
        self.status = status
        self.code = status
        msg = email.message.Message()
        for k, v in headers.items():
            msg[k] = v
        self.headers = msg

    def getcode(self) -> int:
        return self.status

    def info(self) -> email.message.Message:
        return self.headers


@pytest.fixture
def mock_jobsuche_http(
    monkeypatch: pytest.MonkeyPatch,
    sample_v6_jobs_pages: dict[int, dict[str, Any]],
    sample_v4_jobs_payload: dict[str, Any],
    sample_jobdetails_payloads: dict[str, dict[str, Any]],
) -> FakeJobsucheRouter:
    """Opt-in HTTP transport mock for requests, httpx, and urllib targeting Jobsuche API."""
    router = FakeJobsucheRouter(sample_v6_jobs_pages, sample_v4_jobs_payload, sample_jobdetails_payloads)

    def _should_intercept(url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        return (
            "rest.arbeitsagentur.de" in parsed.netloc
            or "/jobboerse/jobsuche-service/" in parsed.path
            or "/pc/v6/jobs" in parsed.path
            or "/pc/v4/jobs" in parsed.path
            or "/pc/v4/jobdetails/" in parsed.path
        )

    if importlib.util.find_spec("requests") is not None:
        import requests
        import requests.structures

        orig_send = requests.Session.send

        def _fake_requests_send(
            self: requests.Session,
            request: requests.PreparedRequest,
            **kwargs: Any,
        ) -> requests.Response:
            url = str(request.url or "")
            if not _should_intercept(url):
                return orig_send(self, request, **kwargs)
            try:
                status, resp_headers, body = router.handle(
                    str(request.method or "GET"),
                    url,
                    dict(request.headers or {}),
                )
            except TimeoutError as exc:
                raise requests.exceptions.ReadTimeout(str(exc), request=request) from exc
            resp = requests.Response()
            resp.status_code = status
            resp._content = body
            resp.headers = requests.structures.CaseInsensitiveDict(resp_headers)
            resp.encoding = "utf-8"
            resp.url = url
            resp.request = request
            return resp

        monkeypatch.setattr(requests.Session, "send", _fake_requests_send)

    if importlib.util.find_spec("httpx") is not None:
        import httpx

        orig_httpx_handle = httpx.HTTPTransport.handle_request
        orig_httpx_async_handle = httpx.AsyncHTTPTransport.handle_async_request

        def _fake_httpx_handle(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if not _should_intercept(url):
                return orig_httpx_handle(self, request)
            try:
                status, resp_headers, body = router.handle(
                    request.method,
                    url,
                    dict(request.headers.items()),
                )
            except TimeoutError as exc:
                raise httpx.ReadTimeout(str(exc), request=request) from exc
            return httpx.Response(status_code=status, headers=resp_headers, content=body, request=request)

        async def _fake_httpx_async_handle(
            self: httpx.AsyncHTTPTransport,
            request: httpx.Request,
        ) -> httpx.Response:
            url = str(request.url)
            if not _should_intercept(url):
                return await orig_httpx_async_handle(self, request)
            try:
                status, resp_headers, body = router.handle(
                    request.method,
                    url,
                    dict(request.headers.items()),
                )
            except TimeoutError as exc:
                raise httpx.ReadTimeout(str(exc), request=request) from exc
            return httpx.Response(status_code=status, headers=resp_headers, content=body, request=request)

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _fake_httpx_handle)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _fake_httpx_async_handle)

    orig_urllib_open = urllib.request.OpenerDirector.open

    def _fake_urllib_open(
        self: urllib.request.OpenerDirector,
        fullurl: str | urllib.request.Request,
        data: Any = None,
        timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
    ) -> Any:
        if isinstance(fullurl, urllib.request.Request):
            url = fullurl.full_url
            method = fullurl.get_method()
            req_headers = dict(fullurl.header_items())
        else:
            url = str(fullurl)
            method = "POST" if data is not None else "GET"
            req_headers = {}
        if not _should_intercept(url):
            return orig_urllib_open(self, fullurl, data=data, timeout=timeout)
        try:
            status, resp_headers, body = router.handle(method, url, req_headers)
        except TimeoutError as exc:
            raise urllib.error.URLError(exc) from exc
        raw = _FakeUrllibRawResponse(body, status, resp_headers)
        if status >= 400:
            raise urllib.error.HTTPError(url, status, f"HTTP {status}", raw.headers, raw)
        return urllib.request.addinfourl(raw, raw.headers, url, code=status)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", _fake_urllib_open)
    return router


@pytest.fixture
def local_jobsuche_server(
    sample_v6_jobs_pages: dict[int, dict[str, Any]],
    sample_v4_jobs_payload: dict[str, Any],
    sample_jobdetails_payloads: dict[str, dict[str, Any]],
) -> Iterator[tuple[str, FakeJobsucheRouter]]:
    router = FakeJobsucheRouter(sample_v6_jobs_pages, sample_v4_jobs_payload, sample_jobdetails_payloads)

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            full_url = f"http://127.0.0.1:{self.server.server_address[1]}{self.path}"
            try:
                status, resp_headers, body = router.handle("GET", full_url, dict(self.headers.items()))
            except TimeoutError:
                time.sleep(0.05)
                self.send_response(504)
                self.end_headers()
                return
            self.send_response(status)
            for k, v in resp_headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (f"http://127.0.0.1:{port}", router)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Layer 4: Deterministic TOTP Clock & IMAP Fallback Fixtures
# ---------------------------------------------------------------------------


class FakeClock:
    SAFE_WINDOW_TS = 1700000020.0
    BOUNDARY_OK_TS = 1700000036.9
    ROLLOVER_TS = 1700000038.0

    def __init__(self, initial_ts: float = SAFE_WINDOW_TS) -> None:
        self.current_ts = float(initial_ts)
        self.sleep_calls: list[float] = []

    def now(self) -> float:
        return self.current_ts

    def sleep(self, seconds: float) -> None:
        dur = float(seconds)
        self.sleep_calls.append(dur)
        if dur > 0:
            self.current_ts += dur

    def set(self, ts: float) -> None:
        self.current_ts = float(ts)


@pytest.fixture
def totp_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock(FakeClock.SAFE_WINDOW_TS)
    monkeypatch.setattr(time, "time", clock.now)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    return clock


class FakeIMAPMailbox:
    def __init__(self) -> None:
        self.messages: list[bytes] = []
        self.fail_auth: bool = False
        self.fail_timeout: bool = False

    def add_otp_email(
        self,
        code: str = "482910",
        *,
        subject: str = "Ihr Sicherheitscode",
        sender: str = "noreply@arbeitsagentur.de",
        body: str | None = None,
    ) -> None:
        msg = email.message.EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = "fisi.test@example.de"
        msg.set_content(body if body is not None else f"Ihr 6-stelliger Code: {code}\n")
        self.messages.append(msg.as_bytes())

    def add_plain_email(self, subject: str, body: str, sender: str = "noreply@arbeitsagentur.de") -> None:
        msg = email.message.EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = "fisi.test@example.de"
        msg.set_content(body)
        self.messages.append(msg.as_bytes())


@pytest.fixture
def mock_imap(monkeypatch: pytest.MonkeyPatch) -> FakeIMAPMailbox:
    mailbox = FakeIMAPMailbox()

    class _FakeIMAP4SSL:
        def __init__(self, host: str, port: int = 993, timeout: float | None = None) -> None:
            self.host = host
            self.port = port
            if mailbox.fail_timeout:
                raise TimeoutError("Simulated IMAP timeout")

        def __enter__(self) -> _FakeIMAP4SSL:
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
            if mailbox.fail_auth:
                raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
            return ("OK", [b"Logged in"])

        def select(self, folder: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
            return ("OK", [str(len(mailbox.messages)).encode("ascii")])

        def search(self, charset: str | None, *criteria: str) -> tuple[str, list[bytes]]:
            ids = " ".join(str(i + 1) for i in range(len(mailbox.messages)))
            return ("OK", [ids.encode("ascii")])

        def fetch(self, message_set: str | bytes, message_parts: str) -> tuple[str, list[Any]]:
            idx_str = message_set.decode("ascii") if isinstance(message_set, bytes) else str(message_set)
            idx = int(idx_str.strip()) - 1
            raw = mailbox.messages[idx]
            return ("OK", [(f"{idx + 1} (RFC822 {{{len(raw)}}})".encode("ascii"), raw)])

        def close(self) -> tuple[str, list[bytes]]:
            return ("OK", [b"CLOSE completed"])

        def logout(self) -> tuple[str, list[bytes]]:
            return ("BYE", [b"Logging out"])

    monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeIMAP4SSL)
    return mailbox


# ---------------------------------------------------------------------------
# Layer 5: Playwright Mock Browser / Context / Page / CAPTCHA Fixtures
# ---------------------------------------------------------------------------


class _FakeLocator:
    def __init__(self, page: FakePage, selector: str, index: int | None = None) -> None:
        self._page = page
        self._selector = selector
        self._index = index

    @property
    def first(self) -> _FakeLocator:
        return _FakeLocator(self._page, self._selector, index=0)

    def nth(self, index: int) -> _FakeLocator:
        return _FakeLocator(self._page, self._selector, index=index)

    def count(self) -> int:
        return len(self._page._match_elements(self._selector))

    def all(self) -> list[_FakeLocator]:
        matches = self._page._match_elements(self._selector)
        return [_FakeTextElement(self._page, self._selector, text, index=idx) for idx, text in enumerate(matches)]

    def is_visible(self, timeout: float | None = None) -> bool:
        return self.count() > 0

    def wait_for(self, state: str = "visible", timeout: float | None = None) -> None:
        if state == "visible" and self.count() == 0:
            raise TimeoutError(f"Selector not found: {self._selector}")

    def fill(self, value: str, timeout: float | None = None) -> None:
        self._page._record_fill(self._selector, value)

    def type(self, text: str, delay: float | None = None) -> None:
        self._page._record_fill(self._selector, text)

    def click(self, timeout: float | None = None) -> None:
        self._page._record_click(self._selector)

    def press(self, key: str) -> None:
        self._page._record_click(f"{self._selector}:{key}")

    def inner_text(self, timeout: float | None = None) -> str:
        matches = self._page._match_elements(self._selector)
        if not matches:
            return ""
        idx = self._index if self._index is not None else 0
        return matches[idx] if 0 <= idx < len(matches) else ""

    def text_content(self, timeout: float | None = None) -> str:
        return self.inner_text(timeout=timeout)

    def get_attribute(self, name: str, timeout: float | None = None) -> str | None:
        return self._page._get_attribute(self._selector, name, index=self._index or 0)


class _FakeTextElement(_FakeLocator):
    def __init__(self, page: FakePage, selector: str, text_val: str, index: int | None = None) -> None:
        super().__init__(page, selector, index=index)
        self._text_val = text_val

    def inner_text(self, timeout: float | None = None) -> str:
        return self._text_val

    def text_content(self, timeout: float | None = None) -> str:
        return self._text_val


class _FakeKeyboard:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    def type(self, text: str, delay: float | None = None) -> None:
        self._page.env.filled_values.append(("keyboard", text))

    def press(self, key: str) -> None:
        self._page.env.clicked_selectors.append(f"keyboard:{key}")
        if key.lower() == "enter" and self._page.env.scenario == "authenticated_portal":
            if self._page._step == "login":
                self._page._step = "otp"
            elif self._page._step == "otp":
                self._page._step = "dashboard"
                self._page.url = "https://www.arbeitsagentur.de/mein-arbeitsmarkt"


class FakePage:
    def __init__(self, env: FakePlaywrightEnv) -> None:
        self.env = env
        self.url: str = "about:blank"
        self.keyboard = _FakeKeyboard(self)
        self._step: str = "init"
        self._closed: bool = False

    def goto(self, url: str, **kwargs: Any) -> None:
        self.url = url
        self.env.visited_urls.append(url)
        lower = url.lower()
        if self.env.scenario == "unauthenticated" and "login" not in lower:
            self.url = "https://www.arbeitsagentur.de/login"
            self._step = "login"
        elif "login" in lower or "anmeld" in lower or "auth" in lower:
            self._step = "login"
        elif "vermittlung" in lower or "vorschlag" in lower or "postfach" in lower:
            self._step = "vermittlungsvorschlaege"
        elif "jobdetail" in lower or "detail" in lower or re.search(r"\d{5}-\d+", lower):
            self._step = "detail"
        else:
            self._step = "dashboard"

    def title(self) -> str:
        if self._is_captcha_active():
            return "Sicherheitsabfrage — Bundesagentur für Arbeit"
        return "Jobsuche — Bundesagentur für Arbeit"

    def _is_captcha_active(self) -> bool:
        if self.env.scenario == "captcha_login":
            return True
        if self.env.scenario == "captcha_detail" and self._step == "detail":
            return True
        return False

    def content(self) -> str:
        if self._is_captcha_active():
            return (
                "<html><head><title>Sicherheitsabfrage</title></head><body>"
                "<h1>Sicherheitsabfrage</h1>"
                "<p>Bitte bestätigen Sie, dass Sie ein Mensch sind.</p>"
                '<iframe src="https://captcha.arbeitsagentur.de/challenge"></iframe>'
                "</body></html>"
            )
        if self.env.scenario == "unauthenticated" or self._step == "login":
            return (
                "<html><body><h1>Anmeldung</h1>"
                '<form id="login-form">'
                '<input type="text" name="username" id="username" />'
                '<input type="password" name="password" id="password" />'
                '<button type="submit" id="login-submit">Anmelden</button>'
                "</form></body></html>"
            )
        if self._step == "otp":
            return (
                "<html><body><h1>Zwei-Faktor-Authentifizierung</h1>"
                '<form id="otp-form">'
                '<input type="text" name="otp" id="otp-code" maxlength="6" />'
                '<button type="submit" id="otp-submit">Bestätigen</button>'
                "</form></body></html>"
            )
        if self._step == "vermittlungsvorschlaege":
            return (
                "<html><body><h1>Vermittlungsvorschläge</h1>"
                '<div class="vermittlungsvorschlag" data-refnr="10000-1206415041-S">'
                '<h2>Ausbildung Fachinformatiker/in - Systemintegration (m/w/d) 2026</h2>'
                '<span class="company">Müller &amp; Söhne IT-Systemhaus GmbH</span>'
                '<span class="refnr">10000-1206415041-S</span>'
                "</div>"
                '<div class="vermittlungsvorschlag" data-refnr="10000-1206420001-S">'
                '<h2>Ausbildung Systemintegration (Cloud &amp; Proxmox)</h2>'
                '<span class="company">Aachener Rechenzentrum AG</span>'
                '<span class="refnr">10000-1206420001-S</span>'
                "</div>"
                "</body></html>"
            )
        if self._step == "detail":
            if self.env.scenario == "missing_optional_contacts":
                return (
                    "<html><body>"
                    "<h1>Ausbildung Fachinformatiker/in - Systemintegration (m/w/d) 2026</h1>"
                    '<div class="job-description">'
                    "Du betreust Linux (Debian), Windows Server, Active Directory, "
                    "Virtualisierung mit Proxmox und Docker, Netzwerk und Firewall."
                    "</div>"
                    "</body></html>"
                )
            return (
                "<html><body>"
                "<h1>Ausbildung Fachinformatiker/in - Systemintegration (m/w/d) 2026</h1>"
                '<div class="job-description">'
                "Du betreust Linux (Debian), Windows Server, Active Directory, "
                "Virtualisierung mit Proxmox und Docker, sowie Netzwerk/Firewall und IT-Support."
                "</div>"
                '<div class="contact">'
                '<span class="contact-name">Frau Anna Weber</span>'
                '<a href="mailto:karriere@mueller-it.example">karriere@mueller-it.example</a>'
                '<a href="tel:+49 241 123456">+49 241 123456</a>'
                '<a class="apply-link" href="https://karriere.mueller-it.example/apply/10000-1206415041-S">Jetzt bewerben</a>'
                "</div>"
                "</body></html>"
            )
        return "<html><body><h1>Mein Arbeitsmarkt</h1><p>Willkommen</p></body></html>"

    def _match_elements(self, selector: str) -> list[str]:
        html = self.content()
        sel = selector.lower()
        if "captcha" in sel or "sicherheitsabfrage" in sel:
            return ["Sicherheitsabfrage"] if self._is_captcha_active() else []
        if "vermittlung" in sel:
            if self._step == "vermittlungsvorschlaege":
                return ["10000-1206415041-S", "10000-1206420001-S"]
            return []
        if "username" in sel or "email" in sel or "benutzer" in sel:
            return ["username"] if self._step == "login" else []
        if "password" in sel or "passwort" in sel:
            return ["password"] if self._step == "login" else []
        if "otp" in sel or "code" in sel or "totp" in sel or "tan" in sel:
            return ["otp"] if self._step in ("otp", "login") else []
        if "submit" in sel or "button" in sel or "anmeld" in sel or "bestätig" in sel:
            return ["submit"] if self._step in ("login", "otp") else []
        if "mailto:" in sel:
            m = re.findall(r'mailto:([^"]+)', html)
            return m
        if "tel:" in sel:
            m = re.findall(r'tel:([^"]+)', html)
            return m
        if "description" in sel or "beschreibung" in sel:
            m = re.findall(r'<div class="job-description">(.*?)</div>', html, re.S)
            return m
        if "apply" in sel or "bewerb" in sel:
            return re.findall(r'class="apply-link"[^>]*>([^<]*)</a>', html)
        if "contact" in sel or "ansprechpartner" in sel:
            m = re.findall(r'<span class="contact-name">(.*?)</span>', html)
            return m
        return []

    def _get_attribute(self, selector: str, attr: str, index: int = 0) -> str | None:
        html = self.content()
        if attr == "data-refnr":
            matches = re.findall(r'data-refnr="([^"]+)"', html)
            if matches:
                return matches[index] if 0 <= index < len(matches) else matches[0]
            return None
        if attr == "href" and "mailto" in selector.lower():
            m = re.search(r'href="(mailto:[^"]+)"', html)
            return m.group(1) if m else None
        if attr == "href" and "tel" in selector.lower():
            m = re.search(r'href="(tel:[^"]+)"', html)
            return m.group(1) if m else None
        if attr == "href" and ("apply" in selector.lower() or "bewerb" in selector.lower()):
            m = re.search(r'class="apply-link"\s+href="([^"]+)"', html)
            return m.group(1) if m else None
        if attr == "src" and "captcha" in selector.lower() and self._is_captcha_active():
            return "https://captcha.arbeitsagentur.de/challenge"
        return None

    def _record_fill(self, selector: str, value: str) -> None:
        self.env.filled_values.append((selector, value))
        if self._step == "login" and ("otp" in selector.lower() or "totp" in selector.lower()):
            self._step = "otp"

    @staticmethod
    def _is_submit_action(selector: str) -> bool:
        sel = selector.lower()
        if ":enter" in sel:
            return True
        if (
            any(tok in sel for tok in ("username", "password", "passwort", "benutzer", "email"))
            and "submit" not in sel
            and "button" not in sel
        ):
            return False
        if (
            any(tok in sel for tok in ("otp-code", 'name="otp"', "name='otp'"))
            and "submit" not in sel
            and "button" not in sel
        ):
            return False
        return any(
            tok in sel
            for tok in (
                "submit",
                "button",
                "anmeld",
                "bestätig",
                "bestaetig",
                "weiter",
                "continue",
                "confirm",
                "form",
                "login-btn",
                "otp-btn",
            )
        )

    def _record_click(self, selector: str) -> None:
        self.env.clicked_selectors.append(selector)
        if not self._is_submit_action(selector):
            return
        if self._step == "login" and self.env.scenario == "authenticated_portal":
            self._step = "otp"
        elif self._step == "otp" and self.env.scenario == "authenticated_portal":
            self._step = "dashboard"
            self.url = "https://www.arbeitsagentur.de/mein-arbeitsmarkt"

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    def get_by_label(self, text: str | re.Pattern[str], **kwargs: Any) -> _FakeLocator:
        label = text.pattern if isinstance(text, re.Pattern) else str(text)
        return _FakeLocator(self, label)

    def get_by_role(self, role: str, *, name: str | re.Pattern[str] | None = None, **kwargs: Any) -> _FakeLocator:
        name_str = name.pattern if isinstance(name, re.Pattern) else (name or "")
        return _FakeLocator(self, f"{role}:{name_str}".strip(":"))

    def get_by_text(self, text: str | re.Pattern[str], **kwargs: Any) -> _FakeLocator:
        txt = text.pattern if isinstance(text, re.Pattern) else str(text)
        return _FakeLocator(self, txt)

    def get_by_placeholder(self, text: str | re.Pattern[str], **kwargs: Any) -> _FakeLocator:
        ph = text.pattern if isinstance(text, re.Pattern) else str(text)
        return _FakeLocator(self, ph)

    def query_selector(self, selector: str) -> _FakeLocator | None:
        loc = _FakeLocator(self, selector)
        return loc if loc.count() > 0 else None

    def query_selector_all(self, selector: str) -> list[_FakeLocator]:
        return _FakeLocator(self, selector).all()

    def wait_for_selector(self, selector: str, timeout: float | None = None, state: str = "visible") -> _FakeLocator:
        loc = _FakeLocator(self, selector)
        loc.wait_for(state=state, timeout=timeout)
        return loc

    def fill(self, selector: str, value: str, timeout: float | None = None) -> None:
        self._record_fill(selector, value)

    def click(self, selector: str, timeout: float | None = None) -> None:
        self._record_click(selector)

    def press(self, selector: str, key: str) -> None:
        self._record_click(f"{selector}:{key}")

    def wait_for_timeout(self, timeout_ms: float) -> None:
        return None

    def wait_for_load_state(self, state: str = "load", timeout: float | None = None) -> None:
        return None

    def screenshot(self, *, path: str | Path | None = None, **kwargs: Any) -> bytes:
        if path is not None:
            dest = Path(path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(_PNG_SIGNATURE_BYTES)
            self.env.screenshot_paths.append(dest)
        return _PNG_SIGNATURE_BYTES

    def close(self) -> None:
        self._closed = True


class FakeBrowserContext:
    def __init__(self, env: FakePlaywrightEnv, kwargs: dict[str, Any]) -> None:
        self.env = env
        self.kwargs = kwargs
        self._pages: list[FakePage] = []
        self._closed = False

    @property
    def pages(self) -> list[FakePage]:
        return list(self._pages)

    def new_page(self) -> FakePage:
        page = FakePage(self.env)
        self._pages.append(page)
        return page

    def cookies(self, urls: Any = None) -> list[dict[str, Any]]:
        return [{"name": "ba_session", "value": "tok123", "domain": ".arbeitsagentur.de", "path": "/"}]

    def storage_state(self, *, path: str | Path | None = None) -> dict[str, Any]:
        payload = {
            "cookies": [{"name": "ba_session", "value": "tok123", "domain": ".arbeitsagentur.de", "path": "/"}],
            "origins": [],
        }
        if path is not None:
            dest = Path(path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def set_default_timeout(self, timeout: float) -> None:
        return None

    def close(self) -> None:
        self._closed = True


class FakeBrowser:
    def __init__(self, env: FakePlaywrightEnv) -> None:
        self.env = env
        self._closed = False

    def new_context(self, **kwargs: Any) -> FakeBrowserContext:
        self.env.context_calls.append(dict(kwargs))
        return FakeBrowserContext(self.env, kwargs)

    def close(self) -> None:
        self._closed = True


class FakeChromium:
    def __init__(self, env: FakePlaywrightEnv) -> None:
        self.env = env

    def launch(self, **kwargs: Any) -> FakeBrowser:
        self.env.launch_calls.append(dict(kwargs))
        return FakeBrowser(self.env)

    def connect_over_cdp(self, endpoint_url: str, **kwargs: Any) -> FakeBrowser:
        self.env.launch_calls.append({"cdp_url": endpoint_url, **kwargs})
        return FakeBrowser(self.env)


class FakePlaywrightInstance:
    def __init__(self, env: FakePlaywrightEnv) -> None:
        self.chromium = FakeChromium(env)
        self.firefox = self.chromium
        self.webkit = self.chromium

    def stop(self) -> None:
        return None


class FakeSyncPlaywrightCM:
    def __init__(self, env: FakePlaywrightEnv) -> None:
        self._env = env
        self._instance: FakePlaywrightInstance | None = None

    def __enter__(self) -> FakePlaywrightInstance:
        self._instance = FakePlaywrightInstance(self._env)
        return self._instance

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def start(self) -> FakePlaywrightInstance:
        self._instance = FakePlaywrightInstance(self._env)
        return self._instance


class FakePlaywrightEnv:
    def __init__(self, workspace: E2EWorkspace) -> None:
        self.workspace = workspace
        self.scenario: str = "authenticated_portal"
        self.launch_calls: list[dict[str, Any]] = []
        self.context_calls: list[dict[str, Any]] = []
        self.filled_values: list[tuple[str, str]] = []
        self.clicked_selectors: list[str] = []
        self.visited_urls: list[str] = []
        self.screenshot_paths: list[Path] = []
        self.captcha_solver_calls: int = 0

    def sync_playwright(self) -> FakeSyncPlaywrightCM:
        return FakeSyncPlaywrightCM(self)

    def make_page(self) -> FakePage:
        return FakePage(self)


@pytest.fixture
def fake_playwright(monkeypatch: pytest.MonkeyPatch, isolated_env: E2EWorkspace) -> FakePlaywrightEnv:
    sync_api = require_module("playwright.sync_api")
    env = FakePlaywrightEnv(isolated_env)
    monkeypatch.setattr(sync_api, "sync_playwright", env.sync_playwright)
    if importlib.util.find_spec("playwright.async_api") is not None:
        async_api = require_module("playwright.async_api")

        class _FakeAsyncCM:
            async def __aenter__(self) -> FakePlaywrightInstance:
                return FakePlaywrightInstance(env)

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

        monkeypatch.setattr(async_api, "async_playwright", lambda: _FakeAsyncCM())
    return env


# ---------------------------------------------------------------------------
# Layer 6: Hermes Plugin & Antigravity MCP Helpers
# ---------------------------------------------------------------------------


class FakeHermesContext:
    def __init__(self, plugin_id: str = "jobsuche") -> None:
        self.plugin_id = plugin_id
        self.tools: dict[str, dict[str, Any]] = {}
        self.skills: dict[str, dict[str, Any]] = {}
        self.hooks: list[dict[str, Any]] = []
        self.config: dict[str, Any] = {}
        self.unload_callbacks: list[Callable[[], Any]] = []

    def register_tool(
        self,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Callable[..., Any],
        check_fn: Callable[..., Any] | None = None,
        requires_env: list[str] | None = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        override: bool = False,
    ) -> None:
        self.tools[name] = {
            "name": name,
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "check_fn": check_fn,
            "requires_env": requires_env,
            "is_async": is_async,
            "description": description,
            "emoji": emoji,
            "override": override,
        }

    def register_skill(
        self,
        name: str,
        path: str | Path,
        description: str = "",
        frontmatter: dict[str, Any] | None = None,
    ) -> None:
        if ":" in name or not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            raise ValueError(f"Invalid skill name: {name}")
        skill_path = Path(path)
        if not skill_path.exists():
            raise FileNotFoundError(f"Skill file does not exist: {skill_path}")
        self.skills[name] = {
            "name": name,
            "path": skill_path,
            "description": description,
            "frontmatter": frontmatter or {},
        }

    def register_hook(self, event: str, callback: Callable[..., Any], **kwargs: Any) -> None:
        self.hooks.append({"event": event, "callback": callback, **kwargs})

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def set_config(self, key: str, value: Any) -> None:
        self.config[key] = value

    def on_unload(self, callback: Callable[[], Any]) -> None:
        self.unload_callbacks.append(callback)


@pytest.fixture
def hermes_recording_ctx() -> FakeHermesContext:
    return FakeHermesContext()


def load_hermes_jobsuche_module(monkeypatch: pytest.MonkeyPatch, hermes_home: Path | None = None) -> ModuleType:
    init_file = HERMES_PLUGIN_DIR / "__init__.py"
    assert init_file.is_file(), f"Missing Hermes plugin entrypoint: {init_file}"
    if hermes_home is not None:
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    pkg_name = "hermes_plugins"
    if pkg_name not in sys.modules:
        pkg_mod = ModuleType(pkg_name)
        pkg_mod.__path__ = [str(HERMES_PLUGIN_DIR.parent)]
        sys.modules[pkg_name] = pkg_mod

    mod_name = "hermes_plugins.jobsuche"
    for key in list(sys.modules.keys()):
        if key == mod_name or key.startswith(mod_name + "."):
            sys.modules.pop(key, None)

    spec = importlib.util.spec_from_file_location(
        mod_name,
        str(init_file),
        submodule_search_locations=[str(HERMES_PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def parse_antigravity_mcp_command() -> tuple[str, list[str], dict[str, str], Path | None]:
    cfg_path = ANTIGRAVITY_PLUGIN_DIR / "mcp_config.json"
    assert cfg_path.is_file(), f"Missing Antigravity mcp_config.json: {cfg_path}"
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers", {})
    assert len(servers) >= 1, "mcp_config.json must define at least one mcpServer"
    _, srv_cfg = next(iter(servers.items()))

    def _subst(val: str) -> str:
        return val.replace("${PLUGIN_ROOT}", str(ANTIGRAVITY_PLUGIN_DIR))

    command = _subst(str(srv_cfg["command"]))
    args = [_subst(str(a)) for a in srv_cfg.get("args", [])]
    env_overrides = {str(k): _subst(str(v)) for k, v in (srv_cfg.get("env") or {}).items()}
    cwd_raw = srv_cfg.get("cwd")
    cwd = Path(cwd_raw) if cwd_raw else None
    if cwd is not None and not cwd.is_absolute():
        cwd = ANTIGRAVITY_PLUGIN_DIR / cwd
    return command, args, env_overrides, cwd


def run_mcp_stdio(
    workspace: E2EWorkspace,
    callback: Callable[[Any, Any], Any],
    *,
    api_base_url: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> Any:
    mcp_client_mod = require_module("mcp")
    mcp_stdio_mod = require_module("mcp.client.stdio")
    command, args, env_overrides, cwd = parse_antigravity_mcp_command()
    env = build_clean_subprocess_env(workspace, api_base_url=api_base_url, extra=env_overrides)
    if extra_env:
        env.update(extra_env)

    params = mcp_stdio_mod.StdioServerParameters(
        command=command,
        args=args,
        env=env,
        cwd=str(cwd or workspace.root),
    )

    async def _runner() -> Any:
        async with mcp_stdio_mod.stdio_client(params) as (read_stream, write_stream):
            async with mcp_client_mod.ClientSession(read_stream, write_stream) as session:
                init_result = await session.initialize()
                return await callback(session, init_result)

    return asyncio.run(_runner())
