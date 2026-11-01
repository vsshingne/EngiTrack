from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.projects import page_result
from app.core.permissions import project_access, visible_projects
from app.core.security import current_user
from app.db.session import get_db
from app.models import (
    Comment,
    Milestone,
    MilestoneDependency,
    Project,
    Risk,
    Task,
    TaskDependency,
    User,
)
from app.schemas import (
    CommentInput,
    DependencyInput,
    MilestoneInput,
    MilestonePatch,
    RiskInput,
    RiskPatch,
    TaskInput,
    TaskPatch,
)
from app.services.work import add_dependency, as_dict, notify, record, serialize, validate_work

router = APIRouter()


def find(db, user, model, oid, manage=False):
    obj = db.get(model, oid)
    if not obj:
        raise HTTPException(404, "Record not found")
    project_access(db, user, obj.project_id, manage=manage, lock=manage)
    return obj


def save(db, user, obj, values, creating=False):
    old = as_dict(obj) if not creating else {}
    validate_work(db, obj, values, user)
    event = f"{type(obj).__name__.lower()}.{'created' if creating else 'updated'}"
    record(
        db,
        user,
        obj.project_id,
        event,
        {
            "id": obj.id,
            "fields": list(values),
            "old_status": old.get("status"),
            "status": obj.status,
        },
    )
    if isinstance(obj, Task) and (creating or old.get("assignee_id") != obj.assignee_id):
        notify(
            db,
            obj.assignee_id,
            obj.project_id,
            f"Assigned: {obj.title}",
            f"assignment:{obj.id}:{obj.assignee_id}:{obj.updated_at.isoformat()}",
        )
    if (
        isinstance(obj, Risk)
        and obj.probability * obj.impact >= 17
        and obj.status not in ("Resolved", "Accepted")
    ):
        notify(
            db,
            obj.owner_id,
            obj.project_id,
            f"Critical risk: {obj.title}",
            f"critical:{obj.id}:{obj.probability * obj.impact}",
        )
    return serialize(db, obj)


@router.get("/tasks")
def tasks(
    project_id: int | None = None,
    milestone_id: int | None = None,
    assignee_id: int | None = None,
    status: str | None = None,
    priority: str | None = None,
    q: str = "",
    overdue: bool = False,
    due_soon: bool = False,
    mine: bool = False,
    page: int = Query(1, ge=1),
    size: int = Query(25, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    ids = visible_projects(user).where(Project.archived.is_(False)).with_only_columns(Project.id)
    query = select(Task).where(Task.project_id.in_(ids))
    for key, value in [
        ("project_id", project_id),
        ("milestone_id", milestone_id),
        ("assignee_id", user.id if mine else assignee_id),
        ("status", status),
        ("priority", priority),
    ]:
        if value is not None:
            query = query.where(getattr(Task, key) == value)
    if q:
        query = query.where(Task.title.ilike(f"%{q}%"))
    if overdue or due_soon:
        query = query.where(Task.status.not_in(["Completed", "Cancelled"]))
    if overdue:
        query = query.where(Task.due_date < date.today())
    if due_soon:
        query = query.where(Task.due_date.between(date.today(), date.today() + timedelta(days=7)))
    return page_result(db, query.order_by(Task.due_date, Task.id), page, size)


@router.post("/tasks", status_code=201)
def create_task(body: TaskInput, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project_access(db, user, body.project_id, manage=True, lock=True)
    return save(
        db, user, Task(project_id=body.project_id, reporter_id=user.id), body.model_dump(), True
    )


@router.get("/tasks/{oid}")
def get_task(oid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return serialize(db, find(db, user, Task, oid))


@router.patch("/tasks/{oid}")
@router.patch("/tasks/{oid}/status")
def update_task(
    oid: int, body: TaskPatch, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    obj = find(db, user, Task, oid)
    project = project_access(db, user, obj.project_id, lock=True)
    values = body.model_dump(exclude_unset=True)
    if "project_id" in values and values["project_id"] != obj.project_id:
        raise HTTPException(422, "Tasks cannot move between projects")
    manager = user.role == "admin" or (user.role == "manager" and project.manager_id == user.id)
    if not manager and (
        obj.assignee_id != user.id or set(values) - {"status", "progress", "actual_effort"}
    ):
        raise HTTPException(403, "Members may only update progress on their own tasks")
    return save(db, user, obj, values)


@router.delete("/tasks/{oid}", status_code=204)
def delete_task(oid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    obj = find(db, user, Task, oid, True)
    if serialize(db, obj)["downstream"]:
        raise HTTPException(409, "Remove downstream dependencies before deleting")
    record(db, user, obj.project_id, "task.deleted", {"id": oid, "title": obj.title})
    db.delete(obj)


@router.get("/tasks/{oid}/comments")
def comments(
    oid: int,
    page: int = Query(1, ge=1),
    size: int = Query(25, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    find(db, user, Task, oid)
    return page_result(
        db, select(Comment).where(Comment.task_id == oid).order_by(Comment.id), page, size
    )


@router.post("/tasks/{oid}/comments", status_code=201)
def add_comment(
    oid: int, body: CommentInput, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    obj = find(db, user, Task, oid)
    row = Comment(task_id=oid, author_id=user.id, body=body.body)
    db.add(row)
    db.flush()
    record(db, user, obj.project_id, "task.comment", {"task_id": oid})
    return as_dict(row)


def register_resource(plural, model, create_schema, patch_schema):
    def listing(
        project_id: int | None = None,
        status: str | None = None,
        owner_id: int | None = None,
        category: str | None = None,
        severity: str | None = None,
        q: str = "",
        page: int = Query(1, ge=1),
        size: int = Query(25, ge=1, le=500),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
    ):
        ids = (
            visible_projects(user).where(Project.archived.is_(False)).with_only_columns(Project.id)
        )
        query = select(model).where(model.project_id.in_(ids))
        if project_id:
            project_access(db, user, project_id)
            query = query.where(model.project_id == project_id)
        if status:
            query = query.where(model.status == status)
        if owner_id:
            query = query.where(model.owner_id == owner_id)
        if q:
            query = query.where((model.name if model is Milestone else model.title).ilike(f"%{q}%"))
        if model is Risk:
            if category:
                query = query.where(Risk.category == category)
            if severity:
                bounds = {"Low": (1, 4), "Medium": (5, 9), "High": (10, 16), "Critical": (17, 25)}
                if severity not in bounds:
                    raise HTTPException(422, "Unknown severity")
                query = query.where((Risk.probability * Risk.impact).between(*bounds[severity]))
        return page_result(db, query.order_by(model.id), page, size)

    def create(
        project_id: int, body, db: Session = Depends(get_db), user: User = Depends(current_user)
    ):
        project_access(db, user, project_id, manage=model is Milestone, lock=True)
        if model is Risk and user.role == "member" and body.owner_id != user.id:
            raise HTTPException(403, "Members may report risks owned by themselves")
        return save(db, user, model(project_id=project_id), body.model_dump(), True)

    def detail(oid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
        return serialize(db, find(db, user, model, oid))

    def update(oid: int, body, db: Session = Depends(get_db), user: User = Depends(current_user)):
        obj = find(db, user, model, oid, True)
        return save(db, user, obj, body.model_dump(exclude_unset=True))

    def delete(oid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
        obj = find(db, user, model, oid, True)
        if model is Milestone and (
            serialize(db, obj)["downstream"]
            or db.scalar(select(Task.id).where(Task.milestone_id == oid))
        ):
            raise HTTPException(409, "Remove milestone tasks and downstream dependencies first")
        record(db, user, obj.project_id, f"{plural}.deleted", {"id": oid})
        db.delete(obj)

    create.__annotations__["body"] = create_schema
    update.__annotations__["body"] = patch_schema
    for path, fn, methods, code in [
        (f"/{plural}", listing, ["GET"], 200),
        (f"/projects/{{project_id}}/{plural}", listing, ["GET"], 200),
        (f"/projects/{{project_id}}/{plural}", create, ["POST"], 201),
        (f"/{plural}/{{oid}}", detail, ["GET"], 200),
        (f"/{plural}/{{oid}}", update, ["PATCH"], 200),
        (f"/{plural}/{{oid}}", delete, ["DELETE"], 204),
    ]:
        router.add_api_route(
            path,
            fn,
            methods=methods,
            status_code=code,
            name=f"{plural}_{fn.__name__}_{len(router.routes)}",
        )


register_resource("milestones", Milestone, MilestoneInput, MilestonePatch)
register_resource("risks", Risk, RiskInput, RiskPatch)


def register_dependencies(plural, model, dep):
    def create(
        oid: int,
        body: DependencyInput,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
    ):
        obj = find(db, user, model, oid, True)
        add_dependency(db, obj, body.prerequisite_id)
        record(
            db,
            user,
            obj.project_id,
            "dependency.added",
            {"entity": plural, "id": oid, "prerequisite_id": body.prerequisite_id},
        )
        return serialize(db, obj)

    def delete(
        oid: int,
        prerequisite_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
    ):
        obj = find(db, user, model, oid, True)
        row = db.get(dep, (oid, prerequisite_id))
        if not row:
            raise HTTPException(404, "Dependency not found")
        db.delete(row)
        record(
            db,
            user,
            obj.project_id,
            "dependency.removed",
            {"entity": plural, "id": oid, "prerequisite_id": prerequisite_id},
        )

    router.add_api_route(
        f"/{plural}/{{oid}}/dependencies",
        create,
        methods=["POST"],
        status_code=201,
        name=f"{plural}_dependency_add",
    )
    router.add_api_route(
        f"/{plural}/{{oid}}/dependencies/{{prerequisite_id}}",
        delete,
        methods=["DELETE"],
        status_code=204,
        name=f"{plural}_dependency_delete",
    )


register_dependencies("tasks", Task, TaskDependency)
register_dependencies("milestones", Milestone, MilestoneDependency)
