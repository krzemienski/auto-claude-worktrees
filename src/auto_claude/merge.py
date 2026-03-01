"""Priority-weighted merge queue for approved branches.

Handles merging QA-approved branches back into main in dependency
and priority order, with conflict detection and re-execution support.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .models import (
    MergeResult,
    QAResult,
    QAVerdict,
    Task,
    TaskPriority,
    TaskStatus,
    WorktreeState,
)

logger = logging.getLogger(__name__)


def compute_merge_order(
    tasks: list[Task],
    approved_ids: set[str],
) -> list[str]:
    """Compute the optimal merge order based on priority and dependencies.

    Merge strategy (from the blog post):
    1. Foundation tasks first (shared infrastructure, type definitions)
    2. Small focused tasks before large refactors
    3. Dependency ordering respected
    4. Within same priority: alphabetical for determinism

    Args:
        tasks: All tasks in the pipeline.
        approved_ids: Set of task IDs that passed QA.

    Returns:
        Ordered list of task IDs to merge.
    """
    approved_tasks = [t for t in tasks if t.id in approved_ids]

    # Topological sort respecting dependencies
    sorted_ids: list[str] = []
    visited: set[str] = set()
    in_progress: set[str] = set()

    task_map = {t.id: t for t in approved_tasks}

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in in_progress:
            logger.warning("Circular dependency detected involving '%s'", task_id)
            return
        if task_id not in task_map:
            return

        in_progress.add(task_id)
        task = task_map[task_id]

        # Visit dependencies first
        for dep in task.dependencies:
            if dep in approved_ids:
                visit(dep)

        in_progress.discard(task_id)
        visited.add(task_id)
        sorted_ids.append(task_id)

    # Visit in priority order (critical first, then high, etc.)
    priority_order = {
        TaskPriority.CRITICAL: 0,
        TaskPriority.HIGH: 1,
        TaskPriority.MEDIUM: 2,
        TaskPriority.LOW: 3,
    }

    for task in sorted(
        approved_tasks,
        key=lambda t: (priority_order[t.priority], len(t.scope), t.id),
    ):
        visit(task.id)

    return sorted_ids


def check_conflicts(repo_path: Path, branch_name: str) -> list[str]:
    """Check if a branch has merge conflicts with the current main.

    Performs a dry-run merge to detect conflicts without actually merging.

    Args:
        repo_path: Path to the main repository.
        branch_name: Branch name to check.

    Returns:
        List of conflicting file paths (empty if no conflicts).
    """
    # Try a merge dry-run
    result = subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", branch_name],
        cwd=repo_path, capture_output=True, text=True,
    )

    conflicts: list[str] = []

    if result.returncode != 0:
        # Check for conflict markers
        status = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if status.stdout.strip():
            conflicts = status.stdout.strip().split("\n")

    # Always abort the attempted merge
    subprocess.run(
        ["git", "merge", "--abort"],
        cwd=repo_path, capture_output=True,
    )

    return conflicts


def merge_branch(repo_path: Path, branch_name: str, task_id: str) -> MergeResult:
    """Merge a single approved branch into the current branch (main).

    Args:
        repo_path: Path to the main repository.
        branch_name: Branch to merge.
        task_id: Task identifier for logging.

    Returns:
        MergeResult capturing the outcome.
    """
    logger.info("Merging branch '%s' (task: %s)", branch_name, task_id)

    # First check for conflicts
    conflicts = check_conflicts(repo_path, branch_name)
    if conflicts:
        logger.warning(
            "Branch '%s' has conflicts with %d files: %s",
            branch_name, len(conflicts), ", ".join(conflicts[:5]),
        )
        return MergeResult(
            task_id=task_id,
            branch_name=branch_name,
            success=False,
            conflict_files=conflicts,
            error_message=f"Merge conflicts in {len(conflicts)} files",
        )

    # Perform the actual merge
    result = subprocess.run(
        ["git", "merge", "--no-ff", "-m", f"auto-claude: Merge {branch_name} ({task_id})",
         branch_name],
        cwd=repo_path, capture_output=True, text=True,
    )

    if result.returncode != 0:
        # Abort on failure
        subprocess.run(["git", "merge", "--abort"], cwd=repo_path, capture_output=True)
        return MergeResult(
            task_id=task_id,
            branch_name=branch_name,
            success=False,
            error_message=f"Merge failed: {result.stderr.strip()[:200]}",
        )

    # Get the merge commit hash
    commit_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path, capture_output=True, text=True,
    )
    commit_hash = commit_result.stdout.strip() if commit_result.returncode == 0 else None

    logger.info("Successfully merged '%s' -> %s", branch_name, commit_hash[:8] if commit_hash else "unknown")

    return MergeResult(
        task_id=task_id,
        branch_name=branch_name,
        success=True,
        merge_commit=commit_hash,
    )


def run_merge_queue(
    tasks: list[Task],
    states: list[WorktreeState],
    qa_results: list[QAResult],
    repo_path: Path,
) -> list[MergeResult]:
    """Run the priority-weighted merge queue for all approved tasks.

    This is the entry point for the merge stage of the pipeline.

    Args:
        tasks: All tasks in the pipeline (for dependency info).
        states: Worktree states (for branch names).
        qa_results: QA results (to identify approved tasks).
        repo_path: Path to the main repository.

    Returns:
        List of MergeResult objects.
    """
    repo_path = repo_path.resolve()

    # Identify approved tasks
    approved_ids: set[str] = set()
    for result in qa_results:
        if result.verdict == QAVerdict.APPROVED:
            approved_ids.add(result.task_id)

    if not approved_ids:
        logger.warning("No approved tasks to merge")
        return []

    # Build state lookup
    state_map = {s.task_id: s for s in states}

    # Compute merge order
    merge_order = compute_merge_order(tasks, approved_ids)
    logger.info("Merge queue: %d tasks in order: %s", len(merge_order), merge_order)

    results: list[MergeResult] = []
    merged_ids: set[str] = set()
    conflict_ids: set[str] = set()

    for task_id in merge_order:
        state = state_map.get(task_id)
        if not state:
            logger.warning("No worktree state found for approved task '%s'", task_id)
            continue

        merge_result = merge_branch(repo_path, state.branch_name, task_id)
        results.append(merge_result)

        if merge_result.success:
            merged_ids.add(task_id)
            state.status = TaskStatus.MERGE_COMPLETE
        else:
            conflict_ids.add(task_id)
            state.status = TaskStatus.MERGE_CONFLICT

    # Summary
    logger.info(
        "Merge queue complete: %d merged, %d conflicts",
        len(merged_ids), len(conflict_ids),
    )

    if conflict_ids:
        logger.info(
            "Tasks with conflicts (need re-execution against updated main): %s",
            ", ".join(sorted(conflict_ids)),
        )

    return results


def cleanup_merged_branches(repo_path: Path, merge_results: list[MergeResult]) -> int:
    """Delete branches that have been successfully merged.

    Args:
        repo_path: Path to the main repository.
        merge_results: Results from the merge queue.

    Returns:
        Number of branches deleted.
    """
    deleted = 0
    for result in merge_results:
        if not result.success:
            continue

        del_result = subprocess.run(
            ["git", "branch", "-d", result.branch_name],
            cwd=repo_path, capture_output=True, text=True,
        )

        if del_result.returncode == 0:
            deleted += 1
            logger.debug("Deleted merged branch: %s", result.branch_name)
        else:
            logger.warning(
                "Failed to delete branch '%s': %s",
                result.branch_name, del_result.stderr.strip(),
            )

    logger.info("Cleaned up %d merged branches", deleted)
    return deleted
