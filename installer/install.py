"""Install-time rendering and merging for Agent Work Boundaries hooks."""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import stat
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
    return path.exists() or path.is_symlink()


def _assert_no_symlink_components(path: Path, label: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise InstallError(f"{label} contains a symbolic link: {current}")


def _validate_source_tree(source: Path) -> None:
    _assert_no_symlink_components(source, "skill source path")
    if not source.is_dir():
        raise InstallError(f"missing skill source: {source}")
    for path in source.rglob("*"):
        if path.is_symlink():
            raise InstallError(f"skill source contains a symbolic link: {path}")


def _validate_managed_paths(codex_home: Path) -> None:
    _assert_no_symlink_components(codex_home, "CODEX_HOME path")
    if _path_exists(codex_home) and not codex_home.is_dir():
        raise InstallError(f"expected a directory: {codex_home}")
    for path, label in (
        (codex_home / "skills", "skills directory"),
        (codex_home / "backups", "backup directory"),
        (
            codex_home / "backups" / "agent-work-boundaries",
            "bundle backup directory",
        ),
    ):
        if path.is_symlink():
            raise InstallError(f"{label} is a symbolic link: {path}")
        if path.exists() and not path.is_dir():
            raise InstallError(f"expected a directory: {path}")
    hooks_path = codex_home / "hooks.json"
    if hooks_path.is_symlink():
        raise InstallError(f"hooks file is a symbolic link: {hooks_path}")


def _make_directory(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not _path_exists(current):
        missing.append(current)
        current = current.parent
    if not current.is_dir():
        raise InstallError(f"expected a directory: {current}")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
    if not path.is_dir():
        raise InstallError(f"expected a directory: {path}")


def _apply_private_modes(root: Path) -> None:
    root.chmod(0o700)
    for path in root.rglob("*"):
        if path.is_dir():
            path.chmod(0o700)
        elif path.is_file():
            source_mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(0o700 if source_mode & 0o111 else 0o600)


def _load_hooks(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
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
    if path.is_symlink() or path.is_file():
        return [path]
    return sorted(
        candidate
        for candidate in path.rglob("*")
        if candidate.is_symlink() or candidate.is_file()
    )


def _mapped_backup_files(source: Path, destination: Path) -> list[Path]:
    if source.is_symlink() or source.is_file():
        return [destination]
    return [
        destination / path.relative_to(source)
        for path in _files_below(source)
    ]


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
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
        if stage.exists():
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
    _validate_managed_paths(codex_home)

    skills_dir = codex_home / "skills"
    targets = {name: skills_dir / name for name in _SKILL_NAMES}
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
    if not existing_targets and not hooks_path.exists():
        return None, ()
    backup_root = _next_backup_root(codex_home)
    files: list[Path] = []
    for name, target in existing_targets.items():
        files.extend(
            _mapped_backup_files(target, backup_root / "skills" / name)
        )
    if hooks_path.exists():
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
    hooks_existed = hooks_path.exists()
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
    if hooks_path.exists():
        files.append(backup_root / "hooks.json")
    return backup_root, tuple(files)


def uninstall_bundle(codex_home: Path, dry_run: bool) -> InstallReport:
    """Remove only bundle-managed skills and hooks, preserving handoff state."""

    codex_home = Path(codex_home).absolute()
    _validate_managed_paths(codex_home)
    skills_dir = codex_home / "skills"
    targets = {name: skills_dir / name for name in _SKILL_NAMES}
    hooks_path = codex_home / "hooks.json"
    existing_hooks = _load_hooks(hooks_path)
    try:
        remaining_hooks = remove_managed_hooks(existing_hooks)
    except ValueError as error:
        raise InstallError(f"cannot update hooks file {hooks_path}: {error}") from error
    hooks_changed = hooks_path.exists() and remaining_hooks != existing_hooks
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
