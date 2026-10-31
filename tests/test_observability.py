"""Offline, public-API observability contracts, exercised through real temp files."""

import asyncio
import hashlib
import io
import json
import logging
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qsl, urlsplit

import httpx

from muse_bridge import observability as obs
from muse_bridge.config.models import LoggingConfig


REDACTED = "[REDACTED]"
FAILURE_NOTICE = "Muse Bridge: logging output unavailable; application continues."


class CountingStream(httpx.SyncByteStream):
    """A real HTTPX stream that must not be consumed by instrumentation."""

    def __init__(self, body):
        self.body = body
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        yield self.body


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.runtime_path = self.directory / "bridge.log"
        self.trace_path = self.directory / "trace.jsonl"
        self.stderr = io.StringIO()
        # Logging is process-wide. Restore the pre-test logger state even after
        # assertions fail; never change observability's private globals.
        names = {
            "muse_bridge", "muse_bridge.observability_test",
            "muse_bridge.dependencies", "httpx", "httpx._client",
            "httpcore", "httpcore.connection", "websockets",
            "websockets.client",
        }
        # Configuration can also secure already-created dependency descendants.
        # Snapshot those too, so this module does not contaminate other suites.
        for name, logger in list(logging.Logger.manager.loggerDict.items()):
            if isinstance(logger, logging.Logger) and name.startswith(("httpx.", "httpcore.", "websockets.")):
                names.add(name)
        self.loggers = {}
        for name in names:
            logger = logging.getLogger(name)
            self.loggers[name] = (
                logger, list(logger.handlers), list(logger.filters),
                logger.level, logger.propagate, logger.disabled,
            )
            logger.handlers = []
            logger.filters = []
            logger.disabled = False
            logger.setLevel(logging.NOTSET)
            logger.propagate = True
        self.logger = obs.get_logger("observability_test")
        # Cleanup executes LIFO, so close log files before removing the temp dir.
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.restore_logging)

    def restore_logging(self):
        try:
            obs.configure_logging(LoggingConfig(
                level="OFF", console=False, file=None, trace=False,
                trace_file=None,
            ))
        finally:
            for logger, handlers, filters, level, propagate, disabled in self.loggers.values():
                for handler in list(logger.handlers):
                    if handler not in handlers:
                        logger.removeHandler(handler)
                        handler.close()
                logger.handlers = handlers
                logger.filters = filters
                logger.setLevel(level)
                logger.propagate = propagate
                logger.disabled = disabled

    def configure(self, **overrides):
        values = dict(
            level="INFO", console=False, file=str(self.runtime_path),
            trace=True, trace_file=str(self.trace_path),
            payload_limit=0, max_bytes=1_000_000, backup_count=2,
        )
        values.update(overrides)
        config = LoggingConfig(**values)
        with patch("sys.stderr", self.stderr):
            obs.configure_logging(config)
        return config

    @staticmethod
    def read_text(path):
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def events(self):
        return [json.loads(line) for line in self.read_text(self.trace_path).splitlines()]

    def all_output(self):
        return self.read_text(self.runtime_path) + self.read_text(self.trace_path) + self.stderr.getvalue()

    def assert_hidden(self, *values, output=None):
        output = self.all_output() if output is None else output
        for value in values:
            self.assertNotIn(value, output)

    @staticmethod
    def response(body=None, *, url="https://service.test/resource", headers=None):
        return httpx.Response(
            200, json={"ok": True} if body is None else body,
            headers=headers, request=httpx.Request("POST", url),
        )

    def test_normal_level_filters_while_trace_is_independent(self):
        self.configure(level="WARNING", console=True)
        self.logger.debug("filtered-debug-marker")
        self.logger.info("filtered-info-marker")
        self.logger.warning("visible-warning-marker")
        self.logger.error("visible-error-marker")
        obs.trace_event("test", "trace-only-marker", payload={"count": 7})

        runtime = self.read_text(self.runtime_path)
        console = self.stderr.getvalue()
        for output in (runtime, console):
            self.assertNotIn("filtered-debug-marker", output)
            self.assertNotIn("filtered-info-marker", output)
            self.assertNotIn("trace-only-marker", output)
            self.assertEqual(output.count("visible-warning-marker"), 1)
            self.assertEqual(output.count("visible-error-marker"), 1)
        self.assertEqual([e["event"] for e in self.events()], ["trace-only-marker"])
        self.assertEqual(self.events()[0]["payload"], {"count": 7})

    def test_trace_false_does_not_emit_even_at_trace_normal_level(self):
        self.configure(level="TRACE", trace=False)
        obs.trace_event("test", "disabled-trace-marker", payload={"count": 1})
        self.logger.debug("normal-debug-marker")
        self.assertIn("normal-debug-marker", self.read_text(self.runtime_path))
        self.assertEqual(self.events(), [])
        self.assertNotIn("disabled-trace-marker", self.all_output())

    def test_repeated_configure_has_no_duplicate_normal_trace_or_dependency_output(self):
        for _ in range(3):
            self.configure(console=True)
        self.logger.info("one-runtime-marker")
        obs.trace_event("test", "one-trace-marker")
        logging.getLogger("httpx").warning("opaque dependency text")
        runtime = self.read_text(self.runtime_path)
        self.assertEqual(runtime.count("one-runtime-marker"), 1)
        self.assertEqual(self.stderr.getvalue().count("one-runtime-marker"), 1)
        self.assertEqual(runtime.count("httpx event"), 1)
        events = self.events()
        self.assertEqual(sum(e["event"] == "one-trace-marker" for e in events), 1)
        self.assertEqual(sum(e["component"] == "dependency" for e in events), 1)

    def test_off_suppresses_normal_console_trace_and_dependency_output(self):
        self.configure(level="OFF", console=True)
        self.logger.critical("off-runtime-marker")
        obs.trace_event("test", "off-trace-marker", payload={"count": 1})
        logging.getLogger("httpcore").critical("off-dependency-marker")
        self.assertEqual(self.all_output(), "")
        self.assertEqual(self.events(), [])

    def test_payload_limit_zero_keeps_full_sanitized_payload(self):
        self.configure(payload_limit=0)
        payload = {"public": ["完整" * 20] * 1_000, "access_token": "payload-token-secret"}
        obs.trace_event("test", "unlimited", payload=payload)
        result = self.events()[0]["payload"]
        self.assertEqual(result["public"], payload["public"])
        self.assertEqual(result["access_token"], REDACTED)
        self.assertNotIn("truncated", result)
        self.assertEqual(payload["access_token"], "payload-token-secret")
        self.assert_hidden("payload-token-secret")

    def test_payload_truncation_is_explicit_bounded_and_after_redaction(self):
        self.configure(payload_limit=80)
        obs.trace_event("test", "limited", payload={
            "password": "truncate-password-secret", "public": "可见" * 150,
        })
        payload = self.events()[0]["payload"]
        self.assertIs(payload["truncated"], True)
        self.assertGreater(payload["sanitized_bytes"], 80)
        self.assertLessEqual(len(payload["preview"].encode("utf-8")), 80)
        self.assertIn(REDACTED, payload["preview"])
        self.assertRegex(payload["sha256"], r"^[0-9a-f]{64}$")
        self.assert_hidden("truncate-password-secret")

    def test_runtime_and_jsonl_rotate_with_bounded_backups(self):
        self.configure(max_bytes=1400, backup_count=2)
        for number in range(24):
            self.logger.info("rotation-%02d %s", number, "x" * 250)
            obs.trace_event("test", "rotation", number=number, payload={"public": "y" * 250})
        for path in (self.runtime_path, self.trace_path):
            self.assertTrue(path.exists())
            backups = list(self.directory.glob(path.name + ".*"))
            self.assertEqual(len(backups), 2)
            self.assertTrue(all(p.stat().st_size > 0 for p in [path, *backups]))
            self.assertTrue(all(p.stat().st_size <= 1400 for p in [path, *backups]))
        self.assertIn("rotation-23", self.read_text(self.runtime_path))
        self.assertEqual(self.events()[-1]["number"], 23)
        retained = []
        for path in self.directory.glob("trace.jsonl*"):
            retained.extend(json.loads(line) for line in self.read_text(path).splitlines())
        self.assertTrue(retained)
        self.assertEqual(len({e["sequence"] for e in retained}), len(retained))

    def test_structured_headers_nested_json_and_file_info_are_redacted(self):
        self.configure()
        headers = {
            "Cookie": "session=cookie-secret; other=cookie-second-secret",
            "Set-Cookie": "session=set-cookie-secret; HttpOnly",
            "Authorization": "Bearer auth-secret",
            "Proxy-Authorization": "Basic proxy-auth-secret",
        }
        inner = {"deep": [{"refresh_token": "nested-token-secret", "file_info": "nested-file-secret"}]}
        payload = {
            "headers": headers, "file_info": "file-info-secret",
            "object": inner, "encoded_json": json.dumps(inner),
            "encoded_bytes": json.dumps(inner).encode(),
            "verification_code": "verification-secret",
            "code_verifier": "verifier-secret",
            "username": "username-secret", "contact_point": "contact-secret",
            "file_data": "binary-data-secret", "public": {"count": 3},
        }
        obs.trace_event("test", "redaction", payload=payload, headers=headers)
        event = self.events()[0]
        for values in (event["headers"], event["payload"]["headers"]):
            self.assertTrue(all(value == REDACTED for value in values.values()))
        for key in ("object", "encoded_json", "encoded_bytes"):
            self.assertEqual(event["payload"][key]["deep"][0]["file_info"], REDACTED)
        self.assertEqual(event["payload"]["public"], {"count": 3})
        self.assertEqual(payload["file_info"], "file-info-secret")
        self.assert_hidden(
            "cookie-secret", "cookie-second-secret", "set-cookie-secret",
            "auth-secret", "proxy-auth-secret", "nested-token-secret",
            "nested-file-secret", "file-info-secret", "verification-secret",
            "verifier-secret", "username-secret", "contact-secret", "binary-data-secret",
        )

    def test_url_userinfo_all_query_values_and_fragment_are_removed(self):
        self.configure()
        url = ("https://userinfo-secret:password-secret@service.test:8443/path"
               "?signature=query-secret&ordinary=ordinary-secret&empty=#fragment-secret")
        obs.trace_event("test", "url", url=url, payload={"link": url})
        event = self.events()[0]
        for safe in (event["url"], event["payload"]["link"]):
            parsed = urlsplit(safe)
            self.assertIsNone(parsed.username)
            self.assertIsNone(parsed.password)
            self.assertEqual(parsed.netloc, "service.test:8443")
            self.assertEqual(parsed.path, "/path")
            self.assertEqual(parsed.fragment, "")
            self.assertEqual(parse_qsl(parsed.query), [
                ("signature", REDACTED), ("ordinary", REDACTED), ("empty", REDACTED),
            ])
        self.assert_hidden("userinfo-secret", "password-secret", "query-secret",
                           "ordinary-secret", "fragment-secret")

    def test_runtime_auth_url_and_embedded_json_are_sanitized(self):
        self.configure(console=True)
        self.logger.warning(
            'Authorization: Bearer runtime-auth-secret Proxy-Authorization: Basic runtime-proxy-secret '
            'https://runtime-user:runtime-pass@service.test/p?key=runtime-query#runtime-fragment '
            '{"password":"runtime-password-secret","otp":"004321"}',
        )
        self.assertIn("[REDACTED]", self.read_text(self.runtime_path))
        self.assert_hidden(
            "runtime-auth-secret", "runtime-proxy-secret", "runtime-user",
            "runtime-pass", "runtime-query", "runtime-fragment",
            "runtime-password-secret", "004321",
        )

    def test_runtime_cookie_headers_do_not_leak_later_cookie_values(self):
        self.configure(console=True)
        self.logger.warning("Cookie: session=cookie-first-secret; preference=cookie-later-secret")
        self.logger.warning("Set-Cookie: session=setcookie-first-secret; note=setcookie-later-secret")
        self.assert_hidden("cookie-first-secret", "cookie-later-secret",
                           "setcookie-first-secret", "setcookie-later-secret")

    def test_messages_true_hides_every_string_and_bytes_without_low_entropy_hash(self):
        self.configure()
        otp = "004321"
        cases = [otp, otp.encode(), {"unknown": otp, "blob": otp.encode()},
                 [otp, otp.encode(), {"unrecognized": otp}],
                 {"message": otp, "content": otp, "resolved": {"attachment": otp}, "count": 7}]
        for number, payload in enumerate(cases):
            obs.trace_event("test", "message", number=number, payload=payload, messages=True)
        events = self.events()
        self.assertEqual(len(events), len(cases))
        self.assertEqual(events[-1]["payload"]["count"], 7)
        self.assertEqual(events[0]["payload"], REDACTED)
        self.assertIn("payload", events[1])
        self.assert_hidden(otp, *(getattr(hashlib, name)(otp.encode()).hexdigest()
                                  for name in ("md5", "sha1", "sha256")))

    def test_sensitive_payload_is_wholly_hidden_even_with_unknown_keys(self):
        self.configure()
        obs.trace_event("test", "sensitive", sensitive=True, payload={
            "not_a_known_secret_key": "opaque-auth-body-secret", "count": 27,
        })
        self.assertEqual(self.events()[0]["payload"], REDACTED)
        self.assert_hidden("opaque-auth-body-secret")

    def test_observed_auth_path_and_login_component_hide_whole_call_and_return(self):
        self.configure()
        for component, url in (("service", "https://service.test/api/auth/exchange"),
                               ("muse.login", "https://service.test/ordinary")):
            with self.subTest(component=component):
                result = {"unknown": "opaque-result-secret"}
                business = Mock(return_value=result)

                @obs.observed(component)
                def exchange(url, body):
                    return business(url, body)

                body = {"unknown": "opaque-input-secret"}
                self.assertIs(exchange(url, body), result)
                business.assert_called_once_with(url, body)
        self.assertTrue(all(e["payload"] == REDACTED for e in self.events()))
        self.assert_hidden("opaque-input-secret", "opaque-result-secret")

    def test_exception_stack_keeps_locations_not_message_cause_or_source_text(self):
        self.configure(console=True)
        try:
            try:
                raise RuntimeError("cause-body-secret")
            except RuntimeError as cause:
                raise ValueError("exception-body-secret") from cause
        except ValueError as error:
            original = error
            obs.trace_event("test", "exception", error=error, payload=error)
            self.logger.exception("operation failed", stack_info=True)
        event = self.events()[0]
        for diagnostic in (event["error"], event["payload"]):
            self.assertEqual(diagnostic["type"], "ValueError")
            self.assertEqual(diagnostic["cause_type"], "RuntimeError")
            self.assertTrue(diagnostic["stack"])
            frame = diagnostic["stack"][-1]
            self.assertEqual(frame["file"], Path(__file__).name)
            self.assertGreater(frame["line"], 0)
            self.assertIn("test_exception_stack", frame["function"])
        self.assertIsNotNone(original.__traceback__)
        self.assert_hidden("exception-body-secret", "cause-body-secret", "raise ValueError")

    def test_observed_sync_calls_once_and_preserves_arguments_and_result_identity(self):
        self.configure()
        result, argument = object(), object()
        business = Mock(return_value=result)

        @obs.observed("test.sync")
        def operation(value, *, flag):
            return business(value, flag=flag)

        self.assertIs(operation(argument, flag=True), result)
        business.assert_called_once_with(argument, flag=True)
        events = self.events()
        self.assertEqual([e["event"] for e in events], ["operation.start", "operation.end"])
        self.assertEqual(events[0]["request_id"], events[1]["request_id"])
        self.assertGreaterEqual(events[1]["duration_ms"], 0)

    def test_observed_sync_error_preserves_exact_exception_and_restores_context(self):
        self.configure()
        error = ValueError("sync-business-error-secret")
        business = Mock(side_effect=error)

        @obs.observed("test.sync")
        def operation():
            business()

        with obs.log_context(request_id="external-parent"):
            with self.assertRaises(ValueError) as raised:
                operation()
            obs.trace_event("test", "after-error")
        self.assertIs(raised.exception, error)
        business.assert_called_once_with()
        events = self.events()
        self.assertEqual(events[-1]["request_id"], "external-parent")
        self.assertEqual(events[1]["event"], "operation.error")
        self.assertEqual(events[1]["error"]["type"], "ValueError")
        self.assert_hidden("sync-business-error-secret")

    def test_nested_observed_start_and_end_have_correct_parent_and_no_context_leak(self):
        self.configure()
        inner_business = Mock(return_value=object())

        @obs.observed("test.nested")
        def inner():
            obs.trace_event("test", "inside-inner")
            return inner_business()

        @obs.observed("test.nested")
        def outer():
            result = inner()
            obs.trace_event("test", "inside-outer")
            return result

        with obs.log_context(request_id="external-parent", route="test-route"):
            self.assertIs(outer(), inner_business.return_value)
            obs.trace_event("test", "after-outer")
        obs.trace_event("test", "outside-context")
        inner_business.assert_called_once_with()
        events = {e["event"]: e for e in self.events()}
        outer_id = events["outer.start"]["request_id"]
        inner_id = events["inner.start"]["request_id"]
        self.assertNotEqual(outer_id, inner_id)
        for name in ("outer.start", "outer.end", "inside-outer"):
            self.assertEqual(events[name].get("parent_id"), "external-parent", name)
        for name in ("inner.start", "inner.end", "inside-inner"):
            self.assertEqual(events[name].get("parent_id"), outer_id, name)
            self.assertEqual(events[name]["request_id"], inner_id, name)
        self.assertEqual(events["after-outer"]["request_id"], "external-parent")
        self.assertNotIn("route", events["outside-context"])
        self.assertNotIn("request_id", events["outside-context"])

    def test_log_context_nesting_restores_after_exception(self):
        self.configure()
        with obs.log_context(route="outer", request_id="outer-request"):
            try:
                with obs.log_context(route="inner", local=7):
                    obs.trace_event("test", "context-inner")
                    raise ValueError("context error")
            except ValueError:
                pass
            obs.trace_event("test", "context-outer")
        obs.trace_event("test", "context-outside")
        inner, outer, outside = self.events()
        self.assertEqual(inner["route"], "inner")
        self.assertEqual(inner["request_id"], "outer-request")
        self.assertEqual(inner["local"], 7)
        self.assertEqual(outer["route"], "outer")
        self.assertNotIn("local", outer)
        self.assertNotIn("route", outside)
        self.assertNotIn("request_id", outside)

    def test_observed_async_calls_once_and_concurrent_contexts_are_isolated(self):
        self.configure()
        results = {"left": object(), "right": object()}
        business = AsyncMock(side_effect=lambda label: results[label])

        async def scenario():
            ready = asyncio.Event()
            arrivals = []

            @obs.observed("test.async")
            async def operation(label):
                arrivals.append(label)
                if len(arrivals) == 2:
                    ready.set()
                await ready.wait()
                obs.trace_event("test", "inside-async")
                return await business(label)

            async def worker(label):
                with obs.log_context(branch=label, request_id="parent-" + label):
                    result = await operation(label)
                    obs.trace_event("test", "after-worker")
                    return result

            returned = await asyncio.wait_for(
                asyncio.gather(worker("left"), worker("right")), timeout=5,
            )
            obs.trace_event("test", "after-gather")
            return returned

        returned = asyncio.run(scenario())
        self.assertIs(returned[0], results["left"])
        self.assertIs(returned[1], results["right"])
        self.assertEqual(business.await_count, 2)
        self.assertCountEqual([call.args for call in business.await_args_list], [("left",), ("right",)])
        events = self.events()
        ids = []
        for label in results:
            branch = [e for e in events if e.get("branch") == label]
            self.assertEqual(len(branch), 4)
            inner = next(e for e in branch if e["event"] == "inside-async")
            end = next(e for e in branch if e["event"] == "operation.end")
            after = next(e for e in branch if e["event"] == "after-worker")
            self.assertEqual(inner["parent_id"], "parent-" + label)
            self.assertEqual(end["request_id"], inner["request_id"])
            self.assertEqual(end["parent_id"], "parent-" + label)
            self.assertEqual(after["request_id"], "parent-" + label)
            ids.append(inner["request_id"])
        self.assertNotEqual(*ids)
        self.assertNotIn("branch", events[-1])
        self.assertNotIn("request_id", events[-1])

    def test_observed_async_preserves_exception_and_cancellation_identity(self):
        self.configure()
        for error in (ValueError("async-error-secret"), asyncio.CancelledError("cancel-secret")):
            with self.subTest(error=type(error).__name__):
                business = AsyncMock(side_effect=error)

                @obs.observed("test.async")
                async def operation():
                    return await business()

                async def scenario():
                    with obs.log_context(request_id="async-parent"):
                        with self.assertRaises(type(error)) as raised:
                            await operation()
                        self.assertIs(raised.exception, error)
                        obs.trace_event("test", "after-async-error")

                asyncio.run(scenario())
                business.assert_awaited_once_with()
        after = [e for e in self.events() if e["event"] == "after-async-error"]
        self.assertEqual(len(after), 2)
        self.assertTrue(all(e["request_id"] == "async-parent" for e in after))
        self.assert_hidden("async-error-secret", "cancel-secret")

    def test_http_calls_same_method_once_with_same_arguments_and_response(self):
        self.configure()
        url = "https://service.test/resource?signature=request-query-secret"
        response = self.response({"answer": 42, "token": "response-token-secret"}, headers={
            "Set-Cookie": "session=response-cookie-secret", "Content-Type": "application/json",
        })
        client = Mock(spec=["post"])
        client.post.return_value = response
        headers = {"Authorization": "Bearer request-auth-secret"}
        body = {"count": 2, "password": "request-password-secret"}
        extensions = {"opaque": object()}
        result = obs.http_request(client, "post", url, json=body, headers=headers,
                                  timeout=17, follow_redirects=False, extensions=extensions)
        self.assertIs(result, response)
        client.post.assert_called_once_with(url, json=body, headers=headers, timeout=17,
                                            follow_redirects=False, extensions=extensions)
        self.assertIs(client.post.call_args.kwargs["headers"], headers)
        self.assertIs(client.post.call_args.kwargs["json"], body)
        self.assertIs(client.post.call_args.kwargs["extensions"], extensions)
        request, returned = self.events()
        self.assertEqual([request["event"], returned["event"]], ["request", "response"])
        self.assertEqual(request["request_id"], returned["request_id"])
        self.assertEqual(request["method"], "POST")
        self.assertEqual(returned["status"], 200)
        self.assertEqual(returned["payload"]["answer"], 42)
        self.assertGreaterEqual(returned["duration_ms"], 0)
        self.assert_hidden("request-query-secret", "request-auth-secret",
                           "request-password-secret", "response-token-secret", "response-cookie-secret")

    def test_http_disabled_trace_still_calls_once_without_inspecting_response(self):
        self.configure(trace=False)
        response = object()
        client = Mock(spec=["get"])
        client.get.return_value = response
        self.assertIs(obs.http_request(client, "get", "https://service.test/p", timeout=9), response)
        client.get.assert_called_once_with("https://service.test/p", timeout=9)
        self.assertEqual(self.events(), [])

    def test_http_authentication_endpoints_hide_whole_request_and_response_bodies(self):
        self.configure()
        routes = (
            "/api/auth/exchange", "/api/hatch/token", "/api/hatch/noise-notary-token",
            "/app/getAppAccessToken", "/credentials/update", "/capture", "/aymh",
        )
        for route in routes:
            with self.subTest(route=route):
                url = "https://service.test" + route
                response = self.response({"unknown": "auth-response-body-secret"}, url=url)
                client = Mock(spec=["post"])
                client.post.return_value = response
                self.assertIs(obs.http_request(client, "post", url, json={
                    "unknown": "auth-request-body-secret",
                }), response)
                client.post.assert_called_once()
        events = self.events()
        self.assertEqual(len(events), len(routes) * 2)
        self.assertTrue(all(e["payload"] == REDACTED for e in events))
        self.assert_hidden("auth-request-body-secret", "auth-response-body-secret")

    def test_http_unconsumed_stream_is_not_read_or_traced_as_a_body(self):
        self.configure()
        stream = CountingStream(b"unconsumed-body-secret")
        response = httpx.Response(200, stream=stream, request=httpx.Request("GET", "https://service.test/p"))
        self.addCleanup(response.close)
        client = Mock(spec=["get"])
        client.get.return_value = response
        with patch.object(response, "read", wraps=response.read) as read:
            self.assertIs(obs.http_request(client, "get", "https://service.test/p"), response)
            read.assert_not_called()
        client.get.assert_called_once_with("https://service.test/p")
        self.assertEqual(stream.iterations, 0)
        self.assertFalse(response.is_stream_consumed)
        self.assertEqual(self.events()[-1]["event"], "response")
        self.assertIn("omitted", self.events()[-1]["payload"])
        self.assert_hidden("unconsumed-body-secret")

    def test_http_prebuffered_body_is_traced_without_an_extra_read(self):
        self.configure()
        response = self.response({"public": "already-buffered"})
        client = Mock(spec=["get"])
        client.get.return_value = response
        with patch.object(response, "read", wraps=response.read) as read:
            self.assertIs(obs.http_request(client, "get", "https://service.test/p"), response)
            read.assert_not_called()
        self.assertEqual(self.events()[-1]["payload"], {"public": "already-buffered"})

    def test_http_consumed_but_unbuffered_stream_keeps_response_metadata_without_read(self):
        self.configure()
        stream = CountingStream(b"consumed-uncached-body-secret")
        response = httpx.Response(200, stream=stream, request=httpx.Request("GET", "https://service.test/p"))
        self.addCleanup(response.close)
        self.assertEqual(b"".join(response.iter_bytes()), stream.body)
        self.assertTrue(response.is_stream_consumed)
        client = Mock(spec=["get"])
        client.get.return_value = response
        with patch.object(response, "read", wraps=response.read) as read:
            self.assertIs(obs.http_request(client, "get", "https://service.test/p"), response)
            read.assert_not_called()
        self.assertEqual(stream.iterations, 1)
        events = self.events()
        self.assertEqual([e["event"] for e in events], ["request", "response"])
        self.assertEqual(events[-1]["status"], 200)
        self.assertTrue("payload" not in events[-1] or "omitted" in events[-1]["payload"])
        self.assert_hidden("consumed-uncached-body-secret")

    def test_http_original_exception_is_not_wrapped_swallowed_or_retried(self):
        self.configure()
        error = RuntimeError("http-error-body-secret")
        client = Mock(spec=["post"])
        client.post.side_effect = error
        with self.assertRaises(RuntimeError) as raised:
            obs.http_request(client, "post", "https://service.test/p", content=b"{}")
        self.assertIs(raised.exception, error)
        client.post.assert_called_once_with("https://service.test/p", content=b"{}")
        self.assertEqual([e["event"] for e in self.events()], ["request", "error"])
        self.assert_hidden("http-error-body-secret")

    def test_http_redirect_metadata_redacts_signed_url_and_cookie(self):
        self.configure()
        response = self.response()
        response.history = [httpx.Response(302, headers={
            "Set-Cookie": "session=redirect-cookie-secret",
            "Location": "https://target.test/p?sig=location-secret#location-fragment",
        }, request=httpx.Request("GET",
            "https://redirect-user:redirect-pass@origin.test/p?sig=redirect-query#redirect-fragment"))]
        client = Mock(spec=["get"])
        client.get.return_value = response
        self.assertIs(obs.http_request(client, "get", "https://service.test/p"), response)
        redirects = [e for e in self.events() if e["event"] == "redirect"]
        self.assertEqual(len(redirects), 1)
        self.assertEqual(redirects[0]["status"], 302)
        self.assert_hidden("redirect-cookie-secret", "location-secret", "location-fragment",
                           "redirect-user", "redirect-pass", "redirect-query", "redirect-fragment")

    def test_file_write_failure_fixed_notice_and_business_identity_are_preserved(self):
        self.configure()
        # Open the genuine temp-file sinks, then fail only their file writes.
        self.logger.info("warm-runtime")
        obs.trace_event("test", "warm-trace")
        handlers = [h for h in logging.getLogger("muse_bridge").handlers
                    if isinstance(h, logging.FileHandler)]
        self.assertEqual(len(handlers), 2)
        result = object()
        error = ValueError("original-business-error-secret")
        good = Mock(return_value=result)
        bad = Mock(side_effect=error)

        @obs.observed("test.disk")
        def success():
            return good()

        @obs.observed("test.disk")
        def failure():
            return bad()

        with ExitStack() as stack:
            stack.enter_context(patch("sys.stderr", self.stderr))
            for handler in handlers:
                stack.enter_context(patch.object(handler.stream, "write", side_effect=OSError("disk-write-secret")))
            self.logger.warning("failed-runtime-write")
            self.logger.warning("failed-runtime-write-again")
            self.assertIs(success(), result)
            with self.assertRaises(ValueError) as raised:
                failure()
            self.assertIs(raised.exception, error)

            async_good = AsyncMock(return_value=result)
            async_bad = AsyncMock(side_effect=error)

            @obs.observed("test.disk.async")
            async def async_success():
                return await async_good()

            @obs.observed("test.disk.async")
            async def async_failure():
                return await async_bad()

            async def scenario():
                self.assertIs(await async_success(), result)
                with self.assertRaises(ValueError) as raised:
                    await async_failure()
                self.assertIs(raised.exception, error)

            asyncio.run(scenario())
            async_good.assert_awaited_once_with()
            async_bad.assert_awaited_once_with()
            response = self.response()
            client = Mock(spec=["get", "post"])
            client.get.return_value = response
            client.post.side_effect = error
            self.assertIs(obs.http_request(client, "get", "https://service.test/p"), response)
            with self.assertRaises(ValueError) as raised:
                obs.http_request(client, "post", "https://service.test/p")
            self.assertIs(raised.exception, error)
            client.get.assert_called_once_with("https://service.test/p")
            client.post.assert_called_once_with("https://service.test/p")
        good.assert_called_once_with()
        bad.assert_called_once_with()
        lines = self.stderr.getvalue().splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(line == FAILURE_NOTICE for line in lines))
        self.assertLessEqual(len(lines), len(handlers))
        self.assert_hidden("disk-write-secret", "original-business-error-secret")
        obs.trace_event("test", "recovered")
        self.assertEqual(self.events()[-1]["event"], "recovered")

    def test_bad_log_destination_does_not_prevent_configuration_or_business_call(self):
        blocker = self.directory / "not-a-directory"
        blocker.write_text("blocker", encoding="utf-8")
        self.configure(file=str(blocker / "bridge.log"), trace_file=str(blocker / "trace.jsonl"))
        result = object()
        business = Mock(return_value=result)

        @obs.observed("test.disk")
        def operation():
            return business()

        self.assertIs(operation(), result)
        business.assert_called_once_with()
        self.assertTrue(self.stderr.getvalue().splitlines())
        self.assertTrue(all(line == FAILURE_NOTICE for line in self.stderr.getvalue().splitlines()))
        self.assertNotIn(str(blocker), self.stderr.getvalue())

    def test_third_party_raw_messages_args_and_exception_are_not_in_project_outputs(self):
        self.configure(console=True)
        names = ("httpx", "httpcore.connection", "websockets.client")
        for name in names:
            try:
                raise ValueError("dependency-exception-secret")
            except ValueError:
                logging.getLogger(name).warning(
                    "004321 %s https://u:p@service.test/p?q=query-secret#fragment-secret",
                    "dependency-opaque-secret", exc_info=True, stack_info=True,
                )
        events = [e for e in self.events() if e["component"] == "dependency"]
        self.assertEqual(len(events), len(names))
        self.assertCountEqual([e["logger"] for e in events], names)
        self.assertTrue(all(e["error"]["type"] == "ValueError" for e in events))
        self.assert_hidden("004321", "dependency-opaque-secret", "dependency-exception-secret",
                           "query-secret", "fragment-secret", "https://u:p@")

    def test_existing_third_party_handlers_cannot_output_original_secrets(self):
        independent = []
        for name in ("httpx", "httpx._client", "httpcore", "httpcore.connection",
                     "websockets", "websockets.client"):
            path = self.directory / (name + "-independent.log")
            handler = logging.FileHandler(path, encoding="utf-8")
            logging.getLogger(name).addHandler(handler)
            self.addCleanup(handler.close)
            independent.append((name, path, handler))
        self.configure(console=True)
        for name, _, _ in independent:
            logging.getLogger(name).warning("004321 %s", "preexisting-handler-secret")
        for _, _, handler in independent:
            handler.flush()
        independent_output = "".join(self.read_text(path) for _, path, _ in independent)
        self.assert_hidden("004321", "preexisting-handler-secret",
                           output=independent_output + self.all_output())


if __name__ == "__main__":
    unittest.main()
