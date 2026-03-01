"""Data models for the Auto-Claude worktree pipeline.

Defines the core data structures flowing through each pipeline stage:
Ideation → Spec Generation → Worktree Execution → QA Review.
"""

from __future__ import annotations

import enum
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field


class TaskPriority(str, enum.Enum):
    """Priority ranking for ideated tasks, determining merge order."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TaskStatus(str, enum.Enum):
    """Lifecycle status of a task through the pipeline."""

    IDEATED = "ideated"
    SPECIFIED = "specified"
    DEFERRED = "deferred"
    MERGED = "merged"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    QA_PENDING = "qa_pending"
    QA_APPROVED = "qa_approved"
    QA_REJECTED = "qa_rejected"
    QA_REJECTED_PERMANENT = "qa_rejected_permanent"
    MERGE_READY = "merge_ready"
    MERGE_COMPLETE = "merge_complete"
    MERGE_CONFLICT = "merge_conflict"
    FAILED = "failed"


class QAVerdict(str, enum.Enum):
    """Possible outcomes from the QA review pipeline."""

    APPROVED = "approved"
    REJECTED_WITH_FIXES = "rejected_with_fixes"
    REJECTED_PERMANENT = "rejected_permanent"


class Task(BaseModel):
    """A single ideated task from codebase analysis.

    Represents one unit of work identified during the ideation phase.
    Each task has a unique identifier, scope boundary, dependencies,
    and priority ranking.
    """

    id: str = Field(description="Unique task identifier, e.g. 'modularization', 'reduce-any-types'")
    title: str = Field(description="Human-readable task title")
    description: str = Field(description="Detailed description of what needs to be done")
    scope: list[str] = Field(
        default_factory=list,
        description="Files and modules affected by this task",
    )
    dependencies: list[str] = Field(
        default_factory=list,
        description="Task IDs that must complete before this task",
    )
    priority: TaskPriority = Field(default=TaskPriority.MEDIUM)
    status: TaskStatus = Field(default=TaskStatus.IDEATED)
    created_at: datetime = Field(default_factory=datetime.now)
    tags: list[str] = Field(default_factory=list, description="Categorization tags")

    def is_blocked(self, completed_task_ids: set[str]) -> bool:
        """Check if this task is blocked by incomplete dependencies."""
        return bool(set(self.dependencies) - completed_task_ids)

    def has_scope_overlap(self, other: Task) -> bool:
        """Check if two tasks modify overlapping files."""
        return bool(set(self.scope) & set(other.scope))


class Spec(BaseModel):
    """Implementation specification generated from an ideated task.

    Provides enough detail for an autonomous agent to execute the task
    without further human guidance. Includes acceptance criteria for
    downstream QA validation.
    """

    task_id: str = Field(description="ID of the source task")
    objective: str = Field(description="Single-sentence end-state description")
    files_in_scope: list[str] = Field(
        description="Explicit list of files to create, modify, or delete"
    )
    implementation_steps: list[str] = Field(
        description="Ordered sequence of changes to make"
    )
    acceptance_criteria: list[str] = Field(
        description="Concrete, verifiable conditions for completion"
    )
    risk_notes: list[str] = Field(
        default_factory=list,
        description="Known pitfalls, edge cases, or compatibility concerns",
    )
    estimated_complexity: str = Field(
        default="medium",
        description="Complexity estimate: low, medium, high",
    )
    branch_name: str = Field(default="", description="Git branch name (auto-generated if empty)")
    generated_at: datetime = Field(default_factory=datetime.now)

    def get_branch_name(self) -> str:
        """Return the branch name, generating one from task_id if not set."""
        if self.branch_name:
            return self.branch_name
        return f"auto/{self.task_id}"

    def to_prompt_context(self) -> str:
        """Format the spec as context for a Claude agent prompt."""
        lines = [
            f"# Task: {self.task_id}",
            f"\n## Objective\n{self.objective}",
            "\n## Files in Scope",
        ]
        for f in self.files_in_scope:
            lines.append(f"- {f}")
        lines.append("\n## Implementation Steps")
        for i, step in enumerate(self.implementation_steps, 1):
            lines.append(f"{i}. {step}")
        lines.append("\n## Acceptance Criteria")
        for criterion in self.acceptance_criteria:
            lines.append(f"- [ ] {criterion}")
        if self.risk_notes:
            lines.append("\n## Risk Notes")
            for note in self.risk_notes:
                lines.append(f"- ⚠️ {note}")
        return "\n".join(lines)


class WorktreeState(BaseModel):
    """Runtime state of a single worktree execution environment.

    Tracks the lifecycle of an agent working in an isolated git worktree,
    from creation through execution to completion or failure.
    """

    task_id: str
    spec: Spec
    worktree_path: Path
    branch_name: str
    pid: int | None = Field(default=None, description="Process ID of the Claude agent")
    status: TaskStatus = Field(default=TaskStatus.IN_PROGRESS)
    started_at: datetime = Field(default_factory=datetime.now)
    completed_at: datetime | None = None
    error_message: str | None = None
    session_count: int = Field(default=0, description="Number of Claude sessions consumed")
    retry_count: int = Field(default=0, description="Number of QA rejection retries")

    class Config:
        arbitrary_types_allowed = True

    def mark_completed(self) -> None:
        """Mark the worktree execution as completed."""
        self.status = TaskStatus.COMPLETED
        self.completed_at = datetime.now()

    def mark_failed(self, error: str) -> None:
        """Mark the worktree execution as failed with an error message."""
        self.status = TaskStatus.FAILED
        self.error_message = error
        self.completed_at = datetime.now()

    def elapsed_seconds(self) -> float:
        """Calculate elapsed time since execution started."""
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()


class QAResult(BaseModel):
    """Result of a QA review for a completed worktree task.

    The QA agent produces this after reviewing the diff against the
    original spec's acceptance criteria. Contains the verdict and
    detailed findings.
    """

    task_id: str
    verdict: QAVerdict
    summary: str = Field(description="Brief summary of the review findings")
    passed_criteria: list[str] = Field(
        default_factory=list,
        description="Acceptance criteria that passed review",
    )
    failed_criteria: list[str] = Field(
        default_factory=list,
        description="Acceptance criteria that failed review",
    )
    issues: list[str] = Field(
        default_factory=list,
        description="Specific issues found during review",
    )
    remediation_instructions: list[str] = Field(
        default_factory=list,
        description="Instructions for fixing rejected work",
    )
    reviewed_at: datetime = Field(default_factory=datetime.now)
    review_pass_number: int = Field(default=1, description="Which review pass this is (1, 2, ...)")

    def is_approved(self) -> bool:
        """Check if the QA review approved the work."""
        return self.verdict == QAVerdict.APPROVED

    def is_permanently_rejected(self) -> bool:
        """Check if the task was permanently rejected."""
        return self.verdict == QAVerdict.REJECTED_PERMANENT


class MergeResult(BaseModel):
    """Result of attempting to merge an approved branch into main."""

    task_id: str
    branch_name: str
    success: bool
    conflict_files: list[str] = Field(default_factory=list)
    merge_commit: str | None = None
    error_message: str | None = None
    merged_at: datetime = Field(default_factory=datetime.now)


class PipelineManifest(BaseModel):
    """Top-level manifest tracking the entire pipeline run.

    Aggregates state across all four pipeline stages for reporting
    and resumability.
    """

    repo_path: str
    started_at: datetime = Field(default_factory=datetime.now)
    tasks: list[Task] = Field(default_factory=list)
    specs: list[Spec] = Field(default_factory=list)
    worktrees: list[WorktreeState] = Field(default_factory=list)
    qa_results: list[QAResult] = Field(default_factory=list)
    merge_results: list[MergeResult] = Field(default_factory=list)

    @property
    def tasks_ideated(self) -> int:
        return len(self.tasks)

    @property
    def specs_generated(self) -> int:
        return len(self.specs)

    @property
    def qa_reports_produced(self) -> int:
        return len(self.qa_results)

    @property
    def qa_first_pass_rejection_rate(self) -> float:
        """Calculate the first-pass QA rejection rate."""
        first_pass = [r for r in self.qa_results if r.review_pass_number == 1]
        if not first_pass:
            return 0.0
        rejected = sum(1 for r in first_pass if not r.is_approved())
        return rejected / len(first_pass)

    def summary_table(self) -> dict[str, int | float]:
        """Generate summary metrics matching the blog post format."""
        return {
            "tasks_ideated": self.tasks_ideated,
            "specs_generated": self.specs_generated,
            "qa_reports_produced": self.qa_reports_produced,
            "branches_created": len({s.get_branch_name() for s in self.specs}),
            "qa_first_pass_rejection_rate": round(self.qa_first_pass_rejection_rate * 100, 1),
            "merges_completed": sum(1 for m in self.merge_results if m.success),
            "merge_conflicts": sum(1 for m in self.merge_results if not m.success),
        }
