# 与 earendil-works/pi 的实现范围及基本可用性对比

调查日期：2026-10-30（北京时间）。这是源码和本地运行核验报告，不是对功能完成率的估算。

## 结论

本项目已经具备可嵌入 Python 应用的 agent 运行时基础，覆盖固定旧版上游核心包的主要模块，并有实际运行通过的测试支撑。它还不是安装后即可与真实模型对话、操作代码仓库的完整 Pi 编码助手。

最短可用路径是：接入一个真实模型后端 → 提供模型可调用的编码工具 → 组装配置、会话和命令行入口 → 完成真实模型端到端验收。无需先移植整个上游 monorepo 或完整 TUI。

## 对比基准

| 对象 | 核验版本 | 含义 |
| --- | --- | --- |
| 本地仓库 | `a2517400ec34da38b6e65ded602e0ebe63bbcb2b`，包版本 `0.80.10` | 本次本地代码检查和测试对象 |
| 本地声明的上游基线 | `3da591ab74ab9ab407e72ed882600b2c851fae21`，`packages/agent` 版本 `0.80.10` | 2026-07-17 的旧版核心包，不是完整 Pi 产品 |
| 调查时上游 `main` | `f09d01b85e0384647fcab6779d35ab9dabe04be3`，`packages/agent` 版本 `1.4.0` | 提交时间为北京时间 2026-10-30 05:11:08，含尚未发布的修改 |

本地定位见 [README](README.md)、[UPSTREAM](UPSTREAM.md)、[PARITY](PARITY.md)。上游基线直接核验了 [package.json](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/package.json) 和 [公共导出](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/src/index.ts)；当前版本见 [固定提交](https://github.com/earendil-works/pi/commit/f09d01b85e0384647fcab6779d35ab9dabe04be3) 及其 [package.json](https://github.com/earendil-works/pi/blob/f09d01b85e0384647fcab6779d35ab9dabe04be3/packages/agent/package.json)。

## 已经做了什么

下表表示源码中有实现，并有相关本地测试；不表示已对所有上游行为做穷尽差分验证。

| 能力 | 本地实现与核验范围 |
| --- | --- |
| Agent 状态和循环 | `agent.py`、`agent_loop.py`：多轮模型/工具循环、prompt/continue、状态修改、上下文转换 |
| 流式事件 | `event_stream.py`、`abort.py`：异步流、最终结果、生命周期订阅、取消 |
| 工具运行框架 | `agent_loop.py`、`schema.py`：工具声明、参数校验和转换、并行/顺序执行、更新、前后 hooks、终止处理 |
| 用户插话与后续任务 | `agent.py`、`agent_harness.py`：steering、follow-up、next-turn 队列及保存时机 |
| 高层 Harness | `harness/agent_harness.py`：模型/资源配置、provider hooks、会话写入、save-point、分支导航 |
| 会话和持久化 | `harness/session/`：树形会话、JSONL、内存存储、创建/列出/打开/分叉/删除、标签和模型变更记录 |
| 上下文压缩基础 | `harness/compaction/`：token 估算、切分、摘要请求、压缩条目、分支摘要；Harness 提供手动 `compact()` |
| Skills 和提示模板 | `harness/skills.py`、`prompt_templates.py`：发现、frontmatter、诊断、忽略规则、模板参数与调用格式 |
| 本地执行原语 | `harness/env/nodejs.py`：文件读写、路径、目录、shell、超时/取消；名字保留 Node，但实现使用 Python |
| 输出处理 | `harness/utils/`：截断、完整输出留存、Unicode 边界处理 |
| Pi 代理协议客户端 | `proxy.py`：向 `/api/stream` 发请求、解析 SSE、重建部分消息和工具调用 JSON |
| 跨语言兼容 | `_utils.py` 等：camelCase/snake_case 别名、JavaScript 数值/JSON/UTF-16 语义 |

这些模块与旧版上游目录基本一一对应，参见 [本地模块映射](PARITY.md) 与 [旧版 agent 源码目录](https://github.com/earendil-works/pi/tree/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/src)。

## 相对完整 Pi，还没有什么

上游包含多个各司其职的包，不能将缺少兄弟包的功能直接认定为核心包移植失败。当前包职责见 [上游总览](https://github.com/earendil-works/pi/blob/f09d01b85e0384647fcab6779d35ab9dabe04be3/README.md)。

| 上游能力 | 本地现状 | 对基本使用的影响 |
| --- | --- | --- |
| `pi-ai`：真实 provider、模型集合、认证、统一流和 usage | 没有 Python provider 实现或模型注册服务；保留注入接口 | **真实对话的首要缺口** |
| 编码工具：read/bash/edit/write，另有 grep/find/ls 等 | 有执行工具的框架、文件与 shell 原语，但没有封装好的内置编码 `AgentTool` 集合 | **模型还不能开箱即用地读改代码** |
| CLI、交互界面、print/JSON 模式 | 无命令行入口，无 `__main__.py`，无 TUI；`pyproject.toml` 没有 scripts | **普通用户缺少启动方式** |
| 配置和应用组装 | 没有统一的 key/base URL/model 配置、项目上下文加载和现成会话工厂 | 使用者需要手动连接各层 |
| 自动压缩和溢出恢复 | 有 `should_compact`、摘要和手动压缩；Harness 没有自动调度这些能力 | 短会话可先做，长任务需要补齐 |
| 请求失败恢复 | 有错误消息、取消和重试参数透传，没有现成 provider 重试实现或应用级恢复协调 | 网络抖动、限流时体验不足 |
| 扩展、主题、Pi packages、MCP 客户端 | skills/templates/hooks 已有；完整扩展加载、主题、包管理和 MCP 服务连接没有实现 | 属于后续扩展，非首版必需 |
| RPC 集成 | 仅有代理客户端；没有编码助手 RPC 服务/命令协议 | 不影响先做本地 CLI |
| 新版 `pi-durable` 的持久化执行语义 | JSONL 可保存和恢复历史，但没有新版 durable runtime 的中途崩溃恢复协议 | “恢复历史”不能等同于“恢复未完成执行” |

上游对应证据：[pi-ai](https://github.com/earendil-works/pi/blob/f09d01b85e0384647fcab6779d35ab9dabe04be3/packages/ai/README.md)、[编码工具](https://github.com/earendil-works/pi/blob/f09d01b85e0384647fcab6779d35ab9dabe04be3/packages/coding-agent/src/core/tools/index.ts)、[SDK 组装](https://github.com/earendil-works/pi/blob/f09d01b85e0384647fcab6779d35ab9dabe04be3/packages/coding-agent/docs/sdk.md)、[CLI](https://github.com/earendil-works/pi/blob/f09d01b85e0384647fcab6779d35ab9dabe04be3/packages/coding-agent/docs/cli.md)、[自动压缩](https://github.com/earendil-works/pi/blob/f09d01b85e0384647fcab6779d35ab9dabe04be3/packages/coding-agent/docs/compaction.md)、[MCP](https://github.com/earendil-works/pi/blob/f09d01b85e0384647fcab6779d35ab9dabe04be3/packages/coding-agent/docs/mcp.md)、[durable](https://github.com/earendil-works/pi/blob/f09d01b85e0384647fcab6779d35ab9dabe04be3/packages/durable/README.md)。本地依据为 [公开导出](src/pi_agent_core/__init__.py)、[构建配置](pyproject.toml)、[Harness](src/pi_agent_core/harness/agent_harness.py) 和当前 `src/` 文件清单。

## 三个容易误判的边界

### 1. 能调用 Agent，不等于已经连接真实模型

本地 [stream_simple](src/pi_agent_core/agent_loop.py#L82) 只寻找模型对象上的 `stream_fn` 或 `stream`；没有后端时明确报错。本次实测 `Agent(); await agent.prompt('hello')`：

```text
stopReason: error
error: No default Python model provider is configured. Pass stream_fn ...
tools: 0
```

`Agent.prompt()` 可以把异常转成 assistant 错误消息，所以“await 返回了”也不代表请求成功，应用需要检查 `stopReason` / `state.error_message`。证据见 [错误生命周期](src/pi_agent_core/agent.py#L460)。

[README 快速示例](README.md#quick-start) 可以运行，但其回复是固定的 `Hello from Python`，没有发送模型请求。设置 API key 本身不会自动补出 provider。

`stream_proxy` 则需要一个实现 Pi `/api/stream` 协议的后端；它不是通用 OpenAI-compatible endpoint 客户端。本仓库没有提供该后端。见 [代理请求实现](src/pi_agent_core/proxy.py#L386)。

### 2. 能执行文件和 shell 操作，不等于模型有可调用的编码工具

[NodeExecutionEnv](src/pi_agent_core/harness/env/nodejs.py) 提供操作原语；[AgentTool](src/pi_agent_core/types.py) 规定模型工具的参数、描述和执行回调。还需要把两者封装起来，处理参数、输出截断、工具错误及文件编辑匹配失败。本地 README 的 calculator 只展示了工具定义方式。

### 3. 支持压缩，不等于长对话会自动压缩

[Harness.compact](src/pi_agent_core/harness/agent_harness.py#L709) 是手动入口，依赖 `models.complete_simple()`；对话则依赖 `models.stream_simple()`。只补一个流式回调，尚不足以支持完整 Harness 摘要流程。见 [摘要调用边界](src/pi_agent_core/harness/compaction/compaction.py#L308)。

## 本次实际验证

临时环境位于 `/tmp`，没有修改业务代码。安装了本项目声明的开发依赖，在 Python 3.11.16 下执行：

| 检查 | 结果 |
| --- | --- |
| `pytest -q` | **167 passed**，含参数化用例；非真实 LLM 接口验收 |
| `mypy src` | **通过**，34 个源文件 |
| `ruff format --check src tests` | **通过**，47 个文件 |
| `ruff check src tests`，使用 `uv.lock` 的 `0.15.22` | **通过** |
| `ruff check`，按依赖范围安装到 `0.17.3` | **3 条 RUF036**，属于新增类型联合排序规则 |
| 构建 sdist/wheel | **通过** |
| 新建独立环境安装 wheel，用 `python -I` 导入 | **通过**，确认版本 `0.80.10`，从 site-packages 导入 |
| README quick start | **通过**，输出模拟回复 |
| 默认 Agent 真实后端 | **未配置**，返回上述错误消息 |
| 真实模型 → 工具 → 再次推理 | **未验证**；当前仓库无现成后端供该验收使用 |

初始宿主环境未安装项目和完整测试依赖，直接运行会在收集阶段失败；在临时环境正常安装后通过。受限沙箱中的线程/文件测试停滞，允许本地执行后整套用例通过，因此没有将沙箱现象算成项目缺陷。本次只实跑 Python 3.11；3.12/3.13 是 [CI 矩阵配置](.github/workflows/ci.yml)，未在本次分别执行。

CI 当前以 `pip install -e '.[dev]'` 安装，没有消费 `uv.lock`，所以工具新版本可能引入检查漂移。建议固定 CI 开发工具版本或同步更新规则。见 [CI](.github/workflows/ci.yml) 与 [依赖范围](pyproject.toml)。

### 额外复现：代理提前结束可能导致等待不退出

用 `httpx.MockTransport` 返回 HTTP 200 和一个 `start` SSE 事件，随后 EOF，不发 `done/error`：

```text
迭代收到的事件：['start']
stream.result()：在 EOF 后仍未完成，wait_for(..., 0.2) 超时
```

原因是 [proxy.py](src/pi_agent_core/proxy.py#L444) 正常 EOF 时直接 `stream.end()`，而 [EventStream.end](src/pi_agent_core/event_stream.py#L57) 在没有终止事件/显式结果时不会结算结果 future；agent 循环又会等待 `.result()`。如果计划使用代理接入，这是基本可靠性修复项。当前上游已在 [proxy.ts](https://github.com/earendil-works/pi/blob/f09d01b85e0384647fcab6779d35ab9dabe04be3/packages/agent/src/proxy.ts) 对缺终止事件的连接结束生成错误事件。这里记录问题，未进行修复。

## 达到基本可用的建议顺序

### P0：真实模型与工具闭环

1. **先实现一个 provider adapter。** 只选一个确定的 API 协议，支持 messages/system/tools 的转换、文本增量、工具调用参数、usage、结束原因、取消、错误。适配为当前 `AssistantMessageEventStream` 契约，无需先重写全部 `pi-ai`。若走代理，则需同时落实后端和上述 EOF 修复。
2. **需要 Harness 时，再提供 `Models` 适配。** 实现 `stream_simple` 与 `complete_simple`，使普通对话和摘要都能工作。
3. **提供最小编码工具集合。** 优先 read、bash、edit、write；已有参数校验、执行环境和截断组件可以复用。先保证 edit 匹配明确、失败可反馈、shell 超时和取消能贯穿。
4. **完成真实后端验收。** 模型读取临时文件、修改指定内容、运行验证命令、消费 toolResult 并给出正确结果；再测错误 key、工具失败、取消和截断。不能只用模拟回复代替。

### P1：可启动、可持续使用

5. **增加一个统一创建入口和简单 CLI。** 加载 key/base URL/model/cwd/system prompt，装配 tools、resources、JSONL 会话，提供流式输出、Ctrl-C、会话新建/继续及错误退出码。可以先做简易 REPL 和单次 `-p`，完整 TUI 可后置。
6. **连接上下文和资源加载。** 把项目指令文件、skills/templates、工作目录和工具说明组装进实际会话；现有加载器本身不会完成整套应用启动流程。
7. **补长任务恢复。** 调度已有阈值判断和压缩模块，处理 context overflow，给瞬时失败加有界重试。按照实际模型上下文窗口调整 reserve/keepRecent，而非始终照搬默认值。
8. **补可复制的真实示例和稳定 CI 工具版本。** 让新用户能从安装走到真实工具调用，并明确哪些兼容性仅针对旧版上游。

作为 Python SDK，P0 是最关键投入；作为用户每天直接使用的编码助手，还需要 P1。完整 TUI、OAuth、多 provider、MCP、RPC、插件和 durable runtime 可以根据使用场景逐步增加。

## 是否应该先追当前上游

不建议在“先基本可用”的阶段，将上述缺口与整套新版架构迁移绑定。

上游 [agent CHANGELOG](https://github.com/earendil-works/pi/blob/f09d01b85e0384647fcab6779d35ab9dabe04be3/packages/agent/CHANGELOG.md) 显示：

- `0.87.0`（2026-09-21）：`finishTurn` 替代 `shouldStopAfterTurn`，增加 `prepareRequest` 和队列预览。
- `1.0.0`（2026-10-01）：从 agent-core 删除实验性 Harness、sessions、compaction、skills、templates 等导出，持久会话转向 `pi-durable`。
- `1.1.0` 增加工具耗时；`1.4.0` 增加请求身份选项。当前 `main` 还有代理工具参数处理优化。

当前源码还采用 transcript 内的 SystemMessage 等新上下文组织方式；参见 [Agent](https://github.com/earendil-works/pi/blob/f09d01b85e0384647fcab6779d35ab9dabe04be3/packages/agent/src/agent.ts)、[循环类型](https://github.com/earendil-works/pi/blob/f09d01b85e0384647fcab6779d35ab9dabe04be3/packages/agent/src/types.ts)、[当前公共导出](https://github.com/earendil-works/pi/blob/f09d01b85e0384647fcab6779d35ab9dabe04be3/packages/agent/src/index.ts)。

因此，本项目适合明确定位为“0.80.10 核心运行时的 Python 实现，另加实用接入层”。如果后续目标改为持续兼容最新 Pi，则应单独设计迁移计划和兼容测试，尤其不要把旧版 JSONL 会话简单视为新版 durable 执行协议。
