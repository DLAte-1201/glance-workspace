"""Conservative provider protocol adapters; live transport is deliberately disabled.

Only terminal provider result text is a report. Assistant deltas, exit status,
local IDs, and a caller's provenance claim cannot manufacture completion.
These public parsers handle supplied evidence, never attest an execution.
"""
from __future__ import annotations

import copy
import hashlib
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

MAX_OUTPUT_BYTES = 8 * 1024 * 1024
PROVIDERS = ("claude", "antigravity", "codex")


class ExecutionApprovalRequired(PermissionError):
    """No approved live transport exists in this project."""


def load_provider_metadata(connections_path: str | Path = "CONNECTIONS.json") -> dict:
    """Read discovery metadata only; never probe binaries, credentials, or login."""
    try:
        data = json.loads(Path(connections_path).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
            raise ValueError("CONNECTIONS.json must contain a providers object")
        if any(not isinstance(record, dict) for record in data["providers"].values()):
            raise ValueError("Each CONNECTIONS.json provider record must be an object")
    except (OSError, ValueError, RecursionError) as exc:
        return {"providers": {}, "metadata_error": str(exc), "live_transport": "disabled"}
    return {**data, "live_transport": "disabled"}


def _provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ValueError("Unsupported provider: " + str(provider))
    return provider


def _strict_object(pairs: list[tuple[str, Any]]) -> dict:
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key: " + key)
        obj[key] = value
    return obj


def _invalid_constant(value: str) -> None:
    raise ValueError("invalid JSON constant: " + value)


def _decode(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_strict_object, parse_constant=_invalid_constant)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _frames(output: str | bytes, output_format: str) -> tuple[list[dict], list[str], str]:
    issues: list[str] = []
    if output_format not in ("auto", "json", "stream-json"):
        raise ValueError("output_format must be auto, json, or stream-json")
    if not isinstance(output, (str, bytes)):
        return [], ["output_must_be_text_or_bytes"], output_format
    if isinstance(output, bytes):
        try:
            output = output.decode("utf-8")
        except UnicodeDecodeError:
            return [], ["invalid_utf8"], output_format
    try:
        size = len(output.encode("utf-8"))
    except UnicodeEncodeError:
        return [], ["invalid_unicode"], output_format
    if size > MAX_OUTPUT_BYTES:
        return [], ["output_size_limit"], output_format
    if not output.strip():
        return [], ["empty_output"], output_format
    if output_format in ("auto", "json"):
        try:
            decoded = _decode(output)
        except (ValueError, RecursionError):
            if output_format == "json":
                return [], ["malformed_json"], "json"
        else:
            if not isinstance(decoded, dict):
                return [], ["frame_must_be_object"], "json"
            return [decoded], [], "json"
    frames = []
    for line_number, line in enumerate(output.splitlines(), 1):
        if not line.strip():
            continue
        try:
            decoded = _decode(line)
            if not isinstance(decoded, dict):
                raise ValueError("frame must be object")
            frames.append(decoded)
        except (ValueError, RecursionError):
            issues.append("malformed_frame:" + str(line_number))
    return frames, issues, "stream-json"


def normalize_output(
    provider: str,
    output: str | bytes,
    *,
    expected_conversation_id: str | None = None,
    local_run_id: str,
    exit_code: int | None = 0,
    provenance: str = "synthetic_fixture",
    output_format: str = "auto",
) -> dict:
    """Normalize supplied JSON/NDJSON without running a provider or retrying.

    A JSON result requires an expected external conversation ID. A stream can
    establish that ID with exactly one init frame. Any mismatching observed ID,
    malformed frame, duplicate terminal/init, or conflicting evidence makes the
    result uncertain. Requested model names are never substituted for evidence.
    ``actual_managed_execution`` supplied here is an unverified import claim.
    """
    _provider(provider)
    if not _string(local_run_id):
        raise ValueError("local_run_id must be nonempty")
    if expected_conversation_id is not None and not _string(expected_conversation_id):
        raise ValueError("expected_conversation_id must be nonempty when supplied")
    if provenance not in ("synthetic_fixture", "imported_unverified", "actual_managed_execution"):
        raise ValueError("Unsupported provenance")
    if provider == "codex":
        return _normalize_codex(output, expected_conversation_id=expected_conversation_id,
                                local_run_id=local_run_id, exit_code=exit_code,
                                provenance=provenance, output_format=output_format)
    frames, issues, actual_format = _frames(output, output_format)
    result: dict = {
        "provider": provider, "model": None, "effort": None, "local_run_id": local_run_id,
        "external_conversation_id": None, "status": "uncertain", "report": "",
        "error": None, "issues": issues, "exit_code": exit_code,
        "provenance": "synthetic_fixture" if provenance == "synthetic_fixture" else "imported_unverified",
        "provenance_claim": provenance, "attested": False, "output_format": actual_format,
        "retry_allowed": False, "terminal_result_count": 0,
        "provider_run_id": None, "started_at": None, "finished_at": None,
        "request": None, "response": None, "receipt_source": "supplied_output",
        "usage": None, "usage_semantics": "provider_reported_cumulative_session" if provider == "antigravity" else "provider_reported_terminal",
        "billed_amount": None,
    }
    init_ids: list[str] = []
    all_ids: list[str] = []
    models: list[str] = []
    efforts: list[str] = []
    terminal: list[tuple[str, str, str | None, str | None]] = []
    signals: list[str] = []
    seen_frame_ids: set[str] = set()
    init_count = 0
    saw_terminal = False

    for frame in frames:
        if saw_terminal:
            issues.append("frame_after_terminal")
        frame_id = frame.get("event_id") or frame.get("uuid")
        if frame_id is not None:
            if not _string(frame_id):
                issues.append("invalid_frame_id")
            elif frame_id in seen_frame_ids:
                issues.append("duplicate_frame_id")
            else:
                seen_frame_ids.add(frame_id)
        if provider == "claude":
            kind = frame.get("type")
            if not isinstance(kind, str):
                issues.append("missing_frame_type")
                continue
            cid_values = [frame.get("session_id")]
            is_init = kind == "system" and frame.get("subtype") == "init"
            is_terminal = kind == "result"
            if kind not in ("system", "assistant", "user", "result", "stream_event", "error", "cancelled", "canceled", "rate_limit_event", "tool_progress", "tool_use_summary", "auth_status"):
                issues.append("unknown_frame_type:" + kind)
            payload = frame
            models.append(frame.get("model"))
            efforts.append(frame.get("effort"))
            if kind == "assistant":
                message = frame.get("message")
                if not isinstance(message, dict):
                    issues.append("malformed_assistant_frame")
                else:
                    models.append(message.get("model"))
            if is_terminal:
                subtype = frame.get("subtype")
                is_error = frame.get("is_error", False)
                if type(is_error) is not bool:
                    issues.append("malformed_is_error")
                if subtype == "success" and is_error is True:
                    issues.append("conflicting_success_and_error")
                if subtype in ("cancelled", "canceled") or frame.get("status") in ("cancelled", "canceled"):
                    status = "cancelled"
                elif isinstance(subtype, str) and (subtype.startswith("error") or is_error is True):
                    status = "failed"
                elif subtype == "success" and is_error is False:
                    status = "completed"
                else:
                    status = "uncertain"
                    issues.append("unknown_terminal_status")
                report = frame.get("result", "")
                error = frame.get("errors", frame.get("error"))
            elif kind in ("error", "cancelled", "canceled"):
                signals.append("failed" if kind == "error" else "cancelled")
                result["provider_error"] = frame.get("error", frame.get("message", kind))
        else:
            # Antigravity's protocol is event based, not Claude's type/subtype.
            kind = frame.get("event")
            direct_json = actual_format == "json" and kind is None and "status" in frame
            if direct_json:
                kind = "result"
            if not isinstance(kind, str):
                issues.append("missing_frame_event")
                continue
            is_init = kind == "init"
            is_terminal = kind == "result"
            cid_values = [frame.get("conversation_id")]
            if kind not in ("init", "result", "step_update", "error", "cancelled", "canceled", "user", "message", "tool_call", "tool_result"):
                issues.append("unknown_frame_event:" + kind)
            payload = frame if direct_json else frame.get(kind)
            if isinstance(payload, dict) and not is_terminal:
                cid_values.append(payload.get("conversation_id"))
            if is_init:
                if not isinstance(payload, dict):
                    issues.append("malformed_init")
                else:
                    models.append(payload.get("model"))
                    efforts.append(payload.get("effort"))
                    cid_values.append(payload.get("conversation_id"))
            if is_terminal:
                if not isinstance(payload, dict):
                    issues.append("malformed_result")
                    payload = {}
                cid_values.append(payload.get("conversation_id"))
                if not _string(payload.get("conversation_id")):
                    issues.append("result_missing_conversation_id")
                models.append(payload.get("model"))
                efforts.append(payload.get("effort"))
                raw_status = payload.get("status")
                status = {"SUCCESS": "completed", "FAILED": "failed", "FAILURE": "failed", "ERROR": "failed", "CANCELLED": "cancelled", "CANCELED": "cancelled"}.get(raw_status, "uncertain") if isinstance(raw_status, str) else "uncertain"
                if status == "uncertain":
                    issues.append("unknown_terminal_status")
                report = payload.get("response", "")
                error = payload.get("error", payload.get("errors"))
            elif kind in ("error", "cancelled", "canceled"):
                signals.append("failed" if kind == "error" else "cancelled")
                result["provider_error"] = frame.get("error", payload if payload is not None else kind)

        for value in cid_values:
            if value is not None:
                if not _string(value):
                    issues.append("invalid_conversation_id")
                else:
                    all_ids.append(value)
        frame_cid = next((v for v in reversed(cid_values) if _string(v)), None)
        if is_init:
            init_count += 1
            if init_count > 1:
                issues.append("duplicate_init")
            if saw_terminal:
                issues.append("init_after_terminal")
            if frame_cid:
                init_ids.append(frame_cid)
            else:
                issues.append("init_missing_conversation_id")
        if is_terminal:
            saw_terminal = True
            if not frame_cid:
                issues.append("result_missing_conversation_id")
            if not isinstance(report, str):
                issues.append("report_must_be_text")
                report = ""
            if status == "completed" and not report.strip():
                issues.append("empty_report")
            if status == "completed" and error:
                issues.append("conflicting_success_and_error")
            terminal.append((status, report, frame_cid, error))
            if isinstance(payload.get("usage"), dict):
                result["usage"] = copy.deepcopy(payload["usage"])
            elif payload.get("usage") is not None:
                issues.append("malformed_usage")

    if any(m is not None and not _string(m) for m in models):
        issues.append("invalid_observed_model")
    observed_models = list(dict.fromkeys(m for m in models if _string(m)))
    if len(observed_models) > 1:
        issues.append("conflicting_models")
    elif observed_models:
        result["model"] = observed_models[0]
    if any(value is not None and not _string(value) for value in efforts):
        issues.append("invalid_observed_effort")
    observed_efforts = list(dict.fromkeys(value for value in efforts if _string(value)))
    if len(observed_efforts) > 1:
        issues.append("conflicting_efforts")
    elif observed_efforts:
        result["effort"] = observed_efforts[0]
    unique_ids = list(dict.fromkeys(all_ids))
    if len(unique_ids) == 1:
        result["external_conversation_id"] = unique_ids[0]
    elif len(unique_ids) > 1:
        issues.append("conflicting_conversation_ids")
    expected = expected_conversation_id or (init_ids[0] if init_ids else None)
    if expected and any(cid != expected for cid in all_ids):
        issues.append("conversation_mismatch")
    if not expected:
        issues.append("conversation_not_independently_matched")
    result["terminal_result_count"] = len(terminal)
    if len(terminal) > 1:
        issues.append("duplicate_terminal_result")
    if not terminal:
        issues.append("missing_terminal_result")
    if terminal and signals:
        issues.append("conflicting_failure_or_cancellation_signal")
    if type(exit_code) is not int or exit_code != 0:
        issues.append("process_not_confirmed_exit_zero")
    if len(terminal) == 1:
        status, report, cid, error = terminal[0]
        result["report"] = report
        if error is not None:
            result["provider_error"] = error
        if cid is None or cid != expected:
            issues.append("terminal_conversation_not_matched")
        if not issues:
            result["status"] = status
            if status in ("failed", "cancelled"):
                result["error"] = str(error) if error else "Provider reported " + status
    result["issues"] = list(dict.fromkeys(issues))
    if result["status"] == "uncertain":
        result["error"] = "; ".join(result["issues"]) or "Uncertain provider outcome"
    result["conversation_id"] = result["external_conversation_id"]
    result["response"] = result["report"]
    result["run_status"] = {"completed": "succeeded", "uncertain": "disconnected"}.get(result["status"], result["status"])
    return result


def _normalize_codex(output: str | bytes, *, expected_conversation_id: str | None,
                     local_run_id: str, exit_code: int | None, provenance: str,
                     output_format: str) -> dict:
    """Codex exec JSONL: thread.started, agent_message, turn.completed/failed.

    The documented terminal turn event has no thread_id; its identity is bound
    by ordering after one thread.started, never by the local run ID.
    """
    frames, issues, actual_format = _frames(output, output_format)
    threads, terminals, reports, models, efforts = [], [], [], [], []
    seen_items: set[str] = set()
    for frame in frames:
        kind = frame.get("type")
        if terminals:
            issues.append("frame_after_terminal")
        if kind == "thread.started":
            thread = _string(frame.get("thread_id"))
            if not thread:
                issues.append("init_missing_conversation_id")
            else:
                threads.append(thread)
            if len(threads) > 1:
                issues.append("duplicate_init")
            if _string(frame.get("model")):
                models.append(frame["model"])
            if _string(frame.get("effort")):
                efforts.append(frame["effort"])
        elif kind in ("turn.completed", "turn.failed"):
            if not threads:
                issues.append("terminal_before_init")
            terminals.append(frame)
        elif kind in ("item.started", "item.updated", "item.completed"):
            if not threads:
                issues.append("item_before_init")
            item = frame.get("item")
            if not isinstance(item, dict):
                issues.append("malformed_item")
            elif kind == "item.completed":
                item_id = _string(item.get("id"))
                if not item_id:
                    issues.append("missing_item_id")
                elif item_id in seen_items:
                    issues.append("duplicate_completed_item")
                else:
                    seen_items.add(item_id)
                if item.get("type") == "agent_message":
                    if _string(item.get("text")):
                        reports.append(item["text"])
                    else:
                        issues.append("empty_report")
        elif kind == "error":
            issues.append("provider_error_signal")
        elif kind != "turn.started":
            issues.append("unknown_frame_type")
        observed_id = frame.get("thread_id")
        if observed_id is not None and threads and observed_id != threads[0]:
            issues.append("conversation_mismatch")
    conversation = threads[0] if len(threads) == 1 else None
    if conversation is None:
        issues.append("conversation_not_independently_matched")
    if expected_conversation_id and conversation != expected_conversation_id:
        issues.append("conversation_mismatch")
    if len(terminals) != 1:
        issues.append("duplicate_terminal_result" if terminals else "missing_terminal_result")
    terminal = terminals[0] if len(terminals) == 1 else {}
    if terminal.get("type") == "turn.completed" and not reports:
        issues.append("empty_report")
    if type(exit_code) is not int or exit_code != 0:
        issues.append("process_not_confirmed_exit_zero")
    if len(set(models)) > 1 or len(set(efforts)) > 1:
        issues.append("conflicting_model_or_effort")
    usage = terminal.get("usage")
    if usage is not None and not isinstance(usage, dict):
        issues.append("malformed_usage")
        usage = None
    status = "uncertain" if issues else ("completed" if terminal.get("type") == "turn.completed" else "failed")
    issues = list(dict.fromkeys(issues))
    return {
        "provider": "codex", "local_run_id": local_run_id,
        "external_conversation_id": conversation, "conversation_id": conversation,
        "provider_run_id": None, "model": models[0] if len(set(models)) == 1 else None,
        "effort": efforts[0] if len(set(efforts)) == 1 else None,
        "status": status, "run_status": {"completed": "succeeded", "uncertain": "disconnected"}.get(status, status),
        "report": reports[-1] if reports else "", "response": reports[-1] if reports else "",
        "error": "; ".join(issues) if issues else terminal.get("error"), "issues": issues,
        "exit_code": exit_code, "terminal_result_count": len(terminals), "retry_allowed": False,
        "provenance": "synthetic_fixture" if provenance == "synthetic_fixture" else "imported_unverified",
        "provenance_claim": provenance, "attested": False, "receipt_source": "supplied_output",
        "output_format": actual_format, "request": None, "started_at": None, "finished_at": None,
        "usage": copy.deepcopy(usage), "usage_semantics": "provider_reported_terminal", "billed_amount": None,
    }


def build_execution_plan(
    provider: str, prompt: str, *, local_run_id: str, cwd: str | Path,
    conversation_id: str | None = None, model: str | None = None,
    connections_path: str | Path = "CONNECTIONS.json",
) -> dict:
    """Prepare a reviewable, unvalidated command plan; never spawn a process.

    A future implementation must validate supported flags, scope/tools, approved
    model, time/cost bounds, and fresh execution approval, then persist send intent
    before one write. Imported plans or environment variables confer no approval.
    """
    _provider(provider)
    if not _string(prompt) or not _string(local_run_id):
        raise ValueError("prompt and local_run_id must be nonempty")
    if conversation_id is not None and not _string(conversation_id):
        raise ValueError("conversation_id must be nonempty")
    if model is not None and not _string(model):
        raise ValueError("model must be nonempty")
    metadata = load_provider_metadata(connections_path)
    binary = metadata.get("providers", {}).get(provider, {}).get("binary") or {"claude": "claude", "antigravity": "agy", "codex": "codex"}[provider]
    if provider == "codex":
        argv = [binary, "exec", "--json", "--sandbox", "read-only", "-"]
        if conversation_id:
            argv = [binary, "exec", "resume", conversation_id, "--json", "-"]
        stdin = prompt
    elif provider == "claude":
        argv = [binary, "--print", "--input-format", "text", "--output-format", "stream-json", "--verbose"]
        stdin = prompt
        if conversation_id:
            argv += ["--resume", conversation_id]
    else:
        argv = [binary, "--input-format", "stream-json", "--output-format", "stream-json"]
        stdin = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"
        if conversation_id:
            argv += ["--conversation", conversation_id]
    if model:
        argv += ["--model", model]
    return {
        "provider": provider, "local_run_id": local_run_id,
        "external_conversation_id": conversation_id, "requested_model": model,
        "cwd": str(cwd), "argv": argv, "stdin": stdin, "shell": False,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "status": "pending_specific_approval", "transport": "disabled",
        "command_validated": False, "retry_allowed": False,
        "requirements": ["Specific approval of provider, model, prompt, isolated cwd and tool scope",
                         "Verify CLI flags and explicit tool restrictions before enabling transport",
                         "Bounded timeout/cost policy and durable send-intent ledger before one write",
                         "Validate init identity/model/tool scope before sending Antigravity input",
                         "No automatic resend after failure, cancellation or uncertain outcome"],
    }


def submit_execution(plan: dict, **kwargs: Any) -> None:
    """Fail closed even if passed approval booleans, tokens or environment flags."""
    raise ExecutionApprovalRequired(
        "Live execution is disabled pending specific approval and implementation of "
        "the bounded transport, init gate and durable no-resend ledger. No provider was invoked."
    )


class ProviderConnector(ABC):
    provider: str

    def normalize(self, output: str | bytes, **kwargs: Any) -> dict:
        return normalize_output(self.provider, output, **kwargs)

    def build_plan(self, prompt: str, **kwargs: Any) -> dict:
        return build_execution_plan(self.provider, prompt, **kwargs)

    def status_metadata(self, connections_path: str | Path = "CONNECTIONS.json") -> dict:
        metadata = load_provider_metadata(connections_path)
        return {"provider": self.provider, **metadata.get("providers", {}).get(self.provider, {}),
                "transport": "disabled", "metadata_error": metadata.get("metadata_error")}

    def probe(self, connections_path: str | Path = "CONNECTIONS.json") -> dict:
        """Expose recorded discovery only; no authentication or execution probe."""
        return next(card for card in connector_statuses(connections_path) if card["provider"] == ("codex_dot" if self.provider == "codex" else self.provider))

    def prepare(self, request: dict) -> dict:
        required = ("project_id", "phase_id", "revision", "instruction_version", "instruction_snapshot", "role", "context_references")
        missing = [key for key in required if key not in request]
        if missing:
            raise ValueError("Missing preparation context: " + ", ".join(missing))
        plan = self.build_plan(request.get("prompt", ""), local_run_id=request.get("local_run_id", ""),
                               cwd=request.get("cwd", ""), conversation_id=request.get("conversation_id"),
                               model=request.get("model"), connections_path=request.get("connections_path", "CONNECTIONS.json"))
        plan["context"] = {key: copy.deepcopy(request[key]) for key in required}
        return plan

    def execute(self, prepared: dict, specific_user_authorization: Any = None) -> None:
        self.submit(prepared, specific_user_authorization=specific_user_authorization)

    def cancel(self, run_id: str) -> None:
        raise ExecutionApprovalRequired("No managed provider process exists to cancel; live transport is disabled.")

    @abstractmethod
    def submit(self, plan: dict, **kwargs: Any) -> None:
        raise NotImplementedError


class ClaudeConnector(ProviderConnector):
    provider = "claude"

    def submit(self, plan: dict, **kwargs: Any) -> None:
        submit_execution(plan, **kwargs)


class AntigravityConnector(ProviderConnector):
    provider = "antigravity"

    def submit(self, plan: dict, **kwargs: Any) -> None:
        submit_execution(plan, **kwargs)


class CodexConnector(ProviderConnector):
    provider = "codex"

    def submit(self, plan: dict, **kwargs: Any) -> None:
        submit_execution(plan, **kwargs)


def get_connector(provider: str) -> ProviderConnector:
    return {"claude": ClaudeConnector, "antigravity": AntigravityConnector, "codex": CodexConnector}[_provider(provider)]()


def connector_statuses(connections_path: str | Path = "CONNECTIONS.json") -> list[dict]:
    """UI capability cards reflect recorded discovery, never implied login."""
    metadata = load_provider_metadata(connections_path)
    cards = []
    for provider, role in (("codex_dot", "primary_author"), ("claude", "reviewer_claude"), ("antigravity", "reviewer_gemini")):
        recorded = metadata.get("providers", {}).get(provider, {})
        detected = recorded.get("installed") is True
        cards.append({
            "id": provider, "role": role, "provider": provider, "detected": detected,
            "executable_path": recorded.get("binary") if detected else None,
            "connected": False, "authorized": False, "status": "disabled",
            "reason": "Live execution is disabled pending specific approval and bounded transport implementation.",
            "supports": {"execute": False, "resume": False, "stream": False},
            "checked_at": metadata.get("checked_at"), "authenticated": recorded.get("authenticated", "not_checked"),
            "connection_state": "unconnected", "detection_state": "detected" if detected else "not_established",
            "recorded_discovery": recorded, "metadata_error": metadata.get("metadata_error"),
        })
    return cards
