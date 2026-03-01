"""Click CLI for Auto-Claude: ideate, spec, run, qa, merge.

Provides the `auto-claude` command with subcommands for each
pipeline stage, plus a `full` command that runs the entire pipeline.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .config import PipelineConfig
from .models import PipelineManifest, Task

console = Console()


def setup_logging(verbose: bool) -> None:
    """Configure logging based on verbosity."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
@click.option("--config", "-c", type=click.Path(exists=True, path_type=Path), help="Config file")
@click.pass_context
def cli(ctx: click.Context, verbose: bool, config: Path | None) -> None:
    """Auto-Claude: Automated parallel AI development using git worktrees.

    A 4-stage pipeline that ideates tasks, generates specs, spins up isolated
    git worktrees with independent Claude agents, and runs automated QA.
    """
    ctx.ensure_object(dict)
    setup_logging(verbose)
    ctx.obj["verbose"] = verbose
    ctx.obj["config_path"] = config


@cli.command()
@click.option("--repo", "-r", type=click.Path(exists=True, path_type=Path),
              default=".", help="Repository path")
@click.option("--output", "-o", type=click.Path(path_type=Path),
              default=None, help="Output file for task manifest")
@click.option("--max-tasks", type=int, default=None, help="Maximum tasks to generate")
@click.pass_context
def ideate(ctx: click.Context, repo: Path, output: Path | None, max_tasks: int | None) -> None:
    """Analyze codebase and generate a comprehensive task manifest.

    Scans the repository structure and content, then uses Claude to
    identify improvement tasks with priorities and dependencies.
    """
    from .ideate import ideate as run_ideate

    config = PipelineConfig.load(ctx.obj.get("config_path"), repo)
    if max_tasks:
        config.max_tasks = max_tasks

    output_path = output or (repo / ".auto-claude" / "tasks.json")

    console.print(f"[bold]Ideating tasks for {repo.resolve().name}...[/bold]")

    try:
        tasks = run_ideate(repo, config, output_path)
    except (RuntimeError, ValueError) as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    # Display results
    table = Table(title=f"Ideated Tasks ({len(tasks)})")
    table.add_column("ID", style="cyan")
    table.add_column("Priority", justify="center")
    table.add_column("Title")
    table.add_column("Deps", justify="right")
    table.add_column("Scope", justify="right")

    for task in tasks:
        priority_color = {
            "critical": "red",
            "high": "yellow",
            "medium": "white",
            "low": "dim",
        }.get(task.priority.value, "white")

        table.add_row(
            task.id,
            f"[{priority_color}]{task.priority.value}[/{priority_color}]",
            task.title[:50],
            str(len(task.dependencies)),
            str(len(task.scope)),
        )

    console.print(table)
    console.print(f"\nManifest written to: {output_path}")


@cli.command()
@click.option("--repo", "-r", type=click.Path(exists=True, path_type=Path),
              default=".", help="Repository path")
@click.option("--tasks", "-t", type=click.Path(exists=True, path_type=Path),
              required=True, help="Path to tasks.json from ideate phase")
@click.option("--output", "-o", type=click.Path(path_type=Path),
              default=None, help="Output directory for spec files")
@click.pass_context
def spec(ctx: click.Context, repo: Path, tasks: Path, output: Path | None) -> None:
    """Generate implementation specs from ideated tasks.

    Takes the task manifest from the ideate phase and produces detailed
    specifications with acceptance criteria for each task.
    """
    from .specgen import generate_specs

    config = PipelineConfig.load(ctx.obj.get("config_path"), repo)
    output_dir = output or (repo / ".auto-claude" / "specs")

    # Load tasks
    with open(tasks) as f:
        task_data = json.load(f)

    task_list = [Task.model_validate(t) for t in task_data]
    console.print(f"[bold]Generating specs for {len(task_list)} tasks...[/bold]")

    specs = generate_specs(task_list, repo, config, output_dir)

    console.print(f"\n[green]Generated {len(specs)} specs[/green]")
    console.print(f"Specs written to: {output_dir}")

    for s in specs:
        console.print(f"  - {s.task_id}: {s.objective[:60]}")


@cli.command()
@click.option("--repo", "-r", type=click.Path(exists=True, path_type=Path),
              default=".", help="Repository path")
@click.option("--specs", "-s", type=click.Path(exists=True, path_type=Path),
              required=True, help="Directory containing spec JSON files")
@click.option("--workers", "-w", type=int, default=None,
              help="Max parallel workers (overrides config)")
@click.pass_context
def run(ctx: click.Context, repo: Path, specs: Path, workers: int | None) -> None:
    """Spin up worktrees and execute specs with Claude agents.

    Creates an isolated git worktree for each spec, injects the spec
    as context, and spawns a Claude agent to execute it.
    """
    from .factory import run_factory
    from .models import Spec

    config = PipelineConfig.load(ctx.obj.get("config_path"), repo)
    if workers:
        config.max_parallel_workers = workers

    # Load specs
    spec_files = sorted(specs.glob("*.json"))
    if not spec_files:
        console.print(f"[red]No spec files found in {specs}[/red]")
        sys.exit(1)

    spec_list: list[Spec] = []
    for sf in spec_files:
        with open(sf) as f:
            spec_list.append(Spec.model_validate(json.load(f)))

    console.print(f"[bold]Executing {len(spec_list)} specs "
                  f"({config.max_parallel_workers} parallel workers)...[/bold]")

    states = run_factory(spec_list, repo, config)

    # Summary
    completed = sum(1 for s in states if s.status.value == "completed")
    failed = sum(1 for s in states if s.status.value == "failed")
    console.print(f"\n[green]Completed: {completed}[/green] | [red]Failed: {failed}[/red]")

    # Write states
    state_path = repo / ".auto-claude" / "worktree-states.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w") as f:
        json.dump([s.model_dump(mode="json") for s in states], f, indent=2, default=str)
    console.print(f"States written to: {state_path}")


@cli.command()
@click.option("--repo", "-r", type=click.Path(exists=True, path_type=Path),
              default=".", help="Repository path")
@click.option("--worktrees", "-w", type=click.Path(exists=True, path_type=Path),
              default=None, help="Path to worktree-states.json")
@click.pass_context
def qa(ctx: click.Context, repo: Path, worktrees: Path | None) -> None:
    """Run QA review on completed worktree tasks.

    A dedicated QA agent (separate from execution agents) reviews
    each completed task against its spec's acceptance criteria.
    """
    from .models import WorktreeState
    from .qa import run_qa_pipeline

    config = PipelineConfig.load(ctx.obj.get("config_path"), repo)

    states_path = worktrees or (repo / ".auto-claude" / "worktree-states.json")
    with open(states_path) as f:
        state_data = json.load(f)

    states = [WorktreeState.model_validate(s) for s in state_data]
    console.print(f"[bold]Running QA on {len(states)} tasks...[/bold]")

    results = run_qa_pipeline(states, config)

    # Summary
    approved = sum(1 for r in results if r.is_approved())
    rejected = sum(1 for r in results if not r.is_approved())

    table = Table(title="QA Results")
    table.add_column("Task", style="cyan")
    table.add_column("Pass #", justify="center")
    table.add_column("Verdict", justify="center")
    table.add_column("Issues", justify="right")

    for r in results:
        verdict_style = "green" if r.is_approved() else "red"
        table.add_row(
            r.task_id,
            str(r.review_pass_number),
            f"[{verdict_style}]{r.verdict.value}[/{verdict_style}]",
            str(len(r.issues)),
        )

    console.print(table)
    console.print(f"\n[green]Approved: {approved}[/green] | [red]Rejected: {rejected}[/red]")

    # Write results
    results_path = repo / ".auto-claude" / "qa-results.json"
    with open(results_path, "w") as f:
        json.dump([r.model_dump(mode="json") for r in results], f, indent=2, default=str)


@cli.command()
@click.option("--repo", "-r", type=click.Path(exists=True, path_type=Path),
              default=".", help="Repository path")
@click.option("--approved", "-a", type=click.Path(exists=True, path_type=Path),
              default=None, help="Path to qa-results.json")
@click.pass_context
def merge(ctx: click.Context, repo: Path, approved: Path | None) -> None:
    """Merge approved branches into main using priority-weighted queue.

    Merges in dependency and priority order, detecting conflicts and
    flagging tasks that need re-execution against the updated main.
    """
    from .merge import run_merge_queue
    from .models import QAResult, WorktreeState

    config = PipelineConfig.load(ctx.obj.get("config_path"), repo)

    # Load tasks, states, and QA results
    tasks_path = repo / ".auto-claude" / "tasks.json"
    states_path = repo / ".auto-claude" / "worktree-states.json"
    qa_path = approved or (repo / ".auto-claude" / "qa-results.json")

    for required in [tasks_path, states_path, qa_path]:
        if not required.exists():
            console.print(f"[red]Required file not found: {required}[/red]")
            sys.exit(1)

    with open(tasks_path) as f:
        task_list = [Task.model_validate(t) for t in json.load(f)]
    with open(states_path) as f:
        states = [WorktreeState.model_validate(s) for s in json.load(f)]
    with open(qa_path) as f:
        qa_results = [QAResult.model_validate(r) for r in json.load(f)]

    console.print(f"[bold]Merging approved branches...[/bold]")

    results = run_merge_queue(task_list, states, qa_results, repo)

    merged = sum(1 for r in results if r.success)
    conflicts = sum(1 for r in results if not r.success)

    console.print(f"\n[green]Merged: {merged}[/green] | [yellow]Conflicts: {conflicts}[/yellow]")

    for r in results:
        icon = "✅" if r.success else "❌"
        console.print(f"  {icon} {r.task_id} ({r.branch_name})")
        if not r.success and r.conflict_files:
            for cf in r.conflict_files[:3]:
                console.print(f"      Conflict: {cf}")


@cli.command()
@click.option("--repo", "-r", type=click.Path(exists=True, path_type=Path),
              default=".", help="Repository path")
@click.option("--workers", "-w", type=int, default=None, help="Max parallel workers")
@click.pass_context
def full(ctx: click.Context, repo: Path, workers: int | None) -> None:
    """Run the complete pipeline: ideate -> spec -> run -> qa -> merge.

    Executes all four stages sequentially, with each stage feeding
    its output to the next.
    """
    from .factory import run_factory
    from .ideate import ideate as run_ideate
    from .merge import run_merge_queue
    from .qa import run_qa_pipeline
    from .specgen import generate_specs

    config = PipelineConfig.load(ctx.obj.get("config_path"), repo)
    if workers:
        config.max_parallel_workers = workers

    manifest = PipelineManifest(repo_path=str(repo.resolve()))

    # Stage 1: Ideate
    console.print("\n[bold cyan]Stage 1: Ideation[/bold cyan]")
    tasks = run_ideate(repo, config)
    manifest.tasks = tasks
    console.print(f"  Ideated {len(tasks)} tasks")

    # Stage 2: Spec Generation
    console.print("\n[bold cyan]Stage 2: Spec Generation[/bold cyan]")
    specs = generate_specs(tasks, repo, config)
    manifest.specs = specs
    console.print(f"  Generated {len(specs)} specs")

    # Stage 3: Worktree Execution
    console.print("\n[bold cyan]Stage 3: Worktree Execution[/bold cyan]")
    states = run_factory(specs, repo, config)
    manifest.worktrees = states
    completed = sum(1 for s in states if s.status.value == "completed")
    console.print(f"  Executed: {completed} completed, {len(states) - completed} failed")

    # Stage 4: QA
    console.print("\n[bold cyan]Stage 4: QA Pipeline[/bold cyan]")
    qa_results = run_qa_pipeline(states, config)
    manifest.qa_results = qa_results
    approved = sum(1 for r in qa_results if r.is_approved())
    console.print(f"  QA: {approved} approved, {len(qa_results) - approved} rejected")

    # Stage 5: Merge
    console.print("\n[bold cyan]Stage 5: Merge Queue[/bold cyan]")
    merge_results = run_merge_queue(tasks, states, qa_results, repo)
    manifest.merge_results = merge_results
    merged = sum(1 for r in merge_results if r.success)
    console.print(f"  Merged: {merged}, Conflicts: {len(merge_results) - merged}")

    # Final summary
    console.print("\n" + "=" * 50)
    console.print("[bold]Pipeline Complete[/bold]")
    for key, value in manifest.summary_table().items():
        console.print(f"  {key}: {value}")


if __name__ == "__main__":
    cli()
