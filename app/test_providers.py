"""Synthetic protocol fixtures only. No provider, network or login is invoked."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import providers


def stream(*frames):
    return "\n".join(json.dumps(frame) for frame in frames)


def claude_init(cid="session-1", model="observed-claude"):
    return {"type": "system", "subtype": "init", "session_id": cid, "model": model}


def claude_result(cid="session-1", **changes):
    return {"type": "result", "subtype": "success", "is_error": False,
            "session_id": cid, "result": "Actual review report.", **changes}


def agy_init(cid="conversation-1"):
    return {"event": "init", "conversation_id": cid, "init": {"model": "observed-gemini"}}


def agy_result(cid="conversation-1", **changes):
    return {"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS",
                                           "response": "Actual Gemini report.", **changes}}


class ProviderNormalizationTests(unittest.TestCase):
    def normalize(self, provider, output, **kwargs):
        return providers.normalize_output(provider, output, local_run_id="local-123", **kwargs)

    def test_claude_stream_success_has_observed_identity_and_synthetic_source(self):
        result = self.normalize("claude", stream(claude_init(), claude_result()))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["run_status"], "succeeded")
        self.assertEqual(result["model"], "observed-claude")
        self.assertEqual(result["local_run_id"], "local-123")
        self.assertEqual(result["external_conversation_id"], "session-1")
        self.assertNotEqual(result["external_conversation_id"], result["local_run_id"])
        self.assertEqual(result["provenance"], "synthetic_fixture")
        self.assertFalse(result["attested"])
        self.assertFalse(result["retry_allowed"])

    def test_codex_documented_jsonl_correlates_thread_report_and_usage(self):
        frames = [{"type": "thread.started", "thread_id": "codex-thread"}, {"type": "turn.started"},
                  {"type": "item.completed", "item": {"id": "message-1", "type": "agent_message", "text": "Actual report"}},
                  {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}]
        result = self.normalize("codex", stream(*frames), expected_conversation_id="codex-thread")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["conversation_id"], "codex-thread")
        self.assertEqual(result["report"], "Actual report")
        self.assertIsNone(result["model"])
        self.assertIsNone(result["effort"])
        self.assertEqual(result["usage"], {"input_tokens": 10, "output_tokens": 2})
        self.assertNotIn("total_tokens", result["usage"])
        self.assertFalse(result["attested"])

    def test_codex_missing_thread_report_or_terminal_and_duplicates_are_uncertain(self):
        init = {"type": "thread.started", "thread_id": "thread"}
        report = {"type": "item.completed", "item": {"id": "id", "type": "agent_message", "text": "Report"}}
        terminal = {"type": "turn.completed", "usage": {"total_tokens": 1}}
        for frames in ((init, report), (init, terminal), (report, terminal), (init, init, report, terminal),
                       (init, report, report, terminal), (init, report, terminal, terminal)):
            result = self.normalize("codex", stream(*frames))
            self.assertEqual(result["status"], "uncertain")

    def test_antigravity_final_usage_preserved_without_step_sum(self):
        result = self.normalize("antigravity", stream(agy_init(), {"event": "step_update", "step_update": {"usage": {"total_tokens": 9}}}, agy_result(usage={"total_tokens": 20})))
        self.assertEqual(result["usage"], {"total_tokens": 20})
        self.assertIsNone(result["billed_amount"])

    def test_effort_only_from_observed_output_and_conflicts_fail_closed(self):
        init = agy_init()
        init["init"]["effort"] = "high"
        result = self.normalize("antigravity", stream(init, agy_result()))
        self.assertEqual(result["effort"], "high")
        result = self.normalize("antigravity", stream(init, agy_result(effort="low")))
        self.assertEqual(result["status"], "uncertain")
        self.assertIn("conflicting_efforts", result["issues"])

    def test_agy_stream_official_event_shape(self):
        result = self.normalize("antigravity", stream(agy_init(), {"event": "step_update", "step_update": {"text": "partial"}}, agy_result()))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["model"], "observed-gemini")
        self.assertEqual(result["report"], "Actual Gemini report.")

    def test_claude_json_with_expected_session(self):
        result = self.normalize("claude", json.dumps(claude_result()), expected_conversation_id="session-1", output_format="json")
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["model"])
        self.assertIsNone(result["effort"])

    def test_agy_json_envelope_and_direct_payload(self):
        for data in (agy_result(), agy_result()["result"]):
            with self.subTest(data=data):
                result = self.normalize("antigravity", json.dumps(data), expected_conversation_id="conversation-1", output_format="json")
                self.assertEqual(result["status"], "completed")
                self.assertIsNone(result["model"])

    def test_json_result_without_expected_id_is_unmatched(self):
        for provider, frame in (("claude", claude_result()), ("antigravity", agy_result())):
            with self.subTest(provider=provider):
                result = self.normalize(provider, json.dumps(frame))
                self.assertEqual(result["status"], "uncertain")
                self.assertIn("conversation_not_independently_matched", result["issues"])

    def test_exit_zero_and_assistant_text_do_not_complete(self):
        result = self.normalize("claude", stream(claude_init(), {"type": "assistant", "session_id": "session-1", "message": {"model": "observed-claude", "content": [{"type": "text", "text": "Done"}]}}))
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["report"], "")
        self.assertIn("missing_terminal_result", result["issues"])

    def test_success_requires_report(self):
        for report in ("", "  ", None, {"text": "not a report string"}):
            with self.subTest(report=report):
                result = self.normalize("claude", stream(claude_init(), claude_result(result=report)))
                self.assertEqual(result["status"], "uncertain")

    def test_expected_init_and_terminal_ids_must_all_match(self):
        cases = [stream(claude_init(), claude_result("other")),
                 stream(claude_init("wrong"), claude_result()),
                 stream(claude_init(), {"type": "stream_event", "session_id": "wrong", "event": {}}, claude_result())]
        for output in cases:
            result = self.normalize("claude", output, expected_conversation_id="session-1")
            self.assertEqual(result["status"], "uncertain")
            self.assertIn("conversation_mismatch", result["issues"])

    def test_agy_outer_and_nested_ids_cannot_conflict(self):
        terminal = agy_result()
        terminal["conversation_id"] = "wrong"
        result = self.normalize("antigravity", stream(agy_init(), terminal))
        self.assertEqual(result["status"], "uncertain")
        self.assertIn("conflicting_conversation_ids", result["issues"])

    def test_agy_outer_id_cannot_replace_missing_result_identity(self):
        terminal = agy_result()
        terminal["conversation_id"] = "conversation-1"
        del terminal["result"]["conversation_id"]
        result = self.normalize("antigravity", stream(agy_init(), terminal))
        self.assertEqual(result["status"], "uncertain")
        self.assertIn("result_missing_conversation_id", result["issues"])

    def test_any_post_terminal_frame_is_uncertain(self):
        result = self.normalize("claude", stream(claude_init(), claude_result(), {"type": "system", "subtype": "other"}))
        self.assertEqual(result["status"], "uncertain")
        self.assertIn("frame_after_terminal", result["issues"])

    def test_missing_terminal_id_cannot_be_replaced_by_local_or_init_id(self):
        terminal = claude_result()
        del terminal["session_id"]
        result = self.normalize("claude", stream(claude_init(), terminal))
        self.assertEqual(result["status"], "uncertain")
        self.assertIn("result_missing_conversation_id", result["issues"])

    def test_duplicate_or_conflicting_terminal_results(self):
        for second in (claude_result(), claude_result(subtype="error_during_execution", is_error=True)):
            result = self.normalize("claude", stream(claude_init(), claude_result(), second))
            self.assertEqual(result["status"], "uncertain")
            self.assertIn("duplicate_terminal_result", result["issues"])

    def test_duplicate_init_and_duplicate_frame_id(self):
        result = self.normalize("antigravity", stream(agy_init(), agy_init(), agy_result()))
        self.assertIn("duplicate_init", result["issues"])
        init = {**claude_init(), "uuid": "event-1"}
        terminal = {**claude_result(), "uuid": "event-1"}
        result = self.normalize("claude", stream(init, terminal))
        self.assertIn("duplicate_frame_id", result["issues"])
        self.assertEqual(result["status"], "uncertain")

    def test_malformed_trailing_frame_prevents_completion(self):
        for extra in ('{"type":', 'null', '[]', 'not-json', '{"type":"result","type":"result"}', '{"value":NaN}'):
            result = self.normalize("claude", stream(claude_init(), claude_result()) + "\n" + extra)
            self.assertEqual(result["status"], "uncertain")
            self.assertTrue(any(issue.startswith("malformed_frame") for issue in result["issues"]))

    def test_duplicate_keys_in_json_and_nonfinite_values_rejected(self):
        for output in ('{"type":"result","type":"result"}', '{"x":Infinity}'):
            result = self.normalize("claude", output, output_format="json")
            self.assertEqual(result["issues"], ["malformed_json", "conversation_not_independently_matched", "missing_terminal_result"])

    def test_conflicting_models_are_uncertain(self):
        result = self.normalize("claude", stream(claude_init(), {"type": "assistant", "message": {"model": "different-model"}}, claude_result()))
        self.assertEqual(result["status"], "uncertain")
        self.assertIsNone(result["model"])
        self.assertIn("conflicting_models", result["issues"])

    def test_failure_and_cancellation_preserved_separately(self):
        for status, subtype in (("failed", "error_during_execution"), ("cancelled", "cancelled")):
            result = self.normalize("claude", stream(claude_init(), claude_result(subtype=subtype, is_error=status == "failed", result="")))
            self.assertEqual(result["status"], status)
            self.assertFalse(result["retry_allowed"])
        for status, raw in (("failed", "ERROR"), ("cancelled", "CANCELLED")):
            result = self.normalize("antigravity", stream(agy_init(), agy_result(status=raw, response="")))
            self.assertEqual(result["status"], status)

    def test_error_signal_conflicting_with_success_is_uncertain(self):
        result = self.normalize("claude", stream(claude_init(), {"type": "error", "error": "failed"}, claude_result()))
        self.assertEqual(result["status"], "uncertain")
        result = self.normalize("antigravity", stream(agy_init(), agy_result(error="failure despite success")))
        self.assertEqual(result["status"], "uncertain")

    def test_success_with_nonzero_unknown_or_bool_exit_is_uncertain(self):
        for exit_code in (1, -15, None, True):
            result = self.normalize("claude", stream(claude_init(), claude_result()), exit_code=exit_code)
            self.assertEqual(result["status"], "uncertain")

    def test_caller_cannot_attest_imported_output(self):
        for provenance in ("imported_unverified", "actual_managed_execution"):
            result = self.normalize("claude", stream(claude_init(), claude_result()), provenance=provenance)
            self.assertEqual(result["provenance"], "imported_unverified")
            self.assertEqual(result["provenance_claim"], provenance)
            self.assertFalse(result["attested"])
            self.assertEqual(result["receipt_source"], "supplied_output")

    def test_malformed_or_unknown_shapes_fail_closed(self):
        cases = ["", b"\xff", "[]", "42", json.dumps({"event": "result", "result": []}),
                 json.dumps({"type": "unknown"})]
        for output in cases:
            with self.subTest(output=output):
                self.assertEqual(self.normalize("claude", output)["status"], "uncertain")
        result = self.normalize("antigravity", stream(agy_init(), {"event": "result", "result": {"conversation_id": "conversation-1", "status": [], "response": "x"}}))
        self.assertEqual(result["status"], "uncertain")

    def test_deeply_nested_json_fails_closed_in_each_format(self):
        deeply_nested = "[" * 5000 + "0" + "]" * 5000
        for provider in providers.PROVIDERS:
            for output_format in ("auto", "json", "stream-json"):
                with self.subTest(provider=provider, output_format=output_format):
                    result = self.normalize(provider, deeply_nested, output_format=output_format)
                    self.assertEqual(result["status"], "uncertain")
                    self.assertTrue(any(issue.startswith("malformed_") for issue in result["issues"]))

    def test_size_limit_and_invalid_observed_model(self):
        with patch.object(providers, "MAX_OUTPUT_BYTES", 10):
            result = self.normalize("claude", " " * 11)
            self.assertIn("output_size_limit", result["issues"])
        result = self.normalize("claude", stream(claude_init(model=[]), claude_result()))
        self.assertEqual(result["status"], "uncertain")

    def test_provider_protocols_not_interchangeable(self):
        self.assertEqual(self.normalize("antigravity", stream(claude_init(), claude_result()))["status"], "uncertain")
        self.assertEqual(self.normalize("claude", stream(agy_init(), agy_result()))["status"], "uncertain")


class DisabledTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.metadata = Path(self.tmp.name) / "CONNECTIONS.json"
        self.metadata.write_text(json.dumps({"checked_at": "fixture-time", "providers": {
            "antigravity": {"installed": True, "binary": "C:\\fixture\\agy.exe", "authenticated": "not_checked"},
            "claude": {"installed": "not_found_in_limited_check"}}}), encoding="utf-8")

    def test_metadata_cards_truthfully_separate_discovery_from_connection(self):
        cards = providers.connector_statuses(self.metadata)
        self.assertEqual(len(cards), 3)
        agy = next(c for c in cards if c["provider"] == "antigravity")
        self.assertTrue(agy["detected"])
        self.assertEqual(agy["executable_path"], "C:\\fixture\\agy.exe")
        for card in cards:
            self.assertFalse(card["connected"])
            self.assertFalse(card["authorized"])
            self.assertFalse(any(card["supports"].values()))
            self.assertEqual(card["status"], "disabled")

    def test_missing_or_invalid_metadata_has_visible_error(self):
        self.assertIsNotNone(providers.load_provider_metadata(self.metadata.with_suffix(".missing"))["metadata_error"])
        self.metadata.write_text("[]", encoding="utf-8")
        self.assertIsNotNone(providers.load_provider_metadata(self.metadata)["metadata_error"])

    def test_malformed_nested_metadata_keeps_connector_cards_available(self):
        for value in ([], None, "not-an-object", 1):
            with self.subTest(value=value):
                self.metadata.write_text(json.dumps({"providers": {"claude": value}}), encoding="utf-8")
                cards = providers.connector_statuses(self.metadata)
                self.assertEqual(len(cards), 3)
                self.assertTrue(all(card["metadata_error"] for card in cards))
                self.assertTrue(all(not card["connected"] for card in cards))
        self.metadata.write_text("[" * 5000 + "0" + "]" * 5000, encoding="utf-8")
        self.assertTrue(providers.load_provider_metadata(self.metadata)["metadata_error"])

    def test_plans_are_concrete_but_never_authorized(self):
        for provider in providers.PROVIDERS:
            plan = providers.build_execution_plan(provider, "Review safely $(fixture)", local_run_id="local-id", cwd=self.tmp.name,
                conversation_id="external-existing", model="requested-only", connections_path=self.metadata)
            self.assertEqual(plan["argv"][-2:], ["--model", "requested-only"])
            self.assertFalse(plan["shell"])
            self.assertFalse(plan["command_validated"])
            self.assertEqual(plan["transport"], "disabled")
            self.assertEqual(plan["external_conversation_id"], "external-existing")
            self.assertEqual(plan["status"], "pending_specific_approval")
            self.assertNotIn("local-id", plan["argv"])
        agy = providers.build_execution_plan("antigravity", "Report", local_run_id="local", cwd=self.tmp.name, connections_path=self.metadata)
        self.assertEqual(json.loads(agy["stdin"])["message"]["content"], "Report")

    def test_no_boolean_token_or_environment_flag_bypasses_submit(self):
        for provider in providers.PROVIDERS:
            connector = providers.get_connector(provider)
            with patch.dict(os.environ, {"ALLOW_EXTERNAL_EXECUTION": "1", "WORKROOM_EXECUTION_APPROVED": "true"}):
                with self.assertRaises(providers.ExecutionApprovalRequired):
                    connector.submit({"approved": True}, approved=True)
                with self.assertRaises(providers.ExecutionApprovalRequired):
                    connector.execute({"approved": True}, specific_user_authorization="yes")
                with self.assertRaises(providers.ExecutionApprovalRequired):
                    connector.cancel("nonexistent")

    def test_prepare_requires_context_and_preserves_it(self):
        connector = providers.get_connector("antigravity")
        with self.assertRaises(ValueError):
            connector.prepare({"prompt": "hello"})
        request = {"project_id": "p", "phase_id": "phase", "revision": "rev", "instruction_version": 3,
                   "instruction_snapshot": ["Correction"], "role": "reviewer_gemini", "context_references": ["event"],
                   "prompt": "Review", "cwd": self.tmp.name, "local_run_id": "local", "connections_path": self.metadata}
        plan = connector.prepare(request)
        self.assertEqual(plan["context"]["instruction_version"], 3)
        self.assertEqual(connector.probe(self.metadata)["provider"], "antigravity")

    def test_unknown_provider_and_invalid_identifiers_rejected(self):
        with self.assertRaises(ValueError):
            providers.get_connector("other")
        with self.assertRaises(ValueError):
            providers.normalize_output("claude", "{}", local_run_id="")
        with self.assertRaises(ValueError):
            providers.build_execution_plan("claude", "", local_run_id="run", cwd=self.tmp.name)


if __name__ == "__main__":
    unittest.main()
