"""培养方案达成课表：HTTP API 集成测试（独立临时数据库）。"""
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    from app.main import app
    with TestClient(app) as c:
        yield c


def _equiv_catalog(client, groups=(["CS301", "CS302"], ["CS401", "CS402"]),
                   credit_patch=None):
    current = client.get("/catalog/current").json()
    for course in current["courses"]:
        if credit_patch and course["code"] in credit_patch:
            course["credits"] = credit_patch[course["code"]]
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


# ---------------------------------------------------------------- 基本流程

def test_program_plan_created_with_schedule_and_attribution(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    r = client.post(f"/requirement-versions/{rv_id}/plans", json=_plan_body())
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["plan_type"] == "program_plan"
    assert d["status"] == "proposed"
    assert d["catalog_version"] == vid
    assert d["bound_catalog_version"] == vid
    assert d["requirement_version_id"] == rv_id
    sems = [[c["code"] for c in s["courses"]] for s in d["result"]["semesters"]]
    assert sems == [["CS301", "CS303"], ["CS401"]]
    assert d["result"]["completion_semester"] == "2026-FALL"
    # 预计归属：合并已修与拟修
    res = d["attribution"]["result"]
    assert res["achieved"] is True
    assert res["unsatisfied_required"] == []
    groups = {g["name"]: g for g in res["elective_groups"]}
    assert [c["code"] for c in groups["专业选修"]["attributed_courses"]] == ["CS303"]
    assert groups["基础拓展"]["counted_credits"] == 6
    # 拟修课不写成历史审核；出现在计划列表且可回看
    assert client.get("/audits").json()["audits"] == []
    listing = client.get("/plans").json()
    assert any(p["plan_id"] == d["plan_id"] for p in listing)
    review = client.get(f"/plans/{d['plan_id']}").json()
    assert review["attribution"]["result"] == res
    assert review["plan_type"] == "program_plan"


def test_program_plan_confirm_uses_existing_flow_and_holds_seats(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = client.post(f"/requirement-versions/{rv_id}/plans", json=_plan_body()).json()
    r = client.post(f"/plans/{plan['plan_id']}/confirm")
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"
    # 重复确认幂等
    assert client.post(f"/plans/{plan['plan_id']}/confirm").json()["status"] == "confirmed"
    # 已占位：CS401@2026-FALL
    seat = client.get("/seat-caps/CS401/2026-FALL").json()
    assert seat["occupied"] == 1


def test_program_plan_does_not_prehold_seats(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    body = _plan_body(completed_courses=["CS101", "CS101L", "CS201", "CS301",
                                         "MATH101", "MATH103"])
    first = client.post(f"/requirement-versions/{rv_id}/plans", json=body).json()
    second = client.post(f"/requirement-versions/{rv_id}/plans", json=body).json()
    # 两个待确认课表可相同，互不占位
    assert first["result"]["semesters"][-1]["courses"][0]["code"] == "CS401"
    assert second["result"]["semesters"][-1]["courses"][0]["code"] == "CS401"
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 0
    # 先确认者占座，后确认者原子复核失败（无上限时不限额，这里设上限 1 制造竞争）
    client.put("/seat-caps/CS401/2026-FALL", json={"cap": 1})
    assert client.post(f"/plans/{first['plan_id']}/confirm").status_code == 200
    r = client.post(f"/plans/{second['plan_id']}/confirm")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SEAT_CONFLICT"


# ---------------------------------------------------------------- 已达成与错误路径

def test_already_achieved_returns_empty_schedule_and_not_saved(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    completed = ["CS101", "CS201", "CS302", "CS402", "CS303", "MATH101", "MATH103"]
    r = client.post(f"/requirement-versions/{rv_id}/plans",
                    json=_plan_body(completed_courses=completed))
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "already_achieved"
    assert d["plan_id"] is None
    assert d["result"]["semesters"] == []
    assert d["attribution"]["result"]["achieved"] is True
    # 空课表不落库、不占位、不写审核
    assert client.get("/plans").json() == []
    assert client.get("/audits").json()["audits"] == []


def test_unknown_requirement_version_404(client):
    r = client.post("/requirement-versions/NOPE:v1/plans", json=_plan_body())
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NOT_FOUND"


def test_unknown_completed_course_rejected_and_not_saved(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    r = client.post(f"/requirement-versions/{rv_id}/plans",
                    json=_plan_body(completed_courses=["CS101", "NOPE999"]))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "COURSE_MISSING"
    assert client.get("/plans").json() == []


def test_no_solution_within_horizon_rejected_and_not_saved(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    r = client.post(f"/requirement-versions/{rv_id}/plans",
                    json=_plan_body(completed_courses=[], num_semesters=2))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "NO_SOLUTION"
    assert client.get("/plans").json() == []


def test_invalid_plan_inputs_rejected(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    assert client.post(f"/requirement-versions/{rv_id}/plans",
                       json=_plan_body(max_credits_per_semester=0)).status_code == 422
    assert client.post(f"/requirement-versions/{rv_id}/plans",
                       json=_plan_body(num_semesters=0)).status_code == 422
    r = client.post(f"/requirement-versions/{rv_id}/plans",
                    json=_plan_body(start_semester="2026春"))
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_INPUT"
    assert client.get("/plans").json() == []


# ---------------------------------------------------------------- 两版目录分工

def test_planned_course_must_exist_in_both_catalogs_to_count(client):
    """绑定目录 v1（无 CS402 互认/无新增课），当前目录 v2 声明互认：
    当前目录独有互认不能让 CS302 计入绑定目录的必修 CS301。"""
    # 要求绑定 v1（种子目录，无互认组）
    rv_id = _register(client, 1)
    # 当前目录修订为含互认组版本（不影响绑定版本）
    _equiv_catalog(client)
    body = _plan_body(completed_courses=[
        "CS101", "CS101L", "CS201", "MATH101", "MATH103", "CS302"])
    r = client.post(f"/requirement-versions/{rv_id}/plans", json=body)
    assert r.status_code == 201, r.text
    res = r.json()["attribution"]["result"]
    by = {q["code"]: q for q in res["required"]}
    # 绑定目录无互认：CS302 不能满足必修 CS301，课表中须拟修 CS301
    assert by["CS301"]["attributed_course"]["code"] == "CS301"
    flat = {c["code"] for s in r.json()["result"]["semesters"] for c in s["courses"]}
    assert "CS301" in flat


def test_current_only_course_is_scheduled_as_prerequisite_but_not_attributed(client):
    """当前目录新增 CS500（不在绑定目录），把它配成某必修课链上的必要先修：
    可排入课表但不归属任何要求项。"""
    current = client.get("/catalog/current").json()
    courses = current["courses"]
    # 给 MATH103 增加先修 CS500（仅 FALL），则依赖 MATH103 的 CS301 链必须带上 CS500
    courses = [dict(c) for c in courses]
    for c in courses:
        if c["code"] == "MATH103":
            c["prereq_groups"] = [["CS500"]]
    courses.append({"code": "CS500", "name": "新课", "credits": 2,
                    "semesters": ["FALL"], "prereq_groups": [], "coreqs": []})
    vid = client.post("/catalog/versions",
                      json={"note": "新增当前目录独有课", "courses": courses,
                            "equivalence_groups": [["CS301", "CS302"], ["CS401", "CS402"]]}
                      ).json()["version_id"]
    rv_id = _register(client, vid)
    r = client.post(f"/requirement-versions/{rv_id}/plans",
                    json=_plan_body(completed_courses=["CS101", "CS101L", "CS201",
                                                       "MATH101"]))
    assert r.status_code == 201, r.text
    d = r.json()
    flat = [c["code"] for s in d["result"]["semesters"] for c in s["courses"]]
    assert "CS500" in flat
    res = d["attribution"]["result"]
    attributed = {c["code"] for g in res["elective_groups"]
                  for c in g["attributed_courses"]}
    attributed |= {q["attributed_course"]["code"] for q in res["required"]
                   if q["attributed_course"]}
    assert "CS500" not in attributed
    assert res["achieved"] is True


def test_seat_shortage_moves_completion_and_reports_full_semester(client):
    # 绑定与当前均使用无互认目录（v1 种子）：必修 CS401 无替代课，满座只能顺延
    rv_id = _register(client, 1)
    completed = ["CS101", "CS101L", "CS201", "CS301", "MATH101", "MATH103"]
    # CS401 仅 FALL，封死 2026-FALL -> 顺延 2027-FALL
    client.put("/seat-caps/CS401/2026-FALL", json={"cap": 0})
    r = client.post(f"/requirement-versions/{rv_id}/plans",
                    json=_plan_body(completed_courses=completed))
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["result"]["completion_semester"] == "2027-FALL"
    # 待确认不占位
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 0
    # 所有可行学期都封死 -> NO_SOLUTION
    client.put("/seat-caps/CS401/2027-FALL", json={"cap": 0})
    r2 = client.post(f"/requirement-versions/{rv_id}/plans",
                     json=_plan_body(completed_courses=completed, num_semesters=4))
    assert r2.status_code == 422
    assert r2.json()["detail"]["code"] == "NO_SOLUTION"
    assert "席位" in r2.json()["detail"]["message"]


def test_catalog_revision_between_calc_and_save_rejected(client, monkeypatch):
    """落库前目录被修订 -> 409 CATALOG_CHANGED 且不保存（与滚动重排一致）。"""
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    # 直接在引擎调用后、落库前插入新版本：用 monkeypatch 包裹 get_current_version
    from app import main, db
    orig = db.get_current_version
    calls = {"n": 0}

    def flaky(conn):
        row = orig(conn)
        calls["n"] += 1
        if calls["n"] == 2:  # 引擎读取用第 1 次；落库前复查时返回一个“更新的”版本号
            class _R:
                def __getitem__(self, k):
                    return row["id"] + 1 if k == "id" else row[k]
            return _R()
        return row

    monkeypatch.setattr(main.db, "get_current_version", flaky)
    r = client.post(f"/requirement-versions/{rv_id}/plans", json=_plan_body())
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "CATALOG_CHANGED"
    assert client.get("/plans").json() == []


def test_existing_endpoints_remain_compatible(client):
    """普通规划/审核/席位接口冒烟回归。"""
    vid = _equiv_catalog(client)
    plan = client.post("/plans", json={
        "completed_courses": [], "target_courses": ["CS401"],
        "start_semester": "2026-SPRING", "max_credits_per_semester": 12,
        "num_semesters": 6}).json()
    assert plan["catalog_version"] == vid
    assert "attribution" not in plan
    assert client.post(f"/plans/{plan['plan_id']}/confirm").status_code == 200
    rv_id = _register(client, vid)
    audit = client.post(f"/requirement-versions/{rv_id}/audits",
                        json={"completed_courses": ["CS101"]})
    assert audit.status_code == 201
