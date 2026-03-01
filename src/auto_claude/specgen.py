"""Spec generation from task descriptions.

Takes ideated tasks and produces detailed implementation specifications
with acceptance criteria, file scopes, and risk notes — enough detail
for an autonomous agent to execute without further human guidance.
"""

from __future__ import annotations

import json
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import PipelineConfig
from .models import Spec, Task, TaskStatus

logger = logging.getLogger(__name__)

SPEC_SYSTEM_PROMPT = """\
You are an expert technical writer generating implementation specifications for autonomous AI agents.

Given a task description and codebase context, produce a detailed spec that an agent can execute
without further clarification. The spec must be precise enough that a QA reviewer can verify
completion against the acceptance criteria.

Respond with a single JSON object containing:
- "task_id": The task identifier (from input)
- "objective": A single sentence describing the end state
- "files_in_scope": Explicit list of files to create, modify, or delete
- "implementation_steps": Ordered sequence of specific changes
- "acceptance_criteria": Concrete, verifiable conditions for completion
- "risk_notes": Known pitfalls, edge cases, or compatibility concerns
- "estimated_complexity": "low", "medium", or "high"

Guidelines:
- Be explicit about file paths — no vague references like "the config file"
- Each implementation step should be one atomic action
- Acceptance criteria must be objectively verifiable (not "code is clean")
- Risk notes should warn about specific gotchas the agent might hit

Respond with ONLY the JSON object. No markdown, no commentary."""


def build_task_context(task: Task, repo_path: Path) -> str:
    """Build context string for a specific task's scope.

    Reads the files referenced in the task's scope to give the spec
    generator concrete context about what exists.

    Args:
        task: The task to build context for.
        repo_path: Repository root path.

    Returns:
        Formatted context string.
    """
    parts: list[str] = [
        f"# Task: {task.id}",
        f"## Title: {task.title}",
        f"## Description\n{task.description}",
        f"## Priority: {task.priority.value}",
        f"## Tags: {', '.join(task.tags)}",
    ]

    if task.dependencies:
        parts.append(f"## Dependencies: {', '.join(task.dependencies)}")

    if task.scope:
        parts.append("\n## Files in Scope")
        for pattern in task.scope:
            matched = list(repo_path.glob(pattern))
            if matched:
                for match in matched[:10]:
                    try:
                        rel = match.relative_to(repo_path)
                        content = match.read_text(errors="replace")[:2000]
                        parts.append(f"\n### {rel}\n```\n{content}\n```")
                    except (OSError, UnicodeDecodeError):
                        parts.append(f"\n### {pattern} (unreadable)")
            else:
                parts.append(f"- {pattern} (no matches — may need to be created)")

    return "\n".join(parts)


def generate_single_spec(
    task: Task,
    repo_path: Path,
    config: PipelineConfig,
) -> Spec | None:
    """Generate a spec for a single task.

    Args:
        task: The task to generate a spec for.
        repo_path: Repository root path.
        config: Pipeline configuration.

    Returns:
        Spec object if generation succeeds, None on failure.
    """
    context = build_task_context(task, repo_path)

    prompt = f"""\
Generate an implementation specification for this task.

Repository: {repo_path.name}

{context}

The spec will be used by an autonomous Claude agent working in an isolated
git worktree. The agent has full filesystem access within its worktree but
no access to other tasks or the main branch during execution.
"""

    cmd = [
        "claude", "--print",
        "--model", config.model,
        "--system-prompt", SPEC_SYSTEM_PROMPT,
        prompt,
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=config.timeout_seconds,
        )
    except FileNotFoundError:
        logger.error("Claude CLI not found")
        return None
    except subprocess.TimeoutExpired:
        logger.error("Spec generation timed out for task '%s'", task.id)
        return None

    if result.returncode != 0:
        logger.error("Spec generation failed for task '%s': %s", task.id, result.stderr.strip())
        return None

    return parse_spec_response(task.id, result.stdout.strip())


def parse_spec_response(task_id: str, raw_response: str) -> Spec | None:
    """Parse Claude's JSON response into a Spec object.

    Args:
        task_id: The task ID this spec belongs to.
        raw_response: Raw text from Claude.

    Returns:
        Spec object if parsing succeeds, None on failure.
    """
    text = raw_response.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse spec JSON for '%s': %s", task_id, e)
        return None

    if not isinstance(data, dict):
        logger.error("Expected JSON object for spec, got %s", type(data).__name__)
        return None

    try:
        return Spec(
            task_id=data.get("task_id", task_id),
            objective=str(data.get("objective", "")),
            files_in_scope=list(data.get("files_in_scope", [])),
            implementation_steps=list(data.get("implementation_steps", [])),
            acceptance_criteria=list(data.get("acceptance_criteria", [])),
            risk_notes=list(data.get("risk_notes", [])),
            estimated_complexity=str(data.get("estimated_complexity", "medium")),
            branch_name=f"auto/{task_id}",
        )
    except Exception as e:
        logger.error("Failed to create Spec for '%s': %s", task_id, e)
        return None


def filter_specifiable_tasks(
    tasks: list[Task],
    completed_ids: set[str] | None = None,
) -> list[Task]:
    """Filter tasks that are ready for spec generation.

    Excludes tasks that are blocked by dependencies, already specified,
    or have been deferred/merged.

    Args:
        tasks: Full list of ideated tasks.
        completed_ids: Set of task IDs already completed.

    Returns:
        Tasks that are ready for spec generation.
    """
    completed = completed_ids or set()
    ready: list[Task] = []

    for task in tasks:
        if task.status != TaskStatus.IDEATED:
            continue
        if task.is_blocked(completed):
            logger.debug("Task '%s' blocked by dependencies: %s", task.id, task.dependencies)
            continue
        ready.append(task)

    return ready


def generate_specs(
    tasks: list[Task],
    repo_path: Path,
    config: PipelineConfig,
    output_dir: Path | None = None,
) -> list[Spec]:
    """Generate specs for all eligible tasks, optionally in parallel.

    This is the entry point for the spec generation stage of the pipeline.

    Args:
        tasks: List of ideated tasks.
        repo_path: Repository root path.
        config: Pipeline configuration.
        output_dir: Optional directory to write individual spec files.

    Returns:
        List of generated Spec objects.
    """
    repo_path = repo_path.resolve()
    eligible = filter_specifiable_tasks(tasks)

    if not eligible:
        logger.warning("No tasks eligible for spec generation")
        return []

    logger.info(
        "Generating specs for %d/%d tasks (max %d parallel)",
        len(eligible), len(tasks), config.max_parallel_workers,
    )

    specs: list[Spec] = []

    with ThreadPoolExecutor(max_workers=config.max_parallel_workers) as executor:
        future_to_task = {
            executor.submit(generate_single_spec, task, repo_path, config): task
            for task in eligible
        }

        for future in as_completed(future_to_task):
            task = future_to_task[future]
            try:
                spec = future.result()
                if spec:
                    specs.append(spec)
                    task.status = TaskStatus.SPECIFIED
                    logger.info("Spec generated for '%s' (%s)", task.id, spec.estimated_complexity)
                else:
                    task.status = TaskStatus.DEFERRED
                    logger.warning("Spec generation failed for '%s', deferring", task.id)
            except Exception as e:
                task.status = TaskStatus.DEFERRED
                logger.error("Spec generation error for '%s': %s", task.id, e)

    # Write individual spec files
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        for spec in specs:
            spec_path = output_dir / f"{spec.task_id}.json"
            with open(spec_path, "w") as f:
                json.dump(spec.model_dump(mode="json"), f, indent=2, default=str)
            logger.debug("Spec written to %s", spec_path)

    logger.info(
        "Spec generation complete: %d generated, %d deferred",
        len(specs), len(eligible) - len(specs),
    )

    return specs
