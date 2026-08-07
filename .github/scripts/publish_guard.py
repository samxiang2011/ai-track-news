#!/usr/bin/env python3
"""Fail-closed publish guard for commits created by an automated workflow.

The command scans every commit in ``base..ref`` before a named remote receives
the selected target ref.  It inspects repository-relative paths, commit and tag
messages, and added text.  Sensitive paths and binary changes fail closed by
default.  A caller may declare a small set of exact binary-to-text provenance
contracts; those blobs are read with strict bounds and the text companion is
scanned in full before the binary change is allowed.

The implementation is intentionally standalone and uses only the Python
standard library plus Git.  Output contains rule identifiers and safe relative
locators, never matched text or remote locations.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Sequence


EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

DEFAULT_MAX_COMMITS = 8
MAX_ALLOWED_COMMITS = 1024
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_COMMIT_MESSAGE_BYTES = 1024 * 1024
MAX_PATCH_BYTES = 64 * 1024 * 1024
MAX_FINDINGS = 10_000
DEFAULT_DETAIL_LIMIT = 20
MAX_BINARY_PROVENANCE_ENTRIES = 32
MAX_BINARY_PROVENANCE_PATH_BYTES = 1024
MAX_BINARY_PROVENANCE_BLOB_BYTES = 16 * 1024 * 1024
MAX_BINARY_PROVENANCE_TEXT_BYTES = 8 * 1024 * 1024
MAX_BINARY_PROVENANCE_TOTAL_BYTES = 64 * 1024 * 1024
REGULAR_BLOB_MODES = frozenset({"100644", "100755"})

OID_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
REMOTE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
HUNK_RE = re.compile(
    r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@"
)
COMMIT_MARKER_RE = re.compile(r"^\x1e(?P<commit>[0-9a-fA-F]{40,64})\x1f$")

POSIX_HOME_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?:Users|home)/"
    r"(?P<user>[^/\s\"'<>]+)(?=/|$|[\s\"'<>),.;:])"
)
WINDOWS_HOME_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])[A-Z]:\\Users\\"
    r"(?P<user>[^\\\s\"'<>]+)(?=\\|$|[\s\"'<>),.;:])"
)
RELATIVE_HOME_PATH_RE = re.compile(r"(?:^|/)(?:Users|home)/[^/]+(?:/|$)")

SENSITIVE_ALLOWED_ENV_FILES = {".env.example", ".env.sample", ".env.template"}
SENSITIVE_KEY_SUFFIXES = {".key", ".p12", ".pfx", ".pem"}
SENSITIVE_BACKUP_SUFFIXES = (
    ".backup",
    ".bak",
    ".old",
    ".orig",
    ".save",
    ".swp",
    "~",
)
SENSITIVE_DIR_NAMES = {
    ".aws",
    ".azure",
    ".docker",
    ".gnupg",
    ".kube",
    ".secrets",
    ".ssh",
    "secrets",
}
SENSITIVE_FILE_NAMES = {
    ".bash_history",
    ".envrc",
    ".git-credentials",
    ".mysql_history",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".psql_history",
    ".python_history",
    ".zsh_history",
    "cookies",
    "cookies.sqlite",
    "credentials",
    "credentials.json",
    "credentials.yaml",
    "credentials.yml",
    "id_ed25519",
    "id_rsa",
}


class PublishGuardError(RuntimeError):
    """A validation or read failure that prevents a trustworthy verdict."""


@dataclass(frozen=True)
class RuleMatch:
    rule: str
    hard: bool = True


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    line: int
    hard: bool = True
    source: str = "history"
    commit: str | None = None

    def key(self) -> tuple[str, str, int, bool, str, str | None]:
        return (
            self.rule,
            self.path,
            self.line,
            self.hard,
            self.source,
            self.commit,
        )


@dataclass(frozen=True)
class ScanResult:
    commits: tuple[str, ...]
    findings: tuple[Finding, ...]


@dataclass
class _ReadBudget:
    limit: int
    used: int = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def consume(self, amount: int) -> None:
        if amount < 0 or amount > self.remaining:
            raise PublishGuardError("binary provenance data exceeds its limit")
        self.used += amount


LinePolicy = Callable[[str, str, frozenset[str]], Iterable[RuleMatch]]
PathPolicy = Callable[[str], Iterable[RuleMatch]]


def _git_env() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    return environment


def _run_git_bytes(
    repo: Path,
    args: Sequence[str],
    *,
    allowed_codes: frozenset[int] = frozenset({0}),
    max_bytes: int = MAX_METADATA_BYTES,
) -> tuple[bytes, int]:
    if max_bytes < 0:
        raise PublishGuardError("git output limit is invalid")
    try:
        process = subprocess.Popen(
            ["git", "-C", str(repo), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_git_env(),
        )
    except OSError as exc:
        raise PublishGuardError("git query could not start") from exc

    assert process.stdout is not None
    output_stream = process.stdout
    chunks: list[bytes] = []
    total = 0
    try:
        try:
            while True:
                chunk = output_stream.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    process.kill()
                    process.wait()
                    raise PublishGuardError("git query output exceeds its limit")
                chunks.append(chunk)
            return_code = process.wait()
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait()
            raise
    finally:
        output_stream.close()

    if return_code not in allowed_codes:
        raise PublishGuardError("git query failed")
    return b"".join(chunks), return_code


def run_git(
    repo: Path,
    args: list[str],
    *,
    text: bool = True,
    allowed_codes: frozenset[int] = frozenset({0}),
    max_bytes: int = MAX_METADATA_BYTES,
) -> str | bytes:
    """Run a bounded, non-interactive Git query."""
    output, _ = _run_git_bytes(
        repo,
        args,
        allowed_codes=allowed_codes,
        max_bytes=max_bytes,
    )
    if text:
        return output.decode("utf-8", errors="replace")
    return output


def ensure_repo_root(repo: Path) -> Path:
    candidate = repo.resolve()
    if not candidate.is_dir():
        raise PublishGuardError("repository path is unavailable")
    root = str(run_git(candidate, ["rev-parse", "--show-toplevel"])).strip()
    if not root or Path(root).resolve() != candidate:
        raise PublishGuardError("target must be the repository root")
    return candidate


def _validate_ref_selector(repo: Path, value: str) -> None:
    if value == "HEAD" or OID_RE.fullmatch(value):
        return
    if (
        not value
        or len(value) > 1024
        or value.startswith("-")
        or any(character.isspace() or ord(character) < 32 for character in value)
        or not value.startswith(("refs/heads/", "refs/tags/", "refs/remotes/"))
    ):
        raise PublishGuardError("ref selector is invalid")
    _, return_code = _run_git_bytes(
        repo,
        ["check-ref-format", value],
        allowed_codes=frozenset({0, 1}),
        max_bytes=1024,
    )
    if return_code != 0:
        raise PublishGuardError("ref selector is invalid")


def resolve_commit(repo: Path, value: str) -> str:
    _validate_ref_selector(repo, value)
    object_id = str(
        run_git(repo, ["rev-parse", "--verify", f"{value}^{{commit}}"], max_bytes=1024)
    ).strip()
    if not OID_RE.fullmatch(object_id):
        raise PublishGuardError("commit selector did not resolve safely")
    return object_id.lower()


def validate_target(repo: Path, remote: str, target_ref: str) -> None:
    if not REMOTE_NAME_RE.fullmatch(remote):
        raise PublishGuardError("target remote must be a configured name")
    configured = {
        name
        for name in str(run_git(repo, ["remote"], max_bytes=64 * 1024)).splitlines()
        if REMOTE_NAME_RE.fullmatch(name)
    }
    if remote not in configured:
        raise PublishGuardError("target remote must be a configured name")

    if (
        not target_ref.startswith(("refs/heads/", "refs/tags/"))
        or len(target_ref) > 1024
        or any(
            character.isspace() or ord(character) < 32 for character in target_ref
        )
    ):
        raise PublishGuardError("target ref is invalid")
    _, return_code = _run_git_bytes(
        repo,
        ["check-ref-format", target_ref],
        allowed_codes=frozenset({0, 1}),
        max_bytes=1024,
    )
    if return_code != 0:
        raise PublishGuardError("target ref is invalid")


def list_reachable_commits(
    repo: Path,
    ref: str,
    *,
    exclude_refs: Sequence[str] = (),
    max_commits: int | None = DEFAULT_MAX_COMMITS,
) -> tuple[str, ...]:
    """List commits reachable from one ref and not from any explicit exclusion."""
    if max_commits is not None and not 1 <= max_commits <= MAX_ALLOWED_COMMITS:
        raise PublishGuardError("commit limit is invalid")

    included = resolve_commit(repo, ref)
    excluded = [resolve_commit(repo, item) for item in exclude_refs]
    args = ["rev-list", "--reverse", "--topo-order"]
    if max_commits is not None:
        args.append(f"--max-count={max_commits + 1}")
    args.append(included)
    if excluded:
        args.extend(["--not", *excluded])

    output = str(run_git(repo, args, max_bytes=MAX_METADATA_BYTES))
    commits = tuple(item.strip().lower() for item in output.splitlines() if item.strip())
    if any(not OID_RE.fullmatch(item) for item in commits):
        raise PublishGuardError("commit list is invalid")
    if max_commits is not None and len(commits) > max_commits:
        raise PublishGuardError("commit range exceeds its limit")
    return commits


def scan_line(line: str) -> list[RuleMatch]:
    """Return high-confidence public matches without retaining matched text."""
    if POSIX_HOME_RE.search(line) or WINDOWS_HOME_RE.search(line):
        return [RuleMatch("HOME_PATH")]
    return []


def public_line_policy(
    _path: str,
    line: str,
    _tracked_paths: frozenset[str],
) -> Iterable[RuleMatch]:
    return scan_line(line)


def scan_path(path: str) -> list[RuleMatch]:
    matches = scan_line(path)
    if RELATIVE_HOME_PATH_RE.search(path):
        matches.append(RuleMatch("HOME_PATH_IN_FILENAME"))
    unique: dict[tuple[str, bool], RuleMatch] = {}
    for match in matches:
        unique[(match.rule, match.hard)] = match
    return list(unique.values())


def public_path_policy(path: str) -> Iterable[RuleMatch]:
    return scan_path(path)


def _binary_matches(data: bytes) -> list[RuleMatch]:
    normalized_data = data.lower()
    anchors = ("/users/", "/home/", "\\users\\")
    encoded_anchors = {
        encoded
        for anchor in anchors
        for encoded in (
            anchor.encode("utf-8"),
            anchor.encode("utf-16-le"),
            anchor.encode("utf-16-be"),
            anchor.encode("utf-32-le"),
            anchor.encode("utf-32-be"),
        )
    }
    if any(anchor in normalized_data for anchor in encoded_anchors):
        return [RuleMatch("BINARY_PRIVATE_REFERENCE")]
    return []


def is_sensitive_path(path: str) -> bool:
    pure = PurePosixPath(path)
    lowered_sequence = [part.casefold() for part in pure.parts]
    lowered_parts = set(lowered_sequence)
    name = pure.name.casefold()
    if lowered_parts & SENSITIVE_DIR_NAMES:
        return True
    for index, part in enumerate(lowered_sequence):
        if part == ".env" or part.startswith(".env."):
            allowed_leaf = (
                index == len(lowered_sequence) - 1
                and part in SENSITIVE_ALLOWED_ENV_FILES
            )
            if not allowed_leaf:
                return True

    def sensitive_leaf(candidate: str) -> bool:
        return (
            candidate in SENSITIVE_FILE_NAMES
            or PurePosixPath(candidate).suffix in SENSITIVE_KEY_SUFFIXES
        )

    if sensitive_leaf(name):
        return True
    for suffix in SENSITIVE_BACKUP_SUFFIXES:
        if name.endswith(suffix) and sensitive_leaf(name[: -len(suffix)]):
            return True
    return False


def _validate_binary_provenance_path(value: object) -> str:
    """Validate one canonical, exact repository-relative contract path."""
    if not isinstance(value, str) or not value:
        raise PublishGuardError("binary provenance path is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PublishGuardError("binary provenance path is invalid") from exc
    if len(encoded) > MAX_BINARY_PROVENANCE_PATH_BYTES:
        raise PublishGuardError("binary provenance path exceeds its limit")
    if any(
        ord(character) < 32
        or ord(character) == 127
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise PublishGuardError("binary provenance path is invalid")
    if any(character in value for character in "*?[]{}"):
        raise PublishGuardError("binary provenance path must be exact")
    if "\\" in value or "=" in value or value.startswith(":("):
        raise PublishGuardError("binary provenance path must be canonical")

    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise PublishGuardError("binary provenance path must be repository-relative")
    if is_sensitive_path(value) or scan_path(value):
        raise PublishGuardError("binary provenance path is not public-safe")
    return value


def validate_binary_provenance(
    contracts: Mapping[str, str] | None,
) -> dict[str, str]:
    """Return a bounded, validated copy of exact binary provenance contracts."""
    if contracts is None:
        return {}
    if not isinstance(contracts, Mapping):
        raise PublishGuardError("binary provenance contract is invalid")
    items = list(contracts.items())
    if len(items) > MAX_BINARY_PROVENANCE_ENTRIES:
        raise PublishGuardError("binary provenance contract count exceeds its limit")

    validated: dict[str, str] = {}
    for binary_value, companion_value in items:
        binary = _validate_binary_provenance_path(binary_value)
        companion = _validate_binary_provenance_path(companion_value)
        if binary == companion:
            raise PublishGuardError("binary provenance paths must be distinct")
        if binary in validated:
            raise PublishGuardError("binary provenance path is duplicated")
        validated[binary] = companion
    return validated


def parse_binary_provenance(values: Sequence[str]) -> dict[str, str]:
    """Parse repeatable ``BINARY=TEXT_COMPANION`` command-line values."""
    if len(values) > MAX_BINARY_PROVENANCE_ENTRIES:
        raise PublishGuardError("binary provenance contract count exceeds its limit")
    parsed: dict[str, str] = {}
    for value in values:
        if not isinstance(value, str) or value.count("=") != 1:
            raise PublishGuardError("binary provenance contract is invalid")
        binary, companion = value.split("=", 1)
        binary = _validate_binary_provenance_path(binary)
        companion = _validate_binary_provenance_path(companion)
        if binary == companion:
            raise PublishGuardError("binary provenance paths must be distinct")
        if binary in parsed:
            raise PublishGuardError("binary provenance path is duplicated")
        parsed[binary] = companion
    return parsed


def _decode_history_path(raw: bytes) -> str:
    path = raw.decode("utf-8", errors="surrogateescape")
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or ".." in pure.parts or "\0" in path:
        raise PublishGuardError("git history contains an unsafe path")
    return path


def parse_name_status(raw: bytes) -> list[tuple[str, str]]:
    """Parse ``--name-status -z`` output with rename detection disabled."""
    tokens = raw.split(b"\0")
    changes: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index].lstrip(b"\n")
        index += 1
        if not token:
            continue
        if OID_RE.fullmatch(token.decode("ascii", errors="ignore")):
            continue
        if b"\t" in token:
            status_raw, path_raw = token.split(b"\t", 1)
        else:
            status_raw = token
            if index >= len(tokens):
                raise PublishGuardError("git name-status output is incomplete")
            path_raw = tokens[index]
            index += 1
        status = status_raw.decode("ascii", errors="replace")
        if not re.fullmatch(r"[ACDMRTUXB][0-9]*", status):
            raise PublishGuardError("git name-status output is invalid")
        if status.startswith(("R", "C")):
            if index >= len(tokens):
                raise PublishGuardError("git rename output is incomplete")
            path_raw = tokens[index]
            index += 1
        changes.append((status[0], _decode_history_path(path_raw)))
    return changes


def parse_binary_numstat(raw: bytes) -> set[str]:
    """Return paths Git identified as binary in ``--numstat -z`` output."""
    paths: set[str] = set()
    for token in raw.split(b"\0"):
        token = token.lstrip(b"\n")
        if not token:
            continue
        fields = token.split(b"\t", 2)
        if len(fields) != 3:
            raise PublishGuardError("git numstat output is invalid")
        added, deleted, path_raw = fields
        if added == b"-" and deleted == b"-":
            paths.add(_decode_history_path(path_raw))
    return paths


def _parse_added_path(line: str) -> str | None:
    try:
        parts = shlex.split(line)
    except ValueError as exc:
        raise PublishGuardError("patch path is invalid") from exc
    if len(parts) < 2 or parts[0] != "+++" or parts[1] == "/dev/null":
        return None
    path = parts[1]
    relative = path[2:] if path.startswith("b/") else path
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\0" in relative:
        raise PublishGuardError("patch contains an unsafe path")
    return relative


def parse_patch_additions(
    patch: str,
    tracked_paths_at_tip: set[str],
    *,
    tracked_paths_by_commit: dict[str, set[str]] | None = None,
    line_policy: LinePolicy = public_line_policy,
    default_commit: str | None = None,
) -> list[Finding]:
    """Scan added patch lines; a later removal never cancels an earlier match."""
    findings: list[Finding] = []
    commit = default_commit
    path: str | None = None
    new_line: int | None = None
    in_hunk = False

    for line in patch.split("\n"):
        marker = COMMIT_MARKER_RE.match(line)
        if marker:
            commit = marker.group("commit").lower()
            path = None
            new_line = None
            in_hunk = False
            continue
        if line.startswith("diff --git "):
            path = None
            new_line = None
            in_hunk = False
            continue
        hunk = HUNK_RE.match(line)
        if hunk:
            new_line = int(hunk.group("start"))
            in_hunk = True
            continue
        if in_hunk and path is not None and new_line is not None:
            if line.startswith("+"):
                pointer_paths = tracked_paths_at_tip
                if tracked_paths_by_commit is not None:
                    if commit is None or commit not in tracked_paths_by_commit:
                        raise PublishGuardError("history tree is unavailable for a commit")
                    pointer_paths = tracked_paths_by_commit[commit]
                added_text = line[1:]
                if "\0" in added_text:
                    findings.append(
                        Finding(
                            "BINARY_HISTORY_UNVERIFIED",
                            path,
                            new_line,
                            commit=commit[:12] if commit else None,
                        )
                    )
                else:
                    for match in line_policy(path, added_text, frozenset(pointer_paths)):
                        findings.append(
                            Finding(
                                match.rule,
                                path,
                                new_line,
                                match.hard,
                                commit=commit[:12] if commit else None,
                            )
                        )
                new_line += 1
            elif line.startswith("-"):
                continue
            elif line.startswith(" "):
                new_line += 1
            continue
        if line.startswith("+++ "):
            path = _parse_added_path(line)

    return _deduplicate(findings)


def _commit_path_changes(repo: Path, commit: str) -> list[tuple[str, str]]:
    raw = bytes(
        run_git(
            repo,
            [
                "diff-tree",
                "--root",
                "-m",
                "--no-commit-id",
                "--name-status",
                "-z",
                "-r",
                "--no-renames",
                commit,
            ],
            text=False,
        )
    )
    return parse_name_status(raw)


def _commit_tree_entries(repo: Path, commit: str) -> dict[str, tuple[str, str]]:
    """Return path -> (mode, object type) for one commit tree."""
    raw = bytes(
        run_git(
            repo,
            ["ls-tree", "-r", "-z", commit],
            text=False,
        )
    )
    entries: dict[str, tuple[str, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, separator, path_raw = record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3:
            raise PublishGuardError("git tree output is invalid")
        mode_raw, object_type_raw, object_id_raw = fields
        if not re.fullmatch(rb"[0-7]{6}", mode_raw):
            raise PublishGuardError("git tree mode is invalid")
        if object_type_raw not in {b"blob", b"commit"}:
            raise PublishGuardError("git tree object type is invalid")
        if not OID_RE.fullmatch(object_id_raw.decode("ascii", errors="ignore")):
            raise PublishGuardError("git tree object id is invalid")
        path = _decode_history_path(path_raw)
        if path in entries:
            raise PublishGuardError("git tree path is duplicated")
        entries[path] = (mode_raw.decode("ascii"), object_type_raw.decode("ascii"))
    return entries


def _commit_tree_paths(repo: Path, commit: str) -> set[str]:
    return set(_commit_tree_entries(repo, commit))


def _read_commit_blob(
    repo: Path,
    commit: str,
    path: str,
    *,
    per_blob_limit: int,
    budget: _ReadBudget,
) -> bytes:
    if per_blob_limit < 1 or budget.remaining < 1:
        raise PublishGuardError("binary provenance data exceeds its limit")
    data = bytes(
        run_git(
            repo,
            ["cat-file", "blob", f"{commit}:{path}"],
            text=False,
            max_bytes=min(per_blob_limit, budget.remaining),
        )
    )
    budget.consume(len(data))
    return data


def _binary_provenance_findings(
    repo: Path,
    commit: str,
    binary_path: str,
    companion_path: str,
    commit_entries: Mapping[str, tuple[str, str]],
    *,
    budget: _ReadBudget,
    line_policy: LinePolicy,
) -> list[Finding]:
    """Verify one exact contract; binary inspection is deliberately raw-only."""
    binary_entry = commit_entries.get(binary_path)
    companion_entry = commit_entries.get(companion_path)
    if companion_entry is None:
        raise PublishGuardError("binary provenance companion is unavailable")
    if (
        binary_entry is None
        or binary_entry[0] not in REGULAR_BLOB_MODES
        or binary_entry[1] != "blob"
        or companion_entry[0] not in REGULAR_BLOB_MODES
        or companion_entry[1] != "blob"
    ):
        raise PublishGuardError("binary provenance paths must be regular files")

    binary_data = _read_commit_blob(
        repo,
        commit,
        binary_path,
        per_blob_limit=MAX_BINARY_PROVENANCE_BLOB_BYTES,
        budget=budget,
    )
    findings = [
        Finding(
            match.rule,
            binary_path,
            0,
            match.hard,
            source="binary-provenance-raw",
            commit=commit[:12],
        )
        for match in _binary_matches(binary_data)
    ]

    companion_data = _read_commit_blob(
        repo,
        commit,
        companion_path,
        per_blob_limit=MAX_BINARY_PROVENANCE_TEXT_BYTES,
        budget=budget,
    )
    if b"\0" in companion_data:
        raise PublishGuardError("binary provenance companion is not text")
    try:
        companion_text = companion_data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublishGuardError("binary provenance companion is not UTF-8") from exc

    frozen_paths = frozenset(commit_entries)
    for line_number, line in enumerate(companion_text.splitlines(), start=1):
        for match in line_policy(companion_path, line, frozen_paths):
            findings.append(
                Finding(
                    match.rule,
                    companion_path,
                    line_number,
                    match.hard,
                    source="binary-provenance-companion",
                    commit=commit[:12],
                )
            )
            if len(findings) > MAX_FINDINGS:
                raise PublishGuardError("finding count exceeds its limit")
    return findings


def _scan_message_bytes(
    message: bytes,
    *,
    commit_paths: set[str],
    locator: str,
    source: str,
    object_id: str,
    unverified_rule: str,
    line_policy: LinePolicy = public_line_policy,
) -> list[Finding]:
    try:
        text = message.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    if b"\0" in message or (not text and message):
        return [
            Finding(
                unverified_rule,
                locator,
                0,
                source=source,
                commit=object_id[:12],
            )
        ]
    findings: list[Finding] = []
    frozen_paths = frozenset(commit_paths)
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in line_policy(locator, line, frozen_paths):
            findings.append(
                Finding(
                    match.rule,
                    locator,
                    line_number,
                    match.hard,
                    source=source,
                    commit=object_id[:12],
                )
            )
    return findings


def _commit_message_findings(
    repo: Path,
    commit: str,
    commit_paths: set[str],
    *,
    line_policy: LinePolicy = public_line_policy,
) -> list[Finding]:
    raw_commit = bytes(
        run_git(
            repo,
            ["cat-file", "commit", commit],
            text=False,
            max_bytes=MAX_COMMIT_MESSAGE_BYTES,
        )
    )
    _, separator, message = raw_commit.partition(b"\n\n")
    if not separator:
        raise PublishGuardError("commit object has no message boundary")
    return _scan_message_bytes(
        message,
        commit_paths=commit_paths,
        locator="[commit-message]",
        source="history-message",
        object_id=commit,
        unverified_rule="COMMIT_MESSAGE_UNVERIFIED",
        line_policy=line_policy,
    )


def _tag_message_findings(
    repo: Path,
    ref: str,
    commit_paths: set[str],
    *,
    line_policy: LinePolicy = public_line_policy,
) -> list[Finding]:
    _validate_ref_selector(repo, ref)
    object_id = str(run_git(repo, ["rev-parse", "--verify", ref], max_bytes=1024)).strip()
    findings: list[Finding] = []
    seen: set[str] = set()
    while object_id:
        if object_id in seen or not OID_RE.fullmatch(object_id):
            raise PublishGuardError("tag object chain is invalid")
        seen.add(object_id)
        object_type = str(
            run_git(repo, ["cat-file", "-t", object_id], max_bytes=1024)
        ).strip()
        if object_type != "tag":
            break
        tag_object = bytes(
            run_git(
                repo,
                ["cat-file", "tag", object_id],
                text=False,
                max_bytes=MAX_COMMIT_MESSAGE_BYTES,
            )
        )
        headers, separator, message = tag_object.partition(b"\n\n")
        target_line = next(
            (line for line in headers.splitlines() if line.startswith(b"object ")),
            None,
        )
        if target_line is None:
            raise PublishGuardError("tag object has no target")
        if separator:
            findings.extend(
                _scan_message_bytes(
                    message,
                    commit_paths=commit_paths,
                    locator="[tag-message]",
                    source="history-tag-message",
                    object_id=object_id,
                    unverified_rule="TAG_MESSAGE_UNVERIFIED",
                    line_policy=line_policy,
                )
            )
        object_id = target_line.removeprefix(b"object ").strip().decode("ascii")
    return findings


def _commit_binary_paths(
    repo: Path,
    commit: str,
    *,
    excluded_paths: set[str],
) -> set[str]:
    args = [
        "diff-tree",
        "--root",
        "-m",
        "--no-commit-id",
        "--numstat",
        "-z",
        "-r",
        "--no-renames",
        commit,
    ]
    if excluded_paths:
        args.extend(["--", "."])
        args.extend(f":(exclude,literal){path}" for path in sorted(excluded_paths))
    raw = bytes(run_git(repo, args, text=False))
    return parse_binary_numstat(raw)


def _commit_patch(
    repo: Path,
    commit: str,
    *,
    excluded_paths: set[str],
    max_bytes: int,
) -> bytes:
    args = [
        "-c",
        "core.quotePath=false",
        "show",
        "--format=",
        "--root",
        "-m",
        "-p",
        "--no-renames",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--unified=0",
        commit,
        "--",
    ]
    if excluded_paths:
        args.append(".")
        args.extend(f":(exclude,literal){path}" for path in sorted(excluded_paths))
    return bytes(run_git(repo, args, text=False, max_bytes=max_bytes))


def _deduplicate(findings: Iterable[Finding]) -> list[Finding]:
    by_key: dict[tuple[str, str, int, bool, str, str | None], Finding] = {}
    for finding in findings:
        by_key[finding.key()] = finding
        if len(by_key) > MAX_FINDINGS:
            raise PublishGuardError("finding count exceeds its limit")
    return sorted(
        by_key.values(),
        key=lambda item: (
            not item.hard,
            item.path,
            item.line,
            item.rule,
            item.commit or "",
        ),
    )


def scan_commits(
    repo: Path,
    commits: Sequence[str],
    *,
    line_policy: LinePolicy = public_line_policy,
    path_policy: PathPolicy = public_path_policy,
    binary_provenance: Mapping[str, str] | None = None,
    max_patch_bytes: int = MAX_PATCH_BYTES,
) -> tuple[Finding, ...]:
    """Scan an explicit, ordered commit list with injectable public-safe policy."""
    if max_patch_bytes < 1:
        raise PublishGuardError("patch limit is invalid")
    contracts = validate_binary_provenance(binary_provenance)
    normalized: list[str] = []
    for commit in commits:
        if not OID_RE.fullmatch(commit):
            raise PublishGuardError("commit list is invalid")
        normalized.append(commit.lower())

    findings: list[Finding] = []
    patch_bytes = 0
    provenance_budget = _ReadBudget(MAX_BINARY_PROVENANCE_TOTAL_BYTES)
    for commit in normalized:
        commit_entries = _commit_tree_entries(repo, commit)
        commit_paths = set(commit_entries)
        findings.extend(
            _commit_message_findings(
                repo,
                commit,
                commit_paths,
                line_policy=line_policy,
            )
        )
        changes = _commit_path_changes(repo, commit)
        sensitive_paths = {path for _, path in changes if is_sensitive_path(path)}
        binary_paths = _commit_binary_paths(
            repo,
            commit,
            excluded_paths=sensitive_paths,
        )
        live_paths = {path for status, path in changes if status != "D"}

        for status, path in changes:
            if status == "D":
                continue
            if is_sensitive_path(path):
                findings.append(
                    Finding("SENSITIVE_PATH", path, 0, commit=commit[:12])
                )
            for match in path_policy(path):
                findings.append(
                    Finding(
                        match.rule,
                        path,
                        0,
                        match.hard,
                        commit=commit[:12],
                    )
                )
        contracted_changes = live_paths & contracts.keys()
        for path in sorted(contracted_changes):
            findings.extend(
                _binary_provenance_findings(
                    repo,
                    commit,
                    path,
                    contracts[path],
                    commit_entries,
                    budget=provenance_budget,
                    line_policy=line_policy,
                )
            )
            if len(findings) > MAX_FINDINGS:
                raise PublishGuardError("finding count exceeds its limit")

        for path in sorted((binary_paths & live_paths) - contracts.keys()):
            findings.append(
                Finding(
                    "BINARY_HISTORY_UNVERIFIED",
                    path,
                    0,
                    commit=commit[:12],
                )
            )
            if len(findings) > MAX_FINDINGS:
                raise PublishGuardError("finding count exceeds its limit")

        remaining = max_patch_bytes - patch_bytes
        if remaining < 1:
            raise PublishGuardError("patch data exceeds its limit")
        patch_raw = _commit_patch(
            repo,
            commit,
            excluded_paths=sensitive_paths,
            max_bytes=remaining,
        )
        patch_bytes += len(patch_raw)
        try:
            patch = patch_raw.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(
                Finding(
                    "PATCH_TEXT_UNVERIFIED",
                    "[history-patch]",
                    0,
                    commit=commit[:12],
                )
            )
        else:
            findings.extend(
                parse_patch_additions(
                    patch,
                    commit_paths,
                    line_policy=line_policy,
                    default_commit=commit,
                )
            )

        if len(findings) > MAX_FINDINGS:
            raise PublishGuardError("finding count exceeds its limit")

    return tuple(_deduplicate(findings))


def scan_range(
    repo: Path,
    base: str,
    ref: str,
    *,
    target_remote: str,
    target_ref: str,
    min_commits: int = 0,
    max_commits: int = DEFAULT_MAX_COMMITS,
    line_policy: LinePolicy = public_line_policy,
    path_policy: PathPolicy = public_path_policy,
    binary_provenance: Mapping[str, str] | None = None,
) -> ScanResult:
    """Validate the target and scan every commit in ``base..ref``."""
    if min_commits < 0 or min_commits > max_commits:
        raise PublishGuardError("commit bounds are invalid")
    verified_repo = ensure_repo_root(repo)
    validate_target(verified_repo, target_remote, target_ref)
    if not OID_RE.fullmatch(base):
        raise PublishGuardError("base must be a full commit object id")
    base_id = resolve_commit(verified_repo, base)
    ref_id = resolve_commit(verified_repo, ref)

    _, ancestor_status = _run_git_bytes(
        verified_repo,
        ["merge-base", "--is-ancestor", base_id, ref_id],
        allowed_codes=frozenset({0, 1}),
        max_bytes=1024,
    )
    if ancestor_status != 0:
        raise PublishGuardError("base is not an ancestor of the selected ref")

    commits = list_reachable_commits(
        verified_repo,
        ref_id,
        exclude_refs=(base_id,),
        max_commits=max_commits,
    )
    if len(commits) < min_commits:
        raise PublishGuardError("commit range is below its minimum")
    findings = list(
        scan_commits(
            verified_repo,
            commits,
            line_policy=line_policy,
            path_policy=path_policy,
            binary_provenance=binary_provenance,
        )
    )
    ref_tree = _commit_tree_paths(verified_repo, ref_id)
    findings.extend(
        _tag_message_findings(
            verified_repo,
            ref,
            ref_tree,
            line_policy=line_policy,
        )
    )
    return ScanResult(commits, tuple(_deduplicate(findings)))


def _safe_finding_path(finding: Finding) -> str:
    if finding.rule == "SENSITIVE_PATH":
        return "[sensitive-path]"
    path = finding.path
    if (
        len(path) > 512
        or any(
            ord(character) < 32
            or ord(character) == 127
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in path
        )
        or POSIX_HOME_RE.search(path)
        or WINDOWS_HOME_RE.search(path)
        or RELATIVE_HOME_PATH_RE.search(path)
    ):
        return "[redacted-path]"
    return path


def _format_finding(finding: Finding) -> str:
    label = finding.rule if finding.hard else f"REVIEW/{finding.rule}"
    path = _safe_finding_path(finding)
    locator = f"{path}:{finding.line}" if finding.line else path
    if finding.commit:
        locator += f" @ {finding.commit}"
    return f"  - {label} {locator}"


def render_result(result: ScanResult, *, detail_limit: int = DEFAULT_DETAIL_LIMIT) -> int:
    blocking = [finding for finding in result.findings if finding.hard]
    review = [finding for finding in result.findings if not finding.hard]
    if blocking:
        print(
            f"- FAIL: publish guard found {len(blocking)} blocking finding(s) "
            f"across {len(result.commits)} commit(s); review={len(review)}."
        )
    elif review:
        print(
            f"- INFO: publish guard found no blocker across {len(result.commits)} "
            f"commit(s); review={len(review)}."
        )
    else:
        print(f"- OK: publish guard passed for {len(result.commits)} commit(s).")

    ordered = [*blocking, *review]
    for finding in ordered[:detail_limit]:
        print(_format_finding(finding))
    if len(ordered) > detail_limit:
        print(f"  - ... {len(ordered) - detail_limit} more finding(s) not shown")
    return EXIT_FINDINGS if blocking else EXIT_OK


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise PublishGuardError("invocation is invalid")


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--base", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--target-remote", required=True)
    parser.add_argument("--target-ref", required=True)
    parser.add_argument("--min-commits", type=int, default=0)
    parser.add_argument("--max-commits", type=int, default=DEFAULT_MAX_COMMITS)
    parser.add_argument(
        "--allow-binary-provenance",
        action="append",
        default=[],
        metavar="BINARY=TEXT_COMPANION",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        binary_provenance = parse_binary_provenance(args.allow_binary_provenance)
        result = scan_range(
            args.repo,
            args.base,
            args.ref,
            target_remote=args.target_remote,
            target_ref=args.target_ref,
            min_commits=args.min_commits,
            max_commits=args.max_commits,
            binary_provenance=binary_provenance,
        )
        return render_result(result)
    except (PublishGuardError, OSError, UnicodeError):
        print("- WARN: publish guard could not produce a safe verdict.")
        return EXIT_ERROR
    except Exception as exc:  # pragma: no cover - defensive process boundary
        print(
            "- WARN: publish guard could not produce a safe verdict "
            f"({type(exc).__name__})."
        )
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
