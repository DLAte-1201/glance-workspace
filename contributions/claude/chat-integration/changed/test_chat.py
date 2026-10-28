"""통합 채팅(3개 제공자) 합성 검사. 네트워크·실제 모델 없이 실제 Workroom 명령과 실제 로컬 합성 하위 프로세스(fixtures/transport_child.py)를 사용한다.
검사 결과는 미리 만든 카드가 아니라 명령 실행 뒤 상태·전송 기록·하위 프로세스가 보고한 수신 해시에서 계산한다."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest

from workroom import DomainError, Workroom

HERE = Path(__file__).resolve().parent
TERMINAL = ("succeeded", "failed", "cancelled", "disconnected", "timed_out", "interrupted", "uncertain")
DISPLAY = {"codex": "ChatGPT", "claude": "Claude", "antigravity": "Gemini"}


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def expected_ask_prompt(question, instructions, version):
    # 문서화된 전송 형식을 검사 쪽에서 따로 조립한다(구현 함수 재사용 아님).
    lines = ["[Glance 공통 맥락 v%d]" % version] + ["- (%s) %s" % (k, t) for k, t in instructions] if instructions else ["[Glance 공통 맥락 v%d]" % version, "- (없음)"]
    return "\n".join(lines) + "\n\n[질문]\n" + question


def expected_review_prompt(question, answer, source_display, instructions, version):
    lines = ["[Glance 공통 맥락 v%d]" % version] + ["- (%s) %s" % (k, t) for k, t in instructions]
    return ("\n".join(lines) + "\n\n[원래 질문]\n" + question + "\n\n[검토할 답변 · " + source_display + "]\n" + answer +
            "\n\n[요청]\n위 답변을 원래 질문과 공통 맥락에 비추어 검토하고, 틀리거나 빠진 점과 근거를 짧게 적어 주세요.")


class ChatBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.room = Workroom(self.tmp.name)

    def tearDown(self):
        self.room.close()
        self.tmp.cleanup()

    def cmd(self, name, **payload):
        for _ in range(50):
            try:
                return self.room.command(name, self.room.state()["version"], payload)
            except DomainError as exc:
                if exc.code != "VERSION_CONFLICT":
                    raise
                time.sleep(0.02)
        raise AssertionError("version conflict did not settle")

    def runs(self, ids):
        by = {r["id"]: r for r in self.room.state()["runs"]}
        return [by[i] for i in ids]

    def wait(self, ids, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            rs = self.runs(ids)
            if all(r["status"] in TERMINAL for r in rs):
                return rs
            time.sleep(0.05)
        raise AssertionError("runs did not finish: " + str([(r["provider"], r["status"]) for r in self.runs(ids)]))

    def received(self, run):
        resp = run["response"] or ""
        part = [x for x in resp.split() if x.startswith("received_sha256=")]
        return part[0].split("=", 1)[1] if part else None

    def journals(self):
        root = Path(self.tmp.name, "transport-journal")
        return sorted(p.name for p in root.glob("*.jsonl")) if root.exists() else []


class ChatFlowTests(ChatBase):
    def add_context(self):
        self.cmd("add_instruction", text="답은 한국어로 짧게", kind="instruction", scope="project", phase_id=None, supersedes=[])
        st = self.room.state()
        return [("instruction", "답은 한국어로 짧게")], st["project"]["instruction_version"]

    def test_three_providers_succeed_with_same_context(self):
        ctx, ver = self.add_context()
        q = "쿠폰 값을 0..10으로 자른 뒤 합하면 [-2, 3, 14]의 합은?"
        r = self.cmd("chat_send", text=q, providers=["codex", "claude", "antigravity"], submission_id="ask-three-0001")
        runs = self.wait(r["run_ids"])
        turn = next(t for t in self.room.state()["chat_turns"] if t["id"] == r["turn_id"])
        prompt = expected_ask_prompt(q, ctx, ver)
        self.assertEqual(["succeeded"] * 3, [x["status"] for x in runs])
        self.assertEqual(["codex", "claude", "antigravity"], [x["provider"] for x in runs])
        self.assertEqual(["ChatGPT", "Claude", "Gemini"], [x["display_name"] for x in turn["runs"]])
        for run in runs:
            self.assertEqual(sha(prompt), self.received(run), run["provider"] + " 하위 프로세스가 받은 입력")
            self.assertEqual("synthetic-" + run["provider"] + "-fixture", run["reported_model"])
            self.assertIsNone(run["requested_model"])
            self.assertEqual("synthetic_fixture", run["provenance"])
            self.assertIn("provider=" + run["provider"], run["response"])
        self.assertEqual(1, len({x["input_sha256"] for x in runs}))
        self.assertEqual(sha(prompt), runs[0]["input_sha256"])

    def test_one_timeout_one_failure_do_not_block_others(self):
        r = self.cmd("chat_send", text="부분 실패 검사", providers=["codex", "claude", "antigravity"], submission_id="ask-partial-01",
                     fixture_scenarios={"claude": "timeout", "antigravity": "nonzero_exit"})
        t0 = time.time()
        codex = None
        while time.time() - t0 < 10:
            codex = self.runs([r["run_ids"][0]])[0]
            if codex["status"] == "succeeded":
                break
            time.sleep(0.05)
        codex_done_at = time.time() - t0
        runs = self.wait(r["run_ids"])
        by = {x["provider"]: x for x in runs}
        self.assertEqual("succeeded", by["codex"]["status"])
        self.assertLess(codex_done_at, 4.0, "시간 초과 제공자를 기다리지 않고 먼저 완료돼야 함")
        self.assertEqual("timed_out", by["claude"]["status"])
        self.assertFalse(by["claude"]["response"], "실패를 응답 문장으로 대체하지 않음")
        self.assertIn(by["antigravity"]["status"], ("failed",))
        self.assertFalse(by["antigravity"]["response"])
        shown = {x["provider"]: x for t in self.room.state()["chat_turns"] if t["id"] == r["turn_id"] for x in t["runs"]}
        self.assertIsNone(shown["claude"]["response"]); self.assertIsNone(shown["antigravity"]["response"])
        self.assertIn("provider=codex", shown["codex"]["response"])
        self.assertTrue(by["claude"]["failure"] and by["antigravity"]["failure"])
        self.assertIn("provider=codex", by["codex"]["response"])

    def test_review_transfers_exact_question_answer_context_only(self):
        ctx, ver = self.add_context()
        q = "이 함수의 경계값 처리가 맞는지 봐 줘"
        r = self.cmd("chat_send", text=q, providers=["codex", "claude", "antigravity"], submission_id="ask-review-001")
        src, other1, other2 = self.wait(r["run_ids"])
        rv = self.cmd("chat_review", source_run_id=src["id"], reviewer="claude", submission_id="review-0000001")
        (review,) = self.wait(rv["run_ids"])
        prompt = expected_review_prompt(q, src["response"], "ChatGPT", ctx, ver)
        self.assertEqual("succeeded", review["status"])
        self.assertEqual(sha(prompt), self.received(review), "검토 제공자가 받은 입력 = 원래 질문·선택 답변·공통 맥락")
        plan = next(p for p in self.room.state()["execution_plans"] if p["run_id"] == review["id"])
        self.assertEqual(prompt, plan["request"]["input_text"])
        self.assertIn(src["response"], prompt)
        self.assertNotIn(other1["response"], plan["request"]["input_text"])
        self.assertNotIn(other2["response"], plan["request"]["input_text"])
        turn = next(t for t in self.room.state()["chat_turns"] if t["id"] == rv["turn_id"])
        self.assertEqual("review", turn["kind"])
        self.assertEqual(src["id"], turn["review_of"]["run_id"])
        self.assertEqual(r["turn_id"], turn["review_of"]["root_turn_id"])
        self.assertEqual(sha(src["response"]), turn["review_of"]["answer_sha256"])
        event = next(e for e in self.room.state()["events"] if e["type"] == "chat_turn_submitted" and e["payload"].get("chat_turn_id") == rv["turn_id"])
        self.assertIn(src["id"], event["references"])
        self.assertIn(src["response_event_id"], event["references"])

    def test_review_rules(self):
        r = self.cmd("chat_send", text="규칙 검사", providers=["codex", "claude"], submission_id="ask-rules-0001", fixture_scenarios={"claude": "nonzero_exit"})
        codex, claude = self.wait(r["run_ids"])
        with self.assertRaises(DomainError) as same:
            self.cmd("chat_review", source_run_id=codex["id"], reviewer="codex", submission_id="review-same-01")
        self.assertEqual("SAME_PROVIDER_REVIEW", same.exception.code)
        with self.assertRaises(DomainError) as failed:
            self.cmd("chat_review", source_run_id=claude["id"], reviewer="antigravity", submission_id="review-fail-01")
        self.assertEqual("ANSWER_NOT_AVAILABLE", failed.exception.code)

    def test_intentional_repeat_vs_resend_of_same_submission(self):
        a = self.cmd("chat_send", text="같은 문장", providers=["codex", "claude"], submission_id="repeat-0000001")
        b = self.cmd("chat_send", text="같은 문장", providers=["codex", "claude"], submission_id="repeat-0000002")
        self.wait(a["run_ids"] + b["run_ids"])
        v = self.room.state()["version"]
        j = self.journals()
        resend = self.cmd("chat_send", text="같은 문장", providers=["codex", "claude"], submission_id="repeat-0000001")
        st = self.room.state()
        self.assertNotEqual(a["turn_id"], b["turn_id"])
        self.assertEqual(4, len(set(a["run_ids"] + b["run_ids"])))
        self.assertTrue(resend["duplicate"])
        self.assertEqual(a["turn_id"], resend["turn_id"])
        self.assertEqual(v, st["version"], "재전송은 새 이벤트를 만들지 않음")
        self.assertEqual(2, len([t for t in st["chat_turns"] if t["text"] == "같은 문장"]))
        self.assertEqual(j, self.journals(), "재전송은 새 하위 프로세스를 띄우지 않음")
        with self.assertRaises(DomainError) as reused:
            self.cmd("chat_send", text="다른 문장", providers=["codex", "claude"], submission_id="repeat-0000001")
        self.assertEqual("SUBMISSION_ID_REUSED", reused.exception.code)

    def test_cancel_then_late_response_stays_cancelled(self):
        r = self.cmd("chat_send", text="취소 검사", providers=["codex", "claude"], submission_id="cancel-0000001", fixture_scenarios={"claude": "wait_for_cancel"})
        claude_id = r["run_ids"][1]
        end = time.time() + 5
        while self.runs([claude_id])[0]["status"] != "running" and time.time() < end:
            time.sleep(0.02)
        # 제공자가 취소 신호를 받지 못하고 나중에 성공 응답을 돌려보내는 상황: 워커의 취소 신호를 떼어 두고 실제 기록 함수로 늦은 결과를 전달한다.
        real_signal = self.room._cancel_events[claude_id]
        self.room._cancel_events[claude_id] = threading.Event()
        self.cmd("cancel_run", run_id=claude_id)
        self.assertEqual("cancel_requested", self.runs([claude_id])[0]["status"])
        late = {"status": "succeeded", "outcome": "succeeded", "response": "늦게 도착한 정상 응답", "finished_at": "2026-10-27T00:00:00Z"}
        self.assertTrue(self.room._record_fixture_update(claude_id, late))
        run = self.runs([claude_id])[0]
        self.assertEqual("cancelled", run["status"])
        self.assertIsNone(run["response"])
        self.assertTrue(run["late_response_ignored"])
        self.assertEqual("늦게 도착한 정상 응답", run["late_receipt"]["response"])
        real_signal.set()
        self.wait(r["run_ids"])
        run = self.runs([claude_id])[0]
        self.assertEqual("cancelled", run["status"], "워커의 이후 결과도 취소를 되돌리지 않음")
        self.assertEqual("succeeded", self.runs([r["run_ids"][0]])[0]["status"])

    def test_live_pending_prepares_without_execution(self):
        r = self.cmd("chat_send", text="실제 요청 준비", providers=["codex", "claude", "antigravity"], mode="live_pending", submission_id="live-000000001",
                     models={"claude": "사용자가 지정한 모델 문자열"})
        st = self.room.state()
        runs = self.runs(r["run_ids"])
        self.assertEqual(["awaiting_approval"] * 3, [x["status"] for x in runs])
        self.assertEqual([], self.journals(), "준비 단계는 전송 기록·프로세스를 만들지 않음")
        self.assertEqual({}, {k: v for k, v in self.room._workers.items() if v.is_alive()})
        plans = [p for p in st["execution_plans"] if p["run_id"] in r["run_ids"]]
        self.assertTrue(all(p["authorization"]["state"] == "not_granted" for p in plans))
        self.assertEqual("사용자가 지정한 모델 문자열", runs[1]["requested_model"])
        self.assertIsNone(runs[0]["requested_model"])
        with self.assertRaises(DomainError) as blocked:
            self.cmd("invoke_provider", run_id=r["run_ids"][0])
        self.assertEqual("EXTERNAL_EXECUTION_NOT_AUTHORIZED", blocked.exception.code)
        self.cmd("cancel_run", run_id=r["run_ids"][0])
        self.assertEqual("cancelled", self.runs([r["run_ids"][0]])[0]["status"])

    def test_capacity_rejects_whole_request_without_partial_runs(self):
        a = self.cmd("chat_send", text="오래 걸림 1", providers=["codex", "claude", "antigravity"], submission_id="cap-000000001",
                     fixture_scenarios={"codex": "wait_for_cancel", "claude": "wait_for_cancel", "antigravity": "wait_for_cancel"})
        b = self.cmd("chat_send", text="오래 걸림 2", providers=["codex", "claude", "antigravity"], submission_id="cap-000000002",
                     fixture_scenarios={"codex": "wait_for_cancel", "claude": "wait_for_cancel", "antigravity": "wait_for_cancel"})
        n = len(self.room.state()["runs"])
        with self.assertRaises(DomainError) as full:
            self.cmd("chat_send", text="넘침", providers=["codex"], submission_id="cap-000000003")
        self.assertEqual("RUN_LIMIT", full.exception.code)
        self.assertEqual(n, len(self.room.state()["runs"]))
        for rid in a["run_ids"] + b["run_ids"]:
            self.cmd("cancel_run", run_id=rid)
        rs = self.wait(a["run_ids"] + b["run_ids"])
        self.assertTrue(all(x["status"] == "cancelled" for x in rs))


class ChatRestoreTests(unittest.TestCase):
    def test_restart_restores_conversation_without_resending(self):
        tmp = tempfile.TemporaryDirectory(); copy_dir = tempfile.TemporaryDirectory()
        room = Workroom(tmp.name)
        try:
            v = room.state()["version"]
            r = room.command("chat_send", v, {"text": "복원 검사", "providers": ["codex", "claude"], "submission_id": "restore-000001",
                                              "fixture_scenarios": {"claude": "wait_for_cancel"}})
            end = time.time() + 10
            while time.time() < end:
                rs = {x["id"]: x for x in room.state()["runs"]}
                if rs[r["run_ids"][0]]["status"] == "succeeded" and rs[r["run_ids"][1]]["status"] == "running":
                    break
                time.sleep(0.05)
            # 실행 중 상태 그대로 저장소를 복사 = 비정상 종료 뒤 재시작
            shutil.copy(Path(tmp.name, "events.jsonl"), Path(copy_dir.name, "events.jsonl"))
            shutil.copytree(Path(tmp.name, "transport-journal"), Path(copy_dir.name, "transport-journal"))
        finally:
            room.close(); tmp.cleanup()
        journals_before = sorted(p.name for p in Path(copy_dir.name, "transport-journal").glob("*.jsonl"))
        restored = Workroom(copy_dir.name)
        try:
            st = restored.state()
            turn = next(t for t in st["chat_turns"] if t["id"] == r["turn_id"])
            by = {x["provider"]: x for x in turn["runs"]}
            self.assertEqual("succeeded", by["codex"]["status"])
            self.assertIn("provider=codex", by["codex"]["response"])
            self.assertTrue(by["codex"]["stored_before_session"])
            self.assertIn(by["claude"]["status"], ("disconnected", "uncertain"))
            self.assertIsNone(by["claude"]["response"])
            run = next(x for x in st["runs"] if x["id"] == by["claude"]["run_id"])
            self.assertIn("not be resent", run["error"])
            self.assertEqual({}, {k: w for k, w in restored._workers.items() if w.is_alive()}, "재시작이 요청을 다시 보내지 않음")
            self.assertEqual(journals_before, sorted(p.name for p in Path(copy_dir.name, "transport-journal").glob("*.jsonl")))
            # 재시작 뒤 새 합성 응답은 '이전 세션 저장' 표시가 없음
            v = st["version"]
            n = restored.command("chat_send", v, {"text": "새 세션 질문", "providers": ["codex"], "submission_id": "restore-000002"})
            end = time.time() + 10
            while time.time() < end and next(x for x in restored.state()["runs"] if x["id"] == n["run_ids"][0])["status"] not in ("succeeded", "failed"):
                time.sleep(0.05)
            new_turn = next(t for t in restored.state()["chat_turns"] if t["id"] == n["turn_id"])
            self.assertFalse(new_turn["runs"][0]["stored_before_session"])
        finally:
            restored.close(); copy_dir.cleanup()

    def test_historical_record_and_synthetic_answer_are_distinguished(self):
        tmp = tempfile.TemporaryDirectory()
        registry = json.loads((HERE / "HISTORICAL-SOURCES.json").read_text(encoding="utf-8-sig"))
        entry = registry["argon-coupon-past"]
        entry["path"] = str(HERE / "historical" / "argon-coupon-audit.ndjson")
        reg_path = Path(tmp.name, "registry.json"); reg_path.write_text(json.dumps(registry), encoding="utf-8")
        room = Workroom(Path(tmp.name, "data"), history_registry_path=reg_path)
        try:
            room.command("import_history", room.state()["version"], {"source_id": "argon-coupon-past"})
            r = room.command("chat_send", room.state()["version"], {"text": "새 질문", "providers": ["antigravity"], "submission_id": "hist-00000001"})
            end = time.time() + 10
            while time.time() < end and next(x for x in room.state()["runs"] if x["id"] == r["run_ids"][0])["status"] != "succeeded":
                time.sleep(0.05)
            st = room.state()
            hist = [x for x in st["runs"] if x.get("run_kind") == "historical_import"]
            chat = [x for x in st["runs"] if x.get("chat_turn_id")]
            self.assertEqual(1, len(hist)); self.assertEqual(1, len(chat))
            self.assertEqual("historical_external_execution", hist[0]["provenance"])
            self.assertIsNone(hist[0].get("chat_turn_id"))
            self.assertEqual("synthetic_fixture", chat[0]["provenance"])
            self.assertEqual("synthetic", chat[0]["mode"])
            self.assertTrue(all(run["run_id"] != hist[0]["id"] for t in st["chat_turns"] for run in t["runs"]), "과거 기록은 채팅 응답으로 섞이지 않음")
        finally:
            room.close(); tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
