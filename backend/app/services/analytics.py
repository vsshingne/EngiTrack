from collections import Counter
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Milestone, Project, Risk, Task, User
from app.services.work import progress


def rate(n: int, d: int) -> float:
    return round(n / d * 100, 1) if d else 0.0


def distribution(values) -> list[dict]:
    return [{"name": key, "value": count} for key, count in sorted(Counter(values).items())]


def metrics(db: Session, project_ids: list[int], today: date | None = None) -> dict:
    today = today or date.today()
    tasks = list(db.scalars(select(Task).where(Task.project_id.in_(project_ids))))
    milestones = list(db.scalars(select(Milestone).where(Milestone.project_id.in_(project_ids))))
    risks = list(db.scalars(select(Risk).where(Risk.project_id.in_(project_ids))))
    projects = list(db.scalars(select(Project).where(Project.id.in_(project_ids))))
    eligible = [t for t in tasks if t.status != "Cancelled"]
    completed = [t for t in eligible if t.status == "Completed"]
    overdue = [t for t in eligible if t.status != "Completed" and t.due_date < today]
    active_risks = [r for r in risks if r.status not in ("Resolved", "Accepted")]
    high = [r for r in active_risks if r.probability * r.impact >= 10]
    critical = [r for r in active_risks if r.probability * r.impact >= 17]
    done_milestones = [m for m in milestones if m.status == "Completed"]
    expected = []
    for task in eligible:
        duration = max((task.due_date - task.start_date).days, 1)
        planned = min(100, max(0, (today - task.start_date).days / duration * 100))
        expected.append((planned, task.estimated_effort))
    effort = sum(weight for _, weight in expected)
    planned_progress = round(sum(p * w for p, w in expected) / effort, 1) if effort else 0
    actual = progress(tasks)
    gap = round(actual - planned_progress, 1)
    delayed_milestones = [m for m in milestones if m.status != "Completed" and m.due_date < today]
    reasons = []
    if critical:
        reasons.append(f"{len(critical)} active critical risk(s)")
    elif high:
        reasons.append(f"{len(high)} active high risk(s)")
    if overdue:
        reasons.append(
            f"{len(overdue)} overdue task(s), {rate(len(overdue), len(eligible))}% of eligible work"
        )
    if delayed_milestones:
        reasons.append(f"{len(delayed_milestones)} overdue milestone(s)")
    if gap < -10:
        reasons.append(f"Actual progress trails plan by {abs(gap)} percentage points")
    health = (
        "Critical"
        if critical or gap < -25 or rate(len(overdue), len(eligible)) > 30
        else "At Risk"
        if reasons
        else "Healthy"
        if eligible
        else "Not Assessed"
    )
    on_time = sum(bool(t.completed_at and t.completed_at.date() <= t.due_date) for t in completed)
    variances = [(t.due_date - t.completed_at.date()).days for t in completed if t.completed_at]
    return {
        "total_projects": len(projects),
        "active_projects": sum(p.status not in ("Completed", "Cancelled") for p in projects),
        "completed_projects": sum(p.status == "Completed" for p in projects),
        "total_tasks": len(tasks),
        "eligible_tasks": len(eligible),
        "completed_tasks": len(completed),
        "task_completion_rate": rate(len(completed), len(eligible)),
        "overdue_tasks": len(overdue),
        "total_milestones": len(milestones),
        "completed_milestones": len(done_milestones),
        "milestone_completion_rate": rate(len(done_milestones), len(milestones)),
        "progress": actual,
        "planned_progress": planned_progress,
        "schedule_variance_pp": gap,
        "completed_schedule_variance_days": round(sum(variances) / len(variances), 1)
        if variances
        else None,
        "on_time_delivery_rate": rate(on_time, len(completed)),
        "on_time_denominator": len(completed),
        "open_risks": len(active_risks),
        "critical_risks": len(critical),
        "risk_exposure": sum(r.probability * r.impact for r in active_risks),
        "health": health,
        "health_reasons": reasons
        or (
            ["No eligible tasks to assess"]
            if not eligible
            else ["No overdue work, material schedule gap, or active high risks"]
        ),
        "project_status": distribution(p.status for p in projects),
        "task_status": distribution(t.status for t in tasks),
        "milestone_status": distribution(m.status for m in milestones),
        "risk_severity": distribution(
            "Critical"
            if r.probability * r.impact >= 17
            else "High"
            if r.probability * r.impact >= 10
            else "Medium"
            if r.probability * r.impact >= 5
            else "Low"
            for r in active_risks
        ),
        "upcoming": [
            {
                "id": t.id,
                "title": t.title,
                "due_date": t.due_date.isoformat(),
                "project_id": t.project_id,
            }
            for t in sorted(eligible, key=lambda t: t.due_date)
            if t.status != "Completed" and today <= t.due_date <= today + timedelta(days=14)
        ],
    }


def workload(db: Session, project_ids: list[int]) -> list[dict]:
    tasks = list(
        db.scalars(
            select(Task).where(
                Task.project_id.in_(project_ids), Task.status.not_in(["Completed", "Cancelled"])
            )
        )
    )
    counts = Counter(t.assignee_id for t in tasks)
    average = len(tasks) / len(counts) if counts else 0
    return [
        {
            "user_id": uid,
            "name": db.get(User, uid).name,
            "value": count,
            "effort": round(sum(t.estimated_effort for t in tasks if t.assignee_id == uid), 1),
            "overloaded": count > average * 1.5,
        }
        for uid, count in counts.items()
    ]


def trends(db: Session, project_ids: list[int], start: date, end: date) -> list[dict]:
    tasks = list(
        db.scalars(
            select(Task).where(
                Task.project_id.in_(project_ids),
                Task.status == "Completed",
                Task.completed_at.is_not(None),
            )
        )
    )
    buckets: dict[str, list] = {}
    month = start.replace(day=1)
    while month <= end:
        buckets[month.strftime("%Y-%m")] = []
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    for task in tasks:
        if start <= task.completed_at.date() <= end:
            buckets[task.completed_at.strftime("%Y-%m")].append(task)
    return [
        {
            "name": month,
            "completed": len(items),
            "variance_days": round(
                sum((t.due_date - t.completed_at.date()).days for t in items) / len(items), 1
            )
            if items
            else None,
        }
        for month, items in buckets.items()
    ]
