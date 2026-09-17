# Agent 工作边界 Skills 设计

日期：2026-09-17
状态：已确认，待实施

## 1. 背景

长期使用 Codex 时，主要风险通常不是单次推理能力不足，而是工作边界逐渐模糊：一个对话承载过多目标、上下文反复压缩、独立杂活污染主任务、并行写入相互干扰，以及交接依赖用户手工复述。

本项目提供两个全局用户级 Skill：

- `project-handoff`：判断何时结束当前对话，生成可继续执行的交接信息，并在确认或五分钟无响应后启动新 thread。
- `task-router`：判断任务应留在主对话，还是交给子 Agent、Git worktree、同项目新对话、新项目或人工 ChatGPT 工作流。

两者分别处理“何时结束当前工作单元”和“工作应放在哪里”，避免职责重叠。

## 2. 目标与非目标

### 2.1 目标

- 在上下文开始失真之前主动提出交接，而不是等任务跑偏。
- 用户确认后自动整理“完成事项、既定规则、下一步”，创建新 thread 并继续工作。
- 发出交接请求后五分钟无用户响应，自动执行相同交接流程。
- 将独立、读密集或高噪声任务隔离出去，仅把结构化结论带回主对话。
- 为并行写入任务选择 worktree，降低文件冲突和上下文污染。
- 所有关键状态持久化、可诊断、可恢复，并提供明确降级路径。

### 2.2 非目标

- 第一版不自动驱动 ChatGPT 网页端并取回回答。Workspace Agents API 能触发运行，但不能形成可靠的结果回收闭环。
- 不根据单一 token 数机械切换对话；判断结合压缩次数、任务边界和语义信号。
- 不把完整 transcript、原始检索材料或子 Agent 日志复制进交接文档。
- 不保证创建新 thread 后前端界面自动聚焦；保证保存 thread ID 和恢复信息。

## 3. 总体架构

源码仓库：

```text
agent-work-boundaries/
├── skills/
│   ├── project-handoff/
│   └── task-router/
├── hooks/
├── tests/
├── docs/superpowers/specs/
└── install.sh
```

安装目标：

```text
~/.codex/skills/project-handoff/
~/.codex/skills/task-router/
~/.codex/hooks.json
~/.codex/state/project-handoff/
```

源码仓库是唯一维护源；全局目录是可重新生成的安装结果。安装程序升级现有 `project-handoff` 前先创建备份，并以增量方式合并 Hooks 配置。

## 4. `project-handoff` 设计

### 4.1 职责

`project-handoff/SKILL.md` 负责：

- 识别主动交接信号。
- 判断应新建同项目对话、独立项目，还是继续当前对话。
- 生成紧凑、可验证的交接草稿。
- 请求用户确认，并调用交接控制器。
- 在旧 thread 已被替代后阻止重复执行。

### 4.2 主动触发信号

触发判断按两类信号组合：

1. 上下文健康信号：
   - 第一次自动压缩：记录，不中断。
   - 第二次自动压缩：在下一个稳定边界提醒交接。
   - 第三次及以后：完成不可中断的小步骤后强烈建议交接。
2. 语义信号：
   - 当前交付物已经完成，下一步是不同目标。
   - 同一对话出现多个互不依赖的长期分支。
   - 关键决定开始被重新讨论或与既定约束冲突。
   - 为继续工作需要反复重建历史、重新扫描大量文件。
   - 用户主动要求暂停、换工具、换项目或交接。

语义信号可以早于压缩阈值触发。Skill 不在构建、迁移或不可安全中断的操作中途强制切换。

### 4.3 交接内容

项目可写时更新 `docs/AI-HANDOFF.md`，保持简短并替换过期信息，默认不超过约 80 行：

```markdown
# Current Focus
# Completed Work
# Agreed Rules and Decisions
# Open Decisions and Risks
# Verification Status
# Next Step
# Resume Command
```

持久代码、测试、提交和项目说明始终优先于交接摘要；新 thread 必须先核对事实来源，不能把摘要当成绝对真相。

### 4.4 组件

- `scripts/handoffctl.py`
  - `prepare`：保存草稿并生成 `pending_id`。
  - `arm`：将草稿绑定当前 session，启动五分钟计时。
  - `confirm`：立即发布交接并创建新 thread。
  - `cancel`：取消当前自动交接。
  - `status`：返回当前状态和恢复建议。
- `scripts/app_server_client.py`
  - 与 `codex app-server` 建立 JSON-RPC 会话。
  - 调用 `thread/start` 和 `turn/start`。
  - 保存新 thread ID、执行状态和精简错误。
- `hooks/handoff_hook.py`
  - 接收 Codex Hooks JSON 输入。
  - 处理 `Stop`、`UserPromptSubmit`、`PostCompact`、`SessionStart` 等事件。
  - 只返回必要的附加上下文或阻止信息。

## 5. 确认与五分钟自动交接

### 5.1 正常确认流程

1. Skill 在稳定边界发现需要交接。
2. 生成草稿，但暂不发布到项目交接文件。
3. `handoffctl prepare` 将草稿写入私有状态目录并返回 `pending_id`。
4. 助手询问是否交接，在响应中加入可机器识别的交接标记。
5. `Stop` Hook 读取标记，将 `pending_id` 绑定 `session_id` 并启动 300 秒后台计时器。
6. 用户回复时，`UserPromptSubmit` Hook 原子地阻止计时器继续抢占：
   - 明确确认：当前 Agent 调用 `confirm`。
   - 明确拒绝：调用 `cancel` 并删除草稿。
   - 其他回复：取消这一次自动交接；之后如仍有必要可重新发起。

### 5.2 超时流程

五分钟内没有用户输入时，后台 worker：

1. 以比较并交换方式将 `armed` 改为 `transferring`。
2. 发布 `docs/AI-HANDOFF.md`，或在项目不可写时保存到用户状态目录。
3. 通过 App Server 创建新 thread。
4. 给新 thread 发送启动指令：读取项目指令和交接文件、核对持久事实、从 `Next Step` 继续。
5. 保存新 thread ID，并把旧 session 标为 `superseded`。

新 thread 在后台继续工作，但不假设 Codex 客户端一定会自动切换到它。

### 5.3 状态机与竞争控制

```text
draft -> armed -> responded -> confirmed -> transferring -> transferred
                    |
                    +-> cancelled

armed -> expired -> transferring -> transferred
                              +----> failed
```

状态文件采用锁和同目录原子替换。用户回复与计时器到期竞争时，只允许一方把 `armed` 转为下一状态；失败方读取最终状态并退出，不重复发布或创建 thread。

旧 thread 在 `superseded` 后收到迟到输入时，由 Hook 注入迁移提示。默认不再执行原任务，只报告新 thread ID 和恢复方式。

## 6. `task-router` 设计

### 6.1 路由矩阵

| 位置 | 适用情况 |
|---|---|
| 当前主对话 | 依赖当前决策链、需频繁用户确认、涉及核心共享文件 |
| 子 Agent | 独立的检索、阅读、分析、测试、方案比较，结果可压缩 |
| Git worktree | 独立且写入量较大的代码任务，需要并行或冲突隔离 |
| 同项目新对话 | 同一代码库中的另一项长期交付物 |
| 新项目 | 仓库、权限、资料、生命周期或产物明显不同 |
| ChatGPT 网页端 | 第一版只生成人工转交模板，不自动闭环 |

### 6.2 路由原则

- 小而直接支持当前目标的插曲留在主对话。
- 能写成明确输入/输出契约的独立任务优先委派。
- 多个任务会同时修改相同文件时不并行；能按文件或模块隔离时才采用 worktree。
- 资料搜索、长文本阅读、方案比较和独立测试默认优先隔离。
- 产品取舍、需求解释、用户授权和不可逆外部操作留在主对话。
- 主 Agent 始终负责最终判断、变更整合和用户沟通。

### 6.3 委派契约

每次委派提供：

```text
目标：
输入与相关文件：
允许修改的范围：
不可触碰的内容：
期望返回格式：
验证要求：
停止条件：
```

子 Agent 只带回：结论、关键证据及位置、验证结果、风险与不确定项、建议的下一步。不得把完整日志和大段原材料倾倒回主上下文。

## 7. Hooks 与持久状态

### 7.1 Hooks

安装程序增量注册以下事件：

- `Stop`：识别交接标记并启动非阻塞计时器。
- `UserPromptSubmit`：取消待执行计时器；对已迁移 session 注入警告。
- `PostCompact`：累计自动/手动压缩次数，并按阈值显示简短状态提示。
- `SessionStart`：当 `source=compact` 时读取计数并注入模型上下文；在启动或恢复时检查未完成或失败的交接。

Hooks 安装后需要用户通过 `/hooks` 审查并信任。未获信任时，Skill 明确退化为手动确认流程，不声称五分钟自动机制已生效。

### 7.2 状态目录

`~/.codex/state/project-handoff/` 保存：

- `pending_id`、session/thread ID。
- 创建时间、截止时间和状态。
- 交接草稿或其路径。
- App Server 的精简错误和恢复指令。
- 压缩计数与旧 thread 的替代关系。

目录使用仅当前用户可访问的权限。不得保存 API 密钥、认证信息或完整 transcript。

## 8. App Server 与降级路径

正常路径使用当前 Codex CLI 的实验性 App Server：

1. 启动并初始化 `codex app-server`。
2. 调用 `thread/start` 创建新 thread。
3. 调用 `turn/start` 发送恢复指令。
4. 记录新 thread ID 和结果。

如果 App Server 不可用、认证失败或协议变化：

- 已发布交接内容不回滚。
- 状态改为 `failed` 并保存精简错误。
- 下一次 Hook 或 Skill 调用给出可直接粘贴到 `/new` 的启动指令。
- 用户可以执行 `handoffctl status` 检查并重试。

## 9. 安装、升级与卸载

- `install.sh` 运行结构验证和测试后安装两个 Skill。
- 安装前备份现有 `~/.codex/skills/project-handoff` 和 Hooks 文件。
- Hooks 合并按命令标识幂等处理，不覆盖其他 Hook。
- 文件先写临时文件，再在同一文件系统原子替换。
- 提供 `--dry-run`、`--uninstall` 和恢复备份的说明。
- 全局安装需要一次明确权限授权；后续行为限于用户级 Codex 配置和状态目录。

## 10. 安全与故障恢复

- 后台 worker 与 Hook 均校验 `pending_id`、状态目录和目标路径，拒绝目录穿越。
- 计时器进程不继承交互式标准输入，输出写入受限日志。
- worker 崩溃时保留 `armed` 或 `failed` 状态；下一次事件提示恢复，不静默丢失交接。
- 项目不可写时把交接文档保存到状态目录，并向新 thread 传递绝对路径。
- App Server 成功但客户端无法聚焦时，明确报告 thread ID。
- 安装器不保存或复制 Codex 凭据。

## 11. 测试策略

### 11.1 单元测试

- 状态转换和非法转换。
- 计时、取消、确认与超时竞争。
- 原子文件更新和锁。
- Hooks JSON 输入输出。
- 配置增量合并与幂等安装。
- 路径与权限校验。

测试可把 300 秒改为短延迟，但生产默认值固定为五分钟。

### 11.2 模拟集成测试

使用假的 App Server 验证：

- 初始化、`thread/start`、`turn/start` 的成功路径。
- 进程退出、超时、错误响应和半完成状态。
- 已发布交接在 App Server 失败后仍可手动恢复。

### 11.3 Skill 行为测试

按照 Skill 编写规范对比无 Skill 基线与启用后的行为，覆盖：

- 独立资料任务是否被正确分流。
- 共享文件冲突是否阻止错误并行。
- 第一、第二、第三次压缩后的不同策略。
- 用户在五分钟内确认、拒绝或发送其他回复。
- 无响应超时与迟到回复。
- 已替代旧 thread 是否停止重复工作。

### 11.4 安装与实机验证

- 使用 Skill 验证器检查两个 `SKILL.md` 的结构和描述。
- 验证全局发现、Hooks JSON、备份和卸载。
- 自动测试不创建真实聊天。
- 安装完成后再单独执行一次真实 App Server 冒烟测试，避免测试阶段产生无用 thread。

## 12. 验收标准

- 两个 Skill 能从任意项目被 Codex 发现。
- 第三次压缩或明确语义边界会触发交接建议。
- 发起交接后，任意用户输入都能可靠取消当前五分钟计时。
- 明确确认或五分钟无响应只创建一个新 thread。
- 新 thread 获得完成事项、既定规则、验证状态和唯一下一步。
- 旧 thread 被标为已替代且不会继续重复执行。
- 独立杂活能被路由到适当隔离层，返回内容保持精炼。
- App Server 失败时仍能通过交接文件和 `/new` 指令恢复。
- 安装不会破坏已有 Skill 或 Hooks 配置。

## 13. 参考资料

- [Codex App Server](https://developers.openai.com/codex/app-server)
- [ChatGPT Projects](https://learn.chatgpt.com/zh-Hans/docs/projects)
- [Codex Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [Git Worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees)
- [Hooks](https://learn.chatgpt.com/zh-Hans/docs/hooks)
- [Workspace Agents API](https://learn.chatgpt.com/workspace-agents/trigger-runs)
