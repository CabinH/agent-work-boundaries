import copy
import json
import unittest
from pathlib import Path

from installer.install import merge_hooks, remove_managed_hooks, render_managed_hooks


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.hook_script = Path(
            "/tmp/codex/skills/project-handoff/scripts/handoff_hook.py"
        )

    def test_merge_preserves_unrelated_handlers_and_is_idempotent(self):
        existing = {
            "description": "user hooks",
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {"type": "command", "command": "python3 /opt/other.py"}
                        ]
                    },
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    "python -u /tmp/codex/skills/project-handoff/"
                                    "scripts/../scripts/handoff_hook.py"
                                ),
                            }
                        ]
                    },
                ]
            },
        }
        managed = render_managed_hooks(self.hook_script)
        existing_before = copy.deepcopy(existing)
        managed_before = copy.deepcopy(managed)

        once = merge_hooks(existing, managed)
        twice = merge_hooks(once, managed)

        self.assertEqual(existing, existing_before)
        self.assertEqual(managed, managed_before)
        self.assertEqual(once, twice)
        commands = [
            handler["command"]
            for group in once["hooks"]["Stop"]
            for handler in group["hooks"]
        ]
        self.assertIn("python3 /opt/other.py", commands)
        self.assertEqual(sum("handoff_hook.py" in command for command in commands), 1)

    def test_render_produces_all_managed_event_groups(self):
        target = str(self.hook_script.resolve())

        self.assertEqual(
            render_managed_hooks(self.hook_script),
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"python3 {target}",
                                    "timeout": 5,
                                }
                            ]
                        }
                    ],
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"python3 {target}",
                                    "timeout": 5,
                                    "additionalContextLimit": 500,
                                }
                            ]
                        }
                    ],
                    "PostCompact": [
                        {
                            "matcher": "manual|auto",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"python3 {target}",
                                    "timeout": 5,
                                }
                            ],
                        }
                    ],
                    "SessionStart": [
                        {
                            "matcher": "startup|resume|clear|compact",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"python3 {target}",
                                    "timeout": 5,
                                    "additionalContextLimit": 500,
                                }
                            ],
                        }
                    ],
                }
            },
        )

    def test_merge_replaces_managed_handler_from_an_obsolete_event(self):
        existing = {
            "hooks": {
                "LegacyEvent": [
                    {
                        "matcher": "legacy",
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    "python3 /tmp/codex/skills/project-handoff/"
                                    "scripts/handoff_hook.py"
                                ),
                            },
                            {"type": "command", "command": "python3 /opt/other.py"},
                        ],
                    }
                ]
            }
        }

        result = merge_hooks(existing, render_managed_hooks(self.hook_script))

        self.assertEqual(
            result["hooks"]["LegacyEvent"],
            [
                {
                    "matcher": "legacy",
                    "hooks": [
                        {"type": "command", "command": "python3 /opt/other.py"}
                    ],
                }
            ],
        )

    def test_remove_keeps_unrelated_handlers_and_drops_empty_managed_groups(self):
        existing = {
            "description": "user hooks",
            "hooks": {
                "Stop": [
                    {
                        "matcher": "mixed",
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    "python /tmp/codex/skills/project-handoff/scripts/"
                                    "../scripts/handoff_hook.py"
                                ),
                            },
                            {"type": "command", "command": "python3 /opt/other.py"},
                        ],
                    },
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    "python3 /tmp/codex/skills/project-handoff/"
                                    "scripts/handoff_hook.py"
                                ),
                            }
                        ]
                    },
                    {
                        "matcher": "unrelated",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python3 /opt/handoff_hook.py",
                            }
                        ],
                    },
                    {"matcher": "empty-unrelated", "hooks": []},
                ]
            },
        }

        result = remove_managed_hooks(existing)

        self.assertEqual(result["description"], "user hooks")
        self.assertEqual(
            result["hooks"]["Stop"],
            [
                {
                    "matcher": "mixed",
                    "hooks": [
                        {"type": "command", "command": "python3 /opt/other.py"}
                    ],
                },
                {
                    "matcher": "unrelated",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 /opt/handoff_hook.py",
                        }
                    ],
                },
                {"matcher": "empty-unrelated", "hooks": []},
            ],
        )

    def test_missing_hooks_is_accepted(self):
        existing = {"description": "user hooks"}
        managed = render_managed_hooks(self.hook_script)

        merged = merge_hooks(existing, managed)

        self.assertEqual(existing, {"description": "user hooks"})
        self.assertEqual(merged["description"], "user hooks")
        self.assertEqual(set(merged["hooks"]), set(managed["hooks"]))
        self.assertEqual(remove_managed_hooks(existing), existing)

    def test_non_object_json_is_rejected(self):
        existing = json.loads('[{"hooks": {}}]')
        managed = render_managed_hooks(self.hook_script)

        with self.assertRaisesRegex(ValueError, "object"):
            merge_hooks(existing, managed)
        with self.assertRaisesRegex(ValueError, "object"):
            remove_managed_hooks(existing)

    def test_invalid_group_or_handler_shape_fails_without_mutation(self):
        malformed_values = [
            {"hooks": {"Stop": {"hooks": []}}},
            {"hooks": {"Stop": [[{"hooks": []}]]}},
            {"hooks": {"Stop": [{"hooks": {}}]}},
            {"hooks": {"Stop": [{"hooks": [42]}]}},
            {"hooks": {"Stop": [{"hooks": [{"type": "command"}]}]}},
        ]
        managed = render_managed_hooks(self.hook_script)

        for malformed in malformed_values:
            with self.subTest(malformed=malformed):
                before = copy.deepcopy(malformed)
                with self.assertRaises(ValueError):
                    merge_hooks(malformed, managed)
                self.assertEqual(malformed, before)

                with self.assertRaises(ValueError):
                    remove_managed_hooks(malformed)
                self.assertEqual(malformed, before)

    def test_invalid_managed_shape_fails_without_mutating_either_input(self):
        existing = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {"type": "command", "command": "python3 /opt/other.py"}
                        ]
                    }
                ]
            }
        }
        malformed_managed = {
            "hooks": {"Stop": [{"hooks": [{"type": "command"}]}]}
        }
        existing_before = copy.deepcopy(existing)
        managed_before = copy.deepcopy(malformed_managed)

        with self.assertRaises(ValueError):
            merge_hooks(existing, malformed_managed)

        self.assertEqual(existing, existing_before)
        self.assertEqual(malformed_managed, managed_before)


if __name__ == "__main__":
    unittest.main()
