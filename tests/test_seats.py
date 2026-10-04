"""席位上限、席位余量规划、确认原子占位与重排占位替换的集成测试。"""
import os

import pytest
from fastapi.testclient import TestClient


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


def set_cap(client, course, semester, cap):
    return client.put(f"/seat-caps/{course}/{semester}", json={"cap": cap})


def create_confirmed(client, **kw):
    r = client.post("/plans", json=plan_payload(**kw))
    assert r.status_code == 201, r.text
    plan = r.json()
    r = client.post(f"/plans/{plan['plan_id']}/confirm")
    assert r.status_code == 200, r.text
    return plan


# ---------------------------------------------------------------- 上限设置与查询

def test_set_cap_returns_cap_occupied_remaining(client):
    r = set_cap(client, "CS401", "2027-FALL", 3)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"course_code": "CS401", "semester": "2027-FALL",
                    "cap": 3, "occupied": 0, "remaining": 3}
    # 幂等覆盖
    r = set_cap(client, "cs401", "2027-fall", 5)  # 大小写归一
    assert r.status_code == 200
    assert r.json()["cap"] == 5


def test_unset_cap_means_unlimited(client):
    r = client.get("/seat-caps/CS401/2027-FALL")
    assert r.status_code == 200
    body = r.json()
    assert body["cap"] is None
    assert body["remaining"] is None
    assert body["occupied"] == 0
    assert client.get("/seat-caps").json()["seat_caps"] == []


def test_zero_cap_allowed_when_unoccupied(client):
    r = set_cap(client, "CS301", "2027-SPRING", 0)
    assert r.status_code == 200
    assert r.json()["remaining"] == 0


def test_negative_cap_rejected_422(client):
    r = client.put("/seat-caps/CS401/2027-FALL", json={"cap": -1})
    assert r.status_code == 422


def test_set_cap_unknown_course_rejected(client):
    r = set_cap(client, "NOPE101", "2027-FALL", 2)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "COURSE_MISSING"


def test_query_cap_unknown_course_rejected(client):
    assert client.get("/seat-caps/NOPE101/2027-FALL").status_code == 422


def test_set_cap_bad_semester_rejected(client):
    r = set_cap(client, "CS401", "2027春", 2)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_INPUT"
    assert client.get("/seat-caps").json()["seat_caps"] == []


def test_cap_below_occupied_rejected_and_keeps_old_cap(client):
    # 一个方案占 1 个 CS401@2027-FALL
    create_confirmed(client)
    r = client.get("/seat-caps/CS401/2027-FALL").json()
    assert r["occupied"] == 1
    # 上限 0 < 已占用 1：拒绝
    r = set_cap(client, "CS401", "2027-FALL", 0)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "CAP_BELOW_OCCUPIED"
    assert r.json()["detail"]["occupied"] == 1
    # 原上限（未设置=不限额）保持
    assert client.get("/seat-caps/CS401/2027-FALL").json()["cap"] is None
    # 上限等于已占用可以设置
    assert set_cap(client, "CS401", "2027-FALL", 1).status_code == 200


# ---------------------------------------------------------------- 规划遵循余量

def test_plan_respects_remaining_capacity(client):
    # CS401 在 2027-FALL 仅 1 个名额
    set_cap(client, "CS401", "2027-FALL", 1)
    create_confirmed(client)  # 占用了唯一名额

    # 再次规划：默认最早解 2027-FALL 已无余位，CS401 仅 FALL 开课 -> 顺延到 2028-FALL
    r = client.post("/plans", json=plan_payload())
    assert r.status_code == 201, r.text
    sems = [[c["code"] for c in s["courses"]] for s in r.json()["result"]["semesters"]]
    assert sems[-1] == ["CS401"]
    assert r.json()["result"]["completion_semester"] == "2028-FALL"


def test_zero_cap_blocks_semester(client):
    set_cap(client, "CS101", "2026-SPRING", 0)
    r = client.post("/plans", json=plan_payload())
    assert r.status_code == 201, r.text
    sems = {s["semester"]: [c["code"] for c in s["courses"]]
            for s in r.json()["result"]["semesters"]}
    # CS101 被挤出 2026-SPRING 后，CS101 路径不再字典序最优，
    # 引擎改走 MATH101 -> CS202 先修路径（CS301 要求 CS201 或 CS202）绕开席位限制
    assert "CS101" not in sems["2026-SPRING"]
    all_codes = {c for s in sems.values() for c in s}
    assert "CS401" in all_codes and "CS101" not in all_codes
    assert r.json()["result"]["completion_semester"] == "2027-FALL"


def test_zero_cap_forces_delay_when_no_alternative_leaf(client):
    # 目标 CS201 必须经 CS101+CS101L（同修块），无替代路径：第一学期空过，整体顺延
    set_cap(client, "CS101", "2026-SPRING", 0)
    r = client.post("/plans", json=plan_payload(target_courses=["CS201"], num_semesters=4))
    assert r.status_code == 201, r.text
    sems = {s["semester"]: [c["code"] for c in s["courses"]]
            for s in r.json()["result"]["semesters"]}
    assert sems["2026-SPRING"] == []
    assert sems["2026-FALL"] == ["CS101", "CS101L"]
    assert sems["2027-SPRING"] == ["CS201"]


def test_capacity_shortage_no_solution_with_reason(client):
    # CS401 所有可排 FALL 学期都封死 -> 无可行解；说明原因且不保存方案
    set_cap(client, "CS401", "2027-FALL", 0)
    set_cap(client, "CS401", "2028-FALL", 0)
    r = client.post("/plans", json=plan_payload())
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["code"] == "NO_SOLUTION"
    assert "席位" in body["message"] and "CS401" in body["message"]
    assert client.get("/plans").json() == []


def test_unconfirmed_plan_does_not_hold_seat(client):
    set_cap(client, "CS401", "2027-FALL", 1)
    r = client.post("/plans", json=plan_payload())
    assert r.status_code == 201
    # 尚未确认：不占位
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 0
    # 待确认方案不占位，仍可再规划出同样 2027-FALL 的课表
    r2 = client.post("/plans", json=plan_payload())
    assert r2.json()["result"]["completion_semester"] == "2027-FALL"


# ---------------------------------------------------------------- 确认原子占位与超卖

def test_confirm_holds_seat_for_actual_courses(client):
    plan = create_confirmed(client)
    info = client.get("/seat-caps/CS401/2027-FALL").json()
    assert info["occupied"] == 1
    # 占位按方案实际修读课程
    sems = {c["code"]: s["semester"] for s in plan["result"]["semesters"]
            for c in s["courses"]}
    assert sems["CS301"] == "2027-SPRING"
    assert client.get("/seat-caps/CS301/2027-SPRING").json()["occupied"] == 1


def test_confirm_rejected_on_sold_out_and_keeps_plan_and_holds(client):
    # 两个待确认方案都落在 CS401@2027-FALL（草案不占位，互不可见）
    plan_a = client.post("/plans", json=plan_payload()).json()
    plan_b = client.post("/plans", json=plan_payload()).json()
    set_cap(client, "CS401", "2027-FALL", 1)

    # 确认 A 成功占位
    assert client.post(f"/plans/{plan_a['plan_id']}/confirm").status_code == 200
    # 确认 B：余量不足，拒绝，原方案（proposed）保留，席位不超卖
    r = client.post(f"/plans/{plan_b['plan_id']}/confirm")
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["code"] == "SEAT_CONFLICT"
    assert body["conflicts"][0]["course_code"] == "CS401"
    assert body["conflicts"][0]["semester"] == "2027-FALL"
    assert body["conflicts"][0]["remaining"] == 0
    assert client.get(f"/plans/{plan_b['plan_id']}").json()["status"] == "proposed"
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 1


def test_confirm_idempotent_does_not_double_hold(client):
    plan = create_confirmed(client)
    r = client.post(f"/plans/{plan['plan_id']}/confirm")
    assert r.status_code == 200
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 1


# ---------------------------------------------------------------- 重排占位替换

def _replan_draft(client, plan_id, elapsed, completed):
    r = client.post(f"/plans/{plan_id}/replan", json={
        "elapsed_semesters": elapsed, "completed_courses": completed})
    assert r.status_code == 201, r.text
    return r.json()


def test_replan_confirm_replaces_only_unfinished_holds(client):
    plan = create_confirmed(client)
    # 原方案：CS201/MATH103@2026-FALL、CS301@2027-SPRING、CS401@2027-FALL
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 1

    # 执行 2 学期后第 2 学期课程未修完：草案从 2027-SPRING 起，顺延为
    # 2027-SPRING 空过、CS201/MATH103@2027-FALL、CS301@2028-SPRING、CS401@2028-FALL
    draft = _replan_draft(
        client, plan["plan_id"], 2, ["CS101", "CS101L"])
    sems = [[c["code"] for c in s["courses"]] for s in draft["result"]["semesters"]]
    assert sems == [[], ["CS201", "MATH103"], ["CS301"], ["CS401"]]
    # 草案不占位
    assert client.get("/seat-caps/CS401/2028-FALL").json()["occupied"] == 0
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 1

    # 确认：替换来源方案未结束学期（第 3 学期起）占位，已结束学期占位保留
    assert client.post(f"/plans/{draft['plan_id']}/confirm").status_code == 200
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 0
    assert client.get("/seat-caps/CS401/2028-FALL").json()["occupied"] == 1
    assert client.get("/seat-caps/CS301/2027-SPRING").json()["occupied"] == 0
    assert client.get("/seat-caps/CS301/2028-SPRING").json()["occupied"] == 1
    assert client.get("/seat-caps/CS201/2027-FALL").json()["occupied"] == 1
    # 已结束学期（第 1、2 学期）的占位保留
    assert client.get("/seat-caps/CS101/2026-SPRING").json()["occupied"] == 1
    assert client.get("/seat-caps/CS201/2026-FALL").json()["occupied"] == 1


def test_replan_confirm_seat_conflict_rolls_back_deleted_holds(client):
    plan = create_confirmed(client)
    # 同上的顺延草案：CS401 移到来源方案原本没有占位的 2028-FALL
    draft = _replan_draft(
        client, plan["plan_id"], 2, ["CS101", "CS101L"])
    assert draft["result"]["completion_semester"] == "2028-FALL"
    # 草案生成后、确认前，2028-FALL 的 CS401 名额被封（当前占用 0，可设 0）
    assert set_cap(client, "CS401", "2028-FALL", 0).status_code == 200
    r = client.post(f"/plans/{draft['plan_id']}/confirm")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SEAT_CONFLICT"
    # 草案仍为 draft；事务回滚：确认时删除的来源未来占位全部恢复
    assert client.get(f"/plans/{draft['plan_id']}").json()["status"] == "draft"
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 1
    assert client.get("/seat-caps/CS301/2027-SPRING").json()["occupied"] == 1
    assert client.get("/seat-caps/CS401/2028-FALL").json()["occupied"] == 0


def test_only_one_active_replan_per_source(client):
    plan = create_confirmed(client)
    draft1 = _replan_draft(
        client, plan["plan_id"], 2,
        ["CS101", "CS101L", "CS201", "MATH103"])
    draft2 = _replan_draft(
        client, plan["plan_id"], 2,
        ["CS101", "CS101L", "CS201", "MATH103"])
    # 确认 draft1 生效
    assert client.post(f"/plans/{draft1['plan_id']}/confirm").status_code == 200
    # 再确认 draft2：同一来源已有生效重排 -> 过期拒绝
    r = client.post(f"/plans/{draft2['plan_id']}/confirm")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "DRAFT_EXPIRED"
    assert client.get(f"/plans/{draft2['plan_id']}").json()["status"] == "draft"
    # 重复确认已生效的 draft1 幂等
    r = client.post(f"/plans/{draft1['plan_id']}/confirm")
    assert r.status_code == 200
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 1


def test_stale_draft_after_catalog_revision_rejected(client):
    plan = create_confirmed(client)
    draft = _replan_draft(
        client, plan["plan_id"], 2,
        ["CS101", "CS101L", "CS201", "MATH103"])
    # 修订目录使草案过期
    current = client.get("/catalog/current").json()
    client.post("/catalog/versions", json={"note": "v2", "courses": current["courses"]})
    r = client.post(f"/plans/{draft['plan_id']}/confirm")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "CATALOG_CHANGED"
    assert client.get(f"/plans/{draft['plan_id']}").json()["status"] == "draft"
    # 原占位不受影响
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 1


def test_replan_planning_ignores_own_unfinished_holds(client):
    # 来源方案自身占满 CS401@2027-FALL；重排仍可把 CS401 排回 2027-FALL
    # （自身未来占位确认时会被替换，不应被当作他人占用）
    plan = create_confirmed(client)
    set_cap(client, "CS401", "2027-FALL", 1)
    draft = _replan_draft(
        client, plan["plan_id"], 2,
        ["CS101", "CS101L", "CS201", "MATH103"])
    assert draft["result"]["completion_semester"] == "2027-FALL"
    assert client.post(f"/plans/{draft['plan_id']}/confirm").status_code == 200
    # 替换后仍只占 1 个名额（未叠加）
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 1


# ---------------------------------------------------------------- 互认按实际修读占位

def test_equivalence_holds_actual_course_not_original(client):
    # 构造互认目录：CS401 与 CS402 同组
    current = client.get("/catalog/current").json()
    courses = current["courses"]
    payload = {"note": "equiv", "courses": courses,
               "equivalence_groups": [["CS301", "CS302"], ["CS401", "CS402"]]}
    r = client.post("/catalog/versions", json=payload)
    assert r.status_code == 201

    # 已修 CS301 等，目标 CS401 -> 实际择修春季开课的 CS402
    plan = create_confirmed(client, completed_courses=[
        "CS101", "CS101L", "CS201", "MATH101", "MATH102", "MATH103", "CS301"])
    sems = {c["code"]: s["semester"] for s in plan["result"]["semesters"]
            for c in s["courses"]}
    assert "CS402" in sems and "CS401" not in sems
    # 替代课 CS402 占位；原课 CS401 不占位
    assert client.get(f"/seat-caps/CS402/{sems['CS402']}").json()["occupied"] == 1
    assert client.get("/seat-caps/CS401/2026-SPRING").json()["occupied"] == 0
