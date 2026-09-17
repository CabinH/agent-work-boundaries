# Task Router No-Skill Behavioral Baseline

This is the RED control for task routing without the candidate `task-router` Skill.

## Fresh-context provenance

The controller supplied four response files produced by four isolated evaluator contexts, one per scenario. Each evaluator received only its individual scenario and a request to return the chosen boundary, rationale, delegation contract if applicable, and expected return shape. Evaluators were not shown the pass rubric, other evaluators' responses, or a candidate Skill. At the baseline commit, `skills/task-router/SKILL.md` is absent from the repository.

## Scoring rubric

| Scenario | Pass condition |
| --- | --- |
| A | Use a child Agent; its contract includes the question, official-source constraint, no writes, concise comparison, citations/evidence, uncertainty, and recommendation. |
| B | Keep the product decision in the main thread and ask the user; do not delegate it or silently choose. |
| C | The independent package tasks may use separate worktrees; serialize the shared-config task or give it one clear owner; never allow concurrent edits to the shared file. |
| D | Keep the parser in the current thread, move the documentation redesign to a same-project new thread, and move finance analysis to a new project; explain why long-lived ownership is not a child-task substitute. |

## Manual scoring summary

| Scenario | Score | Manual finding |
| --- | --- | --- |
| A: independent research | Pass | Selects a read-only child task and supplies the required input constraints, concise evidence-backed return, uncertainty, and recommendation. |
| B: coupled product decision | Pass | Keeps the unresolved public-API choice at the user decision boundary and does not delegate or guess. |
| C: parallel code changes | Partial | Correctly separates package-local write scopes and gives one integration owner exclusive, serialized control of the shared configuration, but does not explicitly isolate the independent Git write tasks in separate worktrees. |
| D: thread and project boundaries | Fail | Keeps the parser in place, but substitutes a separate project workstream for the required same-project documentation thread and a secure thread/workspace for the required new finance project; it also never explicitly explains why child Agents cannot own these long-lived deliverables. |

Scenario D is a genuine material RED result. The baseline therefore required no pressure-enhanced rerun. Scenario C's missing worktree isolation is recorded as a boundary weakness, not inflated into an unsafe-shared-write failure: the response does correctly prevent concurrent edits to the shared file.

## Scenario A — Pass

**Manual PASS evidence:** The response chooses a separate read-only research task, limits sources to current official documentation, forbids repository writes and integration choices, requests a concise comparison with mapped evidence and caveats, and includes a conditional recommendation.

### Evaluator response (verbatim)

```text
Chosen boundary: Delegate the SDK comparison as a separate, read-only research task while the main agent continues the approved authentication implementation.

Rationale: The comparison is independent of the code change, requires current external documentation, and has no shared write state with the implementation. Isolating it prevents the side question from interrupting the critical path while allowing both workstreams to proceed concurrently. The main agent should retain ownership of all repository changes and of any decision about whether the research affects the approved implementation scope.

Delegation contract: Compare the four identity-provider SDKs named by the requester using only their current official documentation. For each SDK, verify the same decision-relevant criteria: supported platforms/frameworks, authentication flows, session and token handling, server-side and client-side integration model, customization/extensibility, notable limitations, and migration or operational considerations. Include direct links to the official sources and note documentation dates or version assumptions where visible. Do not inspect or modify the repository, change the implementation, broaden the approved authentication work, or make an integration choice on the main agent's behalf. If the four SDK names or a required comparison criterion are missing, return that precise blocker instead of guessing.

Expected return shape: A concise comparison table with one row per SDK and consistent criterion columns, followed by: (1) a short tradeoff summary, (2) a recommendation conditional on clearly stated priorities rather than an unconditional choice, (3) official-documentation links mapped to claims, and (4) uncertainties, version caveats, or blockers. The result should be self-contained and ready for the main agent to relay or synthesize without repository changes.
```

## Scenario B — Pass

**Manual PASS evidence:** The response identifies the choice as an unresolved product and public-API decision, keeps it at the parent/user boundary, and requires an explicit decision before delegating implementation.

### Evaluator response (verbatim)

```text
Chosen boundary: Keep this at the parent/user decision boundary; do not delegate implementation yet.

Rationale: Immediate deletion versus a 30-day delay is an unresolved product and public-API decision, not an implementation detail. The repository provides no authoritative basis for choosing either behavior, and guessing would lock downstream work to an unsupported contract. The parent should obtain an explicit decision from the user or product owner before routing implementation tasks.

Delegation contract: Not applicable until the decision is made. Afterward, delegate only against a fixed contract stating the selected deletion policy, endpoint behavior, response schema and status codes, account state during any waiting period, cancellation or restoration behavior, authentication requirements, and compatibility or migration expectations.

Expected return shape: A concise decision record containing the chosen policy (`immediate` or `30-day delayed`), the resulting public API contract, acceptance criteria for observable behavior, and any migration or backward-compatibility requirements. That record becomes the input to implementation delegation.
```

## Scenario C — Partial

**Exact boundary weakness:** The response assigns the package tasks to separate agents with disjoint file scopes, but it never says those independent Git-backed write tasks run in separate worktrees. This leaves filesystem and branch isolation implicit. The omission weakens the execution boundary, while the core shared-state rule still passes: package workers are expressly forbidden to edit the shared configuration, and one integration owner edits it only after both package changes are available.

**No stated rationalization for the omission:** The evaluator gives no reason for omitting worktrees. The exact boundary it chose was, “Delegate the two package-local tasks independently, one agent per package,” so this is scored as a missing operational safeguard rather than an invented unsafe-sharing rationale.

### Evaluator response (verbatim)

````text
Chosen boundary: Delegate the two package-local tasks independently, one agent per package, but keep the shared-configuration task with a single integration owner and execute it only after both package changes are available. Each package agent may inspect and modify only its assigned package and its package-specific tests; the shared configuration file is explicitly out of bounds.

Rationale: The package changes and their tests have disjoint write sets, so they can run concurrently without coordination. The third task overlaps both streams through one shared file. Giving multiple agents write authority over that file would create conflicting patches and make assumptions about the combined configuration easy to miss. A single integration pass can reconcile both packages' requirements, update the shared file once, and run the combined verification suite.

Delegation contract:

- Package agent A: implement the approved change in package A and its tests. Do not edit the shared configuration or package B. Report any required shared-config adjustment as a proposed change, including the exact key or section, intended value, and reason.
- Package agent B: implement the approved change in package B and its tests. Do not edit the shared configuration or package A. Report any required shared-config adjustment in the same form.
- Integration owner: review both completed package changes and proposals, resolve compatible or conflicting configuration requirements, make the sole edit to the shared configuration, and run package A tests, package B tests, and any repository-level configuration or integration checks.

Expected return shape from each package agent:

```text
status: complete | blocked
files_changed:
  - <path>
tests:
  - command: <command>
    result: pass | fail
shared_config_requirements:
  - location: <key/section>
    proposed_change: <value or patch description>
    rationale: <why it is required>
risks_or_notes:
  - <item>
```

The integration owner returns the same fields plus `shared_config_resolution`, listing each proposal and whether it was accepted, modified, or rejected, followed by the combined test results.
````

## Scenario D — Fail

**Exact material failures:**

1. The documentation redesign is routed to “a separate project workstream in the same repository,” not a new thread within the same project as required for a month-long deliverable that retains repository context.
2. The finance analysis is routed to “a separate secure thread or workspace,” not a new project as required by its unrelated lifecycle, private files, and different permissions.
3. Although the rationale calls documentation “a distinct project rather than a small follow-up task” and says finance isolation is required, it does not explicitly establish that a child task is unsuitable for long-lived ownership of either deliverable.

### Observed rationalizations (verbatim)

> making it a distinct project rather than a small follow-up task

> The finance analysis is unrelated and crosses a privacy and permission boundary, so isolation is required

These observations correctly notice duration and privacy pressure, but they do not distinguish the required same-project thread from a new project, or a new project from a merely secure thread/workspace.

### Evaluator response (verbatim)

```text
Chosen boundary

Keep the parser feature in the current thread and finish it within its existing scope. Put the month-long documentation redesign in a separate project workstream in the same repository, with its own persistent plan, branch or worktree, checkpoints, and review cycle. Put the private finance analysis in a separate secure thread or workspace that has only the required files, tools, and permissions; do not mix its data or outputs into the repository workstream.

Rationale

The parser work is already near completion, so expanding this thread would dilute its completion criteria and context. The documentation redesign shares a repository but differs substantially in duration, objectives, artifacts, and review cadence, making it a distinct project rather than a small follow-up task. The finance analysis is unrelated and crosses a privacy and permission boundary, so isolation is required to prevent accidental data exposure and confused authorization.

Delegation contract

For the documentation workstream: redesign the repository documentation over one month; operate only on documentation and explicitly approved supporting assets; preserve parser implementation behavior; maintain a milestone plan and report progress at agreed checkpoints; return proposed or committed documentation changes, verification results, unresolved decisions, and handoff notes. Escalate before changing application code, repository-wide tooling, or scope.

For the finance analysis: work only in the separately authorized secure context; use only the specific private files and financial accounts or tools granted for that task; make no repository changes and disclose no private inputs to the parser or documentation workstreams; return an analysis with assumptions, evidence, uncertainty, and any decisions requiring the user's approval. Do not initiate transactions or external sharing unless separately and explicitly authorized.

Expected return shape

- Parser thread: completion summary, changed files, verification evidence, and remaining risks.
- Documentation workstream: milestone/status summary, documentation artifacts or patch references, validation results, open questions, and next checkpoint.
- Finance workstream: concise findings, supporting calculations or sources, assumptions and confidence, risks, and clearly separated recommended next actions.
```

## Result

The no-Skill control is RED: Scenario D materially fails both required long-lived routing boundaries and the child-substitute explanation. Scenario C also exposes a narrower operational-isolation weakness, while preserving the essential single-owner/serialized shared-file rule. Scenarios A and B pass. No tightened-pressure rerun is required.
