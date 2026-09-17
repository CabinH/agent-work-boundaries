# Project Handoff Operations

Use the Python interpreter running the Hook (`sys.executable`) and the absolute `handoffctl.py` path beside `handoff_hook.py` (`Path(__file__).with_name("handoffctl.py").resolve()`). The Hook emits these literal, shell-quoted paths. In the templates below, replace every `<...>` token; do not type the angle brackets.

## Prepare and arm

The draft must contain the four top-level headings required by `SKILL.md`. Keep the target beneath the project root.

```bash
"<sys.executable>" "<absolute-skill-directory>/scripts/handoffctl.py" prepare --cwd "<absolute-project-root>" --from-file "<absolute-draft-file>" --target "docs/AI-HANDOFF.md"
```

Read `pending_id` from the JSON response. Normally, ask for confirmation and end the assistant response with exactly one canonical marker; the trusted `Stop` Hook arms the five-minute worker:

```text
<!-- project-handoff:pending=<pending_id> -->
```

To arm manually, use the current conversation's real session ID. Production timeout is exactly 300 seconds:

```bash
"<sys.executable>" "<absolute-skill-directory>/scripts/handoffctl.py" arm --pending-id "<pending_id>" --session-id "<current-session-id>" --timeout-seconds 300
```

## Control and inspect

Any user prompt atomically stops an armed countdown. Then use the matching command:

```bash
"<sys.executable>" "<absolute-skill-directory>/scripts/handoffctl.py" confirm --pending-id "<pending_id>"
"<sys.executable>" "<absolute-skill-directory>/scripts/handoffctl.py" cancel --pending-id "<pending_id>"
"<sys.executable>" "<absolute-skill-directory>/scripts/handoffctl.py" status --session-id "<current-session-id>"
```

`confirm` publishes the handoff and starts a new thread. `cancel` disarms it. `status` reports the durable record, including the destination thread or recovery prompt.

States progress through `draft`, `armed`, `responded` or `expired`, `transferring`, then `transferred`; terminal alternatives are `cancelled` and `failed`. Only one confirmation or expiry claim can transfer.

## Hooks and failure recovery

After installation, run `/hooks`, inspect the `Stop`, `UserPromptSubmit`, `PostCompact`, and `SessionStart` handlers, and trust their current hash. Until then, automatic countdown and compaction tracking are inactive. Use `prepare`, manual `arm`, and—only after the user confirms—manual `confirm`; use `cancel` on rejection or another prompt.

If a timer, Hook, or transfer fails, run `status`. A `failed` result includes `recovery_prompt`; retry `confirm` if automatic creation is appropriate, or recover manually:

1. Copy the complete `recovery_prompt` from the `status` JSON.
2. Enter `/new` in the same project.
3. Paste the prompt unchanged and send it.

App Server may create and start a thread without focusing it in the UI. Open the reported thread ID yourself. If project publication is unavailable, the prompt points to the private saved handoff instead.

On resume, read project instructions and the handoff, then verify durable source-of-truth files. Code, tests, commits, configuration, and current command results override the summary. Report contradictions before changing files; do not redo completed work. A transferred old conversation is superseded and must not continue duplicate work.
