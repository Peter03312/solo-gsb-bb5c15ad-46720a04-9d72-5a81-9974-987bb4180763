#!/usr/bin/env bash
# 批次联合排课端到端调用示例：
#   1) 给 CS401 在 2027-FALL 只留 1 个名额；
#   2) 一次提交两份完全相同的普通课表申请（标识 app-2026-001、app-2026-002），
#      两份在联合排课下一份排入 2027-FALL、另一份顺延 2028-FALL（均获得完整课表）；
#   3) 再提交一份期限内无解的申请，演示“未入选原因”；
#   4) 整批确认（单事务原子复核目录版本与整批席位，一并占位，重复确认幂等）；
#   5) 回看批次与成员课表。
set -euo pipefail
BASE="${BASE:-http://localhost:8000}"

echo "== 1. 设置 CS401@2027-FALL 席位上限 1 =="
curl -sS -X PUT "$BASE/seat-caps/CS401/2027-FALL" \
  -H 'Content-Type: application/json' -d '{"cap": 1}'
echo

echo "== 2. 一次提交两份普通课表申请（联合排课）=="
BATCH_JSON=$(curl -sS -X POST "$BASE/plan-batches" \
  -H 'Content-Type: application/json' -d '{
    "applications": [
      {"application_id": "app-2026-002",
       "completed_courses": [], "target_courses": ["CS401"],
       "start_semester": "2026-SPRING",
       "max_credits_per_semester": 12, "num_semesters": 6},
      {"application_id": "app-2026-001",
       "completed_courses": [], "target_courses": ["CS401"],
       "start_semester": "2026-SPRING",
       "max_credits_per_semester": 12, "num_semesters": 6}
    ]
  }')
echo "$BATCH_JSON" | python3 -m json.tool
BATCH_ID=$(echo "$BATCH_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["batch_id"])')
echo "batch_id=$BATCH_ID（待确认，不预占席位）"

echo "== 3. 标识重复：整批拒绝（422，不保存）=="
curl -sS -X POST "$BASE/plan-batches" -H 'Content-Type: application/json' -d '{
  "applications": [
    {"application_id": "dup", "target_courses": ["CS401"], "start_semester": "2026-SPRING",
     "max_credits_per_semester": 12, "num_semesters": 6},
    {"application_id": "dup", "target_courses": ["CS401"], "start_semester": "2026-SPRING",
     "max_credits_per_semester": 12, "num_semesters": 6}
  ]}'
echo

echo "== 4. 无一可排：422 NO_SOLUTION，不保存批次 =="
curl -sS -X POST "$BASE/plan-batches" -H 'Content-Type: application/json' -d '{
  "applications": [
    {"application_id": "impossible", "target_courses": ["CS401"],
     "start_semester": "2026-SPRING",
     "max_credits_per_semester": 12, "num_semesters": 1}
  ]}'
echo

echo "== 5. 成员课表不得单独确认：409 BATCH_CONFIRM_REQUIRED =="
PLAN_ID=$(echo "$BATCH_JSON" | python3 -c '
import sys,json
ms=json.load(sys.stdin)["applications"]
print(next(m["plan_id"] for m in ms if m["selected"]))')
curl -sS -X POST "$BASE/plans/$PLAN_ID/confirm"
echo

echo "== 6. 整批确认（原子复核目录版本与整批席位，一并占位）=="
curl -sS -X POST "$BASE/plan-batches/$BATCH_ID/confirm" | python3 -m json.tool

echo "== 7. 重复确认幂等 =="
curl -sS -X POST "$BASE/plan-batches/$BATCH_ID/confirm" \
  | python3 -c 'import sys,json;print("status =", json.load(sys.stdin)["status"])'

echo "== 8. 回看批次 =="
curl -sS "$BASE/plan-batches/$BATCH_ID" | python3 -m json.tool
