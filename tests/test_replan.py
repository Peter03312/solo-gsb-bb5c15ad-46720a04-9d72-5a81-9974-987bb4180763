"""滚动重排：HTTP 集成测试与差异计算单元测试（独立临时数据库）。"""
import os

import pytest
from fastapi.testclient import TestClient

from app.replan import next_semester_label, remaining_diff


@pytest.fixture()
def client(tmp_path):
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    from app.main import app
    with TestClient(app) as c:
        yield c


def plan_payload(**kw):
    payload = {
        "completed_courses": [],
        "target_courses": ["CS401"],
        "start_semester": "2026-SPRING",
        "max_credits_per_semester": 12,
        "num_semesters": 6,
    }
    payload.update(kw)
    return payload


def _create_confirmed(client, **kw):
    r = client.post("/plans", json=plan_payload(**kw))
    assert r.status_code == 201, r.text
    plan = r.json()
    r = client.post(f"/plans/{plan['plan_id']}/confirm")
    assert r.status_code == 200
    return plan


# ---------------------------------------------------------------- 单元：学期与差异

def test_next_semester_label():
    assert next_semester_label("2026-SPRING", 1) == "2026-FALL"
    assert next_semester_label("2026-SPRING", 2) == "2027-SPRING"
    assert next_semester_label("2026-FALL", 3) == "2028-SPRING"


def test_remaining_diff_aligns_semesters_and_summarizes():
    original = {"semesters": [
        {"semester": "2026-SPRING", "courses": [{"code": "CS101"}, {"code": "MATH103"}]},
        {"semester": "2026-FALL", "courses": [{"code": "CS201"}]},
        {"semester": "2027-SPRING", "courses": [{"code": "CS301"}]},
    ]}
    new = {"semesters": [
        {"semester": "2026-FALL", "courses": [{"code": "CS201"}, {"code": "MATH103"}]},
        {"semester": "2027-SPRING", "courses": [{"code": "CS301"}]},
        {"semester": "2027-FALL", "courses": [{"code": "CS401"}]},
    ]}
    diff = remaining_diff(original, new, elapsed_semesters=1)
    assert diff["semesters"] == [
        {"semester": "2026-FALL", "added_courses": ["MATH103"], "removed_courses": []},
        {"semester": "2027-FALL", "added_courses": ["CS401"], "removed_courses": []},
    ]
    assert diff["added_courses"] == ["CS401", "MATH103"]
    assert diff["removed_courses"] == []


# ---------------------------------------------------------------- API：成功路径

def test_replan_creates_draft_linked_to_confirmed_plan(client):
    plan = _create_confirmed(client)
    r = client.post(f"/plans/{plan['plan_id']}/replan", json={
        "elapsed_semesters": 2,
        "completed_courses": ["CS101", "CS101L", "CS201", "MATH103"],
    })
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["status"] == "draft"
    assert draft["source_plan_id"] == plan["plan_id"]
    assert draft["catalog_version"] == 1

    # 剩余 4 学期，从 2027-SPRING 起；已修课程不重复排入
    req = draft["request"]
    assert req["start_semester"] == "2027-SPRING"
    assert req["num_semesters"] == 4
    sems = [[c["code"] for c in s["courses"]] for s in draft["result"]["semesters"]]
    assert sems == [["CS301"], ["CS401"]]

    # 与原方案剩余学期（原第 3、4 学期）的差异：课表完全一致，无差异
    diff = draft["replan"]["difference"]
    assert diff["semesters"] == []
    assert diff["added_courses"] == [] and diff["removed_courses"] == []

    # 原方案及其结果不变、仍为 confirmed
    orig = client.get(f"/plans/{plan['plan_id']}").json()
    assert orig["status"] == "confirmed"
    assert "replan" not in orig
    assert len(orig["result"]["semesters"]) == 4

    # 列表可见来源关联
    items = {p["plan_id"]: p for p in client.get("/plans").json()}
    assert items[draft["plan_id"]]["source_plan_id"] == plan["plan_id"]
    assert "source_plan_id" not in items[plan["plan_id"]]

    # 草案可按既有约定确认；确认不影响原方案
    r = client.post(f"/plans/{draft['plan_id']}/confirm")
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"
    assert client.get(f"/plans/{plan['plan_id']}").json()["status"] == "confirmed"


def test_replan_with_extra_completed_advances_completion(client):
    # 原方案：空已修，CS401，6 学期；执行 1 学期后已额外修掉 MATH103
    plan = _create_confirmed(client)
    r = client.post(f"/plans/{plan['plan_id']}/replan", json={
        "elapsed_semesters": 1,
        "completed_courses": ["CS101", "CS101L", "MATH103"],
    })
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["request"]["start_semester"] == "2026-FALL"
    assert draft["request"]["num_semesters"] == 5
    sems = [[c["code"] for c in s["courses"]] for s in draft["result"]["semesters"]]
    assert sems == [["CS201"], ["CS301"], ["CS401"]]
    # 原第 2 学期的 MATH103 被移除
    fall = next(s for s in draft["replan"]["difference"]["semesters"]
                if s["semester"] == "2026-FALL")
    assert fall["removed_courses"] == ["MATH103"]


def test_all_targets_completed_produces_empty_draft(client):
    plan = _create_confirmed(client)
    r = client.post(f"/plans/{plan['plan_id']}/replan", json={
        "elapsed_semesters": 1,
        "completed_courses": ["CS101", "CS101L", "CS201", "MATH103", "CS301", "CS401"],
    })
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["status"] == "draft"
    assert draft["result"]["semesters"] == []
    assert draft["result"]["completion_semester"] is None


# ---------------------------------------------------------------- API：拒绝路径

def test_replan_unknown_plan_404(client):
    r = client.post("/plans/nope/replan", json={"elapsed_semesters": 1, "completed_courses": []})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NOT_FOUND"


def test_replan_requires_confirmed_plan(client):
    plan = client.post("/plans", json=plan_payload()).json()  # 未确认
    r = client.post(f"/plans/{plan['plan_id']}/replan",
                    json={"elapsed_semesters": 1, "completed_courses": []})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PLAN_NOT_CONFIRMED"
    assert client.get("/plans").json()  # 未产生草案
    assert all(p["plan_id"] != plan["plan_id"] or p["status"] == "proposed"
               for p in client.get("/plans").json())


def test_replan_elapsed_must_be_within_horizon(client):
    plan = _create_confirmed(client)
    # pydantic 层：非正
    r = client.post(f"/plans/{plan['plan_id']}/replan",
                    json={"elapsed_semesters": 0, "completed_courses": []})
    assert r.status_code == 422
    # 业务层：达到/超过原规划期限（原 6 学期）
    r = client.post(f"/plans/{plan['plan_id']}/replan",
                    json={"elapsed_semesters": 6, "completed_courses": []})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_INPUT"


def test_replan_cannot_revoke_declared_completed(client):
    plan = _create_confirmed(client, completed_courses=["CS101", "CS101L"])
    r = client.post(f"/plans/{plan['plan_id']}/replan", json={
        "elapsed_semesters": 1,
        "completed_courses": ["CS101"],  # 撤销了原声明的 CS101L
    })
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["code"] == "COMPLETED_REVOKED"
    assert "CS101L" in body["message"]
    # 不保存草案
    assert all(p["status"] != "draft" for p in client.get("/plans").json())


def test_replan_no_solution_returns_reason_and_saves_nothing(client):
    plan = _create_confirmed(client)
    # 执行 5 学期后仅剩 1 学期，但 CS301/CS401 都未修，1 学期排不完
    r = client.post(f"/plans/{plan['plan_id']}/replan", json={
        "elapsed_semesters": 5,
        "completed_courses": ["CS101", "CS101L"],
    })
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "NO_SOLUTION"
    assert all(p["status"] != "draft" for p in client.get("/plans").json())


def test_replan_against_revised_catalog_uses_current_version(client):
    # 原方案基于版本 1 并已确认；目录修订为版本 2（CS401 变 4 学分）
    plan = _create_confirmed(client)
    current = client.get("/catalog/current").json()
    courses = current["courses"]
    cs401 = next(c for c in courses if c["code"] == "CS401")
    cs401["credits"] = 4
    r = client.post("/catalog/versions", json={"note": "v2", "courses": courses})
    assert r.status_code == 201

    r = client.post(f"/plans/{plan['plan_id']}/replan", json={
        "elapsed_semesters": 3,
        "completed_courses": ["CS101", "CS101L", "CS201", "MATH103", "CS301"],
    })
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["catalog_version"] == 2
    assert draft["result"]["semesters"][-1]["total_credits"] == 4
    # 草案基于新版本，可确认；原方案仍绑定版本 1
    assert client.post(f"/plans/{draft['plan_id']}/confirm").status_code == 200
    assert client.get(f"/plans/{plan['plan_id']}").json()["catalog_version"] == 1


def test_replan_target_missing_after_revision_rejected(client):
    plan = _create_confirmed(client)
    current = client.get("/catalog/current").json()
    courses = [c for c in current["courses"] if c["code"] != "CS401"]  # 目标课被移除
    client.post("/catalog/versions", json={"note": "v2", "courses": courses})
    r = client.post(f"/plans/{plan['plan_id']}/replan", json={
        "elapsed_semesters": 2,
        "completed_courses": ["CS101", "CS101L", "CS201", "MATH103"],
    })
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "COURSE_MISSING"
    assert all(p["status"] != "draft" for p in client.get("/plans").json())


def test_replan_rejects_stale_result_when_catalog_changes_during_compute(client, monkeypatch):
    plan = _create_confirmed(client)

    import app.main as main_mod
    original_get_current = main_mod.db.get_current_version
    calls = {"n": 0}

    def current_with_concurrent_revision(conn):
        row = original_get_current(conn)
        calls["n"] += 1
        if calls["n"] == 2:
            # 第二次取“当前版本”（落库前复查）：模拟计算期间目录被修订
            return dict(row) | {"id": row["id"] + 1}
        return row

    monkeypatch.setattr(main_mod.db, "get_current_version", current_with_concurrent_revision)
    r = client.post(f"/plans/{plan['plan_id']}/replan", json={
        "elapsed_semesters": 2,
        "completed_courses": ["CS101", "CS101L", "CS201", "MATH103"],
    })
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "CATALOG_CHANGED"
    # 旧版本结果不落库
    rows = client.get("/plans").json()
    assert all(not (p["status"] == "draft") for p in rows)
