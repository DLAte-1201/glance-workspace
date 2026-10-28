"""Invariant tests: local persistence, finite gates, uploads and HTTP security."""
import concurrent.futures
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from server import LocalServer
from workroom import DomainError, MAX_UPLOAD, Workroom


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.room = Workroom(self.tmp.name)

    def tearDown(self):
        self.room.close()
        self.tmp.cleanup()

    def command(self, command, **payload):
        return self.room.command(command, self.room.state()["version"], payload)["state"]

    def phase(self):
        return self.command("start_phase", title="Real phase", draft_revision="r1")["phases"][-1]["id"]

    def scenario(self):
        return self.command("scenario_start")["phases"][-1]["id"]

    def assertCode(self, code, call):
        with self.assertRaises(DomainError) as caught:
            call()
        self.assertEqual(code, caught.exception.code)

    def test_append_reload_preserves_history_and_source_ids(self):
        self.assertEqual("Glance", self.room.state()["project"]["title"])
        state = self.command("add_instruction", text="User clause", kind="instruction", scope="project")
        self.assertEqual(state["events"][0]["id"], state["instructions"][0]["source_event_id"])
        self.room.close()
        self.room = Workroom(self.tmp.name)
        self.assertEqual("Glance", self.room.state()["project"]["title"])
        self.assertEqual(state, self.room.state())
        self.assertEqual(1, len(Path(self.tmp.name, "events.jsonl").read_text().splitlines()))

    def test_newest_correction_precedes_summary_and_history_survives(self):
        a = self.command("add_instruction", text="Earlier rule", kind="instruction", scope="project")["instructions"][-1]["id"]
        b = self.command("add_instruction", text="Latest correction", kind="correction", scope="project", supersedes=[a])["instructions"][-1]["id"]
        state = self.command("add_instruction", text="Old style summary", kind="summary", scope="project")
        self.assertEqual(b, state["effective_instructions"][0]["id"])
        self.assertFalse(state["instructions"][0]["effective"])
        self.assertEqual(b, state["instructions"][0]["superseded_by"])
        self.assertEqual(3, len(state["instructions"]))
        self.assertCode("INVALID_SUPERSESSION", lambda: self.command("add_instruction", text="Summary cannot replace correction", kind="summary", scope="project", supersedes=[b]))

    def test_user_instruction_precedes_newer_summary_and_synthetic_clause(self):
        self.command("add_instruction", text="Use Python", kind="instruction")
        self.command("add_instruction", text="Summary suggests JS", kind="summary")
        self.scenario()
        state = self.room.state()
        self.assertEqual("Use Python", state["effective_instructions"][0]["text"])
        self.command("add_instruction", text="Latest real user correction", kind="correction")
        self.assertEqual("Latest real user correction", self.room.state()["effective_instructions"][0]["text"])

    def test_scope_and_missing_reference_rejected(self):
        a = self.command("add_instruction", text="Project rule", kind="instruction", scope="project")["instructions"][-1]["id"]
        phase = self.phase()
        self.command("add_instruction", text="Phase only", kind="correction", scope="phase", phase_id=phase)
        self.assertCode("INVALID_SUPERSESSION", lambda: self.command("add_instruction", text="Narrow", scope="phase", phase_id=phase, supersedes=[a]))
        self.assertCode("NOT_FOUND", lambda: self.command("add_instruction", text="Missing", supersedes=["nonexistent"]))

    def test_version_check_serializes_concurrent_writers(self):
        version = self.room.state()["version"]
        def write(n):
            try:
                self.room.command("add_instruction", version, {"text": "Rule " + str(n)})
                return "ok"
            except DomainError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(write, range(8)))
        self.assertEqual(1, results.count("ok"))
        self.assertEqual(7, results.count("VERSION_CONFLICT"))
        self.assertEqual(1, self.room.state()["version"])

    def test_second_process_owner_is_rejected(self):
        self.assertCode("STORAGE_LOCKED", lambda: Workroom(self.tmp.name))

    def test_corruption_is_visible_and_never_truncated(self):
        self.command("add_instruction", text="Keep me")
        self.room.close()
        path = Path(self.tmp.name, "events.jsonl")
        with path.open("ab") as stream:
            stream.write(b'{"batch":[')
        before = path.read_bytes()
        self.room = Workroom(self.tmp.name)
        self.assertFalse(self.room.state()["storage_writable"])
        self.assertEqual(1, len(self.room.state()["instructions"]))
        self.assertCode("STORAGE_ERROR", lambda: self.command("add_instruction", text="Blocked"))
        self.assertEqual(before, path.read_bytes())

    def test_malformed_storage_payload_or_entity_is_visible(self):
        state = self.command("add_instruction", text="Valid prefix")
        self.room.close()
        path = Path(self.tmp.name, "events.jsonl")
        valid = path.read_bytes()
        for bad in (None, {"entities": []}, {"entities": {"instructions": [{"id": "bad"}]}}):
            event = dict(state["events"][0])
            event.update(id="0bd8f57a-6a41-4aa7-9a6a-481f42f47fbc", seq=state["version"] + 1, payload=bad)
            path.write_bytes(valid + (json.dumps({"batch": [event]}) + "\n").encode())
            self.room = Workroom(self.tmp.name)
            self.assertFalse(self.room.state()["storage_writable"])
            self.assertEqual(1, len(self.room.state()["instructions"]))
            self.assertCode("STORAGE_ERROR", lambda: self.command("load_demo"))
            self.room.close()
        path.write_bytes(valid)
        self.room = Workroom(self.tmp.name)

    def test_malformed_discovery_metadata_does_not_break_state(self):
        path = Path(self.tmp.name, "connections.json")
        for metadata in ([], {"providers": []}, {"providers": {"claude": []}}):
            path.write_text(json.dumps(metadata))
            self.room.connections_path = path
            state = self.room.state()
            self.assertEqual(3, len(state["connectors"]))
            self.assertTrue(all(x["authorized"] is False for x in state["connectors"]))

    def test_missing_phase_id_or_actor_name_disables_corrupted_storage(self):
        state = self.command("add_instruction", text="Valid prefix")
        self.room.close()
        path = Path(self.tmp.name, "events.jsonl")
        valid = path.read_bytes()
        for missing in ("phase_id", "actor_name"):
            event = json.loads(json.dumps(state["events"][0]))
            event.update(id="0bd8f57a-6a41-4aa7-9a6a-481f42f47fbc", seq=state["version"] + 1)
            if missing == "phase_id":
                del event["phase_id"]
            else:
                del event["actor"]["name"]
            path.write_bytes(valid + (json.dumps({"batch": [event]}) + "\n").encode())
            self.room = Workroom(self.tmp.name)
            self.assertFalse(self.room.state()["storage_writable"])
            self.assertCode("STORAGE_ERROR", lambda: self.command("load_demo"))
            self.room.close()
        path.write_bytes(valid)
        self.room = Workroom(self.tmp.name)

    def test_bad_entity_scalar_or_completion_key_disables_corrupted_storage(self):
        state = self.command("add_instruction", text="Valid prefix")
        self.room.close()
        path = Path(self.tmp.name, "events.jsonl")
        valid = path.read_bytes()
        for field in ("kind", "seq", "completion_key"):
            event = json.loads(json.dumps(state["events"][0]))
            event.update(id="0bd8f57a-6a41-4aa7-9a6a-481f42f47fbc", seq=state["version"] + 1)
            if field == "completion_key":
                event["payload"][field] = []
            else:
                event["payload"]["entities"]["instructions"][0][field] = []
            path.write_bytes(valid + (json.dumps({"batch": [event]}) + "\n").encode())
            self.room = Workroom(self.tmp.name)
            self.assertFalse(self.room.state()["storage_writable"])
            self.assertCode("STORAGE_ERROR", lambda: self.command("load_demo"))
            self.room.close()
        path.write_bytes(valid)
        self.room = Workroom(self.tmp.name)

    def test_manual_import_is_not_verified_gate_or_provider_receipt(self):
        phase = self.phase()
        state = self.command("request_reviews", phase_id=phase, stage="initial")
        self.assertEqual({"claude", "antigravity"}, {x["reviewer"] for x in state["reviews"]})
        self.assertTrue(all(x["status"] == "disabled" for x in state["runs"]))
        for review in state["reviews"]:
            self.command("import_review", review_id=review["id"], response_text="Claimed pass", verdict="pass", model="untrusted name", conversation_id="untrusted id")
        self.assertCode("GATES_UNMET", lambda: self.command("advance_phase", phase_id=phase, target="revising"))
        self.assertTrue(all(not r["verified"] and r["provenance"] == "manual_import" for r in self.room.state()["reviews"]))

    def test_correction_stales_pending_reviews_and_requires_explicit_new_requests(self):
        phase = self.phase()
        self.command("request_reviews", phase_id=phase, stage="initial")
        state = self.command("add_instruction", text="Updated constraint", kind="correction")
        self.assertTrue(all(r["status"] == "stale" for r in state["reviews"]))
        self.command("request_reviews", phase_id=phase, stage="initial")
        self.assertEqual(4, len(self.room.state()["reviews"]))
        self.assertEqual(2, sum(r["status"] == "requested" for r in self.room.state()["reviews"]))

    def test_fixture_finite_sequence_actual_assertions_and_deduplication(self):
        phase = self.scenario()
        self.command("scenario_advance", phase_id=phase)
        self.command("scenario_advance", phase_id=phase)
        state = self.command("scenario_advance", phase_id=phase)
        self.assertEqual("done", state["phases"][-1]["status"])
        self.assertEqual("active", state["project"]["status"])
        self.assertEqual(2, state["progress"]["verified_count"])
        evidence = state["evidence"][-1]
        self.assertEqual(4, evidence["assertion_count"])
        self.assertIn("[-5, 3]: expected=3, actual=3, PASS", evidence["output"])
        self.assertTrue(all(e["provenance"] == "synthetic_fixture" for e in state["events"]))
        self.assertTrue(all("[SYNTHETIC" in message["text"] for message in state["messages"]))
        self.assertTrue(all(f["resolution_event_id"] for r in state["reviews"] for f in r["findings"]))
        event_ids = {e["id"] for e in state["events"]}
        self.assertTrue(all(c["integrated"] and c["integration_event_id"] in event_ids for c in state["contributions"]))
        self.assertTrue(all(e["source_event_id"] in event_ids for e in state["evidence"]))
        self.assertTrue(all(r["request_event_id"] in event_ids and r["response_event_id"] in event_ids for r in state["runs"]))
        before = state["version"]
        self.assertCode("SCENARIO_TERMINAL", lambda: self.command("scenario_advance", phase_id=phase))
        self.assertEqual(before, self.room.state()["version"])
        self.assertCode("GATES_UNMET", lambda: self.command("complete_project", user_confirmation=True))

    def test_failed_or_cancelled_scenario_preserves_prior_progress(self):
        self.command("load_demo")
        for action in ("scenario_fail", "scenario_cancel"):
            phase = self.scenario()
            state = self.command(action, phase_id=phase)
            self.assertEqual(2, state["progress"]["verified_count"])
            self.assertCode("SCENARIO_TERMINAL", lambda: self.command("scenario_advance", phase_id=phase))

    def test_demo_load_idempotency_conflict_and_one_atomic_batch(self):
        self.command("load_demo")
        self.assertCode("DEMO_ALREADY_LOADED", lambda: self.command("load_demo"))
        records = Path(self.tmp.name, "events.jsonl").read_text().splitlines()
        self.assertEqual(1, len(records))
        self.assertGreater(len(json.loads(records[0])["batch"]), 10)

    def test_history_snapshots_cannot_change_when_later_phase_records_change(self):
        state = self.command("load_demo")
        phases = [(e["seq"], p["status"], p["current_revision"]) for e in state["events"]
                  for p in e["payload"].get("entities", {}).get("phases", [])]
        self.assertEqual(["draft", "reviewing", "revising", "revising", "revalidating", "done"], [p[1] for p in phases])
        self.assertEqual("fixture-r1", phases[2][2])
        self.assertEqual("fixture-r2", phases[3][2])
        revalidating_seq = next(seq for seq, status, _ in phases if status == "revalidating")
        done_seq = next(seq for seq, status, _ in phases if status == "done")
        receipts = [e["seq"] for e in state["events"] if e["type"] == "review_received" and
                    e["payload"]["entities"]["reviews"][0]["stage"] == "revalidation"]
        self.assertTrue(all(revalidating_seq < seq < done_seq for seq in receipts))
        before = json.dumps(state["events"], sort_keys=True)
        self.command("add_instruction", text="Later correction", kind="correction")
        self.assertEqual(before, json.dumps(self.room.state()["events"][:len(state["events"])], sort_keys=True))
        self.room.close()
        self.room = Workroom(self.tmp.name)
        self.assertEqual(before, json.dumps(self.room.state()["events"][:len(state["events"])], sort_keys=True))

    def test_revalidation_failure_blocks_without_awarding_new_progress(self):
        self.command("load_demo")
        # Arrange a received fixture revalidation round, then exercise the public
        # gate with a nonpass response. This setup does not invent external trust.
        phase = self.room._state["phases"][-1]
        phase["status"] = "revalidating"
        reviews = [r for r in self.room._state["reviews"] if r["stage"] == "revalidation"]
        reviews[0]["verdict"] = "pass_with_notes"
        state = self.command("advance_phase", phase_id=phase["id"], target="done")
        self.assertEqual("blocked", state["phases"][-1]["status"])
        self.assertEqual(2, state["progress"]["verified_count"])
        self.assertCode("INVALID_TRANSITION", lambda: self.command("advance_phase", phase_id=phase["id"], target="done"))

    def test_old_revision_and_missing_second_review_cannot_complete(self):
        self.command("load_demo")
        phase = self.room._state["phases"][-1]
        phase["status"] = "revalidating"
        reviews = [r for r in self.room._state["reviews"] if r["stage"] == "revalidation"]
        reviews[1]["revision"] = "fixture-r1"
        self.assertCode("GATES_UNMET", lambda: self.command("advance_phase", phase_id=phase["id"], target="done"))
        reviews[1]["revision"] = "fixture-r2"
        reviews[1]["status"] = "requested"
        self.assertCode("GATES_UNMET", lambda: self.command("advance_phase", phase_id=phase["id"], target="done"))

    def test_new_failed_test_evidence_overrides_older_pass(self):
        phase = self.scenario()
        self.command("scenario_advance", phase_id=phase)
        self.command("scenario_advance", phase_id=phase)
        self.command("add_evidence", phase_id=phase, kind="test", label="New failing observation", status="failed", revision="fixture-r2", output="expected 3 but got 2")
        self.assertCode("GATES_UNMET", lambda: self.command("scenario_advance", phase_id=phase))
        self.assertEqual(1, self.room.state()["progress"]["verified_count"])

    def test_tested_milestone_is_evidenced_and_survives_failure_or_cancel(self):
        for action in ("scenario_fail", "scenario_cancel"):
            phase = self.scenario()
            self.command("scenario_advance", phase_id=phase)
            self.assertFalse(any(e["phase_id"] == phase for e in self.room.state()["verified_completions"]))
            state = self.command("scenario_advance", phase_id=phase)
            events = [e for e in state["verified_completions"] if e["phase_id"] == phase]
            self.assertEqual(1, len(events))
            tested = events[0]
            self.assertTrue(tested["payload"]["completion_key"].endswith(":tested"))
            evidence = [e for e in state["evidence"] if e["id"] in tested["payload"]["evidence_refs"]]
            self.assertEqual({"change", "test"}, {e["kind"] for e in evidence})
            self.assertTrue(all(e["status"] == "passed" for e in evidence))
            self.assertEqual("revising", state["phases"][-1]["status"])
            self.assertEqual("active", state["project"]["status"])
            before = state["progress"]["completion_keys"]
            state = self.command(action, phase_id=phase)
            self.assertEqual(before, state["progress"]["completion_keys"])
            self.assertCode("SCENARIO_TERMINAL", lambda: self.command("scenario_advance", phase_id=phase))
            self.assertFalse(any(e["phase_id"] == phase and e["payload"]["completion_key"].endswith(":verified") for e in state["verified_completions"]))

    def test_user_message_refs_persist_without_provider_run(self):
        phase = self.scenario()
        ref = self.room.state()["messages"][0]["id"]
        state = self.command("send_message", text="Question for reviewer", task_id=phase, agent_id="claude", references=[ref])
        self.assertEqual("Question for reviewer", state["messages"][-1]["text"])
        self.assertEqual([ref], state["messages"][-1]["references"])
        self.assertEqual("user", state["messages"][-1]["role"])
        self.assertEqual([], state["runs"])
        self.assertCode("INVALID_REFERENCE", lambda: self.command("send_message", text="Bad ref", references=["absent"]))
        self.assertCode("INVALID_AGENT", lambda: self.command("send_message", text="Bad target", agent_id="absent"))

    def test_stale_fixture_review_cannot_advance(self):
        phase = self.scenario()
        self.command("scenario_advance", phase_id=phase)
        self.command("add_instruction", text="A correction during review", kind="correction")
        self.assertCode("GATES_UNMET", lambda: self.command("scenario_advance", phase_id=phase))
        self.assertEqual(0, self.room.state()["progress"]["verified_count"])

    def test_integration_requires_confirmation_and_correct_state(self):
        phase = self.phase()
        state = self.command("add_contribution", phase_id=phase, workspace_path="isolated/helper", base_revision="r1", revision="r2", changed_files=["example.py"], diff_text="- old\n+ new")
        contribution = state["contributions"][-1]["id"]
        self.assertCode("CONFIRMATION_REQUIRED", lambda: self.command("integrate_contribution", phase_id=phase, contribution_id=contribution))
        self.assertCode("INVALID_INTEGRATION", lambda: self.command("integrate_contribution", phase_id=phase, contribution_id=contribution, primary_author_confirmation=True))
        self.assertEqual("r1", self.room.state()["phases"][-1]["current_revision"])

    def test_provider_invocation_has_zero_side_effect_and_no_progress(self):
        before = self.room.state()
        self.assertCode("EXTERNAL_EXECUTION_NOT_AUTHORIZED", lambda: self.command("invoke_provider", connector_id="antigravity", phase_id="anything"))
        self.assertEqual(before, self.room.state())
        self.assertFalse(Path(self.tmp.name, "events.jsonl").exists())

    def test_unknown_or_invalid_command_has_no_append(self):
        self.assertCode("UNKNOWN_COMMAND", lambda: self.command("shell", command_text="echo nope"))
        self.assertCode("INVALID_PAYLOAD", lambda: self.room.command("start_phase", 0, []))
        self.assertEqual(0, self.room.state()["version"])

    def test_upload_success_utf8_size_hash_persistence(self):
        content = "한글\ntext"
        upload = self.command("begin_upload", name="notes.txt", size=len(content.encode()))["uploads"][-1]["id"]
        self.command("send_upload", upload_id=upload, text=content)
        state = self.command("complete_upload", upload_id=upload)
        self.assertEqual("completed", state["uploads"][-1]["status"])
        self.assertEqual(64, len(state["uploads"][-1]["sha256"]))
        self.assertEqual(content, state["artifacts"][-1]["output"])
        self.assertEqual(upload, state["artifacts"][-1]["upload_id"])
        self.room.close()
        self.room = Workroom(self.tmp.name)
        self.assertEqual(content, self.room.state()["uploads"][-1]["text"])
        self.assertCode("UPLOAD_TERMINAL", lambda: self.command("complete_upload", upload_id=upload))

    def test_upload_cancel_and_failure_persist_truthfully(self):
        upload = self.command("begin_upload", name="cancel.md", size=4)["uploads"][-1]["id"]
        self.command("send_upload", upload_id=upload, text="ab")
        self.assertEqual("cancelled", self.command("cancel_upload", upload_id=upload)["uploads"][-1]["status"])
        upload = self.command("begin_upload", name="short.txt", size=4)["uploads"][-1]["id"]
        self.command("send_upload", upload_id=upload, text="ab")
        self.assertEqual("failed", self.command("complete_upload", upload_id=upload)["uploads"][-1]["status"])
        upload = self.command("begin_upload", name="overflow.txt", size=1)["uploads"][-1]["id"]
        self.assertEqual("failed", self.command("send_upload", upload_id=upload, text="overflow")["uploads"][-1]["status"])

    def test_invalid_upload_type_or_unicode_persists_failed_outcome(self):
        for content in ([], "\ud800"):
            upload = self.command("begin_upload", name="bad.txt", size=1)["uploads"][-1]["id"]
            state = self.command("send_upload", upload_id=upload, text=content)
            self.assertEqual("failed", state["uploads"][-1]["status"])
            self.assertEqual("upload_failed", state["events"][-1]["type"])
            self.assertEqual("", state["uploads"][-1]["text"])
            self.assertCode("UPLOAD_TERMINAL", lambda: self.command("complete_upload", upload_id=upload))
        self.room.close()
        self.room = Workroom(self.tmp.name)
        self.assertTrue(all(u["status"] == "failed" for u in self.room.state()["uploads"]))

    def test_upload_rejects_traversal_executable_and_size(self):
        for name in ("../escape.txt", "..\\escape.txt", "C:\\escape.txt", "folder/name.txt", "a:b.txt"):
            self.assertCode("INVALID_UPLOAD_NAME", lambda n=name: self.command("begin_upload", name=n, size=0))
        self.assertCode("DISALLOWED_UPLOAD_TYPE", lambda: self.command("begin_upload", name="run.exe", size=0))
        self.assertCode("UPLOAD_TOO_LARGE", lambda: self.command("begin_upload", name="huge.txt", size=MAX_UPLOAD+1))
        self.assertEqual(0, self.room.state()["version"])

    def test_upload_complete_cancel_race_has_one_terminal_outcome(self):
        upload = self.command("begin_upload", name="race.txt", size=0)["uploads"][-1]["id"]
        version = self.room.state()["version"]
        def terminate(command):
            try:
                self.room.command(command, version, {"upload_id": upload})
                return "ok"
            except DomainError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(terminate, ["complete_upload", "cancel_upload"]))
        self.assertEqual(["VERSION_CONFLICT", "ok"], sorted(results))
        state = self.room.state()
        terminal = [e for e in state["events"] if e["type"] in ("upload_completed", "upload_cancelled")]
        self.assertEqual(1, len(terminal))
        self.assertIn(state["uploads"][-1]["status"], ("completed", "cancelled"))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.room = Workroom(self.tmp.name)
        self.server = LocalServer(("127.0.0.1", 0), self.room)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.room.close()
        self.tmp.cleanup()

    def request(self, method="GET", path="/api/state", body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        raw = response.read()
        result = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
        status = response.status
        conn.close()
        return status, result

    def headers(self):
        return {"Content-Type": "application/json", "X-Workroom-Token": self.server.session_token,
                "Origin": "http://127.0.0.1:" + str(self.port)}

    def test_get_state_and_authenticated_write(self):
        status, state = self.request()
        self.assertEqual(200, status)
        self.assertEqual(self.server.session_token, state["session_token"])
        body = json.dumps({"command": "add_instruction", "expected_version": 0, "payload": {"text": "Rule"}})
        status, result = self.request("POST", "/api/commands", body, self.headers())
        self.assertEqual(200, status)
        self.assertEqual(1, result["version"])
        self.assertEqual(409, self.request("POST", "/api/commands", body, self.headers())[0])

    def test_no_token_foreign_origin_host_rejected(self):
        body = '{"command":"load_demo","expected_version":0,"payload":{}}'
        self.assertEqual(403, self.request("POST", "/api/commands", body, {"Content-Type": "application/json"})[0])
        headers = self.headers()
        headers["Origin"] = "https://evil.example"
        self.assertEqual(403, self.request("POST", "/api/commands", body, headers)[0])
        self.assertEqual(403, self.request(headers={"Host": "evil.example:" + str(self.port)})[0])
        self.assertEqual(0, self.room.state()["version"])

    def test_malformed_duplicate_json_and_unknown_top_fields(self):
        for body in ('{', '{"command":"load_demo","command":"scenario_start","expected_version":0,"payload":{}}',
                     '{"command":"load_demo","expected_version":0,"payload":{},"provenance":"external_execution"}',
                     '{"command":"load_demo","expected_version":NaN,"payload":{}}'):
            self.assertEqual(400, self.request("POST", "/api/commands", body, self.headers())[0])
        self.assertEqual(0, self.room.state()["version"])

    def test_excessive_json_nesting_is_structured_error(self):
        body = '{"command":"load_demo","expected_version":0,"payload":{"junk":' + '[' * 5000 + '0' + ']' * 5000 + '}}'
        status, result = self.request("POST", "/api/commands", body, self.headers())
        self.assertEqual(400, status)
        self.assertEqual("INVALID_JSON", result["error"]["code"])
        self.assertEqual(0, self.room.state()["version"])

    def test_static_allowlist_rejects_traversal_and_private_sources(self):
        for path in ("/../CONNECTIONS.json", "/%2e%2e/server.py", "/server.py", "/.state/events.jsonl", "/C:/Windows/win.ini"):
            self.assertEqual(404, self.request(path=path)[0])
        self.assertEqual(403, self.request("OPTIONS")[0])

    def test_actual_ui_assets_are_served_and_port_cannot_be_shared(self):
        for path, marker in (("/", b"<!DOCTYPE html>"), ("/app.js", b"api/state"), ("/style.css", b"{")):
            status, content = self.request(path=path)
            self.assertEqual(200, status, path)
            self.assertIn(marker.lower(), content.lower(), path)
        with self.assertRaises(OSError):
            LocalServer(("127.0.0.1", self.port), self.room)

    def test_http_transport_prepare_and_actual_async_fixture(self):
        def send(command, payload):
            version = self.request()[1]["version"]
            return self.request("POST", "/api/commands", json.dumps({"command": command, "expected_version": version, "payload": payload}), self.headers())
        status, result = send("prepare_run", {"provider": "codex", "phase_id": None, "prompt": "Review this draft only."})
        self.assertEqual(200, status)
        self.assertEqual("awaiting_approval", result["state"]["runs"][-1]["status"])
        status, result = send("start_fixture_run", {"scenario": "success", "phase_id": None})
        self.assertEqual(200, status)
        self.assertEqual("queued", result["state"]["runs"][-1]["status"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.request()[1]
            run = next(r for r in state["runs"] if r["id"] == result["run_id"])
            if run["status"] not in ("queued", "running"):
                break
            time.sleep(.02)
        self.assertEqual("succeeded", run["status"])
        self.assertEqual(0, state["progress"]["verified_count"])
        status, error = send("invoke_provider", {"approved": True})
        self.assertEqual(403, status)
        self.assertEqual("EXTERNAL_EXECUTION_NOT_AUTHORIZED", error["error"]["code"])

    def test_content_type_body_limit_and_nonloopback_bind(self):
        headers = self.headers()
        headers["Content-Type"] = "text/plain"
        self.assertEqual(400, self.request("POST", "/api/commands", "{}", headers)[0])
        headers = self.headers()
        headers["Content-Length"] = str(1024 * 1024 + 1)
        self.assertEqual(400, self.request("POST", "/api/commands", "", headers)[0])
        with self.assertRaises(ValueError):
            LocalServer(("0.0.0.0", 0), self.room)


class TransportWorkroomTests(unittest.TestCase):
    """Actual owned local subprocess integration; never an inference provider."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Path(self.tmp.name, "sources.json")
        self.registry.write_text("{}")
        self.room = Workroom(self.tmp.name, history_registry_path=self.registry)

    def tearDown(self):
        self.room.close()
        self.tmp.cleanup()

    def command(self, name, **payload):
        return self.room.command(name, self.room.state()["version"], payload)

    def wait_run(self, run_id, timeout=9):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = next(r for r in self.room.state()["runs"] if r["id"] == run_id)
            if run["status"] not in ("queued", "running", "cancel_requested"):
                return run
            time.sleep(.02)
        self.fail("Fixture worker did not finish within its bounded timeout")

    def test_prepare_all_providers_inert_immutable_correlated_plans(self):
        with patch("subprocess.Popen") as spawn:
            for provider in ("codex", "claude", "antigravity"):
                result = self.command("prepare_run", provider=provider, prompt="Read this isolated draft only.", model=None, effort=None, conversation_id=None)
                run = result["state"]["runs"][-1]
                plan = result["state"]["execution_plans"][-1]
                self.assertEqual("awaiting_approval", run["status"])
                self.assertEqual("live_pending", run["mode"])
                self.assertEqual(run["plan_id"], plan["id"])
                self.assertEqual(plan["id"], plan["request"]["plan_id"])
                self.assertEqual(plan["id"], plan["snapshot"]["id"])
                self.assertEqual("not_granted", run["authorization"]["state"])
                self.assertFalse(plan["snapshot"]["shell"])
            spawn.assert_not_called()
        before = self.room.state()["execution_plans"]
        self.command("add_instruction", text="New user correction", kind="correction")
        self.assertEqual(before, self.room.state()["execution_plans"])
        self.assertFalse(Path(self.tmp.name, "transport-journal").exists())
        self.assertFalse(Path(self.tmp.name, "runs").exists())

    def test_real_fixture_async_success_persists_without_phase_progress(self):
        start = time.monotonic()
        result = self.command("start_fixture_run", scenario="success")
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual("queued", result["state"]["runs"][-1]["status"])
        run_id = result["run_id"]
        queued_snapshot = result["state"]["events"][-1]
        run = self.wait_run(run_id)
        self.assertEqual("succeeded", run["status"])
        self.assertTrue(run["receipt"]["managed_fixture_execution"])
        self.assertIsInstance(run["receipt"]["pid"], int)
        self.assertFalse(run["eligible_current_phase"])
        self.assertEqual("synthetic_fixture", run["usage"]["source"])
        self.assertEqual(0, self.room.state()["progress"]["verified_count"])
        self.assertEqual([], self.room.state()["reviews"])
        self.assertEqual(queued_snapshot, next(e for e in self.room.state()["events"] if e["id"] == queued_snapshot["id"]))
        self.assertTrue(Path(self.tmp.name, run["receipt"]["raw_stdout_artifact"]).is_file())
        self.room.close()
        self.room = Workroom(self.tmp.name, history_registry_path=self.registry)
        self.assertEqual("succeeded", self.room.state()["runs"][-1]["status"])
        self.assertEqual(run["intent_id"], self.room.state()["runs"][-1]["intent_id"])

    def test_worker_events_preserve_intervening_user_write_and_version_guard(self):
        result = self.command("start_fixture_run", scenario="wait_for_cancel")
        run_id = result["run_id"]
        deadline = time.monotonic() + 2
        while self.room.state()["version"] == result["version"] and time.monotonic() < deadline:
            time.sleep(.01)
        with self.assertRaises(DomainError) as caught:
            self.room.command("add_instruction", result["version"], {"text": "Stale write"})
        self.assertEqual("VERSION_CONFLICT", caught.exception.code)
        self.command("add_instruction", text="Intervening user rule")
        self.command("cancel_run", run_id=run_id)
        self.wait_run(run_id)
        self.assertEqual("Intervening user rule", self.room.state()["instructions"][-1]["text"])

    def test_cancel_actual_child_pending_then_terminal_and_idempotent(self):
        result = self.command("start_fixture_run", scenario="wait_for_cancel")
        run_id = result["run_id"]
        journal = Path(self.tmp.name, "transport-journal", run_id + ".jsonl")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if journal.exists() and "process_started" in journal.read_text():
                break
            time.sleep(.01)
        self.assertIn("process_started", journal.read_text())
        result = self.command("cancel_run", run_id=run_id)
        self.assertEqual("cancel_requested", result["state"]["runs"][-1]["status"])
        run = self.wait_run(run_id)
        self.assertEqual("cancelled", run["status"])
        self.assertEqual("CANCELLED", run["failure"]["code"])
        self.assertTrue(run["failure"]["dispatch_uncertain"])
        version = self.room.state()["version"]
        result = self.command("cancel_run", run_id=run_id)
        self.assertTrue(result["unchanged"])
        self.assertEqual(version, result["version"])
        self.assertEqual([], result["event_ids"])

    def test_nonzero_malformed_mismatch_and_overflow_never_progress(self):
        for scenario, failure in (("nonzero_exit", "NONZERO_EXIT"), ("malformed_output", "INVALID_OUTPUT"),
                                  ("conversation_mismatch", "CORRELATION_MISMATCH"), ("output_overflow", "OUTPUT_LIMIT")):
            result = self.command("start_fixture_run", scenario=scenario)
            run = self.wait_run(result["run_id"])
            self.assertNotEqual("succeeded", run["status"], scenario)
            self.assertEqual(failure, run["failure"]["code"], scenario)
        self.assertEqual(0, self.room.state()["progress"]["verified_count"])

    def test_actual_timeout_has_bounded_terminal_receipt(self):
        start = time.monotonic()
        result = self.command("start_fixture_run", scenario="timeout")
        run = self.wait_run(result["run_id"])
        self.assertEqual("timed_out", run["status"])
        self.assertEqual("TIMEOUT", run["failure"]["code"])
        self.assertTrue(run["failure"]["dispatch_uncertain"])
        self.assertLess(time.monotonic() - start, 8)
        self.assertEqual(0, self.room.state()["progress"]["verified_count"])

    def test_restart_unresolved_queue_never_spawns_or_resends(self):
        with patch.object(self.room, "_launch_fixture"):
            result = self.command("start_fixture_run", scenario="success")
        self.room.close()
        with patch("subprocess.Popen") as spawn:
            self.room = Workroom(self.tmp.name, history_registry_path=self.registry)
            spawn.assert_not_called()
        run = next(r for r in self.room.state()["runs"] if r["id"] == result["run_id"])
        self.assertEqual("disconnected", run["status"])
        self.assertEqual("INTERRUPTED", run["failure"]["code"])
        self.assertEqual(0, self.room.state()["progress"]["verified_count"])

    def test_worker_thread_start_failure_is_terminal_and_close_safe(self):
        with patch("threading.Thread.start", side_effect=RuntimeError("local thread unavailable")), patch("subprocess.Popen") as spawn:
            result = self.command("start_fixture_run", scenario="success")
            spawn.assert_not_called()
        run = self.wait_run(result["run_id"])
        self.assertEqual("disconnected", run["status"])
        self.assertIn("could not start", run["error"])
        self.room.close()
        self.assertTrue(self.room._lock_file.closed)

    def test_shutdown_serializes_with_admitted_preparing_command(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        original = self.room._transport_bundle
        def pause(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(*args, **kwargs)
        failures = []
        def prepare():
            try:
                self.command("start_fixture_run", scenario="wait_for_cancel")
            except Exception as exc:
                failures.append(exc)
        def shutdown():
            self.room.close()
            closed.set()
        with patch.object(self.room, "_transport_bundle", side_effect=pause):
            command_thread = threading.Thread(target=prepare)
            command_thread.start()
            self.assertTrue(entered.wait(2))
            shutdown_thread = threading.Thread(target=shutdown)
            shutdown_thread.start()
            self.assertFalse(closed.wait(.05))
            self.assertFalse(self.room._lock_file.closed)
            release.set()
            command_thread.join(4)
            shutdown_thread.join(8)
        self.assertEqual([], failures)
        self.assertTrue(closed.is_set())
        self.assertTrue(self.room._lock_file.closed)
        self.assertFalse(any(t.is_alive() for t in self.room._workers.values()))
        version = self.room.state()["version"]
        time.sleep(.05)
        self.assertEqual(version, self.room.state()["version"])
        with self.assertRaises(DomainError):
            self.command("start_fixture_run", scenario="success")
        self.room = Workroom(self.tmp.name, history_registry_path=self.registry)
        self.assertTrue(self.room.state()["storage_writable"])

    def test_restart_recovers_finished_journal_receipt_without_resend(self):
        result = self.command("start_fixture_run", scenario="success")
        finished = self.wait_run(result["run_id"])
        self.room.close()
        path = Path(self.tmp.name, "events.jsonl")
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual("fixture_run_finished", records[-1]["batch"][-1]["type"])
        path.write_text("\n".join(json.dumps(record) for record in records[:-1]) + "\n", encoding="utf-8")
        with patch("subprocess.Popen") as spawn:
            self.room = Workroom(self.tmp.name, history_registry_path=self.registry)
            spawn.assert_not_called()
        recovered = self.room.state()["runs"][-1]
        self.assertEqual("succeeded", recovered["status"])
        self.assertEqual(finished["intent_id"], recovered["intent_id"])
        self.assertEqual(finished["response"], recovered["response"])

    def history_source(self):
        import hashlib
        source = Path(self.tmp.name, "reviewed-test-archive.ndjson")
        source.write_text(json.dumps({"event": "init", "conversation_id": "past-1", "init": {"model": "reported-model"}}) + "\n" +
            json.dumps({"event": "result", "result": {"conversation_id": "past-1", "status": "SUCCESS", "response": "Fabricated archive for integration test only."}}) + "\n")
        record = {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "provider": "antigravity", "conversation_id": "past-1",
                  "metadata": {"started_at": "2026-01-01T00:00:00Z", "exit_code": 0},
                  "source_evidence": {"kind": "fabricated_test_fixture", "scope": "Privileged registry integration test only", "authenticity": "test_fixture_only"}}
        self.registry.write_text(json.dumps({"test-past": record}))
        return source

    def test_registered_history_preserves_source_and_never_grants_gate(self):
        self.history_source()
        with patch("subprocess.Popen") as spawn:
            result = self.command("import_history", source_id="test-past")
            spawn.assert_not_called()
        run = result["state"]["runs"][-1]
        self.assertEqual("historical_external_execution", run["provenance"])
        self.assertEqual("past-1", run["conversation_id"])
        self.assertEqual("2026-01-01T00:00:00Z", run["started_at"])
        self.assertFalse(run["eligible_current_phase"])
        self.assertEqual(0, run["receipt"]["provider_calls_added"])
        self.assertEqual(0, result["state"]["progress"]["verified_count"])
        self.assertEqual([], result["state"]["reviews"])
        self.assertTrue(result["state"]["history_sources"][0]["already_imported"])
        with self.assertRaises(DomainError) as caught:
            self.command("import_history", source_id="test-past")
        self.assertEqual("HISTORY_ALREADY_IMPORTED", caught.exception.code)

    def test_history_pin_mismatch_or_path_override_rejected_without_append(self):
        source = self.history_source()
        source.write_text("changed")
        with self.assertRaises(DomainError) as caught:
            self.command("import_history", source_id="test-past")
        self.assertEqual("HISTORY_IMPORT_FAILED", caught.exception.code)
        with self.assertRaises(DomainError):
            self.command("import_history", source_id="test-past", path=str(source))
        self.assertEqual(0, self.room.state()["version"])

    def test_client_cannot_supply_fixture_program_or_forge_worker(self):
        with patch("subprocess.Popen") as spawn:
            for payload in ({"scenario": "success", "argv": ["anything"]}, {"scenario": "success", "cwd": "C:\\"}, {"scenario": "success", "limits": {"timeout_seconds": 999}}):
                with self.assertRaises(DomainError):
                    self.room.command("start_fixture_run", self.room.state()["version"], payload)
            with self.assertRaises(DomainError):
                self.command("fixture_run_finished", run_id="fake", provenance="external_execution")
            with self.assertRaises(DomainError) as caught:
                self.command("invoke_provider", approved=True)
            self.assertEqual("EXTERNAL_EXECUTION_NOT_AUTHORIZED", caught.exception.code)
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
