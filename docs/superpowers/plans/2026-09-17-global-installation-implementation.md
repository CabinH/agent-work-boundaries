# Global Installation and Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package, install, upgrade, validate, and safely remove the verified `project-handoff` and `task-router` Skills plus their user-level Codex Hooks.

**Architecture:** A standard-library Python installer performs idempotent Hook merging, timestamped backups, staged directory replacement, dry runs, and uninstall; a thin shell entrypoint provides the requested `install.sh`. Tests operate only against temporary fake home directories, then a final reviewed command installs to `~/.codex`.

**Tech Stack:** Python 3.10+ standard library, `unittest`, POSIX shell, Codex `hooks.json`.

**Spec:** `docs/superpowers/specs/2026-09-17-agent-work-boundaries-design.md`

## Global Constraints

- Never overwrite an existing Skill or Hooks file without a timestamped backup.
- Merge only Agent Work Boundaries handlers; preserve unrelated Hook groups and handlers byte-for-semantics.
- Reinstalling the same version is idempotent and does not duplicate handlers.
- `--dry-run` performs no filesystem mutation.
- Automated tests and installation checks do not create real Codex threads.
- The installed hook command uses the resolved absolute `CODEX_HOME` path.
- Hooks remain inactive until the user reviews and trusts their current hash with `/hooks`.
- Global writes outside the workspace require the normal sandbox approval flow.

---

### Task 1: Implement Hook rendering and merging with TDD

**Files:**
- Create: `installer/install.py`
- Create: `hooks/hooks.json`
- Create: `tests/unit/test_installer.py`

**Interfaces:**
- Consumes: `codex_home: Path`, existing Hook JSON, and the installed `handoff_hook.py` path.
- Produces: `render_managed_hooks(hook_script: Path) -> dict`, `merge_hooks(existing: dict, managed: dict) -> dict`, and `remove_managed_hooks(existing: dict) -> dict`.

- [ ] **Step 1: Write the failing merge test**

```python
def test_merge_preserves_unrelated_handlers_and_is_idempotent(self):
    existing = {
        "description": "user hooks",
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "python3 /opt/other.py"}]}]
        },
    }
    managed = render_managed_hooks(Path("/tmp/codex/skills/project-handoff/scripts/handoff_hook.py"))

    once = merge_hooks(existing, managed)
    twice = merge_hooks(once, managed)

    self.assertEqual(once, twice)
    commands = [
        handler["command"]
        for group in once["hooks"]["Stop"]
        for handler in group["hooks"]
    ]
    self.assertIn("python3 /opt/other.py", commands)
    self.assertEqual(sum("handoff_hook.py" in command for command in commands), 1)
```

- [ ] **Step 2: Run the merge test and verify RED**

Run: `python -m unittest tests.unit.test_installer.InstallerTests.test_merge_preserves_unrelated_handlers_and_is_idempotent -v`

Expected: import failure because `installer/install.py` does not exist.

- [ ] **Step 3: Implement managed Hook groups and semantic identity**

Render these four groups:

```json
{
  "hooks": {
    "Stop": [{"hooks": [{"type": "command", "command": "python3 /resolved/handoff_hook.py", "timeout": 5}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 /resolved/handoff_hook.py", "timeout": 5, "additionalContextLimit": 500}]}],
    "PostCompact": [{"matcher": "manual|auto", "hooks": [{"type": "command", "command": "python3 /resolved/handoff_hook.py", "timeout": 5}]}],
    "SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [{"type": "command", "command": "python3 /resolved/handoff_hook.py", "timeout": 5, "additionalContextLimit": 500}]}]
  }
}
```

At runtime replace `/resolved/handoff_hook.py` with `str(hook_script.resolve())`. A managed handler is any command whose normalized executable target equals the installed Agent Work Boundaries `handoff_hook.py`; replace those handlers while preserving every other handler and group.

- [ ] **Step 4: Run the merge test and verify GREEN**

Run: `python -m unittest tests.unit.test_installer.InstallerTests.test_merge_preserves_unrelated_handlers_and_is_idempotent -v`

Expected: PASS.

- [ ] **Step 5: Add failing remove and malformed-input tests**

Add cases proving that removal keeps unrelated handlers, empty managed groups disappear, missing `hooks` is accepted, non-object JSON is rejected, and invalid group/handler shapes fail without mutation.

- [ ] **Step 6: Run installer unit tests and verify RED**

Run: `python -m unittest tests.unit.test_installer -v`

Expected: FAIL until validation and removal are implemented.

- [ ] **Step 7: Implement validation and removal, then verify GREEN**

Use `json.loads`, explicit `dict`/`list` checks, and deep copies. Run:

```bash
python -m unittest tests.unit.test_installer -v
```

Expected: all current tests PASS.

- [ ] **Step 8: Commit Hook merge behavior**

```bash
git add installer/install.py hooks/hooks.json tests/unit/test_installer.py
git commit -m "feat: merge work-boundary codex hooks"
```

### Task 2: Implement backup, staged installation, and uninstall with TDD

**Files:**
- Modify: `installer/install.py`
- Modify: `tests/unit/test_installer.py`

**Interfaces:**
- Consumes: repository root, fake or real `CODEX_HOME`, `dry_run: bool`, and `uninstall: bool`.
- Produces: `install_bundle(source_root: Path, codex_home: Path, dry_run: bool) -> InstallReport` and `uninstall_bundle(codex_home: Path, dry_run: bool) -> InstallReport`.

- [ ] **Step 1: Write the failing isolated-home installation test**

```python
def test_install_backs_up_existing_skill_and_preserves_hooks(self):
    with tempfile.TemporaryDirectory() as temp_dir:
        codex_home = Path(temp_dir) / "codex-home"
        old_skill = codex_home / "skills" / "project-handoff"
        old_skill.mkdir(parents=True)
        (old_skill / "SKILL.md").write_text("old skill\n")
        (codex_home / "hooks.json").write_text(
            json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}})
        )
        repo_root = Path(__file__).resolve().parents[2]

        report = install_bundle(repo_root, codex_home, dry_run=False)

        self.assertTrue((codex_home / "skills" / "project-handoff" / "SKILL.md").is_file())
        self.assertTrue((codex_home / "skills" / "task-router" / "SKILL.md").is_file())
        self.assertTrue(any(path.name == "SKILL.md" for path in report.backed_up_files))
        self.assertIn("other", (codex_home / "hooks.json").read_text())
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m unittest tests.unit.test_installer.InstallerTests.test_install_backs_up_existing_skill_and_preserves_hooks -v`

Expected: FAIL because bundle installation is absent.

- [ ] **Step 3: Implement staged copy and timestamped backups**

Backup root format:

```text
{CODEX_HOME}/backups/agent-work-boundaries/{UTC-YYYYMMDDTHHMMSSZ}/
├── hooks.json
└── skills/
    ├── project-handoff/
    └── task-router/
```

Copy each source Skill to a sibling staging directory, set directories to `0o700`, regular files to `0o600`, executable scripts to `0o700`, rename existing targets into the backup, then atomically rename staging into place. Write `hooks.json` through a same-directory temporary file plus `os.replace`.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `python -m unittest tests.unit.test_installer.InstallerTests.test_install_backs_up_existing_skill_and_preserves_hooks -v`

Expected: PASS.

- [ ] **Step 5: Add failing dry-run, reinstall, partial-failure, and uninstall tests**

Test these observable outcomes:

- Dry run reports planned files and leaves the fake home byte-for-byte unchanged.
- A second install contains one handler per managed event.
- A staged-copy failure leaves the previous installed Skill and Hooks intact.
- Uninstall removes only the two managed Skill directories and managed Hook handlers.
- Uninstall first backs up installed Skills and Hooks.
- A state directory under `state/project-handoff` is preserved by default.

- [ ] **Step 6: Run the expanded suite and verify RED**

Run: `python -m unittest tests.unit.test_installer -v`

Expected: FAIL on missing transactional, dry-run, or uninstall behavior.

- [ ] **Step 7: Implement transaction cleanup and reports**

Define:

```python
@dataclass(frozen=True)
class InstallReport:
    action: str
    changed_paths: Sequence[Path]
    backed_up_files: Sequence[Path]
    backup_root: Path | None
    dry_run: bool
```

On failure before target replacement, remove only installer-created staging directories. After any target has moved, restore it from the current transaction backup before raising `InstallError`.

- [ ] **Step 8: Run installer tests and verify GREEN**

Run: `python -m unittest tests.unit.test_installer -v`

Expected: all tests PASS.

- [ ] **Step 9: Commit transactional installation**

```bash
git add installer/install.py tests/unit/test_installer.py
git commit -m "feat: install and back up global skills safely"
```

### Task 3: Add the CLI wrapper and operator documentation

**Files:**
- Create: `install.sh`
- Create: `README.md`
- Create: `.gitignore`
- Modify: `installer/install.py`
- Modify: `tests/unit/test_installer.py`

**Interfaces:**
- Consumes: `--codex-home PATH`, `--dry-run`, and `--uninstall` CLI flags.
- Produces: JSON installation reports, documented `/hooks` trust step, recovery commands, and an executable `install.sh`.

- [ ] **Step 1: Write failing CLI parsing tests**

Invoke `main(argv)` with a temporary `--codex-home` for install dry-run, install, and uninstall. Assert exit code `0` and JSON containing `action`, `changed_paths`, `backup_root`, and `dry_run`. Assert malformed Hooks JSON returns nonzero without modifying files.

- [ ] **Step 2: Run the CLI tests and verify RED**

Run: `python -m unittest tests.unit.test_installer -v`

Expected: FAIL because CLI output is incomplete.

- [ ] **Step 3: Implement CLI JSON output**

Use `argparse`; default `--codex-home` to `Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()`. Print one UTF-8 JSON object with paths converted to strings.

- [ ] **Step 4: Add the thin shell entrypoint**

`install.sh` must resolve its own directory and execute:

```sh
#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/installer/install.py" "$@"
```

- [ ] **Step 5: Document install, trust, status, and rollback**

`README.md` contains these exact operator stages:

```text
./install.sh --dry-run
./install.sh
Open Codex and run /hooks, review the four Agent Work Boundaries handlers, and trust their current hash.
python3 ~/.codex/skills/project-handoff/scripts/handoffctl.py status --session-id <current-session-id>
./install.sh --uninstall
```

Explain that App Server creates and starts the new thread but the UI may not focus it, and identify the timestamped backup root used for manual restoration. The angle-bracket session token is user-supplied command syntax, not an unfinished implementation field.

- [ ] **Step 6: Run CLI and shell syntax checks**

Run:

```bash
python -m unittest tests.unit.test_installer -v
sh -n install.sh
./install.sh --codex-home /tmp/agent-work-boundaries-plan-check --dry-run
```

Expected: tests PASS, shell syntax succeeds, and dry-run reports changes without creating `/tmp/agent-work-boundaries-plan-check`.

- [ ] **Step 7: Commit packaging documentation**

```bash
git add install.sh installer/install.py README.md .gitignore tests/unit/test_installer.py
git commit -m "docs: add global installation workflow"
```

### Task 4: Verify the complete source bundle

**Files:**
- Modify only if verification reveals a tested defect.

**Interfaces:**
- Consumes: both completed Skill plans and the installer.
- Produces: a clean repository with passing unit, structural, behavioral, and dry-run checks.

- [ ] **Step 1: Run every unit test**

```bash
python -m unittest discover -s tests/unit -p 'test_*.py' -v
```

Expected: all tests PASS with no tracebacks or warnings.

- [ ] **Step 2: Validate both Skills**

```bash
python /home/huangkaibin/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/project-handoff
python /home/huangkaibin/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/task-router
```

Expected: both validators report success.

- [ ] **Step 3: Run isolated installation twice**

Install into a newly created temporary `CODEX_HOME`, run the installer a second time, parse `hooks.json`, and assert exactly one managed handler for each of `Stop`, `UserPromptSubmit`, `PostCompact`, and `SessionStart`.

- [ ] **Step 4: Run repository hygiene checks**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; status contains only deliberate uncommitted verification artifacts, otherwise it is clean.

- [ ] **Step 5: Apply verification-before-completion**

Read and follow `superpowers:verification-before-completion`; record the exact commands and fresh outputs used to support the completion claim.

### Task 5: Install globally and activate Hooks

**Files:**
- Modify outside repository: `~/.codex/skills/project-handoff/`
- Create outside repository: `~/.codex/skills/task-router/`
- Create or modify outside repository: `~/.codex/hooks.json`
- Create outside repository: `~/.codex/backups/agent-work-boundaries/`

**Interfaces:**
- Consumes: the fully verified repository bundle and one sandbox escalation for user-level installation.
- Produces: globally discoverable Skills, merged user Hooks, backup paths, and a manual trust instruction.

- [ ] **Step 1: Preview the real installation**

Run: `./install.sh --dry-run`

Expected: reports the two Skill targets, Hooks target, and backup plan without modifying `~/.codex`.

- [ ] **Step 2: Install with the normal approval flow**

Run: `./install.sh`

Because this writes outside the workspace, request sandbox escalation for this exact installer command. Do not bypass Hook trust.

- [ ] **Step 3: Verify installed files and merged JSON**

Run read-only checks for both installed `SKILL.md` files, executable scripts, `python -m json.tool ~/.codex/hooks.json`, and presence of unrelated existing Hook handlers.

- [ ] **Step 4: Validate installed Skills**

Run `quick_validate.py` against both installed directories and invoke a fresh Codex skill-discovery check after restarting or opening a new session.

- [ ] **Step 5: Request Hook review from the user**

Tell the user to run `/hooks`, inspect the four commands, and trust their exact current hash. Until that happens, describe automatic timeout and compaction tracking as installed but inactive; manual `handoffctl confirm` remains available.

- [ ] **Step 6: Offer one real App Server smoke thread**

Before creating any real thread, ask whether the user wants a smoke test that creates one clearly named test conversation and sends a no-write verification prompt. If approved, run it through the installed `AppServerClient`, record the returned thread ID, and do not claim the UI focused automatically. If declined, report that fake-protocol integration tests passed and real thread creation remains unexercised.

- [ ] **Step 7: Commit any verification-backed correction**

If real installation or smoke testing exposes a defect, reproduce it with a failing test, fix it through TDD, reinstall, reverify, and commit the correction. If no defect appears, create no empty commit.
