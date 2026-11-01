from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.permissions import project_access, require_manager, visible_projects
from app.core.security import current_user
from app.db.session import get_db
from app.models import ActivityLog, Project, User
from app.schemas import ProjectInput, ProjectPatch
from app.services.work import record, serialize, validate_work

router = APIRouter()


def page_result(db, query, page, size):
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    return {
        "items": [
            serialize(db, obj) for obj in db.scalars(query.offset((page - 1) * size).limit(size))
        ],
        "total": total,
        "page": page,
        "size": size,
    }


@router.get("/projects")
def projects(
    q: str = "",
    status: str | None = None,
    priority: str | None = None,
    owner_id: int | None = None,
    start: date | None = None,
    end: date | None = None,
    sort: str = "updated_at",
    page: int = Query(1, ge=1),
    size: int = Query(25, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    query = visible_projects(user).where(Project.archived.is_(False))
    if q:
        query = query.where(Project.name.ilike(f"%{q}%"))
    for key, value in [("status", status), ("priority", priority), ("owner_id", owner_id)]:
        if value is not None:
            query = query.where(getattr(Project, key) == value)
    if start:
        query = query.where(Project.end_date >= start)
    if end:
        query = query.where(Project.start_date <= end)
    order = {
        "name": Project.name,
        "end_date": Project.end_date,
        "updated_at": Project.updated_at.desc(),
    }.get(sort)
    if order is None:
        raise HTTPException(422, "Unsupported sort")
    return page_result(db, query.order_by(order, Project.id), page, size)


@router.post("/projects", status_code=201)
def create_project(
    body: ProjectInput, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    require_manager(user)
    obj = Project()
    validate_work(db, obj, body.model_dump(), user)
    record(db, user, obj.id, "project.created", {"name": obj.name})
    return serialize(db, obj)


@router.get("/projects/{pid}")
def get_project(pid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return serialize(db, project_access(db, user, pid))


@router.patch("/projects/{pid}")
def update_project(
    pid: int, body: ProjectPatch, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    obj = project_access(db, user, pid, manage=True, lock=True)
    old_status = obj.status
    values = body.model_dump(exclude_unset=True)
    if any(k in values for k in ("owner_id", "manager_id")) and "member_ids" not in values:
        values["member_ids"] = serialize(db, obj)["member_ids"]
    validate_work(db, obj, values, user)
    record(
        db,
        user,
        pid,
        "project.updated",
        {"fields": list(values), "old_status": old_status, "status": obj.status},
    )
    return serialize(db, obj)


@router.delete("/projects/{pid}", status_code=204)
def archive_project(pid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    obj = project_access(db, user, pid, manage=True, lock=True)
    obj.archived = True
    record(db, user, pid, "project.archived", {})


@router.get("/projects/{pid}/activity")
def activity(
    pid: int,
    page: int = Query(1, ge=1),
    size: int = Query(25, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    project_access(db, user, pid)
    return page_result(
        db,
        select(ActivityLog).where(ActivityLog.project_id == pid).order_by(ActivityLog.id.desc()),
        page,
        size,
    )
