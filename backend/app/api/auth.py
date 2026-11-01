from collections import defaultdict, deque
from threading import Lock
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import current_user, passwords, token_for
from app.db.session import get_db
from app.models import User
from app.schemas import Login, Register, UserPatch
from app.services.work import as_dict

router = APIRouter()
attempts: dict[str, deque] = defaultdict(deque)
lock = Lock()
dummy_hash = passwords.hash("not-a-real-account-password")


def limit(request: Request):
    key = request.client.host if request.client else "unknown"
    with lock:
        now = monotonic()
        for stale in [k for k, v in attempts.items() if not v or v[-1] < now - 60]:
            del attempts[stale]
        bucket = attempts[key]
        while bucket and bucket[0] < now - 60:
            bucket.popleft()
        if len(bucket) >= 20:
            raise HTTPException(429, "Too many authentication attempts; retry in one minute")
        bucket.append(now)


@router.post("/auth/register", status_code=201, dependencies=[Depends(limit)])
def register(body: Register, db: Session = Depends(get_db)):
    email = str(body.email).lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "Email already registered")
    user = User(
        name=body.name, email=email, password_hash=passwords.hash(body.password), role="member"
    )
    db.add(user)
    db.flush()
    return as_dict(user)


@router.post("/auth/login", dependencies=[Depends(limit)])
def login(body: Login, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == str(body.email).lower()))
    valid = passwords.verify(body.password, user.password_hash if user else dummy_hash)
    if not valid or not user or not user.active:
        raise HTTPException(401, "Invalid email or password")
    return {"access_token": token_for(user), "token_type": "bearer", "user": as_dict(user)}


@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return as_dict(user)


@router.post("/auth/logout", status_code=204)
def logout(user: User = Depends(current_user)):
    user.token_version += 1


@router.get("/users")
def users(
    page: int = 1,
    size: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    from app.core.permissions import visible_projects
    from app.models import ProjectMember

    query = select(User)
    if user.role == "member":
        ids = visible_projects(user).with_only_columns(
            __import__("app.models", fromlist=["Project"]).Project.id
        )
        query = query.where(
            (User.id == user.id)
            | User.id.in_(select(ProjectMember.user_id).where(ProjectMember.project_id.in_(ids)))
        )
    rows = list(db.scalars(query.order_by(User.name)))
    size = max(1, min(size, 500))
    page = max(1, page)
    return {
        "items": [as_dict(u) for u in rows[(page - 1) * size : page * size]],
        "total": len(rows),
        "page": page,
        "size": size,
    }


@router.get("/users/{uid}")
def get_user(uid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if user.role != "admin" and user.id != uid:
        raise HTTPException(403, "Admin permission required")
    target = db.get(User, uid)
    if not target:
        raise HTTPException(404, "User not found")
    return as_dict(target)


@router.patch("/users/{uid}")
def update_user(
    uid: int, body: UserPatch, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    values = body.model_dump(exclude_unset=True)
    if user.role != "admin" and (uid != user.id or set(values) - {"name"}):
        raise HTTPException(403, "Admin permission required")
    target = db.get(User, uid)
    if not target:
        raise HTTPException(404, "User not found")
    if uid == user.id and (
        values.get("active") is False or values.get("role", user.role) != user.role
    ):
        raise HTTPException(409, "Cannot revoke your own administrative access")
    if any(v is None for v in values.values()):
        raise HTTPException(422, "Fields cannot be null")
    for key, value in values.items():
        setattr(target, key, value)
    if "role" in values or "active" in values:
        target.token_version += 1
    db.flush()
    return as_dict(target)
