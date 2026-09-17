"""Install-time rendering and merging for Agent Work Boundaries hooks."""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import json
import os
import re
import shlex
import shutil
import stat
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


_PYTHON_EXECUTABLE = re.compile(r"python(?:\d+(?:\.\d+)*)?")
_PYTHON_NO_ARGUMENT_OPTIONS = {
    "-b",
    "-bb",
    "-B",
    "-d",
    "-E",
    "-i",
    "-I",
    "-O",
    "-OO",
    "-P",
    "-q",
    "-s",
    "-S",
    "-u",
    "-v",
    "-x",
}
_PYTHON_ARGUMENT_OPTIONS = {"-W", "-X"}
_PYTHON_CLUSTERABLE_NO_ARGUMENT_FLAGS = frozenset("bBdEiIOPqsSuvx")
_PYTHON_NON_FILE_OPTIONS = {
    "-c",
    "-m",
    "-h",
    "-V",
    "--help",
    "--help-env",
    "--help-xoptions",
    "--help-all",
    "--version",
}
_SKILL_NAMES = ("project-handoff", "task-router")
_BUNDLE_NAME = "agent-work-boundaries"
_LOCK_NAME = ".agent-work-boundaries.lock"
_JOURNAL_NAME = ".agent-work-boundaries.transaction.json"
_TRANSACTION_ID = re.compile(r"[0-9a-f]{32}")
_BACKUP_ID = re.compile(r"\d{8}T\d{6}Z(?:-\d{2,})?")


class InstallError(RuntimeError):
    """Raised when the bundle cannot be installed without risking user data."""


@dataclass(frozen=True)
class InstallReport:
    action: str
    changed_paths: Sequence[Path]
    backed_up_files: Sequence[Path]
    backup_root: Path | None
    dry_run: bool


@dataclass(frozen=True)
class _ControlIdentity:
    device: int
    inode: int


def _env_command_index(parts: list[str]) -> int | None:
    index = 1
    while index < len(parts):
        token = parts[index]
        if token == "--":
            index += 1
            break
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            index += 1
            continue
        if token in {"-i", "--ignore-environment", "-0", "--null"}:
            index += 1
            continue
        if token in {"-u", "--unset", "-C", "--chdir"}:
            if index + 1 >= len(parts):
                return None
            index += 2
            continue
        if token.startswith(("--unset=", "--chdir=")):
            if token.endswith("="):
                return None
            index += 1
            continue
        if (token.startswith("-u") or token.startswith("-C")) and len(token) > 2:
            index += 1
            continue
        if token in {"-S", "--split-string"} or token.startswith(
            "--split-string="
        ):
            return None
        if token.startswith("-"):
            return None
        break
    return index if index < len(parts) else None


def _python_script_index(parts: list[str], index: int) -> int | None:
    index += 1
    while index < len(parts):
        option = parts[index]
        if option == "--":
            index += 1
            break
        if option in _PYTHON_NON_FILE_OPTIONS:
            return None
        if option == "--check-hash-based-pycs":
            if index + 1 >= len(parts) or parts[index + 1] not in {
                "default",
                "always",
                "never",
            }:
                return None
            index += 2
            continue
        if option in _PYTHON_ARGUMENT_OPTIONS:
            if index + 1 >= len(parts):
                return None
            index += 2
            continue
        if option.startswith(("-W", "-X")) and len(option) > 2:
            index += 1
            continue
        if option in _PYTHON_NO_ARGUMENT_OPTIONS:
            index += 1
            continue
        if (
            len(option) > 2
            and option.startswith("-")
            and not option.startswith("--")
            and all(
                flag in _PYTHON_CLUSTERABLE_NO_ARGUMENT_FLAGS
                for flag in option[1:]
            )
        ):
            index += 1
            continue
        if option.startswith("-"):
            return None
        break
    return index if index < len(parts) else None


def render_managed_hooks(hook_script: Path) -> dict[str, Any]:
    command = f"python3 {shlex.quote(str(hook_script.resolve()))}"
    base_handler = {"type": "command", "command": command, "timeout": 5}
    return {
        "hooks": {
            "Stop": [{"hooks": [copy.deepcopy(base_handler)]}],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {**copy.deepcopy(base_handler), "additionalContextLimit": 500}
                    ]
                }
            ],
            "PostCompact": [
                {
                    "matcher": "manual|auto",
                    "hooks": [copy.deepcopy(base_handler)],
                }
            ],
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [
                        {**copy.deepcopy(base_handler), "additionalContextLimit": 500}
                    ],
                }
            ],
        }
    }


def _command_target(command: str) -> Path | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if not parts:
        return None

    index = 0
    if Path(parts[index]).name == "env":
        command_index = _env_command_index(parts)
        if command_index is None:
            return None
        index = command_index

    executable = Path(parts[index]).name
    if _PYTHON_EXECUTABLE.fullmatch(executable):
        script_index = _python_script_index(parts, index)
        if script_index is None:
            return None
        target = parts[script_index]
    else:
        target = parts[index]
    return Path(target).expanduser().resolve()


def _as_object(value: object, label: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(f"{label} must be valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _validate_hooks(value: object, label: str) -> dict[str, Any]:
    document = _as_object(value, label)
    hooks = document.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{label}.hooks must be an object")
    for event, groups in hooks.items():
        if not isinstance(event, str) or not isinstance(groups, list):
            raise ValueError(f"{label}.hooks entries must map event names to lists")
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError(f"{label}.{event} groups must be objects")
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                raise ValueError(f"{label}.{event} group hooks must be a list")
            for handler in handlers:
                if not isinstance(handler, dict):
                    raise ValueError(f"{label}.{event} handlers must be objects")
                if not isinstance(handler.get("type"), str):
                    raise ValueError(f"{label}.{event} handler type must be a string")
                if handler["type"] == "command" and not isinstance(
                    handler.get("command"), str
                ):
                    raise ValueError(f"{label}.{event} handler command must be a string")
    return document


def _managed_target(managed: dict[str, Any]) -> Path:
    managed_target: Path | None = None
    for groups in managed["hooks"].values():
        for group in groups:
            if not group["hooks"]:
                raise ValueError(
                    "managed hook groups must contain a command handler"
                )
            for handler in group["hooks"]:
                if handler["type"] != "command":
                    raise ValueError(
                        "managed hook groups may contain only command handlers"
                    )
                target = _command_target(handler["command"])
                if target is None:
                    raise ValueError(
                        "managed hook commands must have a parseable target"
                    )
                if managed_target is None:
                    managed_target = target
                elif target != managed_target:
                    raise ValueError(
                        "managed hook commands must use the same executable target"
                    )
    if managed_target is None:
        raise ValueError("managed hooks contain no command handler")
    return managed_target


def _without_matching_handlers(
    groups: list[dict[str, Any]], predicate: Callable[[Path | None], bool]
) -> list[dict[str, Any]]:
    kept_groups: list[dict[str, Any]] = []
    for group in groups:
        kept_handlers = [
            handler
            for handler in group["hooks"]
            if not (
                handler["type"] == "command"
                and predicate(_command_target(handler["command"]))
            )
        ]
        removed_handler = len(kept_handlers) != len(group["hooks"])
        if kept_handlers or not removed_handler:
            kept_group = copy.deepcopy(group)
            kept_group["hooks"] = copy.deepcopy(kept_handlers)
            kept_groups.append(kept_group)
    return kept_groups


def merge_hooks(existing: dict[str, Any], managed: dict[str, Any]) -> dict[str, Any]:
    existing = _validate_hooks(existing, "existing hooks")
    managed = _validate_hooks(managed, "managed hooks")
    target = _managed_target(managed)
    result = copy.deepcopy(existing)
    result.setdefault("hooks", {})

    for event, existing_groups in result["hooks"].items():
        result["hooks"][event] = _without_matching_handlers(
            existing_groups, lambda candidate: candidate == target
        )
    for event, managed_groups in managed["hooks"].items():
        result["hooks"].setdefault(event, []).extend(copy.deepcopy(managed_groups))
    return result


def remove_managed_hooks(
    existing: dict[str, Any], installed_hook_script: Path
) -> dict[str, Any]:
    existing = _validate_hooks(existing, "existing hooks")
    installed_target = Path(installed_hook_script).expanduser().resolve()
    result = copy.deepcopy(existing)
    if "hooks" not in result:
        return result

    for event, groups in result["hooks"].items():
        result["hooks"][event] = _without_matching_handlers(
            groups, lambda candidate: candidate == installed_target
        )
    return result


def _path_exists(path: Path) -> bool:
    return _lstat_mode(path) is not None


def _lstat_mode(path: Path) -> int | None:
    try:
        return path.lstat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return None


def _mode_description(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "a symbolic link"
    if stat.S_ISFIFO(mode):
        return "a FIFO"
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISCHR(mode):
        return "a character device"
    if stat.S_ISBLK(mode):
        return "a block device"
    return "an unsupported file type"


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry changes without following a replacement link."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise InstallError(f"cannot open directory for fsync {path}: {error}") from error
    try:
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISDIR(mode):
            raise InstallError(f"expected a directory: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_regular_file(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise InstallError(f"cannot open file for fsync {path}: {error}") from error
    try:
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode):
            raise InstallError(f"expected a regular file: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_and_fsync(source: Path, target: Path) -> None:
    source = Path(source)
    target = Path(target)
    os.replace(source, target)
    _fsync_directory(target.parent)
    if source.parent != target.parent:
        _fsync_directory(source.parent)


def _unlink_and_fsync(path: Path) -> None:
    path.unlink()
    _fsync_directory(path.parent)


def _tree_entries(root: Path, label: str) -> list[tuple[Path, int]]:
    entries: list[tuple[Path, int]] = []
    directories = [root]
    while directories:
        directory = directories.pop()
        try:
            with os.scandir(directory) as children:
                for child in children:
                    path = Path(child.path)
                    try:
                        mode = child.stat(follow_symlinks=False).st_mode
                    except OSError as error:
                        raise InstallError(
                            f"cannot inspect {label} entry {path}: {error}"
                        ) from error
                    entries.append((path, mode))
                    if stat.S_ISDIR(mode):
                        directories.append(path)
        except OSError as error:
            raise InstallError(
                f"cannot inspect {label} {directory}: {error}"
            ) from error
    return entries


def _validate_regular_tree(root: Path, label: str) -> None:
    for path, mode in _tree_entries(root, label):
        if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
            raise InstallError(
                f"{label} contains {_mode_description(mode)}: {path}"
            )


def _assert_no_symlink_components(path: Path, label: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        mode = _lstat_mode(current)
        if mode is None:
            break
        if stat.S_ISLNK(mode):
            raise InstallError(f"{label} contains a symbolic link: {current}")
        if current != absolute and not stat.S_ISDIR(mode):
            raise InstallError(f"expected a directory: {current}")


def _validate_source_tree(source: Path) -> None:
    _assert_no_symlink_components(source, "skill source path")
    mode = _lstat_mode(source)
    if mode is None:
        raise InstallError(f"missing skill source: {source}")
    if not stat.S_ISDIR(mode):
        if stat.S_ISLNK(mode):
            raise InstallError(f"skill source contains a symbolic link: {source}")
        raise InstallError(f"skill source must be a directory: {source}")
    _validate_regular_tree(source, "skill source")


def _validate_managed_target(name: str, target: Path) -> None:
    mode = _lstat_mode(target)
    if mode is None or stat.S_ISREG(mode):
        return
    if not stat.S_ISDIR(mode):
        raise InstallError(
            f"managed skill target {name} contains "
            f"{_mode_description(mode)}: {target}"
        )
    _validate_regular_tree(target, f"managed skill target {name}")


def _control_identity(
    metadata: os.stat_result, path: Path, label: str
) -> _ControlIdentity:
    if stat.S_ISLNK(metadata.st_mode):
        raise InstallError(f"{label} is a symbolic link: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise InstallError(f"{label} must be a regular file: {path}")
    if metadata.st_nlink != 1:
        raise InstallError(f"{label} must not have hard links: {path}")
    if metadata.st_uid != os.getuid():
        raise InstallError(f"{label} must be owned by the current user: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise InstallError(
            f"{label} permissions must be private (0600): {path}"
        )
    return _ControlIdentity(metadata.st_dev, metadata.st_ino)


def _lstat_control_identity(
    path: Path, label: str, *, missing_ok: bool = False
) -> _ControlIdentity | None:
    try:
        metadata = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        if missing_ok:
            return None
        raise InstallError(f"{label} changed or disappeared: {path}") from None
    except OSError as error:
        raise InstallError(f"cannot inspect {label} {path}: {error}") from error
    return _control_identity(metadata, path, label)


def _fstat_control_identity(
    descriptor: int, path: Path, label: str
) -> _ControlIdentity:
    try:
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise InstallError(f"cannot inspect open {label} {path}: {error}") from error
    return _control_identity(metadata, path, label)


def _require_control_identity(
    path: Path, label: str, expected: _ControlIdentity
) -> None:
    current = _lstat_control_identity(path, label)
    if current != expected:
        raise InstallError(f"{label} changed while in use: {path}")


def _unlink_control_and_fsync(
    path: Path, label: str, expected: _ControlIdentity
) -> None:
    """Unlink only the control inode previously validated by descriptor."""

    _require_control_identity(path, label, expected)
    path.unlink()
    _fsync_directory(path.parent)


def _validate_private_control_entry(path: Path, label: str) -> None:
    _lstat_control_identity(path, label, missing_ok=True)


def _validate_managed_paths(codex_home: Path, targets: dict[str, Path]) -> None:
    _assert_no_symlink_components(codex_home, "CODEX_HOME path")
    home_mode = _lstat_mode(codex_home)
    if home_mode is not None and not stat.S_ISDIR(home_mode):
        raise InstallError(f"expected a directory: {codex_home}")
    for path, label in (
        (codex_home / "skills", "skills directory"),
        (codex_home / "backups", "backup directory"),
        (
            codex_home / "backups" / "agent-work-boundaries",
            "bundle backup directory",
        ),
    ):
        mode = _lstat_mode(path)
        if mode is None:
            continue
        if stat.S_ISLNK(mode):
            raise InstallError(f"{label} is a symbolic link: {path}")
        if not stat.S_ISDIR(mode):
            raise InstallError(f"expected a directory: {path}")
    for name, target in targets.items():
        _validate_managed_target(name, target)
    hooks_path = codex_home / "hooks.json"
    hooks_mode = _lstat_mode(hooks_path)
    if hooks_mode is not None:
        if stat.S_ISLNK(hooks_mode):
            raise InstallError(f"hooks file is a symbolic link: {hooks_path}")
        if not stat.S_ISREG(hooks_mode):
            raise InstallError(f"hooks file must be a regular file: {hooks_path}")
    _validate_private_control_entry(
        codex_home / _LOCK_NAME, "bundle lock file"
    )
    _validate_private_control_entry(
        codex_home / _JOURNAL_NAME, "transaction journal"
    )


def _make_directory(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not _path_exists(current):
        missing.append(current)
        current = current.parent
    current_mode = _lstat_mode(current)
    if current_mode is None or not stat.S_ISDIR(current_mode):
        raise InstallError(f"expected a directory: {current}")
    for directory in reversed(missing):
        created = False
        try:
            directory.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        directory_mode = _lstat_mode(directory)
        if directory_mode is None or not stat.S_ISDIR(directory_mode):
            if directory_mode is not None and stat.S_ISLNK(directory_mode):
                raise InstallError(
                    f"directory creation encountered a symbolic link: {directory}"
                )
            raise InstallError(f"expected a directory: {directory}")
        if created:
            directory.chmod(0o700)
            _fsync_directory(directory)
            _fsync_directory(directory.parent)
    path_mode = _lstat_mode(path)
    if path_mode is None or not stat.S_ISDIR(path_mode):
        raise InstallError(f"expected a directory: {path}")


def _apply_private_modes(root: Path) -> None:
    root_mode = _lstat_mode(root)
    if root_mode is None:
        raise InstallError(f"missing staged path: {root}")
    if stat.S_ISREG(root_mode):
        root.chmod(0o700 if stat.S_IMODE(root_mode) & 0o111 else 0o600)
        return
    if not stat.S_ISDIR(root_mode):
        raise InstallError(
            f"staged path contains {_mode_description(root_mode)}: {root}"
        )
    root.chmod(0o700)
    for path, mode in _tree_entries(root, "staged skill"):
        if stat.S_ISDIR(mode):
            path.chmod(0o700)
        elif stat.S_ISREG(mode):
            path.chmod(0o700 if stat.S_IMODE(mode) & 0o111 else 0o600)
        else:  # pragma: no cover - source validation precedes staged copying
            raise InstallError(
                f"staged skill contains {_mode_description(mode)}: {path}"
            )


def _fsync_tree(root: Path) -> None:
    mode = _lstat_mode(root)
    if mode is None:
        raise InstallError(f"missing path to fsync: {root}")
    if stat.S_ISREG(mode):
        _fsync_regular_file(root)
        return
    if not stat.S_ISDIR(mode):
        raise InstallError(
            f"cannot fsync {_mode_description(mode)}: {root}"
        )
    entries = _tree_entries(root, "durable tree")
    for path, entry_mode in entries:
        if stat.S_ISREG(entry_mode):
            _fsync_regular_file(path)
        elif not stat.S_ISDIR(entry_mode):
            raise InstallError(
                f"durable tree contains {_mode_description(entry_mode)}: {path}"
            )
    directories = [
        path for path, entry_mode in entries if stat.S_ISDIR(entry_mode)
    ]
    directories.sort(key=lambda candidate: len(candidate.parts), reverse=True)
    for directory in directories:
        _fsync_directory(directory)
    _fsync_directory(root)


def _load_hooks(path: Path) -> dict[str, Any]:
    mode = _lstat_mode(path)
    if mode is None:
        return {}
    if not stat.S_ISREG(mode):
        raise InstallError(f"hooks file must be a regular file: {path}")
    try:
        return _as_object(json.loads(path.read_text(encoding="utf-8")), "hooks file")
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise InstallError(f"cannot read hooks file {path}: {error}") from error


def _next_backup_root(codex_home: Path) -> Path:
    parent = codex_home / "backups" / "agent-work-boundaries"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = parent / timestamp
    suffix = 1
    while _path_exists(candidate):
        candidate = parent / f"{timestamp}-{suffix:02d}"
        suffix += 1
    return candidate


def _create_backup_root(codex_home: Path) -> Path:
    parent = codex_home / "backups" / "agent-work-boundaries"
    _make_directory(parent)
    while True:
        candidate = _next_backup_root(codex_home)
        try:
            candidate.mkdir(mode=0o700)
            candidate.chmod(0o700)
            _fsync_directory(candidate)
            _fsync_directory(candidate.parent)
            return candidate
        except FileExistsError:
            continue


def _files_below(path: Path) -> list[Path]:
    mode = _lstat_mode(path)
    if mode is None:
        return []
    if stat.S_ISREG(mode):
        return [path]
    if not stat.S_ISDIR(mode):
        raise InstallError(
            f"backup source contains {_mode_description(mode)}: {path}"
        )
    files: list[Path] = []
    for candidate, candidate_mode in _tree_entries(path, "backup source"):
        if stat.S_ISREG(candidate_mode):
            files.append(candidate)
        elif not stat.S_ISDIR(candidate_mode):
            raise InstallError(
                "backup source contains "
                f"{_mode_description(candidate_mode)}: {candidate}"
            )
    return sorted(files)


def _mapped_backup_files(source: Path, destination: Path) -> list[Path]:
    mode = _lstat_mode(source)
    if mode is not None and stat.S_ISREG(mode):
        return [destination]
    return [
        destination / path.relative_to(source)
        for path in _files_below(source)
    ]


def _remove_path(path: Path) -> None:
    mode = _lstat_mode(path)
    if mode is None:
        return
    if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        raise InstallError(
            f"refusing to remove {_mode_description(mode)}: {path}"
        )
    if stat.S_ISDIR(mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _remove_path_and_fsync(path: Path) -> None:
    if not _path_exists(path):
        return
    _remove_path(path)
    _fsync_directory(path.parent)


def _ignore_python_runtime_artifacts(
    directory: str, names: list[str]
) -> list[str]:
    ignored: list[str] = []
    for name in names:
        mode = _lstat_mode(Path(directory) / name)
        if name == "__pycache__" and mode is not None and stat.S_ISDIR(mode):
            ignored.append(name)
        elif (
            name.endswith((".pyc", ".pyo"))
            and mode is not None
            and stat.S_ISREG(mode)
        ):
            ignored.append(name)
    return sorted(ignored)


def _skill_stage(codex_home: Path, name: str, transaction_id: str) -> Path:
    return codex_home / "skills" / f".{name}.stage-{transaction_id}"


def _skill_retired(codex_home: Path, name: str, transaction_id: str) -> Path:
    return codex_home / "skills" / f".{name}.retired-{transaction_id}"


def _skill_recovery(codex_home: Path, name: str, transaction_id: str) -> Path:
    return codex_home / "skills" / f".{name}.recovery-{transaction_id}"


def _hooks_stage(codex_home: Path, transaction_id: str) -> Path:
    return codex_home / f".hooks.json.stage-{transaction_id}"


def _hooks_recovery(codex_home: Path, transaction_id: str) -> Path:
    return codex_home / f".hooks.json.recovery-{transaction_id}"


def _stage_skill(source: Path, target: Path, stage: Path) -> Path:
    if _path_exists(stage):
        raise InstallError(f"staged skill path already exists: {stage}")
    try:
        shutil.copytree(source, stage, ignore=_ignore_python_runtime_artifacts)
        _apply_private_modes(stage)
        _fsync_tree(stage)
        _fsync_directory(target.parent)
    except BaseException:
        if _path_exists(stage):
            _remove_path_and_fsync(stage)
        raise
    return stage


def _stage_hooks(path: Path, document: dict[str, Any], stage: Path) -> Path:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(stage, flags, 0o600)
    except OSError as error:
        raise InstallError(f"cannot create hooks stage {stage}: {error}") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        stage.chmod(0o600)
        _fsync_regular_file(stage)
        _fsync_directory(path.parent)
    except BaseException:
        if _path_exists(stage):
            _remove_path_and_fsync(stage)
        raise
    return stage


def _copy_path_durable(source: Path, destination: Path) -> None:
    if _path_exists(destination):
        raise InstallError(f"backup destination already exists: {destination}")
    _make_directory(destination.parent)
    mode = _lstat_mode(source)
    if mode is None:
        raise InstallError(f"backup source disappeared: {source}")
    try:
        if stat.S_ISDIR(mode):
            shutil.copytree(source, destination)
        elif stat.S_ISREG(mode):
            shutil.copy2(source, destination)
        else:
            raise InstallError(
                f"backup source contains {_mode_description(mode)}: {source}"
            )
        _fsync_tree(destination)
        _fsync_directory(destination.parent)
    except BaseException:
        if _path_exists(destination):
            _remove_path_and_fsync(destination)
        raise


def _installation_plan(
    source_root: Path, codex_home: Path
) -> tuple[dict[str, Path], dict[str, Path], Path, dict[str, Any]]:
    sources = _validate_install_sources(source_root)
    skills_dir = codex_home / "skills"
    targets = {name: skills_dir / name for name in _SKILL_NAMES}
    _validate_managed_paths(codex_home, targets)
    hooks_path = codex_home / "hooks.json"
    existing_hooks = _load_hooks(hooks_path)
    managed_hooks = render_managed_hooks(
        targets["project-handoff"] / "scripts" / "handoff_hook.py"
    )
    try:
        merged_hooks = merge_hooks(existing_hooks, managed_hooks)
    except ValueError as error:
        raise InstallError(f"cannot merge hooks file {hooks_path}: {error}") from error
    return sources, targets, hooks_path, merged_hooks


def _validate_install_sources(source_root: Path) -> dict[str, Path]:
    sources = {name: source_root / "skills" / name for name in _SKILL_NAMES}
    for source in sources.values():
        _validate_source_tree(source)
    return sources


def _validate_home_and_controls_for_lock(codex_home: Path) -> None:
    _assert_no_symlink_components(codex_home, "CODEX_HOME path")
    home_mode = _lstat_mode(codex_home)
    if home_mode is not None and not stat.S_ISDIR(home_mode):
        raise InstallError(f"expected a directory: {codex_home}")
    _validate_private_control_entry(
        codex_home / _LOCK_NAME, "bundle lock file"
    )
    _validate_private_control_entry(
        codex_home / _JOURNAL_NAME, "transaction journal"
    )


def _uninstallation_plan(
    codex_home: Path,
) -> tuple[
    dict[str, Path],
    Path,
    dict[str, Any],
    bool,
    tuple[Path, ...],
]:
    targets = {
        name: codex_home / "skills" / name for name in _SKILL_NAMES
    }
    _validate_managed_paths(codex_home, targets)
    hooks_path = codex_home / "hooks.json"
    existing_hooks = _load_hooks(hooks_path)
    installed_hook = (
        targets["project-handoff"] / "scripts" / "handoff_hook.py"
    )
    try:
        remaining_hooks = remove_managed_hooks(existing_hooks, installed_hook)
    except ValueError as error:
        raise InstallError(
            f"cannot update hooks file {hooks_path}: {error}"
        ) from error
    hooks_changed = _path_exists(hooks_path) and remaining_hooks != existing_hooks
    changed_paths = tuple(
        [
            *(target for target in targets.values() if _path_exists(target)),
            *([hooks_path] if hooks_changed else []),
        ]
    )
    return targets, hooks_path, remaining_hooks, hooks_changed, changed_paths


def _planned_backup(
    codex_home: Path, targets: dict[str, Path], hooks_path: Path
) -> tuple[Path | None, tuple[Path, ...]]:
    existing_targets = {
        name: target for name, target in targets.items() if _path_exists(target)
    }
    if not existing_targets and not _path_exists(hooks_path):
        return None, ()
    backup_root = _next_backup_root(codex_home)
    files: list[Path] = []
    for name, target in existing_targets.items():
        files.extend(
            _mapped_backup_files(target, backup_root / "skills" / name)
        )
    if _path_exists(hooks_path):
        files.append(backup_root / "hooks.json")
    return backup_root, tuple(files)


@contextmanager
def _bundle_lock(codex_home: Path):
    """Hold the persistent per-bundle lock without following links."""

    lock_path = codex_home / _LOCK_NAME
    _assert_no_symlink_components(codex_home, "CODEX_HOME path")
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    for attempt in range(3):
        created = False
        try:
            descriptor = os.open(
                lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600
            )
            created = True
        except FileExistsError:
            try:
                descriptor = os.open(lock_path, flags)
            except OSError as error:
                raise InstallError(
                    f"cannot open bundle lock file {lock_path}: {error}"
                ) from error
        except OSError as error:
            raise InstallError(
                f"cannot create bundle lock file {lock_path}: {error}"
            ) from error

        try:
            if created:
                os.fchmod(descriptor, 0o600)
            opened_identity = _fstat_control_identity(
                descriptor, lock_path, "bundle lock file"
            )
            if created:
                os.fsync(descriptor)
                _fsync_directory(codex_home)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    raise InstallError(
                        "another Agent Work Boundaries install or uninstall "
                        "operation is running"
                    ) from error
                raise InstallError(
                    f"cannot acquire bundle lock {lock_path}: {error}"
                ) from error
            try:
                current_identity = _lstat_control_identity(
                    lock_path, "bundle lock file"
                )
                if current_identity != opened_identity:
                    if attempt == 2:
                        raise InstallError(
                            f"bundle lock file changed while acquiring: {lock_path}"
                        )
                    continue
                if (
                    _fstat_control_identity(
                        descriptor, lock_path, "bundle lock file"
                    )
                    != opened_identity
                ):
                    raise InstallError(
                        f"bundle lock file changed while acquiring: {lock_path}"
                    )
                yield
                return
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    raise InstallError(f"bundle lock file changed repeatedly: {lock_path}")


def _journal_path(codex_home: Path) -> Path:
    return codex_home / _JOURNAL_NAME


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise InstallError(f"transaction journal has invalid {label} fields")


def _validate_journal_document(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InstallError("transaction journal must contain a JSON object")
    _exact_keys(
        value,
        {"version", "bundle", "action", "transaction_id", "backup_id", "skills", "hooks"},
        "top-level",
    )
    if type(value["version"]) is not int or value["version"] != 1:
        raise InstallError("transaction journal version is unsupported")
    if value["bundle"] != _BUNDLE_NAME:
        raise InstallError("transaction journal bundle identifier is invalid")
    if value["action"] not in {"install", "uninstall"}:
        raise InstallError("transaction journal action is invalid")
    transaction_id = value["transaction_id"]
    if not isinstance(transaction_id, str) or not _TRANSACTION_ID.fullmatch(
        transaction_id
    ):
        raise InstallError("transaction journal identifier is invalid")
    backup_id = value["backup_id"]
    if backup_id is not None and (
        not isinstance(backup_id, str) or not _BACKUP_ID.fullmatch(backup_id)
    ):
        raise InstallError("transaction journal backup identifier is invalid")

    skills = value["skills"]
    if not isinstance(skills, dict):
        raise InstallError("transaction journal skills field is invalid")
    _exact_keys(skills, set(_SKILL_NAMES), "skills")
    any_original = False
    expected_install = value["action"] == "install"
    for name in _SKILL_NAMES:
        entry = skills[name]
        if not isinstance(entry, dict):
            raise InstallError(f"transaction journal skill {name} is invalid")
        _exact_keys(entry, {"original", "install"}, f"skill {name}")
        if type(entry["original"]) is not bool or type(entry["install"]) is not bool:
            raise InstallError(f"transaction journal skill {name} flags are invalid")
        if entry["install"] != expected_install:
            raise InstallError(f"transaction journal skill {name} intent is invalid")
        any_original = any_original or entry["original"]

    hooks = value["hooks"]
    if not isinstance(hooks, dict):
        raise InstallError("transaction journal hooks field is invalid")
    _exact_keys(hooks, {"original", "replace"}, "hooks")
    if type(hooks["original"]) is not bool or type(hooks["replace"]) is not bool:
        raise InstallError("transaction journal hook flags are invalid")
    if hooks["original"] and not hooks["replace"]:
        raise InstallError("transaction journal hook intent is invalid")
    if expected_install and not hooks["replace"]:
        raise InstallError("install transaction must replace hooks")
    any_original = any_original or hooks["original"]
    if any_original != (backup_id is not None):
        raise InstallError("transaction journal backup intent is inconsistent")
    return value


def _read_journal_with_identity(
    codex_home: Path,
) -> tuple[dict[str, Any], _ControlIdentity] | None:
    path = _journal_path(codex_home)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise InstallError(f"cannot open transaction journal {path}: {error}") from error
    try:
        identity = _fstat_control_identity(
            descriptor, path, "transaction journal"
        )
        _require_control_identity(path, "transaction journal", identity)
        payload = os.read(descriptor, 65537)
        if len(payload) > 65536:
            raise InstallError("transaction journal is too large")
        if (
            _fstat_control_identity(descriptor, path, "transaction journal")
            != identity
        ):
            raise InstallError("transaction journal changed while reading")
        _require_control_identity(path, "transaction journal", identity)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InstallError(f"transaction journal is corrupt: {error}") from error
    return _validate_journal_document(value), identity


def _read_journal(codex_home: Path) -> dict[str, Any] | None:
    result = _read_journal_with_identity(codex_home)
    return None if result is None else result[0]


def _write_journal(codex_home: Path, document: dict[str, Any]) -> None:
    document = _validate_journal_document(document)
    journal = _journal_path(codex_home)
    if _path_exists(journal):
        raise InstallError(f"transaction journal already exists: {journal}")
    temporary = codex_home / f"{_JOURNAL_NAME}.tmp-{document['transaction_id']}"
    if _path_exists(temporary):
        raise InstallError(f"transaction journal stage already exists: {temporary}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    identity: _ControlIdentity | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        identity = _fstat_control_identity(
            descriptor, temporary, "transaction journal stage"
        )
        payload = (
            json.dumps(document, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:  # pragma: no cover - regular-file writes progress
                raise InstallError("transaction journal write made no progress")
            offset += written
        os.fsync(descriptor)
        if (
            _fstat_control_identity(
                descriptor, temporary, "transaction journal stage"
            )
            != identity
        ):
            raise InstallError("transaction journal stage changed while writing")
        _require_control_identity(
            temporary, "transaction journal stage", identity
        )
        _fsync_directory(codex_home)
        _replace_and_fsync(temporary, journal)
        if (
            _fstat_control_identity(
                descriptor, journal, "transaction journal"
            )
            != identity
        ):
            raise InstallError("transaction journal changed while publishing")
        _require_control_identity(journal, "transaction journal", identity)
    except BaseException:
        if identity is not None:
            try:
                _require_control_identity(
                    temporary, "transaction journal stage", identity
                )
            except InstallError:
                pass
            else:
                _unlink_control_and_fsync(
                    temporary, "transaction journal stage", identity
                )
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _clear_journal(codex_home: Path) -> None:
    journal = _journal_path(codex_home)
    result = _read_journal_with_identity(codex_home)
    if result is None:
        return
    document, identity = result
    try:
        _unlink_control_and_fsync(
            journal, "transaction journal", identity
        )
    except Exception as error:
        if not _path_exists(journal):
            try:
                _write_journal(codex_home, document)
            except Exception as restore_error:
                raise InstallError(
                    "transaction journal removal was not durable and its "
                    f"recovery record could not be reinstated: {restore_error}"
                ) from error
        raise


def _backup_root_from_document(
    codex_home: Path, document: dict[str, Any]
) -> Path | None:
    backup_id = document["backup_id"]
    if backup_id is None:
        return None
    return codex_home / "backups" / _BUNDLE_NAME / backup_id


def _validate_transaction_artifacts(
    codex_home: Path, document: dict[str, Any]
) -> None:
    transaction_id = document["transaction_id"]
    backup_root = _backup_root_from_document(codex_home, document)
    if backup_root is not None:
        _assert_no_symlink_components(backup_root, "transaction backup path")
        mode = _lstat_mode(backup_root)
        if mode is None or not stat.S_ISDIR(mode):
            raise InstallError("transaction journal backup directory is missing")
    for name in _SKILL_NAMES:
        entry = document["skills"][name]
        if entry["original"]:
            assert backup_root is not None
            backup = backup_root / "skills" / name
            mode = _lstat_mode(backup)
            if mode is None:
                raise InstallError(f"transaction backup is missing for {name}")
            if stat.S_ISDIR(mode):
                _validate_regular_tree(backup, f"transaction backup {name}")
            elif not stat.S_ISREG(mode):
                raise InstallError(f"transaction backup is invalid for {name}")
        for path, label in (
            (_skill_stage(codex_home, name, transaction_id), "stage"),
            (_skill_retired(codex_home, name, transaction_id), "retired path"),
            (_skill_recovery(codex_home, name, transaction_id), "recovery path"),
        ):
            mode = _lstat_mode(path)
            if mode is None:
                continue
            if stat.S_ISDIR(mode):
                _validate_regular_tree(path, f"transaction {label} {name}")
            elif not stat.S_ISREG(mode):
                raise InstallError(
                    f"transaction journal points to invalid {label} for {name}"
                )
    if document["hooks"]["original"]:
        assert backup_root is not None
        backup_hooks = backup_root / "hooks.json"
        if not stat.S_ISREG(_lstat_mode(backup_hooks) or 0):
            raise InstallError("transaction hooks backup is missing or invalid")
    for path, label in (
        (_hooks_stage(codex_home, transaction_id), "hooks stage"),
        (_hooks_recovery(codex_home, transaction_id), "hooks recovery path"),
    ):
        mode = _lstat_mode(path)
        if mode is not None and not stat.S_ISREG(mode):
            raise InstallError(f"transaction {label} is invalid")


def _restore_backup(
    source: Path, target: Path, recovery: Path
) -> None:
    if _path_exists(recovery):
        _remove_path_and_fsync(recovery)
    _copy_path_durable(source, recovery)
    if _path_exists(target):
        _remove_path_and_fsync(target)
    _replace_and_fsync(recovery, target)


def _recover_document(codex_home: Path, document: dict[str, Any]) -> None:
    _validate_transaction_artifacts(codex_home, document)
    transaction_id = document["transaction_id"]
    backup_root = _backup_root_from_document(codex_home, document)
    targets = {
        name: codex_home / "skills" / name for name in _SKILL_NAMES
    }
    for name in _SKILL_NAMES:
        entry = document["skills"][name]
        target = targets[name]
        retired = _skill_retired(codex_home, name, transaction_id)
        stage = _skill_stage(codex_home, name, transaction_id)
        recovery = _skill_recovery(codex_home, name, transaction_id)
        if entry["original"]:
            assert backup_root is not None
            backup = backup_root / "skills" / name
            if _path_exists(retired):
                if _path_exists(target):
                    _remove_path_and_fsync(target)
                _replace_and_fsync(retired, target)
            else:
                _restore_backup(backup, target, recovery)
        else:
            if _path_exists(target):
                _remove_path_and_fsync(target)
        for path in (stage, retired, recovery):
            if _path_exists(path):
                _remove_path_and_fsync(path)

    hooks = document["hooks"]
    hooks_path = codex_home / "hooks.json"
    hooks_stage = _hooks_stage(codex_home, transaction_id)
    hooks_recovery = _hooks_recovery(codex_home, transaction_id)
    if hooks["replace"]:
        if hooks["original"]:
            assert backup_root is not None
            _restore_backup(
                backup_root / "hooks.json", hooks_path, hooks_recovery
            )
        elif _path_exists(hooks_path):
            _remove_path_and_fsync(hooks_path)
    for path in (hooks_stage, hooks_recovery):
        if _path_exists(path):
            _remove_path_and_fsync(path)


def _recover_unfinished_transaction(codex_home: Path) -> None:
    document = _read_journal(codex_home)
    if document is None:
        return
    _recover_document(codex_home, document)
    _clear_journal(codex_home)


def _transaction_document(
    action: str,
    transaction_id: str,
    backup_root: Path | None,
    skill_originals: dict[str, bool],
    hooks_original: bool,
    hooks_replace: bool,
) -> dict[str, Any]:
    return {
        "version": 1,
        "bundle": _BUNDLE_NAME,
        "action": action,
        "transaction_id": transaction_id,
        "backup_id": backup_root.name if backup_root is not None else None,
        "skills": {
            name: {
                "original": skill_originals[name],
                "install": action == "install",
            }
            for name in _SKILL_NAMES
        },
        "hooks": {"original": hooks_original, "replace": hooks_replace},
    }


def _cleanup_pretransaction(
    codex_home: Path,
    transaction_id: str,
    backup_root: Path | None,
) -> None:
    for name in _SKILL_NAMES:
        for path in (
            _skill_stage(codex_home, name, transaction_id),
            _skill_retired(codex_home, name, transaction_id),
            _skill_recovery(codex_home, name, transaction_id),
        ):
            if _path_exists(path):
                _remove_path_and_fsync(path)
    for path in (
        _hooks_stage(codex_home, transaction_id),
        _hooks_recovery(codex_home, transaction_id),
    ):
        if _path_exists(path):
            _remove_path_and_fsync(path)
    if backup_root is not None and _path_exists(backup_root):
        _remove_path_and_fsync(backup_root)


def _prepare_transaction(
    *,
    action: str,
    codex_home: Path,
    sources: dict[str, Path] | None,
    targets: dict[str, Path],
    hooks_path: Path,
    hooks_document: dict[str, Any],
    hooks_replace: bool,
) -> tuple[dict[str, Any], tuple[Path, ...]]:
    transaction_id = uuid.uuid4().hex
    skill_originals = {
        name: _path_exists(target) for name, target in targets.items()
    }
    hooks_original = hooks_replace and _path_exists(hooks_path)
    needs_backup = hooks_original or any(skill_originals.values())
    backup_root: Path | None = None
    try:
        if action == "install":
            assert sources is not None
            for name in _SKILL_NAMES:
                _stage_skill(
                    sources[name],
                    targets[name],
                    _skill_stage(codex_home, name, transaction_id),
                )
        if hooks_replace:
            _stage_hooks(
                hooks_path,
                hooks_document,
                _hooks_stage(codex_home, transaction_id),
            )

        if needs_backup:
            backup_root = _create_backup_root(codex_home)
            if any(skill_originals.values()):
                _make_directory(backup_root / "skills")
            for name in _SKILL_NAMES:
                if skill_originals[name]:
                    _copy_path_durable(
                        targets[name], backup_root / "skills" / name
                    )
            if hooks_original:
                _copy_path_durable(hooks_path, backup_root / "hooks.json")
            _fsync_tree(backup_root)
            _fsync_directory(backup_root.parent)

        document = _transaction_document(
            action,
            transaction_id,
            backup_root,
            skill_originals,
            hooks_original,
            hooks_replace,
        )
        _write_journal(codex_home, document)
    except BaseException as error:
        journal_exists = _path_exists(_journal_path(codex_home))
        rollback_error: Exception | None = None
        if isinstance(error, Exception) and journal_exists:
            try:
                _recover_unfinished_transaction(codex_home)
            except Exception as caught:
                rollback_error = caught
        elif not journal_exists:
            _cleanup_pretransaction(codex_home, transaction_id, backup_root)
        if isinstance(error, Exception):
            detail = f"{action} preparation failed: {error}"
            if rollback_error is not None:
                detail += (
                    "; rollback failed and journal retained: "
                    f"{rollback_error}"
                )
            raise InstallError(detail) from error
        raise

    backed_up_files: tuple[Path, ...] = ()
    if backup_root is not None:
        _, backed_up_files = _planned_backup_from_root(
            backup_root,
            {
                name: backup_root / "skills" / name
                for name in _SKILL_NAMES
            },
            backup_root / "hooks.json",
        )
    return document, backed_up_files


def _execute_transaction(codex_home: Path, document: dict[str, Any]) -> None:
    transaction_id = document["transaction_id"]
    targets = {
        name: codex_home / "skills" / name for name in _SKILL_NAMES
    }
    for name in _SKILL_NAMES:
        entry = document["skills"][name]
        target = targets[name]
        retired = _skill_retired(codex_home, name, transaction_id)
        if entry["original"]:
            _replace_and_fsync(target, retired)
        if entry["install"]:
            _replace_and_fsync(
                _skill_stage(codex_home, name, transaction_id), target
            )

    if document["hooks"]["replace"]:
        _replace_and_fsync(
            _hooks_stage(codex_home, transaction_id),
            codex_home / "hooks.json",
        )

    for name in _SKILL_NAMES:
        retired = _skill_retired(codex_home, name, transaction_id)
        if _path_exists(retired):
            _remove_path_and_fsync(retired)


def _run_transaction(codex_home: Path, document: dict[str, Any]) -> None:
    try:
        _execute_transaction(codex_home, document)
    except Exception as error:
        rollback_error: Exception | None = None
        try:
            _recover_unfinished_transaction(codex_home)
        except Exception as caught:
            rollback_error = caught
        detail = f"{document['action']} failed: {error}"
        if rollback_error is not None:
            detail += f"; rollback failed and journal retained: {rollback_error}"
        raise InstallError(detail) from error
    try:
        _clear_journal(codex_home)
    except Exception as error:
        raise InstallError(
            f"{document['action']} committed but journal cleanup failed: {error}"
        ) from error


def install_bundle(
    source_root: Path, codex_home: Path, dry_run: bool
) -> InstallReport:
    """Install both skills and merge the managed hooks into a Codex home."""

    source_root = Path(source_root).absolute()
    codex_home = Path(codex_home).absolute()
    if dry_run:
        sources, targets, hooks_path, merged_hooks = _installation_plan(
            source_root, codex_home
        )
        changed_paths = tuple([*targets.values(), hooks_path])
        planned_root, planned_files = _planned_backup(
            codex_home, targets, hooks_path
        )
        if _path_exists(_journal_path(codex_home)):
            _read_journal(codex_home)
            raise InstallError(
                "unfinished transaction journal requires a non-dry-run recovery"
            )
        return InstallReport(
            action="install",
            changed_paths=changed_paths,
            backed_up_files=planned_files,
            backup_root=planned_root,
            dry_run=True,
        )

    if not _path_exists(codex_home):
        _validate_install_sources(source_root)
    _validate_home_and_controls_for_lock(codex_home)
    _make_directory(codex_home)
    with _bundle_lock(codex_home):
        _recover_unfinished_transaction(codex_home)
        sources, targets, hooks_path, merged_hooks = _installation_plan(
            source_root, codex_home
        )
        _make_directory(codex_home / "skills")
        changed_paths = tuple([*targets.values(), hooks_path])
        document, backed_up_files = _prepare_transaction(
            action="install",
            codex_home=codex_home,
            sources=sources,
            targets=targets,
            hooks_path=hooks_path,
            hooks_document=merged_hooks,
            hooks_replace=True,
        )
        backup_root = _backup_root_from_document(codex_home, document)
        _run_transaction(codex_home, document)

    return InstallReport(
        action="install",
        changed_paths=changed_paths,
        backed_up_files=backed_up_files,
        backup_root=backup_root,
        dry_run=False,
    )


def _planned_backup_from_root(
    backup_root: Path, targets: dict[str, Path], hooks_path: Path
) -> tuple[Path, tuple[Path, ...]]:
    files: list[Path] = []
    for name, target in targets.items():
        if _path_exists(target):
            files.extend(
                _mapped_backup_files(target, backup_root / "skills" / name)
            )
    if _path_exists(hooks_path):
        files.append(backup_root / "hooks.json")
    return backup_root, tuple(files)


def uninstall_bundle(codex_home: Path, dry_run: bool) -> InstallReport:
    """Remove only bundle-managed skills and hooks, preserving handoff state."""

    codex_home = Path(codex_home).absolute()
    if dry_run:
        (
            targets,
            hooks_path,
            remaining_hooks,
            hooks_changed,
            changed_paths,
        ) = _uninstallation_plan(codex_home)
        needs_backup = bool(changed_paths)
        planned_root = _next_backup_root(codex_home) if needs_backup else None
        if planned_root is None:
            planned_files: tuple[Path, ...] = ()
        else:
            _, planned_files = _planned_backup_from_root(
                planned_root,
                targets,
                (
                    hooks_path
                    if hooks_changed
                    else codex_home / ".absent-hooks"
                ),
            )
        if _path_exists(_journal_path(codex_home)):
            _read_journal(codex_home)
            raise InstallError(
                "unfinished transaction journal requires a non-dry-run recovery"
            )
        return InstallReport(
            action="uninstall",
            changed_paths=changed_paths,
            backed_up_files=planned_files,
            backup_root=planned_root,
            dry_run=dry_run,
        )

    _validate_home_and_controls_for_lock(codex_home)
    _make_directory(codex_home)
    with _bundle_lock(codex_home):
        _recover_unfinished_transaction(codex_home)
        (
            targets,
            hooks_path,
            remaining_hooks,
            hooks_changed,
            changed_paths,
        ) = _uninstallation_plan(codex_home)
        if not changed_paths:
            return InstallReport(
                action="uninstall",
                changed_paths=(),
                backed_up_files=(),
                backup_root=None,
                dry_run=False,
            )
        document, backed_up_files = _prepare_transaction(
            action="uninstall",
            codex_home=codex_home,
            sources=None,
            targets=targets,
            hooks_path=hooks_path,
            hooks_document=remaining_hooks,
            hooks_replace=hooks_changed,
        )
        backup_root = _backup_root_from_document(codex_home, document)
        _run_transaction(codex_home, document)

    return InstallReport(
        action="uninstall",
        changed_paths=changed_paths,
        backed_up_files=backed_up_files,
        backup_root=backup_root,
        dry_run=False,
    )


def _report_document(report: InstallReport) -> dict[str, Any]:
    return {
        "action": report.action,
        "changed_paths": [str(path) for path in report.changed_paths],
        "backed_up_files": [str(path) for path in report.backed_up_files],
        "backup_root": (
            str(report.backup_root) if report.backup_root is not None else None
        ),
        "dry_run": report.dry_run,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install or uninstall the Agent Work Boundaries bundle."
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser(),
        help="Codex configuration directory (default: CODEX_HOME or ~/.codex)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and report changes without modifying files",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="remove the two managed Skills and their Hook handlers",
    )
    arguments = parser.parse_args(argv)
    codex_home = arguments.codex_home.expanduser()
    action = "uninstall" if arguments.uninstall else "install"
    try:
        if arguments.uninstall:
            report = uninstall_bundle(codex_home, arguments.dry_run)
        else:
            source_root = Path(__file__).resolve().parents[1]
            report = install_bundle(source_root, codex_home, arguments.dry_run)
    except InstallError as error:
        json.dump(
            {
                "action": action,
                "dry_run": arguments.dry_run,
                "error": str(error),
            },
            sys.stderr,
            ensure_ascii=False,
        )
        sys.stderr.write("\n")
        return 1

    json.dump(_report_document(report), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
