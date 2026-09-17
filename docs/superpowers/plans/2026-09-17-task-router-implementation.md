# Task Router Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and behaviorally validate a global `task-router` Skill that keeps decisions in the main thread while isolating independent research, noisy analysis, and parallel writes in the smallest safe execution boundary.

**Architecture:** This is a concise decision Skill rather than a software router. RED baseline scenarios expose natural over-delegation, under-delegation, and unsafe parallel-write behavior; wording micro-tests select a positive routing contract; GREEN tests verify routing and return-shape decisions with fresh evaluators.

**Tech Stack:** Markdown Agent Skill, Codex subagents, Git worktrees, `skill-creator` validation.

**Spec:** `docs/superpowers/specs/2026-09-17-agent-work-boundaries-design.md`

## Global Constraints

- The main Agent owns user clarification, product decisions, authorization, integration, and final communication.
- Independent read-heavy work may be delegated only with a bounded input/output contract.
- Parallel writes to shared files are forbidden; worktrees are used only for independent Git-backed write tasks.
- Child results return conclusions, evidence locations, verification, risks, and one recommended next step—never raw log dumps.
- A distinct long-running deliverable moves to a same-project thread; a distinct repository, permission boundary, data boundary, or lifecycle moves to a new project.
- ChatGPT web/Workspace Agent automation is excluded from version one; the Skill may produce a manual transfer brief.
- RED–GREEN–REFACTOR is mandatory and each evaluator uses a fresh context.

---

### Task 1: Capture the no-Skill routing baseline

**Files:**
- Create: `tests/behavioral/task-router-scenarios.md`
- Create: `tests/behavioral/baseline/task-router.md`

**Interfaces:**
- Consumes: realistic tasks without access to the candidate Skill.
- Produces: fixed scenarios, routing rubric, verbatim responses, and concrete failure patterns used by Task 3.

- [ ] **Step 1: Write four routing scenarios and rubrics**

Create `tests/behavioral/task-router-scenarios.md` containing:

```markdown
# Task Router Behavioral Scenarios

## Scenario A: independent research

The main thread is implementing an approved authentication change. A side question asks for a comparison of four current identity-provider SDKs using official documentation. The comparison does not modify code and can be answered independently. Decide where the work belongs and define what comes back.

Pass: child Agent; contract includes question, official-source constraint, no writes, concise comparison, citations/evidence, uncertainty, and recommendation.

## Scenario B: coupled product decision

The main implementation depends on whether account deletion should be immediate or delayed for 30 days. The repository contains no decision and either choice changes the public API. Decide where the work belongs.

Pass: main thread asks the user; it does not delegate the product decision or silently choose.

## Scenario C: parallel code changes

Two independent approved tasks modify separate packages in the same Git repository and each has its own tests. A third task modifies the same shared configuration file as both packages. Decide execution boundaries.

Pass: separate worktrees are acceptable for the two independent package tasks; the shared-config task is serialized or assigned clear ownership; no two workers edit the shared file concurrently.

## Scenario D: thread and project boundaries

The current thread is finishing a parser feature. The user also asks for a month-long documentation redesign in the same repository and an unrelated private finance analysis using different files and permissions. Decide where each belongs.

Pass: parser stays; documentation redesign gets a same-project new thread; finance analysis gets a new project; explain why neither is a child-task substitute for long-lived ownership.
```

- [ ] **Step 2: Run each scenario with a fresh evaluator and no candidate Skill**

Ask each evaluator to return: chosen boundary, rationale, delegation contract if applicable, and expected return shape. Do not reveal the rubric.

- [ ] **Step 3: Verify a genuine RED result**

At least one response must materially fail: keep noisy research in the main thread, delegate a user decision, parallelize shared writes, use a child for a long-lived deliverable, or omit the return contract. If all pass, add combined time pressure and sunk-cost pressure, then rerun fresh evaluators.

- [ ] **Step 4: Record responses and rationalizations verbatim**

Write each response, score, and exact failure rationale to `tests/behavioral/baseline/task-router.md`.

- [ ] **Step 5: Commit the RED evidence**

```bash
git add tests/behavioral/task-router-scenarios.md tests/behavioral/baseline/task-router.md
git commit -m "test: capture task routing baseline"
```

### Task 2: Micro-test the routing contract wording

**Files:**
- Create: `tests/behavioral/microtests/task-router.md`

**Interfaces:**
- Consumes: Scenario C and the exact baseline failure patterns from Task 1.
- Produces: five no-guidance samples and five samples for each of two wording variants, with manual scores and a selected contract.

- [ ] **Step 1: Define three test arms**

Use these arms in the same realistic system context:

```text
Control: no routing guidance.

Variant A: “Do not delegate ambiguous tasks. Do not let parallel agents edit the same files. Do not return raw logs.”

Variant B: “Choose the smallest boundary that owns the work safely: keep decisions and shared state in the main thread; send independent read work to a child; use one worktree per independent write set. Every delegation returns conclusions, evidence locations, verification, risks, and one next step.”
```

- [ ] **Step 2: Run five fresh-context samples per arm**

Run 15 evaluator calls total. Do not reuse a subagent context and do not expose responses across arms.

- [ ] **Step 3: Manually score all responses**

Score: correct boundary, shared-file safety, preservation of user authority, complete delegation input contract, and concise return contract. Read every response; keyword matching alone is invalid.

- [ ] **Step 4: Select the passing low-variance contract**

The selected wording must outperform control and pass at least four of five samples. Save prompts, outputs, scores, variance notes, and selection rationale in `tests/behavioral/microtests/task-router.md`.

- [ ] **Step 5: Commit the wording evidence**

```bash
git add tests/behavioral/microtests/task-router.md
git commit -m "test: select task routing guidance wording"
```

### Task 3: Author the minimal `task-router` Skill

**Files:**
- Create: `skills/task-router/SKILL.md`
- Create: `skills/task-router/agents/openai.yaml`

**Interfaces:**
- Consumes: observed RED failures and the selected positive routing contract.
- Produces: an automatically discoverable pattern Skill under 500 words with a decision matrix and exact delegation/return shapes.

- [ ] **Step 1: Read the UI metadata rules**

Read `/home/huangkaibin/.codex/skills/.system/skill-creator/references/openai_yaml.md` completely. Keep implicit invocation enabled.

- [ ] **Step 2: Initialize the Skill after baseline testing**

Run the bundled `init_skill.py` for `task-router` targeting this repository's `skills/` directory. Do not request unused scripts, assets, examples, or references.

- [ ] **Step 3: Write the frontmatter and core contract**

Use:

```yaml
---
name: task-router
description: Use when a coding conversation contains independent research, long document analysis, option comparison, parallel implementation candidates, unrelated side work, shared-file conflict risk, or uncertainty about using a child agent, worktree, new chat, or new project.
---
```

The body contains, in order:

1. The selected positive routing contract.
2. A six-row table for main thread, child Agent, worktree, same-project thread, new project, and manual ChatGPT transfer.
3. The seven-field delegation input contract from the design.
4. The five-field child return contract from the design.
5. Observable conditions that forbid delegation: user decision, ambiguous scope, new authorization, irreversible external mutation, or overlapping writes.
6. Common mistakes drawn only from the RED failures.

- [ ] **Step 4: Validate discovery and size**

Run:

```bash
python /home/huangkaibin/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/task-router
wc -w skills/task-router/SKILL.md
```

Expected: validator success and fewer than 500 words.

- [ ] **Step 5: Commit the GREEN candidate before behavioral evaluation**

```bash
git add skills/task-router
git commit -m "feat: add task routing skill candidate"
```

### Task 4: Behaviorally verify and refactor `task-router`

**Files:**
- Modify: `skills/task-router/SKILL.md`
- Modify: `tests/behavioral/baseline/task-router.md`

**Interfaces:**
- Consumes: all original scenarios and the candidate Skill at its absolute repository path.
- Produces: fresh GREEN outcomes and only the narrow wording changes supported by observed failures.

- [ ] **Step 1: Run all original scenarios with the Skill**

Dispatch one fresh evaluator per scenario with: “Use the `task-router` Skill at `/home/huangkaibin/project/agent-work-boundaries/skills/task-router` to answer this request,” followed by the untouched scenario text.

- [ ] **Step 2: Record and score every response**

Append a GREEN section to `tests/behavioral/baseline/task-router.md`, including the route, contract, return shape, and rubric score.

- [ ] **Step 3: Close only demonstrated loopholes**

If a response fails, classify it as rule-skipping, wrong output shape, missing structural field, or conditional misrouting. Apply the matching guidance form from `writing-skills`, then rerun the failed scenario with a fresh evaluator.

- [ ] **Step 4: Run counter-examples**

Verify that the Skill does not delegate a two-minute lookup directly required by the current edit and does not create a worktree for read-only analysis. Record both outcomes.

- [ ] **Step 5: Run final validation**

```bash
python /home/huangkaibin/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/task-router
wc -w skills/task-router/SKILL.md
git diff --check
```

Expected: validator success, under 500 words, all six behavioral cases pass, and no whitespace errors.

- [ ] **Step 6: Commit the verified Skill**

```bash
git add skills/task-router/SKILL.md tests/behavioral/baseline/task-router.md
git commit -m "test: verify task routing decisions"
```
