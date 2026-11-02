"""HTTPS reads for the four official hosts this plugin calls.

Keys come from agent.secret_scope.get_secret on every read. The opener
uses an empty proxy map and does not follow redirects. A local counter
stops Companies House at 600 requests in any 300 second window.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import base64
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

CH_HOST = "api.company-information.service.gov.uk"
GAZETTE_HOST = "www.thegazette.co.uk"
CHARITY_HOST = "api.charitycommission.gov.uk"
LAND_HOST = "landregistry.data.gov.uk"
ALLOWED_HOSTS = frozenset({CH_HOST, GAZETTE_HOST, CHARITY_HOST, LAND_HOST})

CH_KEY = "COMPANIES_HOUSE_API_KEY"
CHARITY_KEY = "CHARITY_COMMISSION_API_KEY"
CHARITY_HEADER = "Ocp-Apim-Subscription-Key"

CH_LIMIT = 600
CH_WINDOW_SECONDS = 300
CH_GAP_SECONDS = 0.6
GAZETTE_GAP_SECONDS = 10
OTHER_GAP_SECONDS = 1
REQUEST_TIMEOUT_SECONDS = 20
CALL_DEADLINE_SECONDS = 60
MAX_BYTES = 1_000_000
USER_AGENT = "hermes-plugin-uk-companies/0.1.0"

_LOCK = threading.Lock()
_clock = time.monotonic
_sleep = time.sleep
_transport: "Transport | None" = None
_ch_times: list[float] = []
_last_at: dict[str, float] = {}
_secrets_seen: list[str] = []
_host_counts: dict[str, int] = {}


class ApiError(Exception):
    def __init__(self, error_type: str, message: str, next_step: str, status: int | None = None):
        self.error_type = error_type
        self.message = message
        self.next_step = next_step
        self.status = status
        super().__init__(error_type)


@dataclass
class HttpResult:
    status: int
    body: bytes
    final_url: str


class Transport:
    def send(self, method: str, url: str, headers: dict[str, str], timeout: float) -> HttpResult:
        raise NotImplementedError


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


class UrllibTransport(Transport):
    def send(self, method: str, url: str, headers: dict[str, str], timeout: float) -> HttpResult:
        request = urllib.request.Request(url, method=method, headers=headers)
        try:
            with build_opener().open(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 0) or 0)
                final_url = response.geturl()
                body = _read_capped(response)
        except urllib.error.HTTPError as error:
            status = int(error.code)
            final_url = url
            try:
                body = error.read(MAX_BYTES + 1)
            except Exception:
                body = b""
            if body is None:
                body = b""
            if len(body) > MAX_BYTES:
                raise ApiError("too_large", "The upstream body exceeded the size cap and was discarded.", NEXT["too_large"], status)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ApiError("down", "The upstream could not be reached.", NEXT["down"]) from None
        return HttpResult(status, body, final_url)


def _read_capped(response) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_BYTES:
            raise ApiError("too_large", "The upstream body exceeded the size cap and was discarded.", NEXT["too_large"])
        chunks.append(chunk)
    return b"".join(chunks)


NEXT = {
    "down": "Check the network and try again in a minute.",
    "auth": "Check the API key in the active profile .env and try again.",
    "permission": "The upstream refused this key. Wait several minutes and try one call again.",
    "rate": "The upstream rate limit answered. Wait five minutes before another Companies House call.",
    "host_rate": "The upstream rate limit answered. Wait before another call to this host.",
    "keyless": "This call sent no API key. Check the query and try once more.",
    "bad_shape": "The upstream body was not the expected JSON. Try the call once more.",
    "upstream": "The upstream returned an error status. Try again later.",
    "body_error": "The upstream returned HTTP 200 and the JSON named an error. The row was not used. Try the call once more.",
    "not_found": "Check the identifier in the request. Waiting leaves this 404 in place.",
    "redirect": "The upstream sent a redirect and it was not followed. Try again later.",
    "too_large": "Narrow the query. Bodies over 1000000 bytes are discarded.",
    "local_rate": "This process reached 600 Companies House requests in 5 minutes. Wait for that window to pass.",
    "deadline": "The 60 second limit for this call was reached. Run the lookup again.",
    "bad_input": "Check the argument format and call the tool again.",
    "no_key": "Set the named key in the active profile .env. This source was not called.",
    "scope": "The profile secret scope is not active, so the key was not read. Retry from a normal Hermes turn.",
}


def configure(*, transport: Transport | None = None, clock=None, sleep=None) -> None:
    global _transport, _clock, _sleep
    with _LOCK:
        _transport = transport
        if clock is not None:
            _clock = clock
        if sleep is not None:
            _sleep = sleep


def reset_runtime() -> None:
    global _transport, _clock, _sleep
    with _LOCK:
        _transport = None
        _clock = time.monotonic
        _sleep = time.sleep
        _ch_times.clear()
        _last_at.clear()
        _secrets_seen.clear()
        _host_counts.clear()


def remember_secret(value: str | None) -> None:
    if not value or len(value) < 12:
        return
    with _LOCK:
        if value not in _secrets_seen:
            _secrets_seen.append(value)


def known_secrets() -> list[str]:
    with _LOCK:
        return list(_secrets_seen)


def host_counts() -> dict[str, int]:
    with _LOCK:
        return dict(_host_counts)


def clear_host_counts() -> None:
    """Drop the tally a tool result reports.

    The 600-request window stays in _ch_times.
    """
    with _LOCK:
        _host_counts.clear()


def clock_now() -> float:
    return float(_clock())


class Deadline:
    def __init__(self, seconds: float = CALL_DEADLINE_SECONDS):
        self.seconds = seconds
        self.start = clock_now()

    def expired(self) -> bool:
        return clock_now() - self.start >= self.seconds

    def remaining(self) -> float:
        return self.seconds - (clock_now() - self.start)


def ensure_time(deadline: Deadline | None) -> None:
    if deadline is not None and deadline.expired():
        raise ApiError("deadline", "The 60 second limit for this call was reached.", NEXT["deadline"])


def read_secret(name: str) -> str | None:
    """Return the key, None when it is unset, or raise ApiError for a broken read.

    UnscopedSecretError is not treated as a missing key.
    """
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret
    except Exception:
        raise ApiError("scope", "The secret reader could not be loaded.", NEXT["scope"]) from None
    try:
        value = get_secret(name, default=None)
    except UnscopedSecretError:
        raise ApiError("scope", "The profile secret scope is not active, so the key was not read.", NEXT["scope"]) from None
    except Exception:
        raise ApiError("scope", "The key could not be read.", NEXT["scope"]) from None
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) < 12 or any(ord(ch) < 32 or ch in " :" for ch in text):
        raise ApiError("auth", "The API key contains a character this plugin will not send.", NEXT["auth"])
    remember_secret(text)
    remember_secret(base64.b64encode(f"{text}:".encode("utf-8")).decode("ascii"))
    return text


def _gap_for(host: str) -> float:
    if host == CH_HOST:
        return CH_GAP_SECONDS
    if host == GAZETTE_HOST:
        return GAZETTE_GAP_SECONDS
    return OTHER_GAP_SECONDS


def _acquire(host: str, deadline: Deadline | None) -> None:
    ensure_time(deadline)
    gap = _gap_for(host)
    while True:
        with _LOCK:
            now = clock_now()
            if host == CH_HOST:
                kept = [item for item in _ch_times if now - item < CH_WINDOW_SECONDS]
                _ch_times[:] = kept
                if len(_ch_times) >= CH_LIMIT:
                    raise ApiError(
                        "local_rate",
                        "The local Companies House cap of 600 requests in 5 minutes was reached.",
                        NEXT["local_rate"],
                    )
            previous = _last_at.get(host)
            wait = 0.0 if previous is None else gap - (now - previous)
            if wait <= 0:
                stamp = clock_now()
                _last_at[host] = stamp
                if host == CH_HOST:
                    _ch_times.append(stamp)
                return
        _sleep(wait)
        ensure_time(deadline)


def _check_url(url: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ApiError("bad_input", "Only https URLs on the official hosts are called.", NEXT["bad_input"])
    host = parsed.hostname or ""
    if host not in ALLOWED_HOSTS or parsed.port not in (None, 443):
        raise ApiError("bad_input", "That host is not one of the official registers this plugin calls.", NEXT["bad_input"])
    if "\\" in url or "\x00" in url:
        raise ApiError("bad_input", "The URL was rejected before the call.", NEXT["bad_input"])
    return parsed


def _path_allowed(host: str, path: str) -> bool:
    if host == CH_HOST:
        if path in {"/search/companies", "/search/disqualified-officers"}:
            return True
        company = re.fullmatch(
            r"/company/(?:[A-Z]{2}\d{6}|[A-Z]\d{7}|\d{6,8})"
            r"(?:/officers|/persons-with-significant-control|/filing-history)?",
            path,
        )
        officer = re.fullmatch(r"/disqualified-officers/natural/[A-Za-z0-9_-]{1,80}", path)
        return bool(company or officer)
    if host == GAZETTE_HOST:
        return path == "/insolvency/notice/data.json"
    if host == CHARITY_HOST:
        prefix = "/register/api/searchCharityName/"
        if path.startswith(prefix):
            raw = path[len(prefix):]
            if not raw or "/" in raw or "\\" in raw:
                return False
            rest = urllib.parse.unquote(raw)
            return bool(rest) and rest not in {".", ".."} and "\\" not in rest and "\x00" not in rest
        return re.fullmatch(r"/register/api/allcharitydetailsV2/\d{1,8}/\d{1,4}", path) is not None
    if host == LAND_HOST:
        return path == "/landregistry/query"
    return False


def request_json(url: str, headers: dict[str, str], deadline: Deadline | None = None) -> tuple[int, object]:
    parsed = _check_url(url)
    if not _path_allowed(parsed.hostname or "", parsed.path):
        raise ApiError("bad_input", "That path is not one this plugin calls.", NEXT["bad_input"])
    _acquire(parsed.hostname or "", deadline)
    timeout = REQUEST_TIMEOUT_SECONDS
    if deadline is not None:
        timeout = min(timeout, max(1.0, deadline.remaining()))
    with _LOCK:
        _host_counts[parsed.hostname or ""] = _host_counts.get(parsed.hostname or "", 0) + 1
    transport = _transport or UrllibTransport()
    try:
        result = transport.send("GET", url, headers, timeout)
    except ApiError:
        raise
    except Exception:
        raise ApiError("down", "The upstream could not be reached.", NEXT["down"]) from None
    final = urllib.parse.urlsplit(result.final_url or url)
    if final.hostname != parsed.hostname or 300 <= result.status < 400:
        raise ApiError("redirect", "A redirect was refused, so the body was not used.", NEXT["redirect"], result.status)
    host = parsed.hostname or ""
    keyed = host in {CH_HOST, CHARITY_HOST}
    if result.status == 401:
        if keyed:
            raise ApiError("auth", "The upstream rejected the API key.", NEXT["auth"], 401)
        raise ApiError("upstream", "The upstream returned HTTP 401. This call sent no API key.", NEXT["keyless"], 401)
    if result.status == 403:
        if keyed:
            raise ApiError("permission", "The upstream refused the request.", NEXT["permission"], 403)
        raise ApiError("upstream", "The upstream returned HTTP 403. This call sent no API key.", NEXT["keyless"], 403)
    if result.status == 429:
        if host == CH_HOST:
            raise ApiError("rate", "The upstream rate limit answered.", NEXT["rate"], 429)
        raise ApiError("rate", "The upstream rate limit answered.", NEXT["host_rate"], 429)
    if result.status == 404:
        raise ApiError("upstream", "The upstream returned 404 for this path.", NEXT["not_found"], 404)
    if result.status != 200:
        raise ApiError("upstream", f"The upstream returned HTTP {result.status}.", NEXT["upstream"], result.status)
    try:
        payload = json.loads(result.body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ApiError("bad_shape", "The upstream returned 200 with a body that is not JSON.", NEXT["bad_shape"], 200) from None
    if _body_reports_error(payload):
        raise ApiError("upstream", "The upstream JSON reported an error and the row was not used.", NEXT["body_error"], 200)
    return result.status, payload


def _body_reports_error(payload: object) -> bool:
    """Return true when a 200 JSON object names an error.

    A truthy error or errors value is an upstream failure even when items
    or company_name is also present.
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("error"):
        return True
    return bool(payload.get("errors"))


def basic_headers(key: str) -> dict[str, str]:
    token = base64.b64encode(f"{key}:".encode("utf-8")).decode("ascii")
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


def json_headers() -> dict[str, str]:
    return {"Accept": "application/json", "User-Agent": USER_AGENT}


def charity_headers(key: str) -> dict[str, str]:
    return {
        CHARITY_HEADER: key,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


def land_headers() -> dict[str, str]:
    return {"Accept": "application/sparql-results+json", "User-Agent": USER_AGENT}


def scrub(text: str) -> str:
    out = text
    for secret in known_secrets():
        if secret:
            out = out.replace(secret, "[redacted]")
    return out


def failed(error_type: str, message: str, next_step: str, **extra: object) -> dict:
    body: dict = {
        "ok": False,
        "error_type": error_type,
        "message": scrub(message),
        "next_step": scrub(next_step),
    }
    for key, value in extra.items():
        body[key] = value
    return body


def from_error(exc: ApiError, **extra: object) -> dict:
    return failed(exc.error_type, exc.message, exc.next_step, **extra)


def dump(payload: dict) -> str:
    return scrub(json.dumps(payload, ensure_ascii=False))
