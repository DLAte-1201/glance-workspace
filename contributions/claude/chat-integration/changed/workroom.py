"""Append-only local workroom domain. No network, subprocess or project file access."""
from __future__ import annotations

import copy
import re
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import threading
from datetime import datetime, timezone
import uuid

PROJECT_ID = "local-workroom"
MAX_UPLOAD = 256 * 1024
TABLES = ("instructions", "phases", "runs", "reviews", "contributions", "evidence", "uploads", "history_imports", "execution_plans", "chat_turns")
# 통합 채팅: 내부 제공자 ID ↔ 화면 이름 ↔ 실제 도구. 모델 이름은 고정하지 않는다(요청 시 사용자가 지정하거나 실행 결과가 보고한 값만 표시).
CHAT_PROVIDERS = {"codex": {"display": "ChatGPT", "tool": "Codex CLI"},
                  "claude": {"display": "Claude", "tool": "Claude Code"},
                  "antigravity": {"display": "Gemini", "tool": "Antigravity (agy)"}}
CHAT_WORKER_LIMIT = 6
CHAT_TERMINAL = ("succeeded", "failed", "cancelled", "disconnected", "timed_out", "interrupted", "uncertain")
SUBMISSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,79}")
REVIEWERS = ("claude", "antigravity")


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def uid():
    return str(uuid.uuid4())


class DomainError(Exception):
    def __init__(self, code, message, status=400, **details):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details

    def body(self):
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


def fail(code, message, status=400, **details):
    raise DomainError(code, message, status, **details)


def text(value, field="text", limit=65536, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
        fail("INVALID_FIELD", field + " must be nonempty bounded UTF-8 text", field=field, max_bytes=limit)
    return value


def initial_state():
    return {"schema_version": 1, "version": 0,
            "project": {"id": PROJECT_ID, "project_id": PROJECT_ID, "created_at": None,
                        "title": "Glance", "status": "active", "primary_author": "dot/Codex",
                        "instruction_version": 0, "active_phase_id": None},
            **{key: [] for key in TABLES}, "events": [], "warnings": []}


class Workroom:
    """One process owns a directory; each command is one durable JSONL batch.

    A process-level lock rejects a second writer. A thread lock serializes version
    checks, domain validation, append+fsync and state publication. A torn line is
    visible corruption on reload and disables writes; history is never repaired.
    """
    def __init__(self, data_dir, connections_path=None, history_registry_path=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "events.jsonl"
        self.connections_path = Path(connections_path or Path(__file__).with_name("CONNECTIONS.json"))
        self.history_registry_path = Path(history_registry_path or Path(__file__).with_name("HISTORICAL-SOURCES.json"))
        self.lock = threading.RLock()
        self._workers = {}
        self._cancel_events = {}
        self._closed = False
        self.storage_error = None
        self.session_started_at = now()
        self._state = initial_state()
        self._lock_file = (self.data_dir / "writer.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._lock_file.seek(0)
                if self._lock_file.read(1) == b"":
                    self._lock_file.write(b"0")
                    self._lock_file.flush()
                self._lock_file.seek(0)
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            fail("STORAGE_LOCKED", "Another process already owns this data directory", 409)
        self._load()
        self._recover_fixture_runs()

    def close(self):
        # Admission, durable queue publication and worker registration share this
        # lock. Shutdown cannot release ownership while an admitted command is
        # still preparing a worker that has not appeared in the snapshot yet.
        with self.lock:
            self._closed = True
            workers = list(self._workers.values())
            for event in list(self._cancel_events.values()):
                event.set()
        for worker in workers:
            if worker is not threading.current_thread() and worker.ident is not None:
                worker.join(timeout=12)
        with self.lock:
            if any(worker.is_alive() for worker in workers):
                # Retain process ownership if an unexpected cleanup hang occurs.
                self.storage_error = "Shutdown incomplete: local fixture cleanup is unresolved; directory ownership retained."
                return
            if not self._lock_file.closed:
                if os.name == "nt":
                    import msvcrt
                    self._lock_file.seek(0)
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                self._lock_file.close()

    def _load(self):
        if not self.path.exists():
            return
        try:
            seen = set()
            with self.path.open("r", encoding="utf-8") as stream:
                for number, line in enumerate(stream, 1):
                    if not line.endswith("\n"):
                        raise ValueError("truncated batch at line " + str(number))
                    record = json.loads(line)
                    batch = record["batch"]
                    if not isinstance(batch, list) or not batch:
                        raise ValueError("invalid empty batch")
                    trial = copy.deepcopy(self._state)
                    for event in batch:
                        self._validate_event(event)
                        if event["seq"] != trial["version"] + 1 or event["id"] in seen or event["project_id"] != PROJECT_ID:
                            raise ValueError("invalid event order, identity or project")
                        uuid.UUID(event["id"])
                        known = {x["id"] for key in TABLES for x in trial[key]} | {e["id"] for e in trial["events"]}
                        if any(ref not in known for ref in event["references"]):
                            raise ValueError("missing historical reference")
                        self._apply(trial, event)
                        seen.add(event["id"])
                    self._state = trial
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            self.storage_error = "Storage history is invalid; mutations disabled: " + str(exc)

    @staticmethod
    def _validate_event(event):
        if not isinstance(event, dict) or type(event.get("seq")) is not int or not isinstance(event.get("payload"), dict):
            raise ValueError("invalid event envelope")
        for field in ("id", "type", "created_at", "project_id"):
            if not isinstance(event.get(field), str):
                raise ValueError("invalid event field: " + field)
        if "phase_id" not in event or (event["phase_id"] is not None and not isinstance(event["phase_id"], str)):
            raise ValueError("invalid or missing event phase_id")
        if not isinstance(event.get("actor"), dict) or event["actor"].get("kind") not in ("user", "primary_author", "reviewer", "system"):
            raise ValueError("invalid event actor")
        if not isinstance(event["actor"].get("name"), str):
            raise ValueError("invalid or missing actor name")
        if event.get("provenance") not in ("manual_import", "synthetic_fixture", "external_execution", "historical_external_execution"):
            raise ValueError("invalid provenance")
        if not isinstance(event.get("references"), list) or not all(isinstance(x, str) for x in event["references"]):
            raise ValueError("invalid reference list")
        entities = event["payload"].get("entities", {})
        if not isinstance(entities, dict) or not isinstance(event["payload"].get("project", {}), dict):
            raise ValueError("invalid entity/project update")
        payload = event["payload"]
        if "completion_key" in payload and (not isinstance(payload["completion_key"], str) or not payload["completion_key"]):
            raise ValueError("invalid completion key")
        if "evidence_refs" in payload and (not isinstance(payload["evidence_refs"], list) or not all(isinstance(x, str) for x in payload["evidence_refs"])):
            raise ValueError("invalid completion evidence references")
        for field, value in payload.get("project", {}).items():
            if field == "instruction_version" and type(value) is not int:
                raise ValueError("invalid project instruction version")
            if field in ("title", "status", "primary_author") and not isinstance(value, str):
                raise ValueError("invalid project text field")
            if field == "active_phase_id" and value is not None and not isinstance(value, str):
                raise ValueError("invalid active phase identity")
        required = {"instructions": ("text", "kind", "scope", "phase_id", "effective", "superseded_by"),
                    "phases": ("title", "status", "mode", "current_revision", "instruction_version"),
                    "reviews": ("phase_id", "reviewer", "stage", "revision", "instruction_version", "status", "findings", "provenance"),
                    "runs": ("phase_id", "status", "provider", "provenance"),
                    "contributions": ("phase_id", "revision", "base_revision", "integrated", "provenance"),
                    "evidence": ("phase_id", "kind", "revision", "status", "provenance"),
                    "uploads": ("name", "size", "status", "text", "received_bytes"),
                    "history_imports": ("source_id", "run_id", "receipt", "provenance"),
                    "execution_plans": ("run_id", "provider", "snapshot", "sha256", "provenance"),
                    "chat_turns": ("kind", "submission_id", "text", "providers", "run_ids", "mode", "context_snapshot", "context_sha256", "provenance")}
        for table, records in entities.items():
            if table not in required or not isinstance(records, list):
                raise ValueError("invalid entity table")
            for record in records:
                if not isinstance(record, dict) or not all(key in record for key in ("id", "project_id", "created_at") + required[table]):
                    raise ValueError("invalid " + table + " record")
                if not isinstance(record["id"], str) or record["project_id"] != PROJECT_ID:
                    raise ValueError("invalid entity identity")
                if not isinstance(record["created_at"], str):
                    raise ValueError("invalid entity timestamp")
                scalar_types = {
                    "instructions": {"text": str, "kind": str, "scope": str, "effective": bool, "seq": int},
                    "phases": {"title": str, "status": str, "mode": str, "current_revision": str, "instruction_version": int, "scenario_step": int, "round": int},
                    "reviews": {"reviewer": str, "stage": str, "revision": str, "instruction_version": int, "status": str, "provenance": str},
                    "runs": {"status": str, "provider": str, "provenance": str},
                    "contributions": {"revision": str, "base_revision": str, "integrated": bool, "provenance": str},
                    "evidence": {"kind": str, "status": str, "provenance": str},
                    "uploads": {"name": str, "size": int, "status": str, "text": str, "received_bytes": int},
                    "history_imports": {"source_id": str, "run_id": str, "receipt": dict, "provenance": str},
                    "execution_plans": {"run_id": str, "provider": str, "snapshot": dict, "sha256": str, "provenance": str},
                    "chat_turns": {"kind": str, "submission_id": str, "text": str, "mode": str, "context_snapshot": dict, "context_sha256": str, "provenance": str}}
                for field, expected in scalar_types[table].items():
                    if field in record and type(record[field]) is not expected:
                        raise ValueError("invalid " + table + " field type: " + field)
                for field in ("phase_id", "superseded_by", "source_event_id", "revision", "run_id"):
                    if field in record and record[field] is not None and not isinstance(record[field], str):
                        raise ValueError("invalid entity reference/revision")
                if table == "chat_turns" and (record["kind"] not in ("ask", "review") or record["mode"] not in ("synthetic", "live_pending")
                        or not all(isinstance(record[f], list) and all(isinstance(x, str) for x in record[f]) for f in ("providers", "run_ids"))):
                    raise ValueError("invalid chat turn")
                for field in ("supersedes", "references", "changed_files", "block_reasons"):
                    if field in record and (not isinstance(record[field], list) or not all(isinstance(x, str) for x in record[field])):
                        raise ValueError("invalid entity text/reference list")
                if table == "instructions" and (record["kind"] not in ("instruction", "correction", "summary") or record["scope"] not in ("project", "phase")):
                    raise ValueError("invalid instruction kind/scope")
                if table == "reviews":
                    if not isinstance(record["findings"], list) or any(not isinstance(f, dict) or not isinstance(f.get("text"), str) or f.get("severity") not in ("info", "warning", "blocking") for f in record["findings"]):
                        raise ValueError("invalid review findings")

    @staticmethod
    def _apply(state, event):
        changes = event["payload"].get("entities", {})
        for table, records in changes.items():
            if table not in TABLES:
                raise ValueError("Unknown entity table")
            for record in records:
                old = next((i for i, item in enumerate(state[table]) if item["id"] == record["id"]), None)
                if old is None:
                    state[table].append(copy.deepcopy(record))
                else:
                    state[table][old] = copy.deepcopy(record)
        state["project"].update(event["payload"].get("project", {}))
        state["events"].append(event)
        state["version"] = event["seq"]

    def _emit(self, kind, phase_id=None, provenance="manual_import", references=None, message="", entities=None, project=None, **extra):
        event = {"id": uid(), "seq": self._working["version"] + 1, "created_at": now(),
                 "project_id": PROJECT_ID, "phase_id": phase_id, "type": kind,
                 "actor": {"kind": "system" if provenance == "synthetic_fixture" else "user",
                           "name": "Synthetic fixture" if provenance == "synthetic_fixture" else "Local user"},
                 "provenance": provenance, "references": references or [],
                 "payload": {"message": message, "entities": entities or {}, "project": project or {}, **extra}}
        # Resolve self-references before taking the immutable event snapshot.
        # Materialized records and callers must never share event payload objects.
        for table, records in event["payload"]["entities"].items():
            for record in records:
                if table in ("instructions", "evidence") and record.get("source_event_id") is None:
                    record["source_event_id"] = event["id"]
                if table == "contributions" and kind == "contribution_integrated":
                    record["integration_event_id"] = event["id"]
                if table == "reviews" and kind == "contribution_integrated":
                    for finding in record.get("findings", []):
                        if finding.get("resolution_event_id") is None:
                            finding["resolution_event_id"] = event["id"]
                if table == "runs" and kind in ("review_requested", "reviews_requested"):
                    record["request_event_id"] = event["id"]
                if table == "runs" and kind == "review_received":
                    record["response_event_id"] = event["id"]
                if table == "runs" and kind in ("run_prepared", "fixture_run_queued", "historical_run_imported", "chat_turn_submitted"):
                    record["request_event_id"] = event["id"]
                if table == "runs" and kind == "fixture_run_finished":
                    record["response_event_id"] = event["id"]
        event = copy.deepcopy(event)
        known = {x["id"] for key in TABLES for x in self._working[key]} | {e["id"] for e in self._working["events"]}
        if any(ref not in known for ref in event["references"]):
            fail("INVALID_REFERENCE", "Reference does not exist in this project")
        self._apply(self._working, event)
        self._pending.append(event)
        return event

    def _entity(self, **fields):
        return {"id": uid(), "project_id": PROJECT_ID, "created_at": now(), **fields}

    def _get(self, table, entity_id):
        record = next((x for x in self._working[table] if x["id"] == entity_id), None)
        if record is None:
            fail("NOT_FOUND", table + " entity not found", 404, id=entity_id)
        return copy.deepcopy(record)

    def state(self):
        with self.lock:
            state = copy.deepcopy(self._state)
        state["capabilities"] = {"external_execution": False, "fixture_demo": True, "manual_import": True,
                                 "uploads": True, "max_upload_bytes": MAX_UPLOAD,
                                 "prepare_run": True, "local_fixture_subprocess": True, "historical_import": True,
                                 "chat_fanout": True, "chat_review": True, "chat_live_execution": False}
        state["storage_writable"] = self.storage_error is None
        if self.storage_error:
            state["warnings"].append(self.storage_error)
        active = state["project"]["active_phase_id"]
        state["effective_instructions"] = self._effective(state, active)
        state["connectors"] = self._connectors()
        state["connections"] = copy.deepcopy(state["connectors"])
        state["projects"] = [state["project"]]
        state["tasks"] = copy.deepcopy(state["phases"])
        state["agents"] = [{"id": "codex", "name": "dot / Codex", "label": "Codex", "connector_id": "codex_dot", "role": "primary_author", "status": "manual_handoff"},
                           {"id": "claude", "name": "Claude", "label": "Claude", "connector_id": "claude", "role": "code_reviewer", "status": "unconnected"},
                           {"id": "antigravity", "name": "Gemini / Antigravity", "label": "Gemini", "connector_id": "antigravity", "role": "context_reviewer", "status": "pending_specific_approval"}]
        cards = {item["id"]: item for item in state["connectors"]}
        for agent in state["agents"]:
            observed = cards[agent["connector_id"]]
            agent.update(status=observed["status"], detected=observed["detected"], authenticated=observed["authenticated"],
                         connected=observed["connected"], authorized=observed["authorized"])
        state["messages"] = [{"id": e["id"], "task_id": e["phase_id"], "agent_id": e["payload"].get("agent_id", "codex"),
                              "role": e["payload"].get("role", e["actor"]["kind"]),
                              "text": e["payload"].get("message", e["type"]), "type": e["type"],
                              "created_at": e["created_at"], "provenance": e["provenance"],
                              "references": e["references"]} for e in state["events"]]
        state["artifacts"] = [{**a, "kind": "contribution", "artifact_type": "contribution"} for a in state["contributions"]] + [
            {**a, "artifact_type": "evidence"} for a in state["evidence"]]
        state["verified_completions"] = []
        keys = set()
        for e in state["events"]:
            key = e["payload"].get("completion_key")
            if key and key not in keys and e["type"] == "verified_completion" and e["payload"].get("validation_status") == "confirmed":
                keys.add(key)
                state["verified_completions"].append(e)
        state["progress"] = {"verified_count": len(keys), "completion_keys": sorted(keys)}
        registry = self._history_registry()
        imported = {(item["source_id"], item.get("source_sha256")) for item in state["history_imports"]}
        state["chat_turns"] = self._chat_projection(state)
        state["chat_providers"] = [{"id": k, **v} for k, v in CHAT_PROVIDERS.items()]
        chat_runs = {r["id"] for r in state["runs"] if r.get("chat_turn_id")}
        for m, e in zip(state["messages"], state["events"]):
            if e["type"] == "chat_turn_submitted":
                m["chat_turn_id"] = e["payload"].get("chat_turn_id")
            elif any(r.get("id") in chat_runs for r in e["payload"].get("entities", {}).get("runs", [])):
                m["chat_internal"] = True
        state["history_sources"] = [{"id": key, "label": value.get("label", key), "provider": value.get("provider"),
                                    "sha256": value.get("sha256"), "already_imported": (key, value.get("sha256")) in imported,
                                    "recorded_execution_at": value.get("metadata", {}).get("started_at") if isinstance(value.get("metadata"), dict) else None,
                                    "source_kind": value.get("source_evidence", {}).get("kind") if isinstance(value.get("source_evidence"), dict) else None,
                                    "provenance": "historical_external_execution", "eligible_current_phase": False}
                                   for key, value in registry.items()]
        state["historical_sources"] = copy.deepcopy(state["history_sources"])
        return state

    @staticmethod
    def _effective(state, phase_id):
        applicable = [dict(x) for x in state["instructions"] if x.get("effective") and
                      (x["scope"] == "project" or x.get("phase_id") == phase_id)]
        priority = {"correction": 3, "instruction": 2, "summary": 1}
        return sorted(applicable, key=lambda x: (x.get("provenance") != "synthetic_fixture", priority.get(x["kind"], 0), x.get("seq", 0)), reverse=True)

    def _connectors(self):
        try:
            metadata = json.loads(self.connections_path.read_text(encoding="utf-8-sig"))
            if not isinstance(metadata, dict) or not isinstance(metadata.get("providers"), dict):
                raise ValueError("Invalid discovery metadata")
        except (OSError, ValueError, RecursionError):
            metadata = {"providers": {}}
        result = []
        for key, role in (("codex_dot", "primary_author"), ("claude", "code_reviewer"), ("antigravity", "context_reviewer")):
            observed = metadata.get("providers", {}).get(key, {})
            if not isinstance(observed, dict):
                observed = {}
            installed = observed.get("installed")
            detected = True if installed is True else False if isinstance(installed, str) and "not_found" in installed else None
            result.append({"id": key, "provider": key, "role": role, "detected": detected,
                           "executable_path": observed.get("binary"), "connected": False, "authorized": False,
                           "status": observed.get("live_execution", "unconnected"), "authenticated": observed.get("authenticated", "not_checked"),
                           "version": observed.get("version"), "account_identity": observed.get("account_identity", "unknown"),
                           "smoke": observed.get("smoke") if isinstance(observed.get("smoke"), dict) else None,
                           "reason": "Live provider execution requires specific approval. Installed/authenticated/historical and synthetic states remain separate.",
                           "checked_at": metadata.get("checked_at"), "supports": {"execute": False, "resume": False, "stream": False,
                           "prepare": True, "local_fixture": True, "historical_import": True}})
        return result

    def _history_registry(self):
        try:
            if self.history_registry_path.stat().st_size > 1024 * 1024:
                return {}
            registry = json.loads(self.history_registry_path.read_text(encoding="utf-8-sig"))
            if not isinstance(registry, dict) or len(registry) > 100:
                return {}
            return {key: value for key, value in registry.items() if isinstance(key, str) and isinstance(value, dict) and
                    isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str) and isinstance(value.get("provider"), str)}
        except (OSError, ValueError, RecursionError):
            return {}

    def _persist_pending(self):
        try:
            data = json.dumps({"batch": self._pending}, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
            with self.path.open("ab") as stream:
                stream.write(data.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, ValueError) as exc:
            self.storage_error = "Append outcome uncertain; restart and inspect history: " + str(exc)
            fail("STORAGE_ERROR", self.storage_error, 500)
        self._state = self._working

    def command(self, command, expected_version, payload=None):
        with self.lock:
            if self.storage_error:
                fail("STORAGE_ERROR", self.storage_error, 500)
            if self._closed:
                fail("SERVICE_CLOSED", "Local workroom is closing", 409)
            if type(expected_version) is not int or expected_version != self._state["version"]:
                fail("VERSION_CONFLICT", "Refresh state before retrying this command", 409, current_version=self._state["version"])
            if not isinstance(payload, dict):
                fail("INVALID_PAYLOAD", "payload must be an object")
            self._working = copy.deepcopy(self._state)
            self._pending = []
            self._after_commit = []
            self._command_result = {}
            self._dispatch(command, payload)
            if not self._pending:
                if self._command_result.get("unchanged") is True:
                    return {"ok": True, "version": self._state["version"], "event_ids": [], "state": self.state(), **self._command_result}
                fail("NO_CHANGE", "Command did not produce an event", 409)
            self._persist_pending()
            result = {"ok": True, "version": self._state["version"], "event_ids": [e["id"] for e in self._pending], "state": self.state(), **self._command_result}
            for action in self._after_commit:
                action()
            return result

    def _dispatch(self, command, p):
        methods = {name: getattr(self, "_" + name) for name in (
            "add_instruction", "start_phase", "request_reviews", "import_review", "add_contribution", "integrate_contribution",
            "add_evidence", "advance_phase", "complete_project", "load_demo", "scenario_start", "scenario_advance", "scenario_fail",
            "scenario_cancel", "begin_upload", "send_upload", "complete_upload", "cancel_upload", "send_message",
            "prepare_run", "import_history", "start_fixture_run", "cancel_run", "chat_send", "chat_review")}
        if command == "invoke_provider":
            fail("EXTERNAL_EXECUTION_NOT_AUTHORIZED", "No provider was invoked. Live execution requires future specific approval and transport implementation.", 403)
        if command not in methods:
            fail("UNKNOWN_COMMAND", "Unknown command")
        methods[command](p)

    def _optional_phase(self, p):
        phase_id = p.get("phase_id")
        return self._get("phases", phase_id) if phase_id is not None else None

    def _transport_bundle(self, provider, run_id, prompt, phase, *, mode, scenario="success", model=None, effort=None, conversation_id=None):
        try:
            from transport import prepare_transport_request
            return prepare_transport_request(provider, run_id=run_id, prompt=prompt,
                workspace_root=self.data_dir, journal_root=self.data_dir / "transport-journal",
                scenario=scenario, model=model, effort=effort, conversation_id=conversation_id,
                project_id=PROJECT_ID, phase_id=phase["id"] if phase else None,
                role="primary_author" if provider in ("codex", "codex_dot") else "reviewer",
                revision=phase["current_revision"] if phase else None,
                instruction_version=phase["instruction_version"] if phase else self._working["project"]["instruction_version"],
                instruction_snapshot=self._effective(self._working, phase["id"] if phase else None), mode=mode,
                connections_path=self.connections_path)
        except (ValueError, OSError, ImportError) as exc:
            fail("TRANSPORT_PREPARATION_FAILED", str(exc), 400)

    def _run(self, provider, phase, mode, status, **fields):
        return self._entity(phase_id=phase["id"] if phase else None, provider=provider,
            connector_id="codex_dot" if provider == "codex" else provider, role="primary_author" if provider == "codex" else "reviewer",
            mode=mode, status=status, provenance="synthetic_fixture" if mode == "synthetic" else "manual_import",
            authorization={"state": "not_granted"}, request_id=None, plan_id=None, intent_id=None,
            conversation_id=None, provider_run_id=None, requested_model=None, reported_model=None, model=None,
            requested_effort=None, reported_effort=None, effort=None, started_at=None, finished_at=None,
            dispatch_state="not_started", response=None, usage=None, failure=None, error=None,
            request_event_id=None, response_event_id=None, eligible_current_phase=False, **fields)

    def _prepare_run(self, p):
        if set(p) - {"provider", "phase_id", "prompt", "model", "effort", "conversation_id"}:
            fail("INVALID_PAYLOAD", "Preparation accepts provider, phase_id, prompt, model, effort and conversation_id only")
        phase = self._optional_phase(p)
        provider = p.get("provider")
        if provider == "codex_dot":
            provider = "codex"
        if provider not in ("codex", "claude", "antigravity"):
            fail("INVALID_PROVIDER", "Choose codex, claude or antigravity")
        prompt = text(p.get("prompt"), "prompt")
        model = text(p.get("model"), "model", 200, True)
        effort = text(p.get("effort"), "effort", 100, True)
        conversation = text(p.get("conversation_id"), "conversation_id", 200, True)
        run = self._run(provider, phase, "live_pending", "awaiting_approval", run_kind="prepared_live")
        bundle = self._transport_bundle(provider, run["id"], prompt, phase, mode="live_pending", model=model, effort=effort, conversation_id=conversation)
        request = bundle["request"]
        snapshot = bundle["plan"]
        plan = self._entity(id=snapshot["id"], run_id=run["id"], provider=provider, mode="live_pending", snapshot=snapshot, request=request,
            sha256=snapshot["plan_sha256"], binding_sha256=hashlib.sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            provenance="manual_import", immutable=True, authorization={"state": "not_granted"})
        run.update(plan_id=plan["id"], request_id=request.get("id"), requested_model=model, requested_effort=effort,
            expected_conversation_id=conversation, plan=snapshot, plan_sha256=plan["sha256"],
            preparation_notice="Prepared, awaiting explicit approval; no provider launched")
        e = self._emit("run_prepared", run["phase_id"], message=run["preparation_notice"], entities={"runs": [run], "execution_plans": [plan]})
        self._command_result = {"run_id": run["id"], "plan_id": plan["id"], "request_id": request.get("id")}

    def _start_fixture_run(self, p):
        if set(p) - {"scenario", "phase_id"}:
            fail("INVALID_PAYLOAD", "Fixtures accept a fixed scenario and optional phase_id only")
        phase = self._optional_phase(p)
        aliases = {"error": "nonzero_exit", "malformed": "malformed_output", "mismatch": "conversation_mismatch", "cancel": "wait_for_cancel"}
        scenario = p.get("scenario")
        if not isinstance(scenario, str):
            fail("INVALID_SCENARIO", "Choose a fixed local fixture scenario")
        scenario = aliases.get(scenario, scenario)
        if scenario not in ("success", "nonzero_exit", "malformed_output", "conversation_mismatch", "timeout", "output_overflow", "wait_for_cancel"):
            fail("INVALID_SCENARIO", "Choose a fixed local fixture scenario")
        if sum(x.is_alive() for x in self._workers.values()) >= 4:
            fail("RUN_LIMIT", "At most four local fixture workers may run concurrently", 409)
        run = self._run("antigravity", phase, "synthetic", "queued", run_kind="local_fixture", scenario=scenario)
        bundle = self._transport_bundle("antigravity", run["id"], "[SYNTHETIC] Fixed local subprocess fixture; no provider invocation.",
                                        phase, mode="synthetic", scenario=scenario)
        plan = self._entity(id=bundle["plan"]["id"], run_id=run["id"], provider="antigravity", mode="synthetic", snapshot=bundle["plan"], request=bundle["request"],
            sha256=bundle["plan"]["plan_sha256"], binding_sha256=hashlib.sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            provenance="synthetic_fixture", immutable=True, authorization={"state": "fixture_only"})
        run.update(plan_id=plan["id"], request_id=bundle["request"].get("id"), plan=bundle["plan"], plan_sha256=plan["sha256"])
        self._emit("fixture_run_queued", run["phase_id"], "synthetic_fixture", message="[SYNTHETIC] Actual local fixture process queued; no external provider call.",
                   entities={"runs": [run], "execution_plans": [plan]})
        self._command_result = {"run_id": run["id"], "plan_id": plan["id"]}
        self._after_commit.append(lambda: self._launch_fixture(run["id"], copy.deepcopy(bundle)))

    def _launch_fixture(self, run_id, bundle):
        if self._closed:
            self._record_fixture_update(run_id, {"status": "cancelled", "outcome": "cancelled", "error": "Service closed before local worker launch", "finished_at": now()})
            return
        cancellation = threading.Event()
        worker = threading.Thread(target=self._fixture_worker, args=(run_id, bundle, cancellation), daemon=True, name="workroom-fixture-" + run_id[:8])
        self._cancel_events[run_id] = cancellation
        self._workers[run_id] = worker
        try:
            worker.start()
        except RuntimeError as exc:
            self._record_fixture_update(run_id, {"status": "disconnected", "outcome": "uncertain", "error": "Local worker could not start: " + str(exc)})

    def _fixture_worker(self, run_id, bundle, cancellation):
        try:
            if not self._record_fixture_update(run_id, None):
                return
            from transport import execute_fixture
            receipt = execute_fixture(bundle, cancel_event=cancellation)
        except Exception as exc:
            receipt = {"status": "disconnected", "outcome": "uncertain", "error": "Local fixture transport failed: " + str(exc),
                       "failure": {"code": "TRANSPORT_ERROR", "message": str(exc), "stage": "worker", "retryable": False, "dispatch_uncertain": True},
                       "finished_at": now()}
        self._record_fixture_update(run_id, receipt)

    def _record_fixture_update(self, run_id, receipt):
        with self.lock:
            if self.storage_error:
                return False
            self._working = copy.deepcopy(self._state)
            self._pending = []
            run = self._get("runs", run_id)
            if run.get("run_kind") != "local_fixture" or run["status"] not in ("queued", "running", "cancel_requested"):
                return False
            if receipt is None:
                if run["status"] == "queued":
                    run["status"] = "running"
                self._emit("fixture_worker_started", run["phase_id"], "synthetic_fixture", message="[SYNTHETIC] Bounded local fixture worker started; launch evidence follows in its receipt.", entities={"runs": [run]})
            else:
                receipt = copy.deepcopy(receipt)
                status = receipt.get("status")
                if status not in ("succeeded", "failed", "cancelled", "disconnected", "timed_out", "interrupted", "uncertain"):
                    status = "disconnected"
                if run["status"] == "cancel_requested" and status != "cancelled":
                    # 취소 요청 뒤 도착한 결과는 취소된 실행을 완료로 되살리지 않는다. 원문은 별도 필드에만 보존.
                    late = {"status": status, "response": receipt.get("response", receipt.get("report")), "error": receipt.get("error"),
                            "finished_at": receipt.get("finished_at"), "arrived_at": now()}
                    run.update(status="cancelled", outcome="cancelled", late_receipt=late, late_response_ignored=True,
                        response=None, error="취소 요청 뒤 결과가 도착해 응답으로 반영하지 않음", finished_at=now(), receipt=receipt,
                        provenance="synthetic_fixture" if run.get("mode") == "synthetic" else run.get("provenance"))
                    self._emit("run_late_result_ignored", run["phase_id"], "synthetic_fixture" if run.get("mode") == "synthetic" else "manual_import",
                        references=[run_id], message="[SYNTHETIC] 취소 뒤 도착한 결과를 기록만 하고 완료로 처리하지 않음: " + status, entities={"runs": [run]})
                    try:
                        self._persist_pending()
                    except DomainError:
                        return False
                    return True
                run.update(status=status, receipt=receipt, outcome=receipt.get("outcome", "uncertain"),
                    provenance="synthetic_fixture", eligible_current_phase=False, attested=False,
                    conversation_id=receipt.get("conversation_id"), provider_run_id=receipt.get("provider_run_id"),
                    reported_model=receipt.get("model"), model=receipt.get("model"), reported_effort=receipt.get("effort"), effort=receipt.get("effort"),
                    response=receipt.get("response", receipt.get("report")), usage=receipt.get("usage"),
                    failure=receipt.get("failure"), error=receipt.get("error"), intent_id=receipt.get("intent_id"),
                    started_at=receipt.get("started_at"), finished_at=receipt.get("finished_at", now()),
                    dispatch_state=receipt.get("dispatch_state", "terminal_response" if status == "succeeded" else "uncertain"))
                self._emit("fixture_run_finished", run["phase_id"], "synthetic_fixture", references=[run_id],
                    message="[SYNTHETIC] Actual local fixture process outcome: " + status + ". No provider inference or phase verification.", entities={"runs": [run]})
            try:
                self._persist_pending()
            except DomainError:
                return False
            return True

    def _recover_fixture_runs(self):
        if self.storage_error:
            return
        active = [run for run in self._state["runs"] if run.get("run_kind") == "local_fixture" and run["status"] in ("queued", "running", "cancel_requested")]
        if not active:
            return
        try:
            from transport import recover_intents
            receipts = {item.get("run_id"): item for item in recover_intents(self.data_dir / "transport-journal") if isinstance(item, dict) and isinstance(item.get("run_id"), str)}
        except (ImportError, ValueError, OSError, TypeError, RecursionError):
            receipts = {}
        for run in active:
            receipt = receipts.get(run["id"])
            if receipt and receipt.get("status") in ("succeeded", "failed", "cancelled", "timed_out", "uncertain"):
                self._record_fixture_update(run["id"], receipt)
            else:
                self._record_fixture_update(run["id"], {"status": "disconnected", "outcome": "uncertain", "finished_at": now(),
                    "error": "Service restarted before a terminal fixture receipt; this attempt will not be resent.",
                    "failure": {"code": "INTERRUPTED", "stage": "recovery", "message": "Previous attempt outcome uncertain", "retryable": False, "dispatch_uncertain": True}})

    def _cancel_run(self, p):
        if set(p) != {"run_id"}:
            fail("INVALID_PAYLOAD", "Cancellation accepts run_id only")
        run = self._get("runs", p.get("run_id"))
        if run["status"] in ("succeeded", "failed", "cancelled", "disconnected", "timed_out", "interrupted", "uncertain"):
            self._command_result = {"run_id": run["id"], "unchanged": True}
            return
        if run.get("run_kind") == "prepared_live" and run.get("chat_turn_id") and run["status"] == "awaiting_approval":
            run.update(status="cancelled", outcome="cancelled", cancel_requested_at=now(), finished_at=now(), dispatch_state="not_started")
            self._emit("chat_prepared_run_cancelled", run["phase_id"], references=[run["id"]],
                message="승인 대기 중이던 실제 요청 계획을 취소함(전송된 적 없음)", entities={"runs": [run]})
            self._command_result = {"run_id": run["id"]}
            return
        if run.get("run_kind") != "local_fixture":
            fail("NO_ACTIVE_FIXTURE", "Only an active local fixture worker can be cancelled", 409)
        run.update(status="cancel_requested", cancel_requested_at=now())
        self._emit("fixture_cancel_requested", run["phase_id"], "synthetic_fixture", references=[run["id"]],
            message="[SYNTHETIC] Cancellation requested; waiting for local child cleanup and terminal receipt.", entities={"runs": [run]})
        self._command_result = {"run_id": run["id"]}
        signal = self._cancel_events.get(run["id"])
        if signal:
            self._after_commit.append(signal.set)

    def _import_history(self, p):
        if set(p) != {"source_id"}:
            fail("INVALID_HISTORY_SOURCE", "Only a registered source_id is accepted; paths and URLs cannot be supplied")
        source_id = text(p.get("source_id"), "source_id", 200)
        registry = self._history_registry()
        if source_id not in registry:
            fail("UNKNOWN_HISTORY_SOURCE", "Historical source is not in the reviewed local registry", 404)
        if any(item["source_id"] == source_id and item.get("source_sha256") == registry[source_id]["sha256"] for item in self._working["history_imports"]):
            fail("HISTORY_ALREADY_IMPORTED", "This registered source/hash was already imported", 409)
        from transport import import_historical_output
        try:
            receipt = import_historical_output(source_id, registry=registry)
        except (ValueError, OSError) as exc:
            fail("HISTORY_IMPORT_FAILED", str(exc), 400)
        run = self._run(registry[source_id]["provider"], None, "historical", receipt.get("status", "uncertain"), run_kind="historical_import")
        receipt = {**receipt, "archive_parser_run_id": receipt.get("run_id"), "run_id": run["id"]}
        run.update(provenance="historical_external_execution", receipt=receipt, source_id=source_id, source_sha256=registry[source_id]["sha256"],
            conversation_id=receipt.get("conversation_id"), provider_run_id=receipt.get("provider_run_id"),
            reported_model=receipt.get("model"), model=receipt.get("model"), reported_effort=receipt.get("effort"), effort=receipt.get("effort"),
            response=receipt.get("response", receipt.get("report")), usage=receipt.get("usage"), failure=receipt.get("failure"), error=receipt.get("error"),
            started_at=receipt.get("started_at"), finished_at=receipt.get("finished_at"), imported_at=now(),
            historical_notice="Previously recorded external execution; imported now. No current provider call or verified review.")
        record = self._entity(source_id=source_id, run_id=run["id"], source_sha256=registry[source_id]["sha256"], receipt=receipt,
            provenance="historical_external_execution", eligible_current_phase=False, imported_at=now())
        self._emit("historical_run_imported", provenance="historical_external_execution", message=run["historical_notice"],
                   entities={"runs": [run], "history_imports": [record]})
        self._command_result = {"run_id": run["id"], "history_import_id": record["id"]}


    # ------------------------------------------------------------------ 통합 채팅
    @staticmethod
    def chat_context_snapshot(state):
        """한 번의 동시 요청에 쓰는 공통 맥락: 그 시점의 유효 프로젝트 지시만(대화 전체·파일은 넣지 않음)."""
        items = [{"id": x["id"], "kind": x["kind"], "text": x["text"]} for x in Workroom._effective(state, None) if x.get("scope") == "project"]
        snap = {"instruction_version": state["project"].get("instruction_version", 0), "instructions": items}
        digest = hashlib.sha256(json.dumps(snap, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return snap, digest

    @staticmethod
    def chat_context_text(snapshot):
        lines = ["[Glance 공통 맥락 v" + str(snapshot["instruction_version"]) + "]"]
        lines += ["- (" + x["kind"] + ") " + x["text"] for x in snapshot["instructions"]] or ["- (없음)"]
        return "\n".join(lines)

    @staticmethod
    def chat_ask_prompt(question, snapshot):
        return Workroom.chat_context_text(snapshot) + "\n\n[질문]\n" + question

    @staticmethod
    def chat_review_prompt(question, answer, source_provider, snapshot):
        name = CHAT_PROVIDERS[source_provider]["display"]
        return (Workroom.chat_context_text(snapshot) + "\n\n[원래 질문]\n" + question +
                "\n\n[검토할 답변 · " + name + "]\n" + answer +
                "\n\n[요청]\n위 답변을 원래 질문과 공통 맥락에 비추어 검토하고, 틀리거나 빠진 점과 근거를 짧게 적어 주세요.")

    def _chat_submission(self, p, allowed):
        if set(p) - allowed:
            fail("INVALID_PAYLOAD", "Unexpected fields: " + ", ".join(sorted(set(p) - allowed)))
        sid = p.get("submission_id")
        if not isinstance(sid, str) or not SUBMISSION_ID.fullmatch(sid):
            fail("INVALID_SUBMISSION_ID", "submission_id must be 8-80 letters, digits, _ or -")
        existing = next((t for t in self._working["chat_turns"] if t["submission_id"] == sid), None)
        mode = p.get("mode", "synthetic")
        if mode not in ("synthetic", "live_pending"):
            fail("INVALID_MODE", "mode must be synthetic or live_pending")
        return sid, existing, mode

    def _chat_runs(self, providers, prompt, mode, turn_id, scenarios, models, chat_role):
        if mode == "synthetic":
            alive = sum(x.is_alive() for x in self._workers.values())
            if alive + len(providers) > CHAT_WORKER_LIMIT:
                fail("RUN_LIMIT", "동시에 실행할 수 있는 합성 실행 수(" + str(CHAT_WORKER_LIMIT) + ")를 넘습니다. 일부를 기다리거나 취소하세요.", 409,
                     active=alive, requested=len(providers))
        runs, plans, launches = [], [], []
        for provider in providers:
            scenario = scenarios.get(provider, "success") if mode == "synthetic" else "success"
            model = models.get(provider)
            if mode == "synthetic":
                run = self._run(provider, None, "synthetic", "queued", run_kind="local_fixture", scenario=scenario)
            else:
                run = self._run(provider, None, "live_pending", "awaiting_approval", run_kind="prepared_live")
            run.update(chat_turn_id=turn_id, chat_role=chat_role, display_name=CHAT_PROVIDERS[provider]["display"],
                       tool_name=CHAT_PROVIDERS[provider]["tool"])
            bundle = self._transport_bundle(provider, run["id"], prompt, None, mode=mode, scenario=scenario, model=model)
            snapshot = bundle["plan"]
            plan = self._entity(id=snapshot["id"], run_id=run["id"], provider=provider, mode=mode, snapshot=snapshot, request=bundle["request"],
                sha256=snapshot["plan_sha256"], binding_sha256=hashlib.sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                provenance="synthetic_fixture" if mode == "synthetic" else "manual_import", immutable=True,
                authorization={"state": "fixture_only" if mode == "synthetic" else "not_granted"})
            run.update(plan_id=plan["id"], request_id=bundle["request"].get("id"), requested_model=model, plan=snapshot, plan_sha256=plan["sha256"],
                       input_sha256=bundle["request"]["input_sha256"])
            if mode == "live_pending":
                run.update(preparation_notice="실제 요청 계획만 준비됨 · 별도 승인 전 전송하지 않음")
            runs.append(run); plans.append(plan)
            if mode == "synthetic":
                launches.append((run["id"], bundle))
        return runs, plans, launches

    def _chat_send(self, p):
        sid, existing, mode = self._chat_submission(p, {"text", "providers", "submission_id", "mode", "models", "fixture_scenarios"})
        question = text(p.get("text"))
        providers = p.get("providers")
        aliases = {"chatgpt": "codex", "codex_dot": "codex", "gemini": "antigravity"}
        if not isinstance(providers, list) or not providers or not all(isinstance(x, str) for x in providers):
            fail("INVALID_PROVIDERS", "ChatGPT·Claude·Gemini 중 하나 이상을 고르세요")
        providers = [aliases.get(x, x) for x in providers]
        if any(x not in CHAT_PROVIDERS for x in providers) or len(set(providers)) != len(providers):
            fail("INVALID_PROVIDERS", "알 수 없거나 중복된 제공자가 있습니다")
        if existing is not None:
            if existing["kind"] != "ask" or existing["text"] != question or existing["providers"] != providers or existing["mode"] != mode:
                fail("SUBMISSION_ID_REUSED", "같은 submission_id로 다른 요청을 보낼 수 없습니다", 409, turn_id=existing["id"])
            self._command_result = {"turn_id": existing["id"], "run_ids": existing["run_ids"], "duplicate": True, "unchanged": True}
            return
        scenarios = p.get("fixture_scenarios") or {}
        models = p.get("models") or {}
        if not isinstance(scenarios, dict) or not isinstance(models, dict):
            fail("INVALID_PAYLOAD", "fixture_scenarios and models must be objects")
        if mode != "synthetic" and scenarios:
            fail("INVALID_PAYLOAD", "fixture_scenarios is only for synthetic mode")
        for k, v in list(models.items()):
            models[aliases.get(k, k)] = text(v, "model", 200, True)
        snap, digest = self.chat_context_snapshot(self._working)
        prompt = self.chat_ask_prompt(question, snap)
        turn = self._entity(kind="ask", submission_id=sid, text=question, providers=providers, run_ids=[], mode=mode,
                            context_snapshot=snap, context_sha256=digest, prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                            review_of=None, provenance="manual_import")
        runs, plans, launches = self._chat_runs(providers, prompt, mode, turn["id"], scenarios, models, "answer")
        turn["run_ids"] = [r["id"] for r in runs]
        self._emit("chat_turn_submitted", None, message=question, entities={"chat_turns": [turn], "runs": runs, "execution_plans": plans},
                   role="user", chat_turn_id=turn["id"], submission_id=sid)
        self._command_result = {"turn_id": turn["id"], "run_ids": turn["run_ids"], "duplicate": False}
        for run_id, bundle in launches:
            self._after_commit.append(lambda r=run_id, b=copy.deepcopy(bundle): self._launch_fixture(r, b))

    def _chat_review(self, p):
        sid, existing, mode = self._chat_submission(p, {"source_run_id", "reviewer", "submission_id", "mode", "fixture_scenario", "model"})
        source = self._get("runs", p.get("source_run_id"))
        reviewer = {"chatgpt": "codex", "codex_dot": "codex", "gemini": "antigravity"}.get(p.get("reviewer"), p.get("reviewer"))
        if existing is not None:
            if existing["kind"] != "review" or existing["providers"] != [reviewer] or (existing["review_of"] or {}).get("run_id") != source["id"]:
                fail("SUBMISSION_ID_REUSED", "같은 submission_id로 다른 요청을 보낼 수 없습니다", 409, turn_id=existing["id"])
            self._command_result = {"turn_id": existing["id"], "run_ids": existing["run_ids"], "duplicate": True, "unchanged": True}
            return
        if not source.get("chat_turn_id"):
            fail("NOT_A_CHAT_ANSWER", "통합 채팅의 답변만 교차 검토할 수 있습니다")
        if source["status"] != "succeeded" or not isinstance(source.get("response"), str) or not source["response"].strip():
            fail("ANSWER_NOT_AVAILABLE", "완료된 답변만 검토 대상으로 보낼 수 있습니다", 409)
        if reviewer not in CHAT_PROVIDERS:
            fail("INVALID_PROVIDERS", "검토할 제공자를 고르세요")
        if reviewer == source["provider"]:
            fail("SAME_PROVIDER_REVIEW", "답변한 제공자와 다른 제공자를 고르세요")
        source_turn = self._get("chat_turns", source["chat_turn_id"])
        root = source_turn if source_turn["kind"] == "ask" else self._get("chat_turns", source_turn["review_of"]["root_turn_id"])
        snap, digest = source_turn["context_snapshot"], source_turn["context_sha256"]
        answer = source["response"]
        prompt = self.chat_review_prompt(root["text"], answer, source["provider"], snap)
        scenario = p.get("fixture_scenario")
        if mode != "synthetic" and scenario:
            fail("INVALID_PAYLOAD", "fixture_scenario is only for synthetic mode")
        turn = self._entity(kind="review", submission_id=sid, text=root["text"], providers=[reviewer], run_ids=[], mode=mode,
                            context_snapshot=snap, context_sha256=digest, prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                            review_of={"run_id": source["id"], "turn_id": source_turn["id"], "root_turn_id": root["id"],
                                       "source_provider": source["provider"], "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
                                       "source_event_id": source.get("response_event_id")},
                            provenance="manual_import")
        runs, plans, launches = self._chat_runs([reviewer], prompt, mode, turn["id"], {reviewer: scenario} if scenario else {},
                                                {reviewer: text(p.get("model"), "model", 200, True)} if p.get("model") else {}, "review")
        turn["run_ids"] = [r["id"] for r in runs]
        refs = [source["id"], source_turn["id"]] + ([source["response_event_id"]] if source.get("response_event_id") else [])
        self._emit("chat_turn_submitted", None, references=refs,
                   message="교차 검토 요청 · " + CHAT_PROVIDERS[source["provider"]]["display"] + " 답변 → " + CHAT_PROVIDERS[reviewer]["display"],
                   entities={"chat_turns": [turn], "runs": runs, "execution_plans": plans}, role="user", chat_turn_id=turn["id"], submission_id=sid)
        self._command_result = {"turn_id": turn["id"], "run_ids": turn["run_ids"], "duplicate": False}
        for run_id, bundle in launches:
            self._after_commit.append(lambda r=run_id, b=copy.deepcopy(bundle): self._launch_fixture(r, b))

    def _chat_projection(self, state):
        runs = {r["id"]: r for r in state["runs"]}
        turns = []
        for t in state["chat_turns"]:
            items = []
            for rid in t["run_ids"]:
                r = runs.get(rid, {})
                finished = r.get("finished_at")
                items.append({"run_id": rid, "provider": r.get("provider"), "display_name": CHAT_PROVIDERS.get(r.get("provider"), {}).get("display"),
                    "tool_name": CHAT_PROVIDERS.get(r.get("provider"), {}).get("tool"), "status": r.get("status"), "mode": r.get("mode"),
                    "provenance": r.get("provenance"), "run_kind": r.get("run_kind"), "response": r.get("response") if r.get("status") == "succeeded" else None, "error": r.get("error"),
                    "failure": r.get("failure"), "requested_model": r.get("requested_model"), "reported_model": r.get("reported_model"),
                    "request_id": r.get("request_id"), "input_sha256": r.get("input_sha256"), "started_at": r.get("started_at"), "finished_at": finished,
                    "late_response_ignored": bool(r.get("late_response_ignored")), "late_receipt": r.get("late_receipt"),
                    "stored_before_session": bool(finished and finished < self.session_started_at),
                    "response_event_id": r.get("response_event_id"), "preparation_notice": r.get("preparation_notice")})
            turns.append({**copy.deepcopy(t), "runs": items})
        return turns

    def _send_message(self, p):
        phase = p.get("task_id")
        if phase is not None:
            self._get("phases", phase)
        agent = p.get("agent_id")
        if agent is not None and agent not in ("codex", "claude", "antigravity"):
            fail("INVALID_AGENT", "Choose an existing role")
        refs = p.get("references", [])
        if not isinstance(refs, list) or not all(isinstance(x, str) for x in refs) or len(refs) > 100:
            fail("INVALID_REFERENCE", "references must be a bounded array of existing IDs")
        self._emit("user_message", phase, references=refs, message=text(p.get("text")), agent_id=agent or "codex", role="user")

    def _add_instruction(self, p):
        kind, scope = p.get("kind", "instruction"), p.get("scope", "project")
        if kind not in ("instruction", "correction", "summary") or scope not in ("project", "phase"):
            fail("INVALID_FIELD", "Invalid instruction kind or scope")
        phase = p.get("phase_id")
        if scope == "phase":
            self._get("phases", phase)
        elif phase is not None:
            fail("INVALID_SCOPE", "Project scope must not contain phase_id")
        supersedes = p.get("supersedes", [])
        if not isinstance(supersedes, list) or not all(isinstance(x, str) for x in supersedes):
            fail("INVALID_REFERENCE", "supersedes must be an array of instruction IDs")
        record = self._entity(text=text(p.get("text")), kind=kind, scope=scope, phase_id=phase,
                              supersedes=supersedes, source_event_id=None, effective=True, superseded_by=None,
                              seq=self._working["version"] + 1, provenance="manual_import")
        updated = []
        for old_id in supersedes:
            old = self._get("instructions", old_id)
            if kind == "summary" or (old["scope"], old["phase_id"]) != (scope, phase):
                fail("INVALID_SUPERSESSION", "Only user instructions in the same scope can supersede a clause")
            old.update(effective=False, superseded_by=record["id"])
            updated.append(old)
        iv = self._working["project"]["instruction_version"] + (kind != "summary")
        event = self._emit("instruction_added", phase, references=supersedes, message=record["text"],
                           entities={"instructions": updated + [record]}, project={"instruction_version": iv})
        if kind != "summary":
            stale = []
            for review in self._working["reviews"]:
                if scope == "project" or review["phase_id"] == phase:
                    stale.append({**review, "status": "stale"})
            phases = [{**x, "instruction_version": iv} for x in self._working["phases"] if
                      x["status"] not in ("done", "blocked", "cancelled") and (scope == "project" or x["id"] == phase)]
            if stale or phases:
                self._emit("reviews_invalidated", phase, message="Effective user instructions changed; explicitly request fresh reviews.",
                           entities={"reviews": stale, "phases": phases})

    def _start_phase(self, p):
        if any(x["mode"] == "real" and x["status"] not in ("done", "blocked", "cancelled") for x in self._working["phases"]):
            fail("ACTIVE_PHASE", "Finish or block the current real phase first", 409)
        previous = p.get("previous_phase_id")
        if previous:
            old = self._get("phases", previous)
            if old["status"] not in ("blocked", "done", "cancelled"):
                fail("INVALID_PREVIOUS_PHASE", "Previous phase is still active", 409)
        revision = text(p.get("draft_revision"), "draft_revision", 200)
        phase = self._entity(title=text(p.get("title"), "title", 200), status="draft", mode="real", round=1,
                             draft_revision=revision, current_revision=revision,
                             instruction_version=self._working["project"]["instruction_version"], block_reasons=[], previous_phase_id=previous)
        self._emit("phase_started", phase["id"], message="User started real phase: " + phase["title"], entities={"phases": [phase]},
                   project={"active_phase_id": phase["id"], "status": "active"})

    def _request_reviews(self, p):
        phase = self._get("phases", p.get("phase_id"))
        stage = p.get("stage")
        allowed = ("draft", "reviewing") if stage == "initial" else ("revising", "revalidating")
        if stage not in ("initial", "revalidation") or phase["status"] not in allowed:
            fail("INVALID_TRANSITION", "Reviews cannot be requested in this state", 409)
        if stage == "revalidation":
            gates = self._revision_gates(phase)
            if gates:
                fail("GATES_UNMET", "Revision is not ready for revalidation", 409, gates=gates)
        fresh = [x for x in self._working["reviews"] if x["phase_id"] == phase["id"] and x["stage"] == stage and x["status"] != "stale"]
        if fresh:
            fail("REVIEWS_ALREADY_REQUESTED", "This round already has current review requests", 409)
        reviews, runs = [], []
        for reviewer in REVIEWERS:
            run = self._entity(phase_id=phase["id"], role="code_reviewer" if reviewer == "claude" else "context_reviewer",
                               provider=reviewer, connector_id=reviewer, status="disabled", provenance="manual_import", model=None, effort=None,
                               conversation_id=None, provider_run_id=None, started_at=None, finished_at=None,
                               error="External execution disabled; request prepared, not dispatched", request_event_id=None, response_event_id=None)
            review = self._entity(phase_id=phase["id"], reviewer=reviewer, stage=stage, revision=phase["current_revision"],
                                  instruction_version=phase["instruction_version"], status="requested", verdict=None, findings=[],
                                  request_text="Review exact revision, effective instructions and referenced evidence.", response_text="",
                                  instruction_snapshot=self._effective(self._working, phase["id"]), run_id=run["id"], references=[],
                                  provenance="manual_import", verified=False)
            reviews.append(review)
            runs.append(run)
        phase["status"] = "reviewing" if stage == "initial" else "revalidating"
        self._emit("reviews_requested", phase["id"], message="Two separate reviewer requests prepared. Neither was dispatched.",
                   entities={"phases": [phase], "reviews": reviews, "runs": runs})

    def _import_review(self, p):
        review = self._get("reviews", p.get("review_id"))
        if review["status"] != "requested":
            fail("INVALID_REVIEW_STATE", "Only a pending current review can receive an import", 409)
        verdict = p.get("verdict")
        if verdict not in ("pass", "changes_requested", "blocked", "pass_with_notes"):
            fail("INVALID_VERDICT", "Unsupported verdict")
        findings = p.get("findings", [])
        if not isinstance(findings, list) or len(findings) > 100:
            fail("INVALID_FINDINGS", "findings must be a bounded array")
        clean = []
        for finding in findings:
            if not isinstance(finding, dict) or finding.get("severity") not in ("info", "warning", "blocking"):
                fail("INVALID_FINDINGS", "Each finding needs a valid severity")
            refs = finding.get("instruction_refs", [])
            if not isinstance(refs, list):
                fail("INVALID_REFERENCE", "instruction_refs must be an array")
            for ref in refs:
                self._get("instructions", ref)
            clean.append(self._entity(category=text(finding.get("category", "correctness"), "category", 100),
                                      severity=finding["severity"], text=text(finding.get("text")), instruction_refs=refs,
                                      file_refs=finding.get("file_refs", []), resolution_event_id=None))
        review.update(status="received", verdict=verdict, findings=clean, response_text=text(p.get("response_text")),
                      claimed_provider=text(p.get("claimed_provider"), "claimed_provider", 200, True), provenance="manual_import", verified=False)
        for field in ("conversation_id", "model", "effort"):
            review[field] = text(p.get(field), field, 200, True)
        self._emit("review_imported", review["phase_id"], references=[review["id"]], message=review["response_text"],
                   entities={"reviews": [review]}, agent_id=review["reviewer"], role="reviewer")

    def _add_contribution(self, p):
        phase = self._get("phases", p.get("phase_id"))
        files = p.get("changed_files", [])
        if not isinstance(files, list) or not 1 <= len(files) <= 100:
            fail("INVALID_FILES", "Provide 1 to 100 changed file names")
        for name in files:
            text(name, "changed_file", 500)
        record = self._entity(phase_id=phase["id"], author="Isolated helper (manual report)", workspace_path=text(p.get("workspace_path"), "workspace_path", 1000),
                              base_revision=text(p.get("base_revision"), "base_revision", 200), revision=text(p.get("revision"), "revision", 200),
                              changed_files=files, diff_text=text(p.get("diff_text")), integrated=False, integration_event_id=None, provenance="manual_import")
        self._emit("contribution_added", phase["id"], message="Manual isolated helper contribution; primary author has not integrated it.", entities={"contributions": [record]})

    def _integrate_contribution(self, p):
        phase = self._get("phases", p.get("phase_id"))
        record = self._get("contributions", p.get("contribution_id"))
        if p.get("primary_author_confirmation") is not True:
            fail("CONFIRMATION_REQUIRED", "Record explicit primary author integration confirmation", 403)
        if phase["status"] != "revising" or record["phase_id"] != phase["id"] or record["base_revision"] != phase["current_revision"] or record["revision"] == phase["current_revision"] or record["integrated"]:
            fail("INVALID_INTEGRATION", "Contribution must revise the current base in revising state", 409)
        phase["current_revision"] = record["revision"]
        record["integrated"] = True
        event = self._emit("contribution_integrated", phase["id"], references=[record["id"]], message="User recorded explicit primary-author integration (manual confirmation).",
                           entities={"phases": [phase], "contributions": [record]})

    def _add_evidence(self, p):
        phase = self._get("phases", p.get("phase_id"))
        if p.get("kind") not in ("test", "change", "document") or p.get("status") not in ("passed", "failed", "not_run", "unknown"):
            fail("INVALID_EVIDENCE", "Invalid evidence kind or status")
        record = self._entity(phase_id=phase["id"], kind=p["kind"], status=p["status"], label=text(p.get("label"), "label", 200),
                              revision=text(p.get("revision"), "revision", 200), command=text(p.get("command"), "command", 2000, True),
                              output=text(p.get("output")), artifact_path=text(p.get("artifact_path"), "artifact_path", 2000, True),
                              provenance="manual_import", source_event_id=None)
        event = self._emit("evidence_added", phase["id"], message="Manually supplied evidence; command was not executed by this server.", entities={"evidence": [record]})

    def _valid_reviews(self, phase, stage):
        return [x for x in self._working["reviews"] if x["phase_id"] == phase["id"] and x["stage"] == stage and x["status"] == "received" and
                x["revision"] == phase["current_revision"] and x["instruction_version"] == phase["instruction_version"] and
                ((phase["mode"] == "synthetic_fixture" and x["provenance"] == "synthetic_fixture") or
                 (phase["mode"] == "real" and x["provenance"] == "external_execution" and x.get("verified") is True))]

    def _revision_gates(self, phase):
        gates = []
        if not any(x["phase_id"] == phase["id"] and x["integrated"] and x["revision"] == phase["current_revision"] for x in self._working["contributions"]):
            gates.append("Explicit primary-author integration of current revision is required")
        tests = [x for x in self._working["evidence"] if x["phase_id"] == phase["id"] and x["kind"] == "test" and x["revision"] == phase["current_revision"]]
        if not tests or tests[-1]["status"] != "passed":
            gates.append("Latest test evidence for current revision must be passed")
        return gates

    def _advance_phase(self, p):
        phase = self._get("phases", p.get("phase_id"))
        target = p.get("target")
        if (phase["status"], target) not in (("reviewing", "revising"), ("revalidating", "done")):
            fail("INVALID_TRANSITION", "Only a gated next transition is allowed", 409)
        stage = "initial" if target == "revising" else "revalidation"
        reviews = self._valid_reviews(phase, stage)
        missing = [name for name in REVIEWERS if not any(x["reviewer"] == name for x in reviews)]
        if missing:
            fail("GATES_UNMET", "Two verified current reviews are required; manual imports are unverified", 409, missing_reviewers=missing)
        if target == "done":
            bad = [x for x in reviews if x["verdict"] != "pass" or any(f["severity"] == "blocking" and not f.get("resolution_event_id") for f in x["findings"])]
            if bad:
                phase.update(status="blocked", block_reasons=["Revalidation did not pass; explicitly start a new round."])
                self._emit("phase_blocked", phase["id"], message=phase["block_reasons"][0], entities={"phases": [phase]})
                return
            gates = self._revision_gates(phase)
            if gates:
                fail("GATES_UNMET", "Completion evidence missing", 409, gates=gates)
        phase["status"] = target
        self._emit("phase_advanced", phase["id"], message="Phase advanced to " + target + "; project remains active.", entities={"phases": [phase]})

    def _complete_project(self, p):
        phases = [x for x in self._working["phases"] if x["mode"] == "real"]
        if p.get("user_confirmation") is not True:
            fail("CONFIRMATION_REQUIRED", "Explicit user confirmation required", 403)
        if not phases or not any(x["status"] == "done" for x in phases) or any(x["status"] not in ("done", "blocked", "cancelled") for x in phases):
            fail("GATES_UNMET", "A real completed phase and no unfinished real phases are required", 409)
        self._emit("project_completed", message="User explicitly completed the real project.", project={"status": "complete"})

    def _scenario_start(self, p):
        phase = self._entity(title="Synthetic fixture: bounded correction review", status="draft", mode="synthetic_fixture", round=1,
                             draft_revision="fixture-r1", current_revision="fixture-r1", instruction_version=self._working["project"]["instruction_version"],
                             block_reasons=[], previous_phase_id=None, scenario_step=0)
        self._emit("scenario_started", phase["id"], "synthetic_fixture", message="[SYNTHETIC] dot/Codex drafts bounded_total in an isolated fixture workspace.",
                   entities={"phases": [phase]}, project={"active_phase_id": phase["id"]}, agent_id="codex")
        # Demonstration clauses are phase-scoped synthetic records. They confer
        # no real user authority and cannot alter a real phase's instruction set.
        old = self._entity(text="[SYNTHETIC] Clamp only values above ten.", kind="instruction", scope="phase", phase_id=phase["id"],
                           supersedes=[], source_event_id=None, effective=True, superseded_by=None, seq=self._working["version"] + 1, provenance="synthetic_fixture")
        e = self._emit("fixture_instruction", phase["id"], "synthetic_fixture", message=old["text"], entities={"instructions": [old]})
        correction = self._entity(text="[SYNTHETIC USER CORRECTION] Clamp each value to 0..10; summaries cannot override this correction.", kind="correction", scope="phase", phase_id=phase["id"],
                                  supersedes=[old["id"]], source_event_id=None, effective=True, superseded_by=None, seq=self._working["version"] + 1, provenance="synthetic_fixture")
        superseded = {**old, "effective": False, "superseded_by": correction["id"]}
        e = self._emit("fixture_correction", phase["id"], "synthetic_fixture", references=[old["id"]], message=correction["text"], entities={"instructions": [superseded, correction]})

    def _scenario_phase(self, p):
        phase = self._get("phases", p.get("phase_id"))
        if phase["mode"] != "synthetic_fixture" or phase["status"] in ("done", "blocked", "cancelled"):
            fail("SCENARIO_TERMINAL", "Scenario is finite and cannot advance after a terminal outcome", 409)
        return phase

    def _fixture_reviews(self, phase, stage, verdicts):
        records = []
        for reviewer, verdict in zip(REVIEWERS, verdicts):
            instructions = self._effective(self._working, phase["id"])
            refs = [i["id"] for i in instructions]
            run = self._entity(phase_id=phase["id"], role="reviewer", provider=reviewer, connector_id=reviewer, status="succeeded", provenance="synthetic_fixture",
                               model=None, effort=None, conversation_id=None, provider_run_id=None, started_at=now(), finished_at=now(), error=None,
                               request_event_id=None, response_event_id=None)
            record = self._entity(phase_id=phase["id"], reviewer=reviewer, stage=stage, revision=phase["current_revision"],
                                  instruction_version=phase["instruction_version"], status="received", verdict=verdict,
                                  findings=[] if verdict == "pass" else [self._entity(category="omission" if reviewer == "claude" else "context_drift", severity="blocking",
                                      text="[SYNTHETIC] Clamp negative values and obey latest correction.", instruction_refs=refs, file_refs=["fixture.py"], resolution_event_id=None)],
                                  request_text="[SYNTHETIC] Review current fixture revision and latest user correction.",
                                  response_text="[SYNTHETIC] " + ("Exact revised fixture passes bounded tests and context check." if verdict == "pass" else "Changes requested: clamp negatives and preserve context."),
                                  run_id=run["id"], references=refs, instruction_snapshot=instructions, provenance="synthetic_fixture", verified=False)
            request_run = {**run, "status": "queued", "finished_at": None}
            request_record = {**record, "status": "requested", "verdict": None, "findings": [], "response_text": ""}
            request = self._emit("review_requested", phase["id"], "synthetic_fixture", references=refs, message=record["request_text"],
                                 entities={"reviews": [request_record], "runs": [request_run]}, agent_id=reviewer, role="reviewer")
            run["request_event_id"] = request["id"]
            record["references"] = refs + [request["id"]]
            event = self._emit("review_received", phase["id"], "synthetic_fixture", references=refs + [request["id"]], message=record["response_text"], entities={"reviews": [record], "runs": [run]},
                               agent_id=reviewer, role="reviewer")
            records.append(record)
        return records

    def _scenario_advance(self, p):
        phase = self._scenario_phase(p)
        step = phase["scenario_step"]
        if step == 0:
            phase.update(status="reviewing", scenario_step=1)
            self._emit("phase_advanced", phase["id"], "synthetic_fixture", message="[SYNTHETIC] Draft sent to two fixture reviewer roles.", entities={"phases": [phase]})
            reviews = self._fixture_reviews(phase, "initial", ("changes_requested", "changes_requested"))
            self._emit("role_question", phase["id"], "synthetic_fixture", references=[x["id"] for x in reviews],
                       message="[SYNTHETIC] Codex asks Gemini: does clamping preserve the latest user correction?", agent_id="codex", role="primary_author")
            self._emit("role_rebuttal", phase["id"], "synthetic_fixture", references=[reviews[0]["id"]],
                       message="[SYNTHETIC] Gemini confirms the correction overrides the older summary; Claude's finding needs a test.", agent_id="antigravity", role="reviewer")
        elif step == 1:
            current = self._valid_reviews(phase, "initial")
            if any(not any(x["reviewer"] == name for x in current) for name in REVIEWERS):
                fail("GATES_UNMET", "Both fixture initial reviews must match the current instruction version; start a new explicit scenario after correction", 409)
            phase.update(status="revising", scenario_step=2)
            self._emit("phase_advanced", phase["id"], "synthetic_fixture", message="[SYNTHETIC] Both initial reviews received; primary author revises.", entities={"phases": [phase]})
            before = "def bounded_total(values):\n    return sum(values)\n"
            after = "def bounded_total(values):\n    return sum(max(0, min(10, value)) for value in values)\n"
            cases = [([], 0), ([-5, 3], 3), ([12, 4], 14), ([-1, 0, 10, 11], 20)]
            # Fixed local toy function only: no eval, imported code or shell execution.
            outputs = [(values, expected, sum(max(0, min(10, value)) for value in values)) for values, expected in cases]
            passed = all(expected == actual for _, expected, actual in outputs)
            contribution = self._entity(phase_id=phase["id"], author="[SYNTHETIC] isolated helper", workspace_path="fixture-only/isolated-helper",
                base_revision="fixture-r1", revision="fixture-r2", changed_files=["fixture.py"], diff_text="--- fixture-r1/fixture.py\n+++ fixture-r2/fixture.py\n@@\n-    return sum(values)\n+    return sum(max(0, min(10, value)) for value in values)\n",
                integrated=False, integration_event_id=None, provenance="synthetic_fixture", before_text=before, after_text=after)
            self._emit("contribution_added", phase["id"], "synthetic_fixture", message="[SYNTHETIC] Isolated helper proposes diff; no primary files touched.", entities={"contributions": [contribution]})
            contribution = copy.deepcopy(contribution)
            contribution["integrated"] = True
            phase["current_revision"] = "fixture-r2"
            resolved = [copy.deepcopy(x) for x in self._working["reviews"] if x["phase_id"] == phase["id"] and x["stage"] == "initial"]
            event = self._emit("contribution_integrated", phase["id"], "synthetic_fixture", references=[contribution["id"]],
                message="[SYNTHETIC] Primary author explicitly integrates fixture-r2 and resolves the bounded-value findings.", entities={"contributions": [contribution], "phases": [phase], "reviews": resolved}, agent_id="codex")
            integration_id = event["id"]
            change = self._entity(phase_id=phase["id"], kind="change", label="Synthetic fixture / integrated bounded-value diff",
                status="passed", revision="fixture-r2", command=None, output=contribution["diff_text"], artifact_path=None,
                provenance="synthetic_fixture", source_event_id=None, evidence_type="local_fixture_change", contribution_id=contribution["id"])
            self._emit("evidence_added", phase["id"], "synthetic_fixture", references=[contribution["id"], integration_id],
                message="[SYNTHETIC] Integrated fixture diff recorded as change evidence.", entities={"evidence": [change]})
            evidence = self._entity(phase_id=phase["id"], kind="test", label="Synthetic scenario / actual local deterministic toy tests", status="passed" if passed else "failed",
                revision="fixture-r2", command="internal fixed bounded_total checks (no shell)", output="\n".join(f"{values!r}: expected={expected}, actual={actual}, {'PASS' if expected == actual else 'FAIL'}" for values, expected, actual in outputs),
                artifact_path=None, provenance="synthetic_fixture", source_event_id=None, assertion_count=len(cases), evidence_type="local_fixture_test")
            e = self._emit("evidence_added", phase["id"], "synthetic_fixture", references=[contribution["id"]], message="[SYNTHETIC SCENARIO] Four actual deterministic local toy assertions passed.", entities={"evidence": [evidence]})
            if passed:
                self._emit("verified_completion", phase["id"], "synthetic_fixture", references=[integration_id, change["id"], evidence["id"]],
                    message="[SYNTHETIC] Integrated fixture passed four local toy tests; tested milestone only, awaiting both revalidations.",
                    completion_key=phase["id"] + ":fixture-r2:tested", evidence_refs=[change["id"], evidence["id"]],
                    validation_status="confirmed", milestone="local_fixture_tested", agent_id="codex")
        elif step == 2:
            gates = self._revision_gates(phase)
            if gates:
                fail("GATES_UNMET", "Fixture revision gate failed", 409, gates=gates)
            phase.update(status="revalidating", scenario_step=3)
            self._emit("phase_advanced", phase["id"], "synthetic_fixture", message="[SYNTHETIC] Revalidate exact integrated revision with both reviewers.", entities={"phases": [phase]})
            reviews = self._fixture_reviews(phase, "revalidation", ("pass", "pass"))
            phase.update(status="done", scenario_step=4)
            self._emit("phase_advanced", phase["id"], "synthetic_fixture", references=[x["id"] for x in reviews],
                       message="[SYNTHETIC] Both fixture revalidations pass. Phase done; real project active.", entities={"phases": [phase]})
            evidence = [x["id"] for x in self._working["evidence"] if x["phase_id"] == phase["id"] and x["status"] == "passed"]
            key = phase["id"] + ":fixture-r2:verified"
            if not any(x["payload"].get("completion_key") == key for x in self._working["events"]):
                self._emit("verified_completion", phase["id"], "synthetic_fixture", references=evidence + [x["id"] for x in reviews],
                    message="[SYNTHETIC] Verified fixture milestone; one permanent world block.", completion_key=key,
                    evidence_refs=evidence, validation_status="confirmed", milestone="phase_verified", agent_id="codex")
        else:
            fail("SCENARIO_TERMINAL", "No automatic retry or further scenario step", 409)

    def _scenario_fail(self, p):
        phase = self._scenario_phase(p)
        phase.update(status="blocked", block_reasons=["[SYNTHETIC] Failed review/test; no automatic retry."], activity="failed")
        self._emit("scenario_failed", phase["id"], "synthetic_fixture", message=phase["block_reasons"][0], entities={"phases": [phase]})

    def _scenario_cancel(self, p):
        phase = self._scenario_phase(p)
        phase.update(status="cancelled", activity="cancelled")
        self._emit("scenario_cancelled", phase["id"], "synthetic_fixture", message="[SYNTHETIC] User cancelled scenario; previous verified work preserved.", entities={"phases": [phase]})

    def _load_demo(self, p):
        if any(e["type"] == "demo_loaded" for e in self._working["events"]):
            fail("DEMO_ALREADY_LOADED", "Fixture demo already loaded", 409)
        self._scenario_start({})
        phase_id = self._working["phases"][-1]["id"]
        for _ in range(3):
            self._scenario_advance({"phase_id": phase_id})
        self._emit("demo_loaded", phase_id, "synthetic_fixture", message="[SYNTHETIC] Finite scenario loaded; no external provider executed.")

    def _begin_upload(self, p):
        name = text(p.get("name"), "name", 120)
        size = p.get("size")
        if name != Path(name).name or PureWindowsPath(name).name != name or any(c in name for c in "/\\:\x00") or ".." in name or name.endswith((" ", ".")):
            fail("INVALID_UPLOAD_NAME", "Upload requires a simple filename without traversal or paths")
        if Path(name).suffix.lower() not in (".txt", ".md", ".json", ".csv", ".py", ".js", ".css", ".html", ".yaml", ".yml", ".toml", ".log"):
            fail("DISALLOWED_UPLOAD_TYPE", "Only bounded plain-text source/document types are accepted")
        if type(size) is not int or not 0 <= size <= MAX_UPLOAD:
            fail("UPLOAD_TOO_LARGE", "Upload size must be at most 256 KiB", max_bytes=MAX_UPLOAD)
        if sum(x["status"] in ("pending", "uploading") for x in self._working["uploads"]) >= 8:
            fail("UPLOAD_LIMIT", "At most eight pending uploads", 409)
        record = self._entity(name=name, size=size, received_bytes=0, text="", status="pending", error=None, sha256=None, provenance="manual_import")
        self._emit("upload_begun", message="Upload slot reserved; no content received yet: " + name, entities={"uploads": [record]})

    def _upload(self, p):
        record = self._get("uploads", p.get("upload_id"))
        if record["status"] not in ("pending", "uploading"):
            fail("UPLOAD_TERMINAL", "Upload already has a terminal outcome", 409)
        return record

    def _send_upload(self, p):
        record = self._upload(p)
        content = p.get("text")
        try:
            if not isinstance(content, str):
                raise ValueError("Upload chunk must be text")
            data = content.encode("utf-8")
        except (ValueError, UnicodeEncodeError):
            record.update(status="failed", text="", error="Upload content must be valid UTF-8 text")
            self._emit("upload_failed", message=record["error"], entities={"uploads": [record]})
            return
        if "\x00" in content or record["received_bytes"] + len(data) > record["size"] or record["received_bytes"] + len(data) > MAX_UPLOAD:
            record.update(status="failed", text="", error="Content is binary or exceeds the declared bounded size")
            self._emit("upload_failed", message=record["error"], entities={"uploads": [record]})
            return
        record.update(status="uploading", text=record["text"] + content, received_bytes=record["received_bytes"] + len(data))
        self._emit("upload_chunk_received", message="Received bounded text chunk for " + record["name"], entities={"uploads": [record]})

    def _complete_upload(self, p):
        record = self._upload(p)
        if record["received_bytes"] != record["size"]:
            record.update(status="failed", text="", error="Received byte count does not match declared size")
            self._emit("upload_failed", message=record["error"], entities={"uploads": [record]})
            return
        record.update(status="completed", sha256=hashlib.sha256(record["text"].encode("utf-8")).hexdigest())
        artifact = self._entity(phase_id=None, kind="document", label=record["name"], status="passed", revision=None,
                               command=None, output=record["text"], artifact_path=None, provenance="manual_import", source_event_id=None,
                               upload_id=record["id"], sha256=record["sha256"])
        event = self._emit("upload_completed", references=[record["id"]], message="Text upload completed and persisted: " + record["name"], entities={"uploads": [record], "evidence": [artifact]})

    def _cancel_upload(self, p):
        record = self._upload(p)
        record.update(status="cancelled", text="", error=None)
        self._emit("upload_cancelled", message="Upload cancelled; no project file was opened or changed.", entities={"uploads": [record]})
