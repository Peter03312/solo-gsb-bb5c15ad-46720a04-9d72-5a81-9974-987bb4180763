"""课程学期规划引擎。

排课规则：
- 先修条件按“组”声明：每组任选一门，所有组均须满足；被依赖的课程必须安排在更早学期。
- 同修课程（coreq）若尚未修过，必须与该课同学期安排。
- 已修课程不重复排入；方案只包含目标课程及满足条件所需课程。
- 可选锁定安排（课程、学期）：锁定课程视为本次必修（即使不在目标课程中），
  必须安排在指定学期；锁定学期须在规划期限内，且不得与开课季节、同修同学期、
  不可排课学期等约束冲突。
- 可选不可排课学期：该学期不安排任何课程。
- 可选课程互认组（随目录版本声明，组间互不重叠）：组内课程可替代满足目标课程
  与先修组选项。已修同组课程视为已满足且不重复排课；未修时择一实际修读，
  所选课仍受自身开课季节、学分、先修及同修约束，替代先修须在被依赖课程之前完成。
  同修与锁定不适用互认：同修仍按指定课程同学期修读（已修除外），
  锁定仍要求指定课程在指定学期修读。同组课程被多个目标或先修组引用时，
  一次修读即可满足。未声明互认组时沿用既有行为。
- 目标：在规划期限（num_semesters）内最早完成；完成学期相同者，按“各学期课程
  代码序列”（每学期内代码升序、再按学期先后组成元组）的字典序取最小者。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import combinations

SEASONS = ("SPRING", "FALL")
SEMESTER_RE = re.compile(r"^(\d{4})-(SPRING|FALL)$")

LEAF_CAP = 512         # 先修选课组合数上限，防止指数爆炸
NODE_BUDGET = 200_000  # 单个候选课程集合的排课搜索节点上限


class PlanError(Exception):
    """规划失败：code 供调用方判断，message 面向用户说明原因。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Course:
    code: str
    name: str
    credits: int
    seasons: frozenset          # {"SPRING"}、{"FALL"} 或两者
    prereq_groups: tuple        # tuple[tuple[str, ...], ...]，每组任选一门，各组均须满足
    coreqs: tuple               # tuple[str, ...]


# ---------------------------------------------------------------- 学期工具

def parse_semester(text):
    """解析 'YYYY-SPRING' / 'YYYY-FALL'，返回 (year, season)。"""
    m = SEMESTER_RE.match((text or "").strip().upper())
    if not m:
        raise PlanError(
            "INVALID_INPUT",
            f"学期格式非法：{text!r}，应为 YYYY-SPRING 或 YYYY-FALL，如 2026-SPRING",
        )
    return int(m.group(1)), m.group(2)


def season_at(start_season, index):
    """第 index 个学期（1 起）的季节。"""
    return SEASONS[(SEASONS.index(start_season) + index - 1) % 2]


def semester_label(start_year, start_season, index):
    """第 index 个学期（1 起）的绝对标签，如 2027-FALL。"""
    year = start_year + (SEASONS.index(start_season) + index - 1) // 2
    return f"{year}-{season_at(start_season, index)}"


def semester_index(start_year, start_season, label):
    """学期标签相对起始学期的序号（1 起）；早于起始学期时返回非正数。"""
    year, season = parse_semester(label)
    return (year - start_year) * 2 + (SEASONS.index(season) - SEASONS.index(start_season)) + 1


# ---------------------------------------------------------------- 互认组

def _equiv_map(equiv_groups):
    """互认组列表 -> {课程代码: 所在组（frozenset）}；未声明时为空映射（沿用既有行为）。

    互认组随目录版本提交，提交时已校验：组间互不重叠、组内课程均存在且不少于两门。
    """
    mapping = {}
    for group in equiv_groups or []:
        members = frozenset(c.strip().upper() for c in group if c.strip())
        for code in members:
            mapping[code] = members
    return mapping


def _expand_options(equiv, group):
    """先修组选项按互认组扩展：任一选项的同组课程均可替代满足该组。"""
    expanded = set()
    for o in group:
        expanded |= equiv.get(o, frozenset({o}))
    return expanded


# ---------------------------------------------------------------- 候选课程集合

def _enumerate_sets(catalog, completed, needed, target_reqs, equiv):
    """枚举满足目标课程要求、全部先修组与同修闭包的候选课程集合。

    needed: 必须精确修读的课程（锁定课程，互认不得豁免）；
    target_reqs: 目标课程的互认闭包列表，每组至少一门入选
    （已被已修同组课程满足的要求由调用方过滤）。
    返回 (leaves, conflicts)：leaves 为 frozenset 列表；conflicts 为枚举过程中
    发现的条件冲突原因（供全部失败时向用户说明）。
    """
    leaves = []
    conflicts = []
    seen = set()

    def close_coreqs(courses):
        result = set(courses)
        stack = list(result)
        while stack:
            cur = stack.pop()
            for co in catalog[cur].coreqs:
                if co in completed or co in result:
                    continue
                if co not in catalog:
                    conflicts.append(f"课程 {cur} 的同修课程 {co} 不在目录中，条件冲突")
                    return None
                result.add(co)
                stack.append(co)
        return frozenset(result)

    def rec(courses):
        if len(leaves) >= LEAF_CAP:
            raise PlanError("TOO_COMPLEX", "先修条件的选课组合过多，超出规划复杂度限制")
        courses = close_coreqs(courses)
        if courses is None or courses in seen:
            return
        seen.add(courses)
        # 目标课程要求：每组（互认闭包）任选一门实际修读；
        # 同组课程被多个目标引用时，一次修读即可满足全部引用
        for req in target_reqs:
            if req & (completed | courses):
                continue
            options = sorted(o for o in req if o in catalog and o not in completed)
            if not options:
                conflicts.append(
                    f"目标课程 {'、'.join(sorted(req))} 在目录中无可选课程，条件冲突")
                return
            for opt in options:
                rec(courses | {opt})
            return
        for code in sorted(courses):
            for group in catalog[code].prereq_groups:
                expanded = _expand_options(equiv, group)
                satisfied = expanded & (completed | courses)
                if code not in group:
                    # 课程自身仅经互认扩展进入本组：不能替代满足自己的先修
                    satisfied -= {code}
                if satisfied:
                    continue
                # 该组尚未满足：枚举可选课程（目录中不存在或已修的选项不可再选；
                # 仅经互认扩展进入本组的课程自身同样不可选）
                options = sorted(o for o in expanded if o in catalog and o not in completed
                                 and (code in group or o != code))
                if not options:
                    label = "、".join(group)
                    extra = sorted(expanded - set(group) - {code})
                    if extra:
                        label += f"（互认替代：{'、'.join(extra)}）"
                    conflicts.append(
                        f"课程 {code} 的先修组 [{label}] 在目录中无可选课程，条件冲突")
                    return
                for opt in options:
                    rec(courses | {opt})
                return
        leaves.append(courses)

    rec(frozenset(needed))
    return leaves, conflicts


# ---------------------------------------------------------------- 同修块

class _Block:
    """同修课程合并成的排课单元：必须同学期修读。"""

    __slots__ = ("codes", "credits", "seasons", "members")

    def __init__(self, members, credits, seasons):
        self.members = members              # tuple[str, ...]
        self.codes = tuple(sorted(members))
        self.credits = credits
        self.seasons = seasons              # frozenset，成员开课季节的交集


def _build_blocks(catalog, courses):
    parent = {c: c for c in courses}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for c in courses:
        for co in catalog[c].coreqs:
            if co in courses:
                a, b = find(c), find(co)
                if a != b:
                    parent[a] = b

    groups = {}
    for c in courses:
        groups.setdefault(find(c), []).append(c)

    blocks = []
    for members in groups.values():
        seasons = None
        credits = 0
        for m in members:
            credits += catalog[m].credits
            seasons = catalog[m].seasons if seasons is None else seasons & catalog[m].seasons
        if not seasons:
            raise PlanError(
                "REQUIREMENT_CONFLICT",
                f"同修课程 {'+'.join(sorted(members))} 没有共同开课学期，无法安排在同一学期",
            )
        blocks.append(_Block(tuple(sorted(members)), credits, frozenset(seasons)))
    blocks.sort(key=lambda b: b.codes)
    return blocks


def _block_requirements(catalog, completed, blocks, equiv):
    """每个块的先修要求：列表的每项是一个块编号集合，须至少一个块排在更早学期。

    先修组选项按互认组扩展：同组课程（含替代课程）排在更早学期即满足。
    """
    block_of = {}
    for bi, b in enumerate(blocks):
        for c in b.members:
            block_of[c] = bi
    reqs = []
    for b in blocks:
        rb = []
        for c in b.members:
            for group in catalog[c].prereq_groups:
                expanded = _expand_options(equiv, group)
                if expanded & completed:
                    continue
                opts = frozenset(block_of[o] for o in expanded if o in block_of)
                if not opts:  # 防御：闭包阶段已保证每组有可选课程
                    return None
                rb.append(opts)
        reqs.append(rb)
    return reqs


def _fixed_assignments(blocks, lock_items, start_season):
    """把锁定课程映射到所属同修块，返回 {块编号: 锁定学期序号}。

    同一同修块被锁定到不同学期、或锁定学期该块不开课时，抛出
    REQUIREMENT_CONFLICT（锁定与约束冲突）。
    """
    block_of = {}
    for bi, b in enumerate(blocks):
        for c in b.members:
            block_of[c] = bi
    fixed = {}
    fixed_by = {}
    for code, idx, label in lock_items:
        bi = block_of[code]
        if bi in fixed and fixed[bi] != idx:
            raise PlanError(
                "REQUIREMENT_CONFLICT",
                f"同修课程 {'+'.join(blocks[bi].codes)} 必须安排在同一学期，"
                f"但被锁定到不同学期，锁定与约束冲突",
            )
        fixed[bi] = idx
        fixed_by[bi] = (code, label)
    for bi, idx in fixed.items():
        if season_at(start_season, idx) not in blocks[bi].seasons:
            code, label = fixed_by[bi]
            seasons = "、".join(s for s in SEASONS if s in blocks[bi].seasons)
            raise PlanError(
                "REQUIREMENT_CONFLICT",
                f"锁定课程 {code} 在锁定学期 {label} 不开课"
                f"（该课及其同修课程仅 {seasons} 开课），锁定与约束冲突",
            )
    return fixed


# ---------------------------------------------------------------- 排课搜索

def _schedule(blocks, reqs, num_semesters, start_year, start_season, cap,
              fixed=None, unavailable=frozenset(), seats=None):
    """返回 (k, seq)：最少 k 个学期排完时字典序最小的方案；期限内无解返回 None。

    seq 为每学期课程代码（升序）组成的元组的元组，长度 k。
    fixed: {块编号: 学期序号}，锁定块必须且只能安排在该学期；
    unavailable: 不可排课学期序号集合，这些学期不安排任何课程；
    seats: 可选席位上下文 (seat_caps, occupied)：
        seat_caps 为 {(课程代码, 学期标签): 非负上限}（缺省即不限额），
        occupied 为 {(课程代码, 学期标签): 他方案已占用数}（不含本方案）。
        规划基于“当时余量”（上限-已占用）排课；同一块在方案内只出现一次，
        故不同块不会争抢同一 (课程, 学期) 名额，块内无重复课程。
    """
    fixed = fixed or {}
    seat_caps, occupied = seats or ({}, {})
    n = len(blocks)
    assign = [-1] * n
    counter = [0]

    def remaining_seat(block_code, idx):
        """该课程在第 idx 个学期的余量；未设上限视为无限。"""
        label = semester_label(start_year, start_season, idx)
        limit = seat_caps.get((block_code, label))
        if limit is None:
            return None
        return limit - occupied.get((block_code, label), 0)

    def dfs(i, k, remaining):
        counter[0] += 1
        if counter[0] > NODE_BUDGET:
            raise PlanError("TOO_COMPLEX", "排课搜索超出复杂度限制，请减少目标课程或缩短学期数")
        if not remaining:
            return True
        if i > k:
            return False
        # 剪枝：锁定学期已错过的块不可能再安排
        for b in remaining:
            f = fixed.get(b)
            if f is not None and f < i:
                return False
        # 剪枝：每个剩余块在 i..k 内至少有一个“匹配季节且有余量”的学期
        for b in remaining:
            f = fixed.get(b)
            windows = [f] if f is not None else range(i, k + 1)
            if not any(
                season_at(start_season, j) in blocks[b].seasons
                and j not in unavailable
                and all((r := remaining_seat(c, j)) is None or r >= 1
                        for c in blocks[b].codes)
                for j in windows
            ):
                return False
        # 剪枝：单季节块的学分不能超过剩余该季节学期的总容量
        for s in SEASONS:
            fixed_season = sum(blocks[b].credits for b in remaining
                               if blocks[b].seasons == frozenset({s}))
            slots = sum(1 for j in range(i, k + 1)
                        if season_at(start_season, j) == s and j not in unavailable)
            if fixed_season > cap * slots:
                return False
        if i in unavailable:
            return dfs(i + 1, k, remaining)  # 不可排课学期只能空过
        must = frozenset(b for b in remaining if fixed.get(b) == i)
        avail = [
            b for b in remaining
            if season_at(start_season, i) in blocks[b].seasons
            and fixed.get(b) in (None, i)  # 锁定块只能在其锁定学期安排
            and all(any(0 < assign[o] < i for o in opts) for opts in reqs[b])
            # 席位余量：块内每门实际修读课程在该学期都须至少剩 1 个名额
            and all((r := remaining_seat(c, i)) is None or r >= 1
                    for c in blocks[b].codes)
        ]
        if not must <= frozenset(avail):
            return False  # 锁定块本学期无法安排（先修未满足等）
        # 按“本学期课程代码序列”的字典序枚举本学期子集（必含锁定块），首个可行即字典序最小
        optional = [b for b in avail if b not in must]
        must_credits = sum(blocks[b].credits for b in must)
        candidates = []
        for r in range(len(optional) + 1):
            for extra in combinations(optional, r):
                if must_credits + sum(blocks[b].credits for b in extra) <= cap:
                    combo = tuple(must) + extra
                    key = tuple(sorted(c for b in combo for c in blocks[b].codes))
                    candidates.append((key, combo))
        candidates.sort(key=lambda x: x[0])
        for _, combo in candidates:
            for b in combo:
                assign[b] = i
            if dfs(i + 1, k, remaining - frozenset(combo)):
                return True
            for b in combo:
                assign[b] = -1
        return False

    min_k = max(fixed.values(), default=1)  # 完成学期不早于最晚的锁定学期
    for k in range(min_k, num_semesters + 1):
        for b in range(n):
            assign[b] = -1
        if dfs(1, k, frozenset(range(n))):
            seq = tuple(
                tuple(sorted(c for b in range(n) if assign[b] == i for c in blocks[b].codes))
                for i in range(1, k + 1)
            )
            return k, seq
    return None


def _deadlocked_codes(blocks, reqs):
    """忽略季节与学分限制仍无法排入的课程（先修循环/不可满足），用于说明原因。"""
    assigned = set()
    progress = True
    while progress:
        progress = False
        for b in range(len(blocks)):
            if b in assigned:
                continue
            if all(any(o in assigned for o in opts) for opts in reqs[b]):
                assigned.add(b)
                progress = True
    return sorted(c for b in range(len(blocks)) if b not in assigned for c in blocks[b].codes)


def _seat_full_detail(codes, label, seat_caps, occupied):
    """块在某学期无余位时，返回首个已满课程的说明，如 CS401 在 2027-FALL 席位已满（上限 1，已占用 1）。"""
    for c in codes:
        limit = seat_caps.get((c, label))
        if limit is not None and limit - occupied.get((c, label), 0) <= 0:
            return f"{c} 在 {label} 席位已满（上限 {limit}，已占用 {occupied.get((c, label), 0)}）"
    return None


def _seat_bottlenecks(blocks, fixed, unavailable, start_year, start_season, scan_k,
                      seat_caps, occupied):
    """放宽学期数与学分上限后仍因席位排不进去的块：其每个可开课学期都无余位。

    返回面向用户的具体原因列表（课程 + 已满学期 + 上限/已占用）。
    """
    notes = []
    for b in blocks:
        f = fixed.get(b)
        windows = [f] if f is not None else range(1, scan_k + 1)
        details = []
        has_open = False
        for j in windows:
            if j in unavailable or season_at(start_season, j) not in blocks[b].seasons:
                continue
            label = semester_label(start_year, start_season, j)
            detail = _seat_full_detail(b.codes, label, seat_caps, occupied)
            if detail is None:
                has_open = True
                break
            details.append(detail)
        if not has_open and details:
            notes.append(f"课程 {'+'.join(b.codes)} 受席位余量限制无学期可排（{details[0]}）")
    return notes


def schedule_course_set(catalog, courses, *, completed, equiv, lock_items=(),
                        unavailable_idx=frozenset(), start_year, start_season,
                        max_credits, num_semesters, seat_caps=None, occupied=None):
    """尝试把一个候选课程集合排进期限，返回 (sched, error)。

    成功：sched=(k, seq)、error=None；失败：sched=None、error=(kind, message)，
    kind 为 'conflict'（结构性条件冲突）或 'no_solution'（期限/学分/席位导致无解）。
    供 build_plan 与培养方案达成课表共用，诊断口径保持一致。
    """
    seat_caps = seat_caps or {}
    occupied = occupied or {}
    try:
        blocks = _build_blocks(catalog, courses)
    except PlanError as exc:
        return None, ("conflict", exc.message)
    reqs = _block_requirements(catalog, completed, blocks, equiv)
    if reqs is None:
        return None, ("conflict", "先修条件无法满足，条件冲突")
    try:
        fixed = _fixed_assignments(blocks, lock_items, start_season)
    except PlanError as exc:
        return None, ("conflict", exc.message)
    heavy = next((b for b in blocks if b.credits > max_credits), None)
    if heavy is not None:
        return None, ("no_solution",
                      f"同修课程 {'+'.join(heavy.codes)} 学分合计 {heavy.credits} "
                      f"超过每学期学分上限 {max_credits}")
    seats = (seat_caps or {}, occupied or {})
    sched = _schedule(blocks, reqs, num_semesters, start_year, start_season, max_credits,
                      fixed=fixed, unavailable=unavailable_idx, seats=seats)
    if sched is not None:
        return sched, None
    scan_k = max(num_semesters, 2 * len(blocks) + 2)
    # 判定 1：保持原期限与学分上限，仅去掉席位约束
    seatless_same = _schedule(
        blocks, reqs, num_semesters, start_year, start_season, max_credits,
        fixed=fixed, unavailable=unavailable_idx) if seat_caps else None
    if seatless_same is not None:
        # 纯席位导致的期限内无解：定位“最短无席位解”上无余位的课程学期
        _, seq0 = seatless_same
        details = []
        for i, codes in enumerate(seq0, start=1):
            label = semester_label(start_year, start_season, i)
            for c in codes:
                limit = seat_caps.get((c, label))
                if limit is not None:
                    occ = occupied.get((c, label), 0)
                    if occ >= limit:
                        details.append(f"{c} 在 {label} 席位已满"
                                       f"（上限 {limit}，已占用 {occ}）")
        return None, ("no_solution",
                      f"受席位余量限制，{num_semesters} 个学期内无法排完所需课程："
                      + ("；".join(details[:3]) if details else "相关课程学期席位余量不足"))
    # 判定 2：放宽学期数与学分上限（席位上限保留）
    relaxed = _schedule(blocks, reqs, scan_k, start_year, start_season,
                        sum(b.credits for b in blocks),
                        fixed=fixed, unavailable=unavailable_idx, seats=seats)
    if relaxed is not None:
        factors = []
        if lock_items:
            factors.append("锁定安排")
        if unavailable_idx:
            factors.append("不可排课学期")
        factors += ["开课学期", "先修顺序"]
        return None, ("no_solution",
                      f"受{'、'.join(factors)}或每学期学分上限 {max_credits} 限制，"
                      f"{num_semesters} 个学期内无法排完所需课程")
    # 放宽学期与学分仍不行：去掉席位上限再试，区分结构性冲突与席位封死
    seatless = _schedule(blocks, reqs, scan_k, start_year, start_season,
                         sum(b.credits for b in blocks),
                         fixed=fixed, unavailable=unavailable_idx)
    if seatless is not None and seat_caps:
        notes = _seat_bottlenecks(blocks, fixed, unavailable_idx, start_year, start_season,
                                  scan_k, seat_caps, occupied)
        return None, ("no_solution",
                      "受席位余量限制，放宽学期数与学分上限仍无法排出课表："
                      + "；".join(notes[:3]))
    dead = "、".join(_deadlocked_codes(blocks, reqs))
    if lock_items:
        note = "锁定安排与先修/同修条件冲突，无法排出满足全部锁定的课表"
        note += f"，涉及课程：{dead}" if dead else ""
    else:
        note = f"先修条件存在循环依赖或无法满足，涉及课程：{dead}"
    return None, ("conflict", note)


def format_schedule(catalog, start_year, start_season, k, seq):
    """(k, seq) 排课结果 -> 对外的课表结构（学分取自所给目录版本）。"""
    semesters = []
    for i, codes in enumerate(seq, start=1):
        courses_in_sem = [
            {"code": c, "name": catalog[c].name, "credits": catalog[c].credits} for c in codes
        ]
        semesters.append({
            "semester": semester_label(start_year, start_season, i),
            "courses": courses_in_sem,
            "total_credits": sum(c["credits"] for c in courses_in_sem),
        })
    return {
        "completion_semester": semester_label(start_year, start_season, k),
        "total_credits": sum(s["total_credits"] for s in semesters),
        "semesters": semesters,
    }


# ---------------------------------------------------------------- 主入口

def build_plan(catalog, completed, targets, start_semester, max_credits, num_semesters,
               locked=None, unavailable=None, equiv_groups=None,
               seat_caps=None, occupied=None):
    """生成完整方案；失败抛出 PlanError（不返回部分课表）。

    catalog:     {code: Course}
    completed:   已修课程代码集合
    targets:     目标课程代码列表（非空）
    locked:      可选锁定安排，(课程代码, 学期标签) 列表；锁定课程视为本次必修，
                 必须安排在指定学期，且学期须在规划期限内
    unavailable: 可选不可排课学期标签列表，这些学期不安排任何课程
    equiv_groups: 可选课程互认组（随目录版本声明）；组内课程可替代满足目标课程
                 与先修组选项，已修同组课程视为已满足；不豁免同修与锁定
    seat_caps:   可选 {(课程代码, 学期标签): 非负上限}；缺省（无键）表示不限额。
                 规划基于当时余量（上限 - occupied），余量为 0 的学期不得排入。
    occupied:    可选 {(课程代码, 学期标签): 他方案已占用数}；不含本方案自身占位。
                 滚动重排时调用方应剔除来源方案尚未结束学期的占位，避免与自身冲突。
    """
    start_year, start_season = parse_semester(start_semester)
    targets = list(dict.fromkeys(t.strip().upper() for t in targets))
    completed = {c.strip().upper() for c in completed}
    locked = locked or []
    unavailable = unavailable or []
    equiv = _equiv_map(equiv_groups)
    seat_caps = seat_caps or {}
    occupied = occupied or {}

    missing = [t for t in targets if t not in catalog]
    if missing:
        raise PlanError("COURSE_MISSING", f"课程缺失：{'、'.join(missing)} 不在当前目录版本中")

    # 锁定安排校验：格式、重复、期限、已修、缺失（不依赖候选集合，一次性检查）
    lock_items = []  # (课程代码, 学期序号, 学期标签)
    seen_locked = set()
    for item in locked:
        code, label = item
        code = code.strip().upper()
        if code in seen_locked:
            raise PlanError("INVALID_INPUT", f"课程 {code} 被重复锁定")
        seen_locked.add(code)
        idx = semester_index(start_year, start_season, label)  # 顺带校验学期格式
        if not 1 <= idx <= num_semesters:
            raise PlanError(
                "INVALID_INPUT",
                f"锁定课程 {code} 的学期 {label} 不在规划期限"
                f"（{start_semester} 起共 {num_semesters} 个学期）内")
        if code not in catalog:
            raise PlanError("COURSE_MISSING", f"课程缺失：{code} 不在当前目录版本中")
        if code in completed:
            raise PlanError("INVALID_INPUT", f"锁定课程 {code} 已是已修课程，不得再锁定排课")
        lock_items.append((code, idx, label.strip().upper()))

    # 不可排课学期：格式校验后换算为学期序号；期限外的学期不影响排课，忽略
    unavailable_idx = set()
    for label in unavailable:
        idx = semester_index(start_year, start_season, label)  # 顺带校验学期格式
        if 1 <= idx <= num_semesters:
            unavailable_idx.add(idx)

    for code, idx, label in lock_items:
        if idx in unavailable_idx:
            raise PlanError(
                "REQUIREMENT_CONFLICT",
                f"锁定课程 {code} 的学期 {label} 为不可排课学期，锁定与约束冲突")

    locked_codes = {code for code, _, _ in lock_items}
    # 目标课程要求：按互认闭包归并（同组多个目标一次修读即可满足）；
    # 已修同组课程视为已满足，不再排课。锁定课程必须精确修读，互认不得豁免。
    target_reqs = set()
    for t in targets:
        req = equiv.get(t, frozenset({t}))
        if req & completed:
            continue
        target_reqs.add(req)
    target_reqs = sorted(target_reqs, key=lambda r: sorted(r))
    needed = locked_codes - completed
    if not needed and not target_reqs:
        return {
            "completion_semester": None,
            "total_credits": 0,
            "semesters": [],
            "message": "目标课程均已完成，无需排课",
        }

    leaves, conflicts = _enumerate_sets(catalog, completed, needed, target_reqs, equiv)
    best = None
    no_solution_notes = []

    for courses in leaves:
        sched, error = schedule_course_set(
            catalog, courses, completed=completed, equiv=equiv,
            lock_items=lock_items, unavailable_idx=unavailable_idx,
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
        if best is None or sched < best:
            best = sched

    if best is None:
        if no_solution_notes:
            raise PlanError("NO_SOLUTION", no_solution_notes[0])
        raise PlanError("REQUIREMENT_CONFLICT",
                        conflicts[0] if conflicts else "先修/同修条件冲突，无法生成方案")

    k, seq = best
    return format_schedule(catalog, start_year, start_season, k, seq)
