from datetime import date, timedelta

import pytest


def task(client, tokens, project, **extra):
    data = {
        "project_id": project["id"],
        "title": "Implement service",
        "assignee_id": 3,
        "start_date": "2020-01-01",
        "due_date": "2025-01-01",
        **extra,
    }
    r = client.post("/api/v1/tasks", headers=tokens["manager"], json=data)
    assert r.status_code == 201, r.text
    return r.json()


def test_auth(context):
    c, tokens, _ = context
    assert c.get("/api/v1/auth/me").status_code == 401
    assert c.get("/api/v1/auth/me", headers={"Authorization": "Bearer bad"}).status_code == 401
    r = c.post(
        "/api/v1/auth/register",
        json={"name": "New", "email": "new@example.com", "password": "StrongPassword123!"},
    )
    assert r.status_code == 201 and r.json()["role"] == "member"
    assert "password_hash" not in r.json()
    assert (
        c.post(
            "/api/v1/auth/login", json={"email": "new@example.com", "password": "wrong"}
        ).status_code
        == 401
    )
    r = c.post(
        "/api/v1/auth/login", json={"email": "new@example.com", "password": "StrongPassword123!"}
    )
    h = {"Authorization": "Bearer " + r.json()["access_token"]}
    assert c.get("/api/v1/auth/me", headers=h).status_code == 200
    assert c.post("/api/v1/auth/logout", headers=h).status_code == 204
    assert c.get("/api/v1/auth/me", headers=h).status_code == 401
    assert (
        c.patch("/api/v1/users/3", headers=tokens["member"], json={"role": "admin"}).status_code
        == 403
    )


def test_project_access(context, project):
    c, h, _ = context
    pid = project["id"]
    assert c.get(f"/api/v1/projects/{pid}", headers=h["Outsider"]).status_code == 404
    assert c.get("/api/v1/projects", headers=h["Outsider"]).json()["total"] == 0
    assert (
        c.patch(f"/api/v1/projects/{pid}", headers=h["member"], json={"name": "Hijack"}).status_code
        == 403
    )
    assert (
        c.patch(f"/api/v1/projects/{pid}", headers=h["manager"], json={"name": "New name"}).json()[
            "name"
        ]
        == "New name"
    )


def test_task_assignment_progress_and_kpis(context, project):
    c, h, _ = context
    t = task(c, h, project)
    assert (
        c.patch(
            f"/api/v1/tasks/{t['id']}", headers=h["manager"], json={"assignee_id": 4}
        ).status_code
        == 422
    )
    assert (
        c.patch(
            f"/api/v1/tasks/{t['id']}", headers=h["member"], json={"title": "Wrong"}
        ).status_code
        == 403
    )
    r = c.patch(
        f"/api/v1/tasks/{t['id']}/status", headers=h["member"], json={"status": "Completed"}
    )
    assert r.status_code == 200 and r.json()["completed_at"] and r.json()["progress"] == 100
    task(c, h, project, title="Cancelled work", status="Cancelled")
    k = c.get(f"/api/v1/analytics/projects/{project['id']}", headers=h["manager"]).json()
    assert k["task_completion_rate"] == 100 and k["eligible_tasks"] == 1
    assert k["on_time_denominator"] == 1
    assert (
        c.get(f"/api/v1/projects/{project['id']}", headers=h["manager"]).json()["progress"] == 100
    )


def test_dependencies(context, project):
    c, h, _ = context
    a, b, d = [task(c, h, project, title=name) for name in ("A", "B", "C")]

    def dep(child, parent):
        return c.post(
            f"/api/v1/tasks/{child['id']}/dependencies",
            headers=h["manager"],
            json={"prerequisite_id": parent["id"]},
        )

    assert dep(a, a).status_code == 422
    assert dep(b, a).status_code == 201
    assert dep(d, b).status_code == 201
    assert dep(a, d).status_code == 409
    assert (
        c.patch(
            f"/api/v1/tasks/{b['id']}", headers=h["member"], json={"status": "In Progress"}
        ).status_code
        == 409
    )
    c.patch(f"/api/v1/tasks/{a['id']}", headers=h["member"], json={"status": "Completed"})
    assert (
        c.patch(
            f"/api/v1/tasks/{b['id']}", headers=h["member"], json={"status": "In Progress"}
        ).status_code
        == 200
    )
    assert (
        c.patch(
            f"/api/v1/tasks/{a['id']}", headers=h["member"], json={"status": "To Do"}
        ).status_code
        == 409
    )


def test_milestone_completion_and_override(context, project):
    c, h, _ = context
    url = f"/api/v1/projects/{project['id']}/milestones"
    r = c.post(
        url,
        headers=h["manager"],
        json={
            "name": "Release",
            "owner_id": 2,
            "start_date": "2020-01-01",
            "due_date": "2030-01-01",
        },
    )
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    task(c, h, project, milestone_id=mid)
    assert (
        c.patch(
            f"/api/v1/milestones/{mid}", headers=h["manager"], json={"status": "Completed"}
        ).status_code
        == 409
    )
    assert (
        c.patch(
            f"/api/v1/milestones/{mid}",
            headers=h["manager"],
            json={"status": "Completed", "override_reason": "Approved partial delivery"},
        ).status_code
        == 200
    )
    events = c.get(f"/api/v1/projects/{project['id']}/activity", headers=h["manager"]).json()[
        "items"
    ]
    assert any(e["event"] == "completion.override" for e in events)


@pytest.mark.parametrize(
    "probability,impact,severity",
    [(1, 4, "Low"), (3, 3, "Medium"), (4, 4, "High"), (5, 5, "Critical")],
)
def test_risk_scores(context, project, probability, impact, severity):
    c, h, _ = context
    r = c.post(
        f"/api/v1/projects/{project['id']}/risks",
        headers=h["manager"],
        json={
            "title": "Supply delay",
            "owner_id": 2,
            "probability": probability,
            "impact": impact,
            "target_date": (date.today() + timedelta(days=30)).isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["score"] == probability * impact and r.json()["severity"] == severity


def test_health_reports_exports(context, project):
    c, h, _ = context
    task(c, h, project)
    k = c.get(f"/api/v1/analytics/projects/{project['id']}", headers=h["manager"]).json()
    assert k["overdue_tasks"] == 1 and k["health"] == "Critical"
    assert (
        c.patch(
            f"/api/v1/projects/{project['id']}", headers=h["manager"], json={"status": "Completed"}
        ).status_code
        == 409
    )
    r = c.post(
        f"/api/v1/projects/{project['id']}/reports",
        headers=h["manager"],
        json={
            "period_start": "2025-01-01",
            "period_end": "2030-01-01",
            "comments": "Delivery review",
        },
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["snapshot"]["kpis"]["overdue_tasks"] == 1
    assert c.get(f"/api/v1/reports/{rid}/export", headers=h["manager"]).content.startswith(b"%PDF")
    assert (
        "Delivery review"
        in c.get(f"/api/v1/reports/{rid}/export?format=csv", headers=h["manager"]).text
    )
    assert c.get(f"/api/v1/reports/{rid}", headers=h["Outsider"]).status_code == 404


def test_patch_validation_and_rollback(context, project):
    c, h, _ = context
    t = task(c, h, project)
    for payload in [
        {"estimated_effort": -1},
        {"progress": 150},
        {"title": ""},
        {"due_date": "2019-01-01"},
        {"status": None},
    ]:
        assert (
            c.patch(f"/api/v1/tasks/{t['id']}", headers=h["manager"], json=payload).status_code
            == 422
        )
    assert c.get(f"/api/v1/tasks/{t['id']}", headers=h["manager"]).json()["estimated_effort"] == 1
