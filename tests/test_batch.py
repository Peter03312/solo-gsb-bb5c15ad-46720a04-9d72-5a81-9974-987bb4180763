"""批次联合排课 HTTP API 集成测试（每次测试使用独立临时数据库）。"""
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    from app.main import app
    with TestClient(app) as c:
        yield c


def app_payload(app_id, **kw):
    payload = {
        "application_id": app_id,
        "completed_courses": [],
        "target_courses": ["CS401"],
        "start_semester": "2026-SPRING",
        "max_credits_per_semester": 12,
        "num_semesters": 6,
    }
    payload.update(kw)
    return payload


def batch(*apps):
    return {"applications": list(apps)}


def set_cap(client, course, semester, cap):
    return client.put(f"/seat-caps/{course}/{semester}", json={"cap": cap})


def create_batch(client, payload):
    r = client.post("/plan-batches", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def members_by_id(body):
    return {m["application_id"]: m for m in body["applications"]}


# ---------------------------------------------------------------- 基本联合排课

def test_batch_joint_schedule_no_competition(client):
    # 无席位上限：两份都排出各自最早课表，互不影响
    body = create_batch(client, batch(app_payload("A"), app_payload("B")))
    assert body["status"] == "proposed"
    assert body["catalog_version"] == 1
    assert body["selected_count"] == 2
    ms = members_by_id(body)
    assert list(members_by_id(body)) == ["A", "B"]  # 按申请标识升序返回
    for m in ms.values():
        assert m["selected"] is True
        assert m["plan_id"]
        assert m["result"]["completion_semester"] == "2027-FALL"
    # 可回看，含绑定目录版本
    got = client.get(f"/plan-batches/{body['batch_id']}").json()
    assert got["batch_id"] == body["batch_id"]
    assert client.get("/plan-batches").json()["batches"][0]["batch_id"] == body["batch_id"]


def test_batch_does_not_prehold_seats(client):
    body = create_batch(client, batch(app_payload("A")))
    pid = members_by_id(body)["A"]["plan_id"]
    # 批次待确认：成员课表均不占位
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 0
    assert client.get(f"/plans/{pid}").json()["status"] == "proposed"
    assert client.get(f"/plans/{pid}").json()["batch_id"] == body["batch_id"]


def test_batch_member_plans_appear_in_plan_list(client):
    body = create_batch(client, batch(app_payload("A"), app_payload("B")))
    plans = client.get("/plans").json()
    ids = {m["plan_id"] for m in body["applications"]}
    listed = {p["plan_id"] for p in plans if p.get("batch_id") == body["batch_id"]}
    assert listed == ids


# ---------------------------------------------------------------- 席位竞争与最大化入选数

def test_batch_maximizes_complete_count(client):
    # CS401 仅 2027-FALL 有 1 个名额、2028-FALL 封死：6 学期内两份申请只有一份可完整
    set_cap(client, "CS401", "2027-FALL", 1)
    set_cap(client, "CS401", "2028-FALL", 0)
    body = create_batch(client, batch(app_payload("B"), app_payload("A")))
    assert body["selected_count"] == 1
    ms = members_by_id(body)
    assert ms["A"]["selected"] is True
    assert ms["A"]["result"]["completion_semester"] == "2027-FALL"
    assert ms["B"]["selected"] is False
    assert ms["B"]["reason_code"] == "BATCH_CAPACITY"
    assert "CS401@2027-FALL" in ms["B"]["reason"]


def test_batch_joint_demand_must_not_exceed_cap_but_can_shift(client):
    # 上限 1 但 2028-FALL 仍开放：两份都入选，一个 2027-FALL、一个顺延 2028-FALL
    set_cap(client, "CS401", "2027-FALL", 1)
    body = create_batch(client, batch(app_payload("A"), app_payload("B")))
    assert body["selected_count"] == 2
    comps = sorted(m["result"]["completion_semester"] for m in body["applications"])
    assert comps == ["2027-FALL", "2028-FALL"]
    # 批次总需求：任一学期 CS401 只占 1
    r = client.post(f"/plan-batches/{body['batch_id']}/confirm")
    assert r.status_code == 200
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 1


def test_individually_infeasible_application_is_unselected_with_reason(client):
    # A 正常；B 期限内无解（1 学期不可能排完 CS401 闭包）
    body = create_batch(client, batch(
        app_payload("A"),
        app_payload("B", num_semesters=1)))
    assert body["selected_count"] == 1
    ms = members_by_id(body)
    assert ms["A"]["selected"] is True
    assert ms["B"]["selected"] is False
    assert ms["B"]["reason_code"] == "NO_SOLUTION"
    assert ms["B"]["reason"]


def test_infeasible_app_does_not_consume_batch_seats(client):
    # B 无解时 A 不受影响，照常排入 2027-FALL（cap=1 恰好够 A 一份）
    set_cap(client, "CS401", "2027-FALL", 1)
    body = create_batch(client, batch(
        app_payload("A"),
        app_payload("B", num_semesters=1)))
    assert members_by_id(body)["A"]["result"]["completion_semester"] == "2027-FALL"
    r = client.post(f"/plan-batches/{body['batch_id']}/confirm")
    assert r.status_code == 200
    assert client.get("/seat-caps/CS401/2027-FALL").json()["remaining"] == 0


def test_missing_course_application_rejects_whole_batch(client):
    # 未知目标课程属于申请参数非法（引用不存在课程）：整批拒绝、不保存
    r = client.post("/plan-batches", json=batch(
        app_payload("A"),
        app_payload("B", target_courses=["NOPE101"])))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "COURSE_MISSING"
    assert client.get("/plan-batches").json()["batches"] == []


# ---------------------------------------------------------------- 决胜规则

def test_tie_break_completion_semester_favors_smaller_id(client):
    # 同一唯一名额：A、B 独立最早课表完全相同，最大化数为 1；
    # 按申请标识升序比较完成学期 -> A 入选（占早学期），B 未入选
    set_cap(client, "CS401", "2027-FALL", 1)
    set_cap(client, "CS401", "2028-FALL", 0)
    body = create_batch(client, batch(app_payload("B"), app_payload("A")))
    ms = members_by_id(body)
    assert ms["A"]["selected"] is True
    assert ms["B"]["selected"] is False

    # 反向标识：Z 与 M 竞争时 M 入选
    body2 = create_batch(client, batch(app_payload("Z"), app_payload("M")))
    ms2 = members_by_id(body2)
    assert ms2["M"]["selected"] is True
    assert ms2["Z"]["selected"] is False


def test_tie_break_sequence_lexicographic_by_id():
    """完成学期向量相同、只是课程代码序列不同的情形，引擎按标识升序取字典序最小。"""
    import os as _os
    _os.environ["DB_PATH"] = "/tmp/tiebreak_engine.db"
    from app.batch_planner import build_batch
    from app.planner import Course

    # 构造一个最小目录：X/Y 都在春季开、无先修，各 1 学分；同一名额 spring-1 只能给一门
    catalog = {
        "X": Course("X", "X", 1, frozenset({"SPRING"}), (), ()),
        "Y": Course("Y", "Y", 1, frozenset({"SPRING"}), (), ()),
    }
    seat_caps = {("X", "2026-SPRING"): 1}
    apps = [
        {"application_id": "A", "completed_courses": [], "target_courses": ["X"],
         "start_semester": "2026-SPRING", "max_credits_per_semester": 12,
         "num_semesters": 2, "locked": [], "unavailable_semesters": []},
        {"application_id": "B", "completed_courses": [], "target_courses": ["X"],
         "start_semester": "2026-SPRING", "max_credits_per_semester": 12,
         "num_semesters": 2, "locked": [], "unavailable_semesters": []},
    ]
    res = build_batch(catalog, apps, equiv_groups=[],
                      seat_caps=seat_caps, occupied={})
    assert res["results"]["A"]["option"] is not None
    assert res["results"]["B"]["option"] is None
    seq_a = res["results"]["A"]["option"].seq
    assert seq_a == (("X",),)


def test_enumeration_has_no_spurious_delayed_completions():
    """枚举的每份可行课表只在其真实完成期限出现一次：不应含尾随空学期的重复方案。"""
    from app.batch_planner import build_application_options
    from app.planner import Course, _equiv_map

    catalog = {
        "X": Course("X", "X", 1, frozenset({"SPRING", "FALL"}), (), ()),
        "Y": Course("Y", "Y", 1, frozenset({"SPRING", "FALL"}), (), ()),
    }
    app = {"application_id": "A", "completed_courses": [], "target_courses": ["X", "Y"],
           "start_semester": "2026-SPRING", "max_credits_per_semester": 12,
           "num_semesters": 4, "locked": [], "unavailable_semesters": []}
    res = build_application_options(
        catalog, app, equiv=_equiv_map([]), seat_caps={}, occupied={}, counter=[0])
    assert res["error"] is None
    # 两门课同学期即可完成（k=1），唯一完成学期是 2026-SPRING；
    # 不得枚举出“排完后空过若干学期”的 k=2/3/4 重复方案
    ranks = {o.rank for o in res["options"]}
    assert ranks == {2026 * 2}
    assert {o.seq for o in res["options"]} == {(("X", "Y"),)}


def test_tie_break3_sequence_lexicographic_picks_smaller_sequence_for_smaller_id():
    """第三级决胜：最大入选数与完成学期向量均相同的组合间，按标识升序比较课表序列。

    目录：C、D 均春秋开课、无先修；互为互认组。C@2026-SPRING 上限 1。
    A 目标 C（可互认修 D），B 目标 C（可互认修 D）。
    两份都能在春季完成的组合只有一种：一份修 C、一份修 D（C 名额唯一）。
    - 组合 X：A 修 C（序列 ("C",)）、B 修 D（序列 ("D",)）
    - 组合 Y：A 修 D、B 修 C
    两者入选数（=2）与完成学期向量（春,春）完全相同；按标识升序比较序列，
    ("C",) < ("D",)，唯一结果为组合 X：A 修 C、B 修 D。
    """
    import os as _os
    _os.environ["DB_PATH"] = "/tmp/tiebreak3_engine.db"
    from app.batch_planner import build_batch
    from app.planner import Course

    catalog = {
        "C": Course("C", "C", 1, frozenset({"SPRING", "FALL"}), (), ()),
        "D": Course("D", "D", 1, frozenset({"SPRING", "FALL"}), (), ()),
    }
    seat_caps = {("C", "2026-SPRING"): 1}

    def mk(aid):
        return {"application_id": aid, "completed_courses": [], "target_courses": ["C"],
                "start_semester": "2026-SPRING", "max_credits_per_semester": 12,
                "num_semesters": 2, "locked": [], "unavailable_semesters": []}

    res = build_batch(catalog, [mk("A"), mk("B")], equiv_groups=[["C", "D"]],
                      seat_caps=seat_caps, occupied={})
    assert res["results"]["A"]["option"].seq == (("C",),)
    assert res["results"]["B"]["option"].seq == (("D",),)

    # 标识顺序对调后仍由“较小标识取更小序列”决定（提交顺序不影响唯一结果）
    res2 = build_batch(catalog, [mk("B"), mk("A")], equiv_groups=[["C", "D"]],
                       seat_caps=seat_caps, occupied={})
    assert res2["results"]["A"]["option"].seq == (("C",),)
    assert res2["results"]["B"]["option"].seq == (("D",),)


def test_batch_three_apps_two_slots_selects_two_with_lexicographic_drop(client):
    # 三个相同申请；CS401 只在 2027-FALL(cap1)、2028-FALL(cap1) 有名额，
    # 6 学期内至多 2 份完整 -> 最大化数为 2，标识最大的 C 未入选
    set_cap(client, "CS401", "2027-FALL", 1)
    set_cap(client, "CS401", "2028-FALL", 1)
    body = create_batch(client, batch(
        app_payload("C"), app_payload("A"), app_payload("B")))
    assert body["selected_count"] == 2
    ms = members_by_id(body)
    assert ms["A"]["selected"] and ms["B"]["selected"]
    assert ms["C"]["selected"] is False
    assert ms["C"]["reason_code"] == "BATCH_CAPACITY"
    comps = sorted([ms["A"]["result"]["completion_semester"],
                    ms["B"]["result"]["completion_semester"]])
    assert comps == ["2027-FALL", "2028-FALL"]


# ---------------------------------------------------------------- 整批确认

def test_batch_confirm_atomically_holds_all(client):
    set_cap(client, "CS401", "2027-FALL", 1)
    body = create_batch(client, batch(app_payload("A"), app_payload("B")))
    r = client.post(f"/plan-batches/{body['batch_id']}/confirm")
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"
    for m in body["applications"]:
        assert client.get(f"/plans/{m['plan_id']}").json()["status"] == "confirmed"
    # A 占 2027-FALL、B 顺延 2028-FALL
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 1
    assert client.get("/seat-caps/CS401/2028-FALL").json()["occupied"] == 1


def test_batch_confirm_idempotent(client):
    body = create_batch(client, batch(app_payload("A")))
    assert client.post(f"/plan-batches/{body['batch_id']}/confirm").status_code == 200
    assert client.post(f"/plan-batches/{body['batch_id']}/confirm").status_code == 200
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 1


def test_batch_confirm_rejected_catalog_changed(client):
    body = create_batch(client, batch(app_payload("A")))
    cur = client.get("/catalog/current").json()
    client.post("/catalog/versions", json={"note": "v2", "courses": cur["courses"]})
    r = client.post(f"/plan-batches/{body['batch_id']}/confirm")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "CATALOG_CHANGED"
    # 批次与成员课表仍为待确认，原占位（无）不变
    assert client.get(f"/plan-batches/{body['batch_id']}").json()["status"] == "proposed"
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 0


def test_batch_confirm_seat_conflict_rolls_back(client):
    # 批次里 A 占 2027-FALL；批次生成后另一个已确认单份方案先占掉同一名额，
    # 整批确认 -> SEAT_CONFLICT 回滚，成员不占位、批次仍 proposed
    set_cap(client, "CS401", "2027-FALL", 1)
    set_cap(client, "CS401", "2028-FALL", 0)  # 封死顺延学期，使冲突必然发生
    body = create_batch(client, batch(app_payload("A")))
    other = client.post("/plans", json={
        "completed_courses": [], "target_courses": ["CS401"],
        "start_semester": "2026-SPRING", "max_credits_per_semester": 12,
        "num_semesters": 6}).json()
    assert client.post(f"/plans/{other['plan_id']}/confirm").status_code == 200
    r = client.post(f"/plan-batches/{body['batch_id']}/confirm")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "SEAT_CONFLICT"
    assert detail["conflicts"][0]["course_code"] == "CS401"
    assert detail["conflicts"][0]["remaining"] == 0
    # 回滚：批次仍待确认、成员未确认、原占位仅 1（不超卖）
    assert client.get(f"/plan-batches/{body['batch_id']}").json()["status"] == "proposed"
    pid = members_by_id(body)["A"]["plan_id"]
    assert client.get(f"/plans/{pid}").json()["status"] == "proposed"
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 1


def test_member_plan_cannot_be_confirmed_alone(client):
    body = create_batch(client, batch(app_payload("A")))
    pid = members_by_id(body)["A"]["plan_id"]
    r = client.post(f"/plans/{pid}/confirm")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "BATCH_CONFIRM_REQUIRED"
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 0


# ---------------------------------------------------------------- 整批拒绝

def test_duplicate_application_id_rejects_whole_batch(client):
    r = client.post("/plan-batches", json=batch(
        app_payload("A"), app_payload(" A ")))  # 去空白后重复
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_INPUT"
    assert client.get("/plan-batches").json()["batches"] == []


def test_empty_application_id_rejected(client):
    r = client.post("/plan-batches", json=batch(app_payload("   ")))
    assert r.status_code == 422
    assert client.get("/plan-batches").json()["batches"] == []


def test_empty_batch_rejected(client):
    assert client.post("/plan-batches", json={"applications": []}).status_code == 422


@pytest.mark.parametrize("field,value", [
    ("target_courses", []),
    ("max_credits_per_semester", 0),
    ("num_semesters", -2),
])
def test_illegal_application_params_reject_whole_batch(client, field, value):
    r = client.post("/plan-batches", json=batch(app_payload("A", **{field: value})))
    assert r.status_code == 422
    assert client.get("/plan-batches").json()["batches"] == []


def test_bad_semester_rejects_whole_batch(client):
    r = client.post("/plan-batches", json=batch(app_payload("A", start_semester="2026春")))
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_INPUT"
    assert client.get("/plan-batches").json()["batches"] == []


def test_no_schedulable_application_means_no_solution_and_not_saved(client):
    r = client.post("/plan-batches", json=batch(
        app_payload("A", num_semesters=1),
        app_payload("B", target_courses=["CS999"])))  # 条件冲突
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "NO_SOLUTION"
    codes = {a["reason_code"] for a in detail["applications"]}
    assert codes == {"NO_SOLUTION", "REQUIREMENT_CONFLICT"}
    assert client.get("/plan-batches").json()["batches"] == []
    # 不保存任何成员课表
    assert client.get("/plans").json() == []


def test_get_unknown_batch_404(client):
    r = client.get("/plan-batches/nope1234")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NOT_FOUND"
    assert client.post("/plan-batches/nope1234/confirm").status_code == 404


# ---------------------------------------------------------------- 空课表申请

def test_already_completed_target_is_trivially_selected(client):
    # CS101 已修，目标 CS101 -> 空课表即完整方案，入选且不占席位
    body = create_batch(client, batch(
        app_payload("A", completed_courses=["CS101"], target_courses=["CS101"]),
        app_payload("B", completed_courses=["CS101"], target_courses=["CS101"])))
    assert body["selected_count"] == 2
    for m in body["applications"]:
        assert m["selected"] is True
        assert m["result"]["semesters"] == []
        assert m["result"]["completion_semester"] is None
    r = client.post(f"/plan-batches/{body['batch_id']}/confirm")
    assert r.status_code == 200
