---
name: task-router
description: Use when a coding conversation contains independent research, long document analysis, option comparison, parallel implementation candidates, unrelated side work, shared-file conflict risk, or uncertainty about using a child agent, worktree, new chat, or new project.
---

# Task Router

Choose the smallest boundary that owns the work safely: keep decisions and shared state in the main thread; send independent read work to a child; use one worktree per independent write set. Every delegation returns conclusions, evidence locations, verification, risks, and one next step.

The main Agent retains final judgment, integration, and user communication.

## Routing Matrix

| Boundary | Use when |
| --- | --- |
| Main thread | Work depends on the current decision chain, needs frequent user confirmation, or owns core shared files. |
| Child Agent | Retrieval, reading, analysis, testing, or comparison is independent, bounded, and compressible. |
| Git worktree | A substantial code task has an independent write set and needs parallelism or conflict isolation. |
| Same-project thread | A different long-lived deliverable needs the same repository context and permissions. |
| New project | Repository, permissions, private data, lifecycle, or output differs materially. |
| Manual ChatGPT transfer | A standalone research or long-analysis task benefits from manual transfer; version 1 does not automate ChatGPT web or Workspace Agent round trips. |

## Delegation Input Contract

Provide every field:

```text
目标：
输入与相关文件：
允许修改的范围：
不可触碰的内容：
期望返回格式：
验证要求：
停止条件：
```

## Child Return Contract

Bring back only:

1. 结论
2. 关键证据及位置
3. 验证结果
4. 风险与不确定项
5. 建议的下一步

Do not return complete logs or large raw source dumps.

## Manual ChatGPT Transfer Template

Sending this brief and retrieving its result are manual; version 1 claims no ChatGPT web, connector, or result-retrieval automation.

```text
目标：
输入与相关文件：
允许修改的范围：
不可触碰的内容：
期望返回格式：
验证要求：
停止条件：

仅返回：1. 结论；2. 关键证据及位置；3. 验证结果；4. 风险与不确定项；5. 建议的下一步。
```

Manually paste the completed template into ChatGPT. When it finishes, manually copy the five-field result back to the main Agent for judgment and integration.

## Do Not Delegate

Keep work in the main thread when it contains a user decision, ambiguous scope, a need for new authorization, an irreversible external mutation, or writes overlapping another active worker. Stop and escalate instead of guessing or silently expanding authority. Parallel writers must have disjoint file ownership; serialize shared-file changes under one owner.

## Common Mistakes

- Routing a month-long deliverable in the same repository to a vague “project workstream” instead of a same-project new thread.
- Routing unrelated private work with different permissions to merely a secure thread instead of a new project.
- Treating a child Agent as the persistent owner of a long-lived deliverable.
- Omitting explicit stop conditions or returning test output without the complete five-part result.
