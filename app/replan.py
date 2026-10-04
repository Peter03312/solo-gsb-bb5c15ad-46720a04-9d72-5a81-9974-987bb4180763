"""已确认方案执行中途的滚动重排。

两类来源共用“顺延学期 + 已修超集校验 + 剩余学期差异”的骨架：

- 普通规划（``replan_plan``）：沿用原方案的目标课程、每学期学分上限，从原起始学期
  顺延 elapsed_semesters 个学期后的下一学期起，按“当前目录”在剩余学期数内求最早
  完成的完整未来课表，并给出与原方案剩余学期课表的课程差异。
- 培养方案达成课表（``replan_program_plan``）：沿用原要求版本（及绑定目录）、每学期
  学分上限与期限；要求归属及计入学分仍按要求绑定目录计算，未来开课、先修、同修与席位
  按当前目录排课，可重新选择拟修课及选修归属；返回未来课表、预计归属与原剩余课表差异。
  仅凭累计已修即达成时，未来课表为空（仍作为草案保存，确认后仅撤换来源未来占位）。
"""
from __future__ import annotations

from .planner import SEASONS, PlanError, build_plan, parse_semester
from .program_plan import build_program_plan


def next_semester_label(start_semester, elapsed_semesters):
    """原起始学期顺延 elapsed_semesters 个学期后的下一学期标签。"""
    year, season = parse_semester(start_semester)
    idx = SEASONS.index(season)
    year += (idx + elapsed_semesters) // 2
    season = SEASONS[(idx + elapsed_semesters) % 2]
    return f"{year}-{season}"


def _sort_key(label):
    y, s = label.split("-")
    return int(y), SEASONS.index(s)


def remaining_diff(original_result, new_result, elapsed_semesters):
    """新方案与原方案“剩余学期”（跳过前 elapsed_semesters 个已结束学期）的课程差异。

    原方案按学期顺序跳过前 elapsed_semesters 个学期后，与新课表按学期标签对齐，
    分别给出每学期的新增/移除课程，并汇总去重后的新增/移除课程。
    """
    by_semester = {}
    for s in original_result.get("semesters", [])[elapsed_semesters:]:
        by_semester.setdefault(s["semester"], {})["old"] = {c["code"] for c in s["courses"]}
    for s in new_result.get("semesters", []):
        by_semester.setdefault(s["semester"], {})["new"] = {c["code"] for c in s["courses"]}

    semesters, added_all, removed_all = [], set(), set()
    for label in sorted(by_semester, key=_sort_key):
        old = by_semester[label].get("old", set())
        new = by_semester[label].get("new", set())
        added, removed = sorted(new - old), sorted(old - new)
        if not added and not removed:
            continue
        added_all.update(added)
        removed_all.update(removed)
        semesters.append({"semester": label, "added_courses": added, "removed_courses": removed})

    return {
        "semesters": semesters,
        "added_courses": sorted(added_all),
        "removed_courses": sorted(removed_all),
    }


def replan_plan(catalog, original_request, original_result, elapsed_semesters, completed,
                equiv_groups=None, seat_caps=None, occupied=None):
    """执行滚动重排，返回 (effective_request, result, diff)；失败抛出 PlanError。

    equiv_groups 为重排所使用目录版本声明的课程互认组（按当前目录判断互认）。
    seat_caps/occupied 为席位上限与重排时点的占用余量（调用方应已剔除来源方案
    尚未结束学期的自身占位，那些占位确认时将被新课表替换）。
    """
    _validate_replan_inputs(original_request, elapsed_semesters, completed)

    remaining = int(original_request["num_semesters"]) - elapsed_semesters
    start_label = next_semester_label(original_request["start_semester"], elapsed_semesters)
    effective_request = {
        "completed_courses": sorted(completed),
        "target_courses": list(original_request["target_courses"]),
        "start_semester": start_label,
        "max_credits_per_semester": int(original_request["max_credits_per_semester"]),
        "num_semesters": remaining,
    }
    result = build_plan(
        catalog,
        completed=set(completed),
        targets=original_request["target_courses"],
        start_semester=start_label,
        max_credits=int(original_request["max_credits_per_semester"]),
        num_semesters=remaining,
        equiv_groups=equiv_groups,
        seat_caps=seat_caps,
        occupied=occupied,
    )
    diff = remaining_diff(original_result, result, elapsed_semesters)
    return effective_request, result, diff


def _validate_replan_inputs(original_request, elapsed_semesters, completed):
    """两类重排共用的输入校验：学期数越界（INVALID_INPUT）与已修回退（COMPLETED_REVOKED）。"""
    if elapsed_semesters >= int(original_request["num_semesters"]):
        raise PlanError(
            "INVALID_INPUT",
            f"已结束学期数 {elapsed_semesters} 必须小于原规划期限 "
            f"{original_request['num_semesters']}",
        )

    declared = set(original_request.get("completed_courses", []))
    revoked = sorted(declared - set(completed))
    if revoked:
        raise PlanError(
            "COMPLETED_REVOKED",
            "累计已修课程不得撤销原方案创建时声明的已修课程，缺少：" + "、".join(revoked),
        )


def replan_program_plan(*, bound_rows, bound_equiv_groups, current_catalog,
                        current_equiv_groups, required, elective_groups,
                        original_request, original_result, elapsed_semesters,
                        completed, seat_caps=None, occupied=None):
    """培养方案达成课表执行中途的滚动重排。

    沿用原要求版本（要求归属及计入学分按其绑定目录计算）、原每学期学分上限与期限；
    从原起始学期顺延 elapsed_semesters 个学期后的下一学期起，在剩余学期数内按当前目录
    （开课、先修、同修、席位）重新求最早达成方案，拟修课与选修归属均可重新选择。

    返回 (effective_request, schedule, attribution, diff)；仅凭累计已修即达成时
    schedule 为空课表结构（completion_semester=None、semesters=[]）、attribution 仍给出
    合并归属；失败抛出 PlanError，不返回部分课表。

    bound_rows / *_equiv_groups / current_catalog 等参数含义同
    program_plan.build_program_plan；seat_caps/occupied 为当前目录的席位上下文，
    调用方应已剔除来源方案尚未结束学期的自身占位。
    """
    _validate_replan_inputs(original_request, elapsed_semesters, completed)

    remaining = int(original_request["num_semesters"]) - elapsed_semesters
    start_label = next_semester_label(
        original_request["start_semester"], elapsed_semesters)
    effective_request = {
        "plan_type": "program_plan",
        "requirement_version_id": original_request["requirement_version_id"],
        "bound_catalog_version": original_request["bound_catalog_version"],
        "completed_courses": sorted(completed),
        "start_semester": start_label,
        "max_credits_per_semester": int(original_request["max_credits_per_semester"]),
        "num_semesters": remaining,
    }
    schedule, attribution = build_program_plan(
        bound_rows=bound_rows,
        bound_equiv_groups=bound_equiv_groups,
        current_catalog=current_catalog,
        current_equiv_groups=current_equiv_groups,
        required=required,
        elective_groups=elective_groups,
        completed=set(completed),
        start_semester=start_label,
        max_credits=int(original_request["max_credits_per_semester"]),
        num_semesters=remaining,
        seat_caps=seat_caps,
        occupied=occupied,
    )
    if schedule is None:
        # 仅凭累计已修即达成：未来课表为空；差异即原方案剩余课表全部移除
        schedule = {
            "completion_semester": None,
            "total_credits": 0,
            "semesters": [],
            "message": "累计已修课程已达成全部必修与选修学分要求，未来无需排课",
        }
    else:
        # 回看一致性：拟修目标集合取自未来课表（普通重排草案无此字段）
        effective_request["target_courses"] = sorted(
            {c["code"] for s in schedule["semesters"] for c in s["courses"]})
    diff = remaining_diff(original_result, schedule, elapsed_semesters)
    return effective_request, schedule, attribution, diff
