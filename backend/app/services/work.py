from datetime import date

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ActivityLog,
    Milestone,
    MilestoneDependency,
    Notification,
    Project,
    ProjectMember,
    Risk,
    Task,
    TaskDependency,
    User,
    now,
)
from app.schemas import MilestoneInput, ProjectInput, RiskInput, TaskInput


def record(db: Session, user: User, project_id: int, event: str, detail: dict):
    db.add(ActivityLog(project_id=project_id, actor_id=user.id, event=event, detail=detail))


def notify(db: Session, user_id: int, project_id: int, message: str, key: str):
    if not db.scalar(
        select(Notification).where(Notification.user_id == user_id, Notification.dedupe_key == key)
    ):
        db.add(
            Notification(
                user_id=user_id, project_id=project_id, message=message[:300], dedupe_key=key
            )
        )
        db.flush()


def member(db: Session, project_id: int, user_id: int):
    user = db.get(User, user_id)
    if not user or not user.active or not db.get(ProjectMember, (project_id, user_id)):
        raise HTTPException(422, "Owner or assignee must be an active project member")


def as_dict(obj) -> dict:
    result = {
        c.name: getattr(obj, c.name)
        for c in obj.__table__.columns
        if c.name not in ("password_hash", "token_version")
    }
    if isinstance(obj, Risk):
        score = obj.probability * obj.impact
        result.update(
            score=score,
            severity="Low"
            if score <= 4
            else "Medium"
            if score <= 9
            else "High"
            if score <= 16
            else "Critical",
            overdue=obj.target_date < date.today() and obj.status not in ("Resolved", "Accepted"),
        )
    if isinstance(obj, Task):
        result["overdue"] = obj.due_date < date.today() and obj.status not in (
            "Completed",
            "Cancelled",
        )
    return result


def progress(tasks: list[Task]) -> float:
    eligible = [t for t in tasks if t.status != "Cancelled"]
    total = sum(t.estimated_effort for t in eligible)
    return (
        round(sum(t.progress * t.estimated_effort for t in eligible) / total, 1) if total else 0.0
    )


def serialize(db: Session, obj) -> dict:
    data = as_dict(obj)
    if isinstance(obj, (Project, Milestone)):
        condition = (
            Task.project_id == obj.id if isinstance(obj, Project) else Task.milestone_id == obj.id
        )
        data["progress"] = progress(list(db.scalars(select(Task).where(condition))))
    if isinstance(obj, Project):
        data["member_ids"] = list(
            db.scalars(select(ProjectMember.user_id).where(ProjectMember.project_id == obj.id))
        )
    if isinstance(obj, (Task, Milestone)):
        dep, column = (
            (TaskDependency, TaskDependency.task_id)
            if isinstance(obj, Task)
            else (MilestoneDependency, MilestoneDependency.milestone_id)
        )
        data["dependencies"] = list(db.scalars(select(dep.prerequisite_id).where(column == obj.id)))
        data["downstream"] = list(db.scalars(select(column).where(dep.prerequisite_id == obj.id)))
        data["blocked_by"] = [
            i for i in data["dependencies"] if db.get(type(obj), i).status != "Completed"
        ]
    return data


def validate_dates(start: date, end: date):
    if end < start:
        raise HTTPException(422, "End date must be on or after start date")


def validate_work(db: Session, obj, values: dict, user: User):
    schema = {Project: ProjectInput, Task: TaskInput, Milestone: MilestoneInput, Risk: RiskInput}[
        type(obj)
    ]
    merged = {
        k: getattr(obj, k)
        for k in schema.model_fields
        if hasattr(obj, k) and getattr(obj, k) is not None
    }
    merged.update(values)
    # Revalidate the complete merged record, including patch field constraints.
    validated = schema.model_validate(merged).model_dump()
    reason = validated.pop("override_reason", None)
    members = validated.pop("member_ids", [])
    if isinstance(obj, Project):
        validate_dates(validated["start_date"], validated["end_date"])
        manager = db.get(User, validated["manager_id"])
        if not manager or not manager.active or manager.role not in ("admin", "manager"):
            raise HTTPException(422, "Project manager must have manager or admin role")
        if user.role != "admin" and validated["manager_id"] != user.id:
            raise HTTPException(403, "Only admins can appoint another project manager")
        if obj.id:
            child_dates = list(db.scalars(select(Task).where(Task.project_id == obj.id))) + list(
                db.scalars(select(Milestone).where(Milestone.project_id == obj.id))
            )
            if any(
                x.start_date < validated["start_date"] or x.due_date > validated["end_date"]
                for x in child_dates
            ):
                raise HTTPException(422, "Project dates must contain existing work")
        if validated["status"] == "Completed":
            remaining = (
                db.scalar(
                    select(Task.id).where(
                        Task.project_id == obj.id, Task.status.not_in(["Completed", "Cancelled"])
                    )
                )
                or db.scalar(
                    select(Milestone.id).where(
                        Milestone.project_id == obj.id, Milestone.status != "Completed"
                    )
                )
                if obj.id
                else None
            )
            if remaining and not reason:
                raise HTTPException(
                    409, "Incomplete work remains; record a manager override reason"
                )
            obj.actual_end_date = obj.actual_end_date or date.today()
        else:
            obj.actual_end_date = None
        if "member_ids" in values or not obj.id:
            ids = set(members + [validated["owner_id"], validated["manager_id"]])
            if len(
                list(db.scalars(select(User.id).where(User.id.in_(ids), User.active.is_(True))))
            ) != len(ids):
                raise HTTPException(422, "All members must be active users")
            if obj.id:
                assigned = (
                    set(db.scalars(select(Task.assignee_id).where(Task.project_id == obj.id)))
                    | set(
                        db.scalars(select(Milestone.owner_id).where(Milestone.project_id == obj.id))
                    )
                    | set(db.scalars(select(Risk.owner_id).where(Risk.project_id == obj.id)))
                )
                if not assigned.issubset(ids):
                    raise HTTPException(409, "Reassign owned work before removing a member")
                for row in db.scalars(
                    select(ProjectMember).where(ProjectMember.project_id == obj.id)
                ):
                    db.delete(row)
                db.flush()
            obj._member_ids = ids
    else:
        project = db.get(Project, obj.project_id)
        if project.status in ("Completed", "Cancelled") or project.archived:
            raise HTTPException(409, "Reopen the project before changing work")
        member(db, obj.project_id, validated.get("assignee_id", validated.get("owner_id")))
        if isinstance(obj, (Task, Milestone)):
            validate_dates(validated["start_date"], validated["due_date"])
            if (
                validated["start_date"] < project.start_date
                or validated["due_date"] > project.end_date
            ):
                raise HTTPException(422, "Work dates must be within project dates")
            if isinstance(obj, Task) and validated["milestone_id"]:
                milestone = db.get(Milestone, validated["milestone_id"])
                if not milestone or milestone.project_id != obj.project_id:
                    raise HTTPException(422, "Milestone must belong to this project")
                if milestone.status == "Completed" and validated["status"] not in (
                    "Completed",
                    "Cancelled",
                ):
                    raise HTTPException(409, "Reopen milestone before changing incomplete work")
            if (
                obj.id
                and validated["status"] in ("In Progress", "In Review", "Completed")
                and serialize(db, obj)["blocked_by"]
            ):
                raise HTTPException(409, "Finish-to-start prerequisites are incomplete")
            if obj.id and obj.status == "Completed" and validated["status"] != "Completed":
                dep, column = (
                    (TaskDependency, TaskDependency.task_id)
                    if isinstance(obj, Task)
                    else (MilestoneDependency, MilestoneDependency.milestone_id)
                )
                descendants = list(db.scalars(select(column).where(dep.prerequisite_id == obj.id)))
                if any(
                    db.get(type(obj), i).status in ("In Progress", "In Review", "Completed")
                    for i in descendants
                ):
                    raise HTTPException(409, "Reopen downstream work first")
            if validated["status"] == "Completed":
                if isinstance(obj, Milestone):
                    tasks = (
                        list(db.scalars(select(Task).where(Task.milestone_id == obj.id)))
                        if obj.id
                        else []
                    )
                    if (
                        not tasks or any(t.status not in ("Completed", "Cancelled") for t in tasks)
                    ) and not reason:
                        raise HTTPException(
                            409, "Complete milestone tasks or record a manager override reason"
                        )
                obj.completed_at = obj.completed_at or now()
            else:
                obj.completed_at = None
            if isinstance(obj, Task):
                if validated["status"] == "Completed":
                    validated["progress"] = 100
                elif validated["status"] in ("Backlog", "To Do", "Cancelled"):
                    validated["progress"] = 0
                elif validated["progress"] == 100:
                    validated["progress"] = 99
        else:
            validate_dates(validated["identified_date"], validated["target_date"])
    for key, value in validated.items():
        setattr(obj, key, value)
    db.add(obj)
    db.flush()
    if isinstance(obj, Project) and hasattr(obj, "_member_ids"):
        for uid in obj._member_ids:
            db.add(ProjectMember(project_id=obj.id, user_id=uid))
        del obj._member_ids
    if reason:
        record(
            db,
            user,
            obj.id if isinstance(obj, Project) else obj.project_id,
            "completion.override",
            {"entity": type(obj).__name__, "id": obj.id, "reason": reason},
        )
    db.flush()


def add_dependency(db: Session, obj, prerequisite_id: int):
    model = type(obj)
    prerequisite = db.get(model, prerequisite_id)
    if not prerequisite or prerequisite.project_id != obj.project_id or obj.id == prerequisite_id:
        raise HTTPException(422, "Dependency must reference different work in the same project")
    dep, column, key = (
        (TaskDependency, TaskDependency.task_id, "task_id")
        if model is Task
        else (MilestoneDependency, MilestoneDependency.milestone_id, "milestone_id")
    )
    edges = list(db.execute(select(column, dep.prerequisite_id)))
    graph: dict[int, list[int]] = {}
    for child, parent in edges:
        graph.setdefault(child, []).append(parent)
    pending, seen = [prerequisite_id], set()
    while pending:
        node = pending.pop()
        if node == obj.id:
            raise HTTPException(409, "Circular dependency rejected")
        if node not in seen:
            seen.add(node)
            pending.extend(graph.get(node, []))
    if (
        obj.status in ("In Progress", "In Review", "Completed")
        and prerequisite.status != "Completed"
    ):
        raise HTTPException(409, "Started work cannot gain an incomplete prerequisite")
    if db.get(dep, (obj.id, prerequisite_id)):
        raise HTTPException(409, "Dependency already exists")
    db.add(dep(**{key: obj.id, "prerequisite_id": prerequisite_id}))
    db.flush()
