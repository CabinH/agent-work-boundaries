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
