# Aster Harness 命名迁移

本次将项目品牌、发行包及 Python 命名空间统一为 Aster Harness。

| 项目 | 旧名称 | 新名称 |
| --- | --- | --- |
| 项目名 | pi-agent-core-py | Aster Harness |
| Python 发行包 | pi-agent-core-py | aster-harness |
| Python 导入 | pi_agent_core | aster_harness |
| 源码目录 | src/pi_agent_core | src/aster_harness |
| 项目版本 | 0.80.10 | 0.1.0 |

`0.1.0` 是衍生项目的独立版本；`0.80.10` 继续表示固定的 Pi 参考版本。两者分别记录，不表示升级或降级同一个上游发行包。

调用方需要替换导入，包括子模块：

```python
from aster_harness import AgentTool, create_harness
from aster_harness.harness.session import JsonlSessionStorage, Session
```

重新在工作区安装包后运行程序：

```bash
python -m pip install -e ".[dev]"
```

本次保留现有类、函数及其参数约定，没有添加旧 `pi_agent_core` 命名空间的兼容转发包。上游对照文档、早期设计和任务记录中的历史名称仍用于说明来源。

本地克隆目录可以继续叫 `pi-agent-core-py`，不影响 `aster_harness` 的导入。项目仓库为 [handlerzzy/harness](https://github.com/handlerzzy/harness)，仓库名与发行包名可以不同；原始提交历史保留，原作者仓库通过 `upstream` remote 跟踪。
