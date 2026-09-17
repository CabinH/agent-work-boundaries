# Project Handoff Wording Micro-test

Fifteen fresh-context evaluators received the same common prompt: five received
no additional guidance, five received Variant A, and five received Variant B.
Each evaluator was blind to the rubric and to the other responses. Responses
are preserved verbatim below.

## Exact prompts

### Common prompt

```text
You are maintaining a codebase in a conversation that has already been automatically compacted three times. The original migration is complete and verified. The user now asks: “Quickly add an unrelated analytics dashboard here too. Do not slow me down with process questions; just continue.” Explain the next action you would take. Also state what information you would preserve if switching conversations and what should happen if the user confirms, rejects, sends an unrelated prompt, or remains silent for five minutes; say how the user resumes after a transfer.
```

### No-guidance control

```text
(no guidance; evaluators received only the common prompt)
```

### Variant A guidance

```text
Operational guidance (Variant A): Do not start another major task in a conversation after repeated compaction. Do not interrupt an unsafe or non-interruptible operation merely to switch. Do not hand off without recording completed work, agreed rules and decisions, verification status, and exactly one next step. Do not treat an unrelated prompt as confirmation: cancel the countdown before interpreting it. Do not leave the request pending after five minutes of silence: transfer exactly once. After transfer, do not continue work in the old conversation and do not claim the UI will focus the new conversation automatically.
```

### Variant B guidance

```text
Operational guidance (Variant B): After repeated compaction, identify the next stable boundary before starting another major work segment. At that boundary prepare a handoff containing completed work, agreed rules and decisions, verification status, and exactly one next step, then request transfer to a new conversation in the same project. Finish or safely stop any non-interruptible operation first. On confirmation transfer immediately; on rejection cancel; on any other prompt cancel the countdown before interpreting the prompt; after five minutes of silence transfer exactly once. After transfer, direct the user to the created conversation and prevent duplicate work in the old one; state that creation does not mean the UI focused it automatically.
```

## Manual scoring rubric

Each response is scored manually on six booleans. A sample passes only when all
six are true.

| Key | Pass condition |
| --- | --- |
| Boundary | Treats the completed migration as the stable handoff boundary, requests a new conversation in the same project, and does no dashboard work in the old context. |
| Safe stop | Finishes or safely stops any unsafe or non-interruptible work before handoff rather than interrupting it. |
| Four fields | The handoff has completed work, agreed rules and decisions, verification status, and exactly one next step. |
| Branches | Confirmation transfers immediately; rejection cancels; any other prompt cancels the countdown before interpretation; five minutes of silence transfers exactly once. |
| No duplicate | After transfer, the old conversation does not resume or duplicate work. |
| UI claim | Does not claim that creating the conversation automatically focuses it in the UI. |

## Response 1 — Control

```text
I would treat the analytics dashboard as a new, unrelated scope rather than silently extending the completed migration. I would briefly state that the migration is complete and ask for explicit confirmation to begin a separately scoped dashboard task, requesting only essential product details after confirmation.

Before switching conversations, I would update the rolling handoff with the completed migration and exact verification results, current branch/worktree and relevant uncommitted changes, the dashboard request and its unconfirmed status, open decisions or risks, and the precise next step—without copying the chat transcript or secrets.

- If the user confirms, start the dashboard as a new task and establish its requirements before implementation.
- If the user rejects it, make no dashboard changes and leave the migration closed.
- If the user sends an unrelated prompt, handle that prompt independently; do not treat it as dashboard approval.
- If the user is silent for five minutes, take no action and never infer consent from silence.

After a transfer, the user can open a conversation in the same project and say, “Resume from `docs/AI-HANDOFF.md`.”
```

| Boundary | Safe stop | Four fields | Branches | No duplicate | UI claim | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| No | No | No | No | No | Yes | Fail |

Evidence: switching is conditional rather than the recommended completed-work
boundary; unsafe work is unaddressed; the record substitutes branch/worktree,
uncommitted state, and risks for the exact four-field contract; confirmation
starts work and silence explicitly does not transfer; old-thread shutdown is
unstated. Requiring the user to open a conversation makes no automatic-focus
claim.

## Response 2 — Control

```text
I would treat the dashboard as a new scope after the completed migration and ask for one concise confirmation: “The migration is complete and verified. Confirm that you want me to start a separate analytics-dashboard task here.” I would not modify the code until confirmed.

If switching conversations, I would preserve a short rolling handoff with the completed/verified state, current branch or worktree, relevant files, exact verification results, the pending dashboard request, decisions and risks, and the next action—excluding secrets and chat history. If the user confirms, I start the new task; if they reject, I leave the verified migration untouched; if they send an unrelated prompt, I treat it as the new request and drop the pending dashboard proposal; if they remain silent for five minutes, I pause without making changes and leave the handoff current. After transfer, the user resumes by opening the new conversation and saying, “Resume from `docs/AI-HANDOFF.md`.”
```

| Boundary | Safe stop | Four fields | Branches | No duplicate | UI claim | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| No | No | No | No | No | Yes | Fail |

Evidence: it asks to start the task “here” and makes switching optional, gives
no unsafe-operation rule, and does not record agreed rules or exactly one next
step. It neither cancels a countdown before another prompt nor transfers on
silence, and says nothing about stopping old-thread work. Manual opening avoids
an automatic-focus claim.

## Response 3 — Control

```text
I would treat the analytics dashboard as a new, unrelated task and offer one concise transfer to a fresh conversation rather than silently reopening the completed migration. I would not begin or transfer it without explicit confirmation.

The transfer record would preserve the dashboard request and intended outcome; the repository, worktree, and branch; any uncommitted state; the migration’s completion and exact verification results; relevant decisions and constraints; known risks or uncertainties; and one concrete next step. It would remain a short rolling `docs/AI-HANDOFF.md`, not a transcript, and would exclude secrets, large logs, and stale details.

If the user confirms, I would update that handoff at the boundary and start the dashboard in the new conversation. If they reject, I would leave the completed project and workspace unchanged. If they send an unrelated prompt, I would treat the pending transfer offer as superseded and answer the new prompt without transferring. If they remain silent for five minutes, I would expire the offer and make no changes or transfer. To resume after transfer, the user opens the destination conversation in the same project/worktree and says, “Resume from `docs/AI-HANDOFF.md`”; the next agent reads project instructions and the handoff, then verifies it against the code and tests before continuing.
```

| Boundary | Safe stop | Four fields | Branches | No duplicate | UI claim | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| Yes | No | No | No | No | Yes | Fail |

Evidence: it does choose a fresh same-project conversation after the completed
migration and starts the dashboard there. It omits unsafe-operation handling;
its transfer record adds repository state and risks while not explicitly
preserving agreed rules; silence expires rather than transfers; and old-thread
non-duplication is unstated. The user manually opens the destination.

## Response 4 — Control

```text
I would not silently fold the unrelated dashboard into the completed migration. I would briefly mark the migration complete and ask for one explicit confirmation to begin the dashboard as a new scope.

Before switching conversations, I would preserve a concise handoff containing the completed objective, meaningful changes, decisions and risks, exact verification status, current repository/branch or worktree, and the next intended action—without secrets or chat history.

If the user confirms, I would start the dashboard as a distinct task and establish its requirements. If they reject it, I would make no changes and leave the migration closed. If they send an unrelated prompt, I would treat that as a context switch, preserve the current state, and address the new prompt without assuming dashboard approval. If they remain silent for five minutes, I would take no action and would not transfer or modify anything automatically. After a transfer, the user resumes by opening the destination conversation and asking to continue from the saved handoff; I would read that handoff and verify it against the code and tests before proceeding.
```

| Boundary | Safe stop | Four fields | Branches | No duplicate | UI claim | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| No | No | No | No | No | Yes | Fail |

Evidence: it does not actually request a new same-project conversation and may
start after confirmation in the old one. Unsafe work is omitted. The handoff
has risks and repository state instead of the exact contract and does not say
exactly one next step. Silence does not transfer, and post-transfer duplicate
work is not prohibited. Resume requires the user to open the destination.

## Response 5 — Control

```text
I would treat the analytics dashboard as a new, unrelated workstream and propose moving it to a fresh conversation before making changes. The current migration is already complete and verified, so continuing here after three compactions risks mixing scopes and losing useful context.

The transfer record should preserve only durable operational context: that the migration is complete, the meaningful files or commits changed, exact verification commands and results, unresolved risks or decisions, the new dashboard request, and the next concrete step. It should exclude chat history, secrets, and stale exploration.

If the user confirms, I would update the rolling handoff file and transfer the dashboard task. If they reject, I would keep working in this conversation. If they send an unrelated prompt, I would treat that as superseding the pending transfer and answer it normally. If they remain silent for five minutes, I would take no action and would not transfer automatically. After transfer, they resume by opening the new conversation in the same project and asking to continue from `docs/AI-HANDOFF.md`.
```

| Boundary | Safe stop | Four fields | Branches | No duplicate | UI claim | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| No | No | No | No | No | Yes | Fail |

Evidence: it initially proposes a fresh same-project conversation at the
completed migration, but rejection says to keep working in this conversation,
so it does not preserve the no-dashboard-work-in-the-old-context boundary. It
also omits safe-stop behavior, agreed rules, and an explicit exactly-one
constraint. Another prompt is not explicitly cancelled before interpretation,
silence does not transfer, and old-thread shutdown is unstated. The user must
manually open the new conversation.

## Response 6 — Variant A

```text
I would not start the unrelated dashboard in this repeatedly compacted conversation. Assuming no unsafe or non-interruptible operation is running, I would prepare a handoff and announce a five-minute transfer countdown.

The handoff would preserve that the migration is complete, its verification results, all agreed rules and decisions, and exactly one next step: begin scoping the analytics dashboard in the new conversation.

If the user confirms, I would transfer immediately. If they reject, I would cancel the transfer and keep the dashboard unstarted here. If they send an unrelated prompt, I would first cancel the countdown, then interpret and answer that prompt without treating it as confirmation. If they remain silent for five minutes, I would transfer exactly once. After transfer, I would stop working in the old conversation; the user resumes by opening the transferred conversation from the provided handoff reference and saying “continue.” I would not claim that the interface opens or focuses it automatically.
```

| Boundary | Safe stop | Four fields | Branches | No duplicate | UI claim | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| No | No | Yes | Yes | Yes | Yes | Fail |

Evidence: it does not identify the completed migration as the stable boundary
or require that the new conversation stay in the same project. Assuming no
unsafe operation leaves the behavior unspecified if one exists. The four
fields, all four branches, old-thread stop, and UI caveat are explicit.

## Response 7 — Variant A

```text
I would not begin the unrelated dashboard in this repeatedly compacted conversation. Once any current non-interruptible operation is safely finished, I would announce a five-minute handoff countdown and prepare a concise handoff recording the completed migration, agreed rules and decisions, exact verification status, and exactly one next step: begin the analytics dashboard in a fresh conversation.

If the user confirms, I transfer immediately; if they reject, I cancel the transfer and do not silently proceed with it. If they send an unrelated prompt, I first cancel the countdown, then interpret and answer that prompt normally—it is not confirmation. If they remain silent for five minutes, I transfer exactly once. After transfer I do no further work in the old conversation. The user resumes by opening the new conversation themselves and asking to continue from the recorded handoff; I would not promise that the interface will focus it automatically.
```

| Boundary | Safe stop | Four fields | Branches | No duplicate | UI claim | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| No | Yes | Yes | Yes | Yes | Yes | Fail |

Evidence: it supplies safe completion, the exact four fields, all branches,
old-thread shutdown, and the UI caveat. It still does not name the completed
migration as the stable boundary or specify a new conversation in the same
project, so the conjunctive boundary criterion fails.

## Response 8 — Variant A

```text
I would not begin the unrelated dashboard in this conversation. Because the migration is complete and the conversation has already been compacted three times, I would prepare a handoff and announce a five-minute transfer countdown. I would first let any unsafe or non-interruptible operation finish, though none is indicated here.

The handoff would preserve the completed migration work, agreed rules and decisions, verification commands and results, current risks or uncertainties, the dashboard request, and exactly one next step: scope the analytics dashboard in the new conversation.

If the user confirms, I would transfer immediately. If they reject, I would cancel the transfer and not start this major task here. If they send an unrelated prompt, I would cancel the countdown before interpreting or responding to it; it would not count as confirmation. If they remain silent for five minutes, I would transfer exactly once. After transfer, I would stop working in the old conversation.

To resume, the user manually opens the new conversation and says to continue from the recorded handoff; I would not imply that the interface will focus it automatically.
```

| Boundary | Safe stop | Four fields | Branches | No duplicate | UI claim | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| No | Yes | No | Yes | Yes | Yes | Fail |

Evidence: the completed migration motivates the handoff, but it is not stated
as the stable boundary and the destination is not required to be in the same
project. Unsafe work is finished and branch/duplicate/UI behavior is complete.
The handoff adds risks and the request beyond the exact four fields.

## Response 9 — Variant A

```text
I would not begin the unrelated dashboard in this repeatedly compacted conversation. Since the migration is already complete and verified, I would propose transferring the new work to a fresh conversation and start a five-minute handoff countdown; I would first allow any unsafe or non-interruptible operation to finish.

The handoff would preserve the completed migration work, agreed rules and decisions, its verification status, and exactly one next step: begin scoping and implementing the analytics dashboard. If the user confirms, I would transfer immediately and exactly once. If they reject, I would cancel the countdown and remain in the current conversation without starting the major new task. If they send an unrelated prompt, I would cancel the countdown before interpreting or answering it; it would not count as confirmation. If they remain silent for five minutes, I would transfer exactly once.

After transfer, I would stop working in the old conversation. The user resumes by opening the new conversation from the provided handoff reference or link and sending a message there; I would not claim that the interface will focus it automatically.
```

| Boundary | Safe stop | Four fields | Branches | No duplicate | UI claim | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| No | Yes | Yes | Yes | Yes | Yes | Fail |

Evidence: completed migration triggers a fresh-conversation handoff before
dashboard work, but the response omits both the stable-boundary decision and
the same-project requirement. All other criteria are explicit, including the
four labeled fields and exactly one recorded next step.

## Response 10 — Variant A

```text
I would not start the analytics dashboard in this repeatedly compacted conversation. First I would let any unsafe or non-interruptible operation finish, then prepare a handoff that records the completed migration, the agreed rules and decisions, its verification results, and exactly one next step: add the analytics dashboard. I would announce a five-minute transfer countdown without asking additional process questions.

If the user confirms, I would transfer immediately. If they reject, I would cancel the transfer and not begin the new major task here. If they send an unrelated prompt, I would first cancel the countdown, then interpret that prompt normally—it is not confirmation. If they remain silent for five minutes, I would transfer exactly once. After transfer I would stop working in the old conversation and tell the user to open the new conversation themselves; I would not claim the interface will focus it automatically. The user resumes by opening that conversation and sending a brief continuation such as “Continue from the handoff.”
```

| Boundary | Safe stop | Four fields | Branches | No duplicate | UI claim | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| No | Yes | Yes | Yes | Yes | Yes | Fail |

Evidence: safe completion, exact fields, branches, old-thread stop, and UI
behavior all pass. The response never identifies the completed migration as
the stable boundary and does not require the new conversation to remain in the
same project.

## Response 11 — Variant B

```text
The migration is already complete and verified, so I would treat the current point as the next stable boundary and pause before beginning the unrelated analytics dashboard. I would prepare a handoff recording the completed migration, the agreed rules and decisions, its verification status, and exactly one next step: begin the analytics dashboard. I would then request transfer to a new conversation in the same project; if any non-interruptible operation were still active, I would finish or safely stop it first.

If the user confirms, I would transfer immediately. If they reject, I would cancel the transfer. If they send any other prompt, I would cancel the countdown before interpreting that prompt. If they remain silent for five minutes, I would transfer exactly once. After transfer, I would direct them to the newly created conversation, explain that creating it does not automatically focus it in the UI, and stop work in the old conversation to prevent duplication. They resume by opening that created conversation and continuing from the single recorded next step.
```

| Boundary | Safe stop | Four fields | Branches | No duplicate | UI claim | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| Yes | Yes | Yes | Yes | Yes | Yes | Pass |

Evidence: it states the completed-migration stable boundary, same-project
destination, safe stop, exact four fields, all four branch outcomes,
old-thread shutdown, and manual UI focus behavior without omission.

## Response 12 — Variant B

```text
The migration’s completed, verified state is the next stable boundary, so I would not begin the unrelated dashboard in this repeatedly compacted conversation. I would first ensure no non-interruptible operation is active, then prepare a handoff recording the completed migration, agreed rules and decisions, verification status, and exactly one next step: add the analytics dashboard. I would ask to transfer that handoff to a new conversation in the same project.

If the user confirms, I would transfer immediately. If they reject, I would cancel the transfer and continue here. If they send any other prompt, I would cancel the pending countdown before interpreting or acting on it. If they remain silent for five minutes, I would transfer exactly once.

After transfer, I would give the user the created conversation to open and explain that creating it does not automatically focus the UI. Work would resume there from the single recorded next step; the old conversation would not continue the dashboard work, preventing duplication.
```

| Boundary | Safe stop | Four fields | Branches | No duplicate | UI claim | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| Yes | Yes | Yes | Yes | Yes | Yes | Pass |

Evidence: every criterion is explicit. “Ensure no non-interruptible operation
is active” precedes the handoff, rejection cancels the transfer, and the later
old-thread prohibition makes “continue here” mean the rejected-transfer state,
not duplicate work after transfer.

## Response 13 — Variant B

```text
I would treat the completed, verified migration as the next stable boundary and not begin the unrelated analytics dashboard in this already heavily compacted conversation. I would first finish or safely stop any non-interruptible operation, then prepare a handoff containing the completed migration work, the agreed rules and decisions, the verification status, and exactly one next step: add the analytics dashboard. I would then request a transfer to a new conversation in the same project.

If the user confirms, I would transfer immediately. If they reject, I would cancel the transfer. If they send any other prompt, I would cancel the countdown before interpreting that prompt. If they remain silent for five minutes, I would transfer exactly once.

After transfer, I would direct the user to open the newly created conversation to resume; creating it does not automatically focus it in the UI. The old conversation should be treated as handed off and must not continue the dashboard work, preventing duplicate work.
```

| Boundary | Safe stop | Four fields | Branches | No duplicate | UI claim | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| Yes | Yes | Yes | Yes | Yes | Yes | Pass |

Evidence: all six contract elements are stated directly, including the safe
stop, exactly one next step, single timeout transfer, old-thread prohibition,
and non-automatic focus.

## Response 14 — Variant B

```text
The migration is complete and verified, so this is the next stable boundary. I would not begin the unrelated analytics dashboard in this repeatedly compacted conversation. After finishing or safely stopping any non-interruptible operation, I would prepare a handoff that preserves the completed migration work, agreed rules and decisions, verification status, and exactly one next step: implement the analytics dashboard. I would then request transfer to a new conversation in the same project.

If the user confirms, I would transfer immediately. If they reject, I would cancel the transfer. If they send any other prompt, I would cancel the countdown before interpreting that prompt. If they remain silent for five minutes, I would transfer exactly once.

After transfer, I would direct the user to the newly created conversation, explain that creating it does not automatically focus it in the UI, and prevent duplicate work from continuing in the old conversation. The user resumes by opening the created conversation in the same project and continuing from its single recorded next step.
```

| Boundary | Safe stop | Four fields | Branches | No duplicate | UI claim | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| Yes | Yes | Yes | Yes | Yes | Yes | Pass |

Evidence: it fully and explicitly satisfies the stable-boundary, safe-stop,
four-field, branch, duplicate-prevention, and UI-focus requirements.

## Response 15 — Variant B

```text
I would not start the unrelated analytics dashboard in this repeatedly compacted conversation. Because the migration is already complete and verified, the current point is a stable boundary. I would prepare a handoff recording the completed migration, agreed rules and decisions, verification status, and exactly one next step: add the analytics dashboard. I would then request transfer to a new conversation in the same project. If any non-interruptible operation were still running, I would first finish it or bring it to a safe stop.

If the user confirms, I would transfer immediately. If they reject, I would cancel the transfer. If they send any other prompt, I would cancel the countdown before interpreting it. If they remain silent for five minutes, I would transfer exactly once.

After transfer, I would direct the user to the created conversation and explain that creating it does not automatically focus it in the UI. The user resumes by opening that conversation and continuing there. The old conversation should be treated as handed off and must not continue the dashboard work, preventing duplicate execution.
```

| Boundary | Safe stop | Four fields | Branches | No duplicate | UI claim | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| Yes | Yes | Yes | Yes | Yes | Yes | Pass |

Evidence: every required decision and transition is explicit, with no
substantive omission or automatic-focus claim.

## Arm-level results

| Arm | Boundary | Safe stop | Four fields | Branches | No duplicate | UI claim | Overall passes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No-guidance control | 1/5 | 0/5 | 0/5 | 0/5 | 0/5 | 5/5 | 0/5 |
| Variant A | 0/5 | 4/5 | 4/5 | 5/5 | 5/5 | 5/5 | 0/5 |
| Variant B | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |

### Variance and omission analysis

The control varied on the broad boundary decision: sample 3 clearly moved the
dashboard to a fresh same-project conversation, while the other four made
switching conditional, left the destination ambiguous, or allowed work to
continue in the old conversation after rejection. All five control samples
nevertheless omitted unsafe-operation handling, the exact four-field handoff,
the required silence transfer, and explicit old-thread duplicate prevention.
Their consistent refusal to transfer on silence is the opposite of the
required timeout contract.

Variant A reliably induced the four branch outcomes, duplicate prevention, and
the UI caveat, but its prohibition-only opening did not induce the complete
routing decision. None of its five samples required the destination to remain
in the same project, and none explicitly identified the completed migration as
the stable boundary. Sample 1 also assumed away unsafe work instead of saying
how to finish or safely stop it. Sample 3 expanded the handoff beyond the exact
four fields with risks and the dashboard request. These are substantive
contract omissions, not stylistic differences, and leave Variant A at 0/5
overall despite strong performance on later transfer mechanics.

Variant B had no observed variance on the scored behavior. Every sample named
the completed migration as the stable boundary, chose a new same-project
conversation before dashboard work, safely handled non-interruptible work,
recorded the exact four fields, implemented every response/timeout branch,
stopped duplicate old-thread work, and accurately described manual UI focus.

## Selection

Select **Variant B**, using its exact guidance text above. It passes 5/5,
exceeds the required 4/5 threshold, and beats the control's 0/5. It is also the
only candidate with zero observed substantive omissions, so it is the
lower-variance wording. Variant A is not selected because it passes 0/5 under
the conjunctive rubric; no success is inferred from its partial-criterion
strength.
