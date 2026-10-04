"""高校课程学期规划服务 —— FastAPI 入口。"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import db
from .audit import run_audit
from .batch_planner import build_batch
from .planner import Course, PlanError, build_plan, format_schedule, parse_semester
from .program_plan import build_program_plan
from .replan import replan_plan, replan_program_plan
from .schemas import (AuditIn, BatchIn, CatalogIn, PlanIn, ProgramPlanIn, ReplanIn,
                      RequirementVersionIn, SeatCapIn)


@asynccontextmanager
async def lifespan(_):
    db.init_db()  # 建表；首次启动写入种子目录（版本 1）
    yield


app = FastAPI(
    title="高校课程学期规划服务",
    version="1.0.0",
    description="基于课程目录版本的学期排课规划：先修组、同修、开课季节、学分上限约束下的最早完成方案。",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def invalid_input_handler(_, exc):
    errors = [
        {"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"detail": {"code": "INVALID_INPUT", "message": "非法输入", "errors": errors}},
    )


def _to_catalog(rows):
    return {
        r["code"]: Course(
            code=r["code"], name=r["name"], credits=r["credits"],
            seasons=frozenset(r["semesters"]),
            prereq_groups=tuple(tuple(g) for g in r["prereq_groups"]),
            coreqs=tuple(r["coreqs"]),
        )
        for r in rows
    }


def _plan_out(row):
    request_data = json.loads(row["request_json"])
    out = {
        "plan_id": row["id"],
        "status": row["status"],
        "catalog_version": row["version_id"],
        "request": request_data,
        "result": json.loads(row["result_json"]),
        "created_at": row["created_at"],
    }
    source = row["source_plan_id"] if "source_plan_id" in row.keys() else None
    if source:
        out["source_plan_id"] = source
        out["replan"] = json.loads(row["replan_json"])
    attribution = row["attribution_json"] if "attribution_json" in row.keys() else None
    if attribution:
        out["attribution"] = json.loads(attribution)
        # 培养方案达成课表：回显类型与绑定信息，便于与普通规划区分、回看
        out["plan_type"] = "program_plan"
        out["requirement_version_id"] = request_data.get("requirement_version_id")
        out["bound_catalog_version"] = request_data.get("bound_catalog_version")
    if "batch_id" in row.keys() and row["batch_id"]:
        out["batch_id"] = row["batch_id"]
    return out


def _validate_seat_ref(conn, course_code, semester):
    """校验席位引用的学期格式与课程是否在当前目录中；返回规范化的 (课程, 学期)。

    非法学期 -> INVALID_INPUT(400)；未知当前目录课程 -> COURSE_MISSING(422)。
    """
    course_code = course_code.strip().upper()
    try:
        parse_semester(semester)
    except PlanError as exc:
        raise HTTPException(status_code=400,
                            detail={"code": exc.code, "message": exc.message})
    semester = semester.strip().upper()
    version = db.get_current_version(conn)
    codes = {r["code"] for r in db.load_courses(conn, version["id"])}
    if course_code not in codes:
        raise HTTPException(status_code=422, detail={
            "code": "COURSE_MISSING",
            "message": f"课程缺失：{course_code} 不在当前目录版本（{version['id']}）中"})
    return course_code, semester.strip().upper()


def _seat_out(conn, course_code, semester):
    """单个席位上限视图：cap 为 null 表示不限额，occupied 为已占用，remaining 为余量。"""
    cap = db.get_seat_cap(conn, course_code, semester)
    occupied = db.seat_occupied(conn, course_code, semester)
    return {
        "course_code": course_code,
        "semester": semester,
        "cap": cap,
        "occupied": occupied,
        "remaining": None if cap is None else cap - occupied,
    }


# ---------------------------------------------------------------- 基础

@app.get("/")
def root():
    return {"service": "高校课程学期规划服务", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 课程目录

@app.post("/catalog/versions", status_code=201)
def create_catalog(payload: CatalogIn):
    """目录修订：全量提交课程集合（可选互不重叠的课程互认组），生成新版本并成为当前版本。

    互认组含不存在课程、重复归组或少于两门时拒绝（422），不生成版本。
    """
    courses = [
        {
            "code": c.code, "name": c.name, "credits": c.credits,
            "semesters": c.semesters, "prereq_groups": c.prereq_groups, "coreqs": c.coreqs,
        }
        for c in payload.courses
    ]
    groups = [list(g) for g in payload.equivalence_groups]
    with db.session() as conn:
        vid = db.create_catalog_version(conn, payload.note, courses, groups)
        version = db.get_version(conn, vid)
    return {
        "version_id": vid,
        "note": version["note"],
        "course_count": len(courses),
        "equivalence_groups": groups,
        "created_at": version["created_at"],
    }


@app.get("/catalog/current")
def current_catalog():
    with db.session() as conn:
        version = db.get_current_version(conn)
        courses = db.load_courses(conn, version["id"])
        groups = db.load_equiv_groups(conn, version["id"])
    return {
        "version_id": version["id"],
        "note": version["note"],
        "created_at": version["created_at"],
        "courses": courses,
        "equivalence_groups": groups,
    }


@app.get("/catalog/versions/{version_id}")
def catalog_version(version_id: int):
    """查看历史目录版本（供回看方案所绑定的目录）。"""
    with db.session() as conn:
        version = db.get_version(conn, version_id)
        if version is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND", "message": f"目录版本 {version_id} 不存在"})
        courses = db.load_courses(conn, version_id)
        groups = db.load_equiv_groups(conn, version_id)
    return {
        "version_id": version["id"],
        "note": version["note"],
        "created_at": version["created_at"],
        "courses": courses,
        "equivalence_groups": groups,
    }


# ---------------------------------------------------------------- 培养方案要求版本

@app.post("/programs/{program_code}/requirement-versions", status_code=201)
def create_requirement_version(program_code: str, payload: RequirementVersionIn):
    """教务员按培养方案编号登记要求版本，绑定一个既有课程目录版本。

    编号内版本号自 1 递增；必修重复项、非正学分门槛、候选引用绑定版本中不存在的
    课程均拒绝（422），绑定目录版本不存在返回 404；任何失败都不写入。
    要求一经登记即不可修改，后续目录修订不影响本版本。
    """
    program_code = program_code.strip()
    with db.session() as conn:
        version = db.get_version(conn, payload.catalog_version_id)
        if version is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND",
                "message": f"课程目录版本 {payload.catalog_version_id} 不存在，无法绑定"})
        codes = {r["code"] for r in db.load_courses(conn, payload.catalog_version_id)}
        missing = [c for c in payload.required_courses if c not in codes]
        for gp in payload.elective_groups:
            missing += [c for c in gp.candidates if c not in codes]
        if missing:
            raise HTTPException(status_code=422, detail={
                "code": "COURSE_MISSING",
                "message": "课程缺失（不在绑定的目录版本 "
                           f"{payload.catalog_version_id} 中）：{'、'.join(sorted(set(missing)))}"})
        # 以路径中的培养方案编号为准登记
        version_no = db.next_requirement_version(conn, program_code)
        rv_id = f"{program_code}:v{version_no}"
        content = {
            "required_courses": payload.required_courses,
            "elective_groups": [
                {"name": gp.name, "candidates": gp.candidates, "min_credits": gp.min_credits}
                for gp in payload.elective_groups
            ],
        }
        db.insert_requirement_version(
            conn, rv_id, program_code, version_no,
            payload.catalog_version_id, payload.note, content)
        row = db.get_requirement_version(conn, rv_id)
    out = db.requirement_version_out(row)
    return out


@app.get("/programs/requirement-versions")
def list_all_requirement_versions():
    """列出全部培养方案的要求版本（按培养方案编号、版本倒序）。"""
    with db.session() as conn:
        rows = db.list_requirement_versions(conn)
    return {"requirement_versions": [db.requirement_version_out(r) for r in rows]}


@app.get("/programs/{program_code}/requirement-versions")
def list_requirement_versions(program_code: str):
    """列出某培养方案编号的全部要求版本（版本倒序）。"""
    with db.session() as conn:
        rows = db.list_requirement_versions(conn, program_code.strip())
    return {"requirement_versions": [db.requirement_version_out(r) for r in rows]}


@app.get("/programs/requirement-versions/{rv_id}")
def get_requirement_version(rv_id: str):
    """回看单个要求版本（含必修与按优先级排列的选修组）。"""
    with db.session() as conn:
        row = db.get_requirement_version(conn, rv_id)
        if row is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND", "message": f"要求版本 {rv_id} 不存在"})
    return db.requirement_version_out(row)


# ---------------------------------------------------------------- 达成审核

def _audit_snapshot(conn, row, completed):
    """按要求版本绑定的目录快照计算审核：学分与互认组均取自绑定版本。"""
    catalog_rows = db.load_courses(conn, row["catalog_version_id"])
    credits = {r["code"]: r["credits"] for r in catalog_rows}
    equiv_groups = db.load_equiv_groups(conn, row["catalog_version_id"])
    content = json.loads(row["content_json"])
    return run_audit(
        credits=credits,
        equiv_groups=equiv_groups,
        required=content["required_courses"],
        elective_groups=content["elective_groups"],
        completed=completed,
    )


@app.post("/requirement-versions/{rv_id}/audits", status_code=201)
def create_audit(rv_id: str, payload: AuditIn):
    """以要求版本和已修课程清单生成可回看的达成审核并落库。

    互认课程可满足对应必修或选修候选，但一门已修课只归属一项、同一互认组至多计入
    一门；先最大化必修满足数，再按选修组顺序最大化各组计入学分（不超过门槛），
    仍相同取归属课程代码序列字典序最小。未知课程拒绝（422）且不留记录。
    审核保存的是快照，后续目录或要求修订不改变历史审核。
    """
    with db.session() as conn:
        row = db.get_requirement_version(conn, rv_id)
        if row is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND", "message": f"要求版本 {rv_id} 不存在"})
        try:
            result = _audit_snapshot(conn, row, payload.completed_courses)
        except PlanError as exc:
            status = 400 if exc.code == "INVALID_INPUT" else 422
            raise HTTPException(status_code=status,
                                detail={"code": exc.code, "message": exc.message})
        audit_id = uuid.uuid4().hex[:12]
        db.insert_audit(conn, audit_id, rv_id, row["catalog_version_id"],
                        payload.completed_courses, result)
        saved = db.get_audit(conn, audit_id)
    return db.audit_out(saved)


@app.get("/audits")
def list_audits(requirement_version_id: str | None = None):
    """审核列表；可用 ?requirement_version_id= 过滤某要求版本的全部历史审核。"""
    with db.session() as conn:
        if requirement_version_id is not None:
            rows = db.list_audits(conn, requirement_version_id)
        else:
            rows = db.list_audits(conn)
    return {"audits": [db.audit_out(r) for r in rows]}


@app.get("/audits/{audit_id}")
def get_audit(audit_id: str):
    """回看历史审核（结果为生成时的快照，不随后续目录/要求修订变化）。"""
    with db.session() as conn:
        row = db.get_audit(conn, audit_id)
        if row is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND", "message": f"审核 {audit_id} 不存在"})
    return db.audit_out(row)


# ---------------------------------------------------------------- 培养方案达成课表

@app.post("/requirement-versions/{rv_id}/plans", status_code=201)
def create_program_plan(rv_id: str, payload: ProgramPlanIn):
    """凭要求版本、累计已修与规划期限生成达成课表。

    要求绑定目录决定互认与计入学分（仅两版目录均有的拟修课可计入要求，当前目录独有
    课程只作先修/同修必要课）；当前目录决定开课、先修、同修、排课学分与席位。返回期限
    内最早完成（同完成学期按学期课程代码序列字典序决胜）的完整课表，以及合并已修与
    拟修后的预计归属（一门课只归属一项、同互认组至多计一门）。

    已全部达成时返回 200 空课表（不保存）；未知要求版本 404、未知已修课 422、期限内
    无解 422 且不保存部分方案。课表待确认、不预占席位；确认沿用既有
    `POST /plans/{id}/confirm`（目录版本与席位原子复核）。
    """
    with db.session() as conn:
        rv = db.get_requirement_version(conn, rv_id)
        if rv is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND", "message": f"要求版本 {rv_id} 不存在"})
        version = db.get_current_version(conn)
        catalog = _to_catalog(db.load_courses(conn, version["id"]))
        current_equiv = db.load_equiv_groups(conn, version["id"])
        bound_rows = db.load_courses(conn, rv["catalog_version_id"])
        bound_equiv = db.load_equiv_groups(conn, rv["catalog_version_id"])
        seat_caps = db.load_seat_caps(conn)
        occupied = db.seat_occupied_map(conn)
        content = json.loads(rv["content_json"])
        try:
            schedule, attribution = build_program_plan(
                bound_rows=bound_rows,
                bound_equiv_groups=bound_equiv,
                current_catalog=catalog,
                current_equiv_groups=current_equiv,
                required=content["required_courses"],
                elective_groups=content["elective_groups"],
                completed=set(payload.completed_courses),
                start_semester=payload.start_semester,
                max_credits=payload.max_credits_per_semester,
                num_semesters=payload.num_semesters,
                seat_caps=seat_caps,
                occupied=occupied,
            )
        except PlanError as exc:
            status = 400 if exc.code == "INVALID_INPUT" else 422
            raise HTTPException(status_code=status,
                                detail={"code": exc.code, "message": exc.message})

        # 已达成：空课表，不保存、不占位
        if schedule is None:
            return JSONResponse(status_code=200, content={
                "plan_id": None,
                "status": "already_achieved",
                "plan_type": "program_plan",
                "requirement_version_id": rv_id,
                "catalog_version": version["id"],
                "bound_catalog_version": rv["catalog_version_id"],
                "request": payload.model_dump(),
                "result": {
                    "completion_semester": None,
                    "total_credits": 0,
                    "semesters": [],
                    "message": "全部必修与选修学分要求均已达成，无需排课",
                },
                "attribution": {
                    "requirement_version_id": rv_id,
                    "bound_catalog_version": rv["catalog_version_id"],
                    "completed_courses": payload.completed_courses,
                    "result": attribution,
                },
            })

        # 落库前复查：目录在计算期间被修订则拒绝旧版本结果（409，不保存）
        current = db.get_current_version(conn)
        if current["id"] != version["id"]:
            raise HTTPException(status_code=409, detail={
                "code": "CATALOG_CHANGED",
                "message": f"目录在规划期间已修订（计算基于版本 {version['id']}，"
                           f"当前版本 {current['id']}），旧版本结果被拒绝，请重新发起",
            })

        plan_id = uuid.uuid4().hex[:12]
        request_data = {
            "plan_type": "program_plan",
            "requirement_version_id": rv_id,
            "bound_catalog_version": rv["catalog_version_id"],
            "completed_courses": payload.completed_courses,
            # 以全部拟修课程作为普通规划目标回看：可确认、可重放；达成语义以归属为准
            "target_courses": sorted(
                {c["code"] for s in schedule["semesters"] for c in s["courses"]}),
            "start_semester": payload.start_semester,
            "max_credits_per_semester": payload.max_credits_per_semester,
            "num_semesters": payload.num_semesters,
        }
        projected = {
            "requirement_version_id": rv_id,
            "bound_catalog_version": rv["catalog_version_id"],
            "completed_courses": payload.completed_courses,
            "result": attribution,
        }
        db.insert_plan(conn, plan_id, version["id"], request_data, schedule,
                       attribution=projected)
        row = db.get_plan(conn, plan_id)
    return _plan_out(row)


# ---------------------------------------------------------------- 席位上限
@app.put("/seat-caps/{course_code}/{semester}")
def set_seat_cap(course_code: str, semester: str, payload: SeatCapIn):
    """按课程代码与学期设置非负席位上限（重复设置为覆盖）；未设置表示不限额。

    未知当前目录课程（422 COURSE_MISSING）、非法学期（400 INVALID_INPUT）拒绝；
    上限低于该 (课程,学期) 当前已占用席位时拒绝（409 CAP_BELOW_OCCUPIED），原上限不变。
    """
    with db.transaction() as conn:
        course_code, semester = _validate_seat_ref(conn, course_code, semester)
        occupied = db.seat_occupied(conn, course_code, semester)
        if payload.cap < occupied:
            raise HTTPException(status_code=409, detail={
                "code": "CAP_BELOW_OCCUPIED",
                "message": f"课程 {course_code} 在 {semester} 已占用 {occupied} 个席位，"
                           f"上限 {payload.cap} 低于已占用数，拒绝设置",
                "course_code": course_code, "semester": semester,
                "cap": payload.cap, "occupied": occupied})
        db.upsert_seat_cap(conn, course_code, semester, payload.cap)
        return _seat_out(conn, course_code, semester)


@app.get("/seat-caps")
def list_seat_caps():
    """列出全部已设置的席位上限（未设置即不限额，不出现在列表中）。"""
    with db.session() as conn:
        return {"seat_caps": db.list_seat_caps(conn)}


@app.get("/seat-caps/{course_code}/{semester}")
def get_seat_cap(course_code: str, semester: str):
    """查询单个 (课程,学期) 的上限、已占用与余量；cap/remaining 为 null 表示不限额。"""
    with db.session() as conn:
        course_code, semester = _validate_seat_ref(conn, course_code, semester)
        return _seat_out(conn, course_code, semester)


# ---------------------------------------------------------------- 规划方案

@app.post("/plans", status_code=201)
def create_plan(payload: PlanIn):
    """生成规划方案并绑定当前目录版本；失败时说明原因，不返回部分课表。

    可选锁定安排（locked：课程+学期）与不可排课学期（unavailable_semesters）：
    锁定课程视为本次必修并须安排在指定学期；不可排课学期不安排任何课程。
    不带这两个字段的请求行为与返回结构同既有版本一致。
    """
    with db.session() as conn:
        version = db.get_current_version(conn)
        catalog = _to_catalog(db.load_courses(conn, version["id"]))
        equiv_groups = db.load_equiv_groups(conn, version["id"])
        # 规划基于“当时余量”：待确认方案不占位，已占用仅来自全部已确认方案
        seat_caps = db.load_seat_caps(conn)
        occupied = db.seat_occupied_map(conn)
        try:
            result = build_plan(
                catalog,
                completed=set(payload.completed_courses),
                targets=payload.target_courses,
                start_semester=payload.start_semester,
                max_credits=payload.max_credits_per_semester,
                num_semesters=payload.num_semesters,
                locked=[(l.course, l.semester) for l in payload.locked],
                unavailable=payload.unavailable_semesters,
                equiv_groups=equiv_groups,
                seat_caps=seat_caps,
                occupied=occupied,
            )
        except PlanError as exc:
            status = 400 if exc.code == "INVALID_INPUT" else 422
            raise HTTPException(status_code=status,
                                detail={"code": exc.code, "message": exc.message})
        # 兼容：未提供新字段时，保存的请求回显不含这两个键，与既有格式一致
        request_data = payload.model_dump()
        for key in ("locked", "unavailable_semesters"):
            if not request_data.get(key):
                request_data.pop(key, None)
        plan_id = uuid.uuid4().hex[:12]
        db.insert_plan(conn, plan_id, version["id"], request_data, result)
        row = db.get_plan(conn, plan_id)
    return _plan_out(row)


@app.get("/plans")
def list_plans():
    with db.session() as conn:
        rows = db.list_plans(conn)
    out = []
    for r in rows:
        item = {
            "plan_id": r["id"],
            "status": r["status"],
            "catalog_version": r["version_id"],
            "completion_semester": json.loads(r["result_json"]).get("completion_semester"),
            "created_at": r["created_at"],
        }
        if r["source_plan_id"]:
            item["source_plan_id"] = r["source_plan_id"]
        if "batch_id" in r.keys() and r["batch_id"]:
            item["batch_id"] = r["batch_id"]
        out.append(item)
    return out


# ---------------------------------------------------------------- 批次联合排课

def _app_to_kwargs(app):
    """BatchApplicationIn -> batch_planner 使用的申请字典。"""
    data = app.model_dump()
    return {
        "application_id": data["application_id"],
        "completed_courses": data["completed_courses"],
        "target_courses": data["target_courses"],
        "start_semester": data["start_semester"],
        "max_credits_per_semester": data["max_credits_per_semester"],
        "num_semesters": data["num_semesters"],
        "locked": data.get("locked") or [],
        "unavailable_semesters": data.get("unavailable_semesters") or [],
    }


def _member_request_data(app_kwargs):
    """成员课表落库的 request（沿用普通规划结构，去掉申请标识；空键省略锁定字段）。"""
    req = {
        "completed_courses": app_kwargs["completed_courses"],
        "target_courses": app_kwargs["target_courses"],
        "start_semester": app_kwargs["start_semester"],
        "max_credits_per_semester": app_kwargs["max_credits_per_semester"],
        "num_semesters": app_kwargs["num_semesters"],
    }
    if app_kwargs.get("locked"):
        req["locked"] = app_kwargs["locked"]
    if app_kwargs.get("unavailable_semesters"):
        req["unavailable_semesters"] = app_kwargs["unavailable_semesters"]
    return req


@app.post("/plan-batches", status_code=201)
def create_plan_batch(payload: BatchIn):
    """一次提交多份普通课表申请，在同一当前目录与席位余量快照上联合排课。

    - 每份申请携带唯一非空申请标识及与 ``POST /plans`` 相同的规划输入；
      标识重复或任一申请参数非法时整批拒绝（422）且不保存。
    - 实际修读课程同一学期的批次席位总需求不得超额：先最大化获得完整课表的申请数，
      再按申请标识升序比较完成学期（未入选晚于任何完成学期），最后按申请标识升序
      比较入选课表逐学期课程代码序列，确定唯一结果。
    - 待确认结果不预占席位；至少一份可排即保存为待确认批次（未入选申请给出原因）。
      无一可排时返回 422 NO_SOLUTION 且不保存批次。
    """
    applications = [_app_to_kwargs(a) for a in payload.applications]
    app_by_id = {a["application_id"]: a for a in applications}
    with db.session() as conn:
        version = db.get_current_version(conn)
        catalog = _to_catalog(db.load_courses(conn, version["id"]))
        equiv_groups = db.load_equiv_groups(conn, version["id"])
        seat_caps = db.load_seat_caps(conn)
        occupied = db.seat_occupied_map(conn)
        try:
            batch_result = build_batch(
                catalog, applications,
                equiv_groups=equiv_groups, seat_caps=seat_caps, occupied=occupied)
        except PlanError as exc:
            status = 400 if exc.code == "INVALID_INPUT" else 422
            raise HTTPException(status_code=status,
                                detail={"code": exc.code, "message": exc.message})

        members = []
        selected_count = 0
        for app_id in batch_result["order"]:
            res = batch_result["results"][app_id]
            app_kwargs = app_by_id[app_id]
            if res["option"] is not None:
                selected_count += 1
                prepared = res["prepared"]
                if res["trivial"]:
                    schedule = {
                        "completion_semester": None,
                        "total_credits": 0,
                        "semesters": [],
                        "message": "目标课程均已完成，无需排课",
                    }
                else:
                    schedule = format_schedule(
                        catalog, prepared["start_year"], prepared["start_season"],
                        res["option"].k, res["option"].seq)
                members.append({
                    "application_id": app_id,
                    "selected": True,
                    "plan_id": None,  # 落库后回填
                    "request": _member_request_data(app_kwargs),
                    "result": schedule,
                })
            else:
                code, message = res["error"]
                members.append({
                    "application_id": app_id,
                    "selected": False,
                    "reason_code": code,
                    "reason": message,
                    "request": _member_request_data(app_kwargs),
                })

        if selected_count == 0:
            # 无一可排：说明无解且不保存批次
            raise HTTPException(status_code=422, detail={
                "code": "NO_SOLUTION",
                "message": "批次内无一申请可排出完整课表，无解，批次未保存",
                "applications": [
                    {"application_id": m["application_id"],
                     "reason_code": m["reason_code"], "reason": m["reason"]}
                    for m in members],
            })

        # 落库前复查：目录在计算期间被修订则拒绝旧版本结果（409，不保存）
        current = db.get_current_version(conn)
        if current["id"] != version["id"]:
            raise HTTPException(status_code=409, detail={
                "code": "CATALOG_CHANGED",
                "message": f"目录在批量规划期间已修订（计算基于版本 {version['id']}，"
                           f"当前版本 {current['id']}），旧版本结果被拒绝，请重新发起",
            })

        batch_id = uuid.uuid4().hex[:12]
        plan_rows = []
        for member in members:
            if not member["selected"]:
                continue
            plan_id = uuid.uuid4().hex[:12]
            member["plan_id"] = plan_id
            plan_rows.append((plan_id, member["request"], member["result"]))
        # 先建批次，再挂成员课表（同一事务，异常整体回滚）
        db.insert_plan_batch(conn, batch_id, version["id"],
                             {"applications": applications}, members)
        for plan_id, request_data, schedule in plan_rows:
            db.insert_plan(conn, plan_id, version["id"], request_data, schedule,
                           batch_id=batch_id)
        saved = db.get_plan_batch(conn, batch_id)
    return _batch_out(saved)


@app.get("/plan-batches")
def list_plan_batches():
    """批次列表（最近创建在前）。"""
    with db.session() as conn:
        rows = db.list_plan_batches(conn)
    return {"batches": [
        {"batch_id": r["id"], "status": r["status"], "catalog_version": r["version_id"],
         "created_at": r["created_at"],
         "selected": sum(1 for m in json.loads(r["members_json"]) if m["selected"]),
         "total": len(json.loads(r["members_json"]))}
        for r in rows]}


@app.get("/plan-batches/{batch_id}")
def get_plan_batch(batch_id: str):
    """回看批次：逐申请的完整课表或未入选原因（含绑定目录版本与确认状态）。"""
    with db.session() as conn:
        row = db.get_plan_batch(conn, batch_id)
        if row is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND", "message": f"批次 {batch_id} 不存在"})
    return _batch_out(row)


def _batch_out(row):
    members = json.loads(row["members_json"])
    return {
        "batch_id": row["id"],
        "status": row["status"],
        "catalog_version": row["version_id"],
        "created_at": row["created_at"],
        "selected_count": sum(1 for m in members if m["selected"]),
        "total_applications": len(members),
        "applications": members,
    }


@app.post("/plan-batches/{batch_id}/confirm")
def confirm_plan_batch(batch_id: str):
    """确认整个批次：单事务内原子复核目录版本与整批席位，成功则一并占位。

    - 重复确认幂等（已确认直接返回，不重复占位）；
    - 目录自批次生成后修订 -> 409 CATALOG_CHANGED，整批拒绝、原占位不变；
    - 任一入选课表席位不足 -> 409 SEAT_CONFLICT，整笔事务回滚，原占位不变、不超卖。
    """
    with db.transaction() as conn:
        batch = db.get_plan_batch(conn, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND", "message": f"批次 {batch_id} 不存在"})
        if batch["status"] == "confirmed":
            return _batch_out(batch)  # 幂等：不重复占位

        current = db.get_current_version(conn)
        if batch["version_id"] != current["id"]:
            raise HTTPException(status_code=409, detail={
                "code": "CATALOG_CHANGED",
                "message": f"目录已修订（当前版本 {current['id']}，批次基于版本 "
                           f"{batch['version_id']}），整批确认被拒绝、原占位不变，"
                           "请重新发起批量规划",
            })

        members = json.loads(batch["members_json"])
        plan_ids = {m["plan_id"] for m in members if m["selected"]}
        plans = {r["id"]: r for r in db.list_plans_by_batch(conn, batch_id)}
        if set(plans) != plan_ids:  # 防御：成员课表缺失属外部破坏
            raise HTTPException(status_code=409, detail={
                "code": "BATCH_BROKEN",
                "message": "批次成员课表不完整，确认被拒绝（数据可能被外部修改）"})

        # 整批席位复核：把全部入选课表的实际修读需求合并计数
        needs = {}
        for plan_id in plans:
            for code, semester in _placements_from_result(
                    json.loads(plans[plan_id]["result_json"])):
                needs[(code, semester)] = needs.get((code, semester), 0) + 1
        occupied = db.seat_occupied_map(conn)
        caps = db.load_seat_caps(conn)
        conflicts = []
        for (code, semester), need in sorted(needs.items()):
            cap = caps.get((code, semester))
            if cap is None:
                continue
            occ = occupied.get((code, semester), 0)
            if occ + need > cap:
                conflicts.append((code, semester, cap, occ, need))
        if conflicts:
            raise HTTPException(status_code=409, detail={
                "code": "SEAT_CONFLICT",
                "message": "确认时整批席位余量不足，整批确认被拒绝、原占位不变："
                           + "；".join(
                               f"{code}@{semester}（批次需 {need}，上限 {cap}，"
                               f"已占用 {occ}，余量 {cap - occ}）"
                               for code, semester, cap, occ, need in conflicts),
                "conflicts": [
                    {"course_code": code, "semester": semester,
                     "cap": cap, "occupied": occ, "requested": need,
                     "remaining": cap - occ}
                    for code, semester, cap, occ, need in conflicts],
            })

        for plan_id, row_plan in plans.items():
            db.insert_holds(conn, plan_id,
                            _placements_from_result(json.loads(row_plan["result_json"])))
            db.set_plan_status(conn, plan_id, "confirmed")
        db.set_plan_batch_status(conn, batch_id, "confirmed")
        out = _batch_out(db.get_plan_batch(conn, batch_id))
    return out


@app.get("/plans/{plan_id}")
def get_plan(plan_id: str):
    """回看方案（含其绑定的目录版本号）。"""
    with db.session() as conn:
        row = db.get_plan(conn, plan_id)
        if row is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND", "message": f"方案 {plan_id} 不存在"})
    return _plan_out(row)


def _placements_from_result(result):
    """方案结果 -> [(实际修读课程代码, 学期标签), ...]。

    互认按实际修读课程占位：方案列出替代课就占替代课名额，不计入原课名额。
    """
    placements = []
    for s in result.get("semesters", []):
        for c in s.get("courses", []):
            placements.append((c["code"], s["semester"]))
    return placements


def _raise_seat_conflict(conflicts):
    raise HTTPException(status_code=409, detail={
        "code": "SEAT_CONFLICT",
        "message": "确认时席位余量已变化，以下课表席位不足，确认被拒绝（原方案与原占位保留）："
                   + "；".join(
                       f"{code}@{semester}（需 {need}，上限 {cap}，已占用 {occ}，余量 {cap - occ}）"
                       for code, semester, cap, occ, need in conflicts),
        "conflicts": [
            {"course_code": code, "semester": semester,
             "cap": cap, "occupied": occ, "requested": need, "remaining": cap - occ}
            for code, semester, cap, occ, need in conflicts
        ],
    })


@app.post("/plans/{plan_id}/confirm")
def confirm_plan(plan_id: str):
    """确认方案或重排草案：单事务内原子复核目录版本与每门课席位并占位。

    - 已确认方案/草案重复确认幂等，直接返回；
    - 目录自方案生成后已修订 -> 409 CATALOG_CHANGED，不写入；
    - 重排草案：同一来源已有生效重排、或草案过期 -> 409 DRAFT_EXPIRED；
      确认时仅将来源方案尚未结束学期的占位替换为新课表，已结束学期占位保留；
    - 余量变化导致席位不足 -> 409 SEAT_CONFLICT，拒绝确认、保留原方案与原占位
      （事务回滚，不出现超卖）。
    """
    with db.transaction() as conn:
        row = db.get_plan(conn, plan_id)
        if row is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND", "message": f"方案 {plan_id} 不存在"})
        if row["status"] == "confirmed":
            return _plan_out(row)  # 幂等：已确认直接返回，不重复占位

        # 批次成员课表不得单独确认：必须经 /plan-batches/{id}/confirm 整批原子占位，
        # 杜绝“一份一份确认”造成的部分占位与整批语义破坏
        if "batch_id" in row.keys() and row["batch_id"]:
            raise HTTPException(status_code=409, detail={
                "code": "BATCH_CONFIRM_REQUIRED",
                "message": f"方案 {plan_id} 属于批次 {row['batch_id']}，不得单独确认；"
                           "请使用 POST /plan-batches/"
                           f"{row['batch_id']}/confirm 整批确认"})

        current = db.get_current_version(conn)
        if row["version_id"] != current["id"]:
            raise HTTPException(status_code=409, detail={
                "code": "CATALOG_CHANGED",
                "message": f"目录已修订（当前版本 {current['id']}，方案基于版本 "
                           f"{row['version_id']}），确认被拒绝，请重新规划",
            })

        result = json.loads(row["result_json"])
        placements = _placements_from_result(result)

        if row["source_plan_id"]:
            # ---- 重排草案：过期判定 + 仅替换来源方案未结束学期占位 ----
            source = db.get_plan(conn, row["source_plan_id"])
            if source is None or source["status"] != "confirmed":
                raise HTTPException(status_code=409, detail={
                    "code": "DRAFT_EXPIRED",
                    "message": "来源方案不存在或未处于已确认状态，重排草案已过期，确认被拒绝"})
            if db.count_active_replan(conn, row["source_plan_id"]) > 0:
                raise HTTPException(status_code=409, detail={
                    "code": "DRAFT_EXPIRED",
                    "message": f"来源方案 {row['source_plan_id']} 已有生效的重排结果，"
                               "同一来源只能有一个生效重排，该草案过期，确认被拒绝"})
            replan_meta = json.loads(row["replan_json"])
            elapsed = int(replan_meta["elapsed_semesters"])
            source_request = json.loads(source["request_json"])
            start_year, start_season = parse_semester(source_request["start_semester"])
            # 先删除来源方案未结束学期占位（事务内，失败随回滚恢复）
            db.delete_holds_after_index(conn, source["id"], start_year, start_season, elapsed)

        # ---- 原子复核每门课席位（cap 缺失即不限额）----
        needs = {}
        for code, semester in placements:
            needs[(code, semester)] = needs.get((code, semester), 0) + 1
        occupied = db.seat_occupied_map(conn)
        caps = db.load_seat_caps(conn)
        conflicts = []
        for (code, semester), need in sorted(needs.items()):
            cap = caps.get((code, semester))
            if cap is None:
                continue
            occ = occupied.get((code, semester), 0)
            if occ + need > cap:
                conflicts.append((code, semester, cap, occ, need))
        if conflicts:
            _raise_seat_conflict(conflicts)

        # ---- 占位并生效 ----
        db.insert_holds(conn, plan_id, placements)
        db.set_plan_status(conn, plan_id, "confirmed")
        out = _plan_out(db.get_plan(conn, plan_id))
    return out


@app.post("/plans/{plan_id}/replan", status_code=201)
def replan_plan_route(plan_id: str, payload: ReplanIn):
    """已确认方案执行中途，凭已结束学期数与累计已修课程按当前目录滚动重排。

    支持两类来源：
    - 普通规划：沿用原目标课程、原每学期学分上限与剩余期限，按当前目录重排未来课表；
    - 培养方案达成课表（``request.plan_type == "program_plan"``）：沿用原要求版本、
      学分上限与期限；未来按当前目录（开课/先修/同修/席位）排课，要求归属及计入学分
      仍按要求绑定目录计算，可重新选择拟修课及选修归属，并返回合并后的预计归属。
      仅凭累计已修即达成时未来课表为空（草案仍保存，确认后撤换来源未来占位）。

    结果作为关联原方案的待确认草案（status=draft）保存；原方案及其历史目录不变。
    目录在计算至落库间被修订时，拒绝旧版本结果（409）且不保存。
    """
    with db.session() as conn:
        row = db.get_plan(conn, plan_id)
        if row is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND", "message": f"方案 {plan_id} 不存在"})
        if row["status"] != "confirmed":
            raise HTTPException(status_code=409, detail={
                "code": "PLAN_NOT_CONFIRMED",
                "message": f"方案 {plan_id} 状态为 {row['status']}，仅已确认方案可滚动重排",
            })

        original_request = json.loads(row["request_json"])
        original_result = json.loads(row["result_json"])
        is_program = original_request.get("plan_type") == "program_plan"

        version = db.get_current_version(conn)
        catalog = _to_catalog(db.load_courses(conn, version["id"]))
        equiv_groups = db.load_equiv_groups(conn, version["id"])
        seat_caps = db.load_seat_caps(conn)
        # 重排余量：来源方案尚未结束学期的占位确认时会被替换，规划时不计入
        start_year, start_season = parse_semester(original_request["start_semester"])
        occupied = db.seat_occupied_map_for_replan(
            conn, plan_id, start_year, start_season, payload.elapsed_semesters)

        attribution = None
        try:
            if is_program:
                rv = db.get_requirement_version(
                    conn, original_request["requirement_version_id"])
                if rv is None:
                    # 防御：要求版本随快照引用仍可回看，缺失只可能是外部破坏
                    raise HTTPException(status_code=404, detail={
                        "code": "NOT_FOUND",
                        "message": "来源课表绑定的要求版本 "
                                   f"{original_request['requirement_version_id']} 不存在，"
                                   "无法按培养方案达成课表重排"})
                bound_rows = db.load_courses(conn, rv["catalog_version_id"])
                bound_equiv = db.load_equiv_groups(conn, rv["catalog_version_id"])
                content = json.loads(rv["content_json"])
                effective_request, result, attribution_result, diff = replan_program_plan(
                    bound_rows=bound_rows,
                    bound_equiv_groups=bound_equiv,
                    current_catalog=catalog,
                    current_equiv_groups=equiv_groups,
                    required=content["required_courses"],
                    elective_groups=content["elective_groups"],
                    original_request=original_request,
                    original_result=original_result,
                    elapsed_semesters=payload.elapsed_semesters,
                    completed=set(payload.completed_courses),
                    seat_caps=seat_caps,
                    occupied=occupied,
                )
                attribution = {
                    "requirement_version_id": rv["id"],
                    "bound_catalog_version": rv["catalog_version_id"],
                    "completed_courses": payload.completed_courses,
                    "result": attribution_result,
                }
            else:
                effective_request, result, diff = replan_plan(
                    catalog,
                    original_request=original_request,
                    original_result=original_result,
                    elapsed_semesters=payload.elapsed_semesters,
                    completed=set(payload.completed_courses),
                    equiv_groups=equiv_groups,
                    seat_caps=seat_caps,
                    occupied=occupied,
                )
        except PlanError as exc:
            status = 400 if exc.code == "INVALID_INPUT" else 422
            raise HTTPException(status_code=status,
                                detail={"code": exc.code, "message": exc.message})

        # 落库前复查：目录在计算期间被修订则拒绝旧版本结果，不保存
        current = db.get_current_version(conn)
        if current["id"] != version["id"]:
            raise HTTPException(status_code=409, detail={
                "code": "CATALOG_CHANGED",
                "message": f"目录在重排期间已修订（计算基于版本 {version['id']}，"
                           f"当前版本 {current['id']}），旧版本结果被拒绝，请重新发起重排",
            })

        draft_id = uuid.uuid4().hex[:12]
        replan_meta = {
            "source_plan_id": plan_id,
            "elapsed_semesters": payload.elapsed_semesters,
            "completed_courses": payload.completed_courses,
            "difference": diff,
        }
        if is_program:
            replan_meta["plan_type"] = "program_plan"
        db.insert_draft(conn, draft_id, version["id"], effective_request, result,
                        source_plan_id=plan_id, replan=replan_meta,
                        attribution=attribution)
        draft = db.get_plan(conn, draft_id)
    return _plan_out(draft)
