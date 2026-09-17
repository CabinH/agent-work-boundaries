---
name: project-handoff
description: Use when a project conversation has been compacted repeatedly, reaches a stable delivery boundary, changes to a distinct long-running goal, resumes with missing context, or needs to pause or move without losing decisions and verification state.
---

# Project Handoff

## Core principle

Move work at a stable boundary, preserving durable decisions and verification rather than relying on the old conversation's context.

## When to hand off

- First compaction: record it; continue.
- Second compaction: request handoff at the next stable boundary.
- Third or later: after the current non-interruptible step, strongly request handoff before another major segment.
- Hand off sooner when a delivery is complete and the next goal is distinct, context must be repeatedly rebuilt, decisions conflict, or the user asks to pause or move.

Do not hand off merely for a small supporting task or while the current operation cannot be safely interrupted. A healthy conversation pursuing the same goal can continue.

## Decision contract

After repeated compaction, identify the next stable boundary before starting another major work segment. At that boundary prepare a handoff containing completed work, agreed rules and decisions, verification status, and exactly one next step, then request transfer to a new conversation in the same project. Finish or safely stop any non-interruptible operation first.

With installed Hooks reviewed and trusted through `/hooks`: on confirmation transfer immediately; on rejection cancel; on any other prompt cancel the countdown before interpreting the prompt; after five minutes of silence transfer exactly once. After transfer, direct the user to the created conversation and prevent duplicate work in the old one; state that creation does not mean the UI focused it automatically.

Until Hooks are installed and trusted, automatic countdown and compaction tracking are unavailable. Require explicit confirmation: manually `arm` without a wait worker, run `confirm` only after yes, and run `cancel` after rejection or any other prompt. Silence does not transfer.

The draft uses these top-level headings:

- `# Completed Work`
- `# Agreed Rules and Decisions`
- `# Verification Status`
- `# Next Step` containing exactly one next step

Run `prepare`, ask whether to transfer, and end the request with exactly one marker:

`<!-- project-handoff:pending=<pending_id> -->`

After transfer, treat the old conversation as superseded: report the destination thread instead of resuming work here.

Read [references/operations.md](references/operations.md) before preparing, controlling, diagnosing, or manually recovering a handoff.
