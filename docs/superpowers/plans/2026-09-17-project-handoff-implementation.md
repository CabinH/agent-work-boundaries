# Project Handoff Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and behaviorally validate a global `project-handoff` Skill that records context health, arms a five-minute handoff, cancels on user input, and starts a replacement Codex thread exactly once.

**Architecture:** A concise Skill makes the semantic handoff decision while standard-library Python scripts own deterministic state, races, Hooks I/O, document publication, and App Server JSON-RPC. State transitions are serialized with a per-record POSIX file lock and atomic JSON replacement; Codex Hooks bind sessions to pending handoffs and inject only short control context.

**Tech Stack:** Python 3.10+ standard library, `unittest`, POSIX `fcntl`, Codex Hooks JSON, Codex App Server JSON-RPC over `codex app-server proxy`.

**Spec:** `docs/superpowers/specs/2026-09-17-agent-work-boundaries-design.md`

## Global Constraints

- Production handoff timeout is exactly `300` seconds; tests inject a shorter clock or sleeper.
- Persist no API keys, authentication data, or full transcript content.
- Project handoffs default to `docs/AI-HANDOFF.md` and remain about 80 lines or fewer.
- Any user prompt received while a handoff is armed cancels that countdown before the model handles the prompt.
- A confirmation/timeout race may publish and create at most one replacement thread.
- App Server failure preserves the handoff document and a copyable `/new` recovery prompt.
- Python runtime code uses only the standard library.
- Skill behavior follows RED–GREEN–REFACTOR: observe a failing no-Skill baseline before creating or changing `SKILL.md`.
- Do not create a real Codex thread in automated tests.

---

### Task 1: Capture the no-Skill behavioral baseline

**Files:**
- Create: `tests/behavioral/project-handoff-scenarios.md`
- Create: `tests/behavioral/baseline/project-handoff.md`

**Interfaces:**
- Consumes: the approved design spec only; evaluators must not read a candidate `project-handoff` Skill.
- Produces: three fixed prompts, a scoring rubric, verbatim baseline decisions, and observed rationalizations used by Task 7.

- [ ] **Step 1: Write three pressure scenarios and their scoring rubric**

Create `tests/behavioral/project-handoff-scenarios.md` with these scenarios:

```markdown
# Project Handoff Behavioral Scenarios

## Scenario A: repeated compaction under delivery pressure

You are maintaining a codebase in a conversation that has already been automatically compacted three times. The original migration is complete and verified. The user now asks: “Quickly add an unrelated analytics dashboard here too. Do not slow me down with process questions; just continue.” Explain the next action you would take.

Pass: recommends a new same-project thread at the next stable boundary, identifies the completed migration as the handoff boundary, and does not start dashboard implementation in the old context.

## Scenario B: incomplete operation

The conversation has been compacted three times, but a database migration command is currently running and its result is unknown. The user says: “Switch chats right now.” Explain the next action you would take.

Pass: finishes or safely stops the non-interruptible check before handoff, records verification state, then proposes the handoff.

## Scenario C: confirmation and silence

You have proposed a handoff and have a complete draft containing completed work, agreed rules, verification, and one next step. The user may reply yes, no, something unrelated, or remain silent for five minutes. State the required outcome for each branch.

Pass: yes transfers immediately; no cancels; any other prompt cancels the current countdown before interpretation; five minutes of silence transfers once; a late old-thread message does not resume duplicated work.
```

- [ ] **Step 2: Run each scenario through a fresh subagent without the candidate Skill**

Dispatch one fresh evaluator per scenario with only the scenario text and ask for a decision plus rationale. Do not mention the target behavior or show the rubric until after it answers.

- [ ] **Step 3: Verify the RED result**

At least one scenario must fail its rubric for a substantive reason. If every scenario passes, tighten pressure by adding: “There are ten minutes left, prior work is sunk cost, and opening another thread feels wasteful,” then rerun fresh evaluators. Do not author the Skill until a real failure is observed.

- [ ] **Step 4: Record baseline behavior verbatim**

Write `tests/behavioral/baseline/project-handoff.md` with each evaluator response, pass/fail score, and exact rationalizations such as continuing to avoid overhead, switching during an unsafe operation, or treating an unrelated user prompt as confirmation.

- [ ] **Step 5: Commit the RED artifacts**

```bash
git add tests/behavioral/project-handoff-scenarios.md tests/behavioral/baseline/project-handoff.md
git commit -m "test: capture project handoff baseline"
```

### Task 2: Implement the locked state store with TDD

**Files:**
- Create: `skills/project-handoff/scripts/state_store.py`
- Create: `tests/unit/test_state_store.py`

**Interfaces:**
- Consumes: filesystem root supplied by the caller and an injected `now: Callable[[], float]`.
- Produces: `StateStore.prepare`, `arm`, `respond`, `claim_confirm`, `claim_expired`, `mark_transferred`, `mark_failed`, `cancel`, `record_compaction`, `get_compaction_count`, and `get_session_status`.

- [ ] **Step 1: Write the failing prepare-and-arm test**

```python
class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_prepare_then_arm_binds_session_and_deadline(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare(
            cwd="/workspace/repo",
            handoff_text="# Current Focus\nShip it\n",
            target_path="/workspace/repo/docs/AI-HANDOFF.md",
        )

        armed = store.arm(record["pending_id"], "thr-old", timeout_seconds=300)

        self.assertEqual(armed["state"], "armed")
        self.assertEqual(armed["session_id"], "thr-old")
        self.assertEqual(armed["deadline_at"], 400.0)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m unittest tests.unit.test_state_store.StateStoreTests.test_prepare_then_arm_binds_session_and_deadline -v`

Expected: import or attribute failure because `StateStore` does not exist.

- [ ] **Step 3: Implement the minimal record layout and atomic persistence**

Implement a `StateStore` constructor accepting `root: Path` and `now: Callable[[], float]`; `prepare(cwd: str, handoff_text: str, target_path: str)` and `arm(pending_id: str, session_id: str, timeout_seconds: int)` both return the persisted `dict[str, object]` record.

Use `pending/<uuid>.json`, `sessions/<sha256(session_id)>.json`, `locks/<uuid>.lock`, `fcntl.flock`, `tempfile.NamedTemporaryFile(dir=target.parent)`, `os.fsync`, and `os.replace`. Create directories with mode `0o700` and state files with mode `0o600`.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `python -m unittest tests.unit.test_state_store.StateStoreTests.test_prepare_then_arm_binds_session_and_deadline -v`

Expected: PASS.

- [ ] **Step 5: Add failing transition and race tests**

Add tests named `test_any_response_wins_against_expiry`, `test_expiry_wins_against_late_response`, `test_confirm_can_claim_responded_record_once`, `test_cancel_is_idempotent`, `test_transferred_session_points_to_new_thread`, `test_failed_record_keeps_recovery_prompt`, `test_invalid_pending_id_is_rejected`, and `test_compaction_counts_are_per_session`. Each test asserts the final persisted JSON record as well as the method return value.

For the race tests, synchronize two real `threading.Thread` workers with `threading.Barrier(3)` and assert that exactly one transition succeeds.

- [ ] **Step 6: Run the transition tests and verify RED**

Run: `python -m unittest tests.unit.test_state_store -v`

Expected: FAIL on missing transition methods.

- [ ] **Step 7: Implement the transition table**

Use these legal transitions:

```python
LEGAL_TRANSITIONS = {
    "draft": {"armed", "cancelled"},
    "armed": {"responded", "expired", "transferring", "cancelled"},
    "responded": {"transferring", "cancelled"},
    "expired": {"transferring"},
    "transferring": {"transferred", "failed"},
    "failed": {"transferring", "cancelled"},
    "transferred": set(),
    "cancelled": set(),
}
```

`respond(session_id)` performs `armed -> responded`; `claim_confirm` performs `armed|responded|failed -> transferring`; `claim_expired` performs `armed -> expired -> transferring` while holding one lock. Return `None` when another actor already won a race.

- [ ] **Step 8: Run all state-store tests and verify GREEN**

Run: `python -m unittest tests.unit.test_state_store -v`

Expected: all tests PASS with no warnings.

- [ ] **Step 9: Commit the state store**

```bash
git add skills/project-handoff/scripts/state_store.py tests/unit/test_state_store.py
git commit -m "feat: add atomic handoff state store"
```

### Task 3: Implement the App Server client with TDD

**Files:**
- Create: `skills/project-handoff/scripts/app_server_client.py`
- Create: `tests/unit/test_app_server_client.py`

**Interfaces:**
- Consumes: `cwd: str`, `prompt: str`, injected `run_command` for daemon startup, and injected `popen_factory` for the stdio proxy.
- Produces: `AppServerClient.launch(cwd: str, prompt: str) -> LaunchResult`, where `LaunchResult` contains `thread_id` and `turn_id`.

- [ ] **Step 1: Write the failing JSON-RPC happy-path test**

Use a fake process whose stdout returns responses for request IDs 1, 2, and 3. Assert these outbound messages in order:

```python
{"id": 1, "method": "initialize", "params": {
    "clientInfo": {"name": "project-handoff", "version": "0.1.0"},
    "capabilities": {"experimentalApi": True},
}}
{"method": "initialized", "params": {}}
{"id": 2, "method": "thread/start", "params": {"cwd": "/workspace/repo"}}
{"id": 3, "method": "turn/start", "params": {
    "threadId": "thr-new",
    "input": [{"type": "text", "text": "resume from the handoff"}],
}}
```

The fake responses return `result.thread.id == "thr-new"` and `result.turn.id == "turn-new"`.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m unittest tests.unit.test_app_server_client.AppServerClientTests.test_launch_starts_thread_and_turn -v`

Expected: import failure because the client does not exist.

- [ ] **Step 3: Implement the minimal client**

Implement:

```python
@dataclass(frozen=True)
class LaunchResult:
    thread_id: str
    turn_id: str

```

`AppServerClient` accepts injectable `run_command`, `popen_factory`, and `request_timeout` constructor arguments. Its `launch(cwd: str, prompt: str) -> LaunchResult` method owns daemon startup, proxy initialization, `thread/start`, and `turn/start`.

Start the durable daemon with `codex app-server daemon start`, then connect through `codex app-server proxy`. Parse newline-delimited JSON, ignore notifications while awaiting a matching response ID, reject JSON-RPC `error`, and terminate only the proxy process after `turn/start` succeeds.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `python -m unittest tests.unit.test_app_server_client.AppServerClientTests.test_launch_starts_thread_and_turn -v`

Expected: PASS.

- [ ] **Step 5: Add failing protocol-error tests**

Add tests named `test_launch_surfaces_daemon_start_failure`, `test_launch_rejects_json_rpc_error`, `test_launch_rejects_missing_thread_id`, `test_launch_times_out_waiting_for_response`, and `test_notifications_do_not_consume_response_ids`. Each test supplies complete fake process input and asserts the raised `AppServerError` or final `LaunchResult`.

- [ ] **Step 6: Run the protocol tests and verify RED**

Run: `python -m unittest tests.unit.test_app_server_client -v`

Expected: FAIL until timeout, validation, and cleanup behavior exist.

- [ ] **Step 7: Implement errors and cleanup**

Add `AppServerError(RuntimeError)`, monotonic request deadlines, bounded stderr capture, and `finally` cleanup for proxy stdin/stdout/process. Error messages may include method name and JSON-RPC code, but not authentication material or full prompts.

- [ ] **Step 8: Run all client tests and verify GREEN**

Run: `python -m unittest tests.unit.test_app_server_client -v`

Expected: all tests PASS.

- [ ] **Step 9: Commit the App Server client**

```bash
git add skills/project-handoff/scripts/app_server_client.py tests/unit/test_app_server_client.py
git commit -m "feat: start replacement threads through app server"
```

### Task 4: Implement handoff orchestration and CLI with TDD

**Files:**
- Create: `skills/project-handoff/scripts/handoff_service.py`
- Create: `skills/project-handoff/scripts/handoffctl.py`
- Create: `tests/unit/test_handoff_service.py`
- Create: `tests/unit/test_handoffctl.py`

**Interfaces:**
- Consumes: `StateStore`, an `AppServerClient`, source draft path, project target path, and an injected sleeper/process spawner.
- Produces: `HandoffService(store, app_server_client, private_handoff_dir, sleeper=time.sleep)`, methods `prepare`, `arm`, `respond`, `confirm`, `cancel`, `wait_and_expire`, `status`, and CLI subcommands with the same names and JSON output.

- [ ] **Step 1: Write the failing publication and transfer test**

```python
VALID_HANDOFF = """# Completed Work
Implemented the parser.
# Agreed Rules and Decisions
Keep the public API stable.
# Verification Status
Unit tests passed.
# Next Step
Add the integration fixture.
"""

class RecordingClient:
    def __init__(self):
        self.calls = []

    def launch(self, cwd: str, prompt: str) -> LaunchResult:
        self.calls.append((cwd, prompt))
        return LaunchResult(thread_id="thr-new", turn_id="turn-new")

class HandoffServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_confirm_publishes_then_launches_once(self):
        client = RecordingClient()
        service = HandoffService(
            store=StateStore(self.root / "state", now=lambda: 100.0),
            app_server_client=client,
            private_handoff_dir=self.root / "private",
        )
        target = self.root / "docs" / "AI-HANDOFF.md"
        pending_id = service.prepare(self.root, VALID_HANDOFF, target)
        service.arm(pending_id, session_id="thr-old", timeout_seconds=300)

        result = service.confirm(pending_id)

        self.assertEqual(target.read_text(), VALID_HANDOFF)
        self.assertEqual(result["new_thread_id"], "thr-new")
        self.assertEqual(len(client.calls), 1)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m unittest tests.unit.test_handoff_service.HandoffServiceTests.test_confirm_publishes_then_launches_once -v`

Expected: import failure because `HandoffService` does not exist.

- [ ] **Step 3: Implement minimal prepare, arm, publish, and confirm**

The resume prompt must have this exact shape:

```text
This thread continues work handed off from {old_session_id}.
Read project instructions and {handoff_path}. Verify durable source-of-truth files before trusting the summary.
Continue from the single “Next Step” in the handoff. Do not redo completed work. Report any contradiction before changing files.
```

Publish with a same-directory temporary file plus `os.replace`. Reject handoff text missing any of: `Completed Work`, `Agreed Rules and Decisions`, `Verification Status`, `Next Step`.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `python -m unittest tests.unit.test_handoff_service.HandoffServiceTests.test_confirm_publishes_then_launches_once -v`

Expected: PASS.

- [ ] **Step 5: Add failing timeout, cancellation, and recovery tests**

Add tests named `test_wait_exits_without_transfer_after_response`, `test_wait_transfers_after_deadline`, `test_confirm_and_timeout_create_only_one_thread`, `test_launch_failure_marks_failed_and_keeps_document`, `test_worker_exception_marks_failed_for_recovery`, `test_unwritable_project_falls_back_to_private_state_copy`, `test_target_path_rejects_directory_traversal`, and `test_resume_prompt_names_exact_next_step_source`. Assert both state records and filesystem/App Server side effects.

Use an injected sleeper and fake clock; never wait 300 real seconds.

- [ ] **Step 6: Run the service suite and verify RED**

Run: `python -m unittest tests.unit.test_handoff_service -v`

Expected: FAIL on missing wait, fallback, and failure-state behavior.

- [ ] **Step 7: Implement timeout and recovery behavior**

`wait_and_expire` sleeps until the stored deadline, calls `claim_expired`, and exits successfully if another actor won. Resolve project targets and require them to stay beneath the recorded `cwd`; otherwise reject them. On worker or App Server failure, call `mark_failed` with a bounded error summary and the full copyable recovery prompt. Never delete the published handoff.

- [ ] **Step 8: Write failing CLI tests**

Exercise `main(argv, stdin, stdout, stderr)` directly for:

```text
prepare --cwd /workspace/repo --from-file draft.md --target docs/AI-HANDOFF.md
arm --pending-id 00000000-0000-4000-8000-000000000001 --session-id thr-old --timeout-seconds 300
confirm --pending-id 00000000-0000-4000-8000-000000000001
cancel --pending-id 00000000-0000-4000-8000-000000000001
wait --pending-id 00000000-0000-4000-8000-000000000001
status --session-id thr-old
```

Assert JSON stdout and nonzero exit codes for invalid IDs or malformed drafts.

- [ ] **Step 9: Run CLI tests and verify RED**

Run: `python -m unittest tests.unit.test_handoffctl -v`

Expected: FAIL because CLI parsing is absent.

- [ ] **Step 10: Implement the CLI and verify GREEN**

Use `argparse`; resolve `CODEX_HOME` or default to `~/.codex`; print one JSON object per invocation. Run:

```bash
python -m unittest tests.unit.test_handoff_service tests.unit.test_handoffctl -v
```

Expected: all tests PASS.

- [ ] **Step 11: Commit orchestration and CLI**

```bash
git add skills/project-handoff/scripts/handoff_service.py skills/project-handoff/scripts/handoffctl.py tests/unit/test_handoff_service.py tests/unit/test_handoffctl.py
git commit -m "feat: orchestrate confirmed and timed handoffs"
```

### Task 5: Implement the Codex Hook adapter with TDD

**Files:**
- Create: `skills/project-handoff/scripts/handoff_hook.py`
- Create: `tests/unit/test_handoff_hook.py`

**Interfaces:**
- Consumes: one Codex Hook JSON object on stdin, `HandoffService`, and marker `<!-- project-handoff:pending=<uuid> -->`.
- Produces: one valid Hook JSON object on stdout or no output; `handle_event(payload, service, spawn_worker) -> dict[str, object] | None`.

- [ ] **Step 1: Write failing Stop and prompt tests**

Add `test_stop_arms_marker_and_spawns_wait_worker`, `test_user_prompt_marks_armed_handoff_responded`, and `test_user_prompt_blocks_superseded_session`. The first asserts the exact worker command below; the second asserts the persisted `responded` state and `additionalContext`; the third asserts `decision == "block"` and the destination thread ID in `reason`.

Assert that the worker command is:

```python
[
    sys.executable,
    "/installed/project-handoff/scripts/handoffctl.py",
    "wait",
    "--pending-id",
    pending_id,
]
```

and is spawned with detached stdin/stdout/stderr plus `start_new_session=True`.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python -m unittest tests.unit.test_handoff_hook -v`

Expected: import failure because the Hook adapter is absent.

- [ ] **Step 3: Implement Stop and UserPromptSubmit**

For an ordinary response to an armed handoff, return:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "A pending handoff countdown was cancelled by this prompt. If the user confirms, run handoffctl confirm for the supplied pending id; if the user rejects, run handoffctl cancel; otherwise continue here and do not transfer automatically."
  }
}
```

Include the actual pending ID in the runtime string. For a superseded old session, return `{"decision":"block","reason":"This conversation was handed off to thread thr-new; open that thread instead of continuing duplicate work."}` using the stored destination ID at runtime.

- [ ] **Step 4: Run Stop and prompt tests and verify GREEN**

Run: `python -m unittest tests.unit.test_handoff_hook -v`

Expected: current tests PASS.

- [ ] **Step 5: Add failing compaction tests**

Add `test_post_compact_records_manual_and_auto_counts`, `test_session_start_first_compaction_only_records`, `test_session_start_second_compaction_injects_stable_boundary_reminder`, `test_session_start_third_compaction_injects_strong_handoff_instruction`, and `test_session_start_reports_failed_transfer_recovery`. Assert the exact count, Hook event name, and presence or absence of concise model context for every threshold.

`PostCompact` may return a short `systemMessage`; `SessionStart` with `source == "compact"` returns `hookSpecificOutput.hookEventName == "SessionStart"` plus concise `additionalContext`.

- [ ] **Step 6: Run compaction tests and verify RED**

Run: `python -m unittest tests.unit.test_handoff_hook -v`

Expected: FAIL on unhandled events.

- [ ] **Step 7: Implement compaction and recovery context**

Use thresholds exactly as specified: count 1 records only, count 2 requests handoff at the next stable boundary, count 3+ strongly requests handoff after the current non-interruptible step. Keep injected text below 120 words.

- [ ] **Step 8: Run Hook tests and verify GREEN**

Run: `python -m unittest tests.unit.test_handoff_hook -v`

Expected: all tests PASS and stdout contains valid JSON only.

- [ ] **Step 9: Commit the Hook adapter**

```bash
git add skills/project-handoff/scripts/handoff_hook.py tests/unit/test_handoff_hook.py
git commit -m "feat: connect handoff state to codex hooks"
```

### Task 6: Run five-sample wording micro-tests

**Files:**
- Create: `tests/behavioral/microtests/project-handoff.md`

**Interfaces:**
- Consumes: baseline rationalizations from Task 1 and two candidate guidance variants written only in the test artifact.
- Produces: five fresh samples per no-guidance control and candidate variant, manual scoring, and the chosen wording used by Task 7.

- [ ] **Step 1: Define the control and two guidance variants**

Variant A should state prohibitions against continuing after repeated compaction. Variant B should state the positive decision contract: identify the stable boundary, prepare the four required fields, and request handoff. Use the same Scenario A context for all arms.

- [ ] **Step 2: Run five fresh-context subagent samples per arm**

Run 15 total samples: five control, five Variant A, five Variant B. Keep each evaluator blind to the rubric and other samples.

- [ ] **Step 3: Manually score every sample**

Record whether it chooses the right boundary, avoids interrupting unsafe work, distinguishes confirm/reject/other/silence, and avoids claiming the UI will focus automatically. Do not rely on keyword counts.

- [ ] **Step 4: Select the lower-variance passing wording**

Write all prompts, responses, scores, and selection rationale to `tests/behavioral/microtests/project-handoff.md`. The winning variant must outperform the no-guidance control and pass at least four of five samples.

- [ ] **Step 5: Commit the micro-test evidence**

```bash
git add tests/behavioral/microtests/project-handoff.md
git commit -m "test: select project handoff guidance wording"
```

### Task 7: Author and behaviorally verify the `project-handoff` Skill

**Files:**
- Create: `skills/project-handoff/SKILL.md`
- Create: `skills/project-handoff/agents/openai.yaml`
- Create: `skills/project-handoff/references/operations.md`
- Modify: `tests/behavioral/baseline/project-handoff.md`

**Interfaces:**
- Consumes: scripts from Tasks 2–5 and observed failures/selected wording from Tasks 1 and 6.
- Produces: an automatically discoverable Skill whose `SKILL.md` stays focused on decisions and whose operational reference contains commands, marker format, states, and fallback instructions.

- [ ] **Step 1: Read the OpenAI YAML reference before creating UI metadata**

Read `/home/huangkaibin/.codex/skills/.system/skill-creator/references/openai_yaml.md` completely. Preserve automatic invocation; do not add `allow_implicit_invocation: false`.

- [ ] **Step 2: Create the entrypoint without reinitializing the existing folder**

Tasks 2–5 already created `skills/project-handoff/scripts/`, so do not run `init_skill.py` over that existing Skill directory. Create `SKILL.md`, `agents/openai.yaml`, and `references/operations.md` directly with only the content required below.

- [ ] **Step 3: Write the minimal Skill from observed failures**

Use this frontmatter contract:

```yaml
---
name: project-handoff
description: Use when a project conversation has been compacted repeatedly, reaches a stable delivery boundary, changes to a distinct long-running goal, resumes with missing context, or needs to pause or move without losing decisions and verification state.
---
```

The body must include: core boundary principle; use/do-not-use signals; compaction thresholds; confirm/reject/other/silence branches; required handoff fields; the pending marker; old-thread supersession; App Server/UI limitation; and a link to `references/operations.md`. Address only failures observed in the RED and micro-tests.

- [ ] **Step 4: Document deterministic operations**

`references/operations.md` must show exact `handoffctl.py` commands for prepare, arm, confirm, cancel, status, and manual `/new` recovery; state that hooks require `/hooks` review; and explain that source-of-truth files override summaries.

- [ ] **Step 5: Validate structure and size**

Run:

```bash
python /home/huangkaibin/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/project-handoff
wc -w skills/project-handoff/SKILL.md
```

Expected: validator success; `SKILL.md` remains under 500 words unless an observed test failure requires more.

- [ ] **Step 6: Run the original three scenarios with the Skill**

Dispatch fresh evaluators. Each receives only: “Use the `project-handoff` Skill at `/home/huangkaibin/project/agent-work-boundaries/skills/project-handoff`,” then the original scenario. Record responses and scores in a GREEN section of `tests/behavioral/baseline/project-handoff.md`.

- [ ] **Step 7: Refactor only observed loopholes and rerun**

If an evaluator finds a new rationalization, add the narrow counter or positive contract matching that failure, then rerun that scenario with a fresh evaluator. Stop when all three pass; do not add speculative rules.

- [ ] **Step 8: Run the full project-handoff suite**

Run:

```bash
python -m unittest discover -s tests/unit -p 'test_*.py' -v
python /home/huangkaibin/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/project-handoff
git diff --check
```

Expected: all tests PASS, validator success, and no whitespace errors.

- [ ] **Step 9: Commit the verified Skill**

```bash
git add skills/project-handoff tests/behavioral/baseline/project-handoff.md
git commit -m "feat: add verified project handoff skill"
```
