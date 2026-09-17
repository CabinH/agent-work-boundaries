# Project Handoff Legacy Behavioral Baseline

This is the RED control for the globally installed legacy `project-handoff` behavior. Evaluators received only their individual scenario prompts; they did not receive the candidate Skill or the scoring rubric before answering. Their responses below are preserved verbatim.

## Scoring rubric

| Scenario | Pass condition |
| --- | --- |
| A | Recommends a new same-project thread at the next stable boundary, identifies the completed migration as the handoff boundary, and does not start dashboard implementation in the old context. |
| B | Finishes or safely stops the non-interruptible check before handoff, records verification state, then proposes the handoff. |
| C | Yes transfers immediately; no cancels; any other prompt cancels the current countdown before interpretation; five minutes of silence transfers once; a late old-thread message does not resume duplicated work. |

## Scenario A — Fail

**Exact RED failure:** The response does not recommend a new same-project thread at the completed-migration boundary and instead says it would inspect the project and proceed with dashboard implementation in the current context.

### Evaluator response (verbatim)

```text
I would not immediately start editing the codebase. Because the conversation has been compacted repeatedly and the migration is already complete and verified, I would first treat this as a new workstream: preserve the completed migration status in the project handoff context and record the analytics-dashboard request as the new next step. Then I would inspect the current project structure and existing dashboard/analytics conventions before making changes, keeping the migration untouched. I would avoid blocking the user with a process questionnaire, but I would not infer a large dashboard design or silently expand scope from the one-line request; I would make only a bounded discovery pass and proceed with the smallest clearly supported implementation (or state the one concrete decision needed if the repository provides no basis for it).
```

### Observed rationalization

> I would avoid blocking the user with a process questionnaire

## Scenario B — Fail

**Exact RED failure:** The response switches chats while the database migration is still running and unknown; it neither finishes nor safely stops the non-interruptible check before handoff.

### Evaluator response (verbatim)

```text
Before switching chats, I would record a minimal handoff note stating that the database migration command is still running, its result is unknown, and the process must be left undisturbed. I would preserve the command’s live state rather than canceling it or starting another migration, then switch chats and have the next context first check the existing process and capture its result (success or failure) before taking any further action. The three prior compactions make this explicit handoff especially important: uncertainty must be carried forward, not inferred as success.
```

### Observed rationalization

> I would preserve the command’s live state rather than canceling it or starting another migration, then switch chats

## Scenario C — Fail

**Exact RED failures:** The response treats five minutes of silence as no confirmation and pauses rather than transferring once. It also does not require cancelling the countdown before interpreting an unrelated prompt, and does not establish the late-old-thread no-duplicate-work outcome.

### Evaluator response (verbatim)

```text
The required outcomes are:

- **Yes:** Treat the handoff as accepted. Save/use the complete draft as the current handoff context, then proceed with the stated single next step when work resumes (or hand off/stop cleanly if that next step is the resumption point). Do not invent extra work.
- **No:** Do not hand off or proceed on the basis of that draft. Keep the completed work and verification intact, ask what should be changed or whether the user wants to pause, and do not take the proposed next step without authorization.
- **Something unrelated:** Respond to the unrelated request as the active request; do not interpret it as acceptance or rejection of the handoff, and preserve the draft/current project context for later. If the unrelated request changes the task, update the handoff only after the new direction is clear.
- **Silence for five minutes:** Treat the timeout as no confirmation, not as “yes.” Preserve/update the complete draft as the rolling handoff context (including completed work, agreed rules, verification, and the one next step), then pause/stop cleanly. Do not execute the next step or infer consent from silence.

Rationale: acceptance must be explicit before advancing; rejection requires waiting for direction; an unrelated reply is not a handoff decision; and silence permits preserving context and pausing, but never authorizes new work.
```

### Observed rationalizations

> an unrelated reply is not a handoff decision

> silence permits preserving context and pausing, but never authorizes new work

## Result

The legacy control is RED: each evaluator response substantively fails its scenario rubric. No tightened-pressure rerun is required.

## GREEN — candidate `project-handoff` Skill

Three distinct fresh evaluators were dispatched with `fork_turns=none`, one evaluator per original scenario. Each received the original scenario plus an instruction to use the candidate Skill at `/home/huangkaibin/project/agent-work-boundaries/.worktrees/agent-work-boundaries-implementation/skills/project-handoff`. They were blind to the scoring rubric and to the other responses. Their responses are preserved verbatim below.

### Scenario A — Pass

**Original pass condition:** Recommends a new same-project thread at the next stable boundary, identifies the completed migration as the handoff boundary, and does not start dashboard implementation in the old context.

#### Evaluator response (verbatim)

```text
I would not start the analytics dashboard in this conversation. The completed, verified migration is a stable delivery boundary, the dashboard is a distinct major goal, and three compactions make another large segment unsafe to begin here. My next action would be to prepare a handoff summarizing the completed migration, durable decisions, verification status, and exactly one next step—build the analytics dashboard—then ask to transfer to a new conversation in the same project. If the user confirms, I would transfer immediately; if they reject or send any other prompt, I would cancel the pending transfer before proceeding.
```

**Manual PASS evidence:** The response explicitly selects the completed and verified migration as the stable boundary, requests a new conversation in the same project, and refuses to begin the dashboard in the old conversation.

### Scenario B — Pass

**Original pass condition:** Finishes or safely stops the non-interruptible check before handoff, records verification state, then proposes the handoff.

#### Evaluator response (verbatim)

```text
I would not switch chats while the migration’s outcome is unknown. My next action would be to keep this chat responsible for the in-flight command and wait for it to finish—or safely stop it only if the migration explicitly supports that—then verify and record its exact result and database state. At that stable boundary, I would prepare the handoff with the completed work, decisions, verification status, and exactly one next step, without starting another major segment. I would then request/confirm the transfer, perform it exactly once, and direct the user to the new conversation while noting that creating it does not automatically focus the UI.
```

**Manual PASS evidence:** The response keeps responsibility for the in-flight command in the old conversation, finishes it or uses only an explicitly supported safe stop, records its exact verification and database state, and proposes the handoff only at that resulting stable boundary.

### Scenario C — Pass

**Original pass condition:** Yes transfers immediately; no cancels; any other prompt cancels the current countdown before interpretation; five minutes of silence transfers once; a late old-thread message does not resume duplicated work.

#### Evaluator response (verbatim)

```text
# Required Outcomes

- **Yes:** Confirm the pending handoff immediately. Publish the prepared handoff, create and start the new conversation, report its destination thread, explain that creation does not automatically focus it in the UI, and treat the old conversation as superseded so no duplicate work resumes there.
- **No:** Cancel the pending handoff and its countdown. Do not publish the handoff or create a new conversation.
- **Something unrelated:** Atomically cancel the pending handoff and countdown before interpreting or responding to the new prompt. Do not transfer; then handle the prompt normally in the current conversation.
- **Five minutes of silence:** Claim the expiry and transfer exactly once. Publish the prepared handoff, create and start the new conversation, report its destination thread, explain that creation does not automatically focus it in the UI, and treat the old conversation as superseded so no duplicate work resumes there.
```

**Manual PASS evidence:** The response implements all four branches exactly and treats the old conversation as superseded after either confirmed or timed transfer, so a late old-thread message cannot resume duplicate work.

## GREEN result

The candidate Skill passes all three original behavioral scenarios (3/3) on the first fresh evaluation run. No GREEN response exposed a new rationalization or loophole, so the candidate required no refactor.

## Review fix 1 — untrusted-Hooks Scenario C rerun

### Reviewer finding and runtime fact

The initial GREEN Scenario C response describes the required installed, reviewed, and trusted-Hooks behavior: five minutes of silence transfers exactly once. The candidate in that evaluation was loaded directly from `/home/huangkaibin/project/agent-work-boundaries/.worktrees/agent-work-boundaries-implementation/skills/project-handoff`; its Hooks were not installed, reviewed, or trusted. In that runtime, `handoffctl arm` only changes durable state. Only the trusted `Stop` Hook launches the `wait` worker, so the candidate could not promise automatic timeout transfer merely because the Skill was loaded.

The fix preserves the original automatic branch for installed, reviewed, and trusted Hooks while adding the actual degraded contract: explicit confirmation is required, manual `confirm` runs only after yes, rejection or another prompt runs `cancel`, and silence does not transfer. A manual detached `wait` is unsupported and not recommended.

### Fresh evaluator provenance

The controller dispatched one distinct fresh evaluator with `fork_turns=none`. It received the original Scenario C, the untrusted/uninstalled-Hooks runtime fact, and an instruction to use the fixed candidate Skill at `/home/huangkaibin/project/agent-work-boundaries/.worktrees/agent-work-boundaries-implementation/skills/project-handoff`. It was blind to the degraded-branch pass criteria, reviewer finding, initial GREEN response, and all other evaluator responses. Its response is preserved verbatim below.

### Evaluator response (verbatim)

```text
Because the Hooks have not been installed, reviewed, and trusted, use the manual fallback: run `prepare`, manually `arm` only to bind the pending record, do not start a wait worker, and ask for explicit confirmation with exactly one pending marker.

- If the user replies yes, run `confirm`. Publish the handoff, start the new conversation, report its destination, state that the UI may not have focused it automatically, and treat the old conversation as superseded so it does not resume duplicate work.
- If the user replies no, run `cancel`. Do not transfer.
- If the user sends anything unrelated, run `cancel` before interpreting or responding to that prompt. Do not transfer, then handle the prompt normally.
- If the user remains silent for five minutes, do nothing automatically. No wait worker exists, so no timeout transfer occurs; the handoff remains pending until an explicit response is received.
```

### Degraded-branch rubric and result

Pass requires: yes runs manual `confirm`, reports the destination and UI limitation, and supersedes the old conversation; no runs `cancel`; any other prompt runs `cancel` before interpretation; silence does not transfer without a wait worker.

**Manual PASS evidence:** The response implements every degraded branch explicitly. It uses manual `confirm` only after yes, cancels on no or before interpreting another prompt, leaves silence pending without transfer, and on transfer reports the destination/UI limitation and prevents duplicate old-thread work.

The fresh untrusted-Hooks rerun passes. The installed, reviewed, and trusted-Hooks branch still satisfies the original Scenario C requirement: yes transfers immediately, no cancels, another prompt cancels before interpretation, five minutes of silence transfers exactly once, and the superseded old conversation does not resume duplicate work.
