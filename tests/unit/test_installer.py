import copy
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import installer.install as installer_module
from installer.install import (
    InstallError,
    install_bundle,
    merge_hooks,
    remove_managed_hooks,
    render_managed_hooks,
)


def _tree_snapshot(root: Path) -> dict[str, tuple[str, int, bytes | str]]:
    if not root.exists():
        return {}
    snapshot: dict[str, tuple[str, int, bytes | str]] = {}
    for path in sorted([root, *root.rglob("*")]):
        relative = "." if path == root else str(path.relative_to(root))
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            snapshot[relative] = ("symlink", mode, os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory", mode, b"")
        else:
            snapshot[relative] = ("file", mode, path.read_bytes())
    return snapshot


def _managed_handler_count(document: dict, event: str) -> int:
    return sum(
        "skills/project-handoff/scripts/handoff_hook.py" in handler["command"]
        for group in document["hooks"].get(event, [])
        for handler in group["hooks"]
    )


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.hook_script = Path(
            "/tmp/codex/skills/project-handoff/scripts/handoff_hook.py"
        )

    def test_install_backs_up_existing_skill_and_preserves_hooks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            old_skill = codex_home / "skills" / "project-handoff"
            old_skill.mkdir(parents=True)
            (old_skill / "SKILL.md").write_text("old skill\n", encoding="utf-8")
            (codex_home / "hooks.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {"type": "command", "command": "other"}
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            repo_root = Path(__file__).resolve().parents[2]

            report = install_bundle(repo_root, codex_home, dry_run=False)

            self.assertTrue(
                (codex_home / "skills" / "project-handoff" / "SKILL.md").is_file()
            )
            self.assertTrue(
                (codex_home / "skills" / "task-router" / "SKILL.md").is_file()
            )
            self.assertTrue(
                any(path.name == "SKILL.md" for path in report.backed_up_files)
            )
            self.assertIn("other", (codex_home / "hooks.json").read_text())

    def test_install_dry_run_reports_plan_without_mutating_home(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            old_skill = codex_home / "skills" / "project-handoff"
            old_skill.mkdir(parents=True)
            (old_skill / "SKILL.md").write_text("old skill\n", encoding="utf-8")
            (codex_home / "hooks.json").write_text("{}\n", encoding="utf-8")
            before = _tree_snapshot(codex_home)

            report = install_bundle(
                Path(__file__).resolve().parents[2], codex_home, dry_run=True
            )

            self.assertEqual(_tree_snapshot(codex_home), before)
            self.assertEqual(report.action, "install")
            self.assertTrue(report.dry_run)
            self.assertEqual(
                set(report.changed_paths),
                {
                    codex_home / "skills" / "project-handoff",
                    codex_home / "skills" / "task-router",
                    codex_home / "hooks.json",
                },
            )
            self.assertIsNotNone(report.backup_root)
            self.assertTrue(
                any(path.name == "SKILL.md" for path in report.backed_up_files)
            )

    def test_install_dry_run_does_not_create_missing_home(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "missing-codex-home"

            report = install_bundle(
                Path(__file__).resolve().parents[2], codex_home, dry_run=True
            )

            self.assertTrue(report.dry_run)
            self.assertFalse(codex_home.exists())

    def test_reinstall_has_one_managed_handler_per_event_and_unique_backups(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            repo_root = Path(__file__).resolve().parents[2]

            first = install_bundle(repo_root, codex_home, dry_run=False)
            second = install_bundle(repo_root, codex_home, dry_run=False)
            third = install_bundle(repo_root, codex_home, dry_run=False)
            hooks = json.loads((codex_home / "hooks.json").read_text())

            for event in ("Stop", "UserPromptSubmit", "PostCompact", "SessionStart"):
                with self.subTest(event=event):
                    self.assertEqual(_managed_handler_count(hooks, event), 1)
            self.assertIsNone(first.backup_root)
            self.assertIsNotNone(second.backup_root)
            self.assertIsNotNone(third.backup_root)
            self.assertNotEqual(second.backup_root, third.backup_root)
            self.assertTrue(second.backup_root.is_dir())
            self.assertTrue(third.backup_root.is_dir())

    def test_staged_copy_failure_leaves_previous_install_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            repo_root = Path(__file__).resolve().parents[2]
            install_bundle(repo_root, codex_home, dry_run=False)
            before = _tree_snapshot(codex_home)
            real_copytree = shutil.copytree
            calls = 0

            def fail_second_copy(source, target, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated staged-copy failure")
                return real_copytree(source, target, *args, **kwargs)

            with patch.object(
                installer_module.shutil, "copytree", side_effect=fail_second_copy
            ):
                with self.assertRaisesRegex(InstallError, "staged-copy failure"):
                    install_bundle(repo_root, codex_home, dry_run=False)

            self.assertEqual(_tree_snapshot(codex_home), before)

    def test_install_backs_up_a_legacy_skill_file_before_replacing_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            skills_dir = codex_home / "skills"
            skills_dir.mkdir(parents=True)
            legacy_target = skills_dir / "task-router"
            legacy_target.write_text("legacy file\n", encoding="utf-8")

            report = install_bundle(
                Path(__file__).resolve().parents[2], codex_home, dry_run=False
            )

            self.assertTrue(legacy_target.is_dir())
            backup_file = report.backup_root / "skills" / "task-router"
            self.assertEqual(backup_file.read_text(), "legacy file\n")
            self.assertIn(backup_file, report.backed_up_files)

    def test_commit_failure_restores_every_replaced_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            repo_root = Path(__file__).resolve().parents[2]
            install_bundle(repo_root, codex_home, dry_run=False)
            local_marker = (
                codex_home / "skills" / "project-handoff" / "local-only.txt"
            )
            local_marker.write_text("must be restored\n", encoding="utf-8")
            shutil.rmtree(codex_home / "skills" / "task-router")
            expected = {
                name: _tree_snapshot(codex_home / "skills" / name)
                for name in ("project-handoff", "task-router")
            }
            expected_hooks = (codex_home / "hooks.json").read_bytes()
            real_replace = os.replace
            failed = False

            def fail_hooks_commit(source, target):
                nonlocal failed
                source_path = Path(source)
                target_path = Path(target)
                if (
                    not failed
                    and target_path == codex_home / "hooks.json"
                    and source_path.name.startswith(".hooks.json.stage-")
                ):
                    failed = True
                    raise OSError("simulated hooks commit failure")
                return real_replace(source, target)

            with patch.object(
                installer_module.os, "replace", side_effect=fail_hooks_commit
            ):
                with self.assertRaisesRegex(InstallError, "hooks commit failure"):
                    install_bundle(repo_root, codex_home, dry_run=False)

            for name, snapshot in expected.items():
                self.assertEqual(_tree_snapshot(codex_home / "skills" / name), snapshot)
            self.assertEqual((codex_home / "hooks.json").read_bytes(), expected_hooks)
            self.assertFalse(
                any(
                    child.name.startswith(".project-handoff.stage-")
                    or child.name.startswith(".task-router.stage-")
                    or child.name.startswith(".hooks.json.stage-")
                    for child in codex_home.rglob("*")
                )
            )

    def test_uninstall_backs_up_managed_files_and_preserves_other_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            codex_home.mkdir()
            (codex_home / "hooks.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "python3 /opt/other.py",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            repo_root = Path(__file__).resolve().parents[2]
            install_bundle(repo_root, codex_home, dry_run=False)
            unrelated_skill = codex_home / "skills" / "keep-me"
            unrelated_skill.mkdir()
            (unrelated_skill / "SKILL.md").write_text("keep\n", encoding="utf-8")
            state = codex_home / "state" / "project-handoff"
            state.mkdir(parents=True)
            (state / "pending.json").write_text("{}\n", encoding="utf-8")

            report = installer_module.uninstall_bundle(codex_home, dry_run=False)

            self.assertEqual(report.action, "uninstall")
            self.assertFalse((codex_home / "skills" / "project-handoff").exists())
            self.assertFalse((codex_home / "skills" / "task-router").exists())
            self.assertEqual((unrelated_skill / "SKILL.md").read_text(), "keep\n")
            self.assertEqual((state / "pending.json").read_text(), "{}\n")
            remaining_hooks = json.loads((codex_home / "hooks.json").read_text())
            self.assertEqual(_managed_handler_count(remaining_hooks, "Stop"), 0)
            self.assertIn("/opt/other.py", json.dumps(remaining_hooks))
            self.assertIsNotNone(report.backup_root)
            self.assertTrue(
                (
                    report.backup_root
                    / "skills"
                    / "project-handoff"
                    / "SKILL.md"
                ).is_file()
            )
            self.assertTrue(
                (report.backup_root / "skills" / "task-router" / "SKILL.md").is_file()
            )
            self.assertTrue((report.backup_root / "hooks.json").is_file())

    def test_uninstall_dry_run_leaves_install_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            install_bundle(
                Path(__file__).resolve().parents[2], codex_home, dry_run=False
            )
            before = _tree_snapshot(codex_home)

            report = installer_module.uninstall_bundle(codex_home, dry_run=True)

            self.assertTrue(report.dry_run)
            self.assertEqual(_tree_snapshot(codex_home), before)

    def test_uninstall_commit_failure_restores_skills_and_hooks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            install_bundle(
                Path(__file__).resolve().parents[2], codex_home, dry_run=False
            )
            marker = codex_home / "skills" / "task-router" / "local-only.txt"
            marker.write_text("restore me\n", encoding="utf-8")
            expected_skills = _tree_snapshot(codex_home / "skills")
            expected_hooks = (codex_home / "hooks.json").read_bytes()
            real_replace = os.replace
            failed = False

            def fail_hooks_commit(source, target):
                nonlocal failed
                source_path = Path(source)
                target_path = Path(target)
                if (
                    not failed
                    and target_path == codex_home / "hooks.json"
                    and source_path.name.startswith(".hooks.json.stage-")
                ):
                    failed = True
                    raise OSError("simulated uninstall hooks failure")
                return real_replace(source, target)

            with patch.object(
                installer_module.os, "replace", side_effect=fail_hooks_commit
            ):
                with self.assertRaisesRegex(InstallError, "uninstall hooks failure"):
                    installer_module.uninstall_bundle(codex_home, dry_run=False)

            self.assertEqual(_tree_snapshot(codex_home / "skills"), expected_skills)
            self.assertEqual((codex_home / "hooks.json").read_bytes(), expected_hooks)

    def test_install_sets_private_modes_and_preserves_executable_intent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_root = temp_root / "source"
            repository_root = Path(__file__).resolve().parents[2]
            shutil.copytree(repository_root / "skills", source_root / "skills")
            executable = (
                source_root
                / "skills"
                / "project-handoff"
                / "scripts"
                / "handoffctl.py"
            )
            executable.chmod(0o755)
            codex_home = temp_root / "codex-home"

            install_bundle(source_root, codex_home, dry_run=False)

            self.assertEqual(
                stat.S_IMODE(
                    (codex_home / "skills" / "project-handoff").stat().st_mode
                ),
                0o700,
            )
            self.assertEqual(
                stat.S_IMODE(
                    (
                        codex_home
                        / "skills"
                        / "project-handoff"
                        / "SKILL.md"
                    ).stat().st_mode
                ),
                0o600,
            )
            self.assertEqual(
                stat.S_IMODE(
                    (
                        codex_home
                        / "skills"
                        / "project-handoff"
                        / "scripts"
                        / "handoffctl.py"
                    ).stat().st_mode
                ),
                0o700,
            )
            self.assertEqual(
                stat.S_IMODE((codex_home / "hooks.json").stat().st_mode), 0o600
            )

    def test_install_rejects_symlinked_managed_parent_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            codex_home = temp_root / "codex-home"
            codex_home.mkdir()
            outside = temp_root / "outside"
            outside.mkdir()
            (codex_home / "skills").symlink_to(outside, target_is_directory=True)
            outside_before = _tree_snapshot(outside)

            with self.assertRaisesRegex(InstallError, "symbolic link"):
                install_bundle(
                    Path(__file__).resolve().parents[2], codex_home, dry_run=False
                )

            self.assertEqual(_tree_snapshot(outside), outside_before)
            self.assertFalse((codex_home / "hooks.json").exists())

    def test_install_rejects_source_tree_symlink_without_creating_home(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_root = temp_root / "source"
            repository_root = Path(__file__).resolve().parents[2]
            shutil.copytree(repository_root / "skills", source_root / "skills")
            external = temp_root / "external.txt"
            external.write_text("secret\n", encoding="utf-8")
            (
                source_root / "skills" / "task-router" / "references-link"
            ).symlink_to(external)
            codex_home = temp_root / "codex-home"

            with self.assertRaisesRegex(InstallError, "symbolic link"):
                install_bundle(source_root, codex_home, dry_run=False)

            self.assertFalse(codex_home.exists())

    def test_install_dry_run_rejects_non_directory_skills_parent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            codex_home.mkdir()
            skills_path = codex_home / "skills"
            skills_path.write_text("not a directory\n", encoding="utf-8")
            before = _tree_snapshot(codex_home)

            with self.assertRaisesRegex(InstallError, "expected a directory"):
                install_bundle(
                    Path(__file__).resolve().parents[2], codex_home, dry_run=True
                )

            self.assertEqual(_tree_snapshot(codex_home), before)

    def test_backup_name_skips_a_broken_symlink_collision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            backup_parent = codex_home / "backups" / "agent-work-boundaries"
            backup_parent.mkdir(parents=True)
            timestamp = installer_module.datetime.now(
                installer_module.timezone.utc
            ).strftime("%Y%m%dT%H%M%SZ")
            collision = backup_parent / timestamp
            collision.symlink_to(Path(temp_dir) / "missing-backup")

            selected = installer_module._next_backup_root(codex_home)

            self.assertNotEqual(selected, collision)
            self.assertEqual(selected.parent, backup_parent)

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

    def test_merge_recognizes_clustered_flags_for_versioned_python(self):
        target = str(self.hook_script.resolve())
        managed_commands = [
            f"/usr/bin/python3.12 -Iu {target}",
            f"python3 -IE {target}",
            f"python3 -OOq {target}",
        ]
        existing = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {"type": "command", "command": command}
                            for command in managed_commands
                        ]
                    }
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

    def test_merge_recognizes_unambiguous_python_argument_options(self):
        target = str(self.hook_script.resolve())
        managed_commands = [
            f"python3 -W ignore {target}",
            f"python3 -Wignore {target}",
            f"python3 -X dev {target}",
            f"python3 -Xdev {target}",
        ]
        existing = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {"type": "command", "command": command}
                            for command in managed_commands
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

        self.assertEqual(sum(target in command for command in commands), 1)

    def test_merge_preserves_unknown_or_ambiguous_python_flags(self):
        target = str(self.hook_script.resolve())
        unrelated_commands = [
            f"python3 -Iz {target}",
            f"python3 -IWignore {target}",
            f"python3 -W {target}",
            f"python3 -X {target}",
            f"python3 -W ignore -Iz {target}",
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
