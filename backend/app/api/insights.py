import csv
import io
from datetime import date, timedelta
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.projects import page_result
from app.core.permissions import project_access, visible_projects
from app.core.security import current_user
from app.db.session import get_db
from app.models import Milestone, Notification, Project, Risk, StatusReport, Task, User
from app.schemas import ReportInput
from app.services.analytics import metrics, trends, workload
from app.services.work import as_dict, record, serialize, validate_dates

router = APIRouter()


def ids_for(db, user, project_id=None):
    if project_id:
        project_access(db, user, project_id)
        return [project_id]
    return list(
        db.scalars(
            visible_projects(user).where(Project.archived.is_(False)).with_only_columns(Project.id)
        )
    )


@router.get("/analytics/overview")
def overview(db: Session = Depends(get_db), user: User = Depends(current_user)):
    ids = ids_for(db, user)
    result = metrics(db, ids)
    result["projects"] = [
        {"id": pid, "name": db.get(Project, pid).name, **metrics(db, [pid])} for pid in ids
    ]
    result["projects_at_risk"] = sum(
        p["health"] in ("At Risk", "Critical") for p in result["projects"]
    )
    return result


@router.get("/analytics/projects/{pid}")
def project_metrics(pid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project_access(db, user, pid)
    return metrics(db, [pid])


@router.get("/analytics/workload")
def team_workload(
    project_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    return workload(db, ids_for(db, user, project_id))


@router.get("/analytics/trends")
def historical(
    start: date | None = None,
    end: date | None = None,
    project_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    start, end = start or date.today() - timedelta(days=180), end or date.today()
    validate_dates(start, end)
    if (end - start).days > 3660:
        raise HTTPException(422, "Date range cannot exceed ten years")
    return trends(db, ids_for(db, user, project_id), start, end)


@router.post("/projects/{pid}/reports", status_code=201)
def generate_report(
    pid: int, body: ReportInput, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    project = project_access(db, user, pid, manage=True, lock=True)
    validate_dates(body.period_start, body.period_end)
    tasks = list(db.scalars(select(Task).where(Task.project_id == pid)))
    ms = list(db.scalars(select(Milestone).where(Milestone.project_id == pid)))
    risks = list(
        db.scalars(
            select(Risk).where(Risk.project_id == pid, Risk.status.not_in(["Resolved", "Accepted"]))
        )
    )
    snapshot = {
        "overview": serialize(db, project),
        "kpis": metrics(db, [pid]),
        "completed_work": [
            serialize(db, t)
            for t in tasks
            if t.completed_at and body.period_start <= t.completed_at.date() <= body.period_end
        ],
        "in_progress": [
            serialize(db, t) for t in tasks if t.status in ("In Progress", "In Review")
        ],
        "upcoming_milestones": [
            serialize(db, m) for m in ms if m.status != "Completed" and m.due_date >= date.today()
        ],
        "overdue_tasks": [serialize(db, t) for t in tasks if as_dict(t)["overdue"]],
        "active_risks": [as_dict(r) for r in risks],
        "blockers": [
            serialize(db, t)
            for t in tasks
            if t.status not in ("Completed", "Cancelled")
            and (t.status == "Blocked" or serialize(db, t)["blocked_by"])
        ],
        "mitigation_actions": [
            {
                "risk": r.title,
                "owner_id": r.owner_id,
                "target_date": r.target_date,
                "strategy": r.mitigation,
                "latest_update": r.latest_update,
            }
            for r in risks
        ],
    }
    report = StatusReport(
        project_id=pid, author_id=user.id, snapshot=jsonable_encoder(snapshot), **body.model_dump()
    )
    db.add(report)
    db.flush()
    record(db, user, pid, "report.created", {"id": report.id})
    return as_dict(report)


@router.get("/projects/{pid}/reports")
def reports(
    pid: int,
    page: int = Query(1, ge=1),
    size: int = Query(25, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    project_access(db, user, pid)
    return page_result(
        db,
        select(StatusReport).where(StatusReport.project_id == pid).order_by(StatusReport.id.desc()),
        page,
        size,
    )


def report_for(db, user, rid):
    report = db.get(StatusReport, rid)
    if not report:
        raise HTTPException(404, "Report not found")
    project_access(db, user, report.project_id)
    return report


@router.get("/reports/{rid}")
def report_detail(rid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return as_dict(report_for(db, user, rid))


@router.get("/reports/{rid}/export")
def export(
    rid: int, format: str = "pdf", db: Session = Depends(get_db), user: User = Depends(current_user)
):
    report = report_for(db, user, rid)
    snap = report.snapshot
    sections = {
        "Reporting period": f"{report.period_start} to {report.period_end}",
        **snap,
        "Manager comments": report.comments,
        "Next-period priorities": report.next_priorities,
    }

    def flatten(value):
        if isinstance(value, dict):
            return "; ".join(f"{k}: {flatten(v)}" for k, v in value.items())
        if isinstance(value, list):
            return " | ".join(flatten(v) for v in value) or "None"
        return str(value)

    if format == "csv":
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["Section", "Content"])
        for key, value in sections.items():
            text = flatten(value)
            writer.writerow(
                [key, "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text]
            )
        content, media = output.getvalue().encode("utf-8-sig"), "text/csv"
    elif format == "pdf":
        output = io.BytesIO()
        styles = getSampleStyleSheet()
        story = [Paragraph("Engineering Program Status Report", styles["Title"])]
        for key, value in sections.items():
            story.append(Paragraph(escape(key.replace("_", " ").title()), styles["Heading2"]))
            # Split records into paragraphs to permit page breaks in large reports.
            entries = value if isinstance(value, list) else [value]
            for entry in entries or ["None"]:
                story.append(Paragraph(escape(flatten(entry)), styles["BodyText"]))
                story.append(Spacer(1, 6))
        SimpleDocTemplate(output).build(story)
        content, media = output.getvalue(), "application/pdf"
    else:
        raise HTTPException(422, "Format must be pdf or csv")
    return Response(
        content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="report-{rid}.{format}"'},
    )


@router.get("/notifications")
def notifications(
    page: int = Query(1, ge=1),
    size: int = Query(25, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return page_result(
        db,
        select(Notification)
        .where(Notification.user_id == user.id, Notification.project_id.in_(ids_for(db, user)))
        .order_by(Notification.id.desc()),
        page,
        size,
    )


@router.patch("/notifications/{nid}/read")
def read_notification(nid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    obj = db.get(Notification, nid)
    if not obj or obj.user_id != user.id:
        raise HTTPException(404, "Notification not found")
    obj.read = True
    return as_dict(obj)
