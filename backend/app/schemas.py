from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, create_model

Priority = Literal["Low", "Medium", "High", "Critical"]
ProjectStatus = Literal[
    "Planning", "Not Started", "In Progress", "On Hold", "At Risk", "Completed", "Cancelled"
]
TaskStatus = Literal[
    "Backlog", "To Do", "In Progress", "Blocked", "In Review", "Completed", "Cancelled"
]
MilestoneStatus = Literal["Not Started", "In Progress", "Completed", "Delayed", "At Risk"]
RiskStatus = Literal[
    "Identified", "Under Assessment", "Mitigation Planned", "In Progress", "Resolved", "Accepted"
]
Name = Annotated[str, Field(min_length=1, max_length=200)]
Text = Annotated[str, Field(max_length=10000)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Register(Input):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    email: EmailStr
    password: Annotated[str, Field(min_length=12, max_length=128)]


class Login(Input):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=128)]


class UserPatch(Input):
    name: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    role: Literal["admin", "manager", "member"] | None = None
    active: bool | None = None


class ProjectInput(Input):
    name: Name
    description: Text = ""
    objective: Text = ""
    scope: Text = ""
    owner_id: int
    manager_id: int
    member_ids: list[int] = Field(default_factory=list, max_length=500)
    start_date: date
    end_date: date
    status: ProjectStatus = "Planning"
    priority: Priority = "Medium"
    budget: float | None = Field(default=None, ge=0)
    area: Annotated[str, Field(max_length=120)] = "Engineering"
    override_reason: Text | None = None


class MilestoneInput(Input):
    name: Name
    description: Text = ""
    owner_id: int
    start_date: date
    due_date: date
    status: MilestoneStatus = "Not Started"
    priority: Priority = "Medium"
    deliverables: Text = ""
    completion_notes: Text = ""
    override_reason: Text | None = None


class TaskInput(Input):
    project_id: int
    milestone_id: int | None = None
    title: Name
    description: Text = ""
    assignee_id: int
    priority: Priority = "Medium"
    status: TaskStatus = "Backlog"
    progress: int = Field(default=0, ge=0, le=100)
    start_date: date
    due_date: date
    estimated_effort: float = Field(default=1, gt=0, le=100000)
    actual_effort: float = Field(default=0, ge=0, le=100000)
    labels: list[Annotated[str, Field(max_length=40)]] = Field(default_factory=list, max_length=30)


class RiskInput(Input):
    title: Name
    description: Text = ""
    owner_id: int
    category: Literal[
        "Technical", "Schedule", "Resource", "Budget", "Operational", "Dependency", "External"
    ] = "Technical"
    probability: int = Field(ge=1, le=5)
    impact: int = Field(ge=1, le=5)
    status: RiskStatus = "Identified"
    identified_date: date = Field(default_factory=date.today)
    target_date: date
    mitigation: Text = ""
    contingency: Text = ""
    latest_update: Text = ""


def partial(name: str, model: type[BaseModel]):
    return create_model(
        name,
        __base__=Input,
        **{
            k: (Annotated[f.annotation | None, *f.metadata], None)
            for k, f in model.model_fields.items()
        },
    )


ProjectPatch = partial("ProjectPatch", ProjectInput)
MilestonePatch = partial("MilestonePatch", MilestoneInput)
TaskPatch = partial("TaskPatch", TaskInput)
RiskPatch = partial("RiskPatch", RiskInput)


class DependencyInput(Input):
    prerequisite_id: int
    kind: Literal["FS"] = "FS"


class CommentInput(Input):
    body: Annotated[str, Field(min_length=1, max_length=10000)]


class ReportInput(Input):
    period_start: date
    period_end: date
    comments: Text = ""
    next_priorities: Text = ""
