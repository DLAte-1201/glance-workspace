#!/usr/bin/env bash
# Glance 통합 채팅 독립 회귀 검사 실행 스크립트 (Claude fix1 적용 전·후 동일 명령으로 검증)
# 사용법: Glance 작업 사본 루트(workroom.py가 있는 폴더)에 test_independent_chat_regression.py와
#        ui_independent_regression.test.cjs를 복사한 뒤 아래 명령을 실행하세요.
set -u
echo "[1/2] Python 백엔드·HTTP·자식 프로세스 독립 회귀 검사 실행..."
python3 -m unittest -v test_independent_chat_regression
PY_EXIT=$?

echo "[2/2] Node 프런트엔드(static/app.js) 응답 유실·새로고침 독립 회귀 검사 실행..."
node --test ui_independent_regression.test.cjs
NODE_EXIT=$?

echo "=== 요약: Python exit=${PY_EXIT}, Node exit=${NODE_EXIT} ==="
if [ "$PY_EXIT" -ne 0 ] || [ "$NODE_EXIT" -ne 0 ]; then
  exit 1
fi
exit 0
