"""批次联合排课引擎。

教务处一次提交多份普通课表申请，在同一份当前目录与“当时席位余量快照”上联合排课：
实际修读课程在同一学期的批次席位总需求不得超过余量。确定性优化目标依次为：

1. 最大化获得完整课表的申请数；
2. 按申请标识升序比较完成学期（未入选视为晚于任何完成学期）；
3. 按申请标识升序比较入选课表逐学期课程代码序列（未入选视为最末）。

每份申请各自沿用单份规划的全部规则（开课季节、先修组、同修、互认、锁定、
不可排课学期、学分上限、单申请内席位余量）。引擎先枚举出每份申请在余量快照下
全部“席位需求不同”的可行课表（同一席位需求只保留字典序最优者），再在申请间
做联合回溯，按上述三级目标选出唯一结果。
"""
from __future__ import annotations

from dataclasses import dataclass

from .planner import (
    PlanError,
    _enumerate_sets,
    _equiv_map,
    enumerate_schedules_for_course_set,
    format_schedule,
    prepare_plan_inputs,
    schedule_course_set,
    semester_label,
)

MAX_APPLICATIONS = 100       # 单批申请数上限
MAX_OPTIONS_PER_APP = 256    # 单份申请保留的“席位需求不同”的可行课表上限
BATCH_NODE_BUDGET = 1_000_000  # 联合回溯节点预算
_INF = 10**9


@dataclass(frozen=True)
class Option:
    """一份申请的一条可行课表。"""
    k: int
    seq: tuple               # 逐学期课程代码（升序）元组的元组
    rank: int                # 完成学期序号（年份*2+季节序），用于完成学期比较
    signature: frozenset     # 涉及席位上限的 {(课程代码, 学期标签)} 需求（实际修读，计 1）


def _completion_rank(schedule_result):
    year, season = schedule_result["completion_semester"].split("-")
    return int(year) * 2 + (0 if season == "SPRING" else 1)


def _capped_signature(seq, start_year, start_season, seat_caps):
    """课表对“设了席位上限”的 (课程,学期) 的需求集合。

    同一课表中一门课只修读一次，故每个键的需求恒为 1；不设上限的键不参与竞争。
    """
    sig = set()
    for i, codes in enumerate(seq, start=1):
        label = semester_label(start_year, start_season, i)
        for code in codes:
            if (code, label) in seat_caps:
                sig.add((code, label))
    return frozenset(sig)


def build_application_options(catalog, application, *, equiv, seat_caps, occupied, counter):
    """枚举单份申请在余量快照下全部“席位需求不同”的可行课表。

    application: {application_id, completed_courses, target_courses, start_semester,
                  max_credits_per_semester, num_semesters, locked, unavailable_semesters}
    返回 dict(application_id, options(list[Option] 按 (rank, seq) 升序),
              trivial(bool), prepared, error)；该申请独立即无解时 options 为空且
    error=(code, message)。
    """
    locked = [(l["course"], l["semester"]) for l in application.get("locked", [])]
    prepared = prepare_plan_inputs(
        catalog,
        completed=set(application["completed_courses"]),
        targets=application["target_courses"],
        start_semester=application["start_semester"],
        max_credits=application["max_credits_per_semester"],
        num_semesters=application["num_semesters"],
        locked=locked,
        unavailable=application.get("unavailable_semesters", []),
        equiv=equiv,
    )
    start_year = prepared["start_year"]
    start_season = prepared["start_season"]
    completed = prepared["completed"]
    lock_items = prepared["lock_items"]
    unavailable_idx = frozenset(prepared["unavailable_idx"])
    max_credits = prepared["max_credits"]
    num_semesters = prepared["num_semesters"]

    # 目标/锁定均已满足：空课表即完整方案（与单份规划“目标课程均已完成”一致）
    if not prepared["needed"] and not prepared["target_reqs"]:
        return {"application_id": application["application_id"], "options": [],
                "trivial": True, "prepared": prepared, "error": None}

    leaves, conflicts = _enumerate_sets(
        catalog, completed, prepared["needed"], prepared["target_reqs"], equiv)

    best_by_sig = {}   # signature -> (k, seq)
    no_solution_notes = []
    for courses in leaves:
        schedules, err = enumerate_schedules_for_course_set(
            catalog, courses, completed=completed, equiv=equiv,
            lock_items=lock_items, unavailable_idx=unavailable_idx,
            start_year=start_year, start_season=start_season,
            max_credits=max_credits, num_semesters=num_semesters,
            seat_caps=seat_caps, occupied=occupied, counter=counter)
        if not schedules:
            if err is None:
                # 枚举器在该课程集合上未产出时（理论上仅期限/学分/席位原因），
                # 沿用单份规划口径定位原因
                _, (kind, message) = schedule_course_set(
                    catalog, courses, completed=completed, equiv=equiv,
                    lock_items=lock_items, unavailable_idx=unavailable_idx,
                    start_year=start_year, start_season=start_season,
                    max_credits=max_credits, num_semesters=num_semesters,
                    seat_caps=seat_caps, occupied=occupied)
            else:
                kind, message = err
            if kind == "no_solution":
                no_solution_notes.append(message)
            else:
                conflicts.append(message)
            continue
        for k, seq in schedules:
            sig = _capped_signature(seq, start_year, start_season, seat_caps)
            old = best_by_sig.get(sig)
            if old is None or (k, seq) < old:
                best_by_sig[sig] = (k, seq)

    error = None
    if not best_by_sig:
        if no_solution_notes:
            error = ("NO_SOLUTION", no_solution_notes[0])
        else:
            error = ("REQUIREMENT_CONFLICT",
                     conflicts[0] if conflicts else "先修/同修条件冲突，无法生成方案")

    options = []
    for sig, (k, seq) in best_by_sig.items():
        rank = _completion_rank(format_schedule(catalog, start_year, start_season, k, seq))
        options.append(Option(k=k, seq=seq, rank=rank, signature=sig))
    options.sort(key=lambda o: (o.rank, o.seq))
    if len(options) > MAX_OPTIONS_PER_APP:
        raise PlanError("TOO_COMPLEX",
                        f"申请 {application['application_id']} 的席位需求不同的可行课表"
                        f"超过 {MAX_OPTIONS_PER_APP} 种，超出批量排课复杂度限制")
    return {"application_id": application["application_id"], "options": options,
            "trivial": False, "prepared": prepared, "error": error}


def _fits(signature, usage, seat_caps):
    """把 signature 叠加到 usage 后是否仍不超任何上限。"""
    for key in signature:
        if usage.get(key, 0) + 1 > seat_caps[key]:
            return False
    return True


def build_batch(catalog, applications, *, equiv_groups=None, seat_caps=None, occupied=None):
    """对多份申请做联合排课，返回确定性唯一结果。

    applications 须已通过申请级参数校验（标识非空唯一、字段合法）。
    返回 {"order": [申请标识升序],
          "results": {app_id: {"trivial", "option"(Option|None),
                               "error"((code,message)|None), "prepared"}}}
    option 为 None 表示未入选（独立无解时 error 为该申请自身的 NO_SOLUTION 等；
    可独立排课但因联合席位未入选时 error 为 BATCH_CAPACITY 原因）。
    """
    if len(applications) > MAX_APPLICATIONS:
        raise PlanError("INVALID_INPUT",
                        f"单批申请数不得超过 {MAX_APPLICATIONS} 份（当前 {len(applications)}）")

    equiv = equiv_groups if not isinstance(equiv_groups, (list, tuple)) else _equiv_map(
        equiv_groups)
    seat_caps = seat_caps or {}
    base_occupied = dict(occupied or {})

    apps = sorted(applications, key=lambda a: a["application_id"])
    built = []
    counter = [0]
    for app in apps:
        built.append(build_application_options(
            catalog, app, equiv=equiv, seat_caps=seat_caps,
            occupied=base_occupied, counter=counter))

    n = len(built)
    usage = dict(base_occupied)
    ranks_pick = [None] * n
    seqs_pick = [None] * n
    chosen = [None] * n
    # 现任最优：(-入选数, 完成学期序向量, 课表序列向量, 每份选择)
    best = {"v": None}
    nodes = [0]

    def dfs(pos, count):
        nodes[0] += 1
        if nodes[0] > BATCH_NODE_BUDGET:
            raise PlanError("TOO_COMPLEX", "批量联合排课搜索超出复杂度限制")

        cur = best["v"]
        upper = count + (n - pos)
        # 剪枝 1：入选数上界已不可能达到现任最优
        if cur is not None and upper < -cur[0]:
            return
        # 剪枝 2：只有追平入选数时，已固定的完成学期前缀字典序才可能决胜；
        # 已劣于现任则提前返回
        if cur is not None and upper == -cur[0]:
            for j in range(pos):
                if ranks_pick[j] != cur[1][j]:
                    if ranks_pick[j] > cur[1][j]:
                        return
                    break

        if pos == n:
            key = (-count, tuple(ranks_pick), tuple(seqs_pick))
            if cur is None or key < (cur[0], cur[1], cur[2]):
                best["v"] = (key[0], key[1], key[2], list(chosen))
            return

        item = built[pos]
        if item["trivial"]:
            # 空课表：完整方案且不占席位，永远入选，完成学期排在最前
            empty = Option(k=0, seq=(), rank=-1, signature=frozenset())
            ranks_pick[pos] = -1
            seqs_pick[pos] = ()
            chosen[pos] = empty
            dfs(pos + 1, count + 1)
            chosen[pos] = None
            ranks_pick[pos] = None
            seqs_pick[pos] = None
            return

        # 按 (完成学期, 字典序) 升序尝试入选，使贪心路径先得到优质现任最优解
        for opt in item["options"]:
            if not _fits(opt.signature, usage, seat_caps):
                continue
            for key in opt.signature:
                usage[key] = usage.get(key, 0) + 1
            ranks_pick[pos] = opt.rank
            seqs_pick[pos] = opt.seq
            chosen[pos] = opt
            dfs(pos + 1, count + 1)
            chosen[pos] = None
            seqs_pick[pos] = None
            ranks_pick[pos] = None
            for key in opt.signature:
                usage[key] -= 1
                if usage[key] == 0:
                    del usage[key]

        # 不入选：完成学期视为晚于任何完成学期
        ranks_pick[pos] = _INF
        seqs_pick[pos] = ()
        dfs(pos + 1, count)
        ranks_pick[pos] = None
        seqs_pick[pos] = None

    dfs(0, 0)
    chosen_opts = best["v"][3]

    # 未入选原因：优先用入选课表的实际席位占用解释超额
    selected_usage = {}
    for opt in chosen_opts:
        if opt is not None and opt.signature:
            for key in opt.signature:
                selected_usage[key] = selected_usage.get(key, 0) + 1

    results = {}
    for item, opt in zip(built, chosen_opts):
        app_id = item["application_id"]
        reason = None
        if opt is None:
            if item["error"] is not None:
                reason = item["error"]
            else:
                reason = ("BATCH_CAPACITY",
                          _capacity_reason(item, seat_caps, base_occupied, selected_usage))
        results[app_id] = {
            "trivial": item["trivial"],
            "option": opt,
            "error": reason,
            "prepared": item["prepared"],
        }
    return {"order": [a["application_id"] for a in apps], "results": results}


def _capacity_reason(item, seat_caps, base_occupied, selected_usage):
    """可独立排课但未入选时，说明其最早课表与批次内已选需求叠加后的超额冲突。"""
    opts = item["options"]
    if not opts:
        return ("申请因批次内同课程同学期席位总量不足未获得完整课表"
                "（已优先保证完整课表申请数最大化）")
    details = []
    for code, label in opts[0].signature:
        total = base_occupied.get((code, label), 0) \
            + selected_usage.get((code, label), 0) + 1
        if total > seat_caps[(code, label)]:
            details.append(
                f"{code}@{label}（批次总需求 {total}，上限 {seat_caps[(code, label)]}）")
    if details:
        return ("申请虽可独立排出完整课表，但批次内同课程同学期席位总量不足，未入选"
                "（已优先保证完整课表申请数最大化）：" + "；".join(details[:3]))
    return ("申请在完整课表数最大化的联合排课中未获得席位，未入选"
            "（已优先保证完整课表申请数最大化）")
