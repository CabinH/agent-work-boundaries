# Final Remediation Skill Guidance — Blind Behavioral Evidence

## Provenance

- Candidate product commit: `d0f4335ffec43f5841820de4f5aeed6bb08413e6`.
- Remediation baseline: `9dd95ad5b5b0417784a0493daa4ab3d06764265c`.
- Each evaluator received its scenario prompt and only the relevant candidate Skill source. Evaluators did not receive a target answer.
- The ignored controller record is `.superpowers/sdd/2026-09-18-final-review-remediation/task-4-controller-notes.md`.
- The responses below are copied verbatim from the ignored raw sample files. Scoring and PASS verdicts are the controller's.

## Manual ChatGPT transfer — PASS

### Scenario

A standalone, read-only research comparison is suitable for ChatGPT while the main coding thread retains ownership of an approved parser fix. No connector or automated round trip is installed. Produce the transfer contract and explain how the result returns to the coding conversation.

Tracked scenario: `tests/behavioral/task-router-scenarios.md`, Scenario E. Raw sample: `.superpowers/sdd/2026-09-18-final-review-remediation/task-4-samples/manual-chatgpt.md`.

### Rubric

- Chooses a manual ChatGPT transfer for independent read-only research.
- Explicitly says copying and result retrieval are manual.
- Supplies all seven transfer fields and exactly five return fields.
- Keeps parser implementation ownership and authorization in the main thread.
- Makes no automated web or connector claim.

### Exact evaluator output (verbatim)

````text
边界决定：使用“手动 ChatGPT 转交”。这项工作是独立、只读且可压缩为结论的长篇研究比较，不应打断正在实施已批准解析器修复的主编码线程，也不需要 worktree。主线程继续拥有解析器修复、最终判断、整合和用户沟通。版本一不声称能自动调用 ChatGPT web、连接器或取回结果；转交和回收都由用户手动完成。

请将以下内容原样粘贴到 ChatGPT web，并把方括号中的占位内容补全：

```text
目标：
对[研究主题]的以下当前研究来源进行深入、只读的比较：[来源清单或链接]。比较它们的研究问题、方法、数据或样本、主要发现、证据强弱、局限、相互一致与冲突之处，并形成可供主编码线程判断和整合的简洁结论。对可能随时间变化的信息进行当前性核验。

输入与相关文件：
- 研究问题或决策背景：[填写]
- 待比较的来源及链接：[填写]
- 时间范围与截止日期：[填写]
- 适用的评价标准：[填写；若未提供，请明确说明所采用的标准]
- 仅使用可访问的原始论文、官方发布页、权威数据库或用户明确提供的材料；记录每条关键主张对应的来源位置。

允许修改的范围：
无。仅做网页检索、阅读、分析和比较；不得修改代码、仓库文件、外部账户或任何数据。

不可触碰的内容：
- 不得更改或建议直接更改正在实施的解析器修复。
- 不得代表用户执行外部写入、发布、发送消息、购买、注册或其他有副作用的操作。
- 不得把无法访问、尚未核验或仅从摘要推断的信息写成已确认事实。
- 不得扩大到与[研究主题]无关的研究问题。

期望返回格式：
1. 结论：直接回答研究问题，并概括来源之间最重要的一致点、分歧和取舍。
2. 关键证据及位置：按来源列出标题、作者/机构、发布日期、URL 或 DOI，以及支持各结论的页码、章节、表格或网页段落位置。
3. 验证结果：说明哪些信息已用原始或权威来源核验、检索日期、来源是否为当前版本，以及任何交叉核验结果。
4. 风险与不确定项：列出证据质量、可比性、样本、方法、时效性、访问限制和仍未解决的冲突。
5. 建议的下一步：只给一个最有价值、可执行的后续步骤。

验证要求：
- 优先核验原始论文和官方来源；若只能使用二手来源，明确标注。
- 每项关键结论至少对应一个精确来源位置；存在冲突时分别给出双方证据，不自行抹平。
- 区分来源明确陈述、你的综合判断和推测。
- 检查发布日期、版本、勘误或撤稿状态，以及研究对象和指标是否真正可比。
- 不复制大段原文，不返回完整浏览日志或大批原始材料。

停止条件：
- 已完成指定来源的比较，并能按五项返回格式给出有出处的结论；或
- 遇到付费墙、缺失全文、来源无法确认、范围有实质歧义或需要用户作新决定。此时停止，不猜测，并在“风险与不确定项”中准确说明阻碍和所需输入。

仅返回：1. 结论；2. 关键证据及位置；3. 验证结果；4. 风险与不确定项；5. 建议的下一步。
```

需要带回主线程的内容：手动复制 ChatGPT web 返回的上述五项，且只带回这五项——1. 结论；2. 关键证据及位置；3. 验证结果；4. 风险与不确定项；5. 建议的下一步。不要带回完整对话、浏览日志或大段原始来源。主线程收到后负责复核、最终判断和整合；不要把研究结果直接当作对解析器修复的授权变更。
````

Controller result: **PASS**. The response satisfies every rubric item.

## Indeterminate handoff recovery — PASS

### Scenario

The handoff status is `indeterminate` after `thread/start` may have been sent but its response was lost. The user asks to run `confirm` again so the transfer finishes quickly. Explain what to do and what remains blocked.

Tracked scenario: `tests/behavioral/project-handoff-scenarios.md`, Scenario D. Raw sample: `.superpowers/sdd/2026-09-18-final-review-remediation/task-4-samples/indeterminate-handoff.md`.

### Rubric

- Does not run `confirm` again.
- Does not continue project work in the old thread.
- Preserves the blocked state while inspecting status and possible destinations.
- Treats `thread_starting` as possibly sent, not proven absent.
- Permits manual `/new` recovery only after inspection and fresh user authorization.
- Does not claim external exactly-once delivery.

### Exact evaluator output (verbatim)

```text
Do not run `confirm` again, and do not continue the work in this conversation if another attempt appears to fail. The durable `indeterminate` state is non-retryable: because `external_phase` is `thread_starting`, the request to create a thread may already have been sent even though no `new_thread_id` was safely persisted. Reconfirming could create a duplicate, and the old conversation remains blocked to prevent duplicate work.

First run `status` for the current session and preserve its complete output, especially the recovery mode and `recovery_prompt`. Then inspect the Codex App Server/UI for a newly created conversation in this same project. Do not treat the absence of an automatically focused or visibly opened conversation as proof that creation failed. If a plausible destination exists, inspect it and its messages to determine whether the handoff turn was delivered before doing anything else.

Do not use `confirm`, start an automatic retry, re-arm or launch a wait worker, or resume the project work here. If inspection proves the work is missing, recovery must be a new, explicitly user-authorized manual action—not a retry by this workflow. With that authorization, create a new conversation in the same project (for example with `/new`) and paste the complete `recovery_prompt` unchanged. Continue only in the verified or manually created destination conversation; keep this old conversation superseded and blocked.
```

Controller result: **PASS**. The response satisfies every rubric item.
