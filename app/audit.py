"""培养方案要求达成审核引擎。

规则（见 README「培养方案要求与达成审核」）：

- 必修项按声明顺序排列；选修组按**优先级**（声明顺序）排列，每组给出候选课程与
  最低学分门槛；同一门课可以出现在多个必修/选修要求中。
- 互认课程可满足对应的必修项或选修候选；但**一门实际已修课只归属一项**，
  且同一互认组内多门已修课**至多计入一门**（全局约束，跨必修与所有选修组生效）。
- 优化目标依次为：
  1. 最大化满足的**必修项数**；
  2. 按选修组优先级顺序最大化各组计入学分（每组计入不超过该组门槛，超出部分
     不占用——可让给后续选修组）；
  3. 仍相同时，取归属课程代码序列（按必修项顺序、再按选修组顺序，组内代码升序）
     的字典序最小者；未满足的必修项以空位参与比较，排在所有已满足项之后。
- 审核只依赖登记时绑定目录版本的学分与互认组；目录或要求后续修订不影响历史审核。
"""
from __future__ import annotations

from .planner import PlanError

STATE_BUDGET = 200_000  # 单份审核的搜索状态上限，防止指数爆炸


def _unit_map(equiv_groups):
    """互认组 -> {课程代码: 所在互认单元 frozenset}；未归组课程自成单元。"""
    mapping = {}
    for group in equiv_groups or []:
        members = frozenset(c.strip().upper() for c in group if c.strip())
        for code in members:
            mapping[code] = members
    return mapping


def run_audit(credits, equiv_groups, required, elective_groups, completed):
    """计算一份达成审核；非法输入抛 PlanError，返回可直接落库/回显的结果字典。

    credits:         绑定目录版本的 {课程代码: 学分}
    equiv_groups:    绑定目录版本声明的互认组
    required:        必修课程代码列表（按声明顺序；重复项由调用方在登记时拒绝）
    elective_groups: [{"name", "candidates", "min_credits"}]，按优先级排列
    completed:       实际已修课程代码可迭代对象（未知课程 -> COURSE_MISSING）
    """
    unit_of = _unit_map(equiv_groups)

    def unit(code):
        return unit_of.get(code, frozenset({code}))

    required = [r.strip().upper() for r in required]
    completed = sorted({c.strip().upper() for c in completed if c.strip()})

    missing = [c for c in completed if c not in credits]
    if missing:
        raise PlanError("COURSE_MISSING",
                        f"课程缺失：{'、'.join(missing)} 不在要求版本绑定的目录版本中")

    # 已修课程按互认单元归并：同一互认组的多门已修课至多计入一门
    completed_by_unit = {}
    for c in completed:
        completed_by_unit.setdefault(unit(c), []).append(c)

    n_required = len(required)
    n_groups = len(elective_groups)
    n_slots = n_required + n_groups

    # 每个必修项可用的已修课程（同互认单元内任一已修课均可计入），按代码升序
    required_options = []
    for r in required:
        options = sorted(set(completed_by_unit.get(unit(r), [])))
        required_options.append(options)

    # 每个选修组的候选课程集合（互认判定用“候选代码集合 ∩ 已修单元”）
    group_candidates = []
    for g in elective_groups:
        cands = sorted({c.strip().upper() for c in g["candidates"]})
        group_candidates.append((cands, frozenset(cands), int(g["min_credits"])))

    counter = [0]
    memo = {}

    def solve(idx, used):
        """返回自第 idx 个槽位起的最优后缀。

        返回 (req_satisfied, group_credits, lex_key, assignment)：
        - req_satisfied：后缀满足的必修项数；
        - group_credits：后缀各选修组“计入（封顶到门槛）学分”的 int 元组；
        - lex_key：归属课程代码序列的拍平字典序键（元素均为二元元组，可直接比较）；
        - assignment：{槽位序号: 计入课程元组}。

        used: frozenset，已被前序槽位占用的实际已修课程代码；互认单元占用由其推导
        （互认组互不重叠），保证“一门课只归属一项、同组至多计入一门”。
        """
        state = (idx, used)
        cached = memo.get(state)
        if cached is not None:
            return cached
        counter[0] += 1
        if counter[0] > STATE_BUDGET:
            raise PlanError("TOO_COMPLEX", "达成审核的归属组合过多，超出复杂度限制")

        if idx == n_slots:
            empty = (0, (), (), {})
            memo[state] = empty
            return empty

        used_units = {unit(c) for c in used}

        def better(cand, best):
            """cand 是否优于 best：必修数 -> 各组学分 -> 归属序列字典序（小者优）。"""
            if best is None:
                return True
            if (cand[0], cand[1]) != (best[0], best[1]):
                return (cand[0], cand[1]) > (best[0], best[1])
            return cand[2] < best[2]

        best = None

        def keep(cand):
            nonlocal best
            if better(cand, best):
                best = cand

        if idx < n_required:
            # ---- 必修槽位：不满足，或计入一门同互认单元的已修课 ----
            suffix = solve(idx + 1, used)
            # 未满足：空槽位 (1, "")，排在任何已归属课程之后。
            # 已满足：(0, 代码) 后接槽位结束标记 (-1, "")——该标记排在任何课程元素
            # (0, x) 之前，使“刚好达到目标、少占课程”的前缀归属序列更小。
            keep((suffix[0], suffix[1], ((1, ""),) + suffix[2],
                  {idx: ()} | suffix[3]))
            for code in required_options[idx]:
                if code in used or unit(code) in used_units:
                    continue
                suffix = solve(idx + 1, used | {code})
                keep((1 + suffix[0], suffix[1],
                      ((0, code), (-1, "")) + suffix[2],
                      {idx: (code,)} | suffix[3]))
        else:
            # ---- 选修槽位：在候选互认单元中，每单元跳过或择一，学分封顶到门槛 ----
            gi = idx - n_required
            cands, cand_set, threshold = group_candidates[gi]
            avail_units = sorted(
                (u for u in completed_by_unit if u & cand_set and u not in used_units),
                key=lambda u: tuple(sorted(u)))

            subsets = []  # (计入课程升序元组, 学分之和)

            def enum_units(ui, chosen, chosen_credits):
                counter[0] += 1
                if counter[0] > STATE_BUDGET:
                    raise PlanError("TOO_COMPLEX", "达成审核的归属组合过多，超出复杂度限制")
                if ui == len(avail_units):
                    subsets.append((tuple(sorted(chosen)), chosen_credits))
                    return
                # 跳过该互认单元
                enum_units(ui + 1, chosen, chosen_credits)
                # 该单元择一已修课计入（组内至多一门）。
                # 学分达到门槛后也不剪枝：字典序决胜下多计入一门代码更小的课会使序列
                # 更小；课程是否让给后续选修组由后缀递归承担。
                for code in completed_by_unit[avail_units[ui]]:
                    if code in used:
                        continue
                    enum_units(ui + 1, chosen | {code}, chosen_credits + credits[code])

            enum_units(0, set(), 0)

            for chosen, raw_credits in subsets:
                counted = min(threshold, raw_credits)
                suffix = solve(idx + 1, used | frozenset(chosen))
                if chosen:
                    key_here = tuple((0, c) for c in chosen) + ((-1, ""),)
                else:
                    key_here = ((1, ""),)
                keep((suffix[0], (counted,) + suffix[1], key_here + suffix[2],
                      {idx: chosen} | suffix[3]))

        memo[state] = best
        return best

    best = solve(0, frozenset())
    _, group_credit_tuple, _, assignment = best

    def via_for(code, ref_code):
        return "direct" if code == ref_code else "equivalence"

    required_out, unsatisfied, assigned = [], [], set()
    for i, r in enumerate(required):
        codes = assignment.get(i, ())
        if codes:
            code = codes[0]
            assigned.add(code)
            required_out.append({
                "code": r,
                "satisfied": True,
                "attributed_course": {
                    "code": code,
                    "credits": credits[code],
                    "via": via_for(code, r),
                },
            })
        else:
            unsatisfied.append(r)
            required_out.append({"code": r, "satisfied": False, "attributed_course": None})

    groups_out = []
    for gi, g in enumerate(elective_groups):
        cands, _, threshold = group_candidates[gi]
        cand_set = set(cands)
        chosen = assignment.get(n_required + gi, ())
        courses_out = []
        for code in chosen:
            assigned.add(code)
            if code in cand_set:
                satisfies, via = code, "direct"
            else:
                # 互认计入：取该互认单元内代码最小的候选课程作为“对应候选”
                satisfies = min(c for c in cands if unit(c) == unit(code))
                via = "equivalence"
            courses_out.append({
                "code": code,
                "credits": credits[code],
                "satisfies": satisfies,
                "via": via,
            })
        courses_out.sort(key=lambda x: x["code"])
        counted = group_credit_tuple[gi]
        groups_out.append({
            "index": gi + 1,
            "name": g.get("name", ""),
            "candidates": cands,
            "min_credits": threshold,
            "counted_credits": counted,
            "credit_gap": max(0, threshold - counted),
            "satisfied": counted >= threshold,
            "attributed_courses": courses_out,
        })

    return {
        "required": required_out,
        "elective_groups": groups_out,
        "unsatisfied_required": unsatisfied,
        "unattributed_courses": [
            {"code": c, "credits": credits[c]}
            for c in completed if c not in assigned
        ],
        "achieved": not unsatisfied and all(g["satisfied"] for g in groups_out),
    }
