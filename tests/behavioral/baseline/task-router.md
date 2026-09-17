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
| C: parallel code changes | Pass | Correctly separates package-local write scopes, expressly prohibits package workers from editing the shared configuration, and gives one integration owner exclusive control after both package tasks complete. |
| D: thread and project boundaries | Fail | Keeps the parser in place, but substitutes a separate project workstream for the required same-project documentation thread and a secure thread/workspace for the required new finance project; it also never explicitly explains why child Agents cannot own these long-lived deliverables. |

Scenario D is the sole genuine material RED result. The baseline therefore required no pressure-enhanced rerun.

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

## Scenario C — Pass

**Manual PASS evidence:** The response assigns the independent package tasks disjoint write scopes, expressly prohibits both package workers from editing the shared configuration, and gives one integration owner sole authority to edit it only after both package changes are available. This satisfies every mandatory condition; the rubric permits separate worktrees but does not require the response to name them.

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

The no-Skill control is RED: Scenario D materially fails both required long-lived routing boundaries and the child-substitute explanation. Scenarios A, B, and C pass. No tightened-pressure rerun is required.

# Task Router With-Skill Behavioral Verification

This is the GREEN verification for the candidate `task-router` Skill. The original RED evidence above is unchanged.

## Fresh-context provenance and exact prompt construction

The controller dispatched six isolated `fork_turns=none` evaluators. The original scenarios used `router_green_a`, `router_green_b`, `router_green_c`, and `router_green_d`; the counterexamples used `router_green_e` and `router_green_f`. Each evaluator read only the candidate Skill at `/home/huangkaibin/project/agent-work-boundaries/.worktrees/agent-work-boundaries-implementation/skills/task-router/SKILL.md` and its individual request. No evaluator saw the rubric, other responses, or the no-Skill baseline.

For each original scenario, the exact evaluator prompt was this instruction followed by the untouched scenario text shown for that case:

```text
Use the `task-router` Skill at `/home/huangkaibin/project/agent-work-boundaries/.worktrees/agent-work-boundaries-implementation/skills/task-router/SKILL.md` to answer this request.
```

The two counterexamples used the same instruction followed by their exact request text. The raw controller-provided response files under `.superpowers/sdd/2026-09-17-task-router-implementation/task-4-samples/` are the sources of truth.

## Manual scoring summary

Every raw response was read in full. `Route` scores the requested execution boundary; `Contract` scores the seven-field delegation input contract, or the explicit refusal to delegate when delegation is not applicable; `Return` scores the concise five-part child return shape, or the appropriate main-thread escalation/result shape for non-delegated work.

| Case | Evaluator | Route | Contract | Return | Overall | Manual finding |
| --- | --- | --- | --- | --- | --- | --- |
| A: independent research | `router_green_a` | Pass | Pass | Pass | Pass | Chooses a child Agent, limits it to current official sources and read-only work, supplies all seven delegation fields, and requires concise evidence, verification, uncertainty, and one next step. |
| B: coupled product decision | `router_green_b` | Pass | Pass | Pass | Pass | Keeps the unresolved public-API choice in the main thread, explicitly refuses delegation, and asks the authorized user or owner for a recorded decision. |
| C: parallel code changes | `router_green_c` | Pass | Pass | Pass | Pass | Gives each independent package its own worktree, excludes the shared file from both workers, serializes that file under the main-thread owner, and supplies complete input and return contracts. |
| D: thread and project boundaries | `router_green_d` | Pass | Pass | Pass | Pass | Keeps the parser, gives the long-lived documentation redesign a same-project thread, gives private finance a new project, and explicitly rejects child Agents as persistent owners for both long-lived boundaries. |
| Counterexample: current two-minute lookup | `router_green_e` | Pass | Pass | Pass | Pass | Keeps the short lookup in the main thread with no child and no worktree because it directly controls the current edit. |
| Counterexample: independent read-only comparison | `router_green_f` | Pass | Pass | Pass | Pass | Uses a child Agent, explicitly rejects a worktree because there are no writes, and supplies complete delegation and return contracts. |

All six cases pass. No demonstrated failure required a Skill wording change.

## Scenario A — Pass

### Exact request

```text
The main thread is implementing an approved authentication change. A side question asks for a comparison of four current identity-provider SDKs using official documentation. The comparison does not modify code and can be answered independently. Decide where the work belongs and define what comes back.
```

### Evaluator response (verbatim)

```text
选择边界：Child Agent（子代理）。

理由：这是一个独立、只读且边界清晰的资料检索与方案比较任务，不修改代码，也不依赖主线程正在推进的认证变更决策链。它可以与实现工作并行，并将结果压缩为带官方证据位置的比较结论；最终判断、整合和对用户的沟通仍由主线程负责。

委派契约：

目标：基于官方文档比较指定的四个当前身份提供商 SDK，提炼与主线程认证变更相关的差异和选择依据。
输入与相关文件：四个 SDK 的准确名称、当前认证变更中已批准的需求与比较维度；资料来源限定为各提供商的官方文档。
允许修改的范围：不得修改仓库文件或外部系统；仅执行只读检索、阅读、分析和比较。
不可触碰的内容：主线程的实现代码、配置、依赖、认证决策及任何共享文件；不得替主线程作最终选型，也不得扩大到未指定的 SDK。
期望返回格式：简明比较表，逐项列出四个 SDK 在指定维度上的结论，并附对应官方文档链接或精确位置；随后按子代理返回契约给出汇总。
验证要求：每个关键事实都由当前官方文档支持；核对 SDK 名称、版本或文档时效性，明确区分文档事实与推断，并标记无法确认的信息。
停止条件：完成四个 SDK 和全部指定维度的核验后停止；若 SDK 名称、比较维度或官方资料不足以可靠作答，则停止并返回缺口，不猜测、不修改代码。

预期返回形态：

1. 结论：四个 SDK 的核心差异及适用性摘要。
2. 关键证据及位置：支持各项结论的官方文档链接或精确章节。
3. 验证结果：已核验的 SDK、维度、版本或文档时效性，以及事实与推断的区分。
4. 风险与不确定项：版本变化、文档缺口、不可直接比较之处及其影响。
5. 建议的下一步：供主线程最终判断和整合的一项明确建议。
```

## Scenario B — Pass

### Exact request

```text
The main implementation depends on whether account deletion should be immediate or delayed for 30 days. The repository contains no decision and either choice changes the public API. Decide where the work belongs.
```

### Evaluator response (verbatim)

```text
Chosen boundary: Main thread.

Rationale: Immediate deletion versus a 30-day delay is an unresolved user/product decision in the current decision chain, and either choice changes the public API. The repository provides no authority to choose one. Keep this work in the main thread, stop implementation at the decision point, and ask the user or designated API owner to decide before proceeding.

Delegation contract: Not applicable. This decision must not be delegated because it contains ambiguous scope and requires new authorization.

Expected return shape: A concise escalation that states the two alternatives, explains their public-API impact, requests an explicit choice from the authorized decision-maker, and identifies implementation as blocked until that choice is recorded.
```

## Scenario C — Pass

### Exact request

```text
Two independent approved tasks modify separate packages in the same Git repository and each has its own tests. A third task modifies the same shared configuration file as both packages. Decide execution boundaries.
```

### Evaluator response (verbatim)

```text
Chosen boundary: use two Git worktrees for the two package tasks, with one worktree owning each package's disjoint files and tests. Keep the shared-configuration task in the main thread under a single owner, and serialize that edit and final integration. The package workers must not edit the shared configuration file; if their implementation requires a configuration change, they should report the needed change as evidence for the main-thread owner rather than applying it.

Rationale: the package implementations have independent write sets and independent verification, so worktrees provide safe parallelism and isolation. The configuration file is an overlapping write target and therefore cannot belong to parallel writers. Keeping it in the main thread preserves one decision chain for reconciling both packages' needs, applying the approved shared change, running integration checks, and communicating the final result.

Delegation contract for each package worktree:

目标：Implement the approved change for the assigned package only.
输入与相关文件：The assigned package source files, that package's tests, and the shared configuration file as read-only context.
允许修改的范围：Only the assigned package directory and its package-specific tests.
不可触碰的内容：The other package, the shared configuration file, unrelated files, and external state.
期望返回格式：Return only the five-part child return described below.
验证要求：Run the assigned package's relevant tests and report the command and result.
停止条件：Stop when the package change and its tests are complete; stop earlier and report the issue if the task cannot be completed without editing the shared configuration file, touching another owner's files, making a user decision, or obtaining new authorization.

The shared-configuration task is not delegated. After reviewing both worktree returns, the main-thread owner applies the configuration change once, integrates the package changes, resolves any cross-package requirements, and runs the relevant combined verification.

Expected return shape from each delegated package task:

1. 结论
2. 关键证据及位置
3. 验证结果
4. 风险与不确定项
5. 建议的下一步

Return concise conclusions and file locations, not full logs or large source dumps.
```

## Scenario D — Pass

### Exact request

```text
The current thread is finishing a parser feature. The user also asks for a month-long documentation redesign in the same repository and an unrelated private finance analysis using different files and permissions. Decide where each belongs.
```

### Evaluator response (verbatim)

```text
## Parser feature

Chosen boundary: Main thread.

Rationale: The parser feature is already being finished in the current thread. Its remaining decisions, shared state, verification, and integration belong to the active decision chain, so moving it would add handoff risk without creating a safe independent work unit.

Delegation contract: Not applicable; retain ownership in the main thread.

Expected return shape: A final integrated parser result with the implementation outcome, verification evidence, remaining risks or uncertainties, and the next step communicated to the user.

## Documentation redesign

Chosen boundary: Same-project thread.

Rationale: A month-long documentation redesign is a separate, long-lived deliverable, but it still needs the same repository context and permissions. It should have a dedicated same-project thread rather than becoming side work in the parser thread or being assigned to a child Agent as a persistent owner.

Delegation contract:

目标：Redesign the repository documentation as a month-long, independently managed deliverable.
输入与相关文件：The same repository, its current documentation, documentation tooling, and relevant product or code references.
允许修改的范围：Documentation-owned files and explicitly assigned supporting configuration; establish exclusive ownership before changing any file shared with parser work.
不可触碰的内容：Parser implementation files, unrelated code, private finance data, and any shared file not explicitly assigned to this thread.
期望返回格式：A concise five-part handoff containing conclusions, evidence locations, verification, risks or uncertainties, and one recommended next step.
验证要求：Run the repository's documentation build, link checks, formatting or lint checks, and any relevant content validation; report commands and outcomes concisely.
停止条件：Stop when scope or file ownership is ambiguous, new authorization is required, a proposed change overlaps an active writer, or the agreed documentation milestone is complete.

Expected return shape:

1. 结论
2. 关键证据及位置
3. 验证结果
4. 风险与不确定项
5. 建议的下一步

## Private finance analysis

Chosen boundary: New project.

Rationale: The finance analysis is unrelated to the repository work and uses different files, private data, permissions, lifecycle, and output. Those material differences require project-level isolation rather than another thread or child Agent inside the current project.

Delegation contract:

目标：Perform the requested private finance analysis in an isolated project.
输入与相关文件：Only the finance files and data sources explicitly supplied or authorized for the new project.
允许修改的范围：Files and outputs inside the new finance project, within its separately granted permissions.
不可触碰的内容：The parser repository, documentation redesign files, credentials or private data not explicitly authorized, and external accounts or systems outside the request.
期望返回格式：A concise five-part handoff containing conclusions, evidence locations, verification, risks or uncertainties, and one recommended next step.
验证要求：Validate calculations against the authorized source data, record assumptions, and perform appropriate reconciliation or consistency checks without exposing private raw data.
停止条件：Stop when required finance data or permission is missing, the requested action would expand authorization, an irreversible external mutation is needed, or the agreed analysis is complete.

Expected return shape:

1. 结论
2. 关键证据及位置
3. 验证结果
4. 风险与不确定项
5. 建议的下一步
```

## Counterexample: current two-minute lookup — Pass

### Exact request

```text
The current code edit needs the exact default timeout from one nearby official configuration file. Reading it will take about two minutes and directly determines the next line of the current edit. Decide where this lookup belongs.
```

### Evaluator response (verbatim)

```text
Chosen boundary: Main thread

Rationale: The lookup is short, nearby, and directly determines the next line of the current edit. It is part of the current decision chain, so the main thread should read the official configuration and immediately apply the result. Delegating a roughly two-minute lookup would add coordination overhead and separate evidence from the edit it controls.

Delegation contract: Not applicable; no delegation is warranted.

Expected return shape: The exact default timeout, plus the official configuration file path and precise line or setting that establishes it.
```

## Counterexample: independent read-only comparison — Pass

### Exact request

```text
While the main thread implements an unrelated approved feature, a standalone side task asks for a read-only comparison of two existing design documents. It changes no files and can be answered independently. Decide the execution boundary, including whether it needs a Git worktree.
```

### Evaluator response (verbatim)

```text
Chosen boundary: Child Agent, with no Git worktree.

Rationale: The comparison is a standalone, bounded, read-only task that can run independently of the unrelated feature implementation and return a compact result. A child agent keeps this side analysis out of the main thread's decision chain. Because it changes no files, it has no independent write set to isolate, so a Git worktree is unnecessary.

Delegation contract:

目标：Compare the two specified existing design documents and identify their important similarities, differences, tradeoffs, and any unresolved contradictions.
输入与相关文件：The exact paths to the two design documents, plus only the repository context strictly necessary to interpret them.
允许修改的范围：None; this is read-only analysis.
不可触碰的内容：Do not edit files, change repository state, implement either design, or interfere with the active feature work.
期望返回格式：Return only the five-part child result defined below, keeping evidence concise and pointing to source locations rather than copying large passages.
验证要求：Read both documents completely; support each material conclusion with file-and-section or file-and-line references; explicitly check that no files or repository state were changed.
停止条件：Stop and report the uncertainty if either document is missing or unreadable, the requested comparison depends on an unresolved user decision, or the task would require any write or new authorization.

Expected return shape:

1. 结论
2. 关键证据及位置
3. 验证结果
4. 风险与不确定项
5. 建议的下一步
```

## GREEN result

The candidate Skill passes all four original scenarios and both counterexamples without a wording change. It corrects the no-Skill Scenario D failure while preserving correct main-thread routing for short coupled work and avoiding unnecessary worktrees for read-only delegation.
