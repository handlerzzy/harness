# 03: 可靠终止、取消任务并有限重试请求

**What to build:** 使用者能够及时停止模型请求和正在执行的工具，并明确区分请求失败、工具失败和取消。安全的瞬时请求失败可有限重试，已经执行的工具不会因重试重复执行；现有代理提前断流也能可靠结束。

**Blocked by:** 02：通过自定义工具完成多轮 Agent 任务。

**Status:** ready-for-agent

**Upstream reference:** 开始实现前，按[固定上游参照与本票模块映射](../../../UPSTREAM.md#sdk-implementation-reference)阅读对应源码及行为测试，并在 Comments 记录参照来源和必要取舍。

**Spec coverage:** 12, 30-33, 46-47.

- [x] 请求建立前、等待响应时、开始生成后以及工具执行中的取消均被正确传递，事件订阅、最终结果等待和 Harness 运行状态最终结束。
- [x] 取消不会触发新一轮重试、模型请求或尚未开始的工具执行；以可取消的真实自定义工具验证，不依赖尚未实现的内置 bash。
- [x] 请求失败、工具失败与用户取消有可观察且可区分的结果；保留已有成功、长度限制和工具调用结束语义，失败不伪装成成功。
- [x] 仅对安全的瞬时请求失败采用有界重试；次数和等待采用明确默认值并文档化，配置错误、错误凭据和主动取消不进行无意义重试。
- [x] 重试范围不跨越已经执行的工具而重跑任务；已发布增量后的失败处理语义明确，不能把两次尝试的内容错误拼接成一次成功响应。
- [x] 修复现有代理在无合法终态 EOF 后最终结果持续等待的问题；合法结束、错误和取消继续正常结算，不引入代理服务端。
- [x] 使用可控 HTTP 的超时、断流和失败响应，验证开始前后取消及重试耗尽；用有界等待断言事件消费和最终结果均可结束。
- [x] 在真实临时文件等可观察副作用发生后触发后续请求失败，验证重试没有再次执行已经完成的工具；保留代理分块 SSE、正常结束及取消的回归覆盖。
- [x] 说明错误分类、取消方法、重试默认值和重试边界，检查 HTTP 客户端及后台任务的生命周期没有因失败或取消遗留等待。

## Comments

- 2026-11-03：合并原十一张方案的 01 与 04，两者共用终态和错误结算的验证主题。03 完成后，04 验证取消贯穿真实 shell 进程。

- 2026-11-03：完成取消、请求级有限重试和代理 EOF 结算。沿用规格已经确认的公开 SDK/Harness + 可控 HTTP seam，以真实自定义工具写入临时文件或等待 AbortSignal；新增行为测试先复现重试缺失、代理结果挂起、未执行调用结果遗漏、启动 hook 取消窗口、无效时限和 payload hook 取消后仍发请求，再逐项实现。
- 固定上游来源：确认 `/tmp/pi-sdk-upstream` HEAD 为 `3da591ab74ab9ab407e72ed882600b2c851fae21`。阅读 `packages/agent/src/agent.ts` 的 runWithLifecycle/abort/finishRun、`src/agent-loop.ts` 的终态和工具批次取消、`src/proxy.ts` 的 SSE/AbortSignal/EOF、`src/harness/agent-harness.ts` 的 executeTurn/abort，以及 `packages/ai/src/api/openai-completions.ts` 的 signal、maxRetries、finish_reason、错误结算。行为参考 `packages/agent/test/agent.test.ts` 的 active signal / idle settlement、`test/agent-loop.test.ts` 的工具与终态顺序、`test/harness/agent-harness-stream.test.ts`，以及 `packages/ai/test/abort.test.ts` 的立即/生成后取消和 `openai-completions-retry.test.ts` 的默认零重试/显式次数。
- 必要取舍：上游适配器默认关闭底层 SDK 重试；按本票本地规格使用已有 httpx 实现请求级策略，默认最多重试 2 次（总计 3 次），指数等待 100/200 ms，等待上限默认 1000 ms，可通过已有 maxRetries/maxRetryDelayMs 选项修改。只允许 408/429/500/502/503/504、超时、网络/远端协议失败，且未发布任何模型事件（包括 start）时重试。永久状态即使读取错误体时超时也不重试；非法配置/凭据/JSON/取消均不重试。每次 HTTP 尝试独立应用有限正数 timeoutMs，默认 60000 ms；不按 Retry-After 延长等待，不引入全任务重跑或代理重试。
- 取消与历史：继续复用共享 AbortSignal 和可协作取消的工具契约；提前在 Harness 开始运行时建立控制器，覆盖启动 hook 窗口。取消中断 HTTP 建立/读取/重试等待，payload hook 取消后不发请求；工具批次取消不再调用 provider、在继续处理队列或调用下一轮 hooks 之前结束运行，为尚未执行的调用记录失败工具结果，保持后续 HTTP 历史配对。进行中的工具用 signal.wait/throw_if_aborted 配合 finally 释放资源；阻塞同步 Python 回调不提供强制终止。
- 终态与资源：已发布增量后的失败保留内容且明确 error，不拼接重试输出。代理无合法终态 EOF 明确 error；收到 done/error 后退出 HTTP 读取，在资源清理后发布终态，消费和 result 都能结束。取消回调在生产任务内注册和移除，保留立即取消语义；外部注入的客户端由调用者拥有，自建客户端仍用 async context 关闭。
- 验证：新增 `tests/test_sdk_lifecycle.py` 43 项，代理增加 6 项并更新旧的无终态 EOF 断言。全部使用离线 MockTransport 和真实工具/临时文件，不需要外部凭据。覆盖请求前/等待中/生成后/退避中取消、工具取消及跳过未开始回调、取消后历史继续、重试耗尽、瞬时/永久状态、已发布内容不重试、无效选项及 HTTP 清理；现有成功、length、toolUse、工具失败后修正和分块 SSE 回归保留。
- 完成检查：`ruff format --check src tests`、`ruff check src tests`、`mypy src`、`git diff --check` 全部通过；Python 3.11.16 完整 `pytest -q` 为 **269 passed**。沙箱中既有 `test_filesystem_and_jsonl_round_trip` 等待，经单项沙箱外验证 0.07 秒通过后，完整离线回归在沙箱外 4.68 秒通过。未连接真实服务。

### Standards

以实施前提交 `8e290e6102537c3f446e49c0b6aaf9b737c16cbb` 为基点，由独立子代理审查实现提交 `e3b08ff`：未发现问题（0 项）。实现遵循领域词汇、Agent/Harness 分层、HTTP 客户端所有权和已确认的公开 SDK/Harness 测试边界。Comments 按 UPSTREAM.md 要求记录固定上游源码、行为测试、重试策略取舍和验证结果。修改中未发现值得报告的基线代码异味；格式、lint、类型等工具覆盖的事项未重复列为审查问题。

### Spec

由另一独立子代理审查：未发现问题（0 项）。本票及引用规格要求均已覆盖，包括可区分终态、HTTP 与协作工具取消、有限请求级重试、已完成工具副作用保留、增量保留为失败且不重试、代理 EOF 结算。公开 SDK/Harness 与 HTTP 测试使用有界等待验证这些可观察行为。未发现需求遗漏、实现错误或范围扩张；相对上游的重试/EOF 差异遵循本地规格。

审查汇总：Standards 0 项，Spec 0 项；两轴均无待修复问题。审查后仅补充本记录并修正文案，运行时代码和测试未变。
