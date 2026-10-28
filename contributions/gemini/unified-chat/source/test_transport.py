"""Actual local subprocess tests; never invoke an inference provider."""
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import transport
from providers import ExecutionApprovalRequired


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.counter = 0

    def prepare(self, **kwargs):
        self.counter += 1
        return transport.prepare_transport_request("antigravity", run_id="run-" + str(self.counter),
            prompt="Synthetic input; never send to a provider", workspace_root=self.root, **kwargs)

    def test_prepare_is_inert_and_plan_matches_input(self):
        plan = self.prepare()
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(plan["request"]["plan_id"], plan["plan"]["id"])
        self.assertFalse(plan["plan"]["shell"])
        self.assertTrue(Path(plan["plan"]["argv"][0]).is_absolute())
        self.assertEqual(plan["plan"]["input_bytes"], len(plan["plan"]["stdin"].encode("utf-8")))

    def test_actual_success_for_all_three_protocols(self):
        for provider in ("antigravity", "claude", "codex"):
            with self.subTest(provider=provider):
                prepared = transport.prepare_transport_request(provider, run_id="actual-" + provider,
                    prompt="Local fixture", workspace_root=self.root)
                receipt = transport.execute_fixture(prepared)
                self.assertEqual(receipt["status"], "succeeded")
                self.assertEqual(receipt["conversation_id"], "fixture-actual-" + provider)
                self.assertIn("Synthetic local fixture", receipt["response"])
                self.assertEqual(receipt["usage"]["total_tokens"], 18)
                self.assertEqual(receipt["usage"]["source"], "synthetic_fixture")
                self.assertTrue(receipt["managed_fixture_execution"])
                self.assertFalse(receipt["external_execution"])
                self.assertFalse(receipt["eligible_current_phase"])
                self.assertFalse(receipt["attested"])
                self.assertTrue(receipt["input_write_attempted"])
                self.assertIn("Synthetic diagnostic", receipt["stderr_preview"])

    def test_terminal_cumulative_usage_is_not_summed_with_step_usage(self):
        receipt = transport.execute_fixture(self.prepare())
        self.assertEqual(receipt["usage"]["total_tokens"], 18)
        self.assertNotEqual(receipt["usage"]["total_tokens"], 27)
        self.assertEqual(receipt["usage_semantics"], "provider_reported_cumulative_session")
        self.assertIsNone(receipt["usage"]["cost"])

    def test_nonzero_exit_is_visible_failure(self):
        receipt = transport.execute_fixture(self.prepare(scenario="nonzero_exit"))
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["failure"]["code"], "NONZERO_EXIT")
        self.assertEqual(receipt["exit_code"], 7)

    def test_malformed_and_mismatching_results_are_uncertain(self):
        for scenario, code in (("malformed_output", "INVALID_OUTPUT"), ("conversation_mismatch", "CORRELATION_MISMATCH")):
            with self.subTest(scenario=scenario):
                receipt = transport.execute_fixture(self.prepare(scenario=scenario))
                self.assertEqual(receipt["status"], "uncertain")
                self.assertEqual(receipt["failure"]["code"], code)
                self.assertFalse(receipt["failure"]["retryable"])

    def test_timeout_is_bounded_and_child_reaped(self):
        started = time.monotonic()
        receipt = transport.execute_fixture(self.prepare(scenario="timeout", limits={"timeout_seconds": 0.25}))
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(receipt["status"], "timed_out")
        self.assertEqual(receipt["failure"]["code"], "TIMEOUT")
        self.assertTrue(receipt["failure"]["dispatch_uncertain"])
        self.assertIsNotNone(receipt["exit_code"])

    def test_cancel_running_child_and_pre_intent_cancel(self):
        event = threading.Event()
        prepared = self.prepare(scenario="wait_for_cancel")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(transport.execute_fixture, prepared, event)
            journal = self.root / "transport-journal" / (prepared["request"]["run_id"] + ".jsonl")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if journal.exists() and "input_send_intent" in journal.read_text(encoding="utf-8"):
                    break
                time.sleep(0.01)
            event.set()
            receipt = future.result(timeout=3)
        self.assertEqual(receipt["status"], "cancelled")
        self.assertTrue(receipt["failure"]["dispatch_uncertain"])
        prepared = self.prepare()
        receipt = transport.execute_fixture(prepared, event)
        self.assertEqual(receipt["dispatch_state"], "not_started")
        self.assertFalse(receipt["failure"]["dispatch_uncertain"])
        self.assertFalse((self.root / "transport-journal" / (prepared["request"]["run_id"] + ".jsonl")).exists())

    def test_output_limit_drains_both_pipes_and_keeps_bounded_artifacts(self):
        receipt = transport.execute_fixture(self.prepare(scenario="output_overflow", limits={"max_output_bytes": 32768}))
        self.assertEqual(receipt["failure"]["code"], "OUTPUT_LIMIT")
        self.assertLessEqual(receipt["raw_stdout_bytes"] + receipt["raw_stderr_bytes"], 32768)
        self.assertGreater(receipt["raw_stdout_bytes"], 0)
        self.assertGreater(receipt["raw_stderr_bytes"], 0)
        self.assertTrue(receipt["stdout_truncated"] or receipt["stderr_truncated"])
        self.assertLessEqual(len(receipt["stdout_preview"].encode("utf-8")), 16384)

    def test_oversized_init_line_is_rejected_before_input_write(self):
        receipt = transport.execute_fixture(self.prepare(limits={"max_line_bytes": 64}))
        self.assertEqual(receipt["failure"]["code"], "OUTPUT_LIMIT")
        self.assertFalse(receipt["input_write_attempted"])

    def test_journal_intent_and_fsync_precede_real_spawn(self):
        prepared = self.prepare()
        real_spawn = subprocess.Popen
        def checked_spawn(*args, **kwargs):
            journal = self.root / "transport-journal" / (prepared["request"]["run_id"] + ".jsonl")
            entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(entries[0]["event"], "send_intent")
            self.assertEqual(entries[0]["plan_sha256"], prepared["plan"]["plan_sha256"])
            self.assertFalse(kwargs["shell"])
            if os.name == "nt":
                self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)
            return real_spawn(*args, **kwargs)
        with patch.object(transport.subprocess, "Popen", side_effect=checked_spawn), patch.object(transport.os, "fsync", wraps=os.fsync) as sync:
            receipt = transport.execute_fixture(prepared)
            self.assertGreaterEqual(sync.call_count, 5)
        entries = [json.loads(line) for line in Path(receipt["journal_path"]).read_text(encoding="utf-8").splitlines()]
        events = [entry["event"] for entry in entries]
        self.assertLess(events.index("send_intent"), events.index("process_started"))
        self.assertLess(events.index("input_send_intent"), events.index("input_sent"))

    def test_duplicate_and_restart_never_resend(self):
        prepared = self.prepare()
        receipt = transport.execute_fixture(prepared)
        self.assertEqual(receipt["status"], "succeeded")
        with patch.object(transport.subprocess, "Popen") as spawn:
            with self.assertRaises(transport.ReplayRefused):
                transport.execute_fixture(prepared)
            spawn.assert_not_called()
        recovered = transport.recover_intents(self.root / "transport-journal")
        self.assertEqual(recovered[0]["intent_id"], receipt["intent_id"])

    def test_concurrent_duplicate_attempt_starts_at_most_one_process(self):
        prepared = self.prepare()
        def run():
            try:
                return transport.execute_fixture(prepared)["status"]
            except transport.ReplayRefused:
                return "refused"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: run(), range(2)))
        self.assertCountEqual(results, ["succeeded", "refused"])

    def test_unresolved_or_corrupt_intent_is_interrupted_and_consumed(self):
        prepared = self.prepare()
        directory = self.root / "transport-journal"
        directory.mkdir()
        path = directory / (prepared["request"]["run_id"] + ".jsonl")
        for content in ('{"event":"send_intent"}\n', '{"truncated":'):
            path.write_text(content, encoding="utf-8")
            self.assertEqual(transport.recover_intents(directory)[0]["status"], "interrupted")
            with self.assertRaises(transport.ReplayRefused):
                transport.execute_fixture(prepared)

    def test_orphan_truncated_conflicting_or_unbound_receipt_never_recovers_success(self):
        prepared = self.prepare()
        receipt = transport.execute_fixture(prepared)
        path = Path(receipt["journal_path"])
        original = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        variants = []
        variants.append([{"event": "finished", "receipt": {"run_id": prepared["request"]["run_id"], "status": "succeeded"}}])
        wrong = copy.deepcopy(original)
        wrong[-1]["receipt"]["plan_id"] = "wrong"
        variants.append(wrong)
        wrong = copy.deepcopy(original)
        wrong[-1]["receipt"]["run_id"] = "other"
        variants.append(wrong)
        wrong = copy.deepcopy(original)
        del wrong[0]["request_sha256"]
        variants.append(wrong)
        wrong = copy.deepcopy(original)
        wrong.insert(1, copy.deepcopy(wrong[0]))
        variants.append(wrong)
        wrong = copy.deepcopy(original)
        wrong[0]["event"] = []
        variants.append(wrong)
        for entries in variants:
            with self.subTest(entries=entries):
                path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
                recovered = transport.recover_intents(path.parent)
                self.assertEqual(recovered[0]["status"], "interrupted")
        path.write_text("\n".join(json.dumps(e) for e in original), encoding="utf-8")
        self.assertEqual(transport.recover_intents(path.parent)[0]["status"], "interrupted")

    def test_journal_write_failure_prevents_spawn_and_consumes_id(self):
        prepared = self.prepare()
        with patch.object(transport.os, "fsync", side_effect=OSError("fixture disk failure")), patch.object(transport.subprocess, "Popen") as spawn:
            with self.assertRaises(OSError):
                transport.execute_fixture(prepared)
            spawn.assert_not_called()
        with self.assertRaises(transport.ReplayRefused):
            transport.execute_fixture(prepared)

    def test_spawn_failure_is_recorded_without_retry(self):
        prepared = self.prepare()
        with patch.object(transport.subprocess, "Popen", side_effect=OSError("fixture spawn failure")):
            receipt = transport.execute_fixture(prepared)
        self.assertEqual(receipt["failure"]["code"], "SPAWN_FAILED")
        self.assertFalse(receipt["failure"]["dispatch_uncertain"])
        with self.assertRaises(transport.ReplayRefused):
            transport.execute_fixture(prepared)

    def test_request_or_plan_mutation_denied_before_launch(self):
        for field, value in (("idempotency_key", "shared-key"), ("id", "wrong-request"), ("local_run_id", "wrong"),
                             ("input_sha256", "0" * 64), ("instruction_snapshot", ["changed"])):
            prepared = self.prepare()
            prepared["request"][field] = value
            with patch.object(transport.subprocess, "Popen") as spawn:
                with self.assertRaises(ValueError):
                    transport.execute_fixture(prepared)
                spawn.assert_not_called()
        prepared = self.prepare()
        prepared["plan"]["argv"] = ["arbitrary.exe"]
        with self.assertRaises(ValueError):
            transport.execute_fixture(prepared)

    def test_live_provider_is_denied_before_intent_or_process(self):
        prepared = self.prepare(mode="live_pending")
        self.assertEqual(prepared["plan"]["status"], "awaiting_approval")
        with patch.object(transport.subprocess, "Popen") as spawn:
            with self.assertRaises(ExecutionApprovalRequired):
                transport.execute_provider(prepared, approved=True)
            with self.assertRaises(ExecutionApprovalRequired):
                transport.execute_fixture(prepared)
            spawn.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_isolated_cwd_and_named_environment_whitelist(self):
        prepared = self.prepare()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-not-a-secret", "HTTPS_PROXY": "fixture", "AWS_SECRET_ACCESS_KEY": "fixture"}):
            env = transport.safe_child_environment(Path(prepared["plan"]["cwd"]))
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertEqual(env["HOME"], prepared["plan"]["cwd"])
        receipt = transport.execute_fixture(prepared)
        self.assertTrue(Path(receipt["cwd"]).is_relative_to(self.root))
        self.assertNotEqual(Path(receipt["cwd"]), Path(__file__).parent)

    def test_paths_scenarios_and_limits_cannot_broaden_execution(self):
        for part in ("..", "/absolute", "nested/path", "nested\\path", "stream:ads"):
            with self.assertRaises(ValueError):
                transport._contained(self.root, part)
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(ValueError):
                transport._contained(self.root, "runs", "plain-id")
        for run_id in ("../escape", "a/b", "C:\\escape"):
            with self.assertRaises(ValueError):
                transport.prepare_transport_request(prompt="x", workspace_root=self.root, run_id=run_id)
        for limits in ({"timeout_seconds": 31}, {"max_output_bytes": 1048577}, {"max_input_bytes": True}, {"timeout_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                self.prepare(limits=limits)
        with self.assertRaises(ValueError):
            self.prepare(scenario="arbitrary-code")
        with self.assertRaises(ValueError):
            transport.prepare_transport_request(prompt="x" * 65537, workspace_root=self.root)


class HistoricalAndPinsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "archive.ndjson"
        frames = [{"event": "init", "conversation_id": "archive-conv", "init": {"model": "observed-archive-model"}},
                  {"event": "step_update", "step_update": {"usage": {"total_tokens": 10}}},
                  {"event": "result", "result": {"conversation_id": "archive-conv", "status": "SUCCESS", "response": "Archived synthetic test report", "usage": {"total_tokens": 20}}}]
        self.source.write_text("\n".join(json.dumps(f) for f in frames), encoding="utf-8")
        self.registry = {"reviewed": {"provider": "antigravity", "path": str(self.source),
            "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(), "conversation_id": "archive-conv",
            "metadata": {"started_at": "past-time", "exit_code": 0},
            "source_evidence": {"kind": "synthetic_test_registry", "scope": "test only", "authenticity": "test archive claim"}}}

    def test_historical_integrity_origin_and_usage_are_separate(self):
        with patch.object(transport.subprocess, "Popen") as spawn:
            receipt = transport.import_historical_output("reviewed", registry=self.registry)
            spawn.assert_not_called()
        self.assertEqual(receipt["mode"], "historical")
        self.assertEqual(receipt["provenance"], "historical_external_execution")
        self.assertEqual(receipt["authenticity"], "source_hash_verified")
        self.assertEqual(receipt["provider_authenticity"], "archive_claim")
        self.assertEqual(receipt["usage"]["total_tokens"], 20)
        self.assertEqual(receipt["usage"]["source"], "historical_record")
        self.assertIsNone(receipt["usage"]["cost"])
        self.assertEqual(receipt["provider_calls_added"], 0)
        self.assertEqual(receipt["recorded_execution_at"], "past-time")
        self.assertFalse(receipt["eligible_current_phase"])
        self.assertFalse(receipt["attested"])

    def test_history_remains_importable_without_fixed_release_but_scope_unavailable(self):
        with patch.object(transport, "_verified_bridge_sources", side_effect=ValueError("BRIDGE_FIXED_RELEASE_UNAVAILABLE")):
            receipt = transport.import_historical_output("reviewed", registry=self.registry)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["bridge_release"]["status"], "unavailable")
        self.assertIsNone(receipt["handoff_scope_validation"]["selected_scope_review_complete"])
        self.assertFalse(receipt["handoff_scope_validation"]["project_complete"])

    def test_unregistered_changed_or_unjustified_archive_is_rejected(self):
        with self.assertRaises(ValueError):
            transport.import_historical_output(str(self.source), registry=self.registry)
        self.source.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SOURCE_HASH_MISMATCH"):
            transport.import_historical_output("reviewed", registry=self.registry)
        self.registry["reviewed"]["source_evidence"] = {}
        with self.assertRaises(ValueError):
            transport.import_historical_output("reviewed", registry=self.registry)

    def test_source_pins_detect_changes_without_importing_source(self):
        source = self.root / "ordinary.py"
        source.write_text("raise RuntimeError('must never execute')", encoding="utf-8")
        pins = {"source_root": str(self.root), "files": {"ordinary.py": hashlib.sha256(source.read_bytes()).hexdigest()}}
        self.assertEqual(transport.verify_source_pins(pins)["status"], "verified")
        source.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SOURCE_HASH_MISMATCH"):
            transport.verify_source_pins(pins)

    def test_parent_registered_coupon_archive_preserves_original_1233_tokens(self):
        registry_path = Path(__file__).parent / "HISTORICAL-SOURCES.json"
        if not registry_path.exists():
            self.skipTest("Optional parent-reviewed archive not packaged")
        registry = json.loads(registry_path.read_text(encoding="utf-8-sig"))
        source = Path(registry["argon-coupon-past"]["path"])
        if not source.exists():
            self.skipTest("Parent-reviewed archive path unavailable on this host")
        receipt = transport.import_historical_output("argon-coupon-past", registry=registry)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["usage"]["total_tokens"], 1233)
        self.assertEqual(receipt["model"], "gemini-4-argon-medium")
        self.assertFalse(receipt["external_execution"])


class FixedBridgeReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def blobs(self, *, coverage_changes=None, result_changes=None):
        encode = lambda value: json.dumps(value, ensure_ascii=False).encode("utf-8")
        source = "Source mentions denied as ordinary text.\nSecond line."
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        packet = encode({"sources": [{"project": "synthetic-project", "document": "example.txt", "source_sha256": source_hash,
            "text": source, "excerpt_ranges": [{"start_line": 1, "end_line": 2, "form": "verbatim"}]}]})
        row = {"project": "synthetic-project", "document": "example.txt", "source_sha256": source_hash,
               "reviewed_lines": "L1-L2", "text_form": "verbatim", **(coverage_changes or {})}
        report = encode({"coverage": {"reviewed_sources": [row]}, "findings": [], "not_reviewed": []})
        cid, model = "synthetic-v11-conversation", "gemini-4-argon-medium"
        result = {"conversation_id": cid, "status": "SUCCESS", "response": report.decode("utf-8"),
                  "num_turns": 1, "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}, **(result_changes or {})}
        receipt = {"job_id": "synthetic-v11-job", "conversation_id": cid, "result_conversation_id": cid,
                   "model_requested": model, "effective_init": {"model": model, "conversation_id": cid},
                   "phase": "finished", "outcome": "completed", "prompt_write_completed": True, "exit_code": 0, "result": result}
        stream = [{"event": "init", "conversation_id": cid, "init": {"model": model}},
                  {"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 0, "step_type": "user_input"}},
                  {"event": "result", "result": result}]
        return {"packet.json": packet, "manifest.json": encode({"sha256": hashlib.sha256(packet).hexdigest()}),
                "receipt.json": encode(receipt), "rawreport.txt": report,
                "stdout.ndjson": b"\n".join(encode(frame) for frame in stream), "routes.json": b"{}"}

    def local_pins(self):
        release, sources = transport._verified_bridge_sources()
        for name, raw in sources.items():
            (self.root / name).write_bytes(raw)
        pins = self.root / "pins.json"
        pins.write_text(json.dumps({"release_version": "1.1", "source_root": str(self.root), "files": transport.BRIDGE_V11_HASHES}), encoding="utf-8")
        return pins

    def test_exact_release_hash_and_normal_coverage_aliases_validate_without_writes(self):
        pins = self.local_pins()
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with patch.object(transport.subprocess, "Popen") as spawn:
            result = transport.validate_bridge_handoff_inputs(self.blobs(), pins_path=pins)
            spawn.assert_not_called()
        self.assertEqual(result["bridge_release"]["files"]["handoff.py"], "a4e0dea2c4eaa47d477dd393c49768d773b4913d8f26ce67004b8ab4401af876")
        self.assertTrue(result["provider_success"])
        self.assertTrue(result["report_integrity"])
        self.assertTrue(result["selected_scope_review_complete"])
        self.assertEqual(result["provider"]["usage"]["total_tokens"], 15)
        self.assertEqual(result["provenance"], "synthetic_fixture")
        self.assertFalse(result["project_complete"])
        self.assertFalse(result["eligible_current_phase"])
        self.assertFalse(result["attested"])
        self.assertEqual(result["application"], "not_performed")
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_entrypoint_or_dependency_mismatch_prevents_all_module_execution(self):
        pins = self.local_pins()
        for name in ("handoff.py", "usage_audit.py"):
            original = (self.root / name).read_bytes()
            (self.root / name).write_bytes(original + b"\n# changed")
            with patch.object(transport, "_execute_pinned_module") as execute:
                with self.assertRaisesRegex(ValueError, "BRIDGE_SOURCE_HASH_MISMATCH"):
                    transport.validate_bridge_handoff_inputs(self.blobs(), pins_path=pins)
                execute.assert_not_called()
            (self.root / name).write_bytes(original)

    def test_loader_ignores_existing_mutable_usage_module_without_global_import_edits(self):
        import types
        existing = types.ModuleType("usage_audit")
        existing.audit_record = lambda *args: (_ for _ in ()).throw(AssertionError("Mutable module used"))
        before_path = list(sys.path)
        with patch.dict(sys.modules, {"usage_audit": existing}):
            result = transport.validate_bridge_handoff_inputs(self.blobs())
            self.assertIs(sys.modules["usage_audit"], existing)
        self.assertTrue(result["provider_success"])
        self.assertEqual(sys.path, before_path)

    def test_coverage_error_kept_separate_from_provider_success(self):
        result = transport.validate_bridge_handoff_inputs(self.blobs(coverage_changes={"reviewed_lines": "L1"}))
        self.assertTrue(result["provider_success"])
        self.assertFalse(result["selected_scope_review_complete"])
        self.assertIn("COVERAGE_RANGE_MISMATCH", result["coverage_errors"])
        result = transport.validate_bridge_handoff_inputs(self.blobs(coverage_changes={"form": "summary"}))
        self.assertFalse(result["selected_scope_review_complete"])

    def test_packet_integrity_denial_and_accounting_fail_closed(self):
        blobs = self.blobs()
        blobs["packet.json"] += b" "
        with self.assertRaisesRegex(ValueError, "BRIDGE_HANDOFF_INPUT_REJECTED"):
            transport.validate_bridge_handoff_inputs(blobs)
        for changes in ({"status": "DENIED"}, {"usage": {"total_tokens": 15}}, {"response": ""}):
            result = transport.validate_bridge_handoff_inputs(self.blobs(result_changes=changes))
            self.assertFalse(result["provider_success"])
            self.assertFalse(result["selected_scope_review_complete"])

    def test_incomplete_or_oversized_contract_rejected_with_bounded_error(self):
        with self.assertRaisesRegex(ValueError, "BRIDGE_HANDOFF_INPUT_SET_MISMATCH"):
            transport.validate_bridge_handoff_inputs({"stdout.ndjson": b"stream only"})
        blobs = self.blobs()
        blobs["rawreport.txt"] = b"x" * 1048577
        with self.assertRaisesRegex(ValueError, "BRIDGE_HANDOFF_INPUT_SIZE_OR_TYPE_LIMIT"):
            transport.validate_bridge_handoff_inputs(blobs)
        blobs = self.blobs(coverage_changes={"reviewed_lines": "L1-L9999999999999999999"})
        with self.assertRaisesRegex(ValueError, "BRIDGE_HANDOFF_INPUT_REJECTED"):
            transport.validate_bridge_handoff_inputs(blobs)


if __name__ == "__main__":
    unittest.main()
