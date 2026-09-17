"""Install-time rendering and merging for Agent Work Boundaries hooks."""

from __future__ import annotations

import copy
import json
import shlex
from pathlib import Path
from typing import Any, Callable


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
        index += 1
        while index < len(parts) and (
            parts[index].startswith("-") or "=" in parts[index]
        ):
            index += 1
        if index == len(parts):
            return None

    executable = Path(parts[index]).name
    if executable.startswith("python"):
        index += 1
        while index < len(parts) and parts[index].startswith("-"):
            option = parts[index]
            if option in {"-c", "-m"}:
                return None
            index += 2 if option in {"-W", "-X"} else 1
        if index == len(parts):
            return None
        target = parts[index]
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
    for groups in managed["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                target = _command_target(handler["command"])
                if target is not None:
                    return target
    raise ValueError("managed hooks contain no command handler")


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
    result = copy.deepcopy(existing)
    result.setdefault("hooks", {})
    target = _managed_target(managed)

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
