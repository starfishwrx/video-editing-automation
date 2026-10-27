"""有总期限、可恢复重试预算的同步视觉请求边界。

这里拥有 HTTP 重试；业务层只消费最终 content 和结构化错误。文件锁同时
串行化同一逻辑请求的恢复，避免两个任务各自得到一套重试预算。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import random
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

import aiohttp

from .file_safety import atomic_write_text, file_lock
from .json_extract import is_blank
from .runtime_policy import active_vision_slots, load_policy, vision_slot


_SCHEMA = 1
_EMPTY_ERRORS = {"EMPTY_HTTP_BODY", "NO_CHOICES", "EMPTY_CONTENT"}
_TRANSIENT_HTTP = {500, 502, 503, 504}
_MIN_START_BUDGET = 0.02


@dataclass
class VisionCallResult:
    ok: bool
    text: str = ""
    error_code: str = ""
    http_status: int | None = None
    request_id: str = ""
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    retryable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def legacy_tuple(self) -> tuple[bool, str]:
        if self.ok:
            return True, self.text
        # 旧调用方用 empty response 判断待恢复；保留它，但不再暴露响应正文。
        detail = "vision model returned an empty response" if self.error_code in _EMPTY_ERRORS else "vision request failed"
        return False, f"{detail} [{self.error_code}]"


def _deadline(value: float | None, name: str) -> float | None:
    raw = value if value is not None else os.environ.get(name, "")
    try:
        number = float(raw)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _remaining(deadlines: tuple[float | None, ...]) -> float:
    values = [value - time.time() for value in deadlines if value is not None]
    return max(0.0, min(values)) if values else float("inf")


def _identity(url: str, body: dict) -> str:
    serialized = json.dumps({"url": url, "body": body}, ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _usage(value: Any, depth: int = 0) -> dict:
    if not isinstance(value, dict) or depth > 2:
        return {}
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.replace("_", "").isalnum():
            continue
        if isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item):
            result[key] = item
        elif isinstance(item, dict):
            result[key] = _usage(item, depth + 1)
    return result


def _retry_after(value: str | None, now: float | None = None) -> float | None:
    if not value:
        return None
    current = time.time() if now is None else now
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, parsed.timestamp() - current)
        except (TypeError, ValueError, OverflowError):
            return None


def _content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(block.get("text", "") for block in value
                       if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
                       and isinstance(block.get("text"), str))
    return ""


def _final_json(text: str) -> Any:
    raw = text.replace("\ufeff", "").strip()
    if "```" in raw:
        # 只允许完整围栏；未闭合围栏不能靠里面恰好完整的子对象蒙混过关。
        fence = re.search(r"```(?:json|JSON|javascript|js)?[ \t]*\r?\n?(.*?)```", raw, re.S)
        if fence is None or any(char in raw[:fence.start()] for char in "{[") or any(
            char in raw[fence.end():] for char in "{}[]"
        ):
            raise ValueError("incomplete JSON fence")
        raw = fence.group(1).strip()
    positions = [position for position in (raw.find("{"), raw.find("[")) if position >= 0]
    if not positions:
        raise ValueError("no final JSON")
    # 第一个顶层结构损坏就是损坏，绝不扫描它内部可解码的子树。
    value, end = json.JSONDecoder().raw_decode(raw, min(positions))
    if any(char in raw[end:] for char in "{}[]"):
        raise ValueError("multiple final JSON values")
    if not isinstance(value, dict) and not (
        isinstance(value, list) and all(isinstance(item, (dict, list)) for item in value)
    ):
        raise ValueError("expected structured JSON")
    return value


def _validate(text: str, validator: Callable[[Any], Any] | None) -> str:
    try:
        parsed = _final_json(text)
        json.dumps(parsed, allow_nan=False)
    except ValueError:
        return "INVALID_JSON"
    if validator is not None:
        try:
            if validator(parsed) is False:
                return "SCHEMA_INVALID"
        except Exception:
            return "SCHEMA_INVALID"
    return ""


def _parse_response(raw: bytes, status: int, headers: dict[str, str],
                    validator: Callable[[Any], Any] | None) -> VisionCallResult:
    metadata: dict[str, Any] = {"response_bytes": len(raw)}
    request_id = headers.get("x-request-id") or headers.get("x-tt-logid") or ""
    result = VisionCallResult(False, http_status=status, request_id=request_id,
                              metadata=metadata)
    try:
        payload = json.loads(raw) if raw.strip() else None
    except (ValueError, UnicodeError):
        payload = None
    if isinstance(payload, dict):
        result.usage = _usage(payload.get("usage"))
        if not request_id and isinstance(payload.get("id"), str):
            result.request_id = payload["id"][:256]
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    error_type = " ".join(str(error.get(key, "")) for key in ("code", "type")) if isinstance(error, dict) else ""
    compact_error = re.sub(r"[^a-z0-9]", "", error_type.lower())
    balance_error = any(code in compact_error for code in (
        "insufficientquota", "insufficientbalance", "insufficientfunds", "insufficientcredit",
        "balancenotenough", "balanceexhausted", "creditexhausted", "paymentrequired",
        "billinghardlimit", "arrearage",
    ))
    if status in {401, 403} or any(code in compact_error for code in ("invalidapikey", "authentication", "unauthorized")):
        result.error_code = "AUTHENTICATION"
    elif status == 402 or balance_error:
        result.error_code = "BALANCE"
    elif status == 429:
        result.error_code, result.retryable = "HTTP_RATE_LIMIT", True
    elif status in _TRANSIENT_HTTP:
        result.error_code, result.retryable = "HTTP_SERVER_ERROR", True
    elif status >= 400:
        result.error_code = "INVALID_REQUEST" if status == 400 else "HTTP_ERROR"
    elif not raw.strip():
        result.error_code, result.retryable = "EMPTY_HTTP_BODY", True
    elif not isinstance(payload, dict):
        result.error_code, result.retryable = "INVALID_RESPONSE_JSON", True
    else:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            result.error_code, result.retryable = "NO_CHOICES", True
        else:
            choice = choices[0]
            result.finish_reason = str(choice.get("finish_reason") or "")[:80]
            message = choice.get("message")
            message = message if isinstance(message, dict) else {}
            final = _content(message.get("content"))
            metadata["content_chars"] = len(final)
            metadata["reasoning_chars"] = len(_content(message.get("reasoning_content")))
            metadata["refusal_present"] = not is_blank(message.get("refusal"))
            if metadata["refusal_present"] or result.finish_reason in {"content_filter", "safety"}:
                result.error_code = "REFUSAL"
            elif result.finish_reason == "length":
                result.error_code = "OUTPUT_TRUNCATED"
            elif is_blank(final):
                result.error_code, result.retryable = "EMPTY_CONTENT", True
            else:
                result.error_code = _validate(final, validator)
                if result.error_code:
                    result.retryable = True
                else:
                    result.ok, result.text = True, final
    delay = _retry_after(headers.get("retry-after"))
    if delay is not None:
        metadata["retry_after_s"] = delay
    return result


async def _http(url: str, api_key: str, body: dict, timeout: float,
                validator: Callable[[Any], Any] | None) -> VisionCallResult:
    started = time.monotonic()
    session = aiohttp.ClientSession(
        # aiohttp 默认会把 >=5 秒的期限向整秒上取整；跨层预算不能多借这一秒。
        timeout=aiohttp.ClientTimeout(total=timeout, ceil_threshold=float("inf")), trust_env=False,
    )
    result = VisionCallResult(False)
    try:
        async with session.post(url, json=body, headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
        }) as response:
            headers = {key.lower(): value for key, value in response.headers.items()}
            result.http_status = response.status
            result.request_id = headers.get("x-request-id") or headers.get("x-tt-logid") or ""
            raw = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                raw.extend(chunk)
                result.metadata["response_bytes"] = len(raw)
            result = _parse_response(bytes(raw), response.status, headers,
                                     validator)
    except (asyncio.TimeoutError, TimeoutError):
        result.error_code, result.retryable = "REQUEST_TIMEOUT", True
    except (aiohttp.ClientError, OSError):
        result.error_code, result.retryable = "NETWORK_ERROR", True
    finally:
        try:
            # 连接关闭完成后协程才返回；外层随后才归还账户槽位。
            await session.close()
            result.metadata["cleanup_completed"] = True
        except Exception:
            result.ok, result.text = False, ""
            result.error_code, result.retryable = "CLEANUP_FAILED", False
            result.metadata["cleanup_completed"] = False
        result.metadata["elapsed_s"] = round(time.monotonic() - started, 6)
    return result


def _save(path: Path, ledger: dict) -> None:
    atomic_write_text(path, json.dumps(ledger, ensure_ascii=False, indent=2, allow_nan=False))


def _record(result: VisionCallResult) -> dict:
    # 成功正文在 output 文件，账本只保存诊断。不可序列化 prompt/图像/key/思考。
    data = asdict(result)
    data.pop("text")
    return data


def _emit_metric(result: VisionCallResult) -> None:
    """Best-effort bounded diagnostic row, without prompt, image or credentials."""
    try:
        path = Path(load_policy().slot_dir) / time.strftime("vision_requests_%Y%m%d.jsonl", time.gmtime())
        with file_lock(path, timeout=1):
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(_record(result), ensure_ascii=False, allow_nan=False) + "\n")
    except (OSError, TimeoutError, ValueError):
        pass


def _restore(data: dict) -> VisionCallResult:
    if not isinstance(data, dict):
        raise ValueError("invalid result ledger")
    names = VisionCallResult.__dataclass_fields__
    return VisionCallResult(**{key: value for key, value in data.items() if key in names})


def _retry_body(body: dict, result: VisionCallResult) -> dict | None:
    if result.error_code == "OUTPUT_TRUNCATED":
        if os.environ.get("JY_VISION_LENGTH_RETRY_VERIFIED") != "1" or body.get("max_tokens") != 16384:
            return None
        retry = copy.deepcopy(body)
        retry["max_tokens"] = 32768
        return retry
    if not result.retryable:
        return None
    if result.error_code in {"INVALID_JSON", "SCHEMA_INVALID"}:
        retry = copy.deepcopy(body)
        messages = retry.get("messages")
        if not isinstance(messages, list):
            return None
        messages.append({"role": "user", "content": "上次最终回答未通过 JSON 格式或结构校验。请严格按原要求重新给出完整合法 JSON；只输出最终 JSON，不要解释、代码围栏或中间草稿。"})
        return retry
    return body


def _finish(ledger: dict, path: Path, result: VisionCallResult, *, terminal: bool) -> VisionCallResult:
    result.attempts = int(ledger["attempts"])
    if terminal:
        result.retryable = False
    result.metadata["history"] = copy.deepcopy(ledger["history"])
    # history 内没有再嵌套 history，保持账本大小随请求次数有界。
    ledger["last_result"] = _record(result)
    ledger["status"] = "success" if result.ok else "failed"
    ledger["updated_at"] = time.time()
    _save(path, ledger)
    return result


def post_chat(url: str, api_key: str, body: dict, output: Path, *, timeout: float = 360,
              max_attempts: int = 2, stage_deadline: float | None = None,
              task_deadline: float | None = None,
              validator: Callable[[Any], Any] | None = None) -> VisionCallResult:
    """调用并保存最终 JSON；validator 收到已解析 JSON，False/异常表示校验失败。

    deadline 使用 epoch 秒，可跨进程传递；timeout 是单次 HTTP 的总期限。
    同一 URL/body 的账本永久保留已消耗次数，恢复只使用剩余预算。同步接口
    用于已有同步调用方/线程池；若处于异步事件循环内，明确失败而不遗弃线程。
    """
    started = time.monotonic()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return VisionCallResult(False, error_code="EVENT_LOOP_ACTIVE")
    try:
        request_timeout = float(timeout)
        attempt_limit = max(1, min(2, int(max_attempts)))
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("timeout must be positive")
        identity = _identity(url, body)
    except (TypeError, ValueError):
        return VisionCallResult(False, error_code="INVALID_REQUEST")
    deadlines = (_deadline(stage_deadline, "JY_STAGE_DEADLINE_TS"),
                 _deadline(task_deadline, "JY_TASK_DEADLINE_TS"))
    remaining = _remaining(deadlines)
    output = Path(output)
    ledger_path = output.with_suffix(".call.json")
    try:
        with file_lock(ledger_path, timeout=min(remaining, request_timeout * attempt_limit + 30)):
            try:
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                if not isinstance(ledger, dict) or ledger.get("schema") != _SCHEMA:
                    return VisionCallResult(False, error_code="LEDGER_INVALID")
            except FileNotFoundError:
                ledger = {}
            except (ValueError, OSError):
                return VisionCallResult(False, error_code="LEDGER_INVALID")
            if ledger.get("identity") != identity:
                ledger = {"schema": _SCHEMA, "identity": identity, "attempts": 0,
                          "attempt_limit": attempt_limit, "history": [], "status": "pending"}
            try:
                attempts = int(ledger["attempts"])
                limit = min(attempt_limit, int(ledger["attempt_limit"]), 2)
                if attempts < 0 or limit < 1 or not isinstance(ledger["history"], list):
                    raise ValueError("invalid attempt ledger")
                previous = _restore(ledger.get("last_result", {"ok": False}))
            except (TypeError, ValueError, KeyError):
                return VisionCallResult(False, error_code="LEDGER_INVALID")
            if ledger.get("status") == "success":
                try:
                    text = output.read_text(encoding="utf-8")
                    if _validate(text, validator):
                        raise ValueError("cached final content invalid")
                except (OSError, ValueError):
                    return VisionCallResult(False, error_code="CACHED_OUTPUT_INVALID", attempts=attempts)
                previous.ok, previous.text, previous.retryable = True, text, False
                previous.attempts = attempts
                previous.metadata["cache_hit"] = True
                return previous
            if attempts >= limit:
                if not previous.error_code:
                    previous.error_code = "RETRY_BUDGET_EXHAUSTED"
                return _finish(ledger, ledger_path, previous, terminal=True)
            request_body = body
            if attempts:
                # 槽位/期限等待也会保存 last_result；恢复动作仍以最后一个真正的
                # HTTP 结果决定，不能把格式修复或定向 token 重试退回原样重复。
                retry_source = _restore(ledger["history"][-1]) if ledger["history"] else previous
                if ledger.get("status") == "in_progress":
                    # 上个进程已在网络前记账，崩溃恢复只能消耗剩余的一次。
                    retry_source = VisionCallResult(False, error_code="INTERRUPTED_ATTEMPT", retryable=True)
                request_body = _retry_body(body, retry_source)
                if request_body is None:
                    return _finish(ledger, ledger_path, previous, terminal=True)
            while int(ledger["attempts"]) < limit:
                remaining = _remaining(deadlines)
                if remaining < min(_MIN_START_BUDGET, request_timeout):
                    result = previous if int(ledger["attempts"]) else VisionCallResult(False, error_code="DEADLINE_EXCEEDED")
                    result.metadata["retry_blocked"] = "deadline"
                    return _finish(ledger, ledger_path, result, terminal=False)
                # 退避的到期时间也持久化；恢复不得跳过 Retry-After。
                delay = max(0.0, float(ledger.get("retry_not_before", 0)) - time.time())
                if delay:
                    # 没有调用方阶段期限时也不做无界 Retry-After 等待；账本保留
                    # 下次允许请求的时刻，调度器可以稍后按阶段恢复。
                    wait_budget = remaining if math.isfinite(remaining) else request_timeout
                    if delay >= wait_budget:
                        previous.metadata["retry_blocked"] = "deadline"
                        return _finish(ledger, ledger_path, previous, terminal=False)
                    time.sleep(delay)
                    ledger["wait_s"] = float(ledger.get("wait_s", 0)) + delay
                remaining = _remaining(deadlines)
                try:
                    with vision_slot(timeout=min(request_timeout, remaining)) as handle:
                        if not handle.acquired:
                            result = VisionCallResult(False, error_code="LIMIT_WAIT_TIMEOUT", retryable=True,
                                                      metadata={"slot_wait_s": float(getattr(handle, "waited", 0))})
                            return _finish(ledger, ledger_path, result, terminal=False)
                        budget = min(request_timeout, _remaining(deadlines))
                        if budget < min(_MIN_START_BUDGET, request_timeout):
                            result = previous if int(ledger["attempts"]) else VisionCallResult(False, error_code="DEADLINE_EXCEEDED")
                            result.metadata["retry_blocked"] = "deadline"
                            return _finish(ledger, ledger_path, result, terminal=False)
                        ledger["attempts"] = int(ledger["attempts"]) + 1
                        ledger["status"], ledger["updated_at"] = "in_progress", time.time()
                        _save(ledger_path, ledger)
                        inflight = active_vision_slots()
                        request_started = time.time()
                        result = asyncio.run(_http(url, api_key, request_body, budget, validator))
                        result.attempts = int(ledger["attempts"])
                        result.metadata.update(inflight=inflight, started_at=request_started, finished_at=time.time())
                        result.metadata["slot_wait_s"] = float(getattr(handle, "waited", 0))
                        result.metadata["http_budget_s"] = budget
                except (ValueError, OSError, RuntimeError):
                    result = VisionCallResult(False, error_code="RUNTIME_CONFIGURATION", attempts=int(ledger["attempts"]))
                result.metadata["total_elapsed_s"] = round(time.monotonic() - started, 6)
                _emit_metric(result)
                ledger["history"].append(_record(result))
                if result.ok:
                    atomic_write_text(output, result.text)
                    return _finish(ledger, ledger_path, result, terminal=True)
                request_body = _retry_body(body, result)
                if request_body is None or int(ledger["attempts"]) >= limit:
                    return _finish(ledger, ledger_path, result, terminal=True)
                delay = result.metadata.get("retry_after_s")
                if delay is None:
                    delay = random.uniform(0.25, 1.0)
                ledger["retry_not_before"] = time.time() + max(0.0, float(delay))
                previous = result
                _finish(ledger, ledger_path, result, terminal=False)
            return _finish(ledger, ledger_path, previous, terminal=True)
    except TimeoutError:
        return VisionCallResult(False, error_code="LEDGER_WAIT_TIMEOUT", retryable=True)
    except OSError:
        return VisionCallResult(False, error_code="LEDGER_IO_ERROR")
