"""Worktree creation and agent spawning.

The core of Auto-Claude: creates isolated git worktrees for each task
and spawns independent Claude agents to execute the specs in parallel.
Each agent operates in complete isolation — no cross-contamination possible.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import PipelineConfig
from .models import Spec, TaskStatus, WorktreeState

logger = logging.getLogger(__name__)

EXECUTION_SYSTEM_PROMPT = """\
You are an autonomous software engineer executing a specific implementation task.

You are working in an isolated git worktree. You have full access to read and write
files within this worktree. Your changes will not affect any other branch or worktree.

Follow the implementation spec exactly. Complete all steps. Meet all acceptance criteria.
When finished, commit your changes with a clear commit message describing what was done.

Important:
- Do NOT modify files outside the spec's scope unless absolutely necessary
- Do NOT introduce new dependencies without documenting why
- Commit early and often — each logical change should be its own commit
- If you encounter a blocker, document it clearly in a BLOCKERS.md file"""


def create_worktree(
    repo_path: Path,
    branch_name: str,
    base_dir: Path,
) -> Path:
    """Create a new git worktree for a task.

    Creates the worktree from the current HEAD of the main branch,
    checked out to a new branch named after the task.

    Args:
        repo_path: Path to the main repository.
        branch_name: Name for the new branch (e.g., 'auto/modularization').
        base_dir: Base directory for worktrees.

    Returns:
        Path to the created worktree.

    Raises:
        RuntimeError: If worktree creation fails.
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    worktree_path = base_dir / branch_name.replace("/", "-")

    # Remove existing worktree if it exists (from a previous run)
    if worktree_path.exists():
        logger.warning("Removing existing worktree at %s", worktree_path)
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_path, capture_output=True,
        )

    # Delete branch if it exists (from a previous run)
    subprocess.run(
        ["git", "branch", "-D", branch_name],
        cwd=repo_path, capture_output=True,
    )

    # Create fresh worktree with new branch
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_path)],
        cwd=repo_path, capture_output=True, text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to create worktree '{branch_name}': {result.stderr.strip()}"
        )

    logger.info("Created worktree: %s -> %s", branch_name, worktree_path)
    return worktree_path


def inject_spec(worktree_path: Path, spec: Spec) -> Path:
    """Write the spec into the worktree as a context file.

    Creates a .auto-claude/spec.md file in the worktree root
    that the agent can reference during execution.

    Args:
        worktree_path: Path to the worktree.
        spec: The implementation specification.

    Returns:
        Path to the written spec file.
    """
    spec_dir = worktree_path / ".auto-claude"
    spec_dir.mkdir(exist_ok=True)

    # Write human-readable spec
    spec_md = spec_dir / "spec.md"
    spec_md.write_text(spec.to_prompt_context())

    # Write machine-readable spec
    spec_json = spec_dir / "spec.json"
    with open(spec_json, "w") as f:
        json.dump(spec.model_dump(mode="json"), f, indent=2, default=str)

    logger.debug("Injected spec into %s", spec_dir)
    return spec_md


def spawn_agent(
    worktree_path: Path,
    spec: Spec,
    config: PipelineConfig,
) -> subprocess.Popen[str]:
    """Spawn a Claude agent scoped to a specific worktree.

    The agent receives the spec as its primary prompt and operates
    entirely within the worktree's filesystem.

    Args:
        worktree_path: Path to the worktree.
        spec: The implementation specification.
        config: Pipeline configuration.

    Returns:
        The running subprocess handle.
    """
    prompt = f"""\
Execute this implementation spec in your current working directory.

{spec.to_prompt_context()}

After completing all steps:
1. Verify each acceptance criterion is met
2. Commit all changes with descriptive messages
3. Create a COMPLETION.md file summarizing what was done
"""

    cmd = [
        "claude", "--print",
        "--model", config.model,
        "--system-prompt", EXECUTION_SYSTEM_PROMPT,
        prompt,
    ]

    logger.info("Spawning agent for '%s' in %s", spec.task_id, worktree_path)

    process = subprocess.Popen(
        cmd,
        cwd=worktree_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    return process


def execute_in_worktree(
    spec: Spec,
    repo_path: Path,
    config: PipelineConfig,
    base_dir: Path,
) -> WorktreeState:
    """Create a worktree and execute a spec in it.

    This is the complete lifecycle for a single task: create worktree,
    inject spec, spawn agent, wait for completion, capture result.

    Args:
        spec: The implementation specification.
        repo_path: Path to the main repository.
        config: Pipeline configuration.
        base_dir: Base directory for worktrees.

    Returns:
        WorktreeState capturing the execution result.
    """
    branch_name = spec.get_branch_name()

    state = WorktreeState(
        task_id=spec.task_id,
        spec=spec,
        worktree_path=Path("."),  # Placeholder, updated below
        branch_name=branch_name,
    )

    try:
        # Stage 1: Create worktree
        worktree_path = create_worktree(repo_path, branch_name, base_dir)
        state.worktree_path = worktree_path

        # Stage 2: Inject spec
        inject_spec(worktree_path, spec)

        # Stage 3: Spawn agent
        process = spawn_agent(worktree_path, spec, config)
        state.pid = process.pid

        # Stage 4: Wait for completion with timeout
        try:
            stdout, stderr = process.communicate(timeout=config.timeout_seconds)
            state.session_count = 1

            if process.returncode == 0:
                state.mark_completed()
                logger.info(
                    "Task '%s' completed in %.1fs",
                    spec.task_id, state.elapsed_seconds(),
                )

                # Write agent output for QA review
                output_path = worktree_path / ".auto-claude" / "agent-output.txt"
                output_path.write_text(stdout)
            else:
                state.mark_failed(f"Agent exited with code {process.returncode}: {stderr.strip()}")
                logger.error("Task '%s' failed: %s", spec.task_id, stderr.strip()[:200])

        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            state.mark_failed(f"Agent timed out after {config.timeout_seconds}s")
            logger.error("Task '%s' timed out", spec.task_id)

    except RuntimeError as e:
        state.mark_failed(str(e))
        logger.error("Worktree setup failed for '%s': %s", spec.task_id, e)

    return state


def run_factory(
    specs: list[Spec],
    repo_path: Path,
    config: PipelineConfig,
) -> list[WorktreeState]:
    """Run the worktree factory: create worktrees and execute all specs in parallel.

    This is the entry point for the worktree execution stage of the pipeline.

    Args:
        specs: List of implementation specifications.
        repo_path: Path to the main repository.
        config: Pipeline configuration.

    Returns:
        List of WorktreeState objects capturing execution results.
    """
    repo_path = repo_path.resolve()
    paths = config.resolve_paths(repo_path)
    base_dir = paths["worktree_base"]

    if not specs:
        logger.warning("No specs to execute")
        return []

    logger.info(
        "Starting worktree factory: %d specs, %d parallel workers",
        len(specs), config.max_parallel_workers,
    )

    states: list[WorktreeState] = []
    start_time = time.monotonic()

    with ThreadPoolExecutor(max_workers=config.max_parallel_workers) as executor:
        future_to_spec = {
            executor.submit(
                execute_in_worktree, spec, repo_path, config, base_dir,
            ): spec
            for spec in specs
        }

        for future in as_completed(future_to_spec):
            spec = future_to_spec[future]
            try:
                state = future.result()
                states.append(state)
            except Exception as e:
                logger.error("Unexpected error executing '%s': %s", spec.task_id, e)
                states.append(WorktreeState(
                    task_id=spec.task_id,
                    spec=spec,
                    worktree_path=base_dir / spec.get_branch_name().replace("/", "-"),
                    branch_name=spec.get_branch_name(),
                    status=TaskStatus.FAILED,
                    error_message=str(e),
                ))

    elapsed = time.monotonic() - start_time
    completed = sum(1 for s in states if s.status == TaskStatus.COMPLETED)
    failed = sum(1 for s in states if s.status == TaskStatus.FAILED)

    logger.info(
        "Worktree factory complete in %.1fs: %d completed, %d failed",
        elapsed, completed, failed,
    )

    return states


def cleanup_worktrees(repo_path: Path, base_dir: Path) -> int:
    """Remove all Auto-Claude worktrees and their branches.

    Args:
        repo_path: Path to the main repository.
        base_dir: Base directory containing worktrees.

    Returns:
        Number of worktrees removed.
    """
    if not base_dir.exists():
        return 0

    count = 0
    for worktree_dir in base_dir.iterdir():
        if not worktree_dir.is_dir():
            continue

        result = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_dir)],
            cwd=repo_path, capture_output=True, text=True,
        )

        if result.returncode == 0:
            count += 1
            logger.debug("Removed worktree: %s", worktree_dir)
        else:
            logger.warning("Failed to remove worktree %s: %s", worktree_dir, result.stderr.strip())

    # Prune stale worktree references
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=repo_path, capture_output=True,
    )

    logger.info("Cleaned up %d worktrees", count)
    return count
