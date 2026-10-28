"""Append-only Markdown record of every provider HTTP exchange.

Entries are written from the urllib Request right before it is opened and from the
bytes read off the connection, not from the engine's own bookkeeping.
"""
import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import BaseHandler

SECRET_HEADERS = {"authorization", "x-api-key", "api-key", "x-goog-api-key", "proxy-authorization",
                  "cookie", "set-cookie"}
REDACTED = "[REDACTED]"

INTRO = """\
Every HTTP exchange with a model provider, in the order it happened. Each request block is
written by a `urllib` handler inside `open()`, after urllib has added its own headers and
right before the connection is made; `http.client` then only adds `Connection: close` and
`Accept-Encoding: identity`. Each response block is written in `providers.completion()` from
the bytes read off the connection. Parallel agents interleave; match blocks by request ID
(`agent/call number`).

- Credentials are replaced with `{redacted}` in headers and bodies.
- JSON bodies are pretty-printed from the exact bytes. Size and SHA-256 refer to those bytes.
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _fence(text, lang=""):
    # A fence longer than any backtick run inside the text cannot be closed early.
    run = longest = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text}\n{fence}\n"


class WireLog:
    def __init__(self, path, run_id):
        self.path = Path(path)
        self.run_id = run_id
        self.lock = threading.Lock()

    def exchange(self, request_id):
        return WireExchange(self, request_id)

    def append(self, text):
        with self.lock:
            fresh = not self.path.exists()
            with open(self.path, "a", encoding="utf-8", newline="\n") as stream:
                if fresh:
                    stream.write(f"# Wire log: run {self.run_id}\n\n"
                                 + INTRO.format(redacted=REDACTED) + "\n")
                stream.write(text)
                stream.flush()


class WireExchange:
    """One request and its outcome. Never raises: logging must not change a run."""

    def __init__(self, log, request_id):
        self.log = log
        self.request_id = request_id
        self.secrets = ()
        self.started = None

    def _redact(self, text):
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        return text

    def _headers(self, items):
        lines = []
        for name, value in items:
            shown = REDACTED if name.lower() in SECRET_HEADERS else self._redact(str(value))
            lines.append(f"{name}: {shown}")
        return _fence("\n".join(lines)) if lines else "_none_\n"

    def _body(self, raw):
        if raw is None:
            return "_no body_\n"
        head = f"{len(raw)} bytes, SHA-256 `{hashlib.sha256(raw).hexdigest()}`\n\n"
        try:
            text = json.dumps(json.loads(raw.decode("utf-8")), indent=2, ensure_ascii=False)
            lang = "json"
        except (UnicodeDecodeError, ValueError):
            text, lang = raw.decode("utf-8", errors="replace"), ""
        return head + _fence(self._redact(text), lang)

    def _write(self, text):
        try:
            self.log.append(text)
        except Exception:
            pass

    def tap(self, timeout, secret=None):
        """A urllib handler that records the request at its final point before sending."""
        return _Tap(self, timeout, secret)

    def sent(self, request, timeout, secret=None):
        self.secrets = tuple(s for s in (secret,) if isinstance(s, str) and len(s) >= 4)
        self.started = time.monotonic()
        try:
            text = (f"## → {self.request_id} · request\n\n"
                    f"- Sent: {_now()}\n"
                    f"- `{request.get_method()} {self._redact(request.full_url)}`\n"
                    f"- Timeout: {timeout} s\n\n"
                    f"**Headers**\n\n{self._headers(request.header_items())}\n"
                    f"**Body**\n\n{self._body(request.data)}\n")
        except Exception as exc:
            text = f"## → {self.request_id} · request\n\n_Request could not be logged: {type(exc).__name__}_\n\n"
        self._write(text)

    def _elapsed(self):
        return "" if self.started is None else f" · {time.monotonic() - self.started:.2f} s"

    def received(self, status, reason, headers, raw):
        self._outcome(f"{status} {reason or ''}".strip(), headers, raw)

    def http_error(self, exc):
        try:
            raw = exc.read(4_000_001)
        except Exception:
            raw = None
        self._outcome(f"HTTP error {exc.code} {exc.reason or ''}".strip(), exc.headers, raw)

    def failed(self, kind, exc):
        detail = self._redact(f"{type(exc).__name__}: {exc}")
        self._write(f"## ← {self.request_id} · {kind}{self._elapsed()}\n\n"
                    f"- At: {_now()}\n\n{_fence(detail)}\n")

    def _outcome(self, status, headers, raw):
        try:
            items = headers.items() if headers is not None else []
            text = (f"## ← {self.request_id} · response {status}{self._elapsed()}\n\n"
                    f"- Received: {_now()}\n\n"
                    f"**Headers**\n\n{self._headers(items)}\n"
                    f"**Body**\n\n{self._body(raw)}\n")
        except Exception as exc:
            text = f"## ← {self.request_id} · response\n\n_Response could not be logged: {type(exc).__name__}_\n\n"
        self._write(text)


class _Tap(BaseHandler):
    handler_order = 1000  # After urllib's own preprocessing adds Host, Content-Length and User-Agent.

    def __init__(self, exchange, timeout, secret):
        self.exchange, self.timeout, self.secret = exchange, timeout, secret

    def http_request(self, request):
        self.exchange.sent(request, self.timeout, self.secret)
        return request

    https_request = http_request
