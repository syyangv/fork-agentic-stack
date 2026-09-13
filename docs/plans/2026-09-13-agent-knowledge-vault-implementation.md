# Agent Knowledge Vault：双 Vault 知识闭环实施计划

- 日期：2026-09-13
- 状态：计划已记录；未开始实施
- 范围：基于 Proma 源码审阅，为当前 Obsidian / agentic-stack / Brain / Paseo stack 增加知识检索与审批沉淀闭环
- 执行原则：逐 phase 实施、测试、验收；本文件不授权部署、迁移、删除、Git 提交或推送

## 1. 已确认的产品决定

1. 保留现有 Personal Vault，仅用于用户的 personal knowledge，不将它改造成 agent 工作日志库。
2. 新建独立 Agent Knowledge Vault，存放经过确认的工作知识、研究、项目背景、决策与复盘。
3. Brain 指“检索与组织层”，不强制使用现有 codejunkie99/brain 产品实现全部能力。
4. 首期优先实现：任务开始前自动检索上下文 → Paseo 执行 → 生成草稿 → 人工确认 → 后续任务复用。
5. 草稿可以自动生成；正式知识必须人工审批。
6. Personal Vault 默认不索引。用户可以为某个任务明确授权指定笔记或目录；授权不自动扩展到未来任务。
7. 读取个人内容不包含永久复制、写回工作 vault 或跨任务复用授权。
8. 本地优先，不要求新的付费 API key，不引入云端知识库或付费 embedding 服务。

### 1.1 权威来源与所有权

| 系统 | 保存什么 | 不保存什么 / 不承担什么 |
|---|---|---|
| Personal Vault | 用户个人知识和私人资料 | agent 默认索引、自动改写的工作材料 |
| Agent Knowledge Vault | 已确认的工作知识 Markdown | 全量 session、工具日志、未经确认的事实 |
| Brain 检索层 | 可重建搜索索引、出处、链接关系、上下文包 | 另一套需要独立维护的知识正文 |
| `.agent` / 项目仓库 | skills、权限协议、操作规则、代码、运行记忆 | 与 vault 双向竞争的知识副本 |
| Paseo | provider、agent、会话、审批和执行生命周期 | 唯一的长期知识存储 |
| Headroom | 既有请求压缩能力 | 知识来源、知识索引或知识写回引擎 |

**原则：每类信息只有一个权威来源，而不是所有信息只能存一个地方。**

示例：部署脚本和 skill 仍由项目 / `.agent` 维护；vault 可以记录“为什么选择该部署方案”并链接原规则，不复制一份可独立修改的部署说明作为另一套执行规则。

### 1.2 非目标

- 不替换 Paseo，不安装 Proma 作为第二套日常执行环境。
- 不迁移整个 `.agent` 或现有 Brain event store。
- 不全量导入个人 vault、聊天历史和工具输出。
- 不新建 provider router、scheduler、向量数据库或知识图数据库。
- 不修改普通 Paseo composer 的所有发送行为。
- 不实现操作系统级 agent 文件沙箱；目录白名单只约束本方案工具，不约束拥有其他宿主机工具的 agent。
- 不新增自动提交、推送、云端同步或对外发布。

## 2. 调研基线与证据

### 2.1 Proma 审阅快照

- 源仓库：<https://github.com/proma-ai/Proma>
- Commit：`f99edbdb594407ab190b97ae073889c5d96637ab`
- 桌面版本：`0.19.53`
- 已核实 release：`v0.19.53`，发布时间 `2026-09-10T09:04:28Z`
- 临时审阅 checkout：`/private/tmp/proma-review-20260913`；不依赖该临时目录作为长期证据存储。

| 可借鉴设计 | 固定快照中的证据 | 本方案采用方式 |
|---|---|---|
| 会话渐进读取、探索分支边界 | `apps/electron/src/main/lib/agent-session-context-prompt.ts:183–213` | 来源片段优先，按需展开；不重复注入完整父历史 |
| 记忆审阅邀请而非自动扫描 | `apps/electron/src/main/lib/agent-memory-refresh-service.ts:19–43` | 显式候选与人工审批；不把生成当确认 |
| 编辑前原文版本检查 | `apps/electron/src/main/lib/agent-workspace-manager.ts:1641–1646` | 草稿与目标内容 hash 绑定 |
| 副作用调用幂等 | `apps/electron/src/main/lib/agent-collaboration-utils.ts:26–55` | 持久 task/turn/draft 标识，重复事件不重复落笔记 |
| 显式项目指令根 | `apps/electron/src/main/lib/project-instruction-resolver.ts:61–92,117–154` | canonical path 与白名单，不把指令范围误认为整个 agent 的文件沙箱 |

不复制 Proma 源码；独立实现工作流。Proma `LICENSE` 标识 AGPL v3；如果后续改成复制或派生源码，需另行审查许可要求，本计划不作法律结论。

静态风险观察（非已复现 exploit）：

- `agent-orchestrator.ts:1447–1464` 对非 planning 的 `mcp__` 工具存在 plan-mode 例外，不能照搬为完整只读保证。
- `automation-manager.ts:40–50` 和 `packages/shared/src/types/automation.ts:47` 涉及 automation 权限默认值及迁移；本方案不改变现有用户授权。
- `automation-scheduler.ts:209–213,251–252` 的超时路径结束 bookkeeping，但未在该路径确认执行终止；本方案区分等待超时与任务停止。
- `channel-manager.ts:439–463` 存在加密不可用时的明文 fallback；本方案不新增 credential 存储。

已运行：项目指令、headless routing、rewind contract 共 10 个测试通过。Session-core suite 因 workspace dependency `@proma/shared` 未安装而无法加载。未完整构建或启动 Proma；以上不构成全面安全审计。

### 2.2 当前 stack 的已验证接入点

- agentic-stack checkout：`/Users/syang/.agentic-stack`，调研时 HEAD `91df2fd`。
- Paseo checkout：`/Users/syang/projects/paseo`，调研时 HEAD `b52442a`。这是本地源码快照，不等同于已安装桌面 / daemon 的版本证明。
- `docs/memory-architecture.md`：现有 `.agent`、external Brain、Headroom 的所有权边界。
- `docs/data-layer.md` 与 `.agent/tools/data_layer_export.py`：现有本地统计入口，不另建遥测系统。
- `docs/data-flywheel.md`：已有 approved-run → context-card/eval 思路，不能绕过 approval 再造一份“事实”。
- `.agent/tools/brain_bridge.py`：现有 external Brain CLI bridge，保持兼容。
- `~/.agent/skills/model-routing/SKILL.md`：现有模型路由唯一策略 owner。
- Paseo `public-docs/plugins/reference.md`：workspace panel、RPC、attachment source、SDK 接入。
- Paseo `public-docs/sdk/reference.md`：agent 创建、`send`、`waitForFinish`、timeline refetch/subscribe。
- 本机 Python SQLite `3.45.3`，已用内存数据库验证 FTS5 trigram 可创建。
- Brain upstream 检视 commit `6cf458a8d2dd9767e06f3e9dc8cf850e306e9d54`：`brain-app` / `brain-index` 为自有 Git event journal 提供派生索引；不能直接推定现有安装已有 vault 索引接口。本期不调用未经安装版本验证的新 Brain API。

## 3. 目标结构、接口与数据流

### 3.1 文件与代码归属

以下均为计划位置，不代表目录已创建：

| 位置 | Owner / 用途 |
|---|---|
| `/Users/syang/obsidian/agent-knowledge` | 工作知识 vault |
| `/Users/syang/.agentic-stack/.agent/tools/knowledge.py` | 薄 CLI 入口 |
| `/Users/syang/.agentic-stack/.agent/tools/knowledge_core/` | 配置、范围检查、解析、索引、检索、上下文、草稿、审批核心 |
| `/Users/syang/.agentic-stack/tests/test_knowledge_*.py` | 核心测试与合成 fixture |
| `/Users/syang/.agentic-stack/integrations/paseo-knowledge/` | 薄 Paseo 插件；不改 Paseo core |
| `/Users/syang/.agent/knowledge/` | 本机配置、派生 SQLite、任务状态、恢复记录；不作为知识库 |

代码 canonical owner 在 agentic-stack；运行态与 Markdown 分离。安装入口只指向 canonical 实现，不手工维护第二份代码或 skill 镜像。现有安装器若需要分发模块，必须一次性分发入口与模块并有安装回归测试。

初始 vault：

```text
Home.md
00-Inbox/
10-Projects/
20-Research/
30-Decisions/
40-Lessons/
90-Archive/
```

默认只检索 `10-Projects`、`20-Research`、`30-Decisions`、`40-Lessons`。草稿、归档、配置、附件正文不入默认检索。禁止为了初始化覆盖已有目录内容。

### 3.2 最小接口

核心使用版本化 JSON 输入 / 输出；stdout 仅返回结构化结果，诊断写 stderr。插件通过受控 subprocess 调用核心，使用 argv 数组及 stdin，不拼接 shell 字符串、不经过登录 shell。

| 接口 | 输入要点 | 输出 / 副作用 |
|---|---|---|
| `status` | 本机配置 | 配置有效性、索引状态；不返回正文 |
| `index` | 注册的 vault ID | 增量刷新统计；只写派生索引 |
| `search` | query、task ID、项目过滤 | 有出处的片段；默认最多 8 条 |
| `read_source` | 已注册 source ID、范围、task ID | 授权范围内的原文片段 |
| `build_context` | task ID、query、排除项 | 上下文包、source manifest、截断提示 |
| `propose_note` | task/turn ID、结构化候选、证据引用 | 未审批 draft ID |
| `review_apply` | draft ID、草稿 hash、目标 hash、新内容 | 人工确认后的新增或单文件更新 |
| `review_reject` | draft ID、原因 | rejected 状态，不触发自动重提 |
| `reconcile` | 已登记 task ID、观察到的 turn 状态 | 补齐状态，不启动新的模型任务 |

`review_apply` 不暴露为 agent 的通用 MCP tool。人工 UI 是正常授权入口；这不构成阻止恶意宿主机进程直接操作文件的安全承诺。

### 3.3 数据对象

仅引入实现安全所需的对象：

- **SourceRef**：vault ID、note ID、相对路径、heading、行范围、content hash。
- **ContextBundle**：task ID、检索 query、选定 Sources、片段、字符数、生成时间、是否包含个人内容。
- **TaskRecord**：稳定 task ID、Paseo agent/turn ID、workspace、provider、状态、source manifest、关联 draft ID。
- **KnowledgeDraft**：draft ID、目标路径/新增类型、内容、来源、验证声明、草稿 hash、目标基础 hash、审批状态。

正文不是遥测字段。正式笔记保留必要 provenance；私有 task record 保留更详细的执行关联。时间统一 UTC，UI 显示本地时区。

### 3.4 状态与安全约定

- 执行状态：`prepared → running → completed / failed / cancelled`；另外允许 `awaiting_permission`、`status_unknown`。
- `completed` 只表示执行回合完成，不表示结论经过验证或用户确认。
- 知识状态：`draft → accepted / rejected`；用户编辑后需基于新 hash 重新确认。
- 等待超时进入 `status_unknown`，通过 Paseo 重新核对；不能自动标为成功或取消。
- 待审草稿不是正式事实；agent 自述“tests passed”标记为 `agent_reported`。仅有可核对执行结果的证据才标记为已核验。
- 个人知识 task grant 存在进程内并绑定任务，任务结束、插件重启或重新打开需重新授权；不写入持久搜索索引。
- 已交给 provider 的个人片段可能保存在会话中。UI 必须明确说明，不承诺撤回会话 / provider 中已发送的内容。

## 4. 实施 Phases 与具体 Steps

### Phase 0 — 兼容性验证与冻结接口

**目标：先证明现有 Paseo 能承载薄插件，避免实施后才发现接入不存在。**

- [ ] P0.1 记录实际安装的 Paseo daemon / desktop 版本、插件能力及与本地源码差异，不读取 auth 文件。
- [ ] P0.2 用最小插件和合成内容验证 workspace panel、RPC、borrowed Paseo connection、agent 创建、turn ID 与 timeline 读取。
- [ ] P0.3 验证关闭再打开面板后，能按 agent/turn ID 找回同一任务结果。
- [ ] P0.4 确认当前 Python executable 及 FTS5 trigram 能力；插件配置绝对 executable 路径，避免依赖 daemon PATH。
- [ ] P0.5 确认新 vault 路径未被占用、私有运行目录不会进入版本控制；检查父仓库忽略规则。
- [ ] P0.6 将 SourceRef / ContextBundle / TaskRecord / KnowledgeDraft 合同固化为测试 fixture。

**验收：**合成任务可以创建、观察和恢复；没有新 provider、没有权限变更、没有个人笔记读取。

**阻塞处理：**若安装版本缺少上述 API，停止 Paseo 接入 phase 并记录需要的升级；不静默升级、不直接 patch Paseo core。核心检索工作可独立继续。

### Phase 1 — 工作 Vault 与授权范围

**依赖：**P0.5；可与 P0 的 UI 验证并行。

- [ ] P1.1 创建新 vault 目录、Home、四种知识模板及草稿说明；不覆盖现有路径。
- [ ] P1.2 注册 `work` vault 和正式目录白名单；Personal Vault 只注册身份，不开启索引。
- [ ] P1.3 实现 canonical path 范围检查：拒绝绝对路径注入、`..`、跨 vault、指向范围外的 symlink。
- [ ] P1.4 为新生成笔记写入稳定 ID；不批量修改其他笔记。没有 ID 的人工笔记使用路径标识，移动后作为删除+新增处理；有 ID 的笔记按 ID 追踪移动。
- [ ] P1.5 为指定的个人笔记/目录创建 task-scoped grant；禁止任意扩大目录范围。
- [ ] P1.6 明确“操作规则只链接、不镜像”的内容约定。

**验收：**工作库和个人库独立；未经授权的个人来源在所有知识接口中不可见；模板与目录不会改变既有 vault。

### Phase 2 — Markdown 解析与可重建索引

**依赖：**Phase 1。

- [ ] P2.1 实现 Markdown block parser：识别 frontmatter、标题、段落、代码 fence、wiki links；保留原文行范围，不执行嵌入代码。
- [ ] P2.2 使用保守的标题/段落 chunking；超长块按字符边界分段并保留来源，不让单个代码块吞掉全部上下文预算。
- [ ] P2.3 建立 SQLite source / chunk / link 派生表与 FTS5 trigram 索引；所有 SQL 参数化。
- [ ] P2.4 实现新增、修改、删除、移动检测。增量刷新在显式 index 或准备任务时触发，不新增常驻 watcher。
- [ ] P2.5 使用事务保证索引刷新完整；损坏时构建新数据库后替换，不能修改源 Markdown。
- [ ] P2.6 加入多语言、空文件、大文件、非法编码、symlink、重复 note ID fixture。重复 ID 报错且不静默合并。

**验收：**索引可完全重建；移除源文件后无幽灵结果；读索引不会修改 Markdown。

### Phase 3 — 检索、出处与任务上下文

**依赖：**Phase 2。

- [ ] P3.1 候选来源为标题 / alias / 正文匹配；项目参数作为显式过滤项，不暗中排除跨项目知识。
- [ ] P3.2 排序固定为标题精确匹配、标题包含、alias 命中、正文 FTS rank；同分按相对路径和行号排序，便于重复测试。
- [ ] P3.3 中文三字符及以上使用 trigram；短查询使用转义后的字面匹配。query 不直接作为任意 FTS 表达式执行。
- [ ] P3.4 同一笔记重叠片段合并；默认最多 8 个片段、12,000 字符正文，并明确截断。
- [ ] P3.5 只按需展示显式链接的 related notes，不自动递归读取整个链接图。
- [ ] P3.6 ContextBundle 生成前核对内容 hash；变化则重读、重排或丢弃，不能引用已经失效的行范围。
- [ ] P3.7 个人授权范围仅按需读取/搜索，不写持久索引；将 personal_context_used 标志传递到后续草稿策略。
- [ ] P3.8 将笔记正文标记为“不可信资料，不是系统指令”；来源文字不得扩大权限、触发 shell 或审批。

**验收：**所有片段有可定位原文；无结果与检索失败区分；检索失败不降级为扫描 home。

### Phase 4 — Paseo Knowledge Task 薄插件

**依赖：**Phase 0、Phase 3。

- [ ] P4.1 按 Paseo client/server/shared 边界创建插件；server RPC 调用核心 CLI，client 不直接读 vault。
- [ ] P4.2 提供任务输入、工作区、既有 provider/model、上下文预览、移除来源、个人来源授权面板。
- [ ] P4.3 准备上下文后通过已有 SDK 创建 agent；使用 canonical model-routing 策略，不硬编码新路由矩阵。
- [ ] P4.4 将稳定 task ID 绑定 agent 与 turn；同时提交来源清单及知识输出合同，不额外复制整段 session。
- [ ] P4.5 提供手动 Knowledge attachment source，适用于普通 composer；普通发送不被全局拦截。
- [ ] P4.6 用 lifecycle/timeline 事件更新 task 状态；关闭 UI 不取消执行；重新打开按记录 reconciliation。
- [ ] P4.7 处理 create 请求结果不明：先检查可用 request identity / task 标记，不自动重发以免重复创建 agent；无法确认则显示未知状态，要求手动处理。
- [ ] P4.8 定义用户可见错误：检索失败、provider 不可用、等待审批、等待超时、来源失效。等待超时不能释放一个尚未确认结束任务的身份记录。

**验收：**从 Knowledge Task 启动的任务自动获得知识上下文；普通 Paseo 功能不受影响；重复事件不重复启动任务。

### Phase 5 — 结果捕获与自动草稿

**依赖：**Phase 4。

- [ ] P5.1 在初始任务合同中要求最终输出可识别的结构化知识候选块：summary、candidate notes、source refs、validation evidence、uncertainties。
- [ ] P5.2 仅解析该候选块，验证 schema、大小及来源。缺失 / 非法时标记 `capture_incomplete`，不把整个回答原样塞进 vault，不自动再调用另一个模型修复。
- [ ] P5.3 以 task ID + turn ID + candidate hash 幂等创建草稿；不要只用内存去重。
- [ ] P5.4 有个人上下文的任务默认只在当前审阅界面展示，不持久写草稿正文；用户明确授权可沉淀部分后才能写工作库。
- [ ] P5.5 普通任务的候选写入 `00-Inbox`；运行记录仅保留 task/turn、provenance 和状态，不复制全部 timeline。
- [ ] P5.6 显式区分 agent 自述证据和可观察证据；不能因为 agent 回答完毕就标记知识已验证。
- [ ] P5.7 面板重开后读取已登记任务的最后回合，补齐漏掉的草稿；包括已归档但仍可读取的任务。读取不可用时标记无法恢复，不从其他任务猜结果。

**验收：**每个候选至多一份；失败/取消任务不产生成功经验；个人内容不在无授权情况下持久沉淀。

### Phase 6 — 审批、冲突检测与正式知识写入

**依赖：**Phase 5。

- [ ] P6.1 审阅面板展示候选正文、来源、验证等级、目标路径与 diff。
- [ ] P6.2 实现接受、编辑后接受、拒绝。拒绝记录保持 terminal，除非用户明确重新打开。
- [ ] P6.3 新笔记使用目标不存在检查；已有笔记修改绑定读取时 hash，用户编辑草稿后重新确认。
- [ ] P6.4 写入使用排他临时文件、仅本用户权限、同目录原子替换；本工具写入串行化，写入前再次核对目标。
- [ ] P6.5 注意：本工具的 lock 不能阻止 Obsidian 或其他编辑器写入。hash 检查不是跨所有 writer 的文件 CAS；保留 preimage 和 post-write 校验，不宣称消除了所有外部写入 race。繁忙编辑文件优先新增补充笔记，而不是原地替换。
- [ ] P6.6 使用可恢复 apply journal，处理“文件已写成功但状态未提交”的中断；重试读取目标 hash 核实，不重复写。
- [ ] P6.7 审批后只更新涉及笔记的索引。草稿状态变为 accepted 后仍留 provenance，不删除审计依据。
- [ ] P6.8 第一版只支持新增与单文件修改，不支持自动移动、删除和多文件合并。

**验收：**已观察到的外部版本冲突必须阻止覆盖；审批内容不能被后续修改复用；中断恢复不产生重复内容。

### Phase 7 — 评估、文档、受控上线

**依赖：**Phase 6。

- [ ] P7.1 用合成 fixture 跑全部核心测试；真实私人内容不提交到测试仓库。
- [ ] P7.2 建立 20 条人工标注的中英混合工作查询；事先固定预期 source，测 Top-5 recall 与来源正确率。
- [ ] P7.3 跑 5 个真实端到端任务：无命中、有命中、来源变更、执行失败、成功沉淀后再次检索。
- [ ] P7.4 覆盖 personal grant、插件重启、客户端离线、归档任务、重复事件、写入中断恢复。
- [ ] P7.5 按现有 data-layer schema 写入允许的本地事件；事件 ID 使用 task/turn 标识稳定派生，防重复聚合；禁止正文与 prompt 进入 metrics。
- [ ] P7.6 文档化安装、禁用、索引重建、恢复、个人授权、知识审阅；明确没有全局 composer 拦截与后台整理承诺。
- [ ] P7.7 获得需要的安装/部署授权后启用插件；先单工作区试点，再用户选择扩大。不能仅因本计划存在就部署。

**验收：**下述 test matrix 全通过；核心可独立运行；禁用插件后现有 Paseo 工作流不变。

## 5. 测试矩阵与量化 Gate

| ID | 场景 | 预期 |
|---|---|---|
| T01 | 未授权个人库查询、路径穿越、symlink 越界 | 所有知识接口拒绝；不返回片段 |
| T02 | 指定单文件个人授权 | 仅本任务可读，重启需重授；无持久个人索引 |
| T03 | 日记/附件/草稿/归档 | 默认不检索 |
| T04 | 中文短词、三字以上词、英文混合、引号字符 | 不报 FTS 语法错误，不执行任意查询表达式 |
| T05 | 修改/删除/移动笔记、重复 note ID | 索引正确刷新；重复 ID 明确诊断 |
| T06 | 丢弃并重建索引 | 正文不变，检索结果集合一致 |
| T07 | 来源在搜索后、发送前修改 | 重新读取或移除，不发送旧行号片段 |
| T08 | 8 条/12,000 字符预算 | 确实受限，带截断提示与完整出处 |
| T09 | 笔记包含 prompt injection | 不能提升工具权限或触发审批 |
| T10 | 任务失败/取消/等待超时 | 不标记成功经验，不伪造验证 |
| T11 | 重复完成事件、重复点击 apply | 同一候选不重复创建或重复写入 |
| T12 | 原文在审批期间被修改 | 检测到 hash 不同则停止，不覆盖 |
| T13 | apply 文件完成、状态更新前中断 | 恢复能识别已完成写入 |
| T14 | 草稿拒绝、后续收到相同结果 | 不重新进入待审 |
| T15 | 使用个人材料后自动捕获 | 先授权永久沉淀，否则不写草稿正文 |
| T16 | 客户端关闭、重开、任务归档 | 恢复状态或明确不可恢复，不重复启动 |
| T17 | 禁用插件 | 普通 composer/provider/session 不受影响 |
| T18 | 任务 A 知识批准后任务 B 查询 | B 可引用正式新笔记及原文出处 |

量化目标（需要实测，不是当前性能声明）：

- 20 个固定查询至少 16 个在 Top-5 命中预期来源。
- 返回来源定位正确率 100%。
- 未授权个人来源泄漏到知识接口结果或草稿次数为 0。
- 重复任务事件造成重复草稿或正式笔记次数为 0。
- 试点规模下预热搜索 p95 ≤ 1 秒，上下文准备 p95 ≤ 3 秒；单独报告刷新与冷启动时间。
- 5 个 E2E 任务中至少 1 个证明跨任务知识复用。

测试执行采用项目现有 test runner；核心新增 Python unittest 风格测试，插件按 scaffold 的 typecheck/test 工具验证。不得为了写计划修改 CI。

## 6. 依赖、切分与工作量

```text
P0 接入验证 ─────────────────────────────┐
P1 Vault/边界 → P2 索引 → P3 检索 ───────┼→ P4 Paseo → P5 草稿 → P6 审批 → P7 验收
                                        ┘
```

| Phase | 工程量估算 | 可独立交付 |
|---|---:|---|
| P0 | 0.5–1 天 | 兼容性与接口验证记录 |
| P1 | 0.5–1 天 | Vault 结构与权限测试 |
| P2 | 1–1.5 天 | 本地可重建索引 |
| P3 | 1–1.5 天 | CLI 检索与上下文 |
| P4 | 1.5–2 天 | Paseo 知识任务入口 |
| P5 | 1 天 | 自动候选捕获 |
| P6 | 1.5–2 天 | 审批与恢复 |
| P7 | 1 天 | E2E 证据与用户文档 |

合计约 8–11 天，不含安装版本不兼容导致的升级、用户等待与额外中文检索调优。估算不是完成承诺。

建议每个 phase 一个独立 patch/PR；如用户授权提交，再按 phase 提交。并行仅用于不共享写入范围的工作：P0 插件验证与 P1–P3 核心；不要让两个 agent 同时改动相同核心协议文件。

## 7. 回滚与兼容性

- 功能禁用：禁用 Paseo 插件即可停止入口；不影响已存在 provider 和普通任务。
- 索引恢复：由工作 vault 正式笔记重新建立，不从缓存恢复“知识正文”。
- 知识写入恢复：使用 apply journal/preimage 和人工选择恢复，不能批量回滚整个 vault。
- 不删除已有 Brain/.agent 数据，不自动重写个人笔记，不修改权限协议。
- Runtime schema 有版本号；不兼容时 fail closed 并提示显式重建派生索引。任务/审批记录不可当缓存丢弃。
- CLI、插件与核心检查协议版本；不同版本不能猜字段继续写入。
- 日志仅保留状态、匿名化标识与计数。恢复 preimage 属于私有工作知识，限制文件权限且不上传。

## 8. 后续阶段（不进入本期验收）

只有前述闭环稳定且评价暴露明确缺口后才考虑：

1. 语义 embedding 或重排；先用评测证明关键词召回不足。
2. 探索分支增量知识回传，不复制完整父历史。
3. 更好的知识关联、重复建议和失效知识审阅。
4. 后台整理；先明确持久授权和唯一 scheduler owner。
5. 更多 vault 或附件正文解析；逐类明确隐私与解析策略。

## 9. 最终成功标准

不是“新增一个 vault 或搜索框”，而是：

> 用户在 Paseo 开始任务时，系统能找到过去已确认且有出处的工作知识；任务结束后，仅把有依据、经用户确认的新知识留下。个人库保持独立，原始日志留在执行系统，检索索引随时可重建，没有第二个知识权威。

## 10. 本次文档交付记录

- 仅新增此计划文件；未创建工作 vault、插件、索引或新运行服务。
- 未执行计划中的实现步骤；未修改 provider、权限、调度、个人笔记或数据库。
- 未提交或推送 Git。
- 文档验证：检查结构、phase ID、待办数量、固定快照和用户确认边界；运行 `git diff --check` 并单独检查新文件尾随空白。
