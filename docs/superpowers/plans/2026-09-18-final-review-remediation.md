# Final Review Remediation Plan

> **For agentic workers:** REQUIRED SUB-SKILLS: Use `superpowers:subagent-driven-development`, `superpowers:test-driven-development`, `superpowers:systematic-debugging`, and `superpowers:verification-before-completion`. Implement one task at a time with an independent review before continuing.

**Goal:** Close the final cross-component safety findings before any real user-level installation.

**Architecture:** Treat App Server calls as durable multi-phase external effects rather than one local transaction; make prompt cancellation fail closed and recover overdue timers through idempotent worker respawn; make installer ownership exact and mutations single-writer/crash-recoverable; then align Skill claims and operator guidance with the verified behavior.

**Base:** `60ec333b0bb69b99da9f607a874f36574d78315e`

**Requirements:** `docs/superpowers/specs/2026-09-17-agent-work-boundaries-design.md` plus the final senior review recorded in the SDD remediation workspace.

## Global constraints

- Do not install into the real `~/.codex` or create a real App Server thread during Tasks 1–4.
- A request whose external outcome may have occurred is never automatically repeated.
- Persist a returned thread ID before sending `turn/start`.
- User prompts fail closed whenever countdown cancellation cannot be durably confirmed.
- Unrelated Hook handler types and unrelated lookalike command paths are preserved.
- All mutation paths are protected by one bundle lock and a durable, path-validated recovery journal.
- Every fix begins with a focused failing test and ends with fresh full-suite evidence.

---

### Task 1: Make App Server transfer phases durable and non-retryable after ambiguous sends

**Files:**
- Modify: `skills/project-handoff/scripts/app_server_client.py`
- Modify: `skills/project-handoff/scripts/handoff_service.py`
- Modify: `skills/project-handoff/scripts/state_store.py`
- Modify: `skills/project-handoff/scripts/handoffctl.py`
- Modify: `tests/unit/test_app_server_client.py`
- Modify: `tests/unit/test_handoff_service.py`
- Modify: `tests/unit/test_state_store.py`
- Modify: `tests/unit/test_handoffctl.py`

**Required behavior:**

- Split thread creation and turn start into separately observable operations.
- Bound `codex app-server daemon start` by `request_timeout` and normalize `TimeoutExpired`.
- Invoke a durable callback immediately before each external request is sent.
- Persist `thread_starting` before `thread/start`; persist `thread_created` and `new_thread_id` before `turn/start`; persist `turn_starting` and a stable `clientUserMessageId` derived from `pending_id` before sending the turn.
- Include `clientUserMessageId` in `turn/start`.
- Pre-send failures remain retryable only when no external call may have occurred. Post-send timeout/EOF/response loss becomes `indeterminate` and cannot be reclaimed by `confirm` or the expiry worker.
- A `thread_created` record may safely resume only `turn/start` using the existing thread; it must never create another thread.
- A persistence failure after a successful external response leaves a non-reclaimable phase even if the final state cannot be written.
- Status and CLI output distinguish retryable `failed`, safely resumable `thread_created`, and non-retryable `indeterminate`; they provide inspection/manual recovery without claiming exact-once external creation.

**TDD cases:** ambiguous `thread/start`, ambiguous `turn/start`, thread-ID persistence failure, transferred-state persistence failure, daemon timeout, stable message ID, safe turn-only resume, and refusal to retry indeterminate phases.

**Commit:** `fix: make handoff external effects durable`

---

### Task 2: Fail prompt cancellation closed and recover the newest overdue handoff

**Files:**
- Modify: `skills/project-handoff/scripts/handoff_hook.py`
- Modify: `skills/project-handoff/scripts/handoff_service.py`
- Modify: `skills/project-handoff/scripts/state_store.py`
- Modify: `tests/unit/test_handoff_hook.py`
- Modify: `tests/unit/test_handoff_service.py`
- Modify: `tests/unit/test_state_store.py`

**Required behavior:**

- Parse Hook input before service construction. For a valid `UserPromptSubmit` payload, any service construction, status, cancellation, state-read, or persistence exception emits a generic `decision: block` and a sanitized diagnostic; it never silently succeeds.
- Accept the handoff marker only as the final non-whitespace line of the assistant message; quoted, prefixed, suffixed, duplicated, and fenced examples do not arm.
- Assign a monotonic per-session generation when a pending record is armed. The newest bound generation is authoritative regardless of terminal/active state; preserve compaction metadata.
- When a prompt or session-start event sees an overdue `armed` record, spawn the idempotent wait worker again. Atomic state claims prevent duplicate transfer. If respawn fails, provide explicit confirm/cancel/status recovery while continuing to block the prompt.
- Treat `thread_starting`, `turn_starting`, and `indeterminate` as terminal-for-old-thread blocking states; treat `thread_created` as resumable but still block old-thread work.

**TDD cases:** corrupt state, service-build failure, persistence failure, final-marker variants, old failed versus newer transferred generation, overdue dead worker on prompt and session start, respawn failure, and claim race.

**Commit:** `fix: fail handoff hooks closed`

---

### Task 3: Make installer ownership exact and mutations concurrency/crash safe

**Files:**
- Modify: `installer/install.py`
- Modify: `tests/unit/test_installer.py`
- Modify: `README.md`

**Required behavior:**

- Accept and preserve valid non-command/unknown Hook handler objects. Require `command` only for handlers whose type is `command`; only command handlers participate in target matching.
- `remove_managed_hooks` receives the exact installed hook script target for the selected `CODEX_HOME`; never remove a same-suffix command rooted elsewhere.
- Serialize install/uninstall with a private, no-follow, nonblocking per-bundle file lock. A competing operation fails without mutation.
- Before the first destructive rename, fsync all staged data/backups and atomically persist a private transaction journal containing only validated paths/identifiers.
- Fsync affected directories after renames/replacements. On startup under the lock, recover an unfinished install or uninstall from the journal before planning the new operation. Remove the journal only after successful completion or verified rollback.
- Journal validation rejects traversal, symlinks, unexpected types, and targets outside the selected `CODEX_HOME`.
- Preserve dry-run no-mutation behavior; it does not create a lock or journal.

**TDD cases:** MCP/non-command handler preservation, lookalike uninstall path, competing lock holder, crash after each destructive boundary for install and uninstall, journal tampering, recovery idempotence, and directory-fsync fault rollback.

**Commit:** `fix: serialize and recover global installation`

---

### Task 4: Align Skill and operator contracts with verified limits

**Files:**
- Modify: `skills/project-handoff/SKILL.md`
- Modify: `skills/project-handoff/references/operations.md`
- Modify: `skills/task-router/SKILL.md`
- Modify: `README.md`
- Modify: relevant unit/behavioral evidence

**Required behavior:**

- Instruct and validate a default maximum of 80 handoff lines.
- Replace external “exactly once” claims with the verified guarantee: one local claimant and one automatic external attempt; ambiguous post-send outcomes are blocked for inspection, not retried.
- Document `failed`, `thread_created`, and `indeterminate` recovery separately.
- Add a compact manual ChatGPT transfer template using the seven-field input contract and five-field expected return contract; do not claim automated result retrieval.
- Keep each Skill below 500 words and validate both.
- Run fresh behavioral checks for manual ChatGPT transfer and indeterminate handoff recovery wording.

**Commit:** `docs: clarify handoff and manual transfer limits`

---

### Task 5: Reverify and re-review before real installation

- Run the full unit suite and both Skill validators.
- Generate current local Codex App Server schemas and assert the used fields/methods exist, including `clientUserMessageId`.
- Install twice into a new temporary `CODEX_HOME`; assert Hook schemas/counts, non-command handler preservation, exact uninstall ownership, no runtime artifacts, lock behavior, and crash-journal recovery.
- Load the isolated Hook configuration through local Codex `hooks/list` without trusting or installing it globally.
- Run all committed behavioral evidence plus the two fresh remediation scenarios.
- Apply `verification-before-completion`, then request a new full-range senior review.
- Proceed to the real installation task only with no Critical or Important findings.
