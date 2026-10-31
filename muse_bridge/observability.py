"""Side-effect-free logging helpers; never change a request to make it observable."""

from __future__ import annotations

import contextlib
import contextvars
import functools
import hashlib
import inspect
import json
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACE = 5
logging.addLevelName(TRACE, "TRACE")
_REDACTED = "[REDACTED]"
_LEVELS = {"TRACE": TRACE, "DEBUG": logging.DEBUG, "INFO": logging.INFO,
           "WARNING": logging.WARNING, "ERROR": logging.ERROR,
           "CRITICAL": logging.CRITICAL, "OFF": 100}
_SECRET = re.compile(r"token|secret|password|passwd|authorization|cookie|csrf|otp|credential|capture_authorization|signature|clientsecret|private_key|file_info", re.I)
_BINARY = {"file_data", "data_base64", "base64", "image_base64"}
_MESSAGE = {"content", "text", "message", "raw_message", "display_text", "markdown", "items"}
_AUTH_PATH = re.compile(r"/api/auth/|/api/hatch/(?:token|noise-notary-token)|getAppAccessToken|credential|capture|/aymh", re.I)
_URL = re.compile(r"(?:https?|wss?)://[^\s<>\"']+", re.I)
_KEY_VALUE = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])"
    r"([A-Za-z0-9_-]{0,80}(?:token|secret|password|passwd|cookie|authorization|csrf|otp)[A-Za-z0-9_-]{0,80})"
    r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;}]{1,4096})"
)
_context = contextvars.ContextVar("bridge_log_context", default={})
_run_id = uuid.uuid4().hex
_trace_enabled = False
_payload_limit = 65536
_sequence = 0
_sequence_lock = threading.Lock()
_owned_handlers = []
_config_lock = threading.RLock()


def new_id():
    return uuid.uuid4().hex


def safe_url(value):
    """Keep route/query names, never signed query values or userinfo."""
    try:
        p = urlsplit(str(value))
        host = p.netloc.rsplit("@", 1)[-1]
        query = urlencode([(key, _REDACTED) for key, _ in parse_qsl(p.query, keep_blank_values=True)])
        return urlunsplit((p.scheme, host, p.path, query, ""))
    except Exception:
        return "[invalid URL]"


def safe_text(value):
    text = str(value)
    text = re.sub(r"(?im)(\b(?:set-cookie|cookie)\s*[:=]\s*)[^\r\n]+", r"\1[REDACTED]", text)
    text = _URL.sub(lambda m: safe_url(m.group(0)), text)
    text = re.sub(r"(?i)\b(?:Bearer|QQBot|Basic)\s+[^\s,;\"']+", "[AUTH REDACTED]", text)
    # The left lookbehind lets the engine reject most start positions in O(1);
    # bounded quantifiers keep backtracking linear on long unbroken strings.
    text = _KEY_VALUE.sub(r"\1[REDACTED]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", _REDACTED, text)
    text = re.sub(r"[\r\n]+", r"\n", text)
    return text


def safe_error(exc):
    """Preserve diagnostic type/status/stack locations, not arbitrary exception bodies."""
    result = {"type": type(exc).__name__}
    for key in ("status", "code", "stage", "errno"):
        value = getattr(exc, key, None)
        if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value)):
            result[key] = value
    if exc.__traceback__:
        result["stack"] = [{"file": os.path.basename(frame.filename), "line": frame.lineno, "function": frame.name}
                           for frame in traceback.extract_tb(exc.__traceback__)]
    cause = exc.__cause__
    if cause is not None:
        result["cause_type"] = type(cause).__name__
    return result


def _clean(value, *, messages=False, depth=0):
    if depth > 30:
        return "[depth limit]"
    if isinstance(value, BaseException):
        return safe_error(value)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key = str(key)
            if _SECRET.search(key) or key.lower() in {"verification_code", "code_verifier", "username", "contact_point"}:
                result[key] = _REDACTED
            elif key.lower() in _BINARY:
                result[key] = {"omitted": "binary", "encoded_length": len(item) if isinstance(item, (str, bytes)) else None}
            elif messages and key.lower() in _MESSAGE:
                result[key] = _REDACTED
            else:
                result[key] = _clean(item, messages=messages, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_clean(item, messages=messages, depth=depth + 1) for item in value]
    if isinstance(value, bytes):
        if messages:
            return {"omitted": "unclassified_bytes", "bytes": len(value)}
        try:
            return _clean(json.loads(value), messages=messages, depth=depth + 1)
        except (ValueError, UnicodeError):
            return {"omitted": "binary_or_unstructured", "bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, str):
        if messages:
            return _REDACTED
        # JSON embedded in a field must pass the same sensitive-key policy.
        if value.lstrip().startswith(("{", "[")):
            try:
                return _clean(json.loads(value), messages=messages, depth=depth + 1)
            except ValueError:
                pass
        return safe_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return {"type": type(value).__name__}


def _payload(value, *, sensitive=False, messages=False):
    if sensitive:
        return _REDACTED
    result = _clean(value, messages=messages)
    encoded = json.dumps(result, ensure_ascii=False, default=lambda _: "[unsupported]")
    if _payload_limit and len(encoded.encode("utf-8")) > _payload_limit:
        raw = encoded.encode("utf-8")
        return {"truncated": True, "sanitized_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "preview": raw[:_payload_limit].decode("utf-8", "ignore")}
    return result


class _SafeFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record):
        # Never let logging's default exception formatter print response bodies.
        clone = logging.makeLogRecord(record.__dict__.copy())
        clone.msg = safe_text(record.getMessage())
        clone.args = ()
        if clone.exc_info and clone.exc_info[1] is not None:
            clone.msg += " " + json.dumps(safe_error(clone.exc_info[1]), ensure_ascii=False)
        clone.exc_info = None
        clone.exc_text = None
        clone.stack_info = None
        clone.run_id = _run_id
        clone.correlation = json.dumps(_context.get(), ensure_ascii=False) if _context.get() else "-"
        return super().format(clone)


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(record.trace_data, ensure_ascii=False, separators=(",", ":"))


def _log_failure(handler=None):
    if handler is not None and getattr(handler, "_warned", False):
        return
    if handler is not None:
        handler._warned = True
    try:
        sys.stderr.write("Muse Bridge: logging output unavailable; application continues.\n")
    except Exception:
        pass


class _QuietStream(logging.StreamHandler):
    def handleError(self, record):
        _log_failure(self)


class _QuietRotating(logging.handlers.RotatingFileHandler):
    def handleError(self, record):
        _log_failure(self)


class _DependencySink(logging.Handler):
    def emit(self, record):
        # Library messages can contain unclassified OTP input or token-bearing URLs.
        # Keep source/level, not their raw format strings, arguments, or stack locals.
        try:
            get_logger("dependencies").log(record.levelno, "%s event (%s:%s)", record.name, record.module, record.lineno)
            trace_event("dependency", "event", logger=record.name, level=record.levelname,
                        source=record.module, line=record.lineno,
                        error=safe_error(record.exc_info[1]) if record.exc_info and record.exc_info[1] else None)
        except Exception:
            pass


class _OnlyRuntime(logging.Filter):
    def filter(self, record):
        return not hasattr(record, "trace_data")


def get_logger(name):
    return logging.getLogger(name if name.startswith("muse_bridge") else "muse_bridge." + name)


_root = logging.getLogger("muse_bridge")
_root.addHandler(logging.NullHandler())


def configure_logging(config=None, *, legacy_debug=False):
    """Own only project handlers. Bad log destinations never prevent startup."""
    global _trace_enabled, _payload_limit
    with _config_lock:
        for handler in _owned_handlers:
            _root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                _log_failure(handler)
        _owned_handlers.clear()
        level = getattr(config, "level", "DEBUG" if legacy_debug else "INFO").upper()
        _root.setLevel(min(_LEVELS.get(level, logging.INFO), TRACE) if getattr(config, "trace", False) else _LEVELS.get(level, logging.INFO))
        _root.propagate = False
        for name in ("httpx", "httpcore", "websockets"):
            dependency = logging.getLogger(name)
            # Existing dependency handlers must not bypass the sanitized sink.
            # Detach, but do not close handlers owned by an embedding application.
            for child_name, child in list(logging.Logger.manager.loggerDict.items()):
                if child_name.startswith(name + ".") and isinstance(child, logging.Logger):
                    child.handlers.clear()
                    child.propagate = True
                    child.setLevel(logging.NOTSET)
            dependency.handlers[:] = [_DependencySink()]
            dependency.propagate = False
            dependency.setLevel(_LEVELS.get(level, logging.INFO))
        _trace_enabled = False
        _payload_limit = getattr(config, "payload_limit", 65536)
        fmt = _SafeFormatter("%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s [%(run_id)s] %(correlation)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
        max_bytes = getattr(config, "max_bytes", 10485760)
        backups = getattr(config, "backup_count", 5)

        def add(handler, formatter, minimum):
            handler.setFormatter(formatter)
            handler.setLevel(minimum)
            _root.addHandler(handler)
            _owned_handlers.append(handler)

        if getattr(config, "console", True):
            handler = _QuietStream(sys.stderr)
            handler.addFilter(_OnlyRuntime())
            add(handler, fmt, _LEVELS.get(level, logging.INFO))
        for path, tracing in ((getattr(config, "file", "logs/bridge.log"), False),
                              (getattr(config, "trace_file", "logs/trace.jsonl") if getattr(config, "trace", False) else "", True)):
            if not path:
                continue
            try:
                directory = os.path.dirname(os.path.abspath(path))
                os.makedirs(directory, exist_ok=True)
                handler = _QuietRotating(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8", delay=True)
                if tracing:
                    handler.addFilter(lambda record: hasattr(record, "trace_data"))
                    add(handler, _JsonFormatter(), TRACE)
                    _trace_enabled = level != "OFF"
                else:
                    handler.addFilter(_OnlyRuntime())
                    add(handler, fmt, _LEVELS.get(level, logging.INFO))
            except Exception:
                # Do not turn a logging/disk problem into authentication or delivery failure.
                _log_failure()


@contextlib.contextmanager
def log_context(**fields):
    token = _context.set({**_context.get(), **_clean(fields)})
    try:
        yield
    finally:
        _context.reset(token)


def trace_event(component, event, *, payload=None, sensitive=False, messages=False, **fields):
    if not _trace_enabled:
        return
    try:
        global _sequence
        with _sequence_lock:
            _sequence += 1
            sequence = _sequence
        data = {"schema": 1, "time": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                "monotonic_ns": time.monotonic_ns(), "run_id": _run_id, "sequence": sequence,
                "component": component, "event": event, "thread": threading.current_thread().name,
                **_context.get(), **_clean(fields)}
        if payload is not None:
            data["payload"] = _payload(payload, sensitive=sensitive, messages=messages)
        _root.log(TRACE, event, extra={"trace_data": data})
    except Exception:
        pass


def observed(component, *, messages=False):
    """Trace call/return/error without retrying, swallowing, or replacing exceptions."""
    def decorate(func):
        signature = inspect.signature(func)

        def begin(args, kwargs):
            if not _trace_enabled:
                return None
            try:
                arguments = dict(signature.bind_partial(*args, **kwargs).arguments)
                arguments.pop("self", None)
                request_id = new_id()
                path = str(arguments.get("url", arguments.get("path", "")))
                sensitive = bool(_AUTH_PATH.search(path)) or "login" in component
                owner = args[0] if args else None
                fields = {"request_id": request_id, "parent_id": _context.get().get("request_id")}
                connection_id = getattr(owner, "_log_connection_id", None)
                if isinstance(connection_id, str):
                    fields["connection_id"] = connection_id
                started = time.perf_counter()
                context_token = _context.set({**_context.get(), **fields})
                trace_event(component, func.__name__ + ".start", request_id=request_id, payload=arguments, sensitive=sensitive, messages=messages)
                return request_id, started, sensitive, context_token
            except Exception:
                return None

        def finish(state, result=None, exc=None):
            if state is None:
                return
            request_id, started, sensitive, context_token = state
            try:
                trace_event(component, func.__name__ + (".error" if exc is not None else ".end"),
                            request_id=request_id, duration_ms=(time.perf_counter() - started) * 1000,
                            error=safe_error(exc) if exc is not None else None,
                            payload=result, sensitive=sensitive, messages=messages)
            except Exception:
                pass
            finally:
                _context.reset(context_token)

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_call(*args, **kwargs):
                state = begin(args, kwargs)
                try:
                    result = await func(*args, **kwargs)
                except BaseException as exc:
                    finish(state, exc=exc)
                    raise
                finish(state, result=result)
                return result
            return async_call

        @functools.wraps(func)
        def call(*args, **kwargs):
            state = begin(args, kwargs)
            try:
                result = func(*args, **kwargs)
            except BaseException as exc:
                finish(state, exc=exc)
                raise
            finish(state, result=result)
            return result
        return call
    return decorate


def http_request(client, method, url, **kwargs):
    """Call the same bound method once. Only inspect already-buffered bodies."""
    if not _trace_enabled:
        return getattr(client, method)(url, **kwargs)
    request_id = new_id()
    started = time.perf_counter()
    sensitive = bool(_AUTH_PATH.search(str(url)))
    trace_event("http", "request", request_id=request_id, method=method.upper(), url=safe_url(url),
                payload=kwargs, sensitive=sensitive)
    try:
        response = getattr(client, method)(url, **kwargs)
    except BaseException as exc:
        trace_event("http", "error", request_id=request_id, error=safe_error(exc), duration_ms=(time.perf_counter() - started) * 1000)
        raise
    # A streamed response can be consumed without being cached: is_stream_consumed
    # is True but .content raises ResponseNotRead. Never read it to log it, and never
    # lose the response metadata because of the body access.
    body = {"omitted": "unconsumed_stream"}
    if getattr(response, "is_stream_consumed", False):
        try:
            body = response.content
        except Exception:
            body = {"omitted": "consumed_unbuffered_stream"}
    try:
        trace_event("http", "response", request_id=request_id, status=response.status_code,
                    headers=dict(response.headers), payload=body, sensitive=sensitive,
                    duration_ms=(time.perf_counter() - started) * 1000)
    except Exception:
        pass
    try:
        for redirect in response.history:
            trace_event("http", "redirect", request_id=request_id, status=redirect.status_code,
                        url=safe_url(redirect.url), headers=dict(redirect.headers))
    except Exception:
        pass
    return response
