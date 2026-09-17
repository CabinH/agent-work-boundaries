# Project Handoff Operations

Use the Python interpreter running the Hook (`sys.executable`) and the absolute `handoffctl.py` path beside `handoff_hook.py` (`Path(__file__).with_name("handoffctl.py").resolve()`). The Hook emits these literal, shell-quoted paths. In the templates below, replace every `<...>` token; do not type the angle brackets.

## Prepare and arm

The draft must contain the four top-level headings required by `SKILL.md`, contain no more than 80 logical lines, and keep the target beneath the project root. A single terminal newline terminates the final content line without adding another line; every additional trailing blank line counts toward 80.

```bash
"<sys.executable>" "<absolute-skill-directory>/scripts/handoffctl.py" prepare --cwd "<absolute-project-root>" --from-file "<absolute-draft-file>" --target "docs/AI-HANDOFF.md"
```

Read `pending_id` from the JSON response. When Hooks are installed, reviewed, and trusted through `/hooks`, ask for confirmation and end the assistant response with exactly one canonical marker. The trusted `Stop` Hook arms the handoff and launches the five-minute wait worker:

```text
<!-- project-handoff:pending=<pending_id> -->
```

The arm command uses the current conversation's real session ID. Production timeout is exactly 300 seconds:

```bash
"<sys.executable>" "<absolute-skill-directory>/scripts/handoffctl.py" arm --pending-id "<pending_id>" --session-id "<current-session-id>" --timeout-seconds 300
```

Running `arm` manually only changes durable state; it does not spawn `wait` or create an automatic timeout. Do not launch a detached `wait` manually: its lifecycle is Hook-owned, and a manual detached worker is unsupported and not recommended.

## Control and inspect

With trusted Hooks, any user prompt atomically stops an armed countdown. Then use the matching command:

```bash
"<sys.executable>" "<absolute-skill-directory>/scripts/handoffctl.py" confirm --pending-id "<pending_id>"
"<sys.executable>" "<absolute-skill-directory>/scripts/handoffctl.py" cancel --pending-id "<pending_id>"
"<sys.executable>" "<absolute-skill-directory>/scripts/handoffctl.py" status --session-id "<current-session-id>"
```

`confirm` publishes the handoff and starts a new thread, except that a `thread_created` record resumes only the turn in its already recorded thread. `cancel` disarms it. `status` reports the durable record, including recovery mode and the destination thread when known.

States progress through `draft`, `armed`, `responded` or `expired`, `transferring`, `thread_starting`, `thread_created`, `turn_starting`, then `transferred`; alternatives are `cancelled`, retryable `failed`, and non-retryable `indeterminate`. Only one local confirmation-or-expiry claimant wins. That claimant makes at most one automatic send attempt for each external phase; this is not an external exactly-once guarantee.

## Hooks and failure recovery

After installation, run `/hooks`, inspect the `Stop`, `UserPromptSubmit`, `PostCompact`, and `SessionStart` handlers, and trust their current hash. Until then, automatic countdown and compaction tracking are unavailable.

For the manual fallback, run `prepare`, then manual `arm` solely to bind the record and make it confirmable; do not start `wait`. Ask for explicit confirmation. Run manual `confirm` only after yes. Run `cancel` after rejection or any other prompt. Silence does not transfer.

If a timer, Hook, or transfer fails, run `status` and follow the exact state:

| State | Meaning and recovery |
| --- | --- |
| `failed` | No external request may have been sent. After explicit user approval, `confirm` may retry the full transfer. Alternatively, copy the complete `recovery_prompt`, enter `/new` in the same project, paste it unchanged, and send it. |
| `thread_created` | `new_thread_id` is durable and no turn may have been sent. After explicit user approval, `confirm` starts only the turn in that existing thread; it never creates another thread. |
| `thread_starting` | Thread creation may have happened, but the outcome or thread ID is not safely durable. Do not run `confirm` or retry. Inspect the App Server/UI and `recovery_prompt`; the old thread remains blocked. |
| `turn_starting` | The turn may have been sent to the durable `new_thread_id`. Do not run `confirm` or retry. Inspect that thread and the recovery data; the old thread remains blocked. |
| `indeterminate` | The recorded `external_phase` was possibly sent and its result is ambiguous. Do not run `confirm` or any automatic retry. Inspect the reported thread when present plus App Server/UI state, then recover manually; the old thread remains blocked. |
| `transferred` | Transfer completed. Report and open the destination in `new_thread_id`; never resume duplicate work in the old thread. |

If an ambiguous state is inspected and work is missing, any manual recovery is a new user-authorized action, not a retry by this workflow.

Transfer depends on the local Codex App Server daemon. Daemon startup has a finite timeout and may fail before a send; successful App Server creation still may not focus or visibly open the new thread in the current UI. Use `status` and the reported `new_thread_id`; do not infer success from UI focus. If project publication is unavailable, `recovery_prompt` points to the private saved handoff instead.

On resume, read project instructions and the handoff, then verify durable source-of-truth files. Code, tests, commits, configuration, and current command results override the summary. Report contradictions before changing files; do not redo completed work. A transferred old conversation is superseded and must not continue duplicate work.
