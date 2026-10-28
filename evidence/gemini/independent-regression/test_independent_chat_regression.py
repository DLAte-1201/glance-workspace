"""Glance 통합 채팅 독립 회귀 검사 스위트 (Gemini 독립 검증 담당).

- 검사 대상: Claude의 glance-chat-integration.zip 패치 적용본 (제품 소스 수정 없음).
- 검증 방식:
  1. 실제 HTTP 서버(server.LocalServer) 및 실제 Workroom 명령 실행
  2. 실제 로컬 합성 하위 프로세스(fixtures/transport_child.py) 실행 및 stdin SHA-256 해시 대조
  3. 기대 프롬프트 및 맥락 해시는 제품의 Workroom.chat_* 함수를 호출하지 않고 명세에서 독립적으로 구성
  4. 미리 작성한 정적 결과 카드 비교 금지
"""
from __future__ import annotations

import hashlib
import http.client
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from server import LocalServer
from workroom import DomainError, Workroom

TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "disconnected", "timed_out", "interrupted", "uncertain")
PROVIDER_DISPLAY = {"codex": "ChatGPT", "claude": "Claude", "antigravity": "Gemini"}


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def spec_context_digest(instruction_version: int, project_instructions: list[dict]) -> str:
    """명세 기반 공통 맥락 스냅샷 SHA-256 계산 (제품 함수 미사용)."""
    snap = {
        "instruction_version": instruction_version,
        "instructions": [
            {"id": item["id"], "kind": item["kind"], "text": item["text"]}
            for item in project_instructions
        ],
    }
    raw = json.dumps(snap, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def spec_ask_prompt(question: str, instruction_version: int, project_instructions: list[dict]) -> str:
    """명세 기반 질문 프롬프트 독립 조립 (제품 함수 미사용)."""
    header = f"[Glance 공통 맥락 v{instruction_version}]"
    if project_instructions:
        body = "\n".join(f"- ({item['kind']}) {item['text']}" for item in project_instructions)
    else:
        body = "- (없음)"
    return f"{header}\n{body}\n\n[질문]\n{question}"


def spec_review_prompt(
    question: str,
    selected_answer: str,
    source_provider: str,
    instruction_version: int,
    project_instructions: list[dict],
) -> str:
    """명세 기반 교차 검토 프롬프트 독립 조립 (제품 함수 미사용)."""
    header = f"[Glance 공통 맥락 v{instruction_version}]"
    if project_instructions:
        body = "\n".join(f"- ({item['kind']}) {item['text']}" for item in project_instructions)
    else:
        body = "- (없음)"
    display = PROVIDER_DISPLAY[source_provider]
    return (
        f"{header}\n{body}\n\n"
        f"[원래 질문]\n{question}\n\n"
        f"[검토할 답변 · {display}]\n{selected_answer}\n\n"
        f"[요청]\n위 답변을 원래 질문과 공통 맥락에 비추어 검토하고, 틀리거나 빠진 점과 근거를 짧게 적어 주세요."
    )


def extract_child_received_sha256(response_text: str | None) -> str | None:
    """가짜 자식 프로세스(fixtures/transport_child.py)가 실제 stdin에서 계산해 보고한 SHA-256 추출."""
    if not response_text:
        return None
    for token in response_text.split():
        if token.startswith("received_sha256="):
            return token.split("=", 1)[1]
    return None


class IndependentRegressionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="glance-indep-reg-")
        self.reg_path = Path(self.tmp.name, "empty-history-registry.json")
        self.reg_path.write_text("{}", encoding="utf-8")
        self.conn_path = Path(self.tmp.name, "isolated-connections.json")
        self.conn_path.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        self.data_dir = Path(self.tmp.name, "state")
        self.room = Workroom(self.data_dir, connections_path=self.conn_path, history_registry_path=self.reg_path)

    def tearDown(self):
        self.room.close()
        self.tmp.cleanup()

    def cmd(self, command_name: str, **payload):
        for _ in range(60):
            try:
                return self.room.command(command_name, self.room.state()["version"], payload)
            except DomainError as exc:
                if exc.code != "VERSION_CONFLICT":
                    raise
                time.sleep(0.02)
        self.fail("VERSION_CONFLICT did not settle")

    def get_runs(self, run_ids: list[str]) -> list[dict]:
        by_id = {r["id"]: r for r in self.room.state()["runs"]}
        return [by_id[rid] for rid in run_ids]

    def wait_runs(self, run_ids: list[str], timeout: float = 15.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            runs = self.get_runs(run_ids)
            if all(r["status"] in TERMINAL_STATUSES for r in runs):
                return runs
            time.sleep(0.03)
        self.fail(f"Runs did not terminate within {timeout}s: {[(r['provider'], r['status']) for r in self.get_runs(run_ids)]}")

    def journal_files(self) -> list[str]:
        jdir = self.data_dir / "transport-journal"
        return sorted(p.name for p in jdir.glob("*.jsonl")) if jdir.exists() else []


class IndependentChatRegressionTests(IndependentRegressionBase):
    """사용자 지시문 섹션 2의 실패 경로 독립 회귀 검사."""

    # -------------------------------------------------------------------------
    # 경로 1 & 2 (백엔드): 서버 접수 뒤 HTTP 응답 유실 및 새로고침 조회·재시도 멱등성
    # -------------------------------------------------------------------------
    def test_01_http_response_lost_after_server_accept_retry_and_reload_do_not_duplicate(self):
        """서버가 요청을 접수·커밋한 뒤 HTTP 응답이 유실됐을 때,
        (1) 새로고침(GET /api/state)으로 먼저 저장된 submission_id를 조회할 수 있고 자동 재전송이 없으며,
        (2) 동일 submission_id로 재시도해도 실행(runs)·전송(journals)·버전(version)이 늘지 않는다."""
        server = LocalServer(("127.0.0.1", 0), self.room)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_port
        try:
            def http_get_state():
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/api/state")
                resp = conn.getresponse()
                data = json.loads(resp.read())
                conn.close()
                return data

            def http_post_command(cmd_name, version, payload, drop_response=False):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                body = json.dumps({"command": cmd_name, "expected_version": version, "payload": payload})
                headers = {
                    "Content-Type": "application/json",
                    "Origin": f"http://127.0.0.1:{port}",
                    "X-Workroom-Token": server.session_token,
                }
                conn.request("POST", "/api/commands", body=body, headers=headers)
                if drop_response:
                    conn.close()
                    return None
                resp = conn.getresponse()
                raw = json.loads(resp.read())
                status = resp.status
                conn.close()
                return status, raw

            st0 = http_get_state()
            sub_id = "lost-http-resp-001"
            payload = {
                "text": "HTTP 응답 유실 후 재확인 질문",
                "providers": ["codex", "claude"],
                "submission_id": sub_id,
                "mode": "synthetic",
            }
            http_post_command("chat_send", st0["version"], payload, drop_response=True)

            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                st_now = http_get_state()
                turns = [t for t in st_now["chat_turns"] if t["submission_id"] == sub_id]
                if turns and all(r["status"] == "succeeded" for r in turns[0]["runs"]):
                    break
                time.sleep(0.05)

            st_reloaded = http_get_state()
            saved_turns = [t for t in st_reloaded["chat_turns"] if t["submission_id"] == sub_id]
            self.assertEqual(1, len(saved_turns))
            run_ids_after_accept = saved_turns[0]["run_ids"]
            self.assertEqual(2, len(run_ids_after_accept))
            journals_after_accept = self.journal_files()
            self.assertEqual(2, len(journals_after_accept))
            version_after_accept = st_reloaded["version"]

            status_retry, body_retry = http_post_command("chat_send", version_after_accept, payload, drop_response=False)
            self.assertEqual(200, status_retry)
            self.assertTrue(body_retry["duplicate"])
            self.assertTrue(body_retry["unchanged"])
            self.assertEqual(run_ids_after_accept, body_retry["run_ids"])
            self.assertEqual(version_after_accept, http_get_state()["version"])
            self.assertEqual(journals_after_accept, self.journal_files())
            self.assertEqual(2, len(http_get_state()["runs"]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    # -------------------------------------------------------------------------
    # 경로 3: 같은 문장을 명시적으로 새 질문으로 제출하면 별도 실행 보존
    # -------------------------------------------------------------------------
    def test_02_explicit_new_submission_with_identical_text_preserves_separate_runs(self):
        """같은 문장을 명시적으로 새 질문(새 submission_id)으로 제출하면 별도 턴과 별도 하위 프로세스 실행이 보존된다."""
        q = "동일한 질문 문장입니다. 독립 실행이 보존되어야 합니다."
        r1 = self.cmd("chat_send", text=q, providers=["codex", "antigravity"], submission_id="explicit-new-0001")
        r2 = self.cmd("chat_send", text=q, providers=["codex", "antigravity"], submission_id="explicit-new-0002")
        self.assertNotEqual(r1["turn_id"], r2["turn_id"])
        self.assertEqual(4, len(set(r1["run_ids"] + r2["run_ids"])))

        runs = self.wait_runs(r1["run_ids"] + r2["run_ids"])
        self.assertEqual(["succeeded"] * 4, [r["status"] for r in runs])
        self.assertEqual(4, len(self.journal_files()))

        expected_prompt = spec_ask_prompt(q, 0, [])
        for r in runs:
            self.assertEqual(sha256_hex(expected_prompt), extract_child_received_sha256(r["response"]))

    # -------------------------------------------------------------------------
    # 경로 4A: 같은 submission_id에서 질문·제공자·실행 방식·검토 원본 변경 거절 (현재 통과 범위)
    # -------------------------------------------------------------------------
    def test_03_same_submission_id_rejects_text_provider_ask_mode_and_review_source_mutation(self):
        """같은 submission_id에서 질문·제공자·질문 mode·검토 원본(source_run_id)·검토자(reviewer)를 바꾸면
        SUBMISSION_ID_REUSED로 거절된다."""
        sid = "idemp-mutate-ask-01"
        r = self.cmd("chat_send", text="원본 질문", providers=["codex", "claude"], mode="synthetic", submission_id=sid)
        codex_run, claude_run = self.wait_runs(r["run_ids"])

        with self.assertRaises(DomainError) as e_text:
            self.cmd("chat_send", text="바뀐 질문", providers=["codex", "claude"], mode="synthetic", submission_id=sid)
        self.assertEqual("SUBMISSION_ID_REUSED", e_text.exception.code)

        with self.assertRaises(DomainError) as e_prov:
            self.cmd("chat_send", text="원본 질문", providers=["codex"], mode="synthetic", submission_id=sid)
        self.assertEqual("SUBMISSION_ID_REUSED", e_prov.exception.code)

        with self.assertRaises(DomainError) as e_mode:
            self.cmd("chat_send", text="원본 질문", providers=["codex", "claude"], mode="live_pending", submission_id=sid)
        self.assertEqual("SUBMISSION_ID_REUSED", e_mode.exception.code)

        with self.assertRaises(DomainError) as e_kind:
            self.cmd("chat_review", source_run_id=codex_run["id"], reviewer="antigravity", submission_id=sid)
        self.assertEqual("SUBMISSION_ID_REUSED", e_kind.exception.code)

        rev_sid = "idemp-mutate-rev-01"
        rv = self.cmd("chat_review", source_run_id=codex_run["id"], reviewer="antigravity", submission_id=rev_sid)
        self.wait_runs(rv["run_ids"])

        # 같은 rev_sid에서 검토 원본(source_run_id) 변경 시 거절
        with self.assertRaises(DomainError) as e_src:
            self.cmd("chat_review", source_run_id=claude_run["id"], reviewer="antigravity", submission_id=rev_sid)
        self.assertEqual("SUBMISSION_ID_REUSED", e_src.exception.code)

        # 같은 rev_sid에서 검토자(reviewer) 변경 시 거절
        with self.assertRaises(DomainError) as e_rev:
            self.cmd("chat_review", source_run_id=codex_run["id"], reviewer="claude", submission_id=rev_sid)
        self.assertEqual("SUBMISSION_ID_REUSED", e_rev.exception.code)

    # -------------------------------------------------------------------------
    # 경로 4B [결함 재현 1]: 같은 submission_id에서 chat_review의 synthetic -> live_pending 변경 거절 검사
    # -------------------------------------------------------------------------
    def test_04_defect_chat_review_same_submission_id_must_reject_mode_change_synthetic_to_live_pending(self):
        """[결함 재현] 검토 요청(chat_review)에서 같은 submission_id로 mode를 synthetic -> live_pending으로 바꾸면
        SUBMISSION_ID_REUSED로 거절되어야 한다."""
        ask = self.cmd("chat_send", text="검토 모드 변경 검사", providers=["codex"], submission_id="ask-for-rev-mode-1")
        (src_run,) = self.wait_runs(ask["run_ids"])

        rev_sid = "rev-mode-change-01"
        rv = self.cmd("chat_review", source_run_id=src_run["id"], reviewer="claude", mode="synthetic", submission_id=rev_sid)
        self.wait_runs(rv["run_ids"])

        with self.assertRaises(DomainError) as ctx:
            self.cmd("chat_review", source_run_id=src_run["id"], reviewer="claude", mode="live_pending", submission_id=rev_sid)
        self.assertEqual("SUBMISSION_ID_REUSED", ctx.exception.code)

    # -------------------------------------------------------------------------
    # 경로 4C [결함 재현 2]: 같은 submission_id에서 모델(models/model) 또는 시나리오(fixture_scenarios/fixture_scenario) 변경 거절 검사
    # -------------------------------------------------------------------------
    def test_05_defect_same_submission_id_must_reject_model_and_scenario_mutation(self):
        """[결함 재현] 같은 submission_id에서 요청 모델(models/model)이나 합성 시나리오(fixture_scenarios/fixture_scenario)를
        바꾸면 SUBMISSION_ID_REUSED로 거절되어야 한다."""
        ask_sid = "ask-param-mutate-1"
        r = self.cmd(
            "chat_send",
            text="모델·시나리오 불변성 검사",
            providers=["codex"],
            mode="synthetic",
            models={"codex": "model-alpha"},
            fixture_scenarios={"codex": "success"},
            submission_id=ask_sid,
        )
        (src_run,) = self.wait_runs(r["run_ids"])

        with self.assertRaises(DomainError) as e_model:
            self.cmd(
                "chat_send",
                text="모델·시나리오 불변성 검사",
                providers=["codex"],
                mode="synthetic",
                models={"codex": "model-beta-changed"},
                fixture_scenarios={"codex": "success"},
                submission_id=ask_sid,
            )
        self.assertEqual("SUBMISSION_ID_REUSED", e_model.exception.code)

        with self.assertRaises(DomainError) as e_scen:
            self.cmd(
                "chat_send",
                text="모델·시나리오 불변성 검사",
                providers=["codex"],
                mode="synthetic",
                models={"codex": "model-alpha"},
                fixture_scenarios={"codex": "nonzero_exit"},
                submission_id=ask_sid,
            )
        self.assertEqual("SUBMISSION_ID_REUSED", e_scen.exception.code)

        rev_sid = "rev-param-mutate-1"
        rv = self.cmd(
            "chat_review",
            source_run_id=src_run["id"],
            reviewer="claude",
            mode="synthetic",
            model="claude-model-v1",
            fixture_scenario="success",
            submission_id=rev_sid,
        )
        self.wait_runs(rv["run_ids"])

        with self.assertRaises(DomainError) as e_rev_model:
            self.cmd(
                "chat_review",
                source_run_id=src_run["id"],
                reviewer="claude",
                mode="synthetic",
                model="claude-model-v2-changed",
                fixture_scenario="success",
                submission_id=rev_sid,
            )
        self.assertEqual("SUBMISSION_ID_REUSED", e_rev_model.exception.code)

        with self.assertRaises(DomainError) as e_rev_scen:
            self.cmd(
                "chat_review",
                source_run_id=src_run["id"],
                reviewer="claude",
                mode="synthetic",
                model="claude-model-v1",
                fixture_scenario="nonzero_exit",
                submission_id=rev_sid,
            )
        self.assertEqual("SUBMISSION_ID_REUSED", e_rev_scen.exception.code)

    # -------------------------------------------------------------------------
    # 경로 5A: 재시도 중 최신 지시가 바뀌어도 원요청 맥락 유지 + 제공자 별칭 정규화 비교
    # -------------------------------------------------------------------------
    def test_06_retry_after_instruction_change_preserves_original_context_and_provider_aliases_normalize(self):
        """제공자 별칭(chatgpt/codex_dot -> codex, gemini -> antigravity)과 기본 mode('synthetic')는 정규화 규칙에 맞게
        동일 요청으로 인정되며, 재시도 전에 프로젝트 최신 지시가 바뀌어도 원래 저장된 요청 맥락은 변하지 않는다."""
        self.cmd("add_instruction", text="초기 지시 v1", kind="instruction", scope="project", phase_id=None, supersedes=[])
        st_v1 = self.room.state()
        proj_inst_v1 = [i for i in st_v1["effective_instructions"] if i["scope"] == "project"]
        expected_ctx_sha_v1 = spec_context_digest(1, proj_inst_v1)
        expected_prompt_v1 = spec_ask_prompt("맥락 고정 질문", 1, proj_inst_v1)

        sid = "alias-and-ctx-0001"
        r1 = self.cmd("chat_send", text="맥락 고정 질문", providers=["chatgpt", "gemini"], submission_id=sid)
        runs = self.wait_runs(r1["run_ids"])

        self.cmd("add_instruction", text="나중에 추가된 수정 지시 v2", kind="correction", scope="project", phase_id=None, supersedes=[])
        self.assertEqual(2, self.room.state()["project"]["instruction_version"])

        retry = self.cmd(
            "chat_send",
            text="맥락 고정 질문",
            providers=["codex", "antigravity"],
            mode="synthetic",
            models={"codex": None},
            fixture_scenarios={"codex": "success", "antigravity": "success"},
            submission_id=sid,
        )
        self.assertTrue(retry["duplicate"])
        self.assertTrue(retry["unchanged"])

        turn = next(t for t in self.room.state()["chat_turns"] if t["id"] == r1["turn_id"])
        self.assertEqual(1, turn["context_snapshot"]["instruction_version"])
        self.assertEqual(expected_ctx_sha_v1, turn["context_sha256"])
        for r in runs:
            self.assertEqual(sha256_hex(expected_prompt_v1), extract_child_received_sha256(r["response"]))

    # -------------------------------------------------------------------------
    # 경로 5B [결함 재현 3]: fixture_scenarios 내 제공자 별칭 키(chatgpt, codex_dot, gemini) 정규화 누락 검사
    # -------------------------------------------------------------------------
    def test_07_defect_fixture_scenarios_provider_and_scenario_aliases_must_normalize_onto_run_records(self):
        """[결함 재현] (1) providers와 models에서 별칭(chatgpt, codex_dot, gemini)을 codex/antigravity로 정규화하듯
        fixture_scenarios의 제공자 별칭 키({chatgpt: nonzero_exit})도 정규화되어야 하며,
        (2) 시나리오 별칭(error -> nonzero_exit) 전달 시 run[scenario]에도 정규화된 정본 이름(nonzero_exit)이 기록되어야 한다."""
        r_scen_alias = self.cmd(
            "chat_send",
            text="시나리오 값 별칭 정규화 검사",
            providers=["codex"],
            fixture_scenarios={"codex": "error"},
            submission_id="alias-scen-val-01",
        )
        (codex_alias_run,) = self.wait_runs(r_scen_alias["run_ids"])
        self.assertEqual("failed", codex_alias_run["status"])
        self.assertEqual("nonzero_exit", codex_alias_run["scenario"])

        r_prov_alias = self.cmd(
            "chat_send",
            text="시나리오 제공자 별칭 키 정규화 검사",
            providers=["chatgpt", "gemini"],
            models={"chatgpt": "gpt-custom-model"},
            fixture_scenarios={"chatgpt": "nonzero_exit", "gemini": "success"},
            submission_id="alias-scen-prov-01",
        )
        runs = {x["provider"]: x for x in self.wait_runs(r_prov_alias["run_ids"])}
        self.assertEqual("gpt-custom-model", runs["codex"]["requested_model"])
        self.assertEqual("nonzero_exit", runs["codex"]["scenario"])
        self.assertEqual("failed", runs["codex"]["status"])
        self.assertEqual("succeeded", runs["antigravity"]["status"])

    # -------------------------------------------------------------------------
    # 경로 6: 교차 검토의 원답변·원질문·저장 맥락 고정 및 다른 메시지/단계 바꿔치기·실패 응답 차단
    # -------------------------------------------------------------------------
    def test_08_cross_review_uses_only_saved_question_answer_context_and_rejects_tampering(self):
        """교차 검토는 성공한 특정 실행의 원답변·원질문·그 질문 시점의 저장 맥락만 사용하며,
        나중에 추가된 지시나 다른 채팅 메시지·다른 단계 실행(start_fixture_run/prepare_run/import_history)·실패 응답으로
        참조를 바꿔치기할 수 없다."""
        self.cmd("add_instruction", text="초기 프로젝트 규칙 A", kind="instruction", scope="project", phase_id=None, supersedes=[])
        st_initial = self.room.state()
        snap_initial_items = [i for i in st_initial["effective_instructions"] if i["scope"] == "project"]

        self.cmd("send_message", text="무관한 일반 메시지 (교차 검토에 포함되면 안 됨)", agent_id="codex")
        other_ask = self.cmd("chat_send", text="다른 질문 턴의 문장", providers=["codex"], submission_id="other-ask-turn-01")
        self.wait_runs(other_ask["run_ids"])

        target_q = "대상 질문: 결제 취소 시 상태 전이 규칙은?"
        ask = self.cmd(
            "chat_send",
            text=target_q,
            providers=["codex", "claude", "antigravity"],
            fixture_scenarios={"claude": "nonzero_exit"},
            submission_id="target-ask-turn-01",
        )
        runs_by_p = {r["provider"]: r for r in self.wait_runs(ask["run_ids"])}
        codex_run = runs_by_p["codex"]
        claude_failed_run = runs_by_p["claude"]
        agy_run = runs_by_p["antigravity"]

        self.cmd("add_instruction", text="질문 이후 추가된 후속 지시 B (원질문 맥락 아님)", kind="correction", scope="project", phase_id=None, supersedes=[])

        rv = self.cmd("chat_review", source_run_id=codex_run["id"], reviewer="antigravity", submission_id="cross-rev-valid-01")
        (rev_run,) = self.wait_runs(rv["run_ids"])
        self.assertEqual("succeeded", rev_run["status"])

        expected_prompt = spec_review_prompt(
            question=target_q,
            selected_answer=codex_run["response"],
            source_provider="codex",
            instruction_version=1,
            project_instructions=snap_initial_items,
        )
        self.assertEqual(sha256_hex(expected_prompt), extract_child_received_sha256(rev_run["response"]))
        plan = next(p for p in self.room.state()["execution_plans"] if p["run_id"] == rev_run["id"])
        self.assertNotIn("질문 이후 추가된 후속 지시 B", plan["request"]["input_text"])
        self.assertNotIn("무관한 일반 메시지", plan["request"]["input_text"])
        self.assertNotIn("다른 질문 턴의 문장", plan["request"]["input_text"])
        self.assertNotIn(agy_run["response"], plan["request"]["input_text"])

        with self.assertRaises(DomainError) as err_fail:
            self.cmd("chat_review", source_run_id=claude_failed_run["id"], reviewer="antigravity", submission_id="cross-rev-bad-fail")
        self.assertEqual("ANSWER_NOT_AVAILABLE", err_fail.exception.code)

        with self.assertRaises(DomainError) as err_same:
            self.cmd("chat_review", source_run_id=codex_run["id"], reviewer="codex", submission_id="cross-rev-bad-same")
        self.assertEqual("SAME_PROVIDER_REVIEW", err_same.exception.code)

        non_chat = self.cmd("start_fixture_run", scenario="success", phase_id=None)
        self.wait_runs([non_chat["run_id"]])
        with self.assertRaises(DomainError) as err_nonchat:
            self.cmd("chat_review", source_run_id=non_chat["run_id"], reviewer="claude", submission_id="cross-rev-bad-nonchat")
        self.assertEqual("NOT_A_CHAT_ANSWER", err_nonchat.exception.code)

        live_ask = self.cmd("chat_send", text="승인 대기 질문", providers=["codex"], mode="live_pending", submission_id="live-ask-for-rev-1")
        with self.assertRaises(DomainError) as err_live:
            self.cmd("chat_review", source_run_id=live_ask["run_ids"][0], reviewer="claude", submission_id="cross-rev-bad-live")
        self.assertEqual("ANSWER_NOT_AVAILABLE", err_live.exception.code)

        with self.assertRaises(DomainError) as err_inject:
            self.cmd(
                "chat_review",
                source_run_id=codex_run["id"],
                reviewer="claude",
                text="조작된 질문",
                submission_id="cross-rev-bad-inject",
            )
        self.assertEqual("INVALID_PAYLOAD", err_inject.exception.code)

    # -------------------------------------------------------------------------
    # 경로 6B [결함 재현 4]: 연쇄 교차 검토(Review-of-Review) 시 root_turn 맥락 고정 검사
    # -------------------------------------------------------------------------
    def test_09_defect_chained_cross_review_must_anchor_context_to_root_turn(self):
        """[결함 재현] 교차 검토(review turn)의 완료된 응답을 제3의 제공자에게 다시 검토 맡길 때,
        원질문(root_turn)의 text뿐 아니라 context_snapshot/context_sha256도 항상 root_turn에서 가져와야 한다."""
        self.cmd("add_instruction", text="루트 질문 시점의 유일한 지시", kind="instruction", scope="project", phase_id=None, supersedes=[])
        ask = self.cmd("chat_send", text="루트 질문입니다", providers=["codex"], submission_id="chain-root-ask-01")
        (codex_run,) = self.wait_runs(ask["run_ids"])

        rv1 = self.cmd("chat_review", source_run_id=codex_run["id"], reviewer="claude", submission_id="chain-rev-step-01")
        (claude_rev_run,) = self.wait_runs(rv1["run_ids"])

        for t in self.room._state["chat_turns"]:
            if t["id"] == rv1["turn_id"]:
                t["context_snapshot"] = {"instruction_version": 99, "instructions": [{"id": "tampered", "kind": "correction", "text": "변조된 중간 맥락"}]}
                t["context_sha256"] = "0" * 64

        rv2 = self.cmd("chat_review", source_run_id=claude_rev_run["id"], reviewer="antigravity", submission_id="chain-rev-step-02")
        self.wait_runs(rv2["run_ids"])
        root_turn = next(t for t in self.room.state()["chat_turns"] if t["id"] == ask["turn_id"])
        rev2_turn = next(t for t in self.room.state()["chat_turns"] if t["id"] == rv2["turn_id"])
        self.assertEqual(root_turn["context_sha256"], rev2_turn["context_sha256"])

    # -------------------------------------------------------------------------
    # 경로 7: 취소 후 늦은 성공, 서버 재시작, 제공자 일부 실패가 다른 정상 응답을 훼손하지 않음
    # -------------------------------------------------------------------------
    def test_10_cancel_late_success_server_restart_and_partial_failure_preserve_healthy_responses(self):
        """한 턴 안에서 (1) codex는 정상 완료, (2) claude는 취소 후 늦은 성공 도착, (3) antigravity는 실행 중 서버 재시작(중단)될 때,
        기존 상태와 codex의 정상 응답이 훼손되지 않고 각 실행 상태가 정확히 분리 보존된다."""
        r = self.cmd(
            "chat_send",
            text="복합 장애 격리 질문",
            providers=["codex", "claude", "antigravity"],
            fixture_scenarios={"codex": "success", "claude": "wait_for_cancel", "antigravity": "wait_for_cancel"},
            submission_id="complex-isolation-01",
        )
        codex_id, claude_id, agy_id = r["run_ids"]

        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            runs = {x["id"]: x for x in self.get_runs(r["run_ids"])}
            if (
                runs[codex_id]["status"] == "succeeded"
                and runs[claude_id]["status"] == "running"
                and runs[agy_id]["status"] == "running"
            ):
                break
            time.sleep(0.03)

        codex_before = self.get_runs([codex_id])[0]
        self.assertEqual("succeeded", codex_before["status"])
        expected_prompt = spec_ask_prompt("복합 장애 격리 질문", 0, [])
        self.assertEqual(sha256_hex(expected_prompt), extract_child_received_sha256(codex_before["response"]))

        real_claude_cancel = self.room._cancel_events[claude_id]
        self.room._cancel_events[claude_id] = threading.Event()
        self.cmd("cancel_run", run_id=claude_id)
        self.assertEqual("cancel_requested", self.get_runs([claude_id])[0]["status"])

        late_receipt = {
            "status": "succeeded",
            "outcome": "completed",
            "response": "취소 후 늦게 도착한 응답 원문",
            "finished_at": "2026-10-28T05:00:00Z",
        }
        self.assertTrue(self.room._record_fixture_update(claude_id, late_receipt))
        real_claude_cancel.set()

        claude_after_late = self.get_runs([claude_id])[0]
        self.assertEqual("cancelled", claude_after_late["status"])
        self.assertIsNone(claude_after_late["response"])
        self.assertTrue(claude_after_late["late_response_ignored"])
        self.assertEqual("취소 후 늦게 도착한 응답 원문", claude_after_late["late_receipt"]["response"])

        restart_dir = Path(self.tmp.name, "restarted-state")
        restart_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.data_dir / "events.jsonl", restart_dir / "events.jsonl")
        shutil.copytree(self.data_dir / "transport-journal", restart_dir / "transport-journal")

        self.room._cancel_events[agy_id].set()
        self.wait_runs(r["run_ids"])

        with patch("subprocess.Popen") as spawn_spy:
            restarted_room = Workroom(restart_dir, connections_path=self.conn_path, history_registry_path=self.reg_path)
            spawn_spy.assert_not_called()
        try:
            st_restarted = restarted_room.state()
            turn_restarted = next(t for t in st_restarted["chat_turns"] if t["id"] == r["turn_id"])
            by_prov = {x["provider"]: x for x in turn_restarted["runs"]}

            self.assertEqual("succeeded", by_prov["codex"]["status"])
            self.assertEqual(codex_before["response"], by_prov["codex"]["response"])
            self.assertTrue(by_prov["codex"]["stored_before_session"])

            self.assertEqual("cancelled", by_prov["claude"]["status"])
            self.assertIsNone(by_prov["claude"]["response"])
            self.assertTrue(by_prov["claude"]["late_response_ignored"])

            self.assertEqual("disconnected", by_prov["antigravity"]["status"])
            self.assertIsNone(by_prov["antigravity"]["response"])
        finally:
            restarted_room.close()


if __name__ == "__main__":
    unittest.main()
