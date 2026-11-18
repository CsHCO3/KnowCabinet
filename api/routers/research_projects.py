"""论文调研项目 API（可选入口，CLI 仍为主）。"""

from __future__ import annotations

from fastapi import APIRouter, Form

from api.schemas.common import APIResponse
from backend.services.research_project import project_store as ps
from backend.services.research_project import matrix as matrix_svc
from backend.services.research_project import stats as stats_svc
from backend.services.research_project import mirror as mirror_svc
from backend.services.research_project import classify as classify_svc
from backend.services.research_project import references as refs_svc

router = APIRouter(prefix="/api/research-projects", tags=["research-projects"])


@router.get("")
def list_projects():
    return {"projects": ps.list_projects()}


@router.get("/{slug}")
def project_status(slug: str):
    try:
        return APIResponse(success=True, data=ps.project_status(slug))
    except FileNotFoundError as e:
        return APIResponse(success=False, error=str(e))


@router.post("")
def create_project(
    name: str = Form(...),
    question: str = Form(""),
    keywords: str = Form(""),
    venues: str = Form(""),
    criteria: str = Form(""),
):
    keywords_list = [x.strip() for x in keywords.split(",") if x.strip()] if keywords else []
    venues_list = [x.strip() for x in venues.split(",") if x.strip()] if venues else []
    include = []
    exclude = []
    for part in criteria.split(";"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        k, _, v = part.partition(":")
        if k.strip() == "include":
            include = [x.strip() for x in v.split(",") if x.strip()]
        elif k.strip() == "exclude":
            exclude = [x.strip() for x in v.split(",") if x.strip()]
    r = ps.init_project(name=name, question=question, keywords=keywords_list,
                        criteria={"include": include, "exclude": exclude},
                        target_venues=venues_list)
    if r.get("ok"):
        mirror_svc.refresh_all(r["id"])
    return APIResponse(success=True, data=r)


@router.post("/{slug}/matrix/basic")
def generate_basic_matrix(slug: str, keywords: str = Form("")):
    try:
        path = matrix_svc.write_basic_matrix(slug, keywords=[x.strip() for x in keywords.split(",") if x.strip()])
        proj = ps.load_project(slug)
        ps.set_stage(proj, "basic_matrix", "completed")
        ps.save_project(proj)
        mirror_svc.refresh_all(slug)
        return APIResponse(success=True, data={"path": str(path)})
    except FileNotFoundError as e:
        return APIResponse(success=False, error=str(e))


@router.post("/{slug}/matrix/semantic")
def generate_semantic_matrix(slug: str, model: str = Form("deepseek-chat")):
    try:
        path = matrix_svc.write_semantic_matrix(slug, model_key=model)
        proj = ps.load_project(slug)
        ps.set_stage(proj, "semantic_matrix", "completed")
        ps.save_project(proj)
        mirror_svc.refresh_all(slug)
        return APIResponse(success=True, data={"path": str(path)})
    except FileNotFoundError as e:
        return APIResponse(success=False, error=str(e))


@router.post("/{slug}/stats")
def generate_stats(slug: str):
    try:
        path = stats_svc.write_stats(slug)
        proj = ps.load_project(slug)
        ps.set_stage(proj, "stats", "completed")
        ps.save_project(proj)
        mirror_svc.refresh_all(slug)
        return APIResponse(success=True, data={"path": str(path)})
    except FileNotFoundError as e:
        return APIResponse(success=False, error=str(e))


@router.post("/{slug}/classify/candidate")
def classify_candidates(slug: str, model: str = Form("deepseek-chat")):
    try:
        r = classify_svc.classify_candidates(slug, model_key=model)
        proj = ps.load_project(slug)
        ps.set_stage(proj, "classify", "completed")
        ps.save_project(proj)
        mirror_svc.refresh_all(slug)
        return APIResponse(success=True, data=r)
    except FileNotFoundError as e:
        return APIResponse(success=False, error=str(e))


@router.post("/{slug}/refs")
def mine_references(slug: str):
    try:
        r = refs_svc.mine_references(slug)
        proj = ps.load_project(slug)
        ps.set_stage(proj, "refs", "completed")
        ps.save_project(proj)
        mirror_svc.refresh_all(slug)
        return APIResponse(success=True, data=r)
    except FileNotFoundError as e:
        return APIResponse(success=False, error=str(e))
