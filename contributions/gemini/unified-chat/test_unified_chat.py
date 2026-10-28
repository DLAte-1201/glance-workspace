"""Unified 3-provider chat and cross-review verification suite (offline synthetic adapter only)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from workroom import DomainError, Workroom


class UnifiedChatVerificationTests(unittest.TestCase):
    """Verifies all 7 mandatory scenarios for the 3-provider unified chat flow."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Path(self.tmp.name, "sources.json")
        self.registry.write_text("{}")
        self.room = Workroom(self.tmp.name, history_registry_path=self.registry)

    def tearDown(self):
        self.room.close()
        self.tmp.cleanup()

    def command(self, command_name, **payload):
        return self.room.command(command_name, self.room.state()["version"], payload)

    def wait_runs(self, run_ids, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.room.state()
            by_id = {r["id"]: r for r in state["runs"] if r["id"] in run_ids}
            if len(by_id) == len(run_ids) and all(
                r["status"] not in ("queued", "running", "cancel_requested") for r in by_id.values()
            ):
                return [by_id[rid] for rid in run_ids]
            time.sleep(0.02)
        self.fail("Unified chat runs did not reach terminal states within timeout")

    def test_1_three_providers_succeed_concurrently_with_shared_context_and_distinct_identities(self):
        self.command("add_instruction", text="모든 제안에는 확인 가능한 근거를 포함할 것", kind="instruction", scope="project")
        result = self.command(
            "send_chat_request",
            text="다음 작업으로 무엇을 먼저 진행해야 할지 제안해줘.",
            providers=["codex", "claude", "antigravity"],
            client_request_id="req-3-success",
        )
        run_ids = result["run_ids"]
        self.assertEqual(3, len(run_ids))
        self.assertEqual(["codex", "claude", "antigravity"], result["selected_providers"])

        queued_runs = [r for r in result["state"]["runs"] if r["id"] in run_ids]
        self.assertEqual(3, len(queued_runs))

        finished_runs = self.wait_runs(run_ids)
        by_provider = {r["provider"]: r for r in finished_runs}
        self.assertEqual({"codex", "claude", "antigravity"}, set(by_provider.keys()))

        self.assertEqual("ChatGPT", by_provider["codex"]["display_name"])
        self.assertEqual("codex_dot", by_provider["codex"]["connector_id"])
        self.assertIsNone(by_provider["codex"]["requested_model"])
        self.assertEqual("synthetic-codex-fixture", by_provider["codex"]["reported_model"])

        self.assertEqual("Claude", by_provider["claude"]["display_name"])
        self.assertEqual("claude", by_provider["claude"]["connector_id"])
        self.assertIsNone(by_provider["claude"]["requested_model"])
        self.assertEqual("synthetic-claude-fixture", by_provider["claude"]["reported_model"])

        self.assertEqual("Gemini", by_provider["antigravity"]["display_name"])
        self.assertEqual("antigravity", by_provider["antigravity"]["connector_id"])
        self.assertIsNone(by_provider["antigravity"]["requested_model"])
        self.assertEqual("synthetic-antigravity-fixture", by_provider["antigravity"]["reported_model"])

        snapshots = {r["context_snapshot"]["snapshot_id"] for r in finished_runs}
        self.assertEqual(1, len(snapshots))
        for p, r in by_provider.items():
            self.assertEqual("succeeded", r["status"], p)
            self.assertEqual("synthetic_fixture", r["provenance"], p)
            self.assertIn("다음 작업으로 무엇을 먼저 진행해야 할지 제안해줘.", r["response"])
            self.assertIn("모든 제안에는 확인 가능한 근거를 포함할 것", r["response"])

    def test_2_one_provider_fails_or_times_out_while_remaining_two_succeed(self):
        res_fail = self.command(
            "send_chat_request",
            text="부분 실패 격리 검증 질문입니다.",
            providers=["codex", "claude", "antigravity"],
            scenarios={"codex": "success", "claude": "nonzero_exit", "antigravity": "success"},
            client_request_id="req-partial-fail",
        )
        runs_fail = {r["provider"]: r for r in self.wait_runs(res_fail["run_ids"])}
        self.assertEqual("succeeded", runs_fail["codex"]["status"])
        self.assertEqual("succeeded", runs_fail["antigravity"]["status"])
        self.assertEqual("failed", runs_fail["claude"]["status"])
        self.assertEqual("NONZERO_EXIT", runs_fail["claude"]["failure"]["code"])
        self.assertEqual("", runs_fail["claude"]["response"] or "")
        self.assertIn("부분 실패 격리 검증 질문입니다.", runs_fail["codex"]["response"])
        self.assertIn("부분 실패 격리 검증 질문입니다.", runs_fail["antigravity"]["response"])

        res_timeout = self.command(
            "send_chat_request",
            text="부분 시간 초과 격리 검증 질문입니다.",
            providers=["codex", "claude", "antigravity"],
            scenarios={"codex": "success", "claude": "success", "antigravity": "timeout"},
            client_request_id="req-partial-timeout",
        )
        runs_timeout = {r["provider"]: r for r in self.wait_runs(res_timeout["run_ids"])}
        self.assertEqual("succeeded", runs_timeout["codex"]["status"])
        self.assertEqual("succeeded", runs_timeout["claude"]["status"])
        self.assertEqual("timed_out", runs_timeout["antigravity"]["status"])
        self.assertEqual("TIMEOUT", runs_timeout["antigravity"]["failure"]["code"])
        self.assertEqual("", runs_timeout["antigravity"]["response"] or "")

    def test_3_cross_review_passes_only_question_selected_answer_and_common_context(self):
        self.command("send_message", text="무관한 과거 잡담 메시지 (검토 프롬프트에 들어가면 안 됨)", agent_id="codex")
        up = self.command("begin_upload", name="unrelated.txt", size=14)["state"]["uploads"][-1]["id"]
        self.command("send_upload", upload_id=up, text="unrelated file")
        self.command("complete_upload", upload_id=up)

        self.command("add_instruction", text="보안 자격증명 파일 접근 금지", kind="instruction", scope="project")

        chat_res = self.command(
            "send_chat_request",
            text="통합 채팅 상태 저장 구조를 어떻게 설계할까?",
            providers=["codex", "claude"],
            client_request_id="req-for-review",
        )
        question_msg_id = chat_res["message_id"]
        finished = {r["provider"]: r for r in self.wait_runs(chat_res["run_ids"])}
        codex_run = finished["codex"]
        self.assertEqual("succeeded", codex_run["status"])

        review_res = self.command(
            "request_cross_review",
            source_run_id=codex_run["id"],
            source_message_id=codex_run["response_event_id"],
            target_providers=["claude", "antigravity"],
            review_note="누락된 예외 처리나 취소 경합 조건이 있는지 검토해줘.",
            client_request_id="req-cross-review-1",
        )
        self.assertEqual(codex_run["id"], review_res["review_of_run_id"])
        self.assertEqual(codex_run["response_event_id"], review_res["review_of_message_id"])
        self.assertEqual(question_msg_id, review_res["source_question_message_id"])

        review_runs = {r["provider"]: r for r in self.wait_runs(review_res["run_ids"])}
        self.assertEqual({"claude", "antigravity"}, set(review_runs.keys()))

        for provider, r in review_runs.items():
            self.assertEqual("succeeded", r["status"], provider)
            self.assertEqual(codex_run["id"], r["review_of_run_id"])
            self.assertEqual(codex_run["response_event_id"], r["review_of_message_id"])
            self.assertEqual(question_msg_id, r["source_question_message_id"])
            self.assertEqual("codex", r["review_source_provider"])

            plan_doc = next(p for p in self.room.state()["execution_plans"] if p["id"] == r["plan_id"])
            sent_prompt = plan_doc["request"]["input_text"]
            self.assertIn("[원질문]\n통합 채팅 상태 저장 구조를 어떻게 설계할까?", sent_prompt)
            self.assertIn("[선택한 응답 (ChatGPT · codex)]", sent_prompt)
            self.assertIn(codex_run["response"], sent_prompt)
            self.assertIn("보안 자격증명 파일 접근 금지", sent_prompt)
            self.assertIn("누락된 예외 처리나 취소 경합 조건이 있는지 검토해줘.", sent_prompt)
            self.assertNotIn("무관한 과거 잡담 메시지", sent_prompt)
            self.assertNotIn("unrelated file", sent_prompt)

    def test_4_identical_sentence_re_question_preserved_while_duplicate_request_id_blocked(self):
        first = self.command(
            "send_chat_request",
            text="같은 문장으로 다시 질문합니다.",
            providers=["codex"],
            client_request_id="idemp-key-100",
        )
        self.wait_runs(first["run_ids"])
        version_after_first = self.room.state()["version"]
        runs_count_after_first = len(self.room.state()["runs"])

        dup = self.command(
            "send_chat_request",
            text="같은 문장으로 다시 질문합니다.",
            providers=["codex"],
            client_request_id="idemp-key-100",
        )
        self.assertTrue(dup.get("unchanged"))
        self.assertEqual("idemp-key-100", dup.get("duplicate_request_id"))
        self.assertEqual(first["run_ids"], dup.get("run_ids"))
        self.assertEqual(version_after_first, self.room.state()["version"])
        self.assertEqual(runs_count_after_first, len(self.room.state()["runs"]))

        second = self.command(
            "send_chat_request",
            text="같은 문장으로 다시 질문합니다.",
            providers=["codex"],
            client_request_id="idemp-key-101",
        )
        self.assertFalse(second.get("unchanged", False))
        self.assertNotEqual(first["message_id"], second["message_id"])
        self.assertNotEqual(first["run_ids"], second["run_ids"])
        self.wait_runs(second["run_ids"])
        self.assertEqual(runs_count_after_first + 1, len(self.room.state()["runs"]))

    def test_5_late_response_after_cancellation_never_revives_cancelled_run(self):
        res = self.command(
            "send_chat_request",
            text="취소 경합 테스트 요청입니다.",
            providers=["antigravity"],
            scenarios={"antigravity": "wait_for_cancel"},
            client_request_id="req-cancel-late",
        )
        run_id = res["run_ids"][0]

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            r = next(x for x in self.room.state()["runs"] if x["id"] == run_id)
            if r["status"] == "running":
                break
            time.sleep(0.01)

        cancel_res = self.command("cancel_run", run_id=run_id)
        self.assertEqual("cancel_requested", cancel_res["state"]["runs"][-1]["status"])

        late_receipt = {
            "status": "succeeded",
            "outcome": "completed",
            "conversation_id": "fixture-" + run_id,
            "model": "synthetic-antigravity-fixture",
            "response": "Late response that should never overwrite cancellation!",
            "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10, "source": "synthetic_fixture"},
            "finished_at": "2026-10-27T00:00:00Z",
        }
        self.room._record_fixture_update(run_id, late_receipt)

        final_run = self.wait_runs([run_id])[0]
        self.assertEqual("cancelled", final_run["status"])
        self.assertTrue(final_run.get("late_response_ignored"))
        self.assertIsNone(final_run["response"])

        self.assertFalse(self.room._record_fixture_update(run_id, late_receipt))
        self.assertEqual("cancelled", next(x for x in self.room.state()["runs"] if x["id"] == run_id)["status"])

    def test_6_reload_restores_conversation_without_resending_and_marks_inflight_disconnected(self):
        res = self.command(
            "send_chat_request",
            text="재시작 복원 검증 질문입니다.",
            providers=["codex", "claude"],
            client_request_id="req-reload-1",
        )
        self.wait_runs(res["run_ids"])

        with patch.object(self.room, "_launch_fixture"):
            inflight_res = self.command(
                "send_chat_request",
                text="재시작 직전 미완료 요청입니다.",
                providers=["antigravity"],
                client_request_id="req-reload-inflight",
            )
        inflight_run_id = inflight_res["run_ids"][0]
        self.room.close()

        with patch("subprocess.Popen") as spawn:
            self.room = Workroom(self.tmp.name, history_registry_path=self.registry)
            spawn.assert_not_called()

        restored = self.room.state()
        for rid in res["run_ids"]:
            r = next(x for x in restored["runs"] if x["id"] == rid)
            self.assertEqual("succeeded", r["status"])
            self.assertIn("재시작 복원 검증 질문입니다.", r["response"])

        inflight_run = next(x for x in restored["runs"] if x["id"] == inflight_run_id)
        self.assertEqual("disconnected", inflight_run["status"])
        self.assertEqual("INTERRUPTED", inflight_run["failure"]["code"])

    def test_7_historical_responses_and_current_synthetic_responses_are_strictly_distinguished(self):
        source = Path(self.tmp.name, "past-archive.ndjson")
        source.write_text(
            json.dumps({"event": "init", "conversation_id": "hist-conv-1", "init": {"model": "gemini-4-argon-medium"}}) + "\n"
            + json.dumps({
                "event": "result",
                "result": {
                    "conversation_id": "hist-conv-1",
                    "status": "SUCCESS",
                    "response": "Past recorded external execution response.",
                    "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
                },
            }) + "\n",
            encoding="utf-8",
        )
        self.registry.write_text(json.dumps({
            "past-archive-1": {
                "path": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "provider": "antigravity",
                "conversation_id": "hist-conv-1",
                "metadata": {"started_at": "2026-10-26T00:00:00Z", "exit_code": 0},
                "source_evidence": {"kind": "historical_archive", "scope": "test", "authenticity": "archive_claim"},
            }
        }))

        hist_res = self.command("import_history", source_id="past-archive-1")
        hist_run_id = hist_res["run_id"]

        synth_res = self.command(
            "send_chat_request",
            text="이번 합성 실행 요청입니다.",
            providers=["antigravity"],
            client_request_id="req-synth-vs-hist",
        )
        synth_run_id = synth_res["run_ids"][0]
        self.wait_runs([synth_run_id])

        state = self.room.state()
        hist_run = next(r for r in state["runs"] if r["id"] == hist_run_id)
        synth_run = next(r for r in state["runs"] if r["id"] == synth_run_id)

        self.assertEqual("historical", hist_run["mode"])
        self.assertEqual("historical_import", hist_run["run_kind"])
        self.assertEqual("historical_external_execution", hist_run["provenance"])
        self.assertEqual("historical_record", hist_run["usage"]["source"])

        self.assertEqual("synthetic", synth_run["mode"])
        self.assertEqual("local_fixture", synth_run["run_kind"])
        self.assertEqual("synthetic_fixture", synth_run["provenance"])
        self.assertEqual("synthetic_fixture", synth_run["usage"]["source"])


if __name__ == "__main__":
    unittest.main()