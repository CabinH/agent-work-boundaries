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
