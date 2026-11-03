---
status: accepted
---

# 沿用统一模型协议，首版实现 Chat Completions 适配器

用户确定首版只实现 OpenAI Chat Completions API 协议，沿用固定版本 Pi 的统一请求、消息、结果及事件契约，使未来其他底层 API 通过各自适配器接入同一上层。统一模型协议以 `pi-ai` 的 `Context`、`AssistantMessage`、`AssistantMessageEvent` / `AssistantMessageEventStream` 和 `Models` 为依据，并连接已有 Python Agent/Harness；模型事件与 Agent 生命周期/工具执行事件保持分层。统一意味着共同的类型和语义，具体模型支持的能力、增量分块和事件数量可以不同；首版不因此扩展为全量 API 或所有兼容厂商特性覆盖。

来源：[统一类型](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/ai/src/types.ts)、[Models](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/ai/src/models.ts)、[Chat Completions 适配器](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/ai/src/api/openai-completions.ts)。

补充：按 [ADR 0004](0004-pi-as-reference-with-deliberate-scope.md)，上述类型及事件是语义和结构参考，具体字段、命名及模块可按本项目需求取舍，不承诺与 Pi 逐字段兼容。

首版统一模型协议的核心能力确定为文本、工具调用及结果、结束状态、错误和取消；thinking、usage 和厂商扩展信息为可选能力，后端缺少时允许缺省。
