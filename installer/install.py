"""Install-time rendering and merging for Agent Work Boundaries hooks."""

from __future__ import annotations

import copy
import json
import re
import shlex
from pathlib import Path
from typing import Any, Callable


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
