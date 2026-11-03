# 01: 通过公开 SDK 入口完成流式文本对话

**What to build:** Python 开发者显式配置 Chat Completions 服务后，可以通过简洁的公开入口创建 Harness、提交文本任务、订阅 Agent 事件并取得最终结果。沿用统一模型协议，把现有循环和 Session 接到真实 HTTP 适配器，形成第一条可运行的文本链路。

**Blocked by:** None (can start immediately).

**Status:** ready-for-agent

**Upstream reference:** 开始实现前，按[固定上游参照与本票模块映射](../../../UPSTREAM.md#sdk-implementation-reference)阅读对应源码及行为测试，并在 Comments 记录参照来源和必要取舍。

**Spec coverage:** 1-8, 12-16, 18, 34, 45-47.

- [x] 公开入口接受 API key、base URL、模型标识、上下文窗口、输出限制、system prompt 和工作目录；默认使用内存 Session，调用者无需自行实现协议转换。
- [x] 仅实现 Chat Completions，正确转换 system、历史消息及必要调用选项；供应商原始负载和响应处理留在适配器边界，Agent 循环消费统一模型协议。
- [x] 提供统一的流式和一次性完成访问方式；一次性完成从同一流的最终结果取得消息，后续摘要可以直接复用。
- [x] 文本增量、累计消息与最终结果语义一致；公开 Agent 事件可以观察模型更新和最终结局，正常停止与长度限制可区分。
- [x] 缺少 thinking、usage 或厂商信息仍能完成任务；存在且适配器识别的可选信息被保留，未知 usage 与真实零用量可区分，不伪造内容或计费数据。
- [x] 错误凭据、HTTP 错误、请求超时和无合法终态的 EOF 明确失败；事件消费与最终结果等待均能在有界测试时间内结束，请求建立失败无需先发开始事件。
- [x] 通过公开 SDK/Harness 和可控 HTTP transport 验证连续文本对话、后续请求中的历史、分块 UTF-8、不同切块下的等价结果及可选字段缺省；普通测试不需要网络或凭据。
- [x] 复用现有 Harness/Session、事件和 hooks 路径，保留现有相关测试；新增最小使用说明，如需改变已有公开 API，记录影响与迁移方式。

## Comments

- 2026-11-03：按最近讨论的七张切分方案发布，对应原十一张方案的 02。工具闭环由 02 扩展，完整取消、重试及代理回归由 03 扩展；本票仍须满足文本路径自身的基本终止保证。

- 2026-11-03：实现 `create_harness(...)`、轻量 `Models.stream_simple/complete_simple` 和独立 Chat Completions HTTP 适配器。保留已有 `AgentHarness`、Session、统一消息/模型事件及 provider/context/lifecycle hooks；原注入流函数 API 无需迁移。默认内存 Session，凭据仅存在 Models 对象，配置上下文窗口供后续上下文管理使用。
- 上游参照：下载源码并确认 HEAD 为 `3da591ab74ab9ab407e72ed882600b2c851fae21`。阅读 `packages/ai/src/types.ts`（Context、消息和终态事件）、`src/models.ts`（completeSimple 从流结果完成）、`src/api/openai-completions.ts`（请求转换、分块文本/thinking、usage、finish_reason、错误和 hooks）、`src/utils/event-stream.ts`，以及 `packages/agent/src/harness/agent-harness.ts`。行为测试参照 `packages/ai/test/stream.test.ts`、`openai-completions-empty-tools.test.ts`、`openai-completions-response-model.test.ts`、`total-tokens.test.ts` 和 `packages/agent/test/harness/agent-harness-stream.test.ts`。
- 取舍：沿用 camelCase 消息/选项及 Python 现有 dict 协议；使用已依赖的 httpx，不增加 OpenAI SDK、模型目录、隐式鉴权或厂商兼容层。默认使用 `max_completion_tokens`，兼容服务可通过已有 payload hook 改写。thinking 只识别 `reasoning_content`；usage 缺省及未知字段保持缺省，不伪造零计数或 cost。增量消息采用深拷贝快照，使积存事件也能准确表示各时刻内容。
- 范围：本票只交付文本路径；工具声明/调用/结果及图片显式失败，工具闭环留给 02。直接 HTTP 路径支持基本取消，默认总请求时限 60 秒、无重试，代理与完整取消/重试场景留给 03。合法 finish_reason 后 EOF 可成功；缺少合法终态一律失败。成功终态在 HTTP 资源释放之后发出，避免关闭失败使事件与结果不一致。
- 本地验证：新增公开 SDK/Harness + MockTransport 行为测试，覆盖两轮历史、hooks、Session、分块 UTF-8 等价性、流/一次性完成、可选元数据及零/未知 usage、正常/长度结束、HTTP/JSON/超时/EOF 失败、建立/读取阶段取消及资源关闭。普通测试无需网络或凭据。完整回归及双轴审查结果见下。

- 完成验证：使用现有 `uv.lock` 的依赖版本（`uv sync --frozen --extra dev`；未改动 lockfile）。`ruff format --check src tests`、`ruff check src tests`、`mypy src` 全部通过。完整 `pytest -q` 为 **190 passed**，其中新增 SDK 测试 23 项。沙箱内既有 JSONL/异步文件 I/O 测试发生等待，单项在沙箱外立即通过，最终完整回归亦在沙箱外通过。未连接真实服务或使用真实凭据。

### Standards

以实施前提交 `a2517400ec34da38b6e65ded602e0ebe63bbcb2b` 为基点，由独立子代理审查：未发现规范违反或有意义的代码异味。遵循领域词汇、模型事件/Agent 事件分层、ADR 0003/0004 的适配器边界，且记录固定上游来源及取舍。

### Spec

由另一独立子代理审查：未发现本票需求遗漏、范围扩张或错误实现。公开配置、内存 Session、文本历史、流/一次性完成、可观察事件、可选元信息、明确失败及离线 HTTP 边界测试均已覆盖；工具、重试、代理修复与自动上下文管理按票据分工保留给后续工作。

审查汇总：Standards 0 项，Spec 0 项；两轴均无待修复问题。
