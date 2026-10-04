"""培养方案达成课表引擎单元测试。

重点：
- 要求绑定目录决定互认与计入学分，当前目录决定开课/先修/同修/排课学分；
- 仅两版目录均有的拟修课可计入要求，当前目录独有课程只作必要先修；
- 最早完成 + 字典序决胜；归属遵守一门一项、同互认组至多计一门。
"""
import pytest

from app.planner import Course, PlanError
from app.program_plan import build_program_plan


def _course(code, credits=3, seasons=("SPRING", "FALL"), prereq_groups=(), coreqs=()):
    return Course(code=code, name=code, credits=credits, seasons=frozenset(seasons),
                  prereq_groups=tuple(prereq_groups), coreqs=tuple(coreqs))


def _rows(catalog, overrides=None):
    """Course 目录 -> 绑定目录行（code/credits），可用 overrides 改学分模拟版本差异。"""
    overrides = overrides or {}
    return [{"code": c, "credits": overrides.get(c, cat.credits)} for c, cat in catalog.items()]


BASE_REQUIRED = ["CS101", "CS201", "CS301", "CS401"]
BASE_ELECTIVES = [
    {"name": "专业选修", "candidates": ["CS303", "CS402"], "min_credits": 3},
    {"name": "基础拓展", "candidates": ["MATH101", "MATH103"], "min_credits": 6},
]


def seed_catalog():
    """与种子目录一致的 Course 映射。"""
    return {
        "MATH101": _course("MATH101", 4),
        "MATH102": _course("MATH102", 4, prereq_groups=(("MATH101",),)),
        "MATH103": _course("MATH103", 3),
        "CS101": _course("CS101", 3, coreqs=("CS101L",)),
        "CS101L": _course("CS101L", 1),
        "CS201": _course("CS201", 3, prereq_groups=(("CS101",),)),
        "CS202": _course("CS202", 3, prereq_groups=(("MATH101",),)),
        "CS301": _course("CS301", 3, prereq_groups=(("CS201", "CS202"), ("MATH103",))),
        "CS302": _course("CS302", 3, seasons=("FALL",), prereq_groups=(("CS201",),)),
        "CS303": _course("CS303", 3, seasons=("SPRING",), prereq_groups=(("CS201",),)),
        "CS401": _course("CS401", 3, seasons=("FALL",), prereq_groups=(("CS301",),)),
        "CS402": _course("CS402", 3, seasons=("SPRING",),
                         prereq_groups=(("CS301",), ("MATH102",))),
    }


def _build(catalog, completed, *, bound_overrides=None, bound_equiv=(), current_equiv=(),
           required=BASE_REQUIRED, electives=BASE_ELECTIVES,
           start="2026-SPRING", cap=12, n=6, seats=None, occ=None):
    return build_program_plan(
        bound_rows=_rows(catalog, bound_overrides),
        bound_equiv_groups=list(bound_equiv),
        current_catalog=catalog,
        current_equiv_groups=list(current_equiv),
        required=required,
        elective_groups=electives,
        completed=set(completed),
        start_semester=start, max_credits=cap, num_semesters=n,
        seat_caps=seats, occupied=occ)


def _codes(schedule):
    return [[c["code"] for c in s["courses"]] for s in schedule["semesters"]]


# ---------------------------------------------------------------- 基本规划

def test_earliest_schedule_with_required_chain_and_electives():
    catalog = seed_catalog()
    schedule, attr = _build(catalog, ["CS101", "CS101L", "CS201", "MATH101", "MATH103"])
    # 2026-SPRING：CS301（先修已齐）+ CS303（春季、先修 CS201 已齐）；2026-FALL：CS401
    assert _codes(schedule) == [["CS301", "CS303"], ["CS401"]]
    assert schedule["completion_semester"] == "2026-FALL"
    res = attr
    assert res["achieved"] is True
    by = {q["code"]: q for q in res["required"]}
    assert by["CS301"]["attributed_course"]["code"] == "CS301"
    groups = {g["name"]: g for g in res["elective_groups"]}
    assert [c["code"] for c in groups["专业选修"]["attributed_courses"]] == ["CS303"]
    assert groups["基础拓展"]["counted_credits"] == 6
    # 已修的 CS101L 不属于任何要求项 -> 未归属
    assert [c["code"] for c in res["unattributed_courses"]] == ["CS101L"]


def test_already_achieved_returns_none_schedule():
    catalog = seed_catalog()
    equiv = [["CS301", "CS302"], ["CS401", "CS402"]]
    completed = ["CS101", "CS201", "CS302", "CS402", "CS303", "MATH101", "MATH103"]
    schedule, attr = _build(catalog, completed, bound_equiv=equiv, current_equiv=equiv)
    assert schedule is None
    assert attr["achieved"] is True


def test_unknown_completed_course_against_bound_catalog():
    catalog = seed_catalog()
    with pytest.raises(PlanError) as exc:
        _build(catalog, ["CS101", "NOPE999"])
    assert exc.value.code == "COURSE_MISSING"


# ---------------------------------------------------------------- 两版目录分工

def test_bound_equivalence_governs_requirement_attribution():
    """互认只在绑定目录声明时有效：已修 CS302 替代必修 CS301，拟修 CS401 不再需要 CS301。"""
    catalog = seed_catalog()
    equiv = [["CS301", "CS302"], ["CS401", "CS402"]]
    schedule, attr = _build(
        catalog, ["CS101", "CS101L", "CS201", "MATH101", "MATH103", "CS302"],
        bound_equiv=equiv, current_equiv=equiv)
    by = {q["code"]: q for q in attr["required"]}
    assert by["CS301"]["attributed_course"] == {
        "code": "CS302", "credits": 3, "via": "equivalence"}
    # CS301 不再排入课表；2026-FALL 即可完成 CS401
    flat = [c for s in _codes(schedule) for c in s]
    assert "CS301" not in flat
    assert schedule["completion_semester"] == "2026-FALL"


def test_equivalence_declared_only_in_current_catalog_does_not_count_for_requirement():
    """当前目录有互认而绑定目录无：互认不能让拟修课计入学分，CS302 不能满足必修 CS301。"""
    catalog = seed_catalog()
    equiv = [["CS301", "CS302"]]
    completed = ["CS101", "CS101L", "CS201", "MATH103", "MATH101", "MATH102"]
    # 仅当前目录互认：CS302 不能替代必修 CS301，必须拟修 CS301 或 CS302？——
    # 绑定无互认时必修 CS301 只能拟修 CS301（CS302 不在该单元）。
    schedule, attr = _build(catalog, completed, bound_equiv=(), current_equiv=equiv)
    by = {q["code"]: q for q in attr["required"]}
    assert by["CS301"]["attributed_course"]["code"] == "CS301"
    assert by["CS301"]["attributed_course"]["via"] == "direct"


def test_bound_only_course_cannot_be_planned():
    """绑定目录独有（当前目录已删除）的必修课：未修时无可替代共有课，条件冲突。"""
    catalog = seed_catalog()
    del catalog["CS401"]  # 当前目录删除 CS401；绑定目录仍包含
    with pytest.raises(PlanError) as exc:
        _build(catalog, ["CS101", "CS101L", "CS201", "MATH101", "MATH103"])
    assert exc.value.code == "REQUIREMENT_CONFLICT"


def test_current_only_course_scheduled_only_as_prerequisite():
    """当前目录独有课程不要求归属，但可作为先修闭包必要课排入。

    构造：当前目录新增 CS501（仅秋季，先修 CS303），CS401 的先修改为 (CS301 或 CS501)
    不现实；改为直接构造一个要求 CS301、而 CS301 在当前目录新增先修 NEW101 的场景：
    NEW101 不在绑定目录 -> 不计学分，但必须排入更早学期。
    """
    catalog = seed_catalog()
    catalog["NEW101"] = _course("NEW101", 2, seasons=("FALL",))
    catalog["CS301"] = _course("CS301", 3,
                               prereq_groups=(("CS201", "CS202"), ("MATH103",), ("NEW101",)))
    schedule, attr = _build(
        catalog, ["CS101", "CS101L", "CS201", "MATH101", "MATH103"],
        n=6)
    flat = [c for s in _codes(schedule) for c in s]
    assert "NEW101" in flat
    # NEW101 不归属任何要求项
    attributed = {c["code"] for g in attr["elective_groups"] for c in g["attributed_courses"]}
    attributed |= {q["attributed_course"]["code"] for q in attr["required"]
                   if q["attributed_course"]}
    assert "NEW101" not in attributed
    # NEW101 仅 FALL 且必须在 CS301 之前：CS301 最早 2027-SPRING
    sems = {s["semester"]: [c["code"] for c in s["courses"]]
            for s in schedule["semesters"]}
    assert "NEW101" in sems.get("2026-FALL", [])
    assert "CS301" in sems.get("2027-SPRING", [])
    assert attr["achieved"] is True


def test_credits_for_requirement_taken_from_bound_catalog():
    """门槛学分按绑定目录学分；排课学分按当前目录。"""
    catalog = seed_catalog()
    for code in list(catalog):
        if code == "CS401":
            catalog[code] = _course("CS401", 6, seasons=("FALL",),
                                    prereq_groups=(("CS301",),))
    # 当前目录 CS401=6 学分；绑定目录仍 3 学分
    schedule, attr = _build(
        catalog, ["CS101", "CS101L", "CS201", "MATH101", "MATH103"],
        bound_overrides={"CS401": 3}, cap=12, n=6)
    sem = {s["semester"]: s for s in schedule["semesters"]}
    # 排课学分取当前目录 6
    assert sem["2026-FALL"]["total_credits"] == 6
    # 归属学分取绑定目录 3
    by = {q["code"]: q for q in attr["required"]}
    assert by["CS401"]["attributed_course"]["credits"] == 3


# ---------------------------------------------------------------- 全局归属约束

def test_one_course_attributed_to_single_item():
    """拟修 CS402 经互认满足必修 CS401 时，不得再计入选修组（即使它是选修候选）。

    已修 CS401 单元会直接满足必修；这里构造“必修必须由拟修 CS402 满足”的场景：
    起始学期设为 2026-FALL，CS401 仅 FALL（拟修须等到 2027-FALL），而 CS402 春季开课
    且先修 MATH102 已修 -> 2027-SPRING 修 CS402 最早完成必修；专业选修的 CS402 候选
    已被必修占用，只能另修 CS303。
    """
    catalog = seed_catalog()
    equiv = [["CS301", "CS302"], ["CS401", "CS402"]]
    completed = ["CS101", "CS101L", "CS201", "CS301", "MATH101", "MATH102", "MATH103"]
    schedule, attr = _build(catalog, completed, bound_equiv=equiv, current_equiv=equiv,
                            start="2026-FALL", n=6)
    codes = _codes(schedule)
    assert "CS402" in codes[1]  # 2027-SPRING
    assert schedule["completion_semester"] == "2027-SPRING"
    by = {q["code"]: q for q in attr["required"]}
    assert by["CS401"]["attributed_course"] == {
        "code": "CS402", "credits": 3, "via": "equivalence"}
    groups = {g["name"]: g for g in attr["elective_groups"]}
    prof = [c["code"] for c in groups["专业选修"]["attributed_courses"]]
    assert "CS402" not in prof  # 已归属必修，不重复计入选修
    assert "CS303" in prof


def test_two_required_in_same_equiv_unit_conflict():
    catalog = seed_catalog()
    with pytest.raises(PlanError) as exc:
        _build(catalog, [], bound_equiv=[["CS401", "CS402"]],
               required=["CS401", "CS402"], electives=[])
    assert exc.value.code == "REQUIREMENT_CONFLICT"


# ---------------------------------------------------------------- 期限与席位

def test_no_solution_within_horizon():
    catalog = seed_catalog()
    # 2 个学期排不完 CS101->CS201->CS301->CS401 的链
    with pytest.raises(PlanError) as exc:
        _build(catalog, [], n=2)
    assert exc.value.code == "NO_SOLUTION"


def test_seat_capacity_constrains_and_reports():
    catalog = seed_catalog()
    # CS401 仅 FALL；期限 4 学期（2026S..2027F）内两个 FALL 均封死 -> 无解并说明已满席位
    seats = {("CS401", "2026-FALL"): 0, ("CS401", "2027-FALL"): 0}
    with pytest.raises(PlanError) as exc:
        _build(catalog, ["CS101", "CS101L", "CS201", "CS301", "MATH101", "MATH103"],
               n=4, seats=seats)
    assert exc.value.code == "NO_SOLUTION"
    assert "席位" in exc.value.message


def test_semester_credit_cap_uses_current_catalog_credits():
    catalog = seed_catalog()
    for code in list(catalog):
        if code == "CS401":
            catalog[code] = _course("CS401", 9, seasons=("FALL",),
                                    prereq_groups=(("CS301",),))
    # 当前目录 CS401=9；同学期还想排别的会受限——CS301 在更早学期，这里主要验证可排
    schedule, _ = _build(
        catalog, ["CS101", "CS101L", "CS201", "MATH101", "MATH103"],
        bound_overrides={"CS401": 3}, cap=9, n=6)
    assert schedule is not None
