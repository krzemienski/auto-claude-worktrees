"""QA pipeline for reviewing completed worktree tasks.

A dedicated QA agent (separate from execution agents) reviews each
completed task against its spec's acceptance criteria. Produces one of
three verdicts: Approved, Rejected with fixes, or Rejected permanently.

Key principle: QA agents must be separate from execution agents.
Self-review doesn't work — the same biases that led an agent to write
buggy code lead it to overlook those bugs in review.
"""

from __future__ import annotations

import json
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import PipelineConfig
from .models import QAResult, QAVerdict, TaskStatus, WorktreeState

logger = logging.getLogger(__name__)

QA_SYSTEM_PROMPT = """\
You are an independent QA reviewer for an autonomous development pipeline.

IMPORTANT: You are NOT the agent that wrote this code. You are a separate reviewer
with fresh eyes. Do not assume anything works — verify everything against the spec.

Your job is to review the code changes in this worktree against the original
implementation specification and produce a verdict:

1. "approved" — All acceptance criteria met, code quality acceptable, no regressions
2. "rejected_with_fixes" — Specific issues found, but the approach is sound. Provide
   detailed remediation instructions for the execution agent.
3. "rejected_permanent" — Fundamental approach is flawed. Task needs re-specification.

Respond with a JSON object containing:
- "verdict": One of "approved", "rejected_with_fixes", "rejected_permanent"
- "summary": Brief summary of findings (2-3 sentences)
- "passed_criteria": List of acceptance criteria that passed
- "failed_criteria": List of acceptance criteria that failed
- "issues": List of specific issues found
- "remediation_instructions": List of specific fix instructions (if rejected)

Be thorough. Check:
- Does the code actually do what the spec says?
- Are all acceptance criteria objectively met?
- Are there edge cases the implementation misses?
- Does the code introduce any regressions?
- Is error handling present and correct?
- Are there hardcoded values that should be configurable?

Respond with ONLY the JSON object. No markdown, no commentary."""


def build_qa_context(state: WorktreeState) -> str:
    """Build the review context for a QA agent.

    Includes the original spec, the git diff of changes, and any
    completion notes left by the execution agent.

    Args:
        state: The worktree state to review.

    Returns:
        Formatted context string for the QA prompt.
    """
    parts: list[str] = []

    # Original spec
    parts.append("# Original Specification\n")
    parts.append(state.spec.to_prompt_context())

    # Git diff
    parts.append("\n\n# Code Changes (git diff)\n")
    diff_result = subprocess.run(
        ["git", "diff", "HEAD~..HEAD", "--stat"],
        cwd=state.worktree_path, capture_output=True, text=True,
    )
    if diff_result.returncode == 0 and diff_result.stdout.strip():
        parts.append(f"```\n{diff_result.stdout.strip()}\n```\n")

    # Full diff
    full_diff = subprocess.run(
        ["git", "diff", "HEAD~..HEAD"],
        cwd=state.worktree_path, capture_output=True, text=True,
    )
    if full_diff.returncode == 0 and full_diff.stdout.strip():
        # Cap diff at 10000 chars to avoid token explosion
        diff_text = full_diff.stdout.strip()[:10000]
        parts.append(f"```diff\n{diff_text}\n```")

    # Completion notes
    completion_path = state.worktree_path / ".auto-claude" / "COMPLETION.md"
    if completion_path.exists():
        parts.append("\n\n# Execution Agent's Completion Notes\n")
        parts.append(completion_path.read_text(errors="replace")[:3000])

    # Blockers
    blockers_path = state.worktree_path / "BLOCKERS.md"
    if blockers_path.exists():
        parts.append("\n\n# Reported Blockers\n")
        parts.append(blockers_path.read_text(errors="replace")[:2000])

    return "\n".join(parts)


def review_single_task(
    state: WorktreeState,
    config: PipelineConfig,
    pass_number: int = 1,
) -> QAResult:
    """Run QA review on a single completed worktree task.

    Args:
        state: The worktree state to review.
        config: Pipeline configuration.
        pass_number: Which review pass this is (1 = first, 2 = after fixes, etc.).

    Returns:
        QAResult with the verdict and findings.
    """
    context = build_qa_context(state)

    prompt = f"""\
Review this completed task against its specification.

{context}

QA Criteria to also check:
{chr(10).join(f'- {c}' for c in config.qa_criteria)}

This is review pass #{pass_number}.
{"This task was previously rejected. Check that ALL previous issues are fixed." if pass_number > 1 else ""}
"""

    cmd = [
        "claude", "--print",
        "--model", config.qa_model,
        "--system-prompt", QA_SYSTEM_PROMPT,
        prompt,
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=config.timeout_seconds,
        )
    except FileNotFoundError:
        return QAResult(
            task_id=state.task_id,
            verdict=QAVerdict.REJECTED_WITH_FIXES,
            summary="QA review failed: Claude CLI not found",
            issues=["Claude CLI not installed"],
            review_pass_number=pass_number,
        )
    except subprocess.TimeoutExpired:
        return QAResult(
            task_id=state.task_id,
            verdict=QAVerdict.REJECTED_WITH_FIXES,
            summary=f"QA review timed out after {config.timeout_seconds}s",
            issues=["Review process timed out — may indicate overly complex changes"],
            review_pass_number=pass_number,
        )

    if result.returncode != 0:
        return QAResult(
            task_id=state.task_id,
            verdict=QAVerdict.REJECTED_WITH_FIXES,
            summary=f"QA review process failed: {result.stderr.strip()[:200]}",
            issues=[f"Review process error: {result.stderr.strip()[:200]}"],
            review_pass_number=pass_number,
        )

    return parse_qa_response(state.task_id, result.stdout.strip(), pass_number)


def parse_qa_response(task_id: str, raw_response: str, pass_number: int) -> QAResult:
    """Parse the QA agent's JSON response into a QAResult.

    Args:
        task_id: The task being reviewed.
        raw_response: Raw text from Claude.
        pass_number: Which review pass this is.

    Returns:
        Parsed QAResult.
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
        logger.error("Failed to parse QA response for '%s': %s", task_id, e)
        return QAResult(
            task_id=task_id,
            verdict=QAVerdict.REJECTED_WITH_FIXES,
            summary=f"QA response parsing failed: {e}",
            issues=["QA response was not valid JSON"],
            review_pass_number=pass_number,
        )

    # Map verdict string to enum
    verdict_str = str(data.get("verdict", "rejected_with_fixes")).lower()
    try:
        verdict = QAVerdict(verdict_str)
    except ValueError:
        if "approved" in verdict_str or "pass" in verdict_str:
            verdict = QAVerdict.APPROVED
        elif "permanent" in verdict_str:
            verdict = QAVerdict.REJECTED_PERMANENT
        else:
            verdict = QAVerdict.REJECTED_WITH_FIXES

    return QAResult(
        task_id=task_id,
        verdict=verdict,
        summary=str(data.get("summary", "")),
        passed_criteria=list(data.get("passed_criteria", [])),
        failed_criteria=list(data.get("failed_criteria", [])),
        issues=list(data.get("issues", [])),
        remediation_instructions=list(data.get("remediation_instructions", [])),
        review_pass_number=pass_number,
    )


def send_back_for_fixes(
    state: WorktreeState,
    qa_result: QAResult,
    config: PipelineConfig,
) -> WorktreeState:
    """Send a rejected task back to the execution agent for fixes.

    Re-spawns the Claude agent in the same worktree with the QA
    feedback as additional context.

    Args:
        state: The worktree state to fix.
        qa_result: The QA result with rejection details.
        config: Pipeline configuration.

    Returns:
        Updated WorktreeState after the fix attempt.
    """
    fix_prompt = f"""\
Your previous implementation was reviewed and REJECTED. Fix the following issues:

## QA Summary
{qa_result.summary}

## Failed Criteria
{chr(10).join(f'- {c}' for c in qa_result.failed_criteria)}

## Specific Issues
{chr(10).join(f'- {i}' for i in qa_result.issues)}

## Remediation Instructions
{chr(10).join(f'{n+1}. {inst}' for n, inst in enumerate(qa_result.remediation_instructions))}

Fix ALL issues listed above. Then commit your changes and update COMPLETION.md.
"""

    cmd = [
        "claude", "--print",
        "--model", config.model,
        "--system-prompt", EXECUTION_SYSTEM_PROMPT,
        fix_prompt,
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=state.worktree_path, timeout=config.timeout_seconds,
        )
        state.retry_count += 1
        state.session_count += 1

        if result.returncode == 0:
            state.status = TaskStatus.QA_PENDING
            logger.info("Fix attempt %d complete for '%s'", state.retry_count, state.task_id)
        else:
            state.mark_failed(f"Fix attempt failed: {result.stderr.strip()[:200]}")

    except subprocess.TimeoutExpired:
        state.mark_failed(f"Fix attempt timed out after {config.timeout_seconds}s")
    except FileNotFoundError:
        state.mark_failed("Claude CLI not found")

    return state


def run_qa_pipeline(
    states: list[WorktreeState],
    config: PipelineConfig,
) -> list[QAResult]:
    """Run the full QA pipeline with rejection and resubmission cycles.

    This is the entry point for the QA stage of the pipeline.
    Reviews completed tasks, sends rejections back for fixes,
    and re-reviews until approved or max retries exceeded.

    Args:
        states: List of completed worktree states to review.
        config: Pipeline configuration.

    Returns:
        List of final QAResult objects.
    """
    completed = [s for s in states if s.status == TaskStatus.COMPLETED]

    if not completed:
        logger.warning("No completed tasks to review")
        return []

    logger.info("Starting QA pipeline for %d completed tasks", len(completed))
    all_results: list[QAResult] = []

    with ThreadPoolExecutor(max_workers=config.max_parallel_workers) as executor:
        # First pass review
        future_to_state = {
            executor.submit(review_single_task, state, config, 1): state
            for state in completed
        }

        for future in as_completed(future_to_state):
            state = future_to_state[future]
            try:
                qa_result = future.result()
                all_results.append(qa_result)

                if qa_result.is_approved():
                    state.status = TaskStatus.QA_APPROVED
                    logger.info("Task '%s' APPROVED on first pass", state.task_id)
                elif qa_result.is_permanently_rejected():
                    state.status = TaskStatus.QA_REJECTED_PERMANENT
                    logger.warning("Task '%s' PERMANENTLY REJECTED", state.task_id)
                else:
                    state.status = TaskStatus.QA_REJECTED
                    logger.info(
                        "Task '%s' REJECTED with %d issues",
                        state.task_id, len(qa_result.issues),
                    )
            except Exception as e:
                logger.error("QA review error for '%s': %s", state.task_id, e)

    # Fix-and-retry cycles for rejected tasks
    for retry in range(config.max_retries):
        rejected = [s for s in completed if s.status == TaskStatus.QA_REJECTED]
        if not rejected:
            break

        logger.info("Starting fix cycle %d for %d rejected tasks", retry + 1, len(rejected))

        for state in rejected:
            # Find the most recent rejection
            task_results = [r for r in all_results if r.task_id == state.task_id]
            last_rejection = task_results[-1] if task_results else None

            if last_rejection:
                # Send back for fixes
                state = send_back_for_fixes(state, last_rejection, config)

                if state.status == TaskStatus.QA_PENDING:
                    # Re-review after fixes
                    qa_result = review_single_task(state, config, retry + 2)
                    all_results.append(qa_result)

                    if qa_result.is_approved():
                        state.status = TaskStatus.QA_APPROVED
                        logger.info(
                            "Task '%s' APPROVED on pass %d", state.task_id, retry + 2,
                        )
                    elif qa_result.is_permanently_rejected():
                        state.status = TaskStatus.QA_REJECTED_PERMANENT
                    else:
                        state.status = TaskStatus.QA_REJECTED

    # Summary
    approved = sum(1 for s in completed if s.status == TaskStatus.QA_APPROVED)
    rejected = sum(
        1 for s in completed
        if s.status in (TaskStatus.QA_REJECTED, TaskStatus.QA_REJECTED_PERMANENT)
    )

    logger.info(
        "QA pipeline complete: %d approved, %d rejected, %d total reports",
        approved, rejected, len(all_results),
    )

    return all_results
