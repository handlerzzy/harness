# 02: 通过自定义工具完成多轮 Agent 任务

**What to build:** 开发者注册带参数约束的自定义工具后，模型能够流式生成工具调用，Agent 执行工具并把结果交回模型，直到完成任务。订阅者能分辨生成调用与实际执行，所有调用和结果在多轮历史中保持正确关联。

**Blocked by:** 01：通过公开 SDK 入口完成流式文本对话。

**Status:** ready-for-agent

**Upstream reference:** 开始实现前，按[固定上游参照与本票模块映射](../../../UPSTREAM.md#sdk-implementation-reference)阅读对应源码及行为测试，并在 Comments 记录参照来源和必要取舍。

**Spec coverage:** 7-13, 17-20, 29, 46-47.

- [x] 公开入口接受现有自定义工具契约及参数 schema，工具声明转换成 Chat Completions 请求；正常工具结果和错误结果均能被下一次模型请求消费。
- [x] 按调用标识正确重建名称和参数；支持一个响应中的多个调用，以及多个调用交错到达的参数片段，不能混合参数或错配结果。
- [x] 工具只有在响应结局允许执行且参数完成解析、通过校验后才执行；长度截断、不完整或非法 JSON 不触发真实工具副作用。
- [x] 参数约束失败、未知工具或工具执行错误产生可理解的失败反馈；模型可根据可恢复的工具错误修正调用并继续。
- [x] 模型事件表达调用生成过程，Agent 事件表达实际工具执行和轮次；生成工具调用本身不宣称工具已执行，最终结果与事件一致。
- [x] 使用已有循环、调度和 hooks 能力，完成至少两次模型请求及真实自定义工具执行；历史回传保留调用标识、参数和结果归属。
- [x] 主测试通过公开 SDK/Harness、可控 HTTP 响应和带可观察副作用的真实自定义工具验证正常闭环、交错调用、非法参数、长度截断和错误后修正，不替换内部调度器。
- [x] 补充注册自定义工具、订阅事件和理解工具错误的使用说明，保留现有 Agent/loop 调度及 hooks 回归测试。

## Comments

- 2026-11-03：对应原十一张方案的 03。此票以自定义工具验证公共契约，内置编码工具在 04 使用同一契约接入。

- 2026-11-03：完成 `create_harness(..., tools=...)`，接受现有 `AgentTool` 与同字段 mapping。工具声明、assistant 工具调用与 `toolResult` 在 Chat Completions HTTP 边界双向转换；复用原 Agent loop 的参数校验/强制转换、调度、更新、hooks 和错误反馈，不引入第二套工具执行器，不更改现有 API。
- 固定上游参照：确认本地源码 checkout 的 HEAD 为 `3da591ab74ab9ab407e72ed882600b2c851fae21`。阅读 [Agent loop](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/src/agent-loop.ts)、[Agent types](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/src/types.ts)、[统一模型类型](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/ai/src/types.ts)、[Chat Completions adapter](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/ai/src/api/openai-completions.ts) 及其 [history transformation](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/ai/src/api/transform-messages.ts)。
- 上游行为测试参照：[agent-loop.test.ts](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/test/agent-loop.test.ts) 的工具闭环、长度截断、参数准备、并发完成顺序/历史源顺序和 hooks；[validation.test.ts](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/ai/test/validation.test.ts) 的 schema 与强制转换；[openai-completions-empty-tools.test.ts](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/ai/test/openai-completions-empty-tools.test.ts)、[openai-completions-reasoning-details.test.ts](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/ai/test/openai-completions-reasoning-details.test.ts) 的工具转换及按 ID 归属设计，以及 [tool-call-without-result.test.ts](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/ai/test/tool-call-without-result.test.ts) 的失败调用后继续提问。
- Python/范围取舍：仅转换函数工具，不引入厂商 strict/deferred-tool 开关、签名扩展或调用 ID 改写。按 stream index/完整 ID 分开累积调用，支持延后到达的 ID、按 ID 续传和名称片段；索引/标识冲突明确失败。增量事件保留原始 JSON 文本，`partial.arguments` 在最终严格解析前保持 `{}`，不使用上游 salvage parser 修补非法 JSON；拒绝 NaN/Infinity 等非 JSON 常量。正常工具响应中任一调用 JSON 非法时，整条模型响应失败且全部调用均不执行。
- 安全执行与错误：只有 `finish_reason=tool_calls` 映射的 `toolUse` 响应可进入实际执行；长度截断沿用原 loop 对整个 batch 生成失败工具结果（无法解析的预览保持空对象），不触发回调。schema 失败、未知工具、执行异常沿原 loop 返回可恢复的失败反馈。`tool_execution_start` 沿用上游语义表示处理尝试，校验/拦截失败后以 `isError=True` 结算，不表示回调必然运行；纯生成事件不启动执行。
- 历史与文档：成功调用和结果保留 ID、名称、参数及源顺序；工具多文本块按换行连接。按上游做法在后续 HTTP 请求中过滤 error/aborted assistant，Session 本身仍保留失败记录，避免重放不完整调用；通用 Session 中断恢复仍由 05 处理。README 新增注册、模型/Agent 事件区分、参数解析、工具错误与错误后修正说明。
- TDD：沿规格已确认的公开 SDK/Harness + 可控 HTTP seam，逐片实现闭环、交错重建、长度截断、非法响应及失败调用后的历史过滤；真实自定义工具写入临时文件，HTTP transport 是唯一替换点。另验证已有 schema coercion 和结果 hooks、完整参数在模型终态前无副作用、直接 Models 只生成工具调用且终态事件与结果一致。
- 验证：新增 `tests/test_sdk_tools.py` **30 项**，连同已有 SDK 测试 **53 passed**；`ruff format --check src tests`、`ruff check src tests`、`mypy src` 和 `git diff --check` 均通过。完整 `pytest -q` 为 **220 passed**，包含原 Agent/loop、调度、hooks、Session 和代理回归。沙箱内完整回归停在既有异步文件/JSONL 测试；该单项及完整回归在沙箱外分别以 0.07s / 0.69s 通过。普通测试均不需网络或凭据；未连接真实模型服务。

### Standards

以实施前提交 `7c3aa7a397208d43ea948f5148c1c4f0ae72390b` 为基点，由独立子代理审查实现提交 `56a2717`：未发现可操作问题（0 项）。全部变更符合领域词汇、ADR 0003/0004 的协议分层及取舍要求；工具执行复用既有 Agent loop，SDK 负责装配，适配器负责 HTTP 转换和模型事件。Comments 已记录固定上游源码、行为测试和 Python 取舍；新增测试沿公开 SDK/Harness 与 HTTP 边界验证可观察副作用。未发现值得单独提出的基线代码异味；跳过格式、lint 和类型工具已覆盖的事项。

### Spec

由另一独立子代理审查：未发现可操作问题（0 项）。公开工具注册、请求/历史转换、交错调用重建、终态及参数校验、可恢复错误、事件分层和 HTTP 边界测试均符合规格；未发现超出本票范围的功能扩张。已核对既有 Agent loop 的长度保护、校验与 hooks，以及固定上游相关实现和行为测试。

审查汇总：Standards 0 项，Spec 0 项；两轴均无待修复问题。审查后仅补充本审查记录，运行时代码和测试未变。
