# Agent Work Boundaries

This bundle installs the `project-handoff` and `task-router` Skills and merges
four project-handoff handlers into the user-level Codex Hooks configuration.
The installer preserves unrelated Skills and Hook handlers, uses staged
replacement, and emits one JSON report for every successful operation.

## Install and activate

Preview the exact targets and backup plan. A dry run validates existing files
but does not create the Codex home:

```sh
./install.sh --dry-run
```

Install or upgrade the bundle:

```sh
./install.sh
```

The JSON report lists `changed_paths`, `backed_up_files`, and `backup_root`.
Hooks are installed but remain inactive until their current hash is trusted.
Finish activation with this required operator step:

```text
Open Codex and run /hooks, review the four Agent Work Boundaries handlers, and trust their current hash.
```

The four handlers are `Stop`, `UserPromptSubmit`, `PostCompact`, and
`SessionStart`. Until they are trusted, automatic five-minute transfer and
compaction tracking are inactive. Explicit manual confirmation through
`handoffctl.py confirm` remains available.

Handoff drafts are limited to 80 logical lines. One terminal newline closes
the final content line; an additional trailing blank line counts as another
line. A transfer has one durable local claimant and at most one automatic send
attempt per external phase. This does not promise external exactly-once
creation: possibly sent requests stop for inspection and are not retried.

Each transfer has its own private handoff snapshot. New threads read that
snapshot, while `docs/AI-HANDOFF.md` remains the project's latest copy.
The preparing Agent passes the source conversation's communication language
with `prepare --conversation-language`; continuation preserves it unless the
user requests otherwise. Omitted language and older records default to Chinese.

The Task Router's ChatGPT route is manual in version 1. Its template carries
goal, inputs/files, allowed changes, forbidden content, expected format,
verification, and stop condition; it requests conclusion, evidence locations,
verification, risks/uncertainties, and one next step. The user manually sends
the brief and manually copies the result back. No web, connector, or result
retrieval automation is claimed.

Install and uninstall are serialized by a private, persistent bundle lock. If
another bundle operation is already running, the competing command exits
without changing either Skill or `hooks.json`; retry it after the first command
finishes. A dry run creates neither the Codex home, the lock, nor a recovery
journal.

To share that same lock with a concurrently starting first install, a non-dry
uninstall against a missing Codex home creates the home and persistent lock,
then reports an empty change set. It creates no Skills, Hooks, journal, or
backup. Use `--dry-run` when even that lock bootstrap is unwanted.

Before changing an installed target, the installer fsyncs its staged files and
timestamped backups, then records an identifier-only private transaction
journal. If a process or machine stops mid-operation, rerun the same non-dry-run
install or uninstall command: while holding the lock, it first restores the
pre-operation Skills and Hooks, removes only its validated staging paths, and
then starts the requested operation. A corrupt, linked, non-private, or
otherwise invalid lock or journal is rejected for manual inspection instead of
being followed or deleted. Both control files must remain owner-only `0600`,
single-link regular files; each open file descriptor is matched back to the
canonical path before it can authorize recovery, mutation, or cleanup.

## Status

Inspect a pending, completed, or failed handoff with:

```sh
python3 ~/.codex/skills/project-handoff/scripts/handoffctl.py status --session-id <current-session-id>
```

Replace `<current-session-id>` with the real ID of the current Codex session;
the angle-bracket token is user-supplied command syntax, not an implementation
field. On transfer, App Server creates and starts the new thread, but the
current Codex UI may not focus it automatically. Use the reported, copyable
`resume_command` to open it with the correct absolute working directory.

Recovery depends on the reported state:

- `transferring` may be an abandoned pre-send claim. When `status` reports a
  `recovery_command`, running it checks the worker lock and changes an abandoned
  claim to `failed` without sending a request. It leaves live workers and later
  phases unchanged. Check status again before following the `failed` rules;
  legacy claims without a worker-lock marker require manual inspection.
- `failed` is retryable only because no external request may have been sent;
  after explicit approval, `confirm` retries the full transfer.
- `thread_created` has a durable destination; after explicit approval,
  `confirm` starts only the missing turn in that existing thread.
- `thread_starting`, `turn_starting`, and `indeterminate` may have external
  effects. Do not run `confirm` or retry them; inspect App Server/UI state and
  the recovery fields while the old thread remains blocked.
- `transferred` reports the destination in `new_thread_id` and the complete
  `codex resume <id> -C <cwd>` command; use it and do not resume duplicate work
  in the old conversation.

Before each connection, the controller checks the running daemon's version
against the CLI under a process-safe lock. It restarts the daemon only when a
mismatch is proven, allows up to 90 seconds for that rare lifecycle operation,
verifies the result, and otherwise fails before sending an external request.
The local proxy is spoken as WebSocket rather than newline-delimited stdio. A
daemon or socket-permission failure can therefore produce `failed` before any
send, while a lost response after a possible send produces a blocked
inspection state. UI focus is not a success signal; rely on durable status and
the reported destination. With PowerShell over SSH, run `resume_command` in
the Linux SSH shell; it targets the remote absolute working directory. If a
managed command sandbox blocks the remote user's `~/.codex` Unix socket, the
controller invocation needs narrow approval outside that sandbox.

## Uninstall and restore

Preview removal with `./install.sh --uninstall --dry-run`, then remove only the
two managed Skills and the four managed Hook handlers:

```sh
./install.sh --uninstall
```

Install, upgrade, and uninstall protect replaced files under the timestamped
backup root reported as `backup_root`:

```text
~/.codex/backups/agent-work-boundaries/YYYYMMDDTHHMMSSZ
```

If two operations start in the same second, the later directory has a suffix
such as `-01`. A first install into an empty Codex home has no prior files to
back up, so `backup_root` is `null`.

For manual restoration, first uninstall the current managed bundle, select the
specific timestamped directory from an earlier JSON report, and copy back only
the entries you intend to restore:

```sh
./install.sh --uninstall
AWB_BACKUP="$HOME/.codex/backups/agent-work-boundaries/YYYYMMDDTHHMMSSZ"
cp -a "$AWB_BACKUP/skills/project-handoff" "$HOME/.codex/skills/"
cp -a "$AWB_BACKUP/skills/task-router" "$HOME/.codex/skills/"
cp "$AWB_BACKUP/hooks.json" "$HOME/.codex/hooks.json"
```

Some backups may contain only the paths that existed before that operation.
Inspect the selected directory before copying. Restoring `hooks.json` restores
the complete saved file, including unrelated handlers as they existed at that
timestamp; run `/hooks` again afterward because its trusted hash may have
changed.
