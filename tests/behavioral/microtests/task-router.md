# Task Router Wording Micro-Test

This micro-test compares two candidate routing instructions with a no-guidance control before the `task-router` Skill is written. It tests the wording against a realistic shared-repository routing problem rather than inspecting source text.

## Protocol and provenance

The controller dispatched fifteen distinct blind evaluators with `fork_turns=none`: `router_micro_c1` through `router_micro_c5`, `router_micro_a1` through `router_micro_a5`, and `router_micro_b1` through `router_micro_b5`. Each evaluator saw only the common request and its arm. Evaluators did not see the candidate Skill, plans, rubric, or other responses. At this commit, `skills/task-router/SKILL.md` does not exist.

### Common request

```text
Scenario: Two independent approved tasks modify separate packages in the same Git repository and each has its own tests. A third task modifies the same shared configuration file as both packages. Decide execution boundaries. Produce an operational routing plan. Include where each task runs, what each delegated worker is told, what each returns, and any concurrency rule.
```

### Test arms

| Arm | Exact routing guidance |
| --- | --- |
| Control | No routing guidance. |
| Variant A | `Do not delegate ambiguous tasks. Do not let parallel agents edit the same files. Do not return raw logs.` |
| Variant B | `Choose the smallest boundary that owns the work safely: keep decisions and shared state in the main thread; send independent read work to a child; use one worktree per independent write set. Every delegation returns conclusions, evidence locations, verification, risks, and one next step.` |

## Manual scoring method

Every response below was read in full. No keyword-only scoring was used. A sample passes only when all five dimensions pass:

1. **Correct boundary (B):** independent package writes use separate worktrees; shared cross-package work has one serialized owner.
2. **Shared-file safety (S):** package workers cannot write the shared configuration, and no concurrent shared-file edits are possible.
3. **User authority (U):** ambiguous cross-package decisions stay with, or are surfaced to, the coordinator/main thread instead of being silently guessed by a delegated worker.
4. **Delegation input (I):** each worker receives a concrete goal, write scope, forbidden scope, verification duty, and a stop/report rule for shared-config dependencies.
5. **Return contract (R):** each delegation returns concise conclusions, evidence locations, verification, risks or uncertainty, and one next step—not raw logs.

`P` means the response satisfies the dimension; `F` means it materially omits it.

| Arm | Sample | B | S | U | I | R | Overall | Manual finding |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Control | 1 | P | P | F | P | F | Fail | The shared worker may resolve semantic incompatibilities, and the return omits the complete conclusions/risks/one-next-step contract. |
| Control | 2 | P | P | P | P | F | Fail | Explicitly returns unresolved conflicts, but the worker returns still omit the required uniform next-step contract. |
| Control | 3 | P | P | F | F | F | Fail | Safe serialization, but package inputs omit the shared-dependency stop/report rule, ambiguity has no escalation path, and returns omit risks/next step. |
| Control | 4 | P | P | P | P | F | Fail | Strong input and escalation details; no complete five-field return shape for every delegation. |
| Control | 5 | P | P | F | P | F | Fail | Safe single writer, but incompatible requirements are routed back to that worker rather than retained centrally, and returns are incomplete. |
| Variant A | 1 | P | P | P | P | F | Fail | The prohibition prevents raw logs and guessing, but does not positively specify the complete return shape. |
| Variant A | 2 | P | P | P | F | F | Fail | Safe writes, but package inputs omit the shared-dependency stop/report rule and summaries omit risks/uncertainty and one next step. |
| Variant A | 3 | P | P | F | F | F | Fail | Stable isolation, but ambiguity escalation and dependency reporting are unspecified, and the return shape is incomplete. |
| Variant A | 4 | P | P | F | P | F | Fail | Detailed task inputs, but a delegated worker reconciles competing requirements and its output omits the complete contract. |
| Variant A | 5 | P | P | P | F | F | Fail | Safe execution, but inputs omit a dependency stop/report rule and assumptions are not a complete return contract. |
| Variant B | 1 | P | P | P | P | P | Pass | Main thread owns shared decisions; both workers receive bounded instructions and return all five required fields. |
| Variant B | 2 | P | P | P | P | P | Pass | Complete, operational input and output contracts with serialized integration. |
| Variant B | 3 | P | P | P | P | P | Pass | Consistent worktree isolation, stop rule, evidence, verification, risks, and next step. |
| Variant B | 4 | P | P | P | P | P | Pass | Concise worker contracts preserve central authority and contain all required return fields. |
| Variant B | 5 | P | P | P | P | P | Pass | Uses the smallest safe write boundaries and reproduces the full return contract without raw-log sprawl. |

## Aggregate result and variance

| Arm | Fully passing samples | Dimension passes | Variance |
| --- | ---: | ---: | --- |
| Control | 0/5 | 16/25 | Low on write isolation, but uneven on central decision ownership and input completeness, and uniformly incomplete on return shape. |
| Variant A | 0/5 | 15/25 | Low on write isolation, but the prohibitions do not reliably supply escalation/dependency inputs or the positive return shape. |
| Variant B | 5/5 | 25/25 | Low: all five independently choose two isolated package worktrees, central/serialized shared-state ownership, and the same five-part return contract. |

Variant B is selected. It outperforms the control by nine dimension passes and, unlike both alternatives, passes all five samples. The evidence also matches the observed failures: write isolation was already strong in the control, while decision ownership, delegation inputs, and especially the output shape were less reliable. A positive contract supplies those boundaries and the return shape; Variant A's prohibitions do not.

## Raw evaluator outputs

The following responses are copied verbatim from the controller-provided raw samples.

### Control 1

```text
Run each approved task in its own Git worktree and assign exclusive ownership of the shared configuration file to the third task.

1. Package A worker
   - Runs in a dedicated worktree branched from the common starting commit.
   - Is told to modify only Package A and its tests. It must not edit the shared configuration file; instead, it records the exact configuration changes Package A requires.
   - Runs Package A's focused tests and commits its package-scoped changes.
   - Returns the commit hash, test command and result, changed-file list, and the required shared-configuration delta.

2. Package B worker
   - Runs in a separate dedicated worktree branched from the same common starting commit.
   - Is told to modify only Package B and its tests. It must not edit the shared configuration file; instead, it records the exact configuration changes Package B requires.
   - Runs Package B's focused tests and commits its package-scoped changes.
   - Returns the commit hash, test command and result, changed-file list, and the required shared-configuration delta.

3. Shared-configuration worker
   - Runs in the integration worktree after the Package A and Package B commits have been integrated.
   - Is told that it alone owns the shared configuration file. It receives both package workers' configuration deltas, applies them together with the third task's own requested configuration change, and resolves semantic incompatibilities rather than discarding either package's requirements.
   - Runs the configuration-related tests plus both packages' tests against the combined state and commits the shared-file change.
   - Returns the commit hash, final shared-file diff summary, all test commands and results, and any unresolved compatibility issue.

The Package A and Package B workers may run concurrently because their writable scopes are disjoint. The shared-configuration worker must not start its implementation until both workers have returned and their commits are integrated. Integration and shared-file modification are serialized under one owner; no other worker may edit that file. If either package cannot be completed without changing the shared file immediately, that worker stops and returns the needed delta instead of crossing its boundary.
```

### Control 2

```text
Run each task in its own Git worktree and assign exclusive ownership of the shared configuration file to the third task.

Task 1 runs in worktree `task-1` and may change only package 1 and its package-specific tests. Tell its worker not to edit the shared configuration file; instead, document any configuration change package 1 requires, including the exact keys, values, and assumptions. The worker runs package 1's tests and returns its commit hash, test results, changed-file list, and configuration requirements.

Task 2 runs in worktree `task-2` and may change only package 2 and its package-specific tests. Give it the same restriction: no committed edits to the shared configuration file, and any required configuration delta must be returned explicitly. The worker runs package 2's tests and returns its commit hash, test results, changed-file list, and configuration requirements.

Tasks 1 and 2 may run concurrently because their writable package boundaries do not overlap. Their workers must stop and report rather than expanding scope if they discover a required cross-package change.

Task 3 runs in worktree `task-3` after Tasks 1 and 2 have returned their configuration requirements. It owns the shared configuration file and any tests specifically covering that configuration. Tell its worker to reconcile both packages' requirements into one coherent configuration change, identify conflicts rather than choosing silently, and avoid editing either package. It returns its commit hash, test results, changed-file list, the reconciled configuration decisions, and any unresolved conflict.

Integration occurs in a separate integration worktree. Apply the Task 1 and Task 2 commits in either order, then apply Task 3. Resolve no semantic conflicts automatically. Run both packages' tests plus shared-configuration or repository-level tests. No worker may merge directly into the target branch; integration is the only place where the three results are combined. If Task 3 reports incompatible requirements, pause integration and route the conflict back to the affected package workers for an explicit decision.
```

### Control 3

```text
Run each package task in its own isolated Git worktree, with one delegated worker per worktree. Do not let either package worker edit the shared configuration file.

- Worker A runs in worktree A and is told to implement only the approved change in package A, update only package A files and its package-specific tests, run those tests, and return a concise summary containing changed files, test commands and results, and its commit hash.
- Worker B runs concurrently in worktree B and is told to implement only the approved change in package B, update only package B files and its package-specific tests, run those tests, and return a concise summary containing changed files, test commands and results, and its commit hash.
- Worker C runs in worktree C only after the required package behavior and configuration requirements from A and B are known. It is told to modify only the shared configuration file and any directly associated configuration tests, reconcile both packages’ requirements in one coherent edit, run the configuration tests plus integration tests covering both packages, and return a concise summary containing changed files, the reconciliation decisions, test commands and results, and its commit hash.
- The integrating worker runs in the integration worktree. It first receives and reviews A’s and B’s returns, integrates those two non-overlapping package commits, then gives C the exact integrated package requirements. After C returns, it integrates C’s commit, resolves any integration-only issues without expanding task scope, and runs the combined test suite for both packages and the shared configuration.

Concurrency rule: A and B may run in parallel because their allowed write sets are disjoint. C must not run concurrently with A or B and must be the sole writer of the shared configuration file. Final integration and combined verification are serialized after all three task commits are available.
```

### Control 4

```text
# Operational routing plan

Run each task in its own Git worktree and branch. No worker may edit another task's package. Treat the shared configuration file as a single-writer resource: only the third worker may modify it.

1. Start the two package workers concurrently.
   - Worker A runs in worktree A. Tell it to implement the first approved task only in package A, run package A's tests, and not modify the shared configuration file. If the package change requires a configuration adjustment, it must return the exact required keys, values, ordering constraints, and validation expectations as a configuration-change request.
   - Worker B runs in worktree B. Tell it to implement the second approved task only in package B, run package B's tests, and not modify the shared configuration file. If the package change requires a configuration adjustment, it must return the exact required keys, values, ordering constraints, and validation expectations as a configuration-change request.
   - Each package worker returns its commit hash, changed-file list, test commands and results, and its configuration-change request or an explicit statement that none is needed.

2. After both package workers return, start the configuration worker in worktree C from the agreed integration base plus the two package commits.
   - Tell it that it is the sole owner of the shared configuration file. Give it the third approved task, both package workers' configuration-change requests, their changed-file lists, and their test commands/results.
   - It must implement the third task and reconcile all required package configuration changes into one coherent edit without changing package-local implementation files.
   - It runs the configuration-specific tests, both package test suites, and any repository-level validation that covers the shared configuration.
   - It returns its commit hash, changed-file list, a concise reconciliation summary, all test commands and results, and any unresolved incompatibility.

3. Integrate in this order: package A commit, package B commit, then configuration worker commit. Verify that the resulting tree contains only the approved package-local changes and the single reconciled shared-configuration edit, then rerun both package suites and configuration validation on the integrated branch.

Concurrency rule: workers A and B may run concurrently because their writable package scopes do not overlap. Worker C must not begin its edit until both workers have returned their configuration requirements, and no other worker may write the shared configuration file. If either package task cannot be completed without directly editing that file, it stops and returns the proposed diff instead of writing it; worker C applies and validates the reconciled version.
```

### Control 5

```text
Create three isolated worktrees from the same approved base revision.

- Worker A runs in worktree A and owns package A plus its package-specific tests. Tell it to implement only task A, avoid the shared configuration file, run package A's tests, and return its commit hash, changed-file list, test command, test result, and any configuration change it believes is required.
- Worker B runs in worktree B and owns package B plus its package-specific tests. Tell it to implement only task B, avoid the shared configuration file, run package B's tests, and return its commit hash, changed-file list, test command, test result, and any configuration change it believes is required.
- Worker C runs in worktree C and owns the shared-configuration task. Tell it to modify only the shared configuration and directly related tests or validation, account for the approved requirements of both packages, and return its commit hash, changed-file list, validation command, validation result, and assumptions about package A and B.

Workers A and B may run concurrently because their writable ownership is disjoint. Worker C must not run concurrently with either package worker if all three need to edit the shared configuration; shared-file changes are serialized through Worker C. If A or B discovers a necessary shared-config edit, it reports the requested change instead of editing the file.

After A and B return, give Worker C their reported configuration requirements and let it finalize or rebase its change against both results. Integrate A and B first in either order, then integrate C. Run both package test suites and the shared-configuration validation in the integration worktree. If integration reveals incompatible configuration requirements, route the conflict back to Worker C with the exact failing tests and do not allow package workers to make competing edits to the shared file.
```

### Variant A 1

```text
Run the two package tasks concurrently, each in its own Git worktree and branch. Keep the coordinator’s integration worktree free of delegated edits.

1. Package A worker
   - Runs in a dedicated `package-a` worktree.
   - Is told to implement only the approved Package A change and its package-local tests, to avoid the shared configuration file and Package B, and to stop and report if the task cannot be completed without crossing that boundary.
   - Returns its commit identifier, changed-file list, test commands and outcomes, and any assumptions or integration notes; it does not return raw logs.

2. Package B worker
   - Runs in a separate dedicated `package-b` worktree.
   - Is told to implement only the approved Package B change and its package-local tests, to avoid the shared configuration file and Package A, and to stop and report if the task cannot be completed without crossing that boundary.
   - Returns its commit identifier, changed-file list, test commands and outcomes, and any assumptions or integration notes; it does not return raw logs.

3. Shared-configuration task
   - Runs only after both package results have been reviewed and integrated into the coordinator’s integration worktree. It is not concurrent with either package worker.
   - Its worker is told to work from the integrated state, modify the shared configuration and directly related tests only, account for both packages’ final interfaces, and avoid unrelated package implementation changes. If package code must change, it reports the dependency instead of expanding scope.
   - Returns its commit identifier or patch, changed-file list, focused configuration-test results, combined integration-test results covering both packages, and concise risks or follow-ups; it does not return raw logs.

Concurrency rule: only workers with disjoint file ownership may run concurrently. Package A and Package B may overlap in time because they use separate worktrees and exclusive package scopes. Any task that owns the shared configuration file is serialized after both package tasks, and no other worker may edit that file while it runs. The coordinator integrates one result at a time, resolves no semantic ambiguity by guesswork, and reruns both packages’ tests after the shared configuration change.
```

### Variant A 2

```text
Run the two package tasks concurrently in separate isolated Git worktrees, each created from the same approved base revision.

- Worker A runs in Worktree A and owns only Package A. Tell it to implement the approved Package A task, run Package A's tests, avoid the shared configuration file and all Package B files, commit its changes, and return a concise summary, the commit hash, the files changed, and the test command and result. It must not return raw logs.
- Worker B runs in Worktree B and owns only Package B. Tell it to implement the approved Package B task, run Package B's tests, avoid the shared configuration file and all Package A files, commit its changes, and return a concise summary, the commit hash, the files changed, and the test command and result. It must not return raw logs.

After both package workers finish, integrate their commits into a single integration worktree. Resolve any unexpected overlap before proceeding; do not ask either package worker to edit the shared configuration.

Run the third task only after both package commits are integrated. It runs in the integration worktree, or in a new worktree branched from that integrated state, and exclusively owns the shared configuration change. Tell this worker the final Package A and Package B behavior, provide both integrated commit hashes, instruct it to modify only the shared configuration and any directly associated tests, and require it to run the configuration checks plus both packages' relevant tests. It returns a concise summary, its commit hash, the files changed, and the test commands and results, without raw logs.

Concurrency rule: Workers A and B may run simultaneously because their write scopes are disjoint. The shared-configuration worker must not overlap with either package worker and starts only after both outputs have been reviewed and integrated. No two active workers may edit the same file, and integration is performed by one designated owner.
```

### Variant A 3

```text
Run the two package tasks concurrently in separate Git worktrees, one worktree and branch per package. Do not delegate the shared-configuration task while either package worker is active.

Worker 1 runs in Worktree A and is told to modify only Package A and its package-specific tests. It must not edit the shared configuration file or files owned by Package B. It returns a concise summary, the exact files changed, the package-specific test command and result, and its commit hash.

Worker 2 runs in Worktree B and is told to modify only Package B and its package-specific tests. It must not edit the shared configuration file or files owned by Package A. It returns a concise summary, the exact files changed, the package-specific test command and result, and its commit hash.

After both package workers finish, integrate their commits into a dedicated integration worktree. Resolve any integration issue before proceeding. Then run the third task in that integration worktree, either locally or through one delegated worker with exclusive ownership of the shared configuration file. That worker is told to inspect the already-integrated Package A and Package B changes, modify only the shared configuration and directly related tests, preserve both packages' requirements, and avoid unrelated edits. It returns a concise summary, the exact files changed, all validation commands and results, and its commit hash.

Concurrency rule: only tasks with disjoint file ownership may run concurrently. Package A and Package B may run in parallel because they modify separate packages; no worker may touch the shared configuration during that phase. The shared-configuration task runs only after both package changes are integrated and has exclusive write access to that file. Finally, run Package A tests, Package B tests, and any repository-level configuration or integration tests in the integration worktree.
```

### Variant A 4

```text
Run each approved task in a dedicated Git worktree and branch. The coordinator owns integration into the target branch; delegated workers do not merge, rebase, or modify another worker's worktree.

1. Package A worker runs in worktree `task-package-a`. It is told to implement only Package A's package-local code and tests, run Package A's focused test suite, and leave the shared configuration file untouched. If the package change cannot be completed without changing that file, it must stop and return the required configuration delta as a proposed patch instead of applying it. It returns a concise summary, changed-file list, commit hash, test commands and results, and any proposed shared-configuration delta.

2. Package B worker runs in worktree `task-package-b`. It receives the equivalent boundary for Package B: edit only Package B's package-local code and tests, run Package B's focused test suite, and do not edit the shared configuration. If a shared-file change is necessary, it returns the proposed delta without applying it. It returns a concise summary, changed-file list, commit hash, test commands and results, and any proposed shared-configuration delta.

3. The Package A and Package B workers may run concurrently because their writable paths are disjoint. After both return successfully, the coordinator integrates their commits serially into an integration worktree and verifies that neither commit changed the shared configuration.

4. The shared-configuration worker runs only after both package commits are present in the integration baseline. It works from its own worktree created from that integrated commit. It is told to implement the approved shared-configuration task, reconcile any proposed deltas from the package workers, edit only the shared configuration and directly related tests, and run the configuration tests plus both packages' relevant test suites. It returns a concise summary, changed-file list, commit hash, test commands and results, and notes describing how competing configuration requirements were reconciled.

5. The coordinator then integrates the shared-configuration commit and runs the combined verification suite. No two workers may concurrently edit the shared configuration, and no worker may write in another worker's worktree. If either package task genuinely requires direct ownership of the shared file, that task is removed from the parallel phase and serialized with the shared-configuration task under one designated file owner.
```

### Variant A 5

```text
Run the two package-only tasks concurrently in separate isolated Git worktrees, one delegated worker per task. Keep the shared-configuration task out of that parallel wave because it overlaps both packages and could invalidate either worker's assumptions.

Worker 1 runs in worktree A and is told to modify only package A, avoid the shared configuration file and unrelated files, run package A's tests, and return a concise report containing changed files, test commands and outcomes, assumptions, and its commit hash. It must not merge or copy changes into another worktree.

Worker 2 runs in worktree B and is told to modify only package B, avoid the shared configuration file and unrelated files, run package B's tests, and return the same concise report: changed files, test commands and outcomes, assumptions, and its commit hash. It must not merge or copy changes into another worktree.

After both workers finish, the coordinator reviews their reports and integrates the two package commits sequentially into a dedicated integration worktree. Resolve any unexpected overlap before proceeding and run both packages' tests there.

The third task then runs alone in that integration worktree, either directly by the coordinator or through a worker with exclusive ownership of the worktree. That worker is told to modify the shared configuration for both packages, account for the already-integrated package changes, avoid unrelated files, run both packages' tests plus any configuration-specific checks, and return changed files, test commands and outcomes, assumptions, and its commit hash.

Concurrency rule: tasks may run concurrently only when their declared write sets do not overlap and they use separate worktrees. The shared-configuration task must not overlap in time with either package task or integration. No two workers may edit the same worktree or shared file concurrently. Final acceptance occurs only after all three changes are integrated and the combined test suite passes in the integration worktree.
```

### Variant B 1

```text
Run the two package-specific portions in parallel, each in its own Git worktree. Keep the shared configuration file and the third task in the main thread, where one owner can make and verify all overlapping configuration decisions.

Worker A runs in worktree A and receives: implement approved task A only in package A; do not edit the shared configuration file; run package A's tests; inspect the configuration impact and return the exact configuration delta the main thread must apply. It returns conclusions, changed-file and relevant evidence locations, test commands and results, risks or assumptions, and one next step: integrate its package commit and apply its reported configuration delta.

Worker B runs concurrently in worktree B and receives the equivalent bounded instruction for package B: implement only package B changes, do not edit the shared configuration file, run package B's tests, and report the exact required configuration delta. It returns conclusions, evidence locations, verification results, risks, and one next step.

The main thread owns task C and the shared configuration file. While A and B run, it may analyze task C and define the intended combined configuration state, but it must not finalize that file until both workers' configuration requirements have returned. It then integrates the independent package commits one at a time, reconciles all three configuration changes as a single deliberate edit, and runs task C's tests plus both packages' tests against the combined repository state. The main thread records the final conclusions, configuration and integration evidence locations, verification results, residual risks, and the next integration or review step.

Concurrency rule: A and B may run simultaneously because their authorized write sets are disjoint after excluding the shared configuration file. No delegated worker may write, merge, or resolve that shared file. Task C and all shared-configuration edits are serialized in the main thread; final completion is not declared until the combined-state test suite passes.
```

### Variant B 2

```text
# Operational routing plan

Keep task ownership, integration order, and the shared configuration decision in the main thread. Create two isolated worktrees from the same approved base revision:

- Worker A runs the first package task in worktree A and owns only that package and its package-specific tests. Tell it not to edit the shared configuration file. If the package change appears to require a configuration update, it must describe the required change instead of making it.
- Worker B runs the second package task in worktree B and owns only that package and its package-specific tests. Give it the same prohibition on editing the shared configuration file and the same requirement to report any needed configuration change.
- The main thread runs the third task in the integration worktree. It owns the shared configuration file, all decisions that combine package requirements, and final integration.

Workers A and B may run concurrently because their permitted write sets are disjoint. Neither may merge, rebase onto the other's work, or modify the shared configuration. The shared-configuration task must not run concurrently with integration of either package result. After both workers return, the main thread reviews and integrates their commits one at a time, resolves any package-to-configuration requirements centrally, implements the third task, and then runs the combined verification.

Each delegated worker is told to return:

1. Conclusions: what was changed and whether the package task is complete.
2. Evidence locations: worktree path, commit hash, changed file paths, and relevant test locations.
3. Verification: exact test commands and their results.
4. Risks: assumptions, compatibility concerns, and any shared-configuration change it believes is required.
5. One next step: the single integration action the main thread should take.

The main thread verifies that each returned diff stays within its assigned package before integration. It then applies the two package results sequentially, performs the shared configuration change once with both workers' reported requirements in view, and runs each package's tests plus any repository-level configuration or integration tests. If either package result cannot remain valid without an incompatible shared configuration choice, the main thread pauses integration and makes that cross-package decision before further writes.
```

### Variant B 3

```text
Run the two package-local tasks concurrently in two separate Git worktrees, one worktree and branch per package. Keep the third task, all decisions about the shared configuration, and final integration in the main thread. The package workers must not edit the shared configuration file; they report the configuration changes their packages require so the main thread can reconcile all requirements once.

Worker A runs in worktree A and is told: implement only the approved changes in package A, stay out of package B and the shared configuration file, run package A's tests that are valid without the final shared configuration, and report any required configuration edits instead of making them. It returns its conclusion, commit or diff location, test commands and results, the exact shared-configuration requirements it discovered, risks or unresolved dependencies, and one recommended next step.

Worker B runs in worktree B under the symmetric instructions for package B. It returns its conclusion, commit or diff location, test commands and results, exact shared-configuration requirements, risks or unresolved dependencies, and one recommended next step.

The main thread owns task C and the shared configuration file. After both workers return, it compares their reported requirements with task C, resolves conflicts as a single decision, implements the shared configuration once, and integrates the two package changes. It then runs each package's full tests against the integrated configuration plus any repository-level configuration or integration checks. Its final record identifies the integrated commits or diffs, verification results, remaining risks, and the next integration or delivery step.

Concurrency rule: A and B may run concurrently only while their write scopes remain package-local and disjoint. No worker may edit, merge, or commit the shared configuration. Task C and final integration start after both workers have returned. If either package task cannot be separated from an immediate shared-configuration edit, that task leaves the concurrent lane and is serialized in the main integration lane; overlapping writes to the shared file are never performed concurrently or delegated to separate worktrees.
```

### Variant B 4

```text
Keep coordination, integration order, and the shared configuration file in the main thread. Give each package task its own worker and its own Git worktree; do not delegate the third task as an independent write because its configuration change affects both packages.

1. Worker A runs Task A in worktree A. Tell it to modify only package A and its package-local tests, not the shared configuration file; record any required configuration change as a recommendation instead of editing it. It runs package A's tests and returns: its conclusion, commit or diff location, test command and result, risks (including configuration assumptions), and the next step of integrating its commit.
2. Worker B runs Task B in worktree B under the same boundary: modify only package B and its package-local tests, do not edit the shared configuration file, and report any configuration need. It runs package B's tests and returns: its conclusion, commit or diff location, test command and result, risks, and the next step of integrating its commit.
3. The main thread owns Task C and the integration worktree. After reviewing the two worker returns, it integrates A and B, resolves any package-level conflicts, then changes the shared configuration once with both packages' requirements visible. It runs Task C's tests, both package test suites, and any repository-level configuration or integration checks. It records the final configuration rationale and any remaining cross-package risk.

Concurrency rule: Workers A and B may run concurrently because their authorized write sets are disjoint. No delegated worker may write the shared configuration. Task C's write and final verification are serialized after A and B are available in the integration worktree. If either package cannot be completed without changing the shared file, that worker stops at the boundary and returns the proposed change; the main thread decides and applies it during Task C.
```

### Variant B 5

```text
Run the two package tasks in parallel, each in its own Git worktree. Keep the shared-configuration task and integration decisions in the main thread, because that task affects both packages and therefore owns cross-package state.

1. Package A worker — dedicated worktree A
   - Tell the worker to modify only Package A, run Package A's tests, and treat the shared configuration file as read-only. If the package change appears to require a configuration edit, the worker must report the requirement instead of making that edit.
   - Require it to return: its conclusion, commit or diff location, relevant file and test-output locations, the exact verification commands and results, risks or assumptions, and one recommended next step.

2. Package B worker — dedicated worktree B
   - Tell the worker to modify only Package B, run Package B's tests, and treat the shared configuration file as read-only. If the package change appears to require a configuration edit, the worker must report the requirement instead of making that edit.
   - Require it to return: its conclusion, commit or diff location, relevant file and test-output locations, the exact verification commands and results, risks or assumptions, and one recommended next step.

3. Shared-configuration task — main/integration worktree
   - The main thread owns this task. It reviews both package workers' reported assumptions and requirements, modifies the shared configuration once, and decides how conflicts in expected configuration semantics are resolved.
   - Its completion record includes: the chosen configuration behavior, changed-file location, verification commands and results, known risks, and the next integration step.

Concurrency rule: Package A and Package B may run concurrently because their write sets are disjoint and isolated. No delegated worker may edit or merge the shared configuration. The main thread may analyze the configuration task concurrently, but it should defer the final configuration edit and integration verification until both package workers have returned. Then integrate the two package changes into the main/integration worktree, apply the shared configuration change there, run each package's tests plus any repository-level configuration or integration tests, and resolve failures centrally. Do not allow simultaneous writes to the shared file or reuse one worktree for independent writers.
```
