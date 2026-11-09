"""Tiny JSON-over-HTTP helper (standard library only) shared by the HA and TeslaMate adapters.

Errors are converted to :class:`AdapterUnavailable` / :class:`AdapterError` with messages that are
safe to show: they never contain the token, and URLs are reduced to ``scheme://host[:port]``.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from .base import AdapterError, AdapterUnavailable

DEFAULT_TIMEOUT_S = 10.0
MAX_BODY_BYTES = 8 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """R17: never follow a redirect — urllib would resend the Authorization header to whatever host it names."""

    def redirect_request(self, *a: Any, **k: Any) -> None:  # type: ignore[override]
        return None


def origin_of(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    try:
        host, port = p.hostname or "", p.port
    except ValueError:
        return "(invalid url)"
    if not (p.scheme and host):
        return "(invalid url)"
    if ":" in host:
        host = f"[{host}]"
    return f"{p.scheme}://{host}" + (f":{port}" if port else "")  # never userinfo (user:password@)


def normalize_base_url(url: str, what: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise AdapterError(f"{what} URL is not configured")
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise AdapterError(f"{what} URL must start with http:// or https:// (got '{origin_of(url)}')")
    if p.username or p.password:
        raise AdapterError(f"{what} URL must not contain a username or password; put the token in the token setting")
    return url


def json_request(method: str, url: str, *, token: Optional[str] = None, body: Optional[Dict[str, Any]] = None,
                 timeout: float = DEFAULT_TIMEOUT_S, verify_tls: bool = True, what: str = "service",
                 extra_headers: Optional[Dict[str, str]] = None) -> Any:
    headers = {"Accept": "application/json", "User-Agent": "pit-crew"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    headers.update(extra_headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    handlers: list = [_NoRedirect()]
    if url.startswith("https") and not verify_tls:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    where = origin_of(url)
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310 - scheme checked
            raw = resp.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise AdapterError(_http_error_message(what, where, exc.code)) from None
    except (urllib.error.URLError, socket.timeout, ConnectionError, ssl.SSLError, OSError,
            http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        raise AdapterUnavailable(f"cannot reach {what} at {where}: {type(reason).__name__}") from None
    if len(raw) > MAX_BODY_BYTES:
        raise AdapterError(f"{what} response was unexpectedly large")
    if not raw.strip():
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise AdapterError(f"{what} at {where} returned something that is not JSON") from None


def _http_error_message(what: str, where: str, code: int) -> str:
    if 300 <= code < 400:
        return f"{what} at {where} answered with a redirect (HTTP {code}); redirects are not followed so credentials never leave {where}. Check the URL (http vs https, trailing path)"
    if code in (401, 403):
        return f"{what} at {where} rejected the credentials (HTTP {code}); check the token"
    if code == 404:
        return f"{what} at {where} says not found (HTTP 404); check the URL, car id or entity names"
    if code >= 500:
        return f"{what} at {where} had an internal error (HTTP {code})"
    return f"{what} at {where} refused the request (HTTP {code})"
