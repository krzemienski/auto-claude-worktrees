"""Configuration management for Auto-Claude.

Loads configuration from TOML files with sensible defaults.
Supports project-level overrides via .auto-claude.toml in the repo root.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "default.toml"


@dataclass
class PipelineConfig:
    """Configuration for the Auto-Claude pipeline."""

    # Execution
    max_parallel_workers: int = 4
    model: str = "sonnet"
    qa_model: str = "opus"
    timeout_seconds: int = 600
    max_retries: int = 2

    # Paths
    worktree_base_dir: str = ".worktrees"
    specs_dir: str = "specs"
    output_dir: str = ".auto-claude"

    # QA criteria
    qa_criteria: list[str] = field(default_factory=lambda: [
        "All acceptance criteria from the spec are met",
        "No regressions introduced in existing functionality",
        "Code quality meets project standards (formatting, naming, structure)",
        "No hardcoded values that should be configurable",
        "Error handling is present for failure paths",
    ])

    # Ideation
    ideation_model: str = "opus"
    max_tasks: int = 200
    include_patterns: list[str] = field(default_factory=lambda: ["**/*.py", "**/*.ts", "**/*.js"])
    exclude_patterns: list[str] = field(default_factory=lambda: [
        "node_modules/**", ".git/**", "__pycache__/**", ".venv/**",
    ])

    # Merge strategy
    merge_strategy: str = "priority-weighted"
    auto_resolve_conflicts: bool = False

    @classmethod
    def load(cls, config_path: Path | None = None, repo_path: Path | None = None) -> PipelineConfig:
        """Load configuration from TOML file with defaults.

        Resolution order:
        1. Built-in defaults (this dataclass)
        2. Package default.toml
        3. Project .auto-claude.toml (if repo_path provided)
        4. Explicit config_path (if provided)
        """
        config = cls()

        # Load package defaults
        if DEFAULT_CONFIG_PATH.exists():
            config._merge_from_toml(DEFAULT_CONFIG_PATH)

        # Load project-level overrides
        if repo_path:
            project_config = repo_path / ".auto-claude.toml"
            if project_config.exists():
                config._merge_from_toml(project_config)

        # Load explicit config file
        if config_path and config_path.exists():
            config._merge_from_toml(config_path)

        return config

    def _merge_from_toml(self, path: Path) -> None:
        """Merge settings from a TOML file into this config."""
        with open(path, "rb") as f:
            data = tomllib.load(f)

        pipeline = data.get("pipeline", {})
        qa = data.get("qa", {})
        ideation = data.get("ideation", {})
        merge = data.get("merge", {})

        # Pipeline settings
        if "max_parallel_workers" in pipeline:
            self.max_parallel_workers = int(pipeline["max_parallel_workers"])
        if "model" in pipeline:
            self.model = str(pipeline["model"])
        if "qa_model" in pipeline:
            self.qa_model = str(pipeline["qa_model"])
        if "timeout_seconds" in pipeline:
            self.timeout_seconds = int(pipeline["timeout_seconds"])
        if "max_retries" in pipeline:
            self.max_retries = int(pipeline["max_retries"])
        if "worktree_base_dir" in pipeline:
            self.worktree_base_dir = str(pipeline["worktree_base_dir"])
        if "specs_dir" in pipeline:
            self.specs_dir = str(pipeline["specs_dir"])
        if "output_dir" in pipeline:
            self.output_dir = str(pipeline["output_dir"])

        # QA settings
        if "criteria" in qa:
            self.qa_criteria = list(qa["criteria"])

        # Ideation settings
        if "model" in ideation:
            self.ideation_model = str(ideation["model"])
        if "max_tasks" in ideation:
            self.max_tasks = int(ideation["max_tasks"])
        if "include_patterns" in ideation:
            self.include_patterns = list(ideation["include_patterns"])
        if "exclude_patterns" in ideation:
            self.exclude_patterns = list(ideation["exclude_patterns"])

        # Merge settings
        if "strategy" in merge:
            self.merge_strategy = str(merge["strategy"])
        if "auto_resolve_conflicts" in merge:
            self.auto_resolve_conflicts = bool(merge["auto_resolve_conflicts"])

    def resolve_paths(self, repo_path: Path) -> dict[str, Path]:
        """Resolve all relative paths against the repo root."""
        return {
            "worktree_base": repo_path / self.worktree_base_dir,
            "specs": repo_path / self.specs_dir,
            "output": repo_path / self.output_dir,
        }

    def to_dict(self) -> dict[str, object]:
        """Serialize config to a dictionary for display."""
        return {
            "pipeline": {
                "max_parallel_workers": self.max_parallel_workers,
                "model": self.model,
                "qa_model": self.qa_model,
                "timeout_seconds": self.timeout_seconds,
                "max_retries": self.max_retries,
                "worktree_base_dir": self.worktree_base_dir,
            },
            "qa": {
                "criteria": self.qa_criteria,
            },
            "ideation": {
                "model": self.ideation_model,
                "max_tasks": self.max_tasks,
            },
            "merge": {
                "strategy": self.merge_strategy,
                "auto_resolve_conflicts": self.auto_resolve_conflicts,
            },
        }
