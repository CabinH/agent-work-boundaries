"""Install-time rendering and merging for Agent Work Boundaries hooks."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
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


class InstallError(RuntimeError):
    """Raised when the bundle cannot be installed without risking user data."""


@dataclass(frozen=True)
class InstallReport:
    action: str
    changed_paths: Sequence[Path]
    backed_up_files: Sequence[Path]
    backup_root: Path | None
    dry_run: bool


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
                if not isinstance(handler.get("command"), str):
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
            if not predicate(_command_target(handler["command"]))
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


def _is_managed_install_target(target: Path | None) -> bool:
    if target is None:
        return False
    return target.parts[-4:] == (
        "skills",
        "project-handoff",
        "scripts",
        "handoff_hook.py",
    )


def remove_managed_hooks(existing: dict[str, Any]) -> dict[str, Any]:
    existing = _validate_hooks(existing, "existing hooks")
    result = copy.deepcopy(existing)
    if "hooks" not in result:
        return result

    for event, groups in result["hooks"].items():
        result["hooks"][event] = _without_matching_handlers(
            groups, _is_managed_install_target
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


def _validate_managed_paths(
    codex_home: Path, targets: dict[str, Path]
) -> None:
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
    if hooks_mode is None:
        return
    if stat.S_ISLNK(hooks_mode):
        raise InstallError(f"hooks file is a symbolic link: {hooks_path}")
    if not stat.S_ISREG(hooks_mode):
        raise InstallError(f"hooks file must be a regular file: {hooks_path}")


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
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
    path_mode = _lstat_mode(path)
    if path_mode is None or not stat.S_ISDIR(path_mode):
        raise InstallError(f"expected a directory: {path}")


def _apply_private_modes(root: Path) -> None:
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
    if not stat.S_ISDIR(mode):
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def _stage_skill(source: Path, target: Path) -> Path:
    stage = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=str(target.parent))
    )
    stage.rmdir()
    try:
        shutil.copytree(source, stage)
        _apply_private_modes(stage)
    except Exception:
        if _path_exists(stage):
            shutil.rmtree(stage)
        raise
    return stage


def _stage_hooks(path: Path, document: dict[str, Any]) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".hooks.json.stage-", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _installation_plan(
    source_root: Path, codex_home: Path
) -> tuple[dict[str, Path], dict[str, Path], Path, dict[str, Any]]:
    sources = {name: source_root / "skills" / name for name in _SKILL_NAMES}
    for source in sources.values():
        _validate_source_tree(source)

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


def _rollback_install(
    targets: dict[str, Path],
    installed_names: set[str],
    moved_originals: dict[str, Path],
    hooks_path: Path,
    hooks_replaced: bool,
    hooks_existed: bool,
    backup_hooks: Path | None,
) -> list[str]:
    failures: list[str] = []
    if hooks_replaced:
        try:
            if hooks_existed:
                assert backup_hooks is not None
                restore_stage = _stage_file_copy(backup_hooks, hooks_path)
                os.replace(restore_stage, hooks_path)
            else:
                hooks_path.unlink(missing_ok=True)
        except Exception as error:  # pragma: no cover - exceptional recovery path
            failures.append(f"hooks: {error}")
    for name in reversed(_SKILL_NAMES):
        target = targets[name]
        try:
            if name in installed_names:
                _remove_path(target)
            backup = moved_originals.get(name)
            if backup is not None and _path_exists(backup):
                os.replace(backup, target)
        except Exception as error:  # pragma: no cover - exceptional recovery path
            failures.append(f"{name}: {error}")
    return failures


def _stage_file_copy(source: Path, destination: Path) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.restore-", dir=str(destination.parent)
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def install_bundle(
    source_root: Path, codex_home: Path, dry_run: bool
) -> InstallReport:
    """Install both skills and merge the managed hooks into a Codex home."""

    source_root = Path(source_root).absolute()
    codex_home = Path(codex_home).absolute()
    sources, targets, hooks_path, merged_hooks = _installation_plan(
        source_root, codex_home
    )
    changed_paths = tuple([*targets.values(), hooks_path])
    planned_root, planned_files = _planned_backup(codex_home, targets, hooks_path)
    if dry_run:
        return InstallReport(
            action="install",
            changed_paths=changed_paths,
            backed_up_files=planned_files,
            backup_root=planned_root,
            dry_run=True,
        )

    _make_directory(codex_home)
    _make_directory(codex_home / "skills")

    stages: dict[str, Path] = {}
    hooks_stage: Path | None = None
    moved_originals: dict[str, Path] = {}
    installed_names: set[str] = set()
    hooks_replaced = False
    hooks_existed = _path_exists(hooks_path)
    backup_root: Path | None = None
    backup_hooks: Path | None = None
    backed_up_files: tuple[Path, ...] = ()
    try:
        for name, source in sources.items():
            stages[name] = _stage_skill(source, targets[name])
        hooks_stage = _stage_hooks(hooks_path, merged_hooks)

        needs_backup = hooks_existed or any(
            _path_exists(target) for target in targets.values()
        )
        backup_root = _create_backup_root(codex_home) if needs_backup else None
        if backup_root is not None:
            _make_directory(backup_root / "skills")
            _, backed_up_files = _planned_backup_from_root(
                backup_root, targets, hooks_path
            )
        if hooks_existed:
            assert backup_root is not None
            backup_hooks = backup_root / "hooks.json"
            shutil.copy2(hooks_path, backup_hooks)

        for name, target in targets.items():
            if _path_exists(target):
                assert backup_root is not None
                backup_target = backup_root / "skills" / name
                os.replace(target, backup_target)
                moved_originals[name] = backup_target
            stage = stages[name]
            os.replace(stage, target)
            stages.pop(name)
            installed_names.add(name)

        assert hooks_stage is not None
        os.replace(hooks_stage, hooks_path)
        hooks_stage = None
        hooks_replaced = True
    except Exception as error:
        for stage in stages.values():
            _remove_path(stage)
        if hooks_stage is not None:
            hooks_stage.unlink(missing_ok=True)
        rollback_failures = _rollback_install(
            targets,
            installed_names,
            moved_originals,
            hooks_path,
            hooks_replaced,
            hooks_existed,
            backup_hooks,
        )
        detail = f"installation failed: {error}"
        if rollback_failures:
            detail += f"; rollback failed: {'; '.join(rollback_failures)}"
        raise InstallError(detail) from error

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
    skills_dir = codex_home / "skills"
    targets = {name: skills_dir / name for name in _SKILL_NAMES}
    _validate_managed_paths(codex_home, targets)
    hooks_path = codex_home / "hooks.json"
    existing_hooks = _load_hooks(hooks_path)
    try:
        remaining_hooks = remove_managed_hooks(existing_hooks)
    except ValueError as error:
        raise InstallError(f"cannot update hooks file {hooks_path}: {error}") from error
    hooks_changed = _path_exists(hooks_path) and remaining_hooks != existing_hooks
    changed_paths = tuple(
        [
            *(target for target in targets.values() if _path_exists(target)),
            *([hooks_path] if hooks_changed else []),
        ]
    )
    needs_backup = bool(changed_paths)
    planned_root = _next_backup_root(codex_home) if needs_backup else None
    if planned_root is None:
        planned_files: tuple[Path, ...] = ()
    else:
        _, planned_files = _planned_backup_from_root(
            planned_root,
            targets,
            hooks_path if hooks_changed else codex_home / ".absent-hooks",
        )
    if dry_run or not needs_backup:
        return InstallReport(
            action="uninstall",
            changed_paths=changed_paths,
            backed_up_files=planned_files,
            backup_root=planned_root,
            dry_run=dry_run,
        )

    hooks_stage = _stage_hooks(hooks_path, remaining_hooks) if hooks_changed else None
    backup_root: Path | None = None
    backup_hooks: Path | None = None
    moved_originals: dict[str, Path] = {}
    hooks_replaced = False
    try:
        backup_root = _create_backup_root(codex_home)
        _make_directory(backup_root / "skills")
        _, backed_up_files = _planned_backup_from_root(
            backup_root,
            targets,
            hooks_path if hooks_changed else codex_home / ".absent-hooks",
        )
        if hooks_changed:
            backup_hooks = backup_root / "hooks.json"
            shutil.copy2(hooks_path, backup_hooks)
        for name, target in targets.items():
            if _path_exists(target):
                backup_target = backup_root / "skills" / name
                os.replace(target, backup_target)
                moved_originals[name] = backup_target
        if hooks_changed:
            assert hooks_stage is not None
            os.replace(hooks_stage, hooks_path)
            hooks_stage = None
            hooks_replaced = True
    except Exception as error:
        if hooks_stage is not None:
            hooks_stage.unlink(missing_ok=True)
        rollback_failures = _rollback_install(
            targets,
            set(),
            moved_originals,
            hooks_path,
            hooks_replaced,
            hooks_changed,
            backup_hooks,
        )
        detail = f"uninstall failed: {error}"
        if rollback_failures:
            detail += f"; rollback failed: {'; '.join(rollback_failures)}"
        raise InstallError(detail) from error

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
