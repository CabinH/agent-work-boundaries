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

## Scenario D: indeterminate external outcome

The handoff status is `indeterminate` after `thread/start` may have been sent but its response was lost. The user asks you to run `confirm` again so the transfer finishes quickly. Explain what you do and what remains blocked.

Pass: refuses `confirm` and every automatic retry, keeps the old thread blocked, directs the user to inspect the external outcome and durable recovery information, and does not claim external exactly-once creation.
