import copy
import fcntl
import io
import json
import os
import shutil
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
        raw_mode = path.lstat().st_mode
        mode = stat.S_IMODE(raw_mode)
        if path.is_symlink():
            snapshot[relative] = ("symlink", mode, os.readlink(path))
        elif stat.S_ISDIR(raw_mode):
            snapshot[relative] = ("directory", mode, b"")
        elif stat.S_ISREG(raw_mode):
            snapshot[relative] = ("file", mode, path.read_bytes())
        else:
            snapshot[relative] = ("special", mode, str(stat.S_IFMT(raw_mode)))
    return snapshot


def _managed_handler_count(document: dict, event: str) -> int:
    return sum(
        "skills/project-handoff/scripts/handoff_hook.py" in handler["command"]
        for group in document["hooks"].get(event, [])
        for handler in group["hooks"]
    )


def _invoke_main(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = installer_module.main(argv)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.hook_script = Path(
            "/tmp/codex/skills/project-handoff/scripts/handoff_hook.py"
        )

    def test_cli_install_dry_run_reports_json_without_creating_home(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "missing-codex-home"

            exit_code, stdout, stderr = _invoke_main(
                ["--codex-home", str(codex_home), "--dry-run"]
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(
                json.loads(stdout),
                {
                    "action": "install",
                    "backed_up_files": [],
                    "backup_root": None,
                    "changed_paths": [
                        str(codex_home / "skills" / "project-handoff"),
                        str(codex_home / "skills" / "task-router"),
                        str(codex_home / "hooks.json"),
                    ],
                    "dry_run": True,
                },
            )
            self.assertFalse(codex_home.exists())

    def test_cli_install_reports_json_and_installs_into_explicit_home(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"

            exit_code, stdout, stderr = _invoke_main(
                ["--codex-home", str(codex_home)]
            )

            report = json.loads(stdout)
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(report["action"], "install")
            self.assertEqual(report["backup_root"], None)
            self.assertEqual(report["dry_run"], False)
            self.assertEqual(
                report["changed_paths"],
                [
                    str(codex_home / "skills" / "project-handoff"),
                    str(codex_home / "skills" / "task-router"),
                    str(codex_home / "hooks.json"),
                ],
            )
            self.assertTrue(
                (codex_home / "skills" / "project-handoff" / "SKILL.md").is_file()
            )
            self.assertTrue(
                (codex_home / "skills" / "task-router" / "SKILL.md").is_file()
            )

    def test_cli_uninstall_reports_json_and_removes_managed_skills(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            install_bundle(
                Path(__file__).resolve().parents[2], codex_home, dry_run=False
            )

            exit_code, stdout, stderr = _invoke_main(
                ["--codex-home", str(codex_home), "--uninstall"]
            )

            report = json.loads(stdout)
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(report["action"], "uninstall")
            self.assertEqual(report["dry_run"], False)
            self.assertIsInstance(report["backup_root"], str)
            self.assertIn(
                str(codex_home / "skills" / "project-handoff"),
                report["changed_paths"],
            )
            self.assertFalse(
                (codex_home / "skills" / "project-handoff").exists()
            )
            self.assertFalse((codex_home / "skills" / "task-router").exists())

    def test_cli_malformed_hooks_returns_nonzero_without_target_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            codex_home.mkdir()
            hooks_path = codex_home / "hooks.json"
            hooks_path.write_text('{"hooks":', encoding="utf-8")
            hooks_before = hooks_path.read_bytes()

            exit_code, stdout, stderr = _invoke_main(
                ["--codex-home", str(codex_home)]
            )

            self.assertNotEqual(exit_code, 0)
            self.assertEqual(stdout, "")
            error = json.loads(stderr)
            self.assertEqual(error["action"], "install")
            self.assertIn("cannot read hooks file", error["error"])
            self.assertEqual(hooks_path.read_bytes(), hooks_before)
            self.assertFalse((codex_home / "skills").exists())
            lock = codex_home / ".agent-work-boundaries.lock"
            self.assertTrue(lock.is_file())
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

    def test_cli_defaults_to_codex_home_environment_variable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "environment-codex-home"
            with patch.dict(
                os.environ,
                {"CODEX_HOME": str(codex_home)},
                clear=False,
            ):
                exit_code, stdout, stderr = _invoke_main(["--dry-run"])

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertIn(
                str(codex_home / "hooks.json"),
                json.loads(stdout)["changed_paths"],
            )
            self.assertFalse(codex_home.exists())

    def test_cli_defaults_to_expanded_user_codex_home(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            expected_home = Path(temp_dir) / ".codex"
            with patch.dict(
                os.environ,
                {"HOME": temp_dir},
                clear=True,
            ):
                exit_code, stdout, stderr = _invoke_main(["--dry-run"])

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertIn(
                str(expected_home / "hooks.json"),
                json.loads(stdout)["changed_paths"],
            )
            self.assertFalse(expected_home.exists())

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

    def test_install_excludes_python_runtime_artifacts_from_both_skills(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_root = temp_root / "source"
            shutil.copytree(
                Path(__file__).resolve().parents[2] / "skills",
                source_root / "skills",
            )
            project = source_root / "skills" / "project-handoff"
            router = source_root / "skills" / "task-router"
            seeded_artifacts = (
                project / "__pycache__" / "root.cpython-312.pyc",
                project
                / "scripts"
                / "__pycache__"
                / "nested.cpython-312.pyo",
                project / "scripts" / "direct.pyc",
                router / "__pycache__" / "router.cpython-312.pyc",
                router / "references" / "nested" / "analysis.pyo",
                router
                / "references"
                / "nested"
                / "__pycache__"
                / "analysis.cpython-312.pyc",
            )
            for artifact in seeded_artifacts:
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_bytes(b"runtime artifact\n")
            legitimate_files = {
                project / "scripts" / "legitimate.py": b"print('keep')\n",
                project / "assets" / "model.pyc.txt": b"asset\n",
                router / "references" / "legitimate.py": b"keep = True\n",
                router / "assets" / "profile.pyo.json": b"{}\n",
            }
            for legitimate, content in legitimate_files.items():
                legitimate.parent.mkdir(parents=True, exist_ok=True)
                legitimate.write_bytes(content)
            codex_home = temp_root / "codex-home"

            install_bundle(source_root, codex_home, dry_run=False)

            for skill_name in ("project-handoff", "task-router"):
                installed = codex_home / "skills" / skill_name
                with self.subTest(skill_name=skill_name):
                    self.assertFalse(
                        any(path.name == "__pycache__" for path in installed.rglob("*"))
                    )
                    self.assertFalse(
                        any(
                            path.is_file() and path.suffix in {".pyc", ".pyo"}
                            for path in installed.rglob("*")
                        )
                    )
            for source_file, content in legitimate_files.items():
                installed_file = (
                    codex_home
                    / "skills"
                    / source_file.relative_to(source_root / "skills")
                )
                self.assertEqual(installed_file.read_bytes(), content)
            for artifact in seeded_artifacts:
                self.assertTrue(artifact.is_file())

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

    def test_managed_target_symlink_is_rejected_without_hook_or_outside_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            codex_home = temp_root / "codex-home"
            skills_dir = codex_home / "skills"
            skills_dir.mkdir(parents=True)
            outside = temp_root / "outside-project-handoff"
            (outside / "scripts").mkdir(parents=True)
            (outside / "sentinel.txt").write_text("outside\n", encoding="utf-8")
            target = skills_dir / "project-handoff"
            target.symlink_to(outside, target_is_directory=True)
            hooks_path = codex_home / "hooks.json"
            hooks_path.write_text(
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
            hooks_before = hooks_path.read_bytes()
            outside_before = _tree_snapshot(outside)
            repo_root = Path(__file__).resolve().parents[2]

            for dry_run in (True, False):
                for attempt in range(2):
                    with self.subTest(
                        operation="install", dry_run=dry_run, attempt=attempt
                    ):
                        with self.assertRaisesRegex(InstallError, "symbolic link"):
                            install_bundle(repo_root, codex_home, dry_run=dry_run)
                        self.assertTrue(target.is_symlink())
                        self.assertEqual(hooks_path.read_bytes(), hooks_before)
                        self.assertNotIn(
                            str(outside / "scripts" / "handoff_hook.py"),
                            hooks_path.read_text(),
                        )
                        self.assertEqual(_tree_snapshot(outside), outside_before)

            for dry_run in (True, False):
                with self.subTest(operation="uninstall", dry_run=dry_run):
                    with self.assertRaisesRegex(InstallError, "symbolic link"):
                        installer_module.uninstall_bundle(
                            codex_home, dry_run=dry_run
                        )
                    self.assertTrue(target.is_symlink())
                    self.assertEqual(hooks_path.read_bytes(), hooks_before)
                    self.assertEqual(_tree_snapshot(outside), outside_before)

    def test_managed_target_nested_symlink_is_rejected_before_hook_rendering(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            codex_home = temp_root / "codex-home"
            target = codex_home / "skills" / "project-handoff"
            target.mkdir(parents=True)
            outside_scripts = temp_root / "outside-scripts"
            outside_scripts.mkdir()
            (target / "scripts").symlink_to(
                outside_scripts, target_is_directory=True
            )
            before = _tree_snapshot(codex_home)

            with self.assertRaisesRegex(InstallError, "symbolic link"):
                install_bundle(
                    Path(__file__).resolve().parents[2],
                    codex_home,
                    dry_run=True,
                )

            self.assertEqual(_tree_snapshot(codex_home), before)
            self.assertFalse((codex_home / "hooks.json").exists())

    def test_hooks_fifo_is_rejected_without_reading_or_mutation(self):
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                with tempfile.TemporaryDirectory() as temp_dir:
                    codex_home = Path(temp_dir) / "codex-home"
                    codex_home.mkdir()
                    hooks_path = codex_home / "hooks.json"
                    os.mkfifo(hooks_path)
                    entries_before = tuple(codex_home.iterdir())
                    real_read_text = Path.read_text

                    def guarded_read_text(path, *args, **kwargs):
                        if path == hooks_path:
                            raise AssertionError("installer attempted to read FIFO")
                        return real_read_text(path, *args, **kwargs)

                    with patch.object(Path, "read_text", guarded_read_text):
                        with self.assertRaisesRegex(
                            InstallError, "hooks file must be a regular file"
                        ):
                            install_bundle(
                                Path(__file__).resolve().parents[2],
                                codex_home,
                                dry_run=dry_run,
                            )

                    if dry_run:
                        self.assertEqual(
                            tuple(codex_home.iterdir()), entries_before
                        )
                    else:
                        self.assertEqual(
                            sorted(path.name for path in codex_home.iterdir()),
                            [".agent-work-boundaries.lock", "hooks.json"],
                        )
                    self.assertTrue(stat.S_ISFIFO(hooks_path.lstat().st_mode))

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

    def test_source_special_entries_are_rejected_before_destination_creation(self):
        for entry_kind in ("fifo", "symlink"):
            for dry_run in (True, False):
                with self.subTest(entry_kind=entry_kind, dry_run=dry_run):
                    with tempfile.TemporaryDirectory() as temp_dir:
                        temp_root = Path(temp_dir)
                        source_root = temp_root / "source"
                        shutil.copytree(
                            Path(__file__).resolve().parents[2] / "skills",
                            source_root / "skills",
                        )
                        special = (
                            source_root
                            / "skills"
                            / "task-router"
                            / f"unsupported-{entry_kind}"
                        )
                        if entry_kind == "fifo":
                            os.mkfifo(special)
                        else:
                            special.symlink_to(temp_root / "outside")
                        codex_home = temp_root / "missing-codex-home"

                        with patch.object(
                            installer_module.shutil,
                            "copytree",
                            side_effect=AssertionError(
                                "installer attempted to copy invalid source"
                            ),
                        ):
                            with self.assertRaisesRegex(
                                InstallError, "skill source contains"
                            ):
                                install_bundle(
                                    source_root,
                                    codex_home,
                                    dry_run=dry_run,
                                )

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

        result = remove_managed_hooks(existing, self.hook_script)

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
        self.assertEqual(remove_managed_hooks(existing, self.hook_script), existing)

    def test_non_object_json_is_rejected(self):
        existing = json.loads('[{"hooks": {}}]')
        managed = render_managed_hooks(self.hook_script)

        with self.assertRaisesRegex(ValueError, "object"):
            merge_hooks(existing, managed)
        with self.assertRaisesRegex(ValueError, "object"):
            remove_managed_hooks(existing, self.hook_script)

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
                    remove_managed_hooks(malformed, self.hook_script)
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

    def test_merge_and_remove_preserve_non_command_and_unknown_handlers(self):
        handlers = [
            {
                "type": "mcp",
                "server": "issue-tracker",
                "tool": "record_event",
            },
            {
                "type": "future-handler",
                "command": {"structured": "not a shell command"},
                "options": ["keep", "verbatim"],
            },
        ]
        existing = {
            "hooks": {
                "Stop": [
                    {
                        "matcher": "all",
                        "hooks": [
                            *copy.deepcopy(handlers),
                            {
                                "type": "command",
                                "command": f"python3 {self.hook_script}",
                            },
                        ],
                    }
                ]
            }
        }

        merged = merge_hooks(existing, render_managed_hooks(self.hook_script))
        removed = remove_managed_hooks(merged, self.hook_script)

        self.assertEqual(removed["hooks"]["Stop"][0]["hooks"], handlers)
        self.assertEqual(existing["hooks"]["Stop"][0]["hooks"][:2], handlers)

    def test_remove_matches_only_the_exact_selected_codex_home_target(self):
        exact = Path(
            "/tmp/selected-home/skills/project-handoff/scripts/handoff_hook.py"
        )
        lookalike = Path(
            "/tmp/other-home/skills/project-handoff/scripts/handoff_hook.py"
        )
        existing = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"python3 {exact}",
                            },
                            {
                                "type": "command",
                                "command": f"python3 {lookalike}",
                            },
                        ]
                    }
                ]
            }
        }

        result = remove_managed_hooks(existing, exact)

        self.assertEqual(
            result["hooks"]["Stop"][0]["hooks"],
            [{"type": "command", "command": f"python3 {lookalike}"}],
        )

    def test_install_fails_without_target_mutation_when_bundle_lock_is_held(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            repo_root = Path(__file__).resolve().parents[2]
            install_bundle(repo_root, codex_home, dry_run=False)
            lock_path = codex_home / ".agent-work-boundaries.lock"
            before = _tree_snapshot(codex_home)
            descriptor = os.open(lock_path, os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(InstallError, "another .* operation"):
                    install_bundle(repo_root, codex_home, dry_run=False)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

            self.assertEqual(_tree_snapshot(codex_home), before)

    def test_empty_home_uninstall_contends_on_the_bundle_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            codex_home.mkdir()
            lock_path = codex_home / ".agent-work-boundaries.lock"
            lock_path.touch(mode=0o600)
            descriptor = os.open(lock_path, os.O_RDWR)
            before = _tree_snapshot(codex_home)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(InstallError, "another .* operation"):
                    installer_module.uninstall_bundle(
                        codex_home, dry_run=False
                    )
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

            self.assertEqual(_tree_snapshot(codex_home), before)

    def test_displaced_lock_inode_never_allows_nested_critical_sections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            codex_home.mkdir()
            lock_path = codex_home / ".agent-work-boundaries.lock"
            lock_path.touch(mode=0o600)
            displaced = codex_home / ".displaced-bundle-lock"
            nested_inside = threading.Event()
            release_nested = threading.Event()
            nested_errors: list[BaseException] = []
            nested_thread: threading.Thread | None = None
            main_thread = threading.current_thread()
            real_flock = fcntl.flock
            swapped = False

            def run_nested_operation():
                try:
                    with installer_module._bundle_lock(codex_home):
                        nested_inside.set()
                        if not release_nested.wait(timeout=5):
                            raise AssertionError("nested lock was not released")
                except BaseException as error:
                    nested_errors.append(error)

            def swap_after_main_lock(descriptor, operation):
                nonlocal swapped, nested_thread
                result = real_flock(descriptor, operation)
                if (
                    threading.current_thread() is main_thread
                    and operation & fcntl.LOCK_EX
                    and not swapped
                ):
                    swapped = True
                    os.replace(lock_path, displaced)
                    lock_path.write_bytes(b"")
                    lock_path.chmod(0o600)
                    nested_thread = threading.Thread(
                        target=run_nested_operation
                    )
                    nested_thread.start()
                    if not nested_inside.wait(timeout=5):
                        raise AssertionError("nested operation did not acquire lock")
                return result

            outer_entered = False
            outer_error: BaseException | None = None
            try:
                with patch.object(
                    installer_module.fcntl,
                    "flock",
                    side_effect=swap_after_main_lock,
                ):
                    try:
                        with installer_module._bundle_lock(codex_home):
                            outer_entered = True
                    except BaseException as error:
                        outer_error = error
            finally:
                release_nested.set()
                if nested_thread is not None:
                    nested_thread.join(timeout=10)

            self.assertTrue(swapped)
            self.assertTrue(nested_inside.is_set())
            self.assertFalse(outer_entered)
            self.assertIsInstance(outer_error, InstallError)
            self.assertEqual(nested_errors, [])
            self.assertIsNotNone(nested_thread)
            self.assertFalse(nested_thread.is_alive())

    def test_uninstall_contends_during_first_install_before_journal_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            repo_root = Path(__file__).resolve().parents[2]
            entered_stage = threading.Event()
            release_stage = threading.Event()
            install_errors: list[BaseException] = []
            real_stage_skill = installer_module._stage_skill

            def block_first_stage(*args, **kwargs):
                if not entered_stage.is_set():
                    entered_stage.set()
                    if not release_stage.wait(timeout=5):
                        raise AssertionError("test did not release first install")
                return real_stage_skill(*args, **kwargs)

            def run_install():
                try:
                    install_bundle(repo_root, codex_home, dry_run=False)
                except BaseException as error:
                    install_errors.append(error)

            with patch.object(
                installer_module, "_stage_skill", side_effect=block_first_stage
            ):
                worker = threading.Thread(target=run_install)
                worker.start()
                self.assertTrue(entered_stage.wait(timeout=5))
                self.assertFalse(
                    (
                        codex_home
                        / ".agent-work-boundaries.transaction.json"
                    ).exists()
                )
                try:
                    with self.assertRaisesRegex(
                        InstallError, "another .* operation"
                    ):
                        installer_module.uninstall_bundle(
                            codex_home, dry_run=False
                        )
                finally:
                    release_stage.set()
                    worker.join(timeout=10)

            self.assertFalse(worker.is_alive())
            self.assertEqual(install_errors, [])
            self.assertTrue(
                (codex_home / "skills" / "project-handoff" / "SKILL.md").is_file()
            )

    def test_missing_home_non_dry_uninstall_bootstraps_shared_lock_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "missing-codex-home"

            report = installer_module.uninstall_bundle(
                codex_home, dry_run=False
            )

            lock_path = codex_home / ".agent-work-boundaries.lock"
            self.assertEqual(report.action, "uninstall")
            self.assertEqual(report.changed_paths, ())
            self.assertTrue(lock_path.is_file())
            metadata = lock_path.stat()
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
            self.assertEqual(metadata.st_nlink, 1)
            self.assertEqual(
                sorted(path.name for path in codex_home.iterdir()),
                [".agent-work-boundaries.lock"],
            )

    def test_missing_home_uninstall_dry_run_creates_nothing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "missing-codex-home"

            report = installer_module.uninstall_bundle(
                codex_home, dry_run=True
            )

            self.assertTrue(report.dry_run)
            self.assertFalse(codex_home.exists())

    def test_dry_run_creates_neither_lock_nor_journal_in_existing_home(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            codex_home.mkdir()
            before = _tree_snapshot(codex_home)

            install_bundle(
                Path(__file__).resolve().parents[2], codex_home, dry_run=True
            )

            self.assertEqual(_tree_snapshot(codex_home), before)
            self.assertFalse(
                (codex_home / ".agent-work-boundaries.lock").exists()
            )
            self.assertFalse(
                (codex_home / ".agent-work-boundaries.transaction.json").exists()
            )

    def test_install_crashes_after_each_target_replace_and_next_run_recovers(self):
        repo_root = Path(__file__).resolve().parents[2]
        for crash_after in range(1, 6):
            with self.subTest(crash_after=crash_after):
                with tempfile.TemporaryDirectory() as temp_dir:
                    codex_home = Path(temp_dir) / "codex-home"
                    install_bundle(repo_root, codex_home, dry_run=False)
                    marker = (
                        codex_home
                        / "skills"
                        / "project-handoff"
                        / "pre-crash-marker.txt"
                    )
                    marker.write_text("original\n", encoding="utf-8")
                    real_replace = installer_module._replace_and_fsync
                    replacements = 0

                    def crash_after_boundary(source, target):
                        nonlocal replacements
                        result = real_replace(source, target)
                        target_path = Path(target)
                        journal = (
                            codex_home
                            / ".agent-work-boundaries.transaction.json"
                        )
                        is_target_boundary = target_path == codex_home / "hooks.json" or (
                            target_path.parent == codex_home / "skills"
                            and (
                                target_path.name in {
                                    "project-handoff",
                                    "task-router",
                                }
                                or ".retired-" in target_path.name
                            )
                        )
                        if journal.exists() and is_target_boundary:
                            replacements += 1
                            if replacements == crash_after:
                                raise SystemExit("simulated process crash")
                        return result

                    with patch.object(
                        installer_module,
                        "_replace_and_fsync",
                        side_effect=crash_after_boundary,
                    ):
                        with self.assertRaisesRegex(
                            SystemExit, "simulated process crash"
                        ):
                            install_bundle(repo_root, codex_home, dry_run=False)

                    self.assertTrue(
                        (
                            codex_home
                            / ".agent-work-boundaries.transaction.json"
                        ).is_file()
                    )
                    report = install_bundle(repo_root, codex_home, dry_run=False)
                    self.assertEqual(report.action, "install")
                    self.assertFalse(
                        (
                            codex_home
                            / ".agent-work-boundaries.transaction.json"
                        ).exists()
                    )
                    self.assertFalse(
                        any(
                            ".retired-" in path.name or ".stage-" in path.name
                            for path in codex_home.rglob("*")
                        )
                    )

    def test_interrupted_install_recovers_before_invalid_source_is_planned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_root = temp_root / "source"
            shutil.copytree(
                Path(__file__).resolve().parents[2] / "skills",
                source_root / "skills",
            )
            codex_home = temp_root / "codex-home"
            install_bundle(source_root, codex_home, dry_run=False)
            marker = (
                codex_home / "skills" / "project-handoff" / "original.txt"
            )
            marker.write_text("restore before validation\n", encoding="utf-8")
            router_marker = codex_home / "skills" / "task-router" / "original.txt"
            router_marker.write_text("router original\n", encoding="utf-8")
            expected_hooks = (codex_home / "hooks.json").read_bytes()
            real_replace = installer_module._replace_and_fsync
            crashed = False

            def crash_after_first_retire(source, target):
                nonlocal crashed
                result = real_replace(source, target)
                target_path = Path(target)
                if (
                    not crashed
                    and target_path.parent == codex_home / "skills"
                    and ".retired-" in target_path.name
                ):
                    crashed = True
                    raise SystemExit("simulated interrupted install")
                return result

            with patch.object(
                installer_module,
                "_replace_and_fsync",
                side_effect=crash_after_first_retire,
            ):
                with self.assertRaisesRegex(SystemExit, "interrupted install"):
                    install_bundle(source_root, codex_home, dry_run=False)

            journal = codex_home / ".agent-work-boundaries.transaction.json"
            self.assertTrue(journal.is_file())
            shutil.rmtree(source_root / "skills" / "project-handoff")

            with self.assertRaisesRegex(InstallError, "missing skill source"):
                install_bundle(source_root, codex_home, dry_run=False)

            self.assertTrue(crashed)
            self.assertEqual(
                marker.read_text(encoding="utf-8"),
                "restore before validation\n",
            )
            self.assertEqual(
                router_marker.read_text(encoding="utf-8"),
                "router original\n",
            )
            self.assertEqual((codex_home / "hooks.json").read_bytes(), expected_hooks)
            self.assertFalse(journal.exists())
            self.assertFalse(
                any(
                    ".retired-" in path.name or ".stage-" in path.name
                    for path in codex_home.rglob("*")
                )
            )

    def test_first_install_crashes_after_each_replace_and_next_run_recovers(self):
        repo_root = Path(__file__).resolve().parents[2]
        for crash_after in range(1, 4):
            with self.subTest(crash_after=crash_after):
                with tempfile.TemporaryDirectory() as temp_dir:
                    codex_home = Path(temp_dir) / "codex-home"
                    real_replace = installer_module._replace_and_fsync
                    replacements = 0

                    def crash_after_boundary(source, target):
                        nonlocal replacements
                        result = real_replace(source, target)
                        target_path = Path(target)
                        journal = (
                            codex_home
                            / ".agent-work-boundaries.transaction.json"
                        )
                        is_target_boundary = target_path == codex_home / "hooks.json" or (
                            target_path.parent == codex_home / "skills"
                            and target_path.name
                            in {"project-handoff", "task-router"}
                        )
                        if journal.exists() and is_target_boundary:
                            replacements += 1
                            if replacements == crash_after:
                                raise SystemExit("simulated first-install crash")
                        return result

                    with patch.object(
                        installer_module,
                        "_replace_and_fsync",
                        side_effect=crash_after_boundary,
                    ):
                        with self.assertRaisesRegex(
                            SystemExit, "first-install crash"
                        ):
                            install_bundle(repo_root, codex_home, dry_run=False)

                    report = install_bundle(repo_root, codex_home, dry_run=False)

                    self.assertEqual(report.action, "install")
                    self.assertTrue(
                        (
                            codex_home
                            / "skills"
                            / "project-handoff"
                            / "SKILL.md"
                        ).is_file()
                    )
                    self.assertFalse(
                        (
                            codex_home
                            / ".agent-work-boundaries.transaction.json"
                        ).exists()
                    )

    def test_uninstall_crashes_after_each_target_replace_and_next_run_recovers(self):
        repo_root = Path(__file__).resolve().parents[2]
        for crash_after in range(1, 4):
            with self.subTest(crash_after=crash_after):
                with tempfile.TemporaryDirectory() as temp_dir:
                    codex_home = Path(temp_dir) / "codex-home"
                    install_bundle(repo_root, codex_home, dry_run=False)
                    real_replace = installer_module._replace_and_fsync
                    replacements = 0

                    def crash_after_boundary(source, target):
                        nonlocal replacements
                        result = real_replace(source, target)
                        target_path = Path(target)
                        journal = (
                            codex_home
                            / ".agent-work-boundaries.transaction.json"
                        )
                        is_target_boundary = target_path == codex_home / "hooks.json" or (
                            target_path.parent == codex_home / "skills"
                            and ".retired-" in target_path.name
                        )
                        if journal.exists() and is_target_boundary:
                            replacements += 1
                            if replacements == crash_after:
                                raise SystemExit("simulated process crash")
                        return result

                    with patch.object(
                        installer_module,
                        "_replace_and_fsync",
                        side_effect=crash_after_boundary,
                    ):
                        with self.assertRaisesRegex(
                            SystemExit, "simulated process crash"
                        ):
                            installer_module.uninstall_bundle(
                                codex_home, dry_run=False
                            )

                    self.assertTrue(
                        (
                            codex_home
                            / ".agent-work-boundaries.transaction.json"
                        ).is_file()
                    )
                    report = installer_module.uninstall_bundle(
                        codex_home, dry_run=False
                    )
                    self.assertEqual(report.action, "uninstall")
                    self.assertFalse(
                        (codex_home / "skills" / "project-handoff").exists()
                    )
                    self.assertFalse(
                        (codex_home / "skills" / "task-router").exists()
                    )
                    self.assertFalse(
                        (
                            codex_home
                            / ".agent-work-boundaries.transaction.json"
                        ).exists()
                    )

    def test_recovery_restores_originals_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            repo_root = Path(__file__).resolve().parents[2]
            install_bundle(repo_root, codex_home, dry_run=False)
            marker = (
                codex_home / "skills" / "project-handoff" / "original.txt"
            )
            marker.write_text("restore exactly\n", encoding="utf-8")
            expected_skills = _tree_snapshot(codex_home / "skills")
            expected_hooks = (codex_home / "hooks.json").read_bytes()
            real_replace = installer_module._replace_and_fsync
            replacements = 0

            def crash_second_target_replace(source, target):
                nonlocal replacements
                result = real_replace(source, target)
                target_path = Path(target)
                journal = codex_home / ".agent-work-boundaries.transaction.json"
                if journal.exists() and target_path.parent == codex_home / "skills":
                    replacements += 1
                    if replacements == 2:
                        raise SystemExit("crash")
                return result

            with patch.object(
                installer_module,
                "_replace_and_fsync",
                side_effect=crash_second_target_replace,
            ):
                with self.assertRaises(SystemExit):
                    install_bundle(repo_root, codex_home, dry_run=False)

            with installer_module._bundle_lock(codex_home):
                installer_module._recover_unfinished_transaction(codex_home)
                installer_module._recover_unfinished_transaction(codex_home)

            self.assertEqual(_tree_snapshot(codex_home / "skills"), expected_skills)
            self.assertEqual((codex_home / "hooks.json").read_bytes(), expected_hooks)
            self.assertFalse(
                (codex_home / ".agent-work-boundaries.transaction.json").exists()
            )

    def test_corrupt_and_traversal_journals_are_rejected_without_target_changes(self):
        repo_root = Path(__file__).resolve().parents[2]
        payloads = (
            b"{not json\n",
            json.dumps(
                {
                    "version": 1,
                    "bundle": "agent-work-boundaries",
                    "action": "install",
                    "transaction_id": "../escape",
                    "backup_id": None,
                    "skills": {
                        "project-handoff": {"original": False, "install": True},
                        "task-router": {"original": False, "install": True},
                    },
                    "hooks": {"original": False, "replace": True},
                }
            ).encode(),
        )
        for payload in payloads:
            with self.subTest(payload=payload[:20]):
                with tempfile.TemporaryDirectory() as temp_dir:
                    codex_home = Path(temp_dir) / "codex-home"
                    install_bundle(repo_root, codex_home, dry_run=False)
                    journal = (
                        codex_home
                        / ".agent-work-boundaries.transaction.json"
                    )
                    journal.write_bytes(payload)
                    journal.chmod(0o600)
                    expected_skills = _tree_snapshot(codex_home / "skills")
                    expected_hooks = (codex_home / "hooks.json").read_bytes()

                    with self.assertRaisesRegex(InstallError, "journal"):
                        install_bundle(repo_root, codex_home, dry_run=False)

                    self.assertEqual(
                        _tree_snapshot(codex_home / "skills"), expected_skills
                    )
                    self.assertEqual(
                        (codex_home / "hooks.json").read_bytes(), expected_hooks
                    )

    def test_install_and_uninstall_preserve_non_command_handlers_end_to_end(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            codex_home.mkdir()
            non_command = {
                "type": "mcp",
                "server": "audit",
                "tool": "record",
                "arguments": {"scope": "hooks"},
            }
            hooks_path = codex_home / "hooks.json"
            hooks_path.write_text(
                json.dumps(
                    {"hooks": {"Stop": [{"hooks": [non_command]}]}}
                ),
                encoding="utf-8",
            )
            repo_root = Path(__file__).resolve().parents[2]

            install_bundle(repo_root, codex_home, dry_run=False)
            installed = json.loads(hooks_path.read_text(encoding="utf-8"))
            installer_module.uninstall_bundle(codex_home, dry_run=False)
            uninstalled = json.loads(hooks_path.read_text(encoding="utf-8"))

            self.assertEqual(installed["hooks"]["Stop"][0]["hooks"], [non_command])
            self.assertEqual(uninstalled["hooks"]["Stop"][0]["hooks"], [non_command])

    def test_symlink_and_special_journals_are_rejected_without_following(self):
        repo_root = Path(__file__).resolve().parents[2]
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_root = Path(temp_dir)
                    codex_home = temp_root / "codex-home"
                    install_bundle(repo_root, codex_home, dry_run=False)
                    journal = (
                        codex_home
                        / ".agent-work-boundaries.transaction.json"
                    )
                    outside = temp_root / "outside-journal"
                    outside.write_text("do not read\n", encoding="utf-8")
                    if kind == "symlink":
                        journal.symlink_to(outside)
                    else:
                        os.mkfifo(journal)
                    expected_skills = _tree_snapshot(codex_home / "skills")
                    outside_before = outside.read_bytes()

                    with self.assertRaisesRegex(InstallError, "journal"):
                        installer_module.uninstall_bundle(
                            codex_home, dry_run=False
                        )

                    self.assertEqual(
                        _tree_snapshot(codex_home / "skills"), expected_skills
                    )
                    self.assertEqual(outside.read_bytes(), outside_before)

    def test_symlink_and_special_lock_files_are_rejected_without_following(self):
        repo_root = Path(__file__).resolve().parents[2]
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_root = Path(temp_dir)
                    codex_home = temp_root / "codex-home"
                    codex_home.mkdir()
                    lock = codex_home / ".agent-work-boundaries.lock"
                    outside = temp_root / "outside-lock"
                    outside.write_text("do not touch\n", encoding="utf-8")
                    if kind == "symlink":
                        lock.symlink_to(outside)
                    else:
                        os.mkfifo(lock)
                    before = _tree_snapshot(codex_home)

                    with self.assertRaisesRegex(InstallError, "lock"):
                        install_bundle(repo_root, codex_home, dry_run=False)

                    self.assertEqual(_tree_snapshot(codex_home), before)
                    self.assertEqual(outside.read_text(), "do not touch\n")

    def test_non_private_control_files_are_rejected_before_use(self):
        repo_root = Path(__file__).resolve().parents[2]
        for control_name, operation in (
            (
                ".agent-work-boundaries.lock",
                lambda home: install_bundle(repo_root, home, dry_run=False),
            ),
            (
                ".agent-work-boundaries.transaction.json",
                lambda home: installer_module.uninstall_bundle(
                    home, dry_run=False
                ),
            ),
        ):
            for unsafe_mode in (0o400, 0o640, 0o644):
                with self.subTest(
                    control_name=control_name, unsafe_mode=oct(unsafe_mode)
                ):
                    with tempfile.TemporaryDirectory() as temp_dir:
                        codex_home = Path(temp_dir) / "codex-home"
                        codex_home.mkdir()
                        control = codex_home / control_name
                        control.write_text("{}\n", encoding="utf-8")
                        control.chmod(unsafe_mode)
                        before = _tree_snapshot(codex_home)

                        with self.assertRaisesRegex(
                            InstallError, "permissions must be private"
                        ):
                            operation(codex_home)

                        self.assertEqual(_tree_snapshot(codex_home), before)

    def test_hard_linked_control_files_are_rejected_before_use(self):
        repo_root = Path(__file__).resolve().parents[2]
        for control_name, operation in (
            (
                ".agent-work-boundaries.lock",
                lambda home: install_bundle(repo_root, home, dry_run=False),
            ),
            (
                ".agent-work-boundaries.transaction.json",
                lambda home: installer_module.uninstall_bundle(
                    home, dry_run=False
                ),
            ),
        ):
            with self.subTest(control_name=control_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_root = Path(temp_dir)
                    codex_home = temp_root / "codex-home"
                    codex_home.mkdir()
                    control = codex_home / control_name
                    control.write_text("{}\n", encoding="utf-8")
                    control.chmod(0o600)
                    alias = temp_root / f"alias-{control_name.lstrip('.')}"
                    os.link(control, alias)
                    before = _tree_snapshot(codex_home)

                    with self.assertRaisesRegex(InstallError, "hard links"):
                        operation(codex_home)

                    self.assertEqual(_tree_snapshot(codex_home), before)
                    self.assertEqual(alias.read_text(encoding="utf-8"), "{}\n")

    def test_created_lock_and_crash_journal_are_private_single_link_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            repo_root = Path(__file__).resolve().parents[2]
            real_replace = installer_module._replace_and_fsync

            def crash_after_first_target(source, target):
                result = real_replace(source, target)
                target_path = Path(target)
                journal = codex_home / ".agent-work-boundaries.transaction.json"
                if (
                    journal.exists()
                    and target_path.parent == codex_home / "skills"
                    and target_path.name == "project-handoff"
                ):
                    raise SystemExit("leave journal for mode inspection")
                return result

            with patch.object(
                installer_module,
                "_replace_and_fsync",
                side_effect=crash_after_first_target,
            ):
                with self.assertRaisesRegex(SystemExit, "mode inspection"):
                    install_bundle(repo_root, codex_home, dry_run=False)

            for control in (
                codex_home / ".agent-work-boundaries.lock",
                codex_home / ".agent-work-boundaries.transaction.json",
            ):
                with self.subTest(control=control.name):
                    metadata = control.stat()
                    self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
                    self.assertEqual(metadata.st_nlink, 1)

    def test_journal_open_races_reject_unsafe_replacements_without_target_mutation(self):
        repo_root = Path(__file__).resolve().parents[2]
        for replacement_kind in ("mode", "hardlink", "symlink"):
            with self.subTest(replacement_kind=replacement_kind):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_root = Path(temp_dir)
                    codex_home = temp_root / "codex-home"
                    install_bundle(repo_root, codex_home, dry_run=False)
                    marker = (
                        codex_home
                        / "skills"
                        / "project-handoff"
                        / "original.txt"
                    )
                    marker.write_text("must stay partial\n", encoding="utf-8")
                    real_replace_and_fsync = installer_module._replace_and_fsync
                    crashed = False

                    def crash_after_retire(source, target):
                        nonlocal crashed
                        result = real_replace_and_fsync(source, target)
                        target_path = Path(target)
                        if (
                            not crashed
                            and target_path.parent == codex_home / "skills"
                            and ".retired-" in target_path.name
                        ):
                            crashed = True
                            raise SystemExit("leave recoverable journal")
                        return result

                    with patch.object(
                        installer_module,
                        "_replace_and_fsync",
                        side_effect=crash_after_retire,
                    ):
                        with self.assertRaisesRegex(
                            SystemExit, "recoverable journal"
                        ):
                            install_bundle(repo_root, codex_home, dry_run=False)

                    journal = (
                        codex_home
                        / ".agent-work-boundaries.transaction.json"
                    )
                    journal_payload = journal.read_bytes()
                    managed_before = {
                        name: _tree_snapshot(codex_home / "skills" / name)
                        for name in ("project-handoff", "task-router")
                    }
                    hooks_before = (codex_home / "hooks.json").read_bytes()
                    replacement = temp_root / "raced-journal"
                    alias = temp_root / "raced-journal-alias"
                    outside = temp_root / "raced-journal-outside"
                    if replacement_kind == "symlink":
                        outside.write_bytes(journal_payload)
                        outside.chmod(0o600)
                        replacement.symlink_to(outside)
                    else:
                        replacement.write_bytes(journal_payload)
                        replacement.chmod(
                            0o644 if replacement_kind == "mode" else 0o600
                        )
                        if replacement_kind == "hardlink":
                            os.link(replacement, alias)
                    real_open = os.open
                    raced = False

                    def replace_immediately_before_open(path, flags, *args, **kwargs):
                        nonlocal raced
                        if Path(path) == journal and not raced:
                            raced = True
                            os.replace(replacement, journal)
                        return real_open(path, flags, *args, **kwargs)

                    with patch.object(
                        installer_module.os,
                        "open",
                        side_effect=replace_immediately_before_open,
                    ):
                        with self.assertRaises(InstallError):
                            install_bundle(repo_root, codex_home, dry_run=False)

                    self.assertTrue(raced)
                    self.assertTrue(
                        all(
                            _tree_snapshot(codex_home / "skills" / name)
                            == managed_before[name]
                            for name in ("project-handoff", "task-router")
                        )
                    )
                    self.assertEqual(
                        (codex_home / "hooks.json").read_bytes(), hooks_before
                    )

    def test_journal_publish_races_reject_unsafe_replacements_without_target_mutation(self):
        repo_root = Path(__file__).resolve().parents[2]
        for replacement_kind in ("mode", "hardlink", "symlink"):
            with self.subTest(replacement_kind=replacement_kind):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_root = Path(temp_dir)
                    codex_home = temp_root / "codex-home"
                    install_bundle(repo_root, codex_home, dry_run=False)
                    marker = (
                        codex_home
                        / "skills"
                        / "project-handoff"
                        / "original.txt"
                    )
                    marker.write_text("must not be replaced\n", encoding="utf-8")
                    managed_before = {
                        name: _tree_snapshot(codex_home / "skills" / name)
                        for name in ("project-handoff", "task-router")
                    }
                    hooks_before = (codex_home / "hooks.json").read_bytes()
                    journal = (
                        codex_home
                        / ".agent-work-boundaries.transaction.json"
                    )
                    alias = temp_root / "published-journal-alias"
                    outside = temp_root / "published-journal-outside"
                    real_replace = os.replace
                    raced = False

                    def replace_after_publish(source, target):
                        nonlocal raced
                        result = real_replace(source, target)
                        source_path = Path(source)
                        target_path = Path(target)
                        if (
                            not raced
                            and target_path == journal
                            and source_path.name.startswith(
                                ".agent-work-boundaries.transaction.json.tmp-"
                            )
                        ):
                            raced = True
                            payload = journal.read_bytes()
                            substitute = temp_root / "published-journal-substitute"
                            if replacement_kind == "symlink":
                                outside.write_bytes(payload)
                                outside.chmod(0o600)
                                substitute.symlink_to(outside)
                            else:
                                substitute.write_bytes(payload)
                                substitute.chmod(
                                    0o644
                                    if replacement_kind == "mode"
                                    else 0o600
                                )
                                if replacement_kind == "hardlink":
                                    os.link(substitute, alias)
                            real_replace(substitute, journal)
                        return result

                    with patch.object(
                        installer_module.os,
                        "replace",
                        side_effect=replace_after_publish,
                    ):
                        with self.assertRaises(InstallError):
                            install_bundle(repo_root, codex_home, dry_run=False)

                    self.assertTrue(raced)
                    self.assertTrue(
                        all(
                            _tree_snapshot(codex_home / "skills" / name)
                            == managed_before[name]
                            for name in ("project-handoff", "task-router")
                        )
                    )
                    self.assertEqual(
                        (codex_home / "hooks.json").read_bytes(), hooks_before
                    )

    def test_journal_cleanup_refuses_a_substituted_canonical_inode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            codex_home = temp_root / "codex-home"
            repo_root = Path(__file__).resolve().parents[2]
            real_replace_and_fsync = installer_module._replace_and_fsync

            def crash_after_first_target(source, target):
                result = real_replace_and_fsync(source, target)
                target_path = Path(target)
                journal = codex_home / ".agent-work-boundaries.transaction.json"
                if (
                    journal.exists()
                    and target_path.parent == codex_home / "skills"
                    and target_path.name == "project-handoff"
                ):
                    raise SystemExit("leave journal for cleanup race")
                return result

            with patch.object(
                installer_module,
                "_replace_and_fsync",
                side_effect=crash_after_first_target,
            ):
                with self.assertRaisesRegex(SystemExit, "cleanup race"):
                    install_bundle(repo_root, codex_home, dry_run=False)

            journal = codex_home / ".agent-work-boundaries.transaction.json"
            substitute = temp_root / "cleanup-substitute"
            substitute.write_text('{"substitute": true}\n', encoding="utf-8")
            substitute.chmod(0o600)
            real_read = installer_module._read_journal_with_identity
            swapped = False

            def read_then_substitute(home):
                nonlocal swapped
                result = real_read(home)
                if not swapped:
                    swapped = True
                    os.replace(substitute, journal)
                return result

            with patch.object(
                installer_module,
                "_read_journal_with_identity",
                side_effect=read_then_substitute,
            ):
                with self.assertRaisesRegex(InstallError, "changed"):
                    installer_module._clear_journal(codex_home)

            self.assertTrue(swapped)
            self.assertEqual(
                journal.read_text(encoding="utf-8"),
                '{"substitute": true}\n',
            )

    def test_directory_fsync_failure_rolls_back_and_clears_journal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            repo_root = Path(__file__).resolve().parents[2]
            install_bundle(repo_root, codex_home, dry_run=False)
            marker = codex_home / "skills" / "task-router" / "original.txt"
            marker.write_text("keep\n", encoding="utf-8")
            expected_skills = _tree_snapshot(codex_home / "skills")
            expected_hooks = (codex_home / "hooks.json").read_bytes()
            real_fsync_directory = installer_module._fsync_directory
            failed = False

            def fail_once_after_journal(path):
                nonlocal failed
                if (
                    not failed
                    and Path(path) == codex_home / "skills"
                    and (
                        codex_home
                        / ".agent-work-boundaries.transaction.json"
                    ).exists()
                ):
                    failed = True
                    raise OSError("simulated directory fsync failure")
                return real_fsync_directory(path)

            with patch.object(
                installer_module,
                "_fsync_directory",
                side_effect=fail_once_after_journal,
            ):
                with self.assertRaisesRegex(
                    InstallError, "directory fsync failure"
                ):
                    install_bundle(repo_root, codex_home, dry_run=False)

            self.assertTrue(failed)
            self.assertEqual(_tree_snapshot(codex_home / "skills"), expected_skills)
            self.assertEqual((codex_home / "hooks.json").read_bytes(), expected_hooks)
            self.assertFalse(
                (codex_home / ".agent-work-boundaries.transaction.json").exists()
            )

    def test_journal_unlink_fsync_failure_reinstates_recovery_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            repo_root = Path(__file__).resolve().parents[2]
            install_bundle(repo_root, codex_home, dry_run=False)
            journal = codex_home / ".agent-work-boundaries.transaction.json"
            real_fsync_directory = installer_module._fsync_directory
            failed = False
            saw_journal = False

            def fail_once_after_journal_unlink(path):
                nonlocal failed, saw_journal
                if journal.exists():
                    saw_journal = True
                if (
                    saw_journal
                    and not failed
                    and Path(path) == codex_home
                    and not journal.exists()
                ):
                    failed = True
                    raise OSError("simulated journal unlink fsync failure")
                return real_fsync_directory(path)

            with patch.object(
                installer_module,
                "_fsync_directory",
                side_effect=fail_once_after_journal_unlink,
            ):
                with self.assertRaisesRegex(
                    InstallError, "journal cleanup failed"
                ):
                    install_bundle(repo_root, codex_home, dry_run=False)

            self.assertTrue(failed)
            self.assertTrue(journal.is_file())

            report = install_bundle(repo_root, codex_home, dry_run=False)

            self.assertEqual(report.action, "install")
            self.assertFalse(journal.exists())

    def test_failed_rollback_retains_journal_for_next_startup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            repo_root = Path(__file__).resolve().parents[2]
            install_bundle(repo_root, codex_home, dry_run=False)
            journal = codex_home / ".agent-work-boundaries.transaction.json"
            real_replace = os.replace
            failed_commit = False

            def fail_hooks_commit(source, target):
                nonlocal failed_commit
                source_path = Path(source)
                target_path = Path(target)
                if (
                    not failed_commit
                    and target_path == codex_home / "hooks.json"
                    and source_path.name.startswith(".hooks.json.stage-")
                ):
                    failed_commit = True
                    raise OSError("simulated commit failure")
                return real_replace(source, target)

            with patch.object(
                installer_module.os, "replace", side_effect=fail_hooks_commit
            ), patch.object(
                installer_module,
                "_recover_document",
                side_effect=OSError("simulated rollback failure"),
            ):
                with self.assertRaisesRegex(
                    InstallError, "rollback failed and journal retained"
                ):
                    install_bundle(repo_root, codex_home, dry_run=False)

            self.assertTrue(failed_commit)
            self.assertTrue(journal.is_file())

            report = install_bundle(repo_root, codex_home, dry_run=False)

            self.assertEqual(report.action, "install")
            self.assertFalse(journal.exists())


if __name__ == "__main__":
    unittest.main()
