"""培养方案要求版本与达成审核：HTTP API 集成测试（独立临时数据库）。"""
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    from app.main import app
    with TestClient(app) as c:
        yield c


def _equiv_catalog(client, groups=(["CS301", "CS302"], ["CS401", "CS402"])):
    current = client.get("/catalog/current").json()
    r = client.post("/catalog/versions", json={
        "note": "互认目录", "courses": current["courses"], "equivalence_groups": list(groups)})
    assert r.status_code == 201, r.text
    return r.json()["version_id"]


def _rv_body(vid, **kw):
    body = {
        "program_code": "CS-2024",
        "catalog_version_id": vid,
        "note": "2024 级",
        "required_courses": ["CS101", "CS201", "CS301", "CS401"],
        "elective_groups": [
            {"name": "专业选修", "candidates": ["CS303", "CS402"], "min_credits": 3},
            {"name": "基础拓展", "candidates": ["MATH101", "MATH103"], "min_credits": 6},
        ],
    }
    body.update(kw)
    return body


def _register(client, program="CS-2024", vid=None, **kw):
    vid = vid or client.get("/catalog/current").json()["version_id"]
    r = client.post(f"/programs/{program}/requirement-versions",
                    json=_rv_body(vid, **kw))
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- 登记校验

def test_register_requirement_version(client):
    body = _rv_body(1)
    r = client.post("/programs/CS-2024/requirement-versions", json=body)
    assert r.status_code == 201
    out = r.json()
    assert out["requirement_version_id"] == "CS-2024:v1"
    assert out["version"] == 1
    assert out["catalog_version_id"] == 1
    assert out["required_courses"] == body["required_courses"]
    assert [g["name"] for g in out["elective_groups"]] == ["专业选修", "基础拓展"]
    # 同编号再次登记 -> v2
    out2 = client.post("/programs/CS-2024/requirement-versions", json=body).json()
    assert out2["requirement_version_id"] == "CS-2024:v2" and out2["version"] == 2
    # 不同编号独立计数
    other = client.post("/programs/AI-2024/requirement-versions", json=body).json()
    assert other["requirement_version_id"] == "AI-2024:v1"


def test_register_unknown_catalog_404_and_no_record(client):
    r = client.post("/programs/X/requirement-versions", json=_rv_body(999))
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NOT_FOUND"
    assert client.get("/programs/X/requirement-versions").json()["requirement_versions"] == []


def test_register_unknown_course_rejected(client):
    r = client.post("/programs/X/requirement-versions",
                    json=_rv_body(1, required_courses=["NOPE101"]))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "COURSE_MISSING"
    r = client.post("/programs/Y/requirement-versions", json=_rv_body(
        1, elective_groups=[{"name": "g", "candidates": ["NOPE202"], "min_credits": 3}]))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "COURSE_MISSING"


def test_register_duplicate_required_rejected(client):
    r = client.post("/programs/X/requirement-versions",
                    json=_rv_body(1, required_courses=["CS101", "cs101"]))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_INPUT"
    assert client.get("/programs/requirement-versions").json()["requirement_versions"] == []


def test_register_non_positive_threshold_rejected(client):
    for thr in (0, -3):
        r = client.post("/programs/X/requirement-versions", json=_rv_body(
            1, elective_groups=[{"name": "g", "candidates": ["CS303"], "min_credits": thr}]))
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "INVALID_INPUT"


def test_register_empty_candidates_rejected(client):
    r = client.post("/programs/X/requirement-versions", json=_rv_body(
        1, elective_groups=[{"name": "g", "candidates": [], "min_credits": 3}]))
    assert r.status_code == 422


def test_requirement_version_review_and_lists(client):
    rv = _register(client)
    assert client.get("/programs/requirement-versions/CS-2024:v1").json() == rv
    listing = client.get("/programs/CS-2024/requirement-versions").json()
    assert [x["requirement_version_id"] for x in listing["requirement_versions"]] == ["CS-2024:v1"]
    assert client.get("/programs/requirement-versions/nope").status_code == 404
    assert "CS-2024:v1" in [x["requirement_version_id"]
                           for x in client.get("/programs/requirement-versions")
                           .json()["requirement_versions"]]


# ---------------------------------------------------------------- 审核计算

def test_audit_full_attribution_with_equivalence(client):
    vid = _equiv_catalog(client)
    rv = _register(client, vid=vid)
    r = client.post(f"/requirement-versions/{rv['requirement_version_id']}/audits", json={
        "completed_courses": ["CS101", "CS201", "CS302", "CS402", "CS303",
                              "MATH101", "MATH103"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["catalog_version_id"] == vid
    result = body["result"]
    assert result["achieved"] is True
    # 必修：CS301<-CS302、CS401<-CS402（互认计入，一门课只归属一项）
    by_code = {x["code"]: x for x in result["required"]}
    assert by_code["CS301"]["attributed_course"] == {
        "code": "CS302", "credits": 3, "via": "equivalence"}
    assert by_code["CS401"]["attributed_course"]["code"] == "CS402"
    assert result["unsatisfied_required"] == []
    # 专业选修：CS402 已用于必修，剩 CS303 直接计入；基础拓展：MATH101(4)+MATH103(3)=7
    groups = {g["name"]: g for g in result["elective_groups"]}
    assert [c["code"] for c in groups["专业选修"]["attributed_courses"]] == ["CS303"]
    assert groups["专业选修"]["credit_gap"] == 0
    assert groups["基础拓展"]["counted_credits"] == 6
    assert groups["基础拓展"]["credit_gap"] == 0
    assert result["unattributed_courses"] == []


def test_audit_unsatisfied_required_and_gap(client):
    rv = _register(client)  # 绑定 v1（无互认组）
    r = client.post(f"/requirement-versions/{rv['requirement_version_id']}/audits", json={
        "completed_courses": ["CS101", "CS201", "CS303"]})
    result = r.json()["result"]
    assert result["achieved"] is False
    assert result["unsatisfied_required"] == ["CS301", "CS401"]
    groups = {g["name"]: g for g in result["elective_groups"]}
    assert groups["专业选修"]["counted_credits"] == 3
    assert groups["专业选修"]["credit_gap"] == 0
    assert groups["基础拓展"]["counted_credits"] == 0
    assert groups["基础拓展"]["credit_gap"] == 6


def test_audit_unknown_completed_rejected_and_not_saved(client):
    rv = _register(client)
    before = client.get("/audits").json()["audits"]
    r = client.post(f"/requirement-versions/{rv['requirement_version_id']}/audits",
                    json={"completed_courses": ["CS101", "NOPE999"]})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "COURSE_MISSING"
    assert client.get("/audits").json()["audits"] == before


def test_audit_missing_requirement_version_404(client):
    r = client.post("/requirement-versions/NOPE:v1/audits", json={"completed_courses": []})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NOT_FOUND"


def test_audit_immutable_after_later_revisions(client):
    """历史审核是快照：后续目录修订与新版要求均不改变既有审核。"""
    vid = _equiv_catalog(client)
    rv = _register(client, vid=vid)
    created = client.post(f"/requirement-versions/{rv['requirement_version_id']}/audits",
                          json={"completed_courses": ["CS101", "CS201", "CS302"]}).json()
    # 再修订目录（新版本，改学分）
    current = client.get("/catalog/current").json()
    for course in current["courses"]:
        if course["code"] == "CS303":
            course["credits"] += 1
    client.post("/catalog/versions", json={
        "note": "学分调整", "courses": current["courses"],
        "equivalence_groups": current["equivalence_groups"]})
    # 登记同编号新版要求
    _register(client, vid=client.get("/catalog/current").json()["version_id"])
    reviewed = client.get(f"/audits/{created['audit_id']}").json()
    assert reviewed["catalog_version_id"] == vid
    assert reviewed["result"] == created["result"]
    assert reviewed["requirement_version_id"] == rv["requirement_version_id"]


def test_audit_list_and_filter(client):
    rv1 = _register(client, program="CS-2024")
    rv2 = _register(client, program="AI-2024")
    a1 = client.post(f"/requirement-versions/{rv1['requirement_version_id']}/audits",
                     json={"completed_courses": []}).json()
    a2 = client.post(f"/requirement-versions/{rv2['requirement_version_id']}/audits",
                     json={"completed_courses": []}).json()
    ids_all = {a["audit_id"] for a in client.get("/audits").json()["audits"]}
    assert {a1["audit_id"], a2["audit_id"]} <= ids_all
    ids_f = {a["audit_id"] for a in client.get(
        "/audits", params={"requirement_version_id": rv1["requirement_version_id"]})
        .json()["audits"]}
    assert ids_f == {a1["audit_id"]}
    assert client.get("/audits/nope").status_code == 404


def test_audit_does_not_touch_existing_plan_behaviors(client):
    """既有规划/确认/重排接口行为不变（冒烟回归）。"""
    payload = {
        "completed_courses": [], "target_courses": ["CS401"],
        "start_semester": "2026-SPRING", "max_credits_per_semester": 12,
        "num_semesters": 6,
    }
    plan = client.post("/plans", json=payload).json()
    assert plan["catalog_version"] == 1
    assert client.post(f"/plans/{plan['plan_id']}/confirm").status_code == 200
    r = client.post(f"/plans/{plan['plan_id']}/replan", json={
        "elapsed_semesters": 2,
        "completed_courses": ["CS101", "CS101L", "CS201", "MATH103", "CS301"]})
    assert r.status_code == 201
    assert r.json()["status"] == "draft"
