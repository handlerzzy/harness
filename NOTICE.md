# 来源与署名

Aster Harness 基于 [Wesley Zhang 的 pi-agent-core-py](https://github.com/wesleyzhangwq/pi-agent-core-py) 继续开发。其运行时基础来自 [Mario Zechner 等人维护的 Pi](https://github.com/earendil-works/pi)，参考版本为 `pi-agent-core 0.80.10`，固定提交为 `3da591ab74ab9ab407e72ed882600b2c851fae21`。

原始 Python 移植提供了 Agent Loop、事件流、工具调度、控制队列、树形会话、JSONL 存储、资源加载与压缩等基础。当前工作区在此基础上已有 Chat Completions SDK、模型协议适配、自定义工具闭环、取消与有界请求重试、内置编码工具等开发提交。具体演进以 Git 历史和实现为准。

Aster Harness 使用独立的项目名、Python 包名和版本号，继续采用 Pi 作为可复查的设计参考。它是社区衍生项目，与 Pi 官方发布独立。

原始版权声明和 MIT 授权条款完整保留在 [LICENSE](LICENSE) 中；技术来源见 [UPSTREAM.md](UPSTREAM.md)，原始移植的模块对应关系见 [PARITY.md](PARITY.md)。
