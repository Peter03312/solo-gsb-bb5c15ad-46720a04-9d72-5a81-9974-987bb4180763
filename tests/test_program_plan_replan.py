"""培养方案达成课表执行中途的滚动重排：HTTP 集成测试（独立临时数据库）。"""
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    from app.main import app
    with TestClient(app) as c:
        yield c


def _equiv_catalog(client, groups=(["CS301", "CS302"], ["CS401", "CS402"])):
    current = client.get("/catalog/current").json()
    r = client.post("/catalog/versions", json={
        "note": "互认目录", "courses": current["courses"],
        "equivalence_groups": [list(g) for g in groups]})
    assert r.status_code == 201, r.text
    return r.json()["version_id"]


def _register(client, vid, program="CS-2024", **kw):
    body = {
        "program_code": program,
        "catalog_version_id": vid,
        "required_courses": ["CS101", "CS201", "CS301", "CS401"],
        "elective_groups": [
            {"name": "专业选修", "candidates": ["CS303", "CS402"], "min_credits": 3},
            {"name": "基础拓展", "candidates": ["MATH101", "MATH103"], "min_credits": 6},
        ],
    }
    body.update(kw)
    r = client.post(f"/programs/{program}/requirement-versions", json=body)
    assert r.status_code == 201, r.text
    return r.json()["requirement_version_id"]


def _plan_body(**kw):
    body = {
        "completed_courses": ["CS101", "CS101L", "CS201", "MATH101", "MATH103"],
        "start_semester": "2026-SPRING",
        "max_credits_per_semester": 12,
        "num_semesters": 6,
    }
    body.update(kw)
    return body


def _confirmed_program_plan(client, rv_id, **kw):
    r = client.post(f"/requirement-versions/{rv_id}/plans", json=_plan_body(**kw))
    assert r.status_code == 201, r.text
    plan = r.json()
    r = client.post(f"/plans/{plan['plan_id']}/confirm")
    assert r.status_code == 200, r.text
    return plan


def _replan(client, plan_id, elapsed, completed):
    return client.post(f"/plans/{plan_id}/replan", json={
        "elapsed_semesters": elapsed, "completed_courses": completed})


# ---------------------------------------------------------------- 成功路径

def test_program_replan_reselects_courses_against_current_catalog(client):
    # 原课表（2026-SPRING 起 6 学期）：SPRING [CS301,CS303]、FALL [CS401]
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)

    # 执行 1 学期后：CS301 已提前修掉；剩余 5 学期自 2026-FALL 起按当前目录重排
    r = _replan(client, plan["plan_id"], 1,
                ["CS101", "CS101L", "CS201", "MATH101", "MATH103", "CS301"])
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["status"] == "draft"
    assert draft["plan_type"] == "program_plan"
    assert draft["source_plan_id"] == plan["plan_id"]
    assert draft["requirement_version_id"] == rv_id
    assert draft["bound_catalog_version"] == vid
    assert draft["catalog_version"] == vid
    assert draft["replan"]["plan_type"] == "program_plan"

    req = draft["request"]
    assert req["plan_type"] == "program_plan"
    assert req["start_semester"] == "2026-FALL"
    assert req["num_semesters"] == 5
    assert req["max_credits_per_semester"] == 12
    assert req["requirement_version_id"] == rv_id
    assert "CS301" not in req["target_courses"]  # 已修不重复安排
    # CS401 仅 FALL 可排入 2026-FALL；CS303 仅 SPRING 顺延到 2027-SPRING
    sems = [[c["code"] for c in s["courses"]] for s in draft["result"]["semesters"]]
    assert sems == [["CS401"], ["CS303"]]

    # 预计归属：合并来源已修 + 累计新增已修 + 拟修
    res = draft["attribution"]["result"]
    assert res["achieved"] is True
    assert "CS301" in draft["attribution"]["completed_courses"]
    by = {q["code"]: q for q in res["required"]}
    assert by["CS301"]["attributed_course"]["code"] == "CS301"
    assert by["CS401"]["attributed_course"]["code"] == "CS401"

    # 原剩余课表差异：原 2026-FALL 为 [CS401]，CS303 新增在 2027-SPRING
    diff = draft["replan"]["difference"]
    spring = next(s for s in diff["semesters"] if s["semester"] == "2027-SPRING")
    assert spring["added_courses"] == ["CS303"]
    assert diff["added_courses"] == ["CS303"]
    assert diff["removed_courses"] == []

    # 原方案不受影响
    orig = client.get(f"/plans/{plan['plan_id']}").json()
    assert orig["status"] == "confirmed"
    assert "replan" not in orig


def test_program_replan_can_reselect_elective_attribution(client):
    # 目录修订后重排：原拟修 CS303 计入专业选修，重排可改选互认课 CS402（SPRING 开课）
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    assert client.post("/catalog/versions", json={
        "note": "原样修订",
        "courses": client.get("/catalog/current").json()["courses"],
        "equivalence_groups": [["CS301", "CS302"], ["CS401", "CS402"]]}).status_code == 201

    # 执行 2 学期后：CS301/CS303 均未修（原课表里 CS303 已在第 1 学期，实际没修成），
    # 只完成基础课；剩余 4 学期自 2027-SPRING 起重排，可改选 CS402 满足专业选修
    r = _replan(client, plan["plan_id"], 2,
                ["CS101", "CS101L", "CS201", "MATH101", "MATH103"])
    assert r.status_code == 201, r.text
    draft = r.json()
    flat = {c["code"] for s in draft["result"]["semesters"] for c in s["courses"]}
    res = draft["attribution"]["result"]
    assert res["achieved"] is True
    groups = {g["name"]: g for g in res["elective_groups"]}
    elective_codes = {c["code"] for c in groups["专业选修"]["attributed_courses"]}
    # 归属与拟修一致：专业选修由未来课表中实际修读的课程承担
    assert elective_codes <= flat
    assert elective_codes <= {"CS303", "CS402"}


def test_program_replan_empty_future_when_already_achieved(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    # 执行 1 学期后所有要求均已由累计已修满足（含互认替代：CS302→CS301、CS402→CS401）
    completed = ["CS101", "CS101L", "CS201", "CS302", "CS402", "CS303",
                 "MATH101", "MATH103"]
    r = _replan(client, plan["plan_id"], 1, completed)
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["status"] == "draft"
    assert draft["result"]["semesters"] == []
    assert draft["result"]["completion_semester"] is None
    assert draft["attribution"]["result"]["achieved"] is True
    # 差异：原方案剩余学期课表全部移除
    diff = draft["replan"]["difference"]
    assert diff["removed_courses"] == ["CS401"]
    assert diff["added_courses"] == []


def test_program_replan_confirm_replaces_only_future_holds(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    # 原占位：CS301/CS303@2026-SPRING、CS401@2026-FALL
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 1

    r = _replan(client, plan["plan_id"], 1,
                ["CS101", "CS101L", "CS201", "CS301", "CS303", "MATH101", "MATH103"])
    draft = r.json()
    # 已结束学期含 CS303，剩余只需 CS401（2026-FALL）
    assert [[c["code"] for c in s["courses"]] for s in draft["result"]["semesters"]] == [
        ["CS401"]]
    # 草案不预占席位
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 1

    assert client.post(f"/plans/{draft['plan_id']}/confirm").status_code == 200
    # 已结束学期占位保留（随来源方案）；未来占位仅替换不叠加：CS401 仍为 1
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 1
    # 重复确认幂等
    assert client.post(f"/plans/{draft['plan_id']}/confirm").json()["status"] == "confirmed"
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 1
    # 同一来源第二个生效重排被拒
    other = _replan(client, plan["plan_id"], 2,
                    ["CS101", "CS101L", "CS201", "CS301", "CS303", "MATH101",
                     "MATH103", "CS401"]).json()
    r = client.post(f"/plans/{other['plan_id']}/confirm")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "DRAFT_EXPIRED"


def test_program_replan_against_revised_catalog_uses_current_catalog(client):
    # 要求与原课表绑定 v2；目录修订为 v3（CS401 学分 3->4），重排按当前目录排课
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    current = client.get("/catalog/current").json()["courses"]
    for c in current:
        if c["code"] == "CS401":
            c["credits"] = 4
    v3 = client.post("/catalog/versions", json={
        "note": "CS401 4 学分", "courses": current,
        "equivalence_groups": [["CS301", "CS302"], ["CS401", "CS402"]]}).json()["version_id"]

    r = _replan(client, plan["plan_id"], 1,
                ["CS101", "CS101L", "CS201", "CS301", "CS303", "MATH101", "MATH103"])
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["catalog_version"] == v3
    assert draft["bound_catalog_version"] == vid  # 要求归属仍绑定原版本
    cs401 = draft["result"]["semesters"][0]["courses"][0]
    assert cs401["code"] == "CS401" and cs401["credits"] == 4  # 排课学分按当前目录


def test_program_replan_current_only_completed_satisfies_prereq(client):
    """执行中途修了当前目录独有的必要先修课：可作为排课基础，不参与要求归属。"""
    # 要求绑定 v2（无 CS500）；当前目录修订为 v3：新增 CS500（FALL）并令 MATH103 以其为先修
    bound_vid = _equiv_catalog(client)
    courses = [dict(c) for c in client.get("/catalog/current").json()["courses"]]
    for c in courses:
        if c["code"] == "MATH103":
            c["prereq_groups"] = [["CS500"]]
    courses.append({"code": "CS500", "name": "新增先修", "credits": 2,
                    "semesters": ["FALL"], "prereq_groups": [], "coreqs": []})
    client.post("/catalog/versions", json={
        "note": "CS500 先修", "courses": courses,
        "equivalence_groups": [["CS301", "CS302"], ["CS401", "CS402"]]})
    rv_id = _register(client, bound_vid)
    plan = _confirmed_program_plan(
        client, rv_id,
        completed_courses=["CS101", "CS101L", "CS201", "MATH101"])
    flat_orig = {c["code"] for s in plan["result"]["semesters"] for c in s["courses"]}
    assert "CS500" in flat_orig

    # 执行 1 学期后，CS500 已修（当前目录独有，不在绑定目录）：不报错、不重复排、不计归属
    r = _replan(client, plan["plan_id"], 1,
                ["CS101", "CS101L", "CS201", "MATH101", "CS500"])
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["bound_catalog_version"] == bound_vid
    flat = {c["code"] for s in draft["result"]["semesters"] for c in s["courses"]}
    assert "CS500" not in flat
    attributed = {c["code"] for g in draft["attribution"]["result"]["elective_groups"]
                  for c in g["attributed_courses"]}
    attributed |= {q["attributed_course"]["code"] for q in
                   draft["attribution"]["result"]["required"] if q["attributed_course"]}
    assert "CS500" not in attributed


# ---------------------------------------------------------------- 拒绝路径

def test_program_replan_source_must_be_confirmed(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = client.post(f"/requirement-versions/{rv_id}/plans",
                       json=_plan_body()).json()  # 未确认
    r = _replan(client, plan["plan_id"], 1, _plan_body()["completed_courses"])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PLAN_NOT_CONFIRMED"


def test_program_replan_elapsed_must_be_positive_and_within_horizon(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    completed = _plan_body()["completed_courses"]
    # 非正：pydantic 422
    assert _replan(client, plan["plan_id"], 0, completed).status_code == 422
    # 达到/超过原规划期限（6 学期）：业务层 400
    r = _replan(client, plan["plan_id"], 6, completed)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_INPUT"


def test_program_replan_cannot_revoke_source_completed(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    # 来源申请时声明了 CS101L，重排清单缺少 -> 回退拒绝
    r = _replan(client, plan["plan_id"], 1,
                ["CS101", "CS201", "MATH101", "MATH103"])
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["code"] == "COMPLETED_REVOKED"
    assert "CS101L" in body["message"]
    assert all(p["status"] != "draft" for p in client.get("/plans").json())


def test_program_replan_unknown_completed_course_rejected(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    completed = _plan_body()["completed_courses"] + ["NOPE999"]
    r = _replan(client, plan["plan_id"], 1, completed)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "COURSE_MISSING"
    assert all(p["status"] != "draft" for p in client.get("/plans").json())


def test_program_replan_no_solution_within_remaining_semesters(client):
    # 方案自 2026-FALL 起 6 学期，未来只需 CS401（仅 FALL，排在 2026-FALL）；
    # 执行 5 学期后仅剩 2029-SPRING，CS401 春季不开 -> 剩余 1 学期内无解
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    completed = ["CS101", "CS101L", "CS201", "CS301", "CS303", "MATH101", "MATH103"]
    plan = client.post(f"/requirement-versions/{rv_id}/plans", json=_plan_body(
        completed_courses=completed, start_semester="2026-FALL")).json()
    assert client.post(f"/plans/{plan['plan_id']}/confirm").status_code == 200
    r = _replan(client, plan["plan_id"], 5, completed)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "NO_SOLUTION"
    assert all(p["status"] != "draft" for p in client.get("/plans").json())


def test_program_replan_catalog_change_during_compute_rejected(client, monkeypatch):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    from app import main, db
    orig = db.get_current_version
    calls = {"n": 0}

    def flaky(conn):
        row = orig(conn)
        calls["n"] += 1
        if calls["n"] == 2:  # 落库前复查
            return dict(row) | {"id": row["id"] + 1}
        return row

    monkeypatch.setattr(main.db, "get_current_version", flaky)
    r = _replan(client, plan["plan_id"], 1,
                _plan_body()["completed_courses"] + ["CS301"])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "CATALOG_CHANGED"
    assert all(p["status"] != "draft" for p in client.get("/plans").json())


def test_program_replan_confirm_after_new_revision_expired(client):
    """草案生成后目录再修订：确认沿用既有契约返回 409 CATALOG_CHANGED。"""
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    draft = _replan(
        client, plan["plan_id"], 1,
        _plan_body()["completed_courses"] + ["CS301"]).json()
    client.post("/catalog/versions", json={
        "note": "再修订",
        "courses": client.get("/catalog/current").json()["courses"],
        "equivalence_groups": [["CS301", "CS302"], ["CS401", "CS402"]]})
    r = client.post(f"/plans/{draft['plan_id']}/confirm")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "CATALOG_CHANGED"
    # 原方案与原占位保留
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 1


def test_program_replan_confirm_empty_future_removes_source_future_holds(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    # 原占位：CS401@2026-FALL（未来学期）与 CS301/CS303@2026-SPRING（已结束学期）
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 1
    completed = ["CS101", "CS101L", "CS201", "CS302", "CS402", "CS303",
                 "MATH101", "MATH103"]
    draft = _replan(client, plan["plan_id"], 1, completed).json()
    assert draft["result"]["semesters"] == []
    assert client.post(f"/plans/{draft['plan_id']}/confirm").status_code == 200
    # 未来占位被撤换为空：CS401@2026-FALL 归零；已结束学期占位保留
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 0
    assert client.get("/seat-caps/CS303/2026-SPRING").json()["occupied"] == 1


def test_normal_plan_replan_still_works_alongside_program(client):
    """普通规划重排与培养方案重排共存、互不影响。"""
    vid = _equiv_catalog(client)
    normal = client.post("/plans", json={
        "completed_courses": [], "target_courses": ["CS401"],
        "start_semester": "2026-SPRING", "max_credits_per_semester": 12,
        "num_semesters": 6})
    assert normal.status_code == 201
    normal_id = normal.json()["plan_id"]
    assert client.post(f"/plans/{normal_id}/confirm").status_code == 200
    r = client.post(f"/plans/{normal_id}/replan", json={
        "elapsed_semesters": 2,
        "completed_courses": ["CS101", "CS101L", "CS201", "MATH103"]})
    assert r.status_code == 201
    draft = r.json()
    assert "attribution" not in draft and "plan_type" not in draft
    assert "plan_type" not in draft["replan"]
    assert draft["request"]["target_courses"] == ["CS401"]
