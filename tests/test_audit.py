"""培养方案要求达成审核：审核引擎单元测试（归属、互认、优先级、字典序决胜）。"""
import pytest

from app.audit import run_audit
from app.planner import PlanError


def cr(**kw):
    c = {"A": 3, "B": 3, "C": 2, "D": 4, "E": 3, "F": 1}
    c.update(kw)
    return c


def g(name, cands, thr):
    return {"name": name, "candidates": list(cands), "min_credits": thr}


def req_map(result):
    return {x["code"]: x for x in result["required"]}


def group_map(result):
    return {x["name"]: x for x in result["elective_groups"]}


# ---------------------------------------------------------------- 必修与互认

def test_direct_completion():
    r = run_audit(cr(), [], ["A", "B"], [], ["A", "B"])
    assert r["achieved"] is True
    assert r["unsatisfied_required"] == []
    assert req_map(r)["A"]["attributed_course"] == {"code": "A", "credits": 3, "via": "direct"}


def test_equivalence_satisfies_required():
    r = run_audit(cr(), [["A", "B"]], ["A"], [], ["B"])
    item = req_map(r)["A"]
    assert item["satisfied"] is True
    assert item["attributed_course"]["code"] == "B"
    assert item["attributed_course"]["via"] == "equivalence"
    assert r["achieved"] is True


def test_same_equiv_group_two_completed_counted_once():
    """同一互认组两门都已修，也至多计入一门必修项。"""
    r = run_audit(cr(), [["A", "B"]], ["A"], [], ["A", "B"])
    assert req_map(r)["A"]["satisfied"] is True
    assert sorted(c["code"] for c in r["unattributed_courses"]) == ["A", "B"] or \
        len(r["unattributed_courses"]) == 1
    # 恰好一门归属，另一门未归属
    assigned = [x["attributed_course"]["code"] for x in r["required"] if x["satisfied"]]
    assert assigned == ["A"] or assigned == ["B"]
    assert len(r["unattributed_courses"]) == 1


def test_unknown_completed_rejected():
    with pytest.raises(PlanError) as e:
        run_audit(cr(), [], ["A"], [], ["NOPE"])
    assert e.value.code == "COURSE_MISSING"
    with pytest.raises(PlanError) as e:
        run_audit(cr(), [], ["A"], [g("x", ["A"], 3)], ["ZZZ"])
    assert e.value.code == "COURSE_MISSING"


def test_empty_everything():
    r = run_audit(cr(), [], [], [], [])
    assert r["achieved"] is True
    assert r["required"] == [] and r["elective_groups"] == []


# ---------------------------------------------------------------- 一门课只归属一项

def test_one_course_attributed_to_single_item():
    """A 既是必修又是 g1 候选：满足必修后不能再计入选修。"""
    groups = [g("g1", ["A"], 3)]
    r = run_audit(cr(), [], ["A"], groups, ["A"])
    assert req_map(r)["A"]["satisfied"] is True
    assert group_map(r)["g1"]["counted_credits"] == 0
    assert group_map(r)["g1"]["attributed_courses"] == []


def test_required_maximized_before_electives():
    """先最大化必修项数：共享同一互认单元时课程让给必修。"""
    groups = [g("g1", ["A"], 3)]
    r = run_audit(cr(), [["A", "B"]], ["A"], groups, ["B"])
    assert req_map(r)["A"]["satisfied"] is True
    assert group_map(r)["g1"]["counted_credits"] == 0
    assert r["achieved"] is False


def test_same_course_appears_in_two_requirements():
    """同门课可出现在不同要求中：两个选修组都含 A，但只够先满足高优先级组。"""
    groups = [g("g1", ["A"], 3), g("g2", ["A"], 3)]
    r = run_audit(cr(), [], [], groups, ["A"])
    assert group_map(r)["g1"]["counted_credits"] == 3
    assert group_map(r)["g2"]["counted_credits"] == 0
    assert group_map(r)["g2"]["credit_gap"] == 3
    assert r["achieved"] is False


# ---------------------------------------------------------------- 选修组优先级与封顶

def test_group_priority_order():
    """高优先级组先拿课：g1 取 A(3) 达门槛，g2 用 C+D 达 6。"""
    groups = [g("g1", ["A", "B"], 3), g("g2", ["A", "C", "D"], 6)]
    r = run_audit(cr(), [], [], groups, ["A", "B", "C", "D"])
    gm = group_map(r)
    assert [c["code"] for c in gm["g1"]["attributed_courses"]] == ["A"]
    assert sorted(c["code"] for c in gm["g2"]["attributed_courses"]) == ["C", "D"]
    assert gm["g2"]["counted_credits"] == 6


def test_credits_capped_at_threshold_and_shared():
    """单组计入不超过门槛；超出的课可让给后续组：两组门槛均为 4 时仍可同时达成。"""
    groups = [g("g1", ["A", "B", "C"], 4), g("g2", ["A", "B", "C", "D"], 4)]
    r = run_audit(cr(A=3, B=3, C=2, D=4), [], [], groups, ["A", "B", "C", "D"])
    gm = group_map(r)
    # g1 达门槛（4）的最小组合：A 单门仅 3 不足；A+B(6) 与 A+C(5) 均封顶 4，
    # 序列上 A,B < A,C，故 g1 取 A,B；g2 用 C(2)+D(4)=6（封顶 4）。
    assert gm["g1"]["counted_credits"] == 4
    assert sorted(c["code"] for c in gm["g1"]["attributed_courses"]) == ["A", "B"]
    assert gm["g2"]["counted_credits"] == 4
    assert sorted(c["code"] for c in gm["g2"]["attributed_courses"]) == ["C", "D"]
    assert r["achieved"] is True


def test_credit_gap_reported():
    groups = [g("g1", ["A"], 3), g("g2", ["B"], 6)]
    r = run_audit(cr(B=3), [], [], groups, ["A", "B"])
    gm = group_map(r)
    assert gm["g1"]["credit_gap"] == 0 and gm["g1"]["satisfied"] is True
    assert gm["g2"]["credit_gap"] == 3 and gm["g2"]["satisfied"] is False
    assert r["achieved"] is False


def test_equiv_group_spanning_elective_groups_counted_once():
    """同一互认组两门已修课在多个选修组中也至多计入一门。"""
    groups = [g("g1", ["A"], 3), g("g2", ["B"], 3)]
    r = run_audit(cr(), [["A", "B"]], [], groups, ["A", "B"])
    gm = group_map(r)
    assert [c["code"] for c in gm["g1"]["attributed_courses"]] == ["A"]
    assert gm["g2"]["attributed_courses"] == []
    assert gm["g2"]["counted_credits"] == 0


def test_equivalence_counts_actual_course_credits():
    """互认计入时学分按实际已修课程计算。"""
    groups = [g("g1", ["A"], 6)]
    r = run_audit(cr(A=3, B=5), [["A", "B"]], [], groups, ["B"])
    item = group_map(r)["g1"]["attributed_courses"][0]
    assert item["code"] == "B" and item["credits"] == 5
    assert item["satisfies"] == "A" and item["via"] == "equivalence"
    assert group_map(r)["g1"]["counted_credits"] == 5
    assert group_map(r)["g1"]["credit_gap"] == 1


# ---------------------------------------------------------------- 字典序决胜

def test_lex_tiebreak_single_group_prefers_smaller_code():
    groups = [g("g1", ["A", "B"], 3)]
    r = run_audit(cr(), [], [], groups, ["A", "B"])
    assert [c["code"] for c in group_map(r)["g1"]["attributed_courses"]] == ["A"]


def test_lex_tiebreak_no_over_attribution_after_threshold():
    """达门槛后不再平白多占课程：A(3) 已满足门槛时不把 B 也算入本组。"""
    groups = [g("g1", ["A", "B", "C"], 3)]
    r = run_audit(cr(C=2), [], [], groups, ["A", "B", "C"])
    assert [c["code"] for c in group_map(r)["g1"]["attributed_courses"]] == ["A"]


def test_lex_tiebreak_required_position_first():
    """两门必修共享一个互认单元、只有一门已修时：优先满足声明顺序更靠前的必修。"""
    r = run_audit(cr(), [["A", "B"]], ["B", "A"], [], ["A"])
    rm = req_map(r)
    assert rm["B"]["satisfied"] is True
    assert rm["B"]["attributed_course"]["code"] == "A"
    assert rm["A"]["satisfied"] is False
    assert r["unsatisfied_required"] == ["A"]


def test_unattributed_listed():
    r = run_audit(cr(), [], ["A"], [g("g1", ["B"], 9)], ["A", "B", "F"])
    codes = {c["code"] for c in r["unattributed_courses"]}
    assert "F" in codes  # 无关课程与未达门槛选修课（计入但不足）均按归属处理
    # B 已归属到 g1（虽未达门槛），因此不在未归属列表
    assert "B" not in codes
    assert "A" not in codes


def test_group_candidates_dedup_and_direct_flag():
    r = run_audit(cr(), [], [], [g("g1", ["A", "A", "B"], 3)], ["A"])
    assert group_map(r)["g1"]["candidates"] == ["A", "B"]
    assert group_map(r)["g1"]["attributed_courses"][0]["via"] == "direct"
