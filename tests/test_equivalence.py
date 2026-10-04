"""课程互认组：规划引擎单元测试与 HTTP API 集成测试（独立临时数据库）。"""
import os

import pytest
from fastapi.testclient import TestClient

from app.planner import Course, PlanError, build_plan


def C(code, credits, seasons=("SPRING", "FALL"), groups=(), coreqs=()):
    return Course(code, code, credits, frozenset(seasons),
                  tuple(tuple(g) for g in groups), tuple(coreqs))


def cat(*courses):
    return {c.code: c for c in courses}


def codes(result):
    return [[c["code"] for c in s["courses"]] for s in result["semesters"]]


# ---------------------------------------------------------------- 目标课程替代

def test_target_satisfied_by_completed_equivalent():
    """已修同组课程视为已满足，目标课程不再排课。"""
    catalog = cat(C("A", 3), C("B", 3))
    r = build_plan(catalog, {"B"}, ["A"], "2026-SPRING", 10, 4,
                   equiv_groups=[["A", "B"]])
    assert r["semesters"] == []
    assert r["completion_semester"] is None
    # 未声明互认组时沿用现有行为：已修 B 与目标 A 无关，A 仍须修读
    r = build_plan(catalog, {"B"}, ["A"], "2026-SPRING", 10, 4)
    assert codes(r) == [["A"]]


def test_target_substitution_lists_actual_course():
    """未修时择一实际修读；方案列出实际修读课程，所选课受自身开课季节约束。"""
    catalog = cat(C("A", 3, seasons=("FALL",)), C("B", 3))
    r = build_plan(catalog, set(), ["A"], "2026-SPRING", 10, 4,
                   equiv_groups=[["A", "B"]])
    # A 仅秋季开课（第 2 学期），B 春季即可修读 -> 实际修读 B，最早完成
    assert codes(r) == [["B"]]
    assert r["completion_semester"] == "2026-SPRING"


def test_target_substitution_lexicographic_tiebreak():
    """同完成学期按实际修读课程代码序列字典序取优。"""
    catalog = cat(C("A", 3), C("B", 3))
    r = build_plan(catalog, set(), ["B"], "2026-SPRING", 10, 4,
                   equiv_groups=[["A", "B"]])
    assert codes(r) == [["A"]]  # A、B 等价且同学期可修，字典序 A 优先


def test_one_course_satisfies_multiple_targets_same_group():
    """同组课程被多个目标引用时，一次修读即可满足。"""
    catalog = cat(C("A", 3), C("B", 3))
    r = build_plan(catalog, set(), ["A", "B"], "2026-SPRING", 10, 4,
                   equiv_groups=[["A", "B"]])
    assert codes(r) == [["A"]]
    assert r["total_credits"] == 3


# ---------------------------------------------------------------- 先修组替代

def test_prereq_satisfied_by_completed_equivalent():
    """先修组选项的同组已修课程视为已满足，不重复排课。"""
    catalog = cat(C("A", 3), C("B", 3), C("X", 3, groups=(("A",),)))
    r = build_plan(catalog, {"B"}, ["X"], "2026-SPRING", 10, 4,
                   equiv_groups=[["A", "B"]])
    assert codes(r) == [["X"]]


def test_substitute_prereq_scheduled_before_dependent():
    """替代先修须安排在被依赖课程之前的学期。"""
    catalog = cat(
        C("A", 3, seasons=("FALL",)), C("B", 3),
        C("X", 3, groups=(("A",),)),
    )
    r = build_plan(catalog, set(), ["X"], "2026-SPRING", 10, 4,
                   equiv_groups=[["A", "B"]])
    # 替代先修 B 可于第 1 学期修读，X 第 2 学期；若选 A 则须等到第 2、3 学期
    assert codes(r) == [["B"], ["X"]]
    assert r["completion_semester"] == "2026-FALL"


def test_substitute_prereq_carries_own_prereqs():
    """所选替代课仍受自身先修约束（替代课的先修链一并排入）。"""
    catalog = cat(
        C("P", 3, seasons=("FALL",)),
        C("A", 3, groups=(("P",),)),
        C("M", 3),
        C("B", 3, groups=(("M",),)),
        C("X", 3, groups=(("A",),)),
    )
    r = build_plan(catalog, set(), ["X"], "2026-SPRING", 10, 6,
                   equiv_groups=[["A", "B"]])
    # A 路径：P(秋)->A->X 共 4 学期；B 路径：M->B->X 共 3 学期，更早完成
    assert codes(r) == [["M"], ["B"], ["X"]]


def test_one_course_satisfies_target_and_prereq_group():
    """同组课程同时被目标与先修组引用时，一次修读即可满足。"""
    catalog = cat(C("A", 3), C("B", 3), C("X", 3, groups=(("A",),)))
    r = build_plan(catalog, set(), ["B", "X"], "2026-SPRING", 10, 6,
                   equiv_groups=[["A", "B"]])
    # A 同时满足目标 B（同组替代）与 X 的先修组
    assert codes(r) == [["A"], ["X"]]
    assert r["total_credits"] == 6


def test_one_course_satisfies_multiple_prereq_groups():
    """同组课程被多门课的先修组引用时，一次修读即可满足。"""
    catalog = cat(
        C("A", 3), C("B", 3),
        C("X", 3, groups=(("A",),)),
        C("Y", 3, groups=(("B",),)),
    )
    r = build_plan(catalog, set(), ["X", "Y"], "2026-SPRING", 10, 6,
                   equiv_groups=[["A", "B"]])
    assert codes(r) == [["A"], ["X", "Y"]]


def test_course_cannot_substitute_its_own_prereq():
    """课程仅经互认扩展进入自己的先修组时，不能替代满足自己的先修。"""
    catalog = cat(C("A", 3), C("X", 3, groups=(("A",),)))
    r = build_plan(catalog, set(), ["X"], "2026-SPRING", 10, 4,
                   locked=[("X", "2026-FALL")],  # 锁定 X 强制其入选
                   equiv_groups=[["A", "X"]])
    # X 与先修选项 A 互认：仍须先修 A（第 1 学期），X 排在锁定学期
    assert codes(r) == [["A"], ["X"]]


# ---------------------------------------------------------------- 同修与锁定不豁免

def test_coreq_not_exempted_by_equivalence():
    """同修仍按指定课程同学期修读：互认组的已修课程不能豁免同修。"""
    catalog = cat(
        C("A", 3, coreqs=("AL",)), C("AL", 1), C("BL", 1),
    )
    r = build_plan(catalog, {"BL"}, ["A"], "2026-SPRING", 10, 4,
                   equiv_groups=[["AL", "BL"]])
    # BL 已修不能豁免 A 的同修课 AL
    assert codes(r) == [["A", "AL"]]


def test_chosen_substitute_uses_own_coreqs():
    """实际修读替代课时，按替代课自身的同修课程安排。"""
    catalog = cat(
        C("A", 3, seasons=("FALL",), coreqs=("AL",)), C("AL", 1, seasons=("FALL",)),
        C("B", 3, coreqs=("BL",)), C("BL", 1),
    )
    r = build_plan(catalog, set(), ["A"], "2026-SPRING", 10, 4,
                   equiv_groups=[["A", "B"]])
    # 修读 B（第 1 学期即可），其同修 BL 同学期；A 的同修 AL 不涉及
    assert codes(r) == [["B", "BL"]]


def test_lock_requires_exact_course_and_satisfies_group_target():
    """锁定安排仍要求指定课程在指定学期修读；该课同时满足同组目标。"""
    catalog = cat(C("A", 3), C("B", 3))
    r = build_plan(catalog, set(), ["B"], "2026-SPRING", 10, 4,
                   locked=[("A", "2026-SPRING")],
                   equiv_groups=[["A", "B"]])
    # 锁定强制修读 A；目标 B 由同组的 A 一次修读即满足，不再另排
    assert codes(r) == [["A"]]


def test_lock_conflict_not_resolved_by_equivalence():
    """互认不得豁免锁定：锁定学期该课不开课时，同组课程不能顶替。"""
    catalog = cat(C("A", 3, seasons=("FALL",)), C("B", 3))
    with pytest.raises(PlanError) as e:
        build_plan(catalog, set(), ["B"], "2026-SPRING", 10, 4,
                   locked=[("A", "2026-SPRING")],
                   equiv_groups=[["A", "B"]])
    assert e.value.code == "REQUIREMENT_CONFLICT"
    assert "A" in e.value.message


# ---------------------------------------------------------------- 无解与冲突

def test_no_feasible_substitute_is_no_solution():
    """所有互认替代在期限内均不可行时，按期限内无解处理。"""
    catalog = cat(C("A", 3, seasons=("FALL",)), C("B", 3, seasons=("FALL",)))
    with pytest.raises(PlanError) as e:
        build_plan(catalog, set(), ["A"], "2026-SPRING", 10, 1,
                   equiv_groups=[["A", "B"]])
    assert e.value.code == "NO_SOLUTION"


def test_equiv_prereq_no_option_is_conflict():
    """先修组及其互认替代在目录中均无可选课程时，说明条件冲突。"""
    catalog = cat(C("X", 3, groups=(("A",),)))
    with pytest.raises(PlanError) as e:
        build_plan(catalog, set(), ["X"], "2026-SPRING", 10, 4,
                   equiv_groups=[["A", "B"]])  # A、B 均不在目录中
    assert e.value.code == "REQUIREMENT_CONFLICT"
    assert "X" in e.value.message


# ---------------------------------------------------------------- API 集成

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


def _revise(client, groups, note="互认修订"):
    current = client.get("/catalog/current").json()
    r = client.post("/catalog/versions", json={
        "note": note, "courses": current["courses"], "equivalence_groups": groups})
    assert r.status_code == 201, r.text
    return r.json()


def test_catalog_equiv_groups_validation(client):
    """互认组含不存在课程、重复归组或少于两门：拒绝且不生成版本。"""
    bad_payloads = [
        [["CS101", "NOPE"]],                    # 含不存在课程
        [["CS101", "CS201"], ["CS201", "CS301"]],  # 重复归组
        [["CS101"]],                            # 少于两门
        [[]],                                   # 空组
    ]
    for groups in bad_payloads:
        current = client.get("/catalog/current").json()
        r = client.post("/catalog/versions", json={
            "courses": current["courses"], "equivalence_groups": groups})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "INVALID_INPUT"
    # 均未生成新版本
    assert client.get("/catalog/current").json()["version_id"] == 1


def test_catalog_equiv_groups_roundtrip(client):
    """合法互认组随版本保存并可回看；未声明的历史版本返回空列表。"""
    body = _revise(client, [["CS301", "CS302"], ["CS401", "CS402"]])
    assert body["version_id"] == 2
    assert body["equivalence_groups"] == [["CS301", "CS302"], ["CS401", "CS402"]]
    current = client.get("/catalog/current").json()
    assert current["equivalence_groups"] == [["CS301", "CS302"], ["CS401", "CS402"]]
    # 历史版本（未声明互认组）保持原语义
    v1 = client.get("/catalog/versions/1").json()
    assert v1["equivalence_groups"] == []
    assert client.get("/catalog/versions/2").json()["equivalence_groups"] == body["equivalence_groups"]


def test_plan_prereq_satisfied_by_completed_equivalent_api(client):
    """按当前目录版本判断互认：已修同组课程视为已满足先修。"""
    _revise(client, [["CS301", "CS302"]])
    r = client.post("/plans", json=plan_payload(
        completed_courses=["CS101", "CS101L", "CS201", "MATH103", "CS302"]))
    assert r.status_code == 201, r.text
    result = r.json()["result"]
    # CS301 的先修要求由同组已修 CS302 满足，直接修读 CS401（仅秋季开课）
    assert [[c["code"] for c in s["courses"]] for s in result["semesters"]] == [[], ["CS401"]]
    assert result["completion_semester"] == "2026-FALL"


def test_plan_target_satisfied_by_completed_equivalent_api(client):
    _revise(client, [["CS301", "CS302"]])
    r = client.post("/plans", json=plan_payload(
        completed_courses=["CS302"], target_courses=["CS301"]))
    assert r.status_code == 201, r.text
    result = r.json()["result"]
    assert result["semesters"] == []
    assert result["completion_semester"] is None


def test_plan_target_substitute_actual_course_api(client):
    """目标可替代时择一实际修读，方案列出实际课程并取最早完成。"""
    _revise(client, [["CS401", "CS402"]])
    r = client.post("/plans", json=plan_payload(
        completed_courses=["CS101", "CS101L", "CS201", "MATH101", "MATH102",
                           "MATH103", "CS301"]))
    assert r.status_code == 201, r.text
    result = r.json()["result"]
    # CS401 仅秋季（第 2 学期）；同组 CS402 春季即可且先修已满足 -> 实际修读 CS402
    assert [[c["code"] for c in s["courses"]] for s in result["semesters"]] == [["CS402"]]
    assert result["completion_semester"] == "2026-SPRING"


def test_plan_lock_not_substituted_api(client):
    """锁定课程不得由同组课程顶替：锁定学期不开课即冲突，不保存方案。"""
    _revise(client, [["CS401", "CS402"]])
    r = client.post("/plans", json=plan_payload(
        completed_courses=["CS101", "CS101L", "CS201", "MATH101", "MATH102",
                           "MATH103", "CS301"],
        locked=[{"course": "CS401", "semester": "2026-SPRING"}]))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "REQUIREMENT_CONFLICT"
    assert client.get("/plans").json() == []


def test_plan_no_solution_with_equiv_saves_nothing(client):
    _revise(client, [["CS401", "CS402"]])
    r = client.post("/plans", json=plan_payload(num_semesters=1))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "NO_SOLUTION"
    assert client.get("/plans").json() == []


def test_old_plan_review_keeps_original_semantics(client):
    """既有方案回看保持原版本语义，不受后续互认组修订影响。"""
    r = client.post("/plans", json=plan_payload(
        completed_courses=["CS101", "CS101L", "CS201", "MATH103", "CS302"]))
    assert r.status_code == 201
    plan = r.json()
    # 版本 1 未声明互认组：CS302 与 CS301 无关，仍需修读 CS301
    assert [[c["code"] for c in s["courses"]] for s in plan["result"]["semesters"]] == [
        ["CS301"], ["CS401"]]
    _revise(client, [["CS301", "CS302"]])
    again = client.get(f"/plans/{plan['plan_id']}").json()
    assert again["catalog_version"] == 1
    assert again["result"] == plan["result"]


def test_replan_uses_current_catalog_equiv_groups(client):
    """滚动重排按当前目录版本判断互认：已修同组课程视为已满足。"""
    r = client.post("/plans", json=plan_payload())
    plan = r.json()
    assert client.post(f"/plans/{plan['plan_id']}/confirm").status_code == 200
    _revise(client, [["CS301", "CS302"]])
    r = client.post(f"/plans/{plan['plan_id']}/replan", json={
        "elapsed_semesters": 2,
        "completed_courses": ["CS101", "CS101L", "CS201", "MATH103", "CS302"],
    })
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["catalog_version"] == 2
    # CS301 由同组已修 CS302 满足；CS401 仅秋季 -> 2027-FALL
    sems = [[c["code"] for c in s["courses"]] for s in draft["result"]["semesters"]]
    assert sems == [[], ["CS401"]]
    # 与原方案剩余学期（2027-SPRING: CS301）相比：移除 CS301
    diff = draft["replan"]["difference"]
    assert diff["removed_courses"] == ["CS301"]
    spring = next(s for s in diff["semesters"] if s["semester"] == "2027-SPRING")
    assert spring["removed_courses"] == ["CS301"]
