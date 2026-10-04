#!/usr/bin/env bash
# 调用示例：先启动服务（docker compose up --build 或 uvicorn app.main:app）
# 用法：bash examples/demo.sh  [默认访问 http://localhost:8000，可用 BASE 覆盖]
BASE="${BASE:-http://localhost:8000}"
HERE="$(cd "$(dirname "$0")" && pwd)"

step() { echo; echo "== $* =="; }

step "1. 健康检查"
curl -s "$BASE/health"; echo

step "2. 当前课程目录（首次启动自动初始化为版本 1）"
curl -s "$BASE/catalog/current"; echo

step "3. 请求规划：目标 编译原理 CS401，2026 春开始，每学期≤12 学分，6 个学期"
PLAN_A=$(curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "completed_courses": [],
  "target_courses": ["CS401"],
  "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12,
  "num_semesters": 6
}')
echo "$PLAN_A"
PLAN_A_ID=$(echo "$PLAN_A" | python3 -c "import sys,json;print(json.load(sys.stdin)['plan_id'])")

step "4. 再规划一个方案 B（暂不确认）"
PLAN_B=$(curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "completed_courses": ["CS101", "CS101L", "CS201"],
  "target_courses": ["CS303"],
  "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12,
  "num_semesters": 4
}')
echo "$PLAN_B"
PLAN_B_ID=$(echo "$PLAN_B" | python3 -c "import sys,json;print(json.load(sys.stdin)['plan_id'])")

step "5. 确认方案 A（目录未变，应成功）"
curl -s -X POST "$BASE/plans/$PLAN_A_ID/confirm"; echo

step "6. 修订目录（生成版本 2）"
curl -s -X POST "$BASE/catalog/versions" -H 'Content-Type: application/json' \
  -d @"$HERE/catalog_v2.json"; echo

step "7. 确认方案 B（目录已变，应 409 拒绝且不写入）"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/plans/$PLAN_B_ID/confirm"
echo "方案 B 状态（仍为 proposed）："
curl -s "$BASE/plans/$PLAN_B_ID"; echo

step "8. 基于新目录重新规划并确认"
PLAN_C=$(curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "completed_courses": ["CS101", "CS101L", "CS201"],
  "target_courses": ["CS303"],
  "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12,
  "num_semesters": 4
}')
echo "$PLAN_C"
PLAN_C_ID=$(echo "$PLAN_C" | python3 -c "import sys,json;print(json.load(sys.stdin)['plan_id'])")
curl -s -X POST "$BASE/plans/$PLAN_C_ID/confirm"; echo

step "9. 回看：方案绑定的目录版本"
curl -s "$BASE/catalog/versions/1"; echo

step "10. 滚动重排：方案 A 执行 3 个学期后，凭原编号/已结束学期数/累计已修按当前目录(v2)重排"
REPLAN_D=$(curl -s -X POST "$BASE/plans/$PLAN_A_ID/replan" -H 'Content-Type: application/json' -d '{
  "elapsed_semesters": 3,
  "completed_courses": ["CS101", "CS101L", "CS201", "MATH103", "CS301"]
}')
echo "$REPLAN_D"
DRAFT_D_ID=$(echo "$REPLAN_D" | python3 -c "import sys,json;print(json.load(sys.stdin)['plan_id'])")

step "11. 重排差异演示：仅执行 2 个学期，但额外提前修完 CS301（原方案第 3 学期课程）"
curl -s -X POST "$BASE/plans/$PLAN_A_ID/replan" -H 'Content-Type: application/json' -d '{
  "elapsed_semesters": 2,
  "completed_courses": ["CS101", "CS101L", "CS201", "MATH103", "CS301"]
}'; echo

step "12. 确认重排草案 D（基于当前目录 v2，应成功；不影响原方案 A）"
curl -s -X POST "$BASE/plans/$DRAFT_D_ID/confirm"; echo
echo "原方案 A 仍为 confirmed 且绑定 v1："
curl -s "$BASE/plans/$PLAN_A_ID"; echo

step "13. 错误示例：对未确认方案 B 重排（应 409 PLAN_NOT_CONFIRMED，不保存草案）"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/plans/$PLAN_B_ID/replan" \
  -H 'Content-Type: application/json' -d '{"elapsed_semesters": 1, "completed_courses": []}'

step "14. 错误示例：已结束学期数越界（0 / 达到原期限 6）"
curl -s -X POST "$BASE/plans/$PLAN_A_ID/replan" -H 'Content-Type: application/json' \
  -d '{"elapsed_semesters": 0, "completed_courses": []}'; echo
curl -s -X POST "$BASE/plans/$PLAN_A_ID/replan" -H 'Content-Type: application/json' \
  -d '{"elapsed_semesters": 6, "completed_courses": []}'; echo

step "15. 错误示例：累计已修撤销了原方案声明的已修课程（422 COMPLETED_REVOKED）"
curl -s -X POST "$BASE/plans/$PLAN_C_ID/replan" -H 'Content-Type: application/json' \
  -d '{"elapsed_semesters": 1, "completed_courses": ["CS101"]}'; echo

step "16. 错误示例：剩余学期内无解（422 NO_SOLUTION，不保存草案）"
curl -s -X POST "$BASE/plans/$PLAN_A_ID/replan" -H 'Content-Type: application/json' \
  -d '{"elapsed_semesters": 5, "completed_courses": ["CS101", "CS101L"]}'; echo

step "17. 错误示例：课程缺失"
curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "target_courses": ["ABC123"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 6
}'; echo

step "18. 错误示例：条件冲突（CS999 与其同修课开课季节互斥）"
curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "target_courses": ["CS999"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 6
}'; echo

step "19. 错误示例：期限内无解"
curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "target_courses": ["CS401"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 2
}'; echo

step "20. 错误示例：非法输入（空目标 / 非正上限）"
curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "target_courses": [], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 6
}'; echo
curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "target_courses": ["CS101"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 0, "num_semesters": 6
}'; echo

step "21. 锁定安排：锁定非目标课 CS303 到 2027-SPRING（视为本次必修，201）"
curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "target_courses": ["CS401"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 6,
  "locked": [{"course": "CS303", "semester": "2027-SPRING"}]
}'; echo

step "22. 不可排课学期：2026-FALL 不安排课程（完成学期顺延，201）"
curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "target_courses": ["CS401"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 6,
  "unavailable_semesters": ["2026-FALL"]
}'; echo

step "23. 锁定 + 不可排课组合：CS401 锁定到 2028-FALL 且 2026-FALL 不可排课（201）"
curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "target_courses": ["CS401"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 6,
  "locked": [{"course": "CS401", "semester": "2028-FALL"}],
  "unavailable_semesters": ["2026-FALL"]
}'; echo

step "24. 错误示例：锁定到不可排课学期（422 REQUIREMENT_CONFLICT，不保存）"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "target_courses": ["CS401"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 6,
  "locked": [{"course": "CS401", "semester": "2027-FALL"}],
  "unavailable_semesters": ["2027-FALL"]
}'

step "25. 错误示例：锁定学期该课不开课（CS401 仅 FALL 开课，422 REQUIREMENT_CONFLICT）"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "target_courses": ["CS401"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 6,
  "locked": [{"course": "CS401", "semester": "2027-SPRING"}]
}'

step "26. 错误示例：重复锁定 / 锁定已修课 / 锁定学期超出期限（400 INVALID_INPUT）"
curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "target_courses": ["CS401"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 6,
  "locked": [{"course": "CS303", "semester": "2027-SPRING"},
             {"course": "CS303", "semester": "2027-FALL"}]
}'; echo
curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "completed_courses": ["CS101", "CS101L"],
  "target_courses": ["CS401"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 6,
  "locked": [{"course": "CS101", "semester": "2026-SPRING"}]
}'; echo
curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "target_courses": ["CS401"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 6,
  "locked": [{"course": "CS401", "semester": "2030-FALL"}]
}'; echo

step "27. 错误示例：锁定导致期限内无解（422 NO_SOLUTION，不保存）"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "target_courses": ["CS401"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 4,
  "locked": [{"course": "MATH103", "semester": "2027-SPRING"}]
}'

step "28. 课程互认组：目录修订声明互认组（CS301/CS302、CS401/CS402），生成版本 3"
curl -s -X POST "$BASE/catalog/versions" -H 'Content-Type: application/json' \
  -d @"$HERE/catalog_v3_equiv.json"; echo
echo "当前目录（含 equivalence_groups）："
curl -s "$BASE/catalog/current"; echo

step "29. 互认：已修同组课程视为已满足——已修 CS302 视同满足 CS301，CS401 直接可排"
curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "completed_courses": ["CS101", "CS101L", "CS201", "MATH103", "CS302"],
  "target_courses": ["CS401"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 6
}'; echo

step "30. 互认：目标替代——目标 CS401，实际择修同组的 CS402（春季即可，方案列出实际修读课程）"
curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "completed_courses": ["CS101", "CS101L", "CS201", "MATH101", "MATH102", "MATH103", "CS301"],
  "target_courses": ["CS401"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 6
}'; echo

step "31. 互认：一次修读满足多处引用——目标 CS301 与 CS302 同组，修读一门即可"
curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "completed_courses": ["CS101", "CS101L", "CS201", "MATH103"],
  "target_courses": ["CS301", "CS302"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 6
}'; echo

step "32. 互认不豁免锁定：CS401 锁定到春季（仅秋季开课），同组 CS402 不能顶替（422 REQUIREMENT_CONFLICT）"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "target_courses": ["CS401"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 6,
  "locked": [{"course": "CS401", "semester": "2027-SPRING"}]
}'

step "33. 滚动重排按当前目录判断互认：方案 A 执行 2 学期后已修 CS302（CS301 同组），重排不再排 CS301"
curl -s -X POST "$BASE/plans/$PLAN_A_ID/replan" -H 'Content-Type: application/json' -d '{
  "elapsed_semesters": 2,
  "completed_courses": ["CS101", "CS101L", "CS201", "MATH103", "CS302"]
}'; echo

step "34. 错误示例：互认组含不存在课程 / 重复归组 / 少于两门（422，不生成版本）"
curl -s -X POST "$BASE/catalog/versions" -H 'Content-Type: application/json' -d '{
  "courses": [{"code": "X1", "credits": 2, "semesters": ["SPRING"]}],
  "equivalence_groups": [["X1", "NOPE"]]
}'; echo
curl -s -X POST "$BASE/catalog/versions" -H 'Content-Type: application/json' -d '{
  "courses": [{"code": "X1", "credits": 2, "semesters": ["SPRING"]},
              {"code": "X2", "credits": 2, "semesters": ["SPRING"]},
              {"code": "X3", "credits": 2, "semesters": ["SPRING"]}],
  "equivalence_groups": [["X1", "X2"], ["X2", "X3"]]
}'; echo
curl -s -X POST "$BASE/catalog/versions" -H 'Content-Type: application/json' -d '{
  "courses": [{"code": "X1", "credits": 2, "semesters": ["SPRING"]}],
  "equivalence_groups": [["X1"]]
}'; echo
echo "当前目录版本（应仍为 3，未因非法提交生成新版本）："
curl -s "$BASE/catalog/current" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['version_id'], d['equivalence_groups'])"

step "35. 回看：历史目录版本保持原语义（版本 1 无互认组）"
curl -s "$BASE/catalog/versions/1" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['version_id'], d['equivalence_groups'])"

step "36. 席位上限：设置 CS303@2028-SPRING 上限 1 并查询（上限/已占用/余量；用新学期避免与前序方案重叠）"
curl -s -X PUT "$BASE/seat-caps/CS303/2028-SPRING" -H 'Content-Type: application/json' -d '{"cap": 1}'; echo
curl -s "$BASE/seat-caps/CS303/2028-SPRING"; echo

step "37. 席位设置拒绝：非法学期（400）/ 未知课程（422）/ 负上限（422）"
curl -s -w '\nHTTP %{http_code}\n' -X PUT "$BASE/seat-caps/CS303/2026春" \
  -H 'Content-Type: application/json' -d '{"cap": 1}'
curl -s -w '\nHTTP %{http_code}\n' -X PUT "$BASE/seat-caps/NOPE101/2026-SPRING" \
  -H 'Content-Type: application/json' -d '{"cap": 1}'
curl -s -w '\nHTTP %{http_code}\n' -X PUT "$BASE/seat-caps/CS303/2028-SPRING" \
  -H 'Content-Type: application/json' -d '{"cap": -1}'

step "38. 未设置即不限额（cap/remaining 为 null）"
curl -s "$BASE/seat-caps/CS401/2027-FALL"; echo

step "39. 余量规划：CS303（仅 SPRING 开课）2028/2029 春季名额封死 -> NO_SOLUTION 且不保存"
curl -s -X PUT "$BASE/seat-caps/CS303/2028-SPRING" -H 'Content-Type: application/json' -d '{"cap": 0}' >/dev/null
curl -s -X PUT "$BASE/seat-caps/CS303/2029-SPRING" -H 'Content-Type: application/json' -d '{"cap": 0}' >/dev/null
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "completed_courses": ["CS101", "CS101L", "CS201"], "target_courses": ["CS303"],
  "start_semester": "2028-SPRING", "max_credits_per_semester": 12, "num_semesters": 2
}'

step "40. 超卖防护：放开 2028-SPRING 名额为 1，两个方案竞争，后确认者 409 SEAT_CONFLICT"
curl -s -X PUT "$BASE/seat-caps/CS303/2028-SPRING" -H 'Content-Type: application/json' -d '{"cap": 1}' >/dev/null
P_X=$(curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "completed_courses": ["CS101", "CS101L", "CS201"], "target_courses": ["CS303"],
  "start_semester": "2028-SPRING", "max_credits_per_semester": 12, "num_semesters": 2
}')
P_X_ID=$(echo "$P_X" | python3 -c "import sys,json;print(json.load(sys.stdin)['plan_id'])")
P_Y=$(curl -s -X POST "$BASE/plans" -H 'Content-Type: application/json' -d '{
  "completed_courses": ["CS101", "CS101L", "CS201"], "target_courses": ["CS303"],
  "start_semester": "2028-SPRING", "max_credits_per_semester": 12, "num_semesters": 2
}')
P_Y_ID=$(echo "$P_Y" | python3 -c "import sys,json;print(json.load(sys.stdin)['plan_id'])")
curl -s -X POST "$BASE/plans/$P_X_ID/confirm" | python3 -c 'import sys,json;print("方案 X:", json.load(sys.stdin)["status"])'
echo "查询余量："; curl -s "$BASE/seat-caps/CS303/2028-SPRING"; echo
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/plans/$P_Y_ID/confirm"
echo "方案 Y 状态（仍 proposed，席位未超卖）："
curl -s "$BASE/plans/$P_Y_ID" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])'

step "41. 上限低于已占用拒绝（409 CAP_BELOW_OCCUPIED，原上限不变）"
curl -s -w '\nHTTP %{http_code}\n' -X PUT "$BASE/seat-caps/CS303/2028-SPRING" \
  -H 'Content-Type: application/json' -d '{"cap": 0}'
echo "上限查询（仍为 1）："; curl -s "$BASE/seat-caps/CS303/2028-SPRING"; echo

step "42. 培养方案要求：按培养方案编号登记要求版本，绑定当前目录（含互认组）"
RV_VID=$(curl -s "$BASE/catalog/current" | python3 -c "import sys,json;print(json.load(sys.stdin)['version_id'])")
RV=$(python3 -c "
import json,sys
p=json.load(open('$HERE/requirement_cs_2024.json'))
p['catalog_version_id']=int(sys.argv[1])
print(json.dumps(p,ensure_ascii=False))" "$RV_VID")
RV_RESP=$(curl -s -X POST "$BASE/programs/CS-BACH-2024/requirement-versions" \
  -H 'Content-Type: application/json' -d "$RV")
echo "$RV_RESP"
RV_ID=$(echo "$RV_RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['requirement_version_id'])")
echo "回看要求版本："; curl -s "$BASE/programs/requirement-versions/$RV_ID"; echo

step "43. 达成审核（应达成）：CS302/CS402 经互认满足必修 CS301/CS401；CS402 只归属必修，专业选修由 CS303 计入"
AUDIT_OK=$(curl -s -X POST "$BASE/requirement-versions/$RV_ID/audits" \
  -H 'Content-Type: application/json' -d '{
    "completed_courses": ["CS101","CS201","CS302","CS402","CS303","MATH101","MATH103"]
  }')
echo "$AUDIT_OK"
AUDIT_OK_ID=$(echo "$AUDIT_OK" | python3 -c "import sys,json;print(json.load(sys.stdin)['audit_id'])")

step "44. 达成审核（有缺口）：缺 CS301/CS401 必修，专业选修计入 CS303，基础拓展 0 学分"
curl -s -X POST "$BASE/requirement-versions/$RV_ID/audits" \
  -H 'Content-Type: application/json' -d '{"completed_courses": ["CS101","CS201","CS303"]}'; echo

step "45. 审核互认去重：CS301 与 CS302 都已修也至多计入一门；CS402 归必修后专业选修无课可计"
curl -s -X POST "$BASE/requirement-versions/$RV_ID/audits" \
  -H 'Content-Type: application/json' -d '{
    "completed_courses": ["CS101","CS201","CS301","CS302","CS402"]
  }' | python3 -c "
import sys,json
r=json.load(sys.stdin)['result']
print('未满足必修:', r['unsatisfied_required'])
for g in r['elective_groups']:
    print('选修组', g['index'], g['name'], '计入学分', g['counted_credits'], '缺口', g['credit_gap'])
print('未归属课程:', [c['code'] for c in r['unattributed_courses']])
"

step "46. 历史审核回看（快照，后续目录/要求修订不改变结果）与审核列表过滤"
curl -s "$BASE/audits/$AUDIT_OK_ID" | python3 -c "import sys,json;d=json.load(sys.stdin);print('achieved:',d['result']['achieved'],'catalog_version_id:',d['catalog_version_id'])"
curl -s "$BASE/audits?requirement_version_id=$RV_ID" \
  | python3 -c "import sys,json;print('该要求版本审核数:', len(json.load(sys.stdin)['audits']))"

step "47. 错误示例：未知目录（404）/ 未知课程（422）/ 重复必修（422）/ 非正门槛（422），均不留记录"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/programs/BAD/requirement-versions" \
  -H 'Content-Type: application/json' -d '{"program_code":"BAD","catalog_version_id": 999, "required_courses": []}'
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/programs/BAD/requirement-versions" \
  -H 'Content-Type: application/json' -d '{"program_code":"BAD","catalog_version_id": 1, "required_courses": ["NOPE101"]}'
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/programs/BAD/requirement-versions" \
  -H 'Content-Type: application/json' -d '{"program_code":"BAD","catalog_version_id": 1, "required_courses": ["CS101","CS101"]}'
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/programs/BAD/requirement-versions" \
  -H 'Content-Type: application/json' -d '{"program_code":"BAD","catalog_version_id": 1, "elective_groups": [{"candidates": ["CS303"], "min_credits": 0}]}'
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/requirement-versions/$RV_ID/audits" \
  -H 'Content-Type: application/json' -d '{"completed_courses": ["UNKNOWN"]}'
echo "要求版本列表（BAD 无任何版本）："
curl -s "$BASE/programs/BAD/requirement-versions"; echo

step "48. 培养方案达成课表：凭要求版本+已修+期限，求最早达成全部必修与选修门槛的课表"
PP=$(curl -s -X POST "$BASE/requirement-versions/$RV_ID/plans" \
  -H 'Content-Type: application/json' -d '{
    "completed_courses": ["CS101", "CS101L", "CS201", "MATH101", "MATH103"],
    "start_semester": "2026-SPRING",
    "max_credits_per_semester": 12,
    "num_semesters": 6
  }')
echo "$PP" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('plan_id:', d['plan_id'], 'status:', d['status'], 'plan_type:', d['plan_type'])
print('当前目录版本:', d['catalog_version'], '要求绑定目录版本:', d['bound_catalog_version'])
for s in d['result']['semesters']:
    print(' ', s['semester'], [c['code'] for c in s['courses']], s['total_credits'], '学分')
print('完成学期:', d['result']['completion_semester'])
r=d['attribution']['result']
print('预计达成:', r['achieved'])
for q in r['required']:
    a=q['attributed_course']
    print(' 必修', q['code'], '<-', a['code'], a['via']) if a else print(' 必修', q['code'], '未满足')
for g in r['elective_groups']:
    print(' 选修', g['name'], '计入', g['counted_credits'], '学分 缺口', g['credit_gap'],
          [(c['code'], c['via']) for c in g['attributed_courses']])
print(' 未归属课程:', [c['code'] for c in r['unattributed_courses']])
"
PP_ID=$(echo "$PP" | python3 -c "import sys,json;print(json.load(sys.stdin)['plan_id'])")
echo "待确认不占位（CS401@2026-FALL occupied 应为 0）："
curl -s "$BASE/seat-caps/CS401/2026-FALL"; echo

step "49. 确认达成课表（沿用既有确认接口：原子复核目录版本与席位后占位）"
curl -s -X POST "$BASE/plans/$PP_ID/confirm" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('status:', d['status'])"
curl -s "$BASE/seat-caps/CS401/2026-FALL"; echo
echo "拟修课未写成历史审核，仅作为待确认/已确认方案的 attribution 回看："
curl -s "$BASE/plans/$PP_ID" | python3 -c "import sys,json;d=json.load(sys.stdin);print('plan_type:', d['plan_type'], '有 attribution:', 'attribution' in d)"

step "50. 已修已达成全部要求：返回 200 空课表（不保存、不占位、不写审核）"
curl -s -w 'HTTP %{http_code}' -X POST "$BASE/requirement-versions/$RV_ID/plans" \
  -H 'Content-Type: application/json' -d '{
    "completed_courses": ["CS101","CS201","CS302","CS402","CS303","MATH101","MATH103"],
    "start_semester": "2026-SPRING",
    "max_credits_per_semester": 12,
    "num_semesters": 6
  }' | python3 -c "
import sys,json
raw=sys.stdin.read()
body, _, code = raw.rpartition('HTTP ')
d=json.loads(body)
print('HTTP', code.strip(), 'status:', d['status'], 'plan_id:', d['plan_id'],
      '空课表:', d['result']['semesters'], '预计达成:', d['attribution']['result']['achieved'])
"

step "51. 达成课表错误示例：未知要求版本（404）/ 未知已修课（422）/ 期限内无解（422），均不保存"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/requirement-versions/NOPE:v1/plans" \
  -H 'Content-Type: application/json' -d '{"start_semester":"2026-SPRING","max_credits_per_semester":12,"num_semesters":6}'
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/requirement-versions/$RV_ID/plans" \
  -H 'Content-Type: application/json' -d '{"completed_courses":["UNKNOWN"],"start_semester":"2026-SPRING","max_credits_per_semester":12,"num_semesters":6}'
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/requirement-versions/$RV_ID/plans" \
  -H 'Content-Type: application/json' -d '{"completed_courses":[],"start_semester":"2026-SPRING","max_credits_per_semester":12,"num_semesters":2}'

step "52. 达成课表执行中途重排：已结束 1 学期且 CS301 已修，剩余 5 学期自 2026-FALL 按当前目录重排"
PPRD=$(curl -s -X POST "$BASE/plans/$PP_ID/replan" \
  -H 'Content-Type: application/json' -d '{
    "elapsed_semesters": 1,
    "completed_courses": ["CS101", "CS101L", "CS201", "CS301", "MATH101", "MATH103"]
  }')
echo "$PPRD" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('draft:', d['plan_id'], 'status:', d['status'], 'plan_type:', d['plan_type'],
      '来源:', d['source_plan_id'])
print('绑定目录(归属):', d['bound_catalog_version'], '当前目录(排课):', d['catalog_version'])
print('有效输入: 起始', d['request']['start_semester'], '剩余学期', d['request']['num_semesters'])
for s in d['result']['semesters']:
    print(' ', s['semester'], [c['code'] for c in s['courses']], s['total_credits'], '学分')
print('完成学期:', d['result']['completion_semester'], '预计达成:', d['attribution']['result']['achieved'])
print('与原剩余课表差异:', json.dumps(d['replan']['difference'], ensure_ascii=False))
"
PPRD_ID=$(echo "$PPRD" | python3 -c "import sys,json;print(json.load(sys.stdin)['plan_id'])")
echo "草案不预占席位（CS303@2027-SPRING occupied 应为 0）："
curl -s "$BASE/seat-caps/CS303/2027-SPRING"; echo

step "53. 确认重排草案：仅替换来源未结束学期占位（CS401 仍占 1），重复确认幂等"
curl -s -X POST "$BASE/plans/$PPRD_ID/confirm" \
  | python3 -c "import sys,json;print('status:', json.load(sys.stdin)['status'])"
echo "CS303@2026-SPRING（已结束学期：来源占位保留；含早前 demo 方案的累计占用）:"
curl -s "$BASE/seat-caps/CS303/2026-SPRING"; echo
echo "CS401@2026-FALL（撤旧占新，仍为 1）:"; curl -s "$BASE/seat-caps/CS401/2026-FALL"; echo
echo "CS303@2027-SPRING（新课表占位，变为 1）:"; curl -s "$BASE/seat-caps/CS303/2027-SPRING"; echo
curl -s -X POST "$BASE/plans/$PPRD_ID/confirm" \
  | python3 -c "import sys,json;print('重复确认幂等 status:', json.load(sys.stdin)['status'])"
echo "CS303@2027-SPRING 仍为 1:"; curl -s "$BASE/seat-caps/CS303/2027-SPRING"; echo

step "54. 重排错误示例：已修回退 422 / 学期数达到原期限 400 / 未知已修课 422 / 同来源第二个生效重排 409"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/plans/$PP_ID/replan" \
  -H 'Content-Type: application/json' -d '{"elapsed_semesters":1,"completed_courses":["CS101","CS201","MATH101","MATH103"]}'
# 撤销了来源申请时声明的 CS101L -> 422 COMPLETED_REVOKED
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/plans/$PP_ID/replan" \
  -H 'Content-Type: application/json' -d '{"elapsed_semesters":6,"completed_courses":["CS101","CS101L","CS201","MATH101","MATH103"]}'
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/plans/$PP_ID/replan" \
  -H 'Content-Type: application/json' -d '{"elapsed_semesters":1,"completed_courses":["CS101","CS101L","CS201","MATH101","MATH103","UNKNOWN"]}'
# 同一来源已有生效重排：新草案（仅凭累计已修即达成、未来课表为空）确认时 409 DRAFT_EXPIRED
EMPTY_RD=$(curl -s -X POST "$BASE/plans/$PP_ID/replan" \
  -H 'Content-Type: application/json' -d '{
    "elapsed_semesters": 2,
    "completed_courses": ["CS101", "CS101L", "CS201", "CS302", "CS402", "CS303", "MATH101", "MATH103"]
  }')
echo "$EMPTY_RD" | python3 -c "import sys,json;d=json.load(sys.stdin);print('空未来课表草案，semesters:', d['result']['semesters'], '预计达成:', d['attribution']['result']['achieved'])"
EMPTY_RD_ID=$(echo "$EMPTY_RD" | python3 -c "import sys,json;print(json.load(sys.stdin)['plan_id'])")
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/plans/$EMPTY_RD_ID/confirm"
echo done
