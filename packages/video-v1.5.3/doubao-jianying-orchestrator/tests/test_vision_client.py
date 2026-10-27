from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import vision_client as client


def answer(content='{"ok": true}', *, reason="stop", reasoning="", refusal=None):
    message = {"content": content, "reasoning_content": reasoning}
    if refusal is not None:
        message["refusal"] = refusal
    return {"id": "chat-test", "choices": [{"message": message, "finish_reason": reason}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 12,
                      "completion_tokens_details": {"reasoning_tokens": 10}}}


class LocalHTTP:
    """真实本机 socket；慢滴流即使一直有字节，也必须被客户端总期限取消。"""

    def __init__(self, *actions):
        self.actions = deque(actions)
        self.calls = []
        self.disconnected = threading.Event()
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with fixture.lock:
                    fixture.calls.append(body)
                    action = fixture.actions.popleft() if fixture.actions else {"payload": answer()}
                    fixture.active += 1
                    fixture.max_active = max(fixture.active, fixture.max_active)
                try:
                    if action.get("disconnect"):
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.close_connection = True
                        return
                    payload = action.get("payload", answer())
                    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                    self.send_response(action.get("status", 200))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("X-Request-Id", "request-test")
                    for name, value in action.get("headers", {}).items():
                        self.send_header(name, value)
                    self.end_headers()
                    if action.get("drip"):
                        for byte in raw:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.025)
                    else:
                        time.sleep(action.get("delay", 0))
                        self.wfile.write(raw)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    fixture.disconnected.set()
                    self.close_connection = True
                finally:
                    with fixture.lock:
                        fixture.active -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}/chat/completions"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)


@contextmanager
def available_slot(timeout=None):
    yield SimpleNamespace(acquired=True, waited=0.0)


class VisionClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "result.json"
        self.body = {"model": "offline-test", "max_tokens": 16384,
                     "messages": [{"role": "user", "content": "offline prompt"}]}
        self.patches = [patch.object(client, "vision_slot", available_slot),
                        patch.object(client.random, "uniform", return_value=0),
                        patch.dict("os.environ", {"JY_STAGE_DEADLINE_TS": "", "JY_TASK_DEADLINE_TS": "",
                                                  "JY_CONCURRENCY_POLICY_FILE": "", "JY_SLOT_DIR": str(self.root / "slots"),
                                                  "JY_VISION_LENGTH_RETRY_VERIFIED": "0"})]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    def call(self, server, **kwargs):
        return client.post_chat(server.url, "test-key-must-not-be-logged", self.body,
                                self.output, timeout=1, **kwargs)

    def test_real_http_calls_share_account_capacity_across_entry_threads(self):
        from orchestrator import runtime_policy
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({"vision_slots": 2, "slot_dir": str(self.root / "real-slots")}))
        with LocalHTTP(*[{"delay": .08} for _ in range(6)]) as server, \
                patch.object(client, "vision_slot", runtime_policy.vision_slot), \
                patch.dict("os.environ", {"JY_CONCURRENCY_POLICY_FILE": str(policy), "JY_VISION_SLOTS": "160"}):
            def call(index):
                return client.post_chat(server.url, "synthetic-key", self.body,
                    self.root / f"call-{index}.json", timeout=2, max_attempts=1)
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(call, range(6)))
            self.assertTrue(all(result.ok for result in results))
            self.assertEqual(server.max_active, 2)
            self.assertEqual(server.active, 0)
            self.assertLessEqual(max(result.metadata["inflight"] for result in results), 2)

    def ledger(self):
        return json.loads(self.output.with_suffix(".call.json").read_text(encoding="utf-8"))

    def test_success_saves_only_final_and_reuses_same_identity(self):
        with LocalHTTP({"payload": answer(reasoning="secret reasoning")}) as server:
            self.body["messages"][0]["content"] = "secret-base64-prompt"
            first = self.call(server)
            second = self.call(server)
        self.assertTrue(first.ok)
        self.assertEqual(first.request_id, "request-test")
        self.assertEqual(first.usage["completion_tokens"], 12)
        self.assertTrue(second.metadata["cache_hit"])
        self.assertEqual(len(server.calls), 1)
        self.assertEqual(self.output.read_text(encoding="utf-8"), '{"ok": true}')
        text = self.output.with_suffix(".call.json").read_text(encoding="utf-8")
        for secret in ("secret reasoning", "secret-base64-prompt", "test-key-must-not-be-logged"):
            self.assertNotIn(secret, text)

    def test_reasoning_json_cannot_replace_empty_final(self):
        action = {"payload": answer("", reasoning='{"ok": true}')}
        with LocalHTTP(action, action) as server:
            result = self.call(server)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "EMPTY_CONTENT")
        self.assertEqual(result.attempts, 2)
        self.assertFalse(result.retryable)
        self.assertIn("empty response", result.legacy_tuple()[1])
        self.assertFalse(self.output.exists())

    def test_slow_drip_cancel_closes_client_before_slot_returns(self):
        releases = []

        @contextmanager
        def observed_slot(timeout=None):
            yield SimpleNamespace(acquired=True, waited=0.0)
            releases.append(time.monotonic())

        with LocalHTTP({"drip": True}) as server, patch.object(client, "vision_slot", observed_slot):
            started = time.monotonic()
            result = client.post_chat(server.url, "unused", self.body, self.output,
                                      timeout=0.15, max_attempts=1)
            elapsed = time.monotonic() - started
            self.assertTrue(server.disconnected.wait(1), "服务器必须观察到客户端连接关闭")
        self.assertEqual(result.error_code, "REQUEST_TIMEOUT")
        self.assertLess(elapsed, 0.8)
        self.assertEqual(len(releases), 1)
        self.assertTrue(result.metadata["cleanup_completed"])
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.request_id, "request-test")
        self.assertGreater(result.metadata["response_bytes"], 0)
        self.assertEqual(server.active, 0)

    def test_network_disconnect_retries_once(self):
        with LocalHTTP({"disconnect": True}, {"payload": answer()}) as server:
            result = self.call(server)
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(self.ledger()["history"][0]["error_code"], "NETWORK_ERROR")
        metadata = self.ledger()["history"][0]["metadata"]
        self.assertIn("network_error_type", metadata)
        self.assertNotIn("test-key-must-not-be-logged", json.dumps(self.ledger()))

    def test_connector_diagnostics_keep_numeric_cause_without_exception_message(self):
        error = client.aiohttp.ClientConnectorError(
            SimpleNamespace(host="synthetic", port=443, ssl=True),
            OSError(10061, "secret-in-exception-message"))
        class Session:
            def post(self, *args, **kwargs):
                raise error
            async def close(self):
                pass
        with patch.object(client.aiohttp, "ClientSession", return_value=Session()):
            result = asyncio.run(client._http("https://synthetic", "secret-key", {}, 1, None))
        self.assertEqual(result.error_code, "NETWORK_ERROR")
        self.assertEqual(result.metadata["network_error_type"], "ClientConnectorError")
        self.assertEqual(result.metadata["network_errno"], 10061)
        self.assertNotIn("secret", json.dumps(result.metadata))

    def test_rate_limit_retry_after_and_persistent_attempt_cap(self):
        action = {"status": 429, "headers": {"Retry-After": "0"}, "payload": {"error": {"code": "rate_limit"}}}
        with LocalHTTP(action, action, {"payload": answer()}) as server:
            first = self.call(server, max_attempts=6)
            restored = self.call(server, max_attempts=6)
        self.assertEqual(len(server.calls), 2)
        self.assertEqual(first.error_code, "HTTP_RATE_LIMIT")
        self.assertEqual(restored.attempts, 2)
        self.assertFalse(restored.retryable)
        self.assertEqual(self.ledger()["attempts"], 2)

    def test_retry_after_seconds_and_http_date(self):
        self.assertEqual(client._retry_after("7", now=100), 7)
        stamp = format_datetime(datetime.fromtimestamp(120, timezone.utc), usegmt=True)
        self.assertEqual(client._retry_after(stamp, now=100), 20)
        self.assertEqual(client._retry_after(stamp, now=130), 0)
        self.assertIsNone(client._retry_after("invalid", now=100))

    def test_retry_after_does_not_exceed_stage_deadline_or_reset_on_resume(self):
        action = {"status": 429, "headers": {"Retry-After": "5"}, "payload": {"error": {"code": "rate_limit"}}}
        with LocalHTTP(action) as server:
            started = time.monotonic()
            first = self.call(server, stage_deadline=time.time() + 0.2)
            restored = self.call(server, stage_deadline=time.time() + 0.2)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(server.calls), 1)
        self.assertEqual(first.error_code, "HTTP_RATE_LIMIT")
        self.assertEqual(restored.attempts, 1)
        self.assertEqual(restored.metadata["retry_blocked"], "deadline")

    def test_stage_deadline_bounds_request_total_time(self):
        with LocalHTTP({"drip": True}) as server:
            started = time.monotonic()
            result = self.call(server, stage_deadline=time.time() + 0.15)
            self.assertTrue(server.disconnected.wait(1))
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertFalse(result.ok)
        self.assertEqual(len(server.calls), 1)
        self.assertEqual(result.attempts, 1)

    def test_resume_after_stage_timeout_only_has_one_remaining_http_attempt(self):
        with LocalHTTP({"drip": True}, {"payload": answer()}) as server:
            first = self.call(server, stage_deadline=time.time() + 0.15)
            self.assertTrue(server.disconnected.wait(1))
            restored = self.call(server, stage_deadline=time.time() + 2)
        self.assertEqual(first.error_code, "REQUEST_TIMEOUT")
        self.assertTrue(restored.ok)
        self.assertEqual(restored.attempts, 2)
        self.assertEqual(len(server.calls), 2)

    def test_resume_retains_format_repair_after_waiting_for_new_stage_budget(self):
        first_response = {"payload": answer("invalid JSON"), "headers": {"Retry-After": "0.15"}}
        with LocalHTTP(first_response, {"payload": answer()}) as server:
            first = self.call(server, stage_deadline=time.time() + 0.05)
            restored = self.call(server, stage_deadline=time.time() + 2)
        self.assertEqual(first.error_code, "INVALID_JSON")
        self.assertTrue(restored.ok)
        self.assertEqual(restored.attempts, 2)
        self.assertEqual(len(server.calls[1]["messages"]), 2)

    def test_cleanup_failure_stops_retry_even_when_http_response_is_good(self):
        session_class = client.aiohttp.ClientSession

        class ClosingFails:
            def __init__(self, *args, **kwargs):
                self.session = session_class(*args, **kwargs)

            def post(self, *args, **kwargs):
                return self.session.post(*args, **kwargs)

            async def close(self):
                await self.session.close()
                raise RuntimeError("synthetic cleanup failure after socket close")

        with LocalHTTP({"payload": answer()}) as server, patch.object(client.aiohttp, "ClientSession", ClosingFails):
            result = self.call(server)
        self.assertEqual(result.error_code, "CLEANUP_FAILED")
        self.assertFalse(result.retryable)
        self.assertFalse(result.metadata["cleanup_completed"])
        self.assertEqual(len(server.calls), 1)
        self.assertFalse(self.output.exists())

    def test_expired_stage_can_reuse_success_without_any_new_http(self):
        with LocalHTTP({"payload": answer()}) as server:
            first = self.call(server)
            restored = self.call(server, stage_deadline=0)
        self.assertTrue(first.ok)
        self.assertTrue(restored.ok)
        self.assertTrue(restored.metadata["cache_hit"])
        self.assertEqual(len(server.calls), 1)

    def test_expired_deadline_or_denied_slot_never_calls_http(self):
        @contextmanager
        def denied_slot(timeout=None):
            yield SimpleNamespace(acquired=False, waited=0.01)

        with LocalHTTP() as server:
            expired = self.call(server, task_deadline=time.time() - 1)
            with patch.object(client, "vision_slot", denied_slot):
                denied = self.call(server)
        self.assertEqual(expired.error_code, "DEADLINE_EXCEEDED")
        self.assertEqual(denied.error_code, "LIMIT_WAIT_TIMEOUT")
        self.assertEqual(denied.attempts, 0)
        self.assertEqual(len(server.calls), 0)

    def test_unverified_length_is_not_retried(self):
        with LocalHTTP({"payload": answer('{"ok":', reason="length")}) as server:
            result = self.call(server)
        self.assertEqual(len(server.calls), 1)
        self.assertEqual(result.error_code, "OUTPUT_TRUNCATED")
        self.assertFalse(self.output.exists())

    def test_verified_length_gets_one_targeted_token_retry(self):
        with LocalHTTP({"payload": answer('{"ok":', reason="length")}, {"payload": answer()}) as server:
            with patch.dict("os.environ", {"JY_VISION_LENGTH_RETRY_VERIFIED": "1"}):
                result = self.call(server)
        self.assertTrue(result.ok)
        self.assertEqual([body["max_tokens"] for body in server.calls], [16384, 32768])

    def test_refusal_auth_balance_and_bad_request_are_terminal(self):
        cases = [({"payload": answer("", refusal="cannot do this")}, "REFUSAL"),
                 ({"status": 401}, "AUTHENTICATION"),
                 ({"status": 429, "payload": {"error": {"code": "insufficient_quota"}}}, "BALANCE"),
                 ({"status": 400}, "INVALID_REQUEST")]
        for index, (action, code) in enumerate(cases):
            with self.subTest(code=code), LocalHTTP(action) as server:
                result = client.post_chat(server.url, "unused", self.body,
                                          self.root / f"terminal-{index}.json", timeout=1)
            self.assertEqual(result.error_code, code)
            self.assertEqual(len(server.calls), 1)

    def test_format_and_schema_repair_share_two_attempt_budget(self):
        with LocalHTTP({"payload": answer("invalid JSON")},
                       {"payload": answer('{"wrong": 1}')}, {"payload": answer()}) as server:
            result = self.call(server, validator=lambda obj: obj.get("ok") is True)
            restored = self.call(server, validator=lambda obj: obj.get("ok") is True)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "SCHEMA_INVALID")
        self.assertEqual(restored.attempts, 2)
        self.assertEqual(len(server.calls), 2)
        self.assertEqual(len(server.calls[1]["messages"]), 2)
        self.assertFalse(self.output.exists())

    def test_truncated_outer_json_cannot_be_replaced_by_complete_inner_subtree(self):
        broken = '{"shots":[{"id":1}]'
        with LocalHTTP({"payload": answer(broken)}, {"payload": answer(broken)}) as server:
            result = self.call(server)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_JSON")
        self.assertEqual(result.attempts, 2)
        self.assertFalse(self.output.exists())

    def test_final_json_allows_complete_fence_and_small_prose_but_rejects_incomplete_fence(self):
        cases = [('这是最终结果： {"ok": true} 已完成。', True),
                 ('结果如下：\n```json\n{"ok": true}\n```', True),
                 ('```json\n{"ok": true}', False)]
        for index, (text, expected) in enumerate(cases):
            with self.subTest(text=text), LocalHTTP({"payload": answer(text)}) as server:
                result = client.post_chat(server.url, "unused", self.body,
                                          self.root / f"fence-{index}.json", timeout=1, max_attempts=1)
            self.assertEqual(result.ok, expected)

    def test_429_quota_rate_limit_is_retried_without_being_misclassified_as_balance(self):
        limited = {"status": 429, "payload": {"error": {"code": "quota_rate_limit_exceeded", "type": "rate_limit_error"}}}
        with LocalHTTP(limited, {"payload": answer()}) as server:
            result = self.call(server)
        self.assertTrue(result.ok)
        self.assertEqual(len(server.calls), 2)
        self.assertEqual(self.ledger()["history"][0]["error_code"], "HTTP_RATE_LIMIT")

    def test_empty_wire_body_and_missing_choices_retry_once(self):
        for index, payload in enumerate((b"", {"choices": []})):
            with self.subTest(payload=payload), LocalHTTP({"payload": payload}, {"payload": answer()}) as server:
                result = client.post_chat(server.url, "unused", self.body,
                                          self.root / f"empty-{index}.json", timeout=1)
            self.assertTrue(result.ok)
            self.assertEqual(result.attempts, 2)

    def test_crashed_attempt_is_not_forgotten(self):
        ledger = {"schema": 1, "identity": client._identity("placeholder", self.body),
                  "attempts": 1, "attempt_limit": 2, "history": [], "status": "in_progress"}
        with LocalHTTP({"payload": answer()}) as server:
            ledger["identity"] = client._identity(server.url, self.body)
            self.output.with_suffix(".call.json").write_text(json.dumps(ledger), encoding="utf-8")
            result = self.call(server)
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(server.calls), 1)

    def test_same_output_concurrent_calls_share_one_http_request(self):
        # 使用真实跨线程文件锁，第二个调用必须在第一个成功后复用最终输出。
        results = []
        with LocalHTTP({"payload": answer()}) as server:
            threads = [threading.Thread(target=lambda: results.append(self.call(server))) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(len(server.calls), 1)

    def test_running_event_loop_fails_without_starting_background_thread(self):
        async def invoke():
            return client.post_chat("http://127.0.0.1:1", "unused", self.body, self.output)

        result = asyncio.run(invoke())
        self.assertEqual(result.error_code, "EVENT_LOOP_ACTIVE")
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
