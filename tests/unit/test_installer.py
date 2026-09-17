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

    def test_merge_recognizes_env_and_python_options_without_duplication(self):
        target = str(self.hook_script.resolve())
        existing = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"env -u PYTHONPATH python3 {target}",
                            }
                        ]
                    },
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    "python3 --check-hash-based-pycs always "
                                    f"{target}"
                                ),
                            }
                        ]
                    },
                ]
            }
        }
        managed = render_managed_hooks(self.hook_script)

        once = merge_hooks(existing, managed)
        twice = merge_hooks(once, managed)
        commands = [
            handler["command"]
            for group in once["hooks"]["Stop"]
            for handler in group["hooks"]
        ]

        self.assertEqual(once, twice)
        self.assertEqual(sum(target in command for command in commands), 1)

    def test_merge_preserves_python_linter_that_mentions_managed_path(self):
        target = str(self.hook_script.resolve())
        linter_command = f"python-linter {target}"
        existing = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {"type": "command", "command": linter_command}
                        ]
                    }
                ]
            }
        }

        result = merge_hooks(existing, render_managed_hooks(self.hook_script))
        commands = [
            handler["command"]
            for group in result["hooks"]["Stop"]
            for handler in group["hooks"]
        ]

        self.assertIn(linter_command, commands)
        self.assertEqual(sum(command == linter_command for command in commands), 1)

    def test_merge_preserves_python_commands_that_do_not_execute_script_file(self):
        target = str(self.hook_script.resolve())
        unrelated_commands = [
            f"python3 -V {target}",
            f"python3 --check-hash-based-pycs=always {target}",
            f"python3 --check-hash-based-pycs sometimes {target}",
        ]
        existing = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {"type": "command", "command": command}
                            for command in unrelated_commands
                        ]
                    }
                ]
            }
        }

        result = merge_hooks(existing, render_managed_hooks(self.hook_script))
        commands = [
            handler["command"]
            for group in result["hooks"]["Stop"]
            for handler in group["hooks"]
        ]

        for command in unrelated_commands:
            self.assertIn(command, commands)

    def test_merge_rejects_unparseable_managed_command_before_mutation(self):
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
        managed = render_managed_hooks(self.hook_script)
        managed["hooks"]["Stop"][0]["hooks"][0]["command"] = (
            "python3 'unterminated"
        )
        existing_before = copy.deepcopy(existing)
        managed_before = copy.deepcopy(managed)

        with self.assertRaisesRegex(ValueError, "parseable target"):
            merge_hooks(existing, managed)

        self.assertEqual(existing, existing_before)
        self.assertEqual(managed, managed_before)
        valid = render_managed_hooks(self.hook_script)
        once = merge_hooks(existing, valid)
        self.assertEqual(merge_hooks(once, valid), once)

    def test_merge_rejects_inconsistent_managed_targets_before_mutation(self):
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
        managed = render_managed_hooks(self.hook_script)
        managed["hooks"]["SessionStart"][0]["hooks"][0]["command"] = (
            "python3 /tmp/other/skills/project-handoff/scripts/handoff_hook.py"
        )
        existing_before = copy.deepcopy(existing)
        managed_before = copy.deepcopy(managed)

        with self.assertRaisesRegex(ValueError, "same executable target"):
            merge_hooks(existing, managed)

        self.assertEqual(existing, existing_before)
        self.assertEqual(managed, managed_before)

    def test_merge_rejects_empty_managed_group_that_breaks_idempotence(self):
        existing = {"hooks": {}}
        managed = render_managed_hooks(self.hook_script)
        managed["hooks"]["Stop"].append({"matcher": "legacy", "hooks": []})
        existing_before = copy.deepcopy(existing)
        managed_before = copy.deepcopy(managed)

        with self.assertRaisesRegex(ValueError, "contain a command handler"):
            merge_hooks(existing, managed)

        self.assertEqual(existing, existing_before)
        self.assertEqual(managed, managed_before)

    def test_hook_template_matches_rendered_managed_hooks(self):
        repository_root = Path(__file__).resolve().parents[2]
        template = json.loads(
            (repository_root / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )

        self.assertEqual(
            template, render_managed_hooks(Path("/resolved/handoff_hook.py"))
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
