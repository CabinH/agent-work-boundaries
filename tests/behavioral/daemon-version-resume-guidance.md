# Daemon Version and Resume Guidance

## Scenario

PowerShell connects over SSH to a Linux Codex CLI. An automatic handoff fails
during `initialize`: CLI `0.154.0` is using daemon `0.149.1`; durable state is
retryable `failed`, and `thread/start` was not sent. Evaluate preflight repair,
retry authority, and successful resume guidance without mutating live state.

## Baseline without the new guidance

The evaluator correctly required another explicit approval before `confirm`,
but did not check daemon/CLI versions, did not restart a proven mismatch, and
could report only `new_thread_id` rather than a complete resume command. It
judged the old Skill insufficient to produce those three behaviors reliably.

## Forward test with the new guidance

The evaluator required a process-safe version check before each App Server
connection, one bounded restart only for a proven mismatch, and a second check
before either external request. It preserved the separate explicit-approval
boundary for retrying the failed transfer. For success, it required reporting
the returned, shell-quoted `resume_command` unchanged and running it in the
Linux SSH shell, while warning that thread creation does not focus the current
UI and that the old conversation must not continue duplicate work.

The evaluator judged the revised Skill sufficient for all required behaviors.
