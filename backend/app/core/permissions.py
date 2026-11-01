from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Project, ProjectMember, User


def visible_projects(user: User):
    query = select(Project)
    if user.role != "admin":
        query = query.where(
            or_(
                Project.manager_id == user.id,
                Project.id.in_(
                    select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
                ),
            )
        )
    return query


def project_access(
    db: Session, user: User, project_id: int, manage: bool = False, lock: bool = False
) -> Project:
    query = visible_projects(user).where(Project.id == project_id)
    if lock:
        query = query.with_for_update()
    project = db.scalar(query)
    if project is None:
        raise HTTPException(404, "Project not found")
    if manage and not (
        user.role == "admin" or (user.role == "manager" and project.manager_id == user.id)
    ):
        raise HTTPException(403, "Project manager permission required")
    if manage and project.archived:
        raise HTTPException(409, "Archived projects are read-only")
    return project


def require_manager(user: User):
    if user.role not in ("admin", "manager"):
        raise HTTPException(403, "Manager permission required")
