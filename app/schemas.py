"""请求体的 Pydantic 模型与校验。非法输入（空目标、非正学分/上限等）一律拒绝。"""
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Season = Literal["SPRING", "FALL"]


def _norm_code(code: str) -> str:
    return code.strip().upper()


class CourseIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = ""
    credits: int = Field(gt=0, description="学分必须为正整数")
    semesters: list[Season] = Field(min_length=1, description="开课季节：SPRING/FALL")
    prereq_groups: list[list[str]] = Field(
        default_factory=list, description="先修条件：每组任选一门，各组均须满足")
    coreqs: list[str] = Field(default_factory=list, description="同修课程")

    @field_validator("code")
    @classmethod
    def norm_code(cls, v):
        return _norm_code(v)

    @field_validator("semesters")
    @classmethod
    def dedupe_semesters(cls, v):
        return sorted(set(v))

    @field_validator("prereq_groups")
    @classmethod
    def check_groups(cls, v):
        groups = []
        for g in v:
            if not g:
                raise ValueError("先修组不能为空")
            groups.append(sorted({_norm_code(x) for x in g}))
        return groups

    @field_validator("coreqs")
    @classmethod
    def check_coreqs(cls, v):
        return sorted({_norm_code(x) for x in v})


class CatalogIn(BaseModel):
    """目录修订：全量提交一套课程（可选互认组），形成新版本。"""

    note: str = ""
    courses: list[CourseIn] = Field(min_length=1)
    equivalence_groups: list[list[str]] = Field(
        default_factory=list,
        description="课程互认组：组内课程可替代满足目标课程与先修组选项；"
                    "组间互不重叠，每组至少两门且均须存在于本次提交的课程中")

    @field_validator("courses")
    @classmethod
    def unique_codes(cls, v):
        codes = [c.code for c in v]
        if len(codes) != len(set(codes)):
            raise ValueError("课程代码重复")
        return v

    @field_validator("equivalence_groups")
    @classmethod
    def norm_equiv_groups(cls, v):
        return [sorted({_norm_code(x) for x in g if x.strip()}) for g in v]

    @model_validator(mode="after")
    def check_equiv_groups(self):
        """互认组校验：含不存在课程、重复归组或少于两门均拒绝（不生成版本）。"""
        codes = {c.code for c in self.courses}
        seen = {}
        for i, group in enumerate(self.equivalence_groups, start=1):
            if len(group) < 2:
                raise ValueError(f"互认组 {i} 少于两门课程，无法成组")
            missing = [c for c in group if c not in codes]
            if missing:
                raise ValueError(f"互认组 {i} 包含不存在的课程：{'、'.join(missing)}")
            for c in group:
                if c in seen:
                    raise ValueError(
                        f"课程 {c} 被重复归入互认组（第 {seen[c]}、{i} 组），互认组不得重叠")
                seen[c] = i
        return self


class LockedPlacement(BaseModel):
    """锁定安排：课程必须安排在指定学期（学期须在规划期限内）。"""

    course: str = Field(min_length=1, max_length=32, description="锁定课程代码")
    semester: str = Field(description="锁定学期，如 2026-FALL")

    @field_validator("course")
    @classmethod
    def norm_course(cls, v):
        return _norm_code(v)


class PlanIn(BaseModel):
    completed_courses: list[str] = Field(default_factory=list, description="已修课程")
    target_courses: list[str] = Field(min_length=1, description="目标课程，非空")
    start_semester: str = Field(description="起始学期，如 2026-SPRING")
    max_credits_per_semester: int = Field(gt=0, description="每学期学分上限，必须为正")
    num_semesters: int = Field(gt=0, description="规划学期数，必须为正")
    locked: list[LockedPlacement] = Field(
        default_factory=list,
        description="锁定安排：课程视为本次必修并须安排在指定学期（即使不在目标课程中）")
    unavailable_semesters: list[str] = Field(
        default_factory=list, description="不可排课学期：这些学期不安排任何课程")

    @field_validator("completed_courses", "target_courses")
    @classmethod
    def norm_codes(cls, v, info):
        codes = sorted({_norm_code(x) for x in v if x.strip()})
        if info.field_name == "target_courses" and not codes:
            raise ValueError("目标课程不能为空")
        return codes


class SeatCapIn(BaseModel):
    """按课程代码与学期设置席位上限；非负整数，未设置即不限额。"""

    cap: int = Field(ge=0, description="非负席位上限；0 表示该学期已不可再占")


class ReplanIn(BaseModel):
    """方案执行中途的滚动重排输入。

    - elapsed_semesters：已结束学期数，须大于 0 且小于原规划期限；
    - completed_courses：截至该时点的累计已修课程，
      不得撤销原方案创建时声明的已修课程（须为其超集）。
    """

    elapsed_semesters: int = Field(description="已结束学期数（0 < x < 原规划学期数）")
    completed_courses: list[str] = Field(
        default_factory=list, description="截至该时点的累计已修课程（须包含原方案声明的已修）")

    @field_validator("elapsed_semesters")
    @classmethod
    def elapsed_positive(cls, v):
        if v <= 0:
            raise ValueError("已结束学期数必须大于 0")
        return v

    @field_validator("completed_courses")
    @classmethod
    def norm_codes(cls, v):
        return sorted({_norm_code(x) for x in v if x.strip()})


# ---------------------------------------------------------------- 培养方案要求与达成审核

class ElectiveGroupIn(BaseModel):
    """选修组：候选课程集合 + 最低学分门槛（必须为正整数）。

    组内候选代码自动去重；同一门课允许出现在不同选修组或必修项中。
    """

    name: str = Field(default="", description="选修组名称，如“专业选修课”")
    candidates: list[str] = Field(min_length=1, description="候选课程代码（非空，组内去重）")
    min_credits: int = Field(description="该组最低学分门槛，必须为正整数")

    @field_validator("candidates")
    @classmethod
    def norm_candidates(cls, v):
        codes = sorted({_norm_code(x) for x in v if x.strip()})
        if not codes:
            raise ValueError("选修组候选课程不能为空")
        return codes

    @field_validator("min_credits")
    @classmethod
    def positive_threshold(cls, v):
        if v <= 0:
            raise ValueError("选修组最低学分门槛必须为正整数")
        return v


class RequirementVersionIn(BaseModel):
    """教务员按培养方案编号登记的要求版本；必须绑定一个既有课程目录版本。

    - program_code：培养方案编号（编号内版本号从 1 起递增，要求一经登记不可修改）；
    - catalog_version_id：绑定的既有目录版本（互认关系与学分均取自该版本快照）；
    - required_courses：必修课程（重复必修项一律拒绝）；
    - elective_groups：按优先级排列的选修组，每组候选课程 + 正学分门槛。
    """

    program_code: str = Field(min_length=1, max_length=64, description="培养方案编号")
    catalog_version_id: int = Field(description="绑定的既有课程目录版本号")
    note: str = ""
    required_courses: list[str] = Field(default_factory=list, description="必修课程代码")
    elective_groups: list[ElectiveGroupIn] = Field(
        default_factory=list, description="选修组（按优先级排列）")

    @field_validator("program_code")
    @classmethod
    def norm_program(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("培养方案编号不能为空")
        return v

    @field_validator("required_courses")
    @classmethod
    def norm_required(cls, v):
        codes = [_norm_code(x) for x in v if x.strip()]
        dupes = sorted({c for c in codes if codes.count(c) > 1})
        if dupes:
            raise ValueError(f"必修课程重复登记：{'、'.join(dupes)}")
        return codes


class AuditIn(BaseModel):
    """达成审核请求：指定要求版本 + 实际已修课程清单。

    已修课程均须存在于要求版本绑定的目录版本中；未知课程拒绝且不留记录。
    """

    completed_courses: list[str] = Field(default_factory=list, description="实际已修课程代码")

    @field_validator("completed_courses")
    @classmethod
    def norm_codes(cls, v):
        return sorted({_norm_code(x) for x in v if x.strip()})


class ProgramPlanIn(BaseModel):
    """培养方案达成课表请求：要求版本 + 累计已修 + 起始学期 + 学期学分上限 + 规划学期数。

    要求绑定目录决定互认与计入学分；当前目录决定未来课程的开课、先修、同修、排课
    学分与席位。仅两版目录均有的拟修课可计入要求；已修课须存在于绑定目录中。
    """

    completed_courses: list[str] = Field(default_factory=list, description="累计已修课程代码")
    start_semester: str = Field(description="起始学期，如 2026-SPRING")
    max_credits_per_semester: int = Field(gt=0, description="每学期学分上限，必须为正")
    num_semesters: int = Field(gt=0, description="规划学期数，必须为正")

    @field_validator("completed_courses")
    @classmethod
    def norm_codes(cls, v):
        return sorted({_norm_code(x) for x in v if x.strip()})
