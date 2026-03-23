#!/usr/bin/env python3
"""Codex workspace housekeeping CLI."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SCRIPT_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_ROOT.parent
DEFAULT_MANIFEST_PATH = REPO_ROOT / "config" / "workspace_manifest.json"
SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    ".venv-personal-calendar",
    "__pycache__",
    "node_modules",
}
MAX_SAMPLE_PATHS = 5
MAX_REPORTED_PATH_REFS = 12
MAX_PATH_LOCATIONS = 3
MAX_TEXT_FILE_SIZE = 5 * 1024 * 1024
VALID_POLICIES = {"ignore", "info", "warn", "fail"}


@dataclass
class RepoConfig:
    id: str
    root: str
    repo_class: str
    checks: dict[str, str] = field(default_factory=dict)
    ignore_untracked: list[str] = field(default_factory=list)
    operational_path_files: list[str] = field(default_factory=list)
    migratable_path_files: list[str] = field(default_factory=list)


@dataclass
class ContractConfig:
    id: str
    source_repo_id: str
    target_repo_ids: list[str]
    notes: str = ""


@dataclass
class Manifest:
    schema_version: str
    workspace_root_marker: str
    durable_zones: list[str]
    allowed_top_level: list[str]
    worktree_roots: list[str]
    repo_defaults: dict[str, Any]
    workspace_path_audit: dict[str, list[str]]
    repos: list[RepoConfig]
    contracts: list[ContractConfig]

    @property
    def repo_map(self) -> dict[str, RepoConfig]:
        return {repo.id: repo for repo in self.repos}

    def repo_by_root(self) -> dict[str, RepoConfig]:
        return {repo.root: repo for repo in self.repos}


@dataclass
class RepoDiscovery:
    rel_path: str
    path: Path
    git_marker_kind: str
    registered_id: str | None
    in_durable_zone: bool


@dataclass
class RepoStatus:
    rel_path: str
    branch: str
    ahead: int = 0
    behind: int = 0
    tracked_paths: list[str] = field(default_factory=list)
    untracked_paths: list[str] = field(default_factory=list)
    ignored_untracked_paths: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class WorktreeRecord:
    common_dir: str
    path: Path
    rel_path: str | None
    branch: str | None
    detached: bool = False
    locked: bool = False
    prunable: bool = False
    bare: bool = False


@dataclass
class Finding:
    severity: str
    bucket: str
    code: str
    summary: str
    details: str | None = None
    path: str | None = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex workspace housekeeping CLI.")
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST_PATH),
        help="Path to the housekeeping manifest JSON.",
    )
    parser.add_argument(
        "--workspace-root",
        default="../..",
        help="Workspace container root relative to the current directory.",
    )
    common_parent = argparse.ArgumentParser(add_help=False)
    common_parent.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH))
    common_parent.add_argument("--workspace-root", default="../..")

    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser(
        "discover",
        parents=[common_parent],
        help="Discover durable repos, candidates, and worktrees.",
    )
    discover_parser.add_argument("--json", action="store_true", help="Emit structured JSON.")

    audit_parser = subparsers.add_parser(
        "audit",
        parents=[common_parent],
        help="Audit housekeeping drift.",
    )
    audit_parser.add_argument("--json", action="store_true", help="Emit structured JSON.")

    review_parser = subparsers.add_parser(
        "review",
        parents=[common_parent],
        help="Summarize the audit into operator-ready buckets.",
    )
    review_parser.add_argument("--json", action="store_true", help="Emit structured JSON.")

    return parser.parse_args(argv)


def load_manifest(path: Path) -> Manifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    repos = [
        RepoConfig(
            id=item["id"],
            root=item["root"],
            repo_class=item["class"],
            checks=item.get("checks", {}),
            ignore_untracked=item.get("ignore_untracked", []),
            operational_path_files=item.get("operational_path_files", []),
            migratable_path_files=item.get("migratable_path_files", []),
        )
        for item in raw.get("repos", [])
    ]
    contracts = [
        ContractConfig(
            id=item["id"],
            source_repo_id=item["source_repo_id"],
            target_repo_ids=item.get("target_repo_ids", []),
            notes=item.get("notes", ""),
        )
        for item in raw.get("contracts", [])
    ]
    return Manifest(
        schema_version=raw["schema_version"],
        workspace_root_marker=raw["workspace_root_marker"],
        durable_zones=raw.get("durable_zones", []),
        allowed_top_level=raw.get("allowed_top_level", []),
        worktree_roots=raw.get("worktree_roots", []),
        repo_defaults=raw.get("repo_defaults", {}),
        workspace_path_audit=raw.get("workspace_path_audit", {}),
        repos=repos,
        contracts=contracts,
    )


def run_command(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True)


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def posix_rel(path: Path, root: Path) -> str:
    return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()


def discover_repo_checkouts(workspace_root: Path, manifest: Manifest) -> tuple[list[RepoDiscovery], list[str]]:
    repo_root_map = manifest.repo_by_root()
    discovered: dict[Path, RepoDiscovery] = {}
    candidates: set[str] = set()

    for zone in manifest.durable_zones:
        zone_root = workspace_root / zone
        if not zone_root.is_dir():
            continue

        for child in sorted(zone_root.iterdir(), key=lambda item: item.name):
            if not child.is_dir():
                continue
            if child.name in SKIP_DIR_NAMES:
                continue
            if not (child / ".git").exists():
                candidates.add(posix_rel(child, workspace_root))

        for current_root, dirnames, filenames in os.walk(zone_root, topdown=True):
            root_path = Path(current_root)
            marker_kind: str | None = None
            if ".git" in dirnames:
                marker_kind = "dir"
            elif ".git" in filenames:
                marker_kind = "file"
            dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
            if not marker_kind:
                continue
            rel_path = posix_rel(root_path, workspace_root)
            discovered[root_path.resolve()] = RepoDiscovery(
                rel_path=rel_path,
                path=root_path.resolve(),
                git_marker_kind=marker_kind,
                registered_id=repo_root_map.get(rel_path).id if rel_path in repo_root_map else None,
                in_durable_zone=True,
            )
    return sorted(discovered.values(), key=lambda item: item.rel_path), sorted(candidates)


def parse_branch_line(line: str) -> tuple[str, int, int]:
    branch = line[3:]
    ahead = 0
    behind = 0
    if "..." in branch:
        branch = branch.split("...", 1)[0]
    match = re.search(r"\[(.*?)\]$", line)
    if match:
        for chunk in match.group(1).split(","):
            item = chunk.strip()
            if item.startswith("ahead "):
                ahead = int(item.split()[1])
            elif item.startswith("behind "):
                behind = int(item.split()[1])
    return branch, ahead, behind


def extract_status_path(line: str) -> str:
    body = line[3:]
    if " -> " in body:
        body = body.split(" -> ", 1)[1]
    return body.strip().strip('"')


def matches_any_glob(path_text: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path_text, pattern) for pattern in patterns)


def repo_status(repo_path: Path, rel_path: str, ignore_untracked: list[str]) -> RepoStatus:
    result = run_command(["git", "-C", str(repo_path), "status", "--porcelain", "--branch"])
    status = RepoStatus(rel_path=rel_path, branch="HEAD")
    if result.returncode != 0:
        status.error = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        return status

    for raw_line in result.stdout.splitlines():
        if raw_line.startswith("## "):
            branch, ahead, behind = parse_branch_line(raw_line)
            status.branch = branch
            status.ahead = ahead
            status.behind = behind
            continue
        if not raw_line:
            continue
        path_text = extract_status_path(raw_line)
        if raw_line.startswith("?? "):
            if matches_any_glob(path_text, ignore_untracked):
                status.ignored_untracked_paths.append(path_text)
            else:
                status.untracked_paths.append(path_text)
            continue
        status.tracked_paths.append(path_text)
    return status


def git_common_dir(repo_path: Path) -> Path | None:
    result = run_command(["git", "-C", str(repo_path), "rev-parse", "--git-common-dir"])
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    common_dir = Path(raw)
    if common_dir.is_absolute():
        return common_dir.resolve(strict=False)
    return (repo_path / common_dir).resolve(strict=False)


def parse_worktree_list(output: str, workspace_root: Path, common_dir: Path) -> list[WorktreeRecord]:
    records: list[WorktreeRecord] = []
    current: dict[str, Any] = {}
    for raw_line in output.splitlines() + [""]:
        line = raw_line.strip()
        if not line:
            if "path" in current:
                worktree_path = Path(current["path"]).expanduser().resolve(strict=False)
                rel_path = posix_rel(worktree_path, workspace_root) if within(worktree_path, workspace_root) else None
                branch = current.get("branch")
                if branch and branch.startswith("refs/heads/"):
                    branch = branch.removeprefix("refs/heads/")
                records.append(
                    WorktreeRecord(
                        common_dir=str(common_dir),
                        path=worktree_path,
                        rel_path=rel_path,
                        branch=branch,
                        detached=current.get("detached", False),
                        locked=current.get("locked", False),
                        prunable=current.get("prunable", False),
                        bare=current.get("bare", False),
                    )
                )
            current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value
        elif key == "branch":
            current["branch"] = value
        elif key == "detached":
            current["detached"] = True
        elif key == "locked":
            current["locked"] = True
        elif key == "prunable":
            current["prunable"] = True
        elif key == "bare":
            current["bare"] = True
    return records


def discover_worktrees(workspace_root: Path, repos: list[RepoDiscovery]) -> list[WorktreeRecord]:
    worktrees: list[WorktreeRecord] = []
    seen_common_dirs: set[Path] = set()
    for repo in repos:
        common_dir = git_common_dir(repo.path)
        if not common_dir or common_dir in seen_common_dirs:
            continue
        seen_common_dirs.add(common_dir)
        result = run_command(["git", "-C", str(repo.path), "worktree", "list", "--porcelain"])
        if result.returncode != 0:
            continue
        worktrees.extend(parse_worktree_list(result.stdout, workspace_root, common_dir))
    return sorted(worktrees, key=lambda item: str(item.path))


def clean_path_candidate(raw_value: str) -> str:
    value = raw_value
    for escaped_break in ("\\n", "\\r", "\\t"):
        value = value.split(escaped_break, 1)[0]
    value = value.rstrip("`'\",.;:)]}>|")
    if value.endswith("/"):
        value = value[:-1]
    return value


def scan_file_for_workspace_paths(
    file_path: Path,
    workspace_root: Path,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    broken: dict[str, list[str]] = {}
    existing: dict[str, list[str]] = {}
    try:
        if file_path.stat().st_size > MAX_TEXT_FILE_SIZE:
            return broken, existing
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return broken, existing

    root_text = str(workspace_root.resolve(strict=False))
    if root_text not in text:
        return broken, existing

    pattern = re.compile(re.escape(root_text) + r"[^\s`'\"<>()\[\]{}|]*")
    for match in pattern.finditer(text):
        candidate = clean_path_candidate(match.group(0))
        if candidate == root_text:
            continue
        line = text.count("\n", 0, match.start()) + 1
        target_map = existing if Path(candidate).exists() else broken
        target_map.setdefault(candidate, [])
        if len(target_map[candidate]) < MAX_PATH_LOCATIONS:
            target_map[candidate].append(f"{file_path}:{line}")
    return broken, existing


def resolve_scan_targets(base_root: Path, patterns: list[str]) -> tuple[list[Path], list[str]]:
    targets: list[Path] = []
    missing: list[str] = []
    for pattern in patterns:
        if any(char in pattern for char in "*?[]"):
            matches = sorted(base_root.glob(pattern))
            targets.extend(match for match in matches if match.is_file())
            continue
        target = base_root / pattern
        if target.exists() and target.is_file():
            targets.append(target)
        else:
            missing.append(pattern)
    return targets, missing


def merge_path_maps(current: dict[str, list[str]], incoming: dict[str, list[str]]) -> dict[str, list[str]]:
    for candidate, locations in incoming.items():
        current.setdefault(candidate, [])
        for location in locations:
            if len(current[candidate]) >= MAX_PATH_LOCATIONS:
                break
            if location not in current[candidate]:
                current[candidate].append(location)
    return current


def apply_policy(policy: str, count: int) -> str | None:
    if count <= 0 or policy == "ignore":
        return None
    if policy not in VALID_POLICIES:
        raise ValueError(f"Unsupported policy: {policy}")
    return "INFO" if policy == "info" else policy.upper()


def finding_from_policy(
    rel_path: str,
    check_name: str,
    count: int,
    sample_paths: list[str],
    policy: str,
) -> Finding | None:
    severity = apply_policy(policy, count)
    if not severity:
        return None
    bucket = "ignore_for_now" if severity == "INFO" else "decide_this_week"
    if severity == "FAIL":
        bucket = "fix_now"
    details = ", ".join(sample_paths[:MAX_SAMPLE_PATHS]) if sample_paths else None
    return Finding(
        severity=severity,
        bucket=bucket,
        code=f"repo_{check_name}",
        summary=f"{rel_path}: {count} {check_name.replace('_', ' ')}",
        details=details,
        path=rel_path,
    )


def repo_policies(manifest: Manifest, repo_config: RepoConfig) -> tuple[dict[str, str], list[str]]:
    checks = dict(manifest.repo_defaults.get("checks", {}))
    checks.update(repo_config.checks)
    ignore_untracked = list(manifest.repo_defaults.get("ignore_untracked", []))
    ignore_untracked.extend(repo_config.ignore_untracked)
    return checks, ignore_untracked


def severity_rank(finding: Finding) -> tuple[int, str, str]:
    rank = {"FAIL": 0, "WARN": 1, "INFO": 2}.get(finding.severity, 3)
    return (rank, finding.code, finding.summary)


def build_audit(workspace_root: Path, manifest: Manifest) -> dict[str, Any]:
    findings: list[Finding] = []
    discovered_repos, candidates = discover_repo_checkouts(workspace_root, manifest)
    discovered_repo_map = {repo.rel_path: repo for repo in discovered_repos}
    repo_statuses: list[RepoStatus] = []
    worktrees = discover_worktrees(workspace_root, discovered_repos)

    marker_path = workspace_root / manifest.workspace_root_marker
    if not marker_path.exists():
        findings.append(
            Finding(
                severity="FAIL",
                bucket="fix_now",
                code="workspace_marker_missing",
                summary=f"Workspace marker missing: {manifest.workspace_root_marker}",
                path=str(marker_path),
            )
        )

    unexpected_top_level = [
        entry.name
        for entry in sorted(workspace_root.iterdir(), key=lambda item: item.name)
        if entry.name not in set(manifest.allowed_top_level)
    ]
    if unexpected_top_level:
        findings.append(
            Finding(
                severity="WARN",
                bucket="decide_this_week",
                code="unexpected_top_level",
                summary="Unexpected top-level items present",
                details=", ".join(unexpected_top_level),
            )
        )

    for repo_config in manifest.repos:
        repo_path = workspace_root / repo_config.root
        if not repo_path.exists():
            findings.append(
                Finding(
                    severity="FAIL",
                    bucket="fix_now",
                    code="registered_repo_missing",
                    summary=f"Registered repo missing on disk: {repo_config.root}",
                    path=repo_config.root,
                )
            )

    for repo in discovered_repos:
        if not repo.registered_id:
            summary = f"Unregistered durable repo discovered: {repo.rel_path}"
            if repo.git_marker_kind == "file":
                summary = f"Worktree checkout lives in a durable zone: {repo.rel_path}"
            findings.append(
                Finding(
                    severity="WARN",
                    bucket="decide_this_week",
                    code="unregistered_repo",
                    summary=summary,
                    path=repo.rel_path,
                )
            )

    for candidate in candidates:
        if candidate not in discovered_repo_map:
            findings.append(
                Finding(
                    severity="WARN",
                    bucket="decide_this_week",
                    code="durable_folder_without_repo",
                    summary=f"Durable-zone folder has no repo yet: {candidate}",
                    path=candidate,
                )
            )

    for repo in discovered_repos:
        repo_config = manifest.repo_by_root().get(repo.rel_path)
        checks = dict(manifest.repo_defaults.get("checks", {}))
        ignore_untracked = list(manifest.repo_defaults.get("ignore_untracked", []))
        if repo_config:
            checks, ignore_untracked = repo_policies(manifest, repo_config)
        status = repo_status(repo.path, repo.rel_path, ignore_untracked)
        repo_statuses.append(status)
        if status.error:
            findings.append(
                Finding(
                    severity="FAIL",
                    bucket="fix_now",
                    code="repo_status_error",
                    summary=f"{repo.rel_path}: unable to read git status",
                    details=status.error,
                    path=repo.rel_path,
                )
            )
            continue
        for check_name, count, sample_paths in (
            ("tracked_changes", len(status.tracked_paths), status.tracked_paths),
            ("ahead", status.ahead, []),
            ("behind", status.behind, []),
            ("untracked", len(status.untracked_paths), status.untracked_paths),
        ):
            finding = finding_from_policy(
                repo.rel_path,
                check_name,
                count,
                sample_paths,
                checks.get(check_name, "warn"),
            )
            if finding:
                findings.append(finding)

    declared_worktree_roots = [(workspace_root / root).resolve(strict=False) for root in manifest.worktree_roots]
    for worktree in worktrees:
        if worktree.bare:
            continue
        is_primary_repo = worktree.rel_path in discovered_repo_map and discovered_repo_map[worktree.rel_path].git_marker_kind == "dir"
        if is_primary_repo:
            continue
        if worktree.prunable:
            findings.append(
                Finding(
                    severity="WARN",
                    bucket="decide_this_week",
                    code="worktree_prunable",
                    summary=f"Prunable worktree detected: {worktree.path}",
                    path=worktree.rel_path or str(worktree.path),
                )
            )
            continue
        if any(within(worktree.path, root) for root in declared_worktree_roots):
            findings.append(
                Finding(
                    severity="INFO",
                    bucket="ignore_for_now",
                    code="managed_worktree",
                    summary=f"Managed worktree present: {worktree.path}",
                    path=worktree.rel_path or str(worktree.path),
                )
            )
            continue
        if worktree.rel_path and worktree.rel_path.split("/", 1)[0] in manifest.durable_zones:
            findings.append(
                Finding(
                    severity="WARN",
                    bucket="decide_this_week",
                    code="worktree_in_durable_zone",
                    summary=f"Worktree checkout should not live in a durable zone: {worktree.rel_path}",
                    path=worktree.rel_path,
                )
            )
            continue
        findings.append(
            Finding(
                severity="WARN",
                bucket="decide_this_week",
                code="worktree_unexpected_location",
                summary=f"Worktree lives outside managed roots: {worktree.path}",
                path=worktree.rel_path or str(worktree.path),
            )
        )

    for contract in manifest.contracts:
        if contract.source_repo_id not in manifest.repo_map:
            findings.append(
                Finding(
                    severity="FAIL",
                    bucket="fix_now",
                    code="contract_source_missing",
                    summary=f"Contract {contract.id} references missing source repo id {contract.source_repo_id}",
                )
            )
        for target_repo_id in contract.target_repo_ids:
            if target_repo_id not in manifest.repo_map:
                findings.append(
                    Finding(
                        severity="FAIL",
                        bucket="fix_now",
                        code="contract_target_missing",
                        summary=f"Contract {contract.id} references missing target repo id {target_repo_id}",
                    )
                )

    broken_fail_refs: dict[str, list[str]] = {}
    broken_warn_refs: dict[str, list[str]] = {}
    existing_warn_refs: dict[str, list[str]] = {}
    missing_scan_targets: list[str] = []

    def absorb_path_refs(targets: list[Path], severity: str) -> None:
        nonlocal broken_fail_refs, broken_warn_refs, existing_warn_refs
        for file_path in targets:
            broken, existing = scan_file_for_workspace_paths(file_path, workspace_root)
            if severity == "fail":
                broken_fail_refs = merge_path_maps(broken_fail_refs, broken)
            else:
                broken_warn_refs = merge_path_maps(broken_warn_refs, broken)
            existing_warn_refs = merge_path_maps(existing_warn_refs, existing)

    workspace_fail_targets, missing = resolve_scan_targets(workspace_root, manifest.workspace_path_audit.get("fail", []))
    absorb_path_refs(workspace_fail_targets, "fail")
    missing_scan_targets.extend(f"workspace:{item}" for item in missing)

    workspace_warn_targets, missing = resolve_scan_targets(workspace_root, manifest.workspace_path_audit.get("warn", []))
    absorb_path_refs(workspace_warn_targets, "warn")
    missing_scan_targets.extend(f"workspace:{item}" for item in missing)

    for repo_config in manifest.repos:
        repo_root = workspace_root / repo_config.root
        if not repo_root.exists():
            continue
        fail_targets, missing = resolve_scan_targets(repo_root, repo_config.operational_path_files)
        absorb_path_refs(fail_targets, "fail")
        missing_scan_targets.extend(f"{repo_config.root}:{item}" for item in missing)

        warn_targets, missing = resolve_scan_targets(repo_root, repo_config.migratable_path_files)
        absorb_path_refs(warn_targets, "warn")
        missing_scan_targets.extend(f"{repo_config.root}:{item}" for item in missing)

    for missing_target in missing_scan_targets:
        findings.append(
            Finding(
                severity="WARN",
                bucket="decide_this_week",
                code="declared_scan_target_missing",
                summary=f"Declared path-audit target missing: {missing_target}",
            )
        )

    for candidate, locations in sorted(broken_fail_refs.items())[:MAX_REPORTED_PATH_REFS]:
        findings.append(
            Finding(
                severity="FAIL",
                bucket="fix_now",
                code="broken_operational_path_ref",
                summary=f"Broken workspace-root path in operational files: {candidate}",
                details=", ".join(locations),
            )
        )

    for candidate, locations in sorted(broken_warn_refs.items())[:MAX_REPORTED_PATH_REFS]:
        findings.append(
            Finding(
                severity="WARN",
                bucket="decide_this_week",
                code="broken_migratable_path_ref",
                summary=f"Broken workspace-root path in docs/state: {candidate}",
                details=", ".join(locations),
            )
        )

    for candidate, locations in sorted(existing_warn_refs.items())[:MAX_REPORTED_PATH_REFS]:
        findings.append(
            Finding(
                severity="INFO",
                bucket="ignore_for_now",
                code="existing_absolute_path_ref",
                summary=f"Existing absolute workspace-root path remains encoded: {candidate}",
                details=", ".join(locations),
            )
        )

    findings.sort(key=severity_rank)
    result = "FAIL" if any(item.severity == "FAIL" for item in findings) else "OK"
    return {
        "workspace_root": str(workspace_root),
        "manifest_path": str(DEFAULT_MANIFEST_PATH),
        "result": result,
        "findings": [finding.__dict__ for finding in findings],
        "discovered_repos": [
            {
                "rel_path": repo.rel_path,
                "git_marker_kind": repo.git_marker_kind,
                "registered_id": repo.registered_id,
            }
            for repo in discovered_repos
        ],
        "candidate_folders": candidates,
        "repo_statuses": [
            {
                "rel_path": status.rel_path,
                "branch": status.branch,
                "ahead": status.ahead,
                "behind": status.behind,
                "tracked_count": len(status.tracked_paths),
                "tracked_paths": status.tracked_paths[:MAX_SAMPLE_PATHS],
                "untracked_count": len(status.untracked_paths),
                "untracked_paths": status.untracked_paths[:MAX_SAMPLE_PATHS],
                "ignored_untracked_count": len(status.ignored_untracked_paths),
                "error": status.error,
            }
            for status in repo_statuses
        ],
        "worktrees": [
            {
                "path": str(item.path),
                "rel_path": item.rel_path,
                "branch": item.branch,
                "detached": item.detached,
                "locked": item.locked,
                "prunable": item.prunable,
            }
            for item in worktrees
        ],
    }


def render_discover(data: dict[str, Any]) -> str:
    lines = [f"Workspace: {data['workspace_root']}", "", "Registered repos discovered:"]
    registered = [repo for repo in data["discovered_repos"] if repo["registered_id"]]
    unregistered = [repo for repo in data["discovered_repos"] if not repo["registered_id"]]
    if registered:
        for repo in registered:
            kind = "worktree" if repo["git_marker_kind"] == "file" else "repo"
            lines.append(f"  OK {repo['rel_path']} ({repo['registered_id']}, {kind})")
    else:
        lines.append("  none")
    lines.extend(["", "Unregistered repos:"])
    if unregistered:
        for repo in unregistered:
            kind = "worktree" if repo["git_marker_kind"] == "file" else "repo"
            lines.append(f"  WARN {repo['rel_path']} ({kind})")
    else:
        lines.append("  none")
    lines.extend(["", "Durable-zone folders without repos:"])
    if data["candidate_folders"]:
        for folder in data["candidate_folders"]:
            lines.append(f"  WARN {folder}")
    else:
        lines.append("  none")
    lines.extend(["", "Worktrees:"])
    if data["worktrees"]:
        for worktree in data["worktrees"]:
            branch = worktree["branch"] or "detached"
            lines.append(f"  INFO {worktree['path']} ({branch})")
    else:
        lines.append("  none")
    return "\n".join(lines)


def render_audit(data: dict[str, Any]) -> str:
    lines = [f"Workspace: {data['workspace_root']}", f"Result: {data['result']}", "", "Findings:"]
    if not data["findings"]:
        lines.append("  OK no findings")
    else:
        for finding in data["findings"]:
            lines.append(f"  {finding['severity']} {finding['summary']}")
            if finding.get("details"):
                lines.append(f"    {finding['details']}")
    return "\n".join(lines)


def build_review(data: dict[str, Any]) -> dict[str, Any]:
    buckets = {"fix_now": [], "decide_this_week": [], "ignore_for_now": []}
    for finding in data["findings"]:
        buckets[finding["bucket"]].append(finding)
    return {
        "workspace_root": data["workspace_root"],
        "result": data["result"],
        "buckets": buckets,
    }


def render_review(review: dict[str, Any]) -> str:
    lines = [
        "# Housekeeping Review",
        "",
        f"- Workspace: `{review['workspace_root']}`",
        f"- Result: `{review['result']}`",
        "",
    ]
    headings = {
        "fix_now": "Fix Now",
        "decide_this_week": "Decide This Week",
        "ignore_for_now": "Ignore For Now",
    }
    for bucket_name in ("fix_now", "decide_this_week", "ignore_for_now"):
        lines.append(f"## {headings[bucket_name]}")
        bucket = review["buckets"][bucket_name]
        if not bucket:
            lines.append("- none")
            lines.append("")
            continue
        for finding in bucket:
            lines.append(f"- {finding['severity']}: {finding['summary']}")
            if finding.get("details"):
                lines.append(f"  Details: {finding['details']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def command_discover(args: argparse.Namespace, workspace_root: Path, manifest: Manifest) -> int:
    repos, candidates = discover_repo_checkouts(workspace_root, manifest)
    primary_repo_paths = {repo.rel_path for repo in repos if repo.git_marker_kind == "dir"}
    worktrees = [
        item
        for item in discover_worktrees(workspace_root, repos)
        if not (item.rel_path in primary_repo_paths)
    ]
    payload = {
        "workspace_root": str(workspace_root),
        "discovered_repos": [
            {
                "rel_path": repo.rel_path,
                "git_marker_kind": repo.git_marker_kind,
                "registered_id": repo.registered_id,
            }
            for repo in repos
        ],
        "candidate_folders": candidates,
        "worktrees": [
            {
                "path": str(item.path),
                "rel_path": item.rel_path,
                "branch": item.branch,
                "prunable": item.prunable,
            }
            for item in worktrees
        ],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render_discover(payload))
    return 0


def command_audit(args: argparse.Namespace, workspace_root: Path, manifest: Manifest) -> int:
    payload = build_audit(workspace_root, manifest)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render_audit(payload))
    return 1 if payload["result"] == "FAIL" else 0


def command_review(args: argparse.Namespace, workspace_root: Path, manifest: Manifest) -> int:
    audit_payload = build_audit(workspace_root, manifest)
    review = build_review(audit_payload)
    if args.json:
        print(json.dumps(review, indent=2))
    else:
        print(render_review(review))
    return 1 if audit_payload["result"] == "FAIL" else 0


def run_legacy_audit(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compatibility wrapper for hk.py audit.")
    parser.add_argument("--workspace-root", default="../..")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    manifest = load_manifest(Path(args.manifest).expanduser().resolve())
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    payload = build_audit(workspace_root, manifest)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render_audit(payload))
    return 1 if payload["result"] == "FAIL" else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = load_manifest(Path(args.manifest).expanduser().resolve())
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    if args.command == "discover":
        return command_discover(args, workspace_root, manifest)
    if args.command == "audit":
        return command_audit(args, workspace_root, manifest)
    if args.command == "review":
        return command_review(args, workspace_root, manifest)
    raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
