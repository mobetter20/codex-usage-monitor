# Workspace Housekeeping OS

Lean discipline for a multi-project Codex workspace. The goal is not generic tidiness. The goal is to stop a few expensive failures:

- running `git` in the wrong place
- leaving automation-owning repos dirty without noticing
- letting path changes break wrappers, launchd, or automations
- letting experiments and worktrees quietly become durable infrastructure

## Backbone

Keep the current workspace shape:

- non-repo workspace container root
- durable repos under `work/` and `personal/`
- disposable surfaces under `scratch/` and `tmp/`
- a narrow git safeguard at the workspace root

Do not replace that with a separate ops repo or a large framework.

## v1 Design

The first release is intentionally observational:

- `discover`: find durable repos, candidate durable folders, and worktrees
- `audit`: apply repo-specific drift rules and narrow path checks
- `review`: turn the audit into an operator-ready weekly summary

What v1 does **not** do yet:

- no central mutating `run` layer
- no broad git hooks
- no recurring automation until the manual CLI stays high-signal for at least a week

## Sources Of Truth

Use two sources of truth, but keep them separate on purpose:

1. Human source of truth: workspace-root `WORKLOG.md`

- This is the only cross-project human ledger for active work.
- Keep `Active Areas` intentionally short.

2. Machine source of truth: `config/workspace_manifest.json`

- This is not a project database.
- It should contain only:
  - registered durable repos
  - repo-specific severity/ignore policy
  - declared cross-repo contracts
  - path-audit targets for operational files and migration-prone state

If the two disagree, fix the machine manifest only when the repo has become durable or operationally relevant.

## Rules

1. The workspace root is a container, not a project.

- Top level should stay limited to the allowed zones and a few root docs/markers.
- New durable projects do not start at the workspace root.

2. Durable work must live in a repo.

- `work/` is for durable work repos.
- `personal/` is for durable personal repos.
- `scratch/` is for experiments and worktrees.

3. Registration happens at operationalization, not at birth.

- New repos may exist before registration.
- Registration becomes mandatory once a repo gets automation, a cross-repo dependency, a remote workflow you rely on, or persistent place in the root `WORKLOG.md`.
- Unregistered durable repos should warn, not fail.

4. Worktrees are first-class and must stay contained.

- Reserve `scratch/worktrees/` for extra worktrees.
- Do not register worktrees as separate durable projects.
- Treat prunable or out-of-place worktrees as drift.

5. Path changes are migration events.

- Repo code should resolve local paths from repo root whenever possible.
- Operational wrappers, launchd files, and manifests should not carry stale workspace-root paths.
- Path-bearing state files must be declared explicitly per repo so moves can be audited and migrated.

6. Repo noise policy must be explicit.

- Automation-owning repos can fail on tracked changes.
- Personal repos can warn instead.
- Untracked noise like `.stfolder/` should be ignored by repo-specific policy, not by pretending all untracked files are harmless.

## Routine

Start or resume work:

```bash
python3 scripts/hk.py discover --workspace-root ../..
python3 scripts/hk.py audit --workspace-root ../..
```

Before leaving a session:

- commit or intentionally park tracked changes in automation-owning repos
- note repos that are ahead and still need push/review
- if a repo moved, update the manifest and rerun the audit before trusting automation again

Weekly:

```bash
python3 scripts/hk.py review --workspace-root ../..
```

The weekly review should remain manual until the CLI output proves stable and low-noise.

## What Not To Add Yet

- no separate housekeeping repo
- no human-maintained project database
- no central multi-repo mutator
- no daily housekeeping automation
- no broad hook system that makes every repo harder to use

If this stays stable for a few weekly cycles, the next upgrade can be a Codex automation that runs `review` and opens a housekeeping thread with a small number of decisions.
