# Auto-Claude Worktrees

[![Featured in Agentic Development Blog — Post #6](https://img.shields.io/badge/Agentic%20Development%20Blog-Post%20%236-blue)](https://github.com/krzemienski/agentic-development-guide)

## Related Post

**Featured in the Agentic Development Blog series — Post #6: 194 Parallel AI Worktrees**

- Send date: Thu Jun 4, 2026
- LinkedIn: _link added on send day_
- Canonical blog post: https://ai.hack.ski/blog/<slug-set-on-send-day>
- Series hub: [agentic-development-guide](https://github.com/krzemienski/agentic-development-guide)

---


**Automated parallel AI development using git worktrees.**

A CLI tool that ideates tasks, generates implementation specs, spins up isolated git worktrees with independent Claude agents, and runs automated QA — all in parallel.

Built from the experience of spawning 194 parallel worktrees to build a codebase, generating 91 specs, producing 71 QA reports across 3,066 sessions.

## Pipeline

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐     ┌─────────────┐     ┌───────────┐
│   Ideate    │────▶│  Spec Gen    │────▶│ Worktree Factory │────▶│  QA Review  │────▶│   Merge   │
│             │     │              │     │                  │     │             │     │           │
│ Analyze     │     │ Task → Spec  │     │ git worktree add │     │ Independent │     │ Priority- │
│ codebase,   │     │ with accept  │     │ per spec, spawn  │     │ agent review│     │ weighted  │
│ generate    │     │ criteria &   │     │ Claude agent in  │     │ vs spec     │     │ merge     │
│ task list   │     │ risk notes   │     │ isolation        │     │ criteria    │     │ queue     │
└─────────────┘     └──────────────┘     └─────────────────┘     └─────────────┘     └───────────┘
     194                  91                    71                     71                  ~60
    tasks               specs               worktrees             QA reports            merged
```

## Install

```bash
pip install auto-claude-worktrees
```

Requires [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated.

## Quick Start

```bash
# Run the full pipeline on a repo
auto-claude full --repo ./my-project --workers 4

# Or run stages individually:

# 1. Ideate tasks from codebase analysis
auto-claude ideate --repo ./my-project

# 2. Generate implementation specs
auto-claude spec --repo ./my-project --tasks .auto-claude/tasks.json

# 3. Spin up worktrees and execute specs
auto-claude run --repo ./my-project --specs .auto-claude/specs/ --workers 8

# 4. Run QA review pipeline
auto-claude qa --repo ./my-project

# 5. Merge approved branches
auto-claude merge --repo ./my-project
```

## How It Works

### Stage 1: Ideation

A Claude agent analyzes your entire codebase — directory structure, file contents, architecture patterns — and generates a comprehensive task manifest. Each task includes:

- **Identifier**: Kebab-case name (e.g., `modularize-storage`)
- **Scope boundary**: Which files and modules are affected
- **Dependencies**: Which other tasks must complete first
- **Priority**: Critical, high, medium, or low

Ideation over-generates deliberately. Generating 194 task descriptions costs a fraction of executing one. The QA pipeline downstream filters what shouldn't ship.

### Stage 2: Spec Generation

Raw task descriptions aren't enough for autonomous agents. The spec generator produces detailed implementation blueprints:

- **Objective**: Single-sentence end state
- **Files in scope**: Explicit list of files to touch
- **Implementation steps**: Ordered sequence of changes
- **Acceptance criteria**: Concrete, verifiable conditions
- **Risk notes**: Known pitfalls and edge cases

### Stage 3: Worktree Factory

The core of the system. For each spec:

1. Creates a git worktree (`git worktree add`) from main
2. Checks out a dedicated branch (`auto/{task-id}`)
3. Injects the spec as context
4. Spawns a Claude agent scoped to that worktree
5. Monitors execution for completion, failure, or timeout

Each agent operates in **complete isolation**. No worktree can interfere with another. No merge conflicts during development.

### Stage 4: QA Pipeline

**QA agents are separate from execution agents.** Self-review doesn't work — the same biases that led to buggy code lead to overlooking those bugs in review.

Every completed task enters a review queue. A fresh QA agent examines changes against the spec's acceptance criteria:

- **Approved**: All criteria met, quality acceptable
- **Rejected with fixes**: Specific issues identified, sent back for remediation
- **Rejected permanently**: Fundamental approach flawed, needs re-specification

The ~22% first-pass rejection rate validates the QA pipeline's necessity. The ~95% second-pass approval rate validates the fix cycle's effectiveness.

### Stage 5: Merge Queue

Priority-weighted merge ordering:

1. **Foundation tasks first**: Shared infrastructure before feature work
2. **Conflict detection**: Dry-run merge before committing
3. **Re-execution on conflict**: Tasks re-enter execution with updated main
4. **Small before large**: Focused tasks merge before broad refactors

## Configuration

Create `.auto-claude.toml` in your repo root:

```toml
[pipeline]
max_parallel_workers = 8
model = "sonnet"
qa_model = "opus"
timeout_seconds = 600
max_retries = 2

[qa]
criteria = [
    "All acceptance criteria from the spec are met",
    "No regressions introduced",
    "Code quality meets project standards",
    "Error handling is present for failure paths",
]

[ideation]
model = "opus"
max_tasks = 200

[merge]
strategy = "priority-weighted"
```

## Results

From the Awesome List project:

| Metric | Value |
|--------|-------|
| Tasks ideated | 194 |
| Specs generated | 91 |
| QA reports produced | 71 |
| Git branches created | 90 |
| Total sessions | 3,066 |
| Conversation data | 470 MB |
| QA first-pass rejection rate | ~22% |
| QA second-pass approval rate | ~95% |

## Key Lessons

- **Isolation is non-negotiable.** Git worktrees provide free, total filesystem separation.
- **Specs are the bottleneck, not execution.** A precise spec passes QA on the first attempt.
- **QA agents must be separate from execution agents.** Self-review doesn't catch interaction bugs.
- **Session count correlates with task breadth, not difficulty.** Wide tasks consume more sessions than complex narrow ones.

## Part of the Agentic Development Series

This tool is part of a blog series on building software with AI agents at scale:

1. [Claude iOS Streaming Bridge](https://github.com/krzemienski/claude-ios-streaming-bridge)
2. [Claude SDK Bridge](https://github.com/krzemienski/claude-sdk-bridge)
3. **Auto-Claude Worktrees** (this repo)
4. [Multi-Agent Consensus](https://github.com/krzemienski/multi-agent-consensus)

## Troubleshooting

### `pip install -e .` fails
Ensure you're using Python 3.10+ and the build backend is `setuptools.build_meta` in `pyproject.toml`.

### `auto-claude` command not found after install
Activate your virtual environment, or install with `pip install -e .` in a venv. The entry point is defined in `[project.scripts]`.

### Git worktree creation fails
Ensure you're in a git repository with at least one commit. Worktrees require a valid HEAD reference.

### Claude CLI not found during execution
Install Claude Code CLI globally: `npm install -g @anthropic-ai/claude-code`. The pipeline spawns `claude` as a subprocess.

### QA review returns parsing errors
The QA agent expects pure JSON output. If Claude returns markdown-wrapped JSON, the parser strips ` ```json ` fences automatically. Check that `--print` mode is being used.

## License

MIT
