"""Bounded stdio engine exercised only by the repository-owned Python fixture.

External transport is hard-gated before intent or process launch. Environment
sanitization is not a network/filesystem sandbox. Live credential policy and
general provider process-tree control remain authorization prerequisites.
"""
from __future__ import annotations

import hashlib
import builtins
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from providers import ExecutionApprovalRequired, _decode, build_execution_plan, normalize_output

SCENARIOS = ("success", "nonzero_exit", "malformed_output", "conversation_mismatch", "timeout", "output_overflow", "wait_for_cancel")
_ALIASES = {"error": "nonzero_exit", "malformed": "malformed_output", "mismatch": "conversation_mismatch", "cancel": "wait_for_cancel"}
DEFAULT_LIMITS = {"timeout_seconds": 5.0, "max_input_bytes": 65536, "max_output_bytes": 1048576, "max_line_bytes": 262144}
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "transport_child.py"
BRIDGE_V11_HASHES = {
    "handoff.py": "a4e0dea2c4eaa47d477dd393c49768d773b4913d8f26ce67004b8ab4401af876",
    "usage_audit.py": "97cd7972c3ead07908d678898d5a49f213a27a7ec50f2b20084f38e0bdd9ce54",
    "APP-HANDOFF-CONTRACT.md": "f0b52e50c86ef8ff832d56a888a57275700e7448e893710fdcd6f08851922459",
}


class ReplayRefused(ValueError):
    code = "INTENT_ALREADY_EXISTS"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _safe_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("Invalid transport run ID")
    return value


def _contained(root: str | Path, *parts: str) -> Path:
    base = Path(root).resolve()
    if any(not isinstance(part, str) or not part or part in (".", "..")
           or any(character in part for character in ("/", "\\", ":")) for part in parts):
        raise ValueError("Generated transport paths require plain relative components")
    path = base.joinpath(*parts)
    # Plain components below a canonical base cannot lexically escape. Inspect
    # every component for links/junctions instead of resolving the final file:
    # Windows may fail canonicalizing an exclusively opened journal during a
    # concurrent reservation. Never interpret that transient as another root.
    current = base
    for part in parts:
        current = current / part
        if current.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(current)):
            raise ValueError("Transport generated paths cannot use symlinks or junctions")
    return path


def _limits(overrides: dict | None) -> dict:
    limits = dict(DEFAULT_LIMITS)
    if overrides is not None:
        if not isinstance(overrides, dict) or set(overrides) - set(limits):
            raise ValueError("Unknown transport limits")
        limits.update(overrides)
    for key, value in limits.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("Invalid transport limit: " + key)
        if key != "timeout_seconds" and (type(value) is not int or value > DEFAULT_LIMITS[key]):
            raise ValueError("Byte limits cannot exceed hard transport bounds")
    if limits["timeout_seconds"] > 30:
        raise ValueError("Timeout cannot exceed 30 seconds")
    return limits


def safe_child_environment(workspace: Path) -> dict:
    """Keep runtime essentials only, with private temporary/home locations."""
    allowed = ("SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PATHEXT")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update({"TEMP": str(workspace), "TMP": str(workspace), "TMPDIR": str(workspace),
                "HOME": str(workspace), "USERPROFILE": str(workspace),
                "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
    return env


def verify_source_pins(manifest: dict) -> dict:
    """Hash explicitly pinned normal source files only; never import/execute them."""
    root = Path(manifest["source_root"]).resolve()
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("Invalid source pin manifest")
    checked = {}
    for relative, digest in files.items():
        source = root / relative
        if (not source.resolve().is_relative_to(root) or source.is_symlink()
                or source.suffix.lower() not in (".py", ".md") or "[dot reject]" in str(source).lower()):
            raise ValueError("Source pin must identify a normal permitted source file")
        with source.open("rb") as handle:
            raw = handle.read(4 * 1048576 + 1)
        if len(raw) > 4 * 1048576 or _hash(raw) != digest:
            raise ValueError("SOURCE_HASH_MISMATCH: " + relative)
        checked[relative] = digest
    return {"status": "verified", "files": checked, "observed_at": _now()}


def _verified_bridge_sources(pins_path: str | Path | None = None) -> tuple[dict, dict]:
    """Verify every fixed runtime dependency before executing either module."""
    path = Path(pins_path) if pins_path else Path(__file__).resolve().parent / "BRIDGE-PINS.json"
    try:
        with path.open("rb") as handle:
            raw_pins = handle.read(65537)
        if len(raw_pins) > 65536:
            raise ValueError("BRIDGE_PINS_SIZE_LIMIT")
        pins = _decode(raw_pins.decode("utf-8-sig"))
        if (not isinstance(pins, dict) or pins.get("release_version") != "1.1"
                or pins.get("files") != BRIDGE_V11_HASHES):
            raise ValueError("BRIDGE_FIXED_RELEASE_PIN_MISMATCH")
        root = Path(pins["source_root"])
        if not root.is_absolute():
            raise ValueError("BRIDGE_RUNTIME_ROOT_NOT_ABSOLUTE")
        sources = {}
        for name, digest in BRIDGE_V11_HASHES.items():
            source = root / name
            if source.is_symlink() or not source.resolve().is_relative_to(root.resolve()):
                raise ValueError("BRIDGE_RUNTIME_PATH_REJECTED")
            with source.open("rb") as handle:
                raw = handle.read(1048577)
            if len(raw) > 1048576 or _hash(raw) != digest:
                raise ValueError("BRIDGE_SOURCE_HASH_MISMATCH:" + name)
            sources[name] = raw
        return {"version": "1.1", "status": "verified", "runtime_root": str(root),
                "files": dict(BRIDGE_V11_HASHES), "observed_at": _now()}, sources
    except ValueError as exc:
        code = str(exc)
        approved_codes = {"BRIDGE_PINS_SIZE_LIMIT", "BRIDGE_FIXED_RELEASE_PIN_MISMATCH",
                          "BRIDGE_RUNTIME_ROOT_NOT_ABSOLUTE", "BRIDGE_RUNTIME_PATH_REJECTED"}
        approved_codes.update("BRIDGE_SOURCE_HASH_MISMATCH:" + name for name in BRIDGE_V11_HASHES)
        raise ValueError(code if code in approved_codes else "BRIDGE_FIXED_RELEASE_PIN_REJECTED") from None
    except (OSError, KeyError, TypeError, AttributeError, RecursionError, UnicodeError):
        raise ValueError("BRIDGE_FIXED_RELEASE_UNAVAILABLE") from None


def _execute_pinned_module(raw: bytes, name: str, filename: str, dependency=None):
    """Private module dictionaries: no sys.path/modules edits or cache writes.

    This executes only the hash-approved normal release sources, whose top-level
    bodies define parsing functions. No CLI main, archive collector, file writer,
    mutable owner module, or provider transport is invoked.
    """
    module = types.ModuleType(name)
    module.__file__ = filename
    local_builtins = dict(vars(builtins))
    if dependency is not None:
        original_import = builtins.__import__
        def pinned_import(import_name, globals=None, locals=None, fromlist=(), level=0):
            if import_name == "usage_audit" and level == 0:
                return dependency
            return original_import(import_name, globals, locals, fromlist, level)
        local_builtins["__import__"] = pinned_import
    module.__dict__["__builtins__"] = local_builtins
    exec(compile(raw, filename, "exec"), module.__dict__)
    return module


def _load_pinned_handoff(pins_path: str | Path | None = None):
    release, sources = _verified_bridge_sources(pins_path)
    root = Path(release["runtime_root"])
    dependency = _execute_pinned_module(sources["usage_audit.py"], "_glance_bridge_v11_usage", str(root / "usage_audit.py"))
    handoff = _execute_pinned_module(sources["handoff.py"], "_glance_bridge_v11_handoff", str(root / "handoff.py"), dependency)
    return release, handoff


def _check_handoff_parse_budget(value: Any) -> None:
    """Bound aggregate line expansion as well as serialized input size."""
    stack, nodes, line_budget = [(None, value)], 0, 0
    range_fields = {"reviewed_lines", "reviewed_ranges", "text_forms", "text_form", "lines"}
    while stack:
        key, item = stack.pop()
        nodes += 1
        if nodes > 50000:
            raise ValueError("HANDOFF_NODE_LIMIT")
        if isinstance(item, dict):
            start, end = item.get("start_line"), item.get("end_line")
            if type(start) is int and type(end) is int:
                line_budget += max(0, end - start + 1)
            stack.extend(item.items())
        elif isinstance(item, list):
            stack.extend((key, child) for child in item)
        elif isinstance(item, str) and key in range_fields:
            for match in re.finditer(r"L?(\d+)(?:\s*-\s*L?(\d+))?", item):
                start = int(match[1])
                end = int(match[2]) if match[2] else start
                line_budget += max(0, end - start + 1)
                if line_budget > 200000:
                    raise ValueError("HANDOFF_LINE_EXPANSION_LIMIT")
        if line_budget > 200000:
            raise ValueError("HANDOFF_LINE_EXPANSION_LIMIT")


def validate_bridge_handoff_inputs(blobs: dict, *, provenance: str = "synthetic_fixture",
                                   pins_path: str | Path | None = None) -> dict:
    """Use v1.1 normal analyze_inputs on explicit bounded saved bytes only.

    Coverage, provider evidence and project application remain separate. This
    helper neither enumerates archives nor creates/adjudicates/applies a handoff.
    The caller supplies the six exact artifacts from a reviewed source registry.
    """
    names = {"packet.json", "manifest.json", "receipt.json", "rawreport.txt", "stdout.ndjson", "routes.json"}
    if not isinstance(blobs, dict) or set(blobs) != names:
        raise ValueError("BRIDGE_HANDOFF_INPUT_SET_MISMATCH")
    if provenance not in ("synthetic_fixture", "historical_external_execution"):
        raise ValueError("BRIDGE_HANDOFF_PROVENANCE_REQUIRED")
    if any(not isinstance(raw, bytes) for raw in blobs.values()) or sum(map(len, blobs.values())) > 1048576:
        raise ValueError("BRIDGE_HANDOFF_INPUT_SIZE_OR_TYPE_LIMIT")
    release, handoff = _load_pinned_handoff(pins_path)
    try:
        # First apply the app's strict JSON/depth checks before the pinned parser.
        for name in ("packet.json", "manifest.json", "receipt.json", "routes.json"):
            parsed = _decode(blobs[name].decode("utf-8-sig"))
            if not isinstance(parsed, dict):
                raise ValueError("ARTIFACT_NOT_OBJECT")
            _check_handoff_parse_budget(parsed)
        _check_handoff_parse_budget(handoff.parse_report(blobs["rawreport.txt"]))
        analysis = handoff.analyze_inputs(blobs)
        provider = analysis["provider"]
        return {"bridge_release": release, "provenance": provenance,
                "provider_success": provider["provider_success"], "provider": provider,
                "report_integrity": provider["raw_report_matches_response"],
                "selected_scope_review_complete": analysis["scope_complete"],
                "coverage_errors": analysis["coverage_errors"], "coverage_warnings": analysis["coverage_warnings"],
                "finding_validation_errors": analysis["candidate_errors"],
                "projects": analysis["projects"], "owner_adjudication": "not_performed",
                "application": "not_performed", "local_reverification": "not_performed",
                "project_complete": False, "eligible_current_phase": False, "attested": False,
                "external_calls_added": 0, "source_artifact_sha256": {name: _hash(raw) for name, raw in blobs.items()}}
    except (ValueError, TypeError, AttributeError, KeyError, IndexError, RecursionError, UnicodeError):
        raise ValueError("BRIDGE_HANDOFF_INPUT_REJECTED") from None


def prepare_transport_request(provider: str = "antigravity", *, run_id: str | None = None,
                              prompt: str, workspace_root: str | Path,
                              journal_root: str | Path | None = None, scenario: str = "success",
                              model: str | None = None, effort: str | None = None,
                              conversation_id: str | None = None, limits: dict | None = None,
                              mode: str = "synthetic", project_id: str = "local-workroom",
                              phase_id: str | None = None, role: str | None = None,
                              revision: str | None = None, instruction_version: int = 0,
                              instruction_snapshot: Any = None,
                              connections_path: str | Path = "CONNECTIONS.json") -> dict:
    """Validate and plan, without creating files or launching any process."""
    provider = "codex" if provider == "codex_dot" else provider
    if provider not in ("claude", "antigravity", "codex"):
        raise ValueError("Unknown provider")
    run_id = _safe_id(run_id or str(uuid.uuid4()))
    scenario = _ALIASES.get(scenario, scenario)
    if scenario not in SCENARIOS or mode not in ("synthetic", "live_pending"):
        raise ValueError("Unknown fixture scenario or execution mode")
    bounds = _limits(limits)
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > bounds["max_input_bytes"]:
        raise ValueError("Transport input must be nonempty and within the byte limit")
    root = Path(workspace_root).resolve()
    journal = Path(journal_root).resolve() if journal_root else root / "transport-journal"
    cwd = _contained(root, "runs", run_id, "workspace")
    _contained(journal, run_id + ".jsonl")
    plan_id, request_id = str(uuid.uuid4()), str(uuid.uuid4())
    request = {"schema_version": 1, "id": request_id, "idempotency_key": run_id,
               "run_id": run_id, "local_run_id": run_id, "provider": provider, "mode": mode,
               "project_id": project_id, "phase_id": phase_id, "role": role,
               "revision": revision, "instruction_version": instruction_version,
               "instruction_snapshot": instruction_snapshot, "plan_id": plan_id,
               "input_text": prompt, "input_sha256": _hash(prompt.encode("utf-8")),
               "requested_model": model, "requested_effort": effort,
               "expected_conversation_id": conversation_id,
               "scenario": scenario, "workspace_root": str(root), "journal_root": str(journal),
               "limits": bounds}
    if mode == "synthetic":
        stdin = (json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n") if provider == "antigravity" else prompt
        if len(stdin.encode("utf-8")) > bounds["max_input_bytes"]:
            raise ValueError("Encoded provider input exceeds byte limit")
        plan = {"argv": [str(Path(sys.executable).resolve()), "-I", "-u", str(_FIXTURE), provider, scenario, run_id],
                "stdin": stdin, "stdin_format": "stream-json" if provider == "antigravity" else "text",
                "fixture_sha256": _hash(_FIXTURE.read_bytes()), "reported_model_expected": "synthetic-" + provider + "-fixture",
                "expected_conversation_id": conversation_id or "fixture-" + run_id,
                "status": "prepared", "transport": "local_python_fixture"}
    else:
        plan = build_execution_plan(provider, prompt, local_run_id=run_id, cwd=cwd,
                                    conversation_id=conversation_id, model=model, connections_path=connections_path)
        plan.update({"status": "awaiting_approval", "stdin_format": "stream-json" if provider == "antigravity" else "text"})
    plan.update({"id": plan_id, "request_id": request_id, "provider": provider, "mode": mode,
                 "cwd": str(cwd), "shell": False, "limits": bounds,
                 "input_sha256": _hash(plan["stdin"].encode("utf-8")),
                 "input_bytes": len(plan["stdin"].encode("utf-8")),
                 "executable_path": plan["argv"][0] if Path(plan["argv"][0]).is_absolute() else None,
                 "allowed_file_scope": [str(cwd)],
                 "environment_keys": sorted(safe_child_environment(cwd)),
                 "expected_output_protocol": provider + ":stream-json",
                 "authorization": {"state": "not_granted"}, "cost_boundary": None,
                 "account_id": None, "requested_model": model, "requested_effort": effort})
    if mode == "live_pending" and effort is not None:
        plan["effort_encoding"] = "not_wired_pending_provider_flag_validation"
    plan["request_sha256"] = _hash(_canonical(request))
    pins_path = Path(__file__).resolve().parent / "BRIDGE-PINS.json"
    if mode == "live_pending" and pins_path.exists():
        try:
            plan["source_pins"] = _verified_bridge_sources(pins_path)[0]
        except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
            plan["source_pins"] = {"status": "mismatch_or_unavailable", "error": str(exc)}
    plan["plan_sha256"] = _hash(_canonical(plan))
    return {"request": request, "plan": plan}


def execute_provider(*args: Any, **kwargs: Any) -> None:
    """No approval boolean, plan field, or environment setting enables live AI."""
    raise ExecutionApprovalRequired("NOT_AUTHORIZED: specific per-call approval and live credential/process-tree policy are pending; no intent or provider process created.")


def _failure(code: str, message: str, stage: str, uncertain: bool, exit_code: int | None = None) -> dict:
    return {"code": code, "message": message, "stage": stage, "retryable": False,
            "dispatch_uncertain": uncertain, "exit_code": exit_code, "details": {}}


def _append_journal(handle: Any, kind: str, **fields: Any) -> None:
    handle.write(_canonical({"event": kind, "created_at": _now(), **fields}) + b"\n")
    handle.flush()
    os.fsync(handle.fileno())


def _validate_prepared(prepared: dict) -> tuple[dict, dict]:
    request, plan = prepared["request"], prepared["plan"]
    if request.get("mode") != "synthetic" or plan.get("mode") != "synthetic":
        raise ExecutionApprovalRequired("Only synthetic fixture requests can execute")
    if request.get("idempotency_key") != request.get("run_id"):
        raise ValueError("PLAN_STALE: idempotency key must be the reserved run ID")
    if plan.get("request_sha256") != _hash(_canonical(request)):
        raise ValueError("PLAN_STALE: request changed after preparation")
    expected = prepare_transport_request(request["provider"], run_id=request["run_id"], prompt=request["input_text"],
        workspace_root=request["workspace_root"], journal_root=request["journal_root"], scenario=request["scenario"],
        model=request.get("requested_model"), effort=request.get("requested_effort"),
        conversation_id=request.get("expected_conversation_id"), limits=request["limits"])
    reference = expected["plan"]
    for key in ("argv", "stdin", "cwd", "limits", "fixture_sha256", "reported_model_expected", "expected_conversation_id"):
        if plan.get(key) != reference.get(key):
            raise ValueError("PLAN_STALE: fixture plan changed: " + key)
    unhashed = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if plan.get("plan_sha256") != _hash(_canonical(unhashed)) or request.get("plan_id") != plan.get("id"):
        raise ValueError("PLAN_STALE: plan hash or identity mismatch")
    return request, plan


def _validate_fixture_init(frame: dict, request: dict, plan: dict) -> str:
    if request["provider"] == "antigravity":
        good_kind = frame.get("event") == "init"
        payload, cid = frame.get("init", {}), frame.get("conversation_id")
    elif request["provider"] == "claude":
        good_kind = frame.get("type") == "system" and frame.get("subtype") == "init"
        payload, cid = frame, frame.get("session_id")
    else:
        good_kind = frame.get("type") == "thread.started"
        payload, cid = frame, frame.get("thread_id")
    if not good_kind or not isinstance(payload, dict) or payload.get("synthetic") is not True:
        raise ValueError("Expected fixture init")
    if cid != plan["expected_conversation_id"] or payload.get("model") != plan["reported_model_expected"]:
        raise ValueError("Fixture init identity/model mismatch")
    if payload.get("tools") != [] or Path(payload.get("cwd", "")).resolve() != Path(plan["cwd"]).resolve():
        raise ValueError("Fixture init tool/cwd mismatch")
    return cid


def _run_stdio(plan: dict, request: dict, journal: Any, cancel_event: threading.Event,
               init_validator=_validate_fixture_init) -> dict:
    """Shared bounded pipe engine. Public callers cannot supply arbitrary argv.

    A future specifically authorized provider entry can use this same drain,
    write, time/cancel, artifacts and journal engine after implementing its
    provider init gate, credential policy and owned-tree lifecycle.
    """
    limits = plan["limits"]
    cwd = Path(plan["cwd"])
    started = _now()
    deadline = time.monotonic() + limits["timeout_seconds"]
    events: queue.Queue = queue.Queue(maxsize=64)
    stopped = threading.Event()
    stdout, stderr = bytearray(), bytearray()
    stdout_line = bytearray()
    line_lengths = {"stdout": 0, "stderr": 0}
    observed = {"stdout": 0, "stderr": 0}
    failure = None
    cleanup_error = None
    process = None
    threads = []
    input_sent, send_attempted, init_validated = False, False, False
    eof = set()

    def enqueue(item):
        while not stopped.is_set():
            try:
                events.put(item, timeout=0.05)
                return
            except queue.Full:
                pass

    def drain(pipe, name):
        try:
            while not stopped.is_set():
                chunk = os.read(pipe.fileno(), 4096)
                if not chunk:
                    break
                enqueue((name, chunk))
        except (OSError, ValueError) as exc:
            if not stopped.is_set():
                enqueue(("reader_error", str(exc)))
        finally:
            enqueue((name, None))

    def write_once(pipe, payload):
        try:
            count = pipe.write(payload)
            if count is not None and count != len(payload):
                raise OSError("Partial stdin write; never resend")
            pipe.flush()
            pipe.close()
            enqueue(("write_done", True))
        except (OSError, ValueError) as exc:
            enqueue(("write_error", str(exc)))

    try:
        kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                  "cwd": str(cwd), "env": safe_child_environment(cwd), "shell": False, "bufsize": 0}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(plan["argv"], **kwargs)
        _append_journal(journal, "process_started", run_id=request["run_id"], pid=process.pid)
        for pipe, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            thread = threading.Thread(target=drain, args=(pipe, name), daemon=True)
            thread.start()
            threads.append(thread)
        while True:
            if cancel_event.is_set():
                failure = _failure("CANCELLED", "Local fixture cancellation requested", "process", True)
                break
            if time.monotonic() >= deadline:
                failure = _failure("TIMEOUT", "Transport wall timeout exceeded", "process", True)
                break
            if len(eof) == 2 and process.poll() is not None and (not send_attempted or input_sent):
                break
            try:
                kind, value = events.get(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if kind == "write_done":
                input_sent = True
                _append_journal(journal, "input_sent", run_id=request["run_id"])
                continue
            if kind in ("write_error", "reader_error"):
                failure = _failure("STDIN_FAILED" if kind == "write_error" else "INVALID_OUTPUT", str(value), "stdio", send_attempted)
                break
            if value is None:
                eof.add(kind)
                continue
            observed[kind] += len(value)
            target = stdout if kind == "stdout" else stderr
            room = max(0, limits["max_output_bytes"] - len(stdout) - len(stderr))
            target.extend(value[:room])
            if observed["stdout"] + observed["stderr"] > limits["max_output_bytes"]:
                failure = _failure("OUTPUT_LIMIT", "Combined output byte limit exceeded", "stdio", True)
                break
            for piece_index, piece in enumerate(value.split(b"\n")):
                line_lengths[kind] += len(piece)
                if line_lengths[kind] > limits["max_line_bytes"]:
                    failure = _failure("OUTPUT_LIMIT", "Output line byte limit exceeded", "stdio", True)
                    break
                if piece_index < value.count(b"\n"):
                    line_lengths[kind] = 0
            if failure:
                break
            if kind == "stdout" and not init_validated:
                stdout_line.extend(value)
                if b"\n" in stdout_line:
                    raw_init = bytes(stdout_line).split(b"\n", 1)[0]
                    try:
                        frame = _decode(raw_init.decode("utf-8"))
                        cid = init_validator(frame, request, plan)
                    except (ValueError, TypeError, AttributeError, RecursionError):
                        failure = _failure("INVALID_OUTPUT", "Fixture init gate rejected output", "init", False)
                        break
                    init_validated = True
                    _append_journal(journal, "init_validated", conversation_id=cid)
                    # This durable record precedes the single stdin write attempt.
                    _append_journal(journal, "input_send_intent", input_sha256=plan["input_sha256"])
                    send_attempted = True
                    writer = threading.Thread(target=write_once, args=(process.stdin, plan["stdin"].encode("utf-8")), daemon=True)
                    writer.start()
                    threads.append(writer)
    except OSError as exc:
        failure = _failure("SPAWN_FAILED" if process is None else "INTERRUPTED", str(exc), "spawn" if process is None else "journal", process is not None)
    finally:
        # The fixed owned child never spawns descendants. Arbitrary PID/tree
        # termination is deliberately not exposed by this fixture entry point.
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=0.4)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=0.8)
            except (OSError, subprocess.TimeoutExpired) as exc:
                cleanup_error = str(exc)
        stopped.set()
        for thread in threads:
            thread.join(timeout=0.15)
        if process is not None:
            for pipe in (process.stdin, process.stdout, process.stderr):
                try:
                    if pipe:
                        pipe.close()
                except OSError:
                    pass
    exit_code = process.poll() if process else None
    normalized = normalize_output(request["provider"], bytes(stdout), local_run_id=request["run_id"],
                                  expected_conversation_id=plan["expected_conversation_id"],
                                  exit_code=exit_code, provenance="synthetic_fixture", output_format="stream-json")
    if failure is None:
        if exit_code != 0:
            failure = _failure("NONZERO_EXIT", "Fixture process exited nonzero", "exit", send_attempted, exit_code)
        elif normalized["status"] == "uncertain":
            mismatch = any("mismatch" in issue or "conflicting_conversation" in issue for issue in normalized["issues"])
            failure = _failure("CORRELATION_MISMATCH" if mismatch else "INVALID_OUTPUT", normalized["error"], "parse", send_attempted, exit_code)
    status = {"completed": "succeeded", "uncertain": "uncertain"}.get(normalized["status"], normalized["status"])
    if failure:
        status = {"TIMEOUT": "timed_out", "CANCELLED": "cancelled", "SPAWN_FAILED": "failed", "NONZERO_EXIT": "failed"}.get(failure["code"], "uncertain")
        failure["exit_code"] = exit_code
    artifacts = cwd.parent
    for name, content in (("stdout.ndjson", stdout), ("stderr.txt", stderr)):
        with (artifacts / name).open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    receipt = {**normalized, "id": str(uuid.uuid4()), "request_id": request["id"], "run_id": request["run_id"],
        "status": status, "outcome": normalized["status"] if not failure else "uncertain",
        "terminal_status": normalized["status"], "text": normalized["report"], "response": normalized["report"],
        "failure": failure, "cleanup_failure": _failure("CLEANUP_FAILED", cleanup_error, "cleanup", True, exit_code) if cleanup_error else None,
        "started_at": started, "finished_at": _now(), "mode": "synthetic", "provenance": "synthetic_fixture",
        "reported_model": normalized["model"], "reported_effort": normalized["effort"],
        "managed_fixture_execution": process is not None, "external_execution": False,
        "eligible_current_phase": False, "attested": False,
        "dispatch_state": "terminal_response" if normalized["status"] == "completed" and not failure else "uncertain" if process else "intent_recorded",
        "input_sent": input_sent, "input_write_attempted": send_attempted,
        "raw_stdout_artifact": "runs/" + request["run_id"] + "/stdout.ndjson",
        "raw_stderr_artifact": "runs/" + request["run_id"] + "/stderr.txt",
        "raw_stdout_bytes": len(stdout), "raw_stderr_bytes": len(stderr),
        "observed_stdout_bytes": observed["stdout"], "observed_stderr_bytes": observed["stderr"],
        "raw_stdout_sha256": _hash(stdout), "raw_stderr_sha256": _hash(stderr),
        "stdout_preview": bytes(stdout[:16384]).decode("utf-8", errors="replace"),
        "stderr_preview": bytes(stderr[:16384]).decode("utf-8", errors="replace"),
        "stdout_preview_truncated": len(stdout) > 16384, "stderr_preview_truncated": len(stderr) > 16384,
        "stdout_truncated": observed["stdout"] > len(stdout), "stderr_truncated": observed["stderr"] > len(stderr),
        "expected_conversation_id": plan["expected_conversation_id"],
        "parser_diagnostics": normalized["issues"], "cwd": str(cwd), "pid": process.pid if process else None}
    if isinstance(receipt.get("usage"), dict):
        receipt["usage"].update({"cost": None, "currency": None, "account_id": None, "source": "synthetic_fixture"})
    return receipt


def execute_fixture(prepared: dict, cancel_event: threading.Event | None = None) -> dict:
    """Reserve one durable attempt and run a fixed, real local Python child."""
    request, plan = _validate_prepared(prepared)
    cancel_event = cancel_event or threading.Event()
    if cancel_event.is_set():
        return {"run_id": request["run_id"], "status": "cancelled", "mode": "synthetic", "provenance": "synthetic_fixture",
                "dispatch_state": "not_started", "failure": _failure("CANCELLED", "Cancelled before intent", "prepare", False),
                "external_execution": False, "eligible_current_phase": False}
    journal_path = _contained(request["journal_root"], request["run_id"] + ".jsonl")
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(journal_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ReplayRefused("INTENT_ALREADY_EXISTS: run consumed; never resend, including after restart") from exc
    intent_id = str(uuid.uuid4())
    with os.fdopen(descriptor, "wb") as journal:
        _append_journal(journal, "send_intent", intent_id=intent_id, run_id=request["run_id"],
                        idempotency_key=request["idempotency_key"], provider=request["provider"], mode="synthetic",
                        request_id=request["id"], plan_id=plan["id"], input_sha256=plan["input_sha256"],
                        request_sha256=_hash(_canonical(request)), plan_sha256=plan["plan_sha256"])
        cwd = _contained(request["workspace_root"], "runs", request["run_id"], "workspace")
        cwd.mkdir(parents=True, exist_ok=False)
        receipt = _run_stdio(plan, request, journal, cancel_event)
        receipt.update({"intent_id": intent_id, "journal_path": str(journal_path), "plan_id": plan["id"]})
        _append_journal(journal, "finished", intent_id=intent_id, receipt=receipt)
    return receipt


def _validated_journal_receipt(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("Journal symlink rejected")
    with path.open("rb") as handle:
        raw = handle.read(4 * 1048576 + 1)
    if len(raw) > 4 * 1048576 or not raw.endswith(b"\n"):
        raise ValueError("Journal oversized or truncated")
    entries = [_decode(line.decode("utf-8")) for line in raw.splitlines()]
    order = {"send_intent": 0, "process_started": 1, "init_validated": 2,
             "input_send_intent": 3, "input_sent": 4, "finished": 5}
    names = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("event") not in order or not isinstance(entry.get("created_at"), str):
            raise ValueError("Invalid journal entry")
        names.append(entry["event"])
    if (not names or names[0] != "send_intent" or names[-1] != "finished"
            or len(set(names)) != len(names) or names != sorted(names, key=order.get)):
        raise ValueError("Invalid journal lifecycle ordering")
    indexed = {entry["event"]: entry for entry in entries}
    intent, finished = entries[0], entries[-1]
    run_id = _safe_id(path.stem)
    for key in ("request_sha256", "plan_sha256", "input_sha256"):
        if not isinstance(intent.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", intent[key]):
            raise ValueError("Missing journal binding hash")
    if (intent.get("run_id") != run_id or intent.get("idempotency_key") != run_id
            or intent.get("mode") != "synthetic" or intent.get("provider") not in ("claude", "antigravity", "codex")
            or not all(isinstance(intent.get(key), str) and intent[key] for key in ("intent_id", "request_id", "plan_id"))):
        raise ValueError("Invalid journal reservation identity")
    receipt = finished.get("receipt")
    if not isinstance(receipt, dict):
        raise ValueError("Missing journal receipt")
    for key in ("run_id", "intent_id", "request_id", "plan_id", "provider", "mode"):
        if receipt.get(key) != intent.get(key):
            raise ValueError("Journal receipt identity mismatch: " + key)
    if finished.get("intent_id") != intent["intent_id"] or receipt.get("local_run_id") != run_id:
        raise ValueError("Finished journal identity mismatch")
    if (receipt.get("status") not in ("succeeded", "failed", "cancelled", "timed_out", "uncertain")
            or receipt.get("provenance") != "synthetic_fixture" or receipt.get("external_execution") is not False
            or receipt.get("eligible_current_phase") is not False):
        raise ValueError("Invalid recovered receipt status/provenance")
    if "init_validated" in indexed and "process_started" not in indexed:
        raise ValueError("Init recorded without process start")
    if "input_send_intent" in indexed:
        if "init_validated" not in indexed or indexed["input_send_intent"].get("input_sha256") != intent["input_sha256"]:
            raise ValueError("Input send missing init/hash binding")
    if "input_sent" in indexed and "input_send_intent" not in indexed:
        raise ValueError("Input sent without intent")
    if "process_started" in indexed:
        process = indexed["process_started"]
        if process.get("run_id") != run_id or type(process.get("pid")) is not int or process["pid"] <= 0 or receipt.get("pid") != process["pid"]:
            raise ValueError("Process identity mismatch")
    if receipt["status"] == "succeeded":
        if (names != list(order) or receipt.get("outcome") != "completed"
                or receipt.get("terminal_status") != "completed" or receipt.get("exit_code") != 0
                or receipt.get("failure") is not None or not receipt.get("response")
                or receipt.get("conversation_id") != indexed["init_validated"].get("conversation_id")):
            raise ValueError("Recovered success lacks correlated completed lifecycle")
    return receipt


def recover_intents(journal_root: str | Path) -> list[dict]:
    """Read reservations without resending; malformed/unresolved entries block reuse."""
    recovered = []
    for path in sorted(Path(journal_root).glob("*.jsonl")):
        try:
            recovered.append(_validated_journal_receipt(path))
            continue
        except (OSError, ValueError, TypeError, AttributeError, RecursionError, KeyError):
            pass
        recovered.append({"run_id": path.stem, "status": "interrupted", "dispatch_state": "uncertain",
                          "retry_allowed": False, "failure": _failure("INTERRUPTED", "Reserved attempt has no validated finished receipt; never resend", "recovery", True)})
    return recovered


def import_historical_output(source_id: str, *, registry: dict) -> dict:
    """Import a root-reviewed source by ID only, never a client path or URL.

    Hash verification proves snapshot integrity. Provider origin remains an
    archive claim justified separately by the trusted registry source evidence.
    """
    if not isinstance(source_id, str) or source_id not in registry:
        raise ValueError("Unregistered historical source")
    record = registry[source_id]
    if (not isinstance(record, dict) or not isinstance(record.get("source_evidence"), dict)
            or not all(record["source_evidence"].get(key) for key in ("kind", "scope", "authenticity"))):
        raise ValueError("Historical provenance requires reviewed source evidence")
    path = Path(record["path"])
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("Historical source must be a registered absolute normal file")
    with path.open("rb") as handle:
        raw = handle.read(DEFAULT_LIMITS["max_output_bytes"] + 1)
    if len(raw) > DEFAULT_LIMITS["max_output_bytes"]:
        raise ValueError("Historical source exceeds bounded import size")
    if _hash(raw) != record.get("sha256"):
        raise ValueError("SOURCE_HASH_MISMATCH: registered historical snapshot changed")
    metadata = record.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("Historical metadata must be an object")
    normalized = normalize_output(record["provider"], raw, local_run_id="historical-" + source_id,
        expected_conversation_id=record.get("conversation_id"), exit_code=metadata.get("exit_code", 0),
        provenance="imported_unverified", output_format=record.get("parser_format", "auto"))
    usage = normalized.get("usage")
    if isinstance(usage, dict):
        usage = {**usage, "source": "historical_record", "cost": None, "currency": None, "account_id": None}
    try:
        release = _verified_bridge_sources()[0]
    except ValueError as exc:
        # Raw archive parsing remains independent. A missing fixed release never
        # falls back to mutable bridge code or invents selected-scope completion.
        release = {"version": "1.1", "status": "unavailable", "reason": str(exc)}
    return {**normalized, "id": str(uuid.uuid4()), "run_id": "historical-" + source_id,
        "status": normalized["run_status"], "outcome": normalized["status"], "text": normalized["report"],
        "mode": "historical", "provenance": "historical_external_execution", "source_id": source_id,
        "source_sha256": _hash(raw), "raw_stdout_bytes": len(raw), "imported_at": _now(),
        "started_at": metadata.get("started_at"), "finished_at": metadata.get("finished_at"),
        "recorded_execution_at": metadata.get("started_at"), "source_evidence": record["source_evidence"],
        "authenticity": "source_hash_verified", "provider_authenticity": "archive_claim",
        "receipt_source": "reviewed_historical_archive", "attested": False,
        "external_execution": False, "eligible_current_phase": False, "provider_calls_added": 0,
        "usage": usage, "account_id": None, "billed_amount": None,
        "bridge_release": release,
        "handoff_scope_validation": {"status": "unavailable", "selected_scope_review_complete": None,
            "reason": "Registered stream and selected metadata omit full packet/manifest/saved receipt/routes; no selected-scope completion inferred.",
            "project_complete": False}}
