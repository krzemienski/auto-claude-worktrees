"""Task ideation from codebase analysis.

Analyzes repository structure and content using Claude to generate
a comprehensive task manifest with priorities, scopes, and dependencies.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .config import PipelineConfig
from .models import Task, TaskPriority

logger = logging.getLogger(__name__)

# System prompt for the ideation agent
IDEATION_SYSTEM_PROMPT = """\
You are a senior software architect analyzing a codebase for improvement opportunities.

Your job is to produce a comprehensive task manifest — a JSON array of tasks that would
improve this codebase across all dimensions: architecture, code quality, type safety,
testing, documentation, deployment, accessibility, performance, and security.

Each task must include:
- "id": A short kebab-case identifier (e.g., "modularize-storage", "add-error-handling")
- "title": A human-readable title
- "description": A detailed description of what needs to be done and why
- "scope": A list of file paths or glob patterns affected
- "dependencies": A list of task IDs that must complete before this task
- "priority": One of "critical", "high", "medium", "low"
- "tags": Categorization tags (e.g., ["refactor"], ["feature"], ["security"])

Guidelines:
- Over-generate deliberately — downstream QA will filter
- Foundation work (types, error handling, shared infra) should be "critical" or "high" priority
- Feature work should depend on relevant foundation tasks
- Group related changes into single tasks when they touch the same files
- Keep tasks focused enough for one agent to complete in isolation

Respond with ONLY a JSON array of task objects. No markdown, no commentary."""


def scan_repository(repo_path: Path, config: PipelineConfig) -> str:
    """Scan the repository and build a context string for the ideation agent.

    Collects directory structure, file listing with sizes, and samples of
    key files to give the agent enough context for task generation.

    Args:
        repo_path: Path to the repository root.
        config: Pipeline configuration with include/exclude patterns.

    Returns:
        Formatted string containing the repository context.
    """
    context_parts: list[str] = []

    # Directory tree (depth-limited)
    context_parts.append("## Directory Structure\n")
    try:
        result = subprocess.run(
            ["find", ".", "-type", "f", "-not", "-path", "./.git/*",
             "-not", "-path", "./node_modules/*", "-not", "-path", "./__pycache__/*",
             "-not", "-path", "./.venv/*"],
            capture_output=True, text=True, cwd=repo_path, timeout=30,
        )
        files = sorted(result.stdout.strip().split("\n")) if result.stdout.strip() else []
        context_parts.append(f"Total files: {len(files)}\n")
        for f in files[:500]:  # Cap at 500 files to avoid token explosion
            context_parts.append(f)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        context_parts.append("(directory scan unavailable)")

    # README and key config files
    for key_file in ["README.md", "pyproject.toml", "package.json", "Cargo.toml",
                     "setup.py", "Makefile", "docker-compose.yml", ".github/workflows/ci.yml"]:
        path = repo_path / key_file
        if path.exists():
            content = path.read_text(errors="replace")[:3000]
            context_parts.append(f"\n## {key_file}\n```\n{content}\n```")

    # Sample source files (first 100 lines each)
    source_files = []
    for pattern in config.include_patterns:
        source_files.extend(repo_path.glob(pattern))

    # Filter out excluded patterns
    def is_excluded(p: Path) -> bool:
        rel = str(p.relative_to(repo_path))
        return any(
            rel.startswith(ex.replace("**", "").rstrip("/"))
            for ex in config.exclude_patterns
        )

    source_files = [f for f in source_files if not is_excluded(f)]
    source_files.sort(key=lambda p: p.stat().st_size, reverse=True)

    context_parts.append(f"\n## Source Files ({len(source_files)} total)\n")
    for src_file in source_files[:50]:  # Sample top 50 by size
        try:
            rel_path = src_file.relative_to(repo_path)
            lines = src_file.read_text(errors="replace").split("\n")[:100]
            preview = "\n".join(lines)
            context_parts.append(f"\n### {rel_path} ({len(lines)} lines sampled)\n```\n{preview}\n```")
        except (OSError, UnicodeDecodeError):
            continue

    return "\n".join(context_parts)


def invoke_claude(prompt: str, system: str, model: str) -> str:
    """Invoke Claude CLI in print mode for one-shot generation.

    Args:
        prompt: The user prompt to send.
        system: The system prompt.
        model: Model name (e.g., 'sonnet', 'opus').

    Returns:
        The raw text response from Claude.

    Raises:
        RuntimeError: If Claude CLI invocation fails.
    """
    cmd = [
        "claude", "--print",
        "--model", model,
        "--system-prompt", system,
        prompt,
    ]

    logger.info("Invoking Claude (%s) for ideation...", model)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Claude CLI not found. Install it with: npm install -g @anthropic-ai/claude-code"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Claude CLI timed out after 600 seconds")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"Claude CLI failed (exit {result.returncode}): {stderr}")

    return result.stdout.strip()


def parse_task_list(raw_response: str) -> list[Task]:
    """Parse Claude's JSON response into a list of Task objects.

    Handles common response formatting issues like markdown code fences.

    Args:
        raw_response: Raw text from Claude, expected to be JSON.

    Returns:
        List of validated Task objects.

    Raises:
        ValueError: If the response cannot be parsed as a task list.
    """
    # Strip markdown code fences if present
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
        raise ValueError(f"Failed to parse task list JSON: {e}\nRaw response:\n{text[:500]}")

    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array of tasks, got {type(data).__name__}")

    tasks: list[Task] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            logger.warning("Skipping non-dict task at index %d", i)
            continue

        # Normalize priority
        priority_str = str(item.get("priority", "medium")).lower()
        try:
            priority = TaskPriority(priority_str)
        except ValueError:
            priority = TaskPriority.MEDIUM

        task = Task(
            id=str(item.get("id", f"task-{i}")),
            title=str(item.get("title", item.get("id", f"Task {i}"))),
            description=str(item.get("description", "")),
            scope=list(item.get("scope", [])),
            dependencies=list(item.get("dependencies", [])),
            priority=priority,
            tags=list(item.get("tags", [])),
        )
        tasks.append(task)

    return tasks


def validate_dependencies(tasks: list[Task]) -> list[str]:
    """Validate that all task dependencies reference existing task IDs.

    Args:
        tasks: List of tasks to validate.

    Returns:
        List of warning messages for invalid dependencies.
    """
    task_ids = {t.id for t in tasks}
    warnings: list[str] = []

    for task in tasks:
        for dep in task.dependencies:
            if dep not in task_ids:
                warnings.append(
                    f"Task '{task.id}' depends on '{dep}' which does not exist"
                )

    return warnings


def ideate(
    repo_path: Path,
    config: PipelineConfig,
    output_path: Path | None = None,
) -> list[Task]:
    """Run the ideation phase: analyze codebase and generate task manifest.

    This is the entry point for the ideation stage of the pipeline.
    Scans the repository, invokes Claude for task generation, parses
    and validates the results.

    Args:
        repo_path: Path to the repository root.
        config: Pipeline configuration.
        output_path: Optional path to write the task manifest JSON.

    Returns:
        List of ideated Task objects.

    Raises:
        RuntimeError: If Claude invocation fails.
        ValueError: If response parsing fails.
    """
    repo_path = repo_path.resolve()

    if not (repo_path / ".git").exists():
        raise ValueError(f"Not a git repository: {repo_path}")

    logger.info("Scanning repository at %s", repo_path)
    context = scan_repository(repo_path, config)

    prompt = f"""\
Analyze this codebase and generate a comprehensive task manifest.

Repository: {repo_path.name}

{context}

Generate up to {config.max_tasks} tasks as a JSON array. Focus on:
1. Foundation work (types, error handling, shared infrastructure)
2. Modularization and architecture improvements
3. Code quality (linting, formatting, dead code removal)
4. Test coverage from zero
5. Documentation
6. Deployment and CI/CD
7. Security hardening
8. Performance optimization
9. Accessibility improvements
"""

    raw_response = invoke_claude(prompt, IDEATION_SYSTEM_PROMPT, config.ideation_model)

    tasks = parse_task_list(raw_response)
    logger.info("Ideation produced %d tasks", len(tasks))

    # Validate dependencies
    dep_warnings = validate_dependencies(tasks)
    for warning in dep_warnings:
        logger.warning(warning)

    # Sort by priority
    priority_order = {
        TaskPriority.CRITICAL: 0,
        TaskPriority.HIGH: 1,
        TaskPriority.MEDIUM: 2,
        TaskPriority.LOW: 3,
    }
    tasks.sort(key=lambda t: priority_order[t.priority])

    # Write output if requested
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump([t.model_dump(mode="json") for t in tasks], f, indent=2, default=str)
        logger.info("Task manifest written to %s", output_path)

    return tasks
