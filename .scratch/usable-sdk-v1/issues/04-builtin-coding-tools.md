# 04: 使用内置编码工具完成修改与验证闭环

**What to build:** 模型在指定工作目录中通过 read、write、edit 和 bash 读取项目、修改文件、运行验证命令，并根据真实工具结果继续处理。复用已有执行环境与输出组件，让开发者通过公开入口即可使用完整的基础编码工具集。

**Blocked by:** 03：可靠终止、取消任务并有限重试请求。

**Status:** ready-for-agent

**Upstream reference:** 开始实现前，按[固定上游参照与本票模块映射](../../../UPSTREAM.md#sdk-implementation-reference)阅读对应源码及行为测试，并在 Comments 记录参照来源和必要取舍。

**Spec coverage:** 21-29, 31, 33, 46-47.

- [x] 四个工具通过同一自定义工具契约提供明确的名称、参数约束和结果；公开入口能启用它们并与自定义工具共同使用。
- [x] read 读取文本文件，write 创建或写入文件，路径按配置的工作目录解析；文件错误作为可理解的工具结果返回模型。
- [x] edit 只在目标文本可明确匹配时修改；匹配缺失或存在歧义时明确失败且文件保持原样，不要求移植全部上游编辑扩展。
- [x] bash 在指定工作目录运行真实命令，返回输出和退出码；非零退出有明确表示，模型能据此继续修正或结束任务。
- [x] shell 超时和 Agent 取消能够终止相关进程；取消后没有后台命令继续产生副作用，也不会继续调度后续工具或模型请求。
- [x] 面向模型的长输出受限，截断有明确标识并提供完整输出位置；检查实际日志内容可读取，不能只返回不可用的路径。
- [x] 通过公开 SDK/Harness 和可控 HTTP 响应，驱动读取临时错误文件、修改、执行本地验证命令及消费结果的完整任务；断言实际文件内容和验证结果。
- [x] 同一边界覆盖 write、edit 缺失或歧义、文件失败、非零退出、长输出、超时和进程取消；结合请求重试验证实际写文件或执行命令不重复。
- [x] 文档说明四个工具的输入、工作目录、错误、超时、取消及输出截断行为；复用现有执行和输出能力，保持现有相关测试通过。

## Comments

- 2026-11-03：合并原十一张方案的 05 与 06。已有文件操作、shell 执行、进程终止和日志捕获作为基础，本票集中补模型工具包装与端到端验证。

- 2026-11-03：实现公开 `create_harness(builtin_tools=True, tools=...)` 与 `create_coding_tools(env)`，四个内置工具复用 AgentTool、NodeExecutionEnv、execute_shell_with_capture 和 truncate_head/tail；默认仍仅启用调用者提供的工具，重名在创建时明确失败。README 记录参数、cwd、工具错误、退出码、超时/取消、调度、截断与日志保留规则。
- 固定上游来源：确认 `/tmp/pi-sdk-upstream` HEAD 为 `3da591ab74ab9ab407e72ed882600b2c851fae21`。参考 `packages/coding-agent/src/core/tools/{read,write,edit,bash}.ts` 的工具契约、唯一匹配、写入和 shell 输出行为；`packages/agent/src/harness/env/nodejs.ts` 的文件/进程组与 AbortSignal；`packages/agent/src/harness/utils/shell-output.ts` 的尾部截断及完整日志。行为测试参考 `packages/coding-agent/test/tools.test.ts` 的 read 截断、write 父目录、edit 缺失/重复、不部分修改、bash 失败/超时/取消日志与按行截断，以及 `packages/agent/test/harness/nodejs-env.test.ts` 的 cwd、非零退出、超时、取消与完整输出。
- 必要取舍：首版 read 仅文本；edit 使用 `path/oldText/newText` 的单次字面替换，拒绝空、缺失及包括重叠在内的歧义匹配，保留未修改的 CRLF/BOM，不移植上游模糊匹配、多区域编辑、diff、图像及 UI。含内置工具的批次沿用现有 sequential 调度，避免同一回复中的写入/编辑/验证竞态；同一工厂工具组的写入/编辑共享锁，不承诺外部进程协调。
- 终止与输出修复：超时覆盖进程及管道读取，主 shell 退出后仍可终止其进程组；命令完成及 Python task 取消同样清理进程组。取消后先收尾，不调度后续工具/请求。保留已有直接 env.exec 的信号退出码默认行为，内置 bash 显式设置 `signalExitCode=True`，将信号结束按 128+信号报告。日志收尾不复用已取消信号，确保成功、非零退出、超时、取消都能返回可读的完整捕获日志；超时错误保留输出及路径，日志存在即标记截断，避免缓冲裁剪隐藏截断事实。read 长输出指向原文件，bash 指向持久临时日志，正文上限沿用 2000 行/50 KiB。
- 验证：`tests/test_sdk_coding_tools.py` 新增 25 项公开 SDK/Harness + 可控 HTTP 行为测试，真实文件及 subprocess 覆盖完整修复闭环、write 父目录、同批次顺序、缺失/歧义/重叠编辑不写文件、文件错误、非零及信号退出、两种截断限制在成功/失败/超时/取消时的完整日志、超时及取消后子进程无延迟副作用，以及 HTTP 重试不重复写入/编辑/命令。TDD 首先复现缺少入口、超时丢输出和 bash 信号退出伪成功，再实现对应行为。相关回归 114 passed；Python 3.11.16 完整离线 `pytest -q` 为 **294 passed**（8.22 秒）。`ruff format --check src tests`、`ruff check src tests`、严格 `mypy`、`git diff --check` 全通过。沙箱阻断既有 asyncio.to_thread 唤醒，单独复现后在沙箱外运行测试。未连接真实模型服务；其他 Python 版本未在本环境运行。

### Standards

以本轮实施前提交 `87bce327eba14c614e543b8bb5ff27b8795eb59b` 为基点，由独立子代理审查实现提交 `75dee25` 及补充的 ticket 记录：未发现问题（0 项）。改动复用 AgentTool、已有执行环境及输出组件，遵循公开命名约定，README 说明了 API 与运行行为；Comments 按 UPSTREAM.md 记录固定上游、行为测试、取舍和验证。未发现值得报告的基线代码异味，未重复报告工具已覆盖的格式、lint 和类型规则。

### Spec

由另一独立子代理审查：未发现问题（0 项）。四个工具、SDK 组装、缺失/歧义编辑不写入、shell 退出状态、超时和 Agent 取消、受限输出与可读完整日志、重试不重复副作用均符合本票；测试遵守已确认的 SDK/Harness 与可控 HTTP 边界。裁剪符合 ADR 0004 与验收标准，未发现遗漏、范围扩张或实现错误。

审查汇总：Standards 0 项，Spec 0 项；两轴均无待修复问题。审查后仅补充本票完成和审查记录，运行时代码及测试未变。
