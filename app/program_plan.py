"""培养方案达成课表引擎。

学生凭**要求版本**、累计已修课程、起始学期、每学期学分上限与规划学期数，求期限内
最早使全部必修项及选修门槛达成的完整课表。两版目录各司其职：

- **要求绑定目录版本**（bound）：决定课程是否互认、学分为何、拟修课能否计入学分；
  仅两版目录均有的课程可计入要求，绑定目录独有课程不在当前开课目录中，无法拟修。
  已修课须存在于绑定目录或当前目录；仅绑定目录中的已修课参与要求归属，当前目录
  独有的已修课（执行中途计划外修读）只作为排课的先修/同修基础、不计入学分。
- **当前目录版本**（current）：决定未来课程的开课季节、先修、同修、排课学分与席位；
  当前目录独有课程只可作为先修/同修等必要课程随闭包排入，不计入任何要求项。

归属规则与达成审核一致（见 audit.run_audit）：一门（合并已修与拟修后的）实际修读
课程只归属一项，同一互认组至多计入一门；必修优先，再按选修组顺序最大化各组封顶
学分，最后按归属课程代码序列字典序决胜。课表本身仍按“最早完成 + 同完成学期字典序”
择优；归属取该最优课表所对应候选集合的唯一确定审核结果。

拟修课不写成历史已修审核：归属为预计（projected），随课表返回但不写入 audits；
课表待确认、不预占席位，确认沿用既有目录版本与席位原子复核。
"""
from __future__ import annotations

from .audit import run_audit
from .planner import (PlanError, _enumerate_sets, _equiv_map,
                      format_schedule, parse_semester, schedule_course_set)

SEED_CAP = 50_000     # “满足要求的拟修选课”枚举递归调用上限，防止指数爆炸
SEED_SET_CAP = 2048   # 去重后的拟修选课集合上限
TOTAL_LEAF_CAP = 20_000  # 跨全部种子去重后的先修闭包候选集合总数上限


def _credits_of(rows):
    return {r["code"]: r["credits"] for r in rows}


def _enumerate_seed_sets(shared_codes, completed, bound_equiv, required, elective_groups):
    """枚举所有“仅由可计入课程组成、可能满足全部要求”的拟修选课集合。

    必修项必须满足：已修同互认单元（按绑定目录互认）可直接满足，否则须从两版共有的
    课程中择一拟修；同一互认单元至多满足一个必修项。
    选修组按优先级在“已修单元 + 可拟修单元”中择课枚举，每组每单元至多一门，
    是否达成门槛由审核引擎最终判定（此处只生成候选）。

    seeds 为拟修课程 frozenset 的去重列表。
    """
    seeds = set()
    completed = set(completed)

    def unit_of(code):
        return bound_equiv.get(code, frozenset({code}))

    counter = [0]

    def tick():
        counter[0] += 1
        if counter[0] > SEED_CAP:
            raise PlanError("TOO_COMPLEX",
                            "培养方案的选课组合过多，超出规划复杂度限制")

    def add_seed(planned):
        if len(seeds) >= SEED_SET_CAP and frozenset(planned) not in seeds:
            raise PlanError("TOO_COMPLEX",
                            "培养方案的选课组合过多，超出规划复杂度限制")
        seeds.add(frozenset(planned))

    def required_unit_choice(unit, used_units):
        """必修单元的满足方式：('completed', 代表课) / ('plan', 可拟修课程列表) / None。

        已修单元是本必修项的合法满足来源（即使它也在已修占用集合中）；仅当该单元已被
        前序必修项拟修/占用时才不可再用。
        """
        done = sorted(c for c in completed if unit_of(c) == unit)
        if done:
            return ("completed", done[0])
        if unit in used_units:
            return None  # 已被前序必修项拟修占用
        options = sorted(c for c in unit if c in shared_codes and c not in completed)
        return ("plan", options) if options else None

    def enum_required(ri, planned, used_units):
        tick()
        if ri == len(required):
            # 必修全部满足后，初始选修占用集合只含“满足必修所用”的单元：
            # 已修单元一旦用于满足必修即对选修不可再用（一门课只归属一项）。
            enum_electives(0, planned, used_units)
            return
        unit = unit_of(required[ri])
        choice = required_unit_choice(unit, used_units)
        if choice is None:
            return  # 该必修项无法满足（单元被占用或无可修课程）：剪枝
        kind, options = choice
        if kind == "completed":
            enum_required(ri + 1, planned, used_units | {unit_of(options)})
        else:
            for code in options:
                enum_required(ri + 1, planned | {code}, used_units | {unit_of(code)})

    def enum_electives(gi, planned, used_units):
        tick()
        if gi == len(elective_groups):
            add_seed(planned)
            return
        cands = frozenset(elective_groups[gi])
        # 本组可触及、且尚未被占用的互认单元：含已修课的单元，或含两版共有课程的单元
        units = set()
        for c in completed:
            if unit_of(c) & cands:
                units.add(unit_of(c))
        for c in cands:
            u = unit_of(c)
            if u not in used_units and any(o in shared_codes for o in u):
                units.add(u)
        ordered_units = sorted(units, key=lambda u: tuple(sorted(u)))

        def rec(ui, chosen, local_units):
            tick()
            if ui == len(ordered_units):
                enum_electives(gi + 1, planned | chosen, used_units | local_units)
                return
            unit = ordered_units[ui]
            if unit in used_units:
                rec(ui + 1, chosen, local_units)  # 前序槽位已占用：本组不可计入
                return
            # 该单元不计入本组
            rec(ui + 1, chosen, local_units)
            done = sorted(c for c in completed if unit_of(c) == unit)
            if done:
                # 以已修课计入：不产生拟修课，但单元对后续组不可再用
                rec(ui + 1, chosen, local_units | {unit})
            else:
                # 该单元择一两版共有的课程拟修（组内至多一门）
                for code in sorted(c for c in unit if c in shared_codes and c not in completed):
                    rec(ui + 1, chosen | {code}, local_units | {unit})

        rec(0, frozenset(), frozenset())

    # 初始为空：必修按声明顺序占用满足自身的已修/拟修单元；未被必修使用的已修单元
    # 仍可留给选修组（一门课只归属一项，跨必修与全部选修组全局生效）。
    enum_required(0, frozenset(), frozenset())
    return sorted(seeds, key=lambda s: tuple(sorted(s)))


def build_program_plan(*, bound_rows, bound_equiv_groups, current_catalog,
                       current_equiv_groups, required, elective_groups,
                       completed, start_semester, max_credits, num_semesters,
                       seat_caps=None, occupied=None):
    """求培养方案达成课表；失败抛 PlanError，不返回部分课表。

    bound_rows:    绑定目录版本的课程行（含 code/credits，结构同 db.load_courses）
    bound_equiv_groups / current_equiv_groups: 两版目录各自声明的互认组
    current_catalog: {code: Course}（当前目录，排课与先修/同修/开课季节以此为准）
    required / elective_groups: 要求版本内容（elective_groups 为
                   [{"name","candidates","min_credits"}]）
    completed:     累计已修课程（须存在于绑定目录或当前目录，否则 COURSE_MISSING；
                   仅绑定目录中的已修课参与要求归属，当前目录独有已修课只作为
                   排课的先修/同修基础、不计入学分）
    """
    bound_credits = _credits_of(bound_rows)
    bound_equiv = _equiv_map(bound_equiv_groups)
    current_equiv = _equiv_map(current_equiv_groups)
    completed = {c.strip().upper() for c in completed}
    start_year, start_season = parse_semester(start_semester)

    # 已修课须存在于“要求绑定目录”（可计入要求）或“当前目录”（执行中途计划外修读的
    # 当前目录独有课，仅可作为后续拟修课的先修/同修基础）；两版目录均无 -> COURSE_MISSING。
    missing = sorted(c for c in completed if c not in bound_credits and c not in current_catalog)
    if missing:
        raise PlanError(
            "COURSE_MISSING",
            f"课程缺失：{'、'.join(missing)} 既不在要求版本绑定的目录版本中，"
            "也不在当前目录版本中")
    # 仅绑定目录中的已修课可参与要求归属；当前目录独有已修课只进入排课的先修/同修判定
    bound_completed = completed & set(bound_credits)

    required = [r.strip().upper() for r in required]
    groups = [
        (sorted({c.strip().upper() for c in g["candidates"]}),
         g.get("name", ""), int(g["min_credits"]))
        for g in elective_groups
    ]
    elective_groups = [{"name": name, "candidates": cands, "min_credits": thr}
                       for cands, name, thr in groups]

    # 快速路径：可计入的已修课程已使全部必修与选修门槛达成 -> 空课表（不落库）
    quick = run_audit(bound_credits, bound_equiv_groups, required,
                      elective_groups, bound_completed)
    if quick["achieved"]:
        return None, quick  # 调用方据此返回空课表

    shared_codes = set(bound_credits) & set(current_catalog)

    # 静态预检：按声明顺序逐必修项判定——同单元已被前序必修占用，或既无已修课可计、
    # 也无两版共有的课程可修时，任何选课组合都无法达成，直接说明原因（不保存部分方案）。
    consumed_units = set()
    for r in required:
        unit = bound_equiv.get(r, frozenset({r}))
        if unit in consumed_units:
            raise PlanError(
                "REQUIREMENT_CONFLICT",
                f"必修课程 {r} 所在互认组（{'、'.join(sorted(unit))}）已被前序必修项占用，"
                "同一互认组至多计入一门，无法达成")
        if unit & bound_completed:
            consumed_units.add(unit)  # 已修同组课程满足本必修
        elif (unit & shared_codes) - bound_completed:
            consumed_units.add(unit)  # 可择一共有的未修课程拟修
        else:
            raise PlanError(
                "REQUIREMENT_CONFLICT",
                f"必修课程 {r} 在当前目录版本中既未修过也无可替代修读的共有课程，"
                "当前目录独有课程不能计入学分，无法达成")

    seeds = _enumerate_seed_sets(
        shared_codes, bound_completed, bound_equiv, required,
        [cands for cands, _, _ in groups])

    best = None                 # (k, seq, attribution)：最早完成 + 字典序最优
    conflicts = []
    no_solution_notes = []
    seen_leaves = set()
    leaf_count = [0]

    def audit_of(courses):
        merged = bound_completed | set(courses)
        return run_audit(bound_credits, bound_equiv_groups, required,
                         elective_groups, merged)

    for seed in seeds:
        # 要求侧拟修课精确修读（绑定目录互认决定的择课）：空目标闭包仅展开
        # 当前目录下的先修/同修必要课程；当前目录独有的必要课闭包后才会出现。
        leaves, leaf_conflicts = _enumerate_sets(
            current_catalog, completed, set(seed), [], current_equiv)
        conflicts.extend(leaf_conflicts)
        for courses in leaves:
            if courses in seen_leaves:
                continue
            seen_leaves.add(courses)
            leaf_count[0] += 1
            if leaf_count[0] > TOTAL_LEAF_CAP:
                raise PlanError("TOO_COMPLEX",
                                "培养方案达成课表的选课组合过多，超出规划复杂度限制")
            # 只有两版共有的课程可计入要求；当前目录独有课程仅作必要课
            countable = courses & shared_codes
            attribution = audit_of(countable)
            if not attribution["achieved"]:
                continue  # 该候选（含先修闭包加课）仍无法达成全部要求
            sched, error = schedule_course_set(
                current_catalog, courses, completed=completed, equiv=current_equiv,
                start_year=start_year, start_season=start_season,
                max_credits=max_credits, num_semesters=num_semesters,
                seat_caps=seat_caps, occupied=occupied)
            if sched is None:
                kind, message = error
                if kind == "no_solution":
                    no_solution_notes.append(message)
                else:
                    conflicts.append(message)
                continue
            if best is None or sched < best[:2]:
                best = (sched[0], sched[1], attribution)

    if best is None:
        if no_solution_notes:
            raise PlanError("NO_SOLUTION", no_solution_notes[0])
        if conflicts:
            raise PlanError("REQUIREMENT_CONFLICT", conflicts[0])
        raise PlanError("NO_SOLUTION",
                        f"受开课学期、先修顺序或每学期学分上限 {max_credits} 限制，"
                        f"{num_semesters} 个学期内无法达成全部必修与选修学分要求")

    k, seq, attribution = best
    schedule = format_schedule(current_catalog, start_year, start_season, k, seq)
    return schedule, attribution
