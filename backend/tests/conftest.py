import os

os.environ["JWT_SECRET"] = "isolated-test-secret-not-for-production-12345"
os.environ["DATABASE_URL"] = "sqlite://"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.auth import attempts
from app.core.security import passwords, token_for
from app.db.session import Base, get_db
from app.main import app
from app.models import User


@pytest.fixture
def context():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def fk(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        users = [
            User(
                name=role,
                email=f"{role}@example.com",
                role=role,
                password_hash=passwords.hash("TestPassword123!"),
            )
            for role in ("admin", "manager", "member")
        ]
        users.append(
            User(
                name="Outsider",
                email="outsider@example.com",
                role="member",
                password_hash=passwords.hash("TestPassword123!"),
            )
        )
        db.add_all(users)
        db.commit()
        tokens = {u.name: {"Authorization": "Bearer " + token_for(u)} for u in users}

    def override():
        with Session(engine, expire_on_commit=False) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    app.dependency_overrides[get_db] = override
    attempts.clear()
    with TestClient(app) as client:
        yield client, tokens, engine
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture
def project(context):
    client, tokens, _ = context
    response = client.post(
        "/api/v1/projects",
        headers=tokens["manager"],
        json={
            "name": "Platform",
            "owner_id": 2,
            "manager_id": 2,
            "member_ids": [3],
            "start_date": "2020-01-01",
            "end_date": "2035-12-31",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()
