# Upstream parity baseline

This project is a Python rewrite of `@earendil-works/pi-agent-core` from
[`earendil-works/pi/packages/agent`](https://github.com/earendil-works/pi/tree/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent).

- Upstream package version: `0.80.10`
- Baseline commit: `3da591ab74ab9ab407e72ed882600b2c851fae21`
- Baseline commit date: `2026-07-17T17:13:39+02:00`
- Upstream license: MIT
- Last verified against upstream `origin/main`: `2026-07-18`

The baseline was captured from the repository source rather than an npm bundle,
so source modules, public exports, package metadata, and upstream behavioral
tests could be audited together. The public parity table and intentional
language-level adaptations are documented in [PARITY.md](PARITY.md).

## SDK implementation reference

Use the baseline commit above for the entire Pi monorepo when implementing the
first usable SDK. The version `0.80.10` identifies the upstream agent package;
the full commit identifies the exact source snapshot, including sibling `ai`
and `coding-agent` packages. The version is verifiable in the pinned
[package metadata](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/package.json).

For each ticket:

1. Read the corresponding source below and locate its relevant behavioral tests
   in the same snapshot before designing the change. A source checkout must be
   checked out at the full baseline commit; verify its HEAD before using it.
   Fixed-commit GitHub source links are also suitable for reading.
2. Reuse the existing Python implementation, adapting the referenced design to
   the accepted SDK spec and ADRs. Record the upstream sources consulted and
   meaningful simplifications or Python adaptations in the ticket's Comments.
3. Verify the ticket's observable behavior at the agreed public SDK/Harness and
   HTTP boundary, retaining relevant existing regressions. Consult upstream
   tests for behavior; the local acceptance criteria determine what ships.

The accepted [scope decision](docs/adr/0004-pi-as-reference-with-deliberate-scope.md)
permits deliberate simplification rather than one-to-one compatibility. Preserve
this fixed architecture reference while making those choices explicit. Reading
another revision for a specific bug fix is supplemental: record its exact commit
and rationale separately. Adopting a newer architecture requires an explicit
change to the baseline decision; do not silently substitute a moving branch.

### Reference map for the seven SDK tickets

| Ticket | Source to consult at the baseline commit |
| --- | --- |
| 01: 流式文本 SDK | Unified [model types](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/ai/src/types.ts), [model access](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/ai/src/models.ts), [Chat Completions adapter](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/ai/src/api/openai-completions.ts), and [Harness](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/src/harness/agent-harness.ts) |
| 02: 自定义工具多轮任务 | [Agent loop](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/src/agent-loop.ts), [Agent types](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/src/types.ts), and the model types/adapter from 01 |
| 03: 终止、取消与重试 | [Agent lifecycle](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/src/agent.ts), [proxy](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/src/proxy.ts), and the adapter from 01; EOF fixes and bounded retry behavior still follow the local spec where the baseline has gaps |
| 04: 内置编码工具 | [Coding tools](https://github.com/earendil-works/pi/tree/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/coding-agent/src/core/tools), [execution environment](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/src/harness/env/nodejs.ts), and [output capture](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/src/harness/utils/shell-output.ts) |
| 05: Session 与资源 | [Session](https://github.com/earendil-works/pi/tree/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/src/harness/session), [skills](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/src/harness/skills.ts), [prompt templates](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/src/harness/prompt-templates.ts), and Harness from 01 |
| 06: 上下文压缩 | [Harness compaction](https://github.com/earendil-works/pi/tree/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/agent/src/harness/compaction), Harness from 01, and [coding-agent session coordination](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/coding-agent/src/core/agent-session.ts) for automatic-compaction ideas |
| 07: 示例、打包与验收 | [Coding-agent SDK assembly](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/coding-agent/src/core/sdk.ts) and [SDK documentation](https://github.com/earendil-works/pi/blob/3da591ab74ab9ab407e72ed882600b2c851fae21/packages/coding-agent/docs/sdk.md); packaging and examples target this Python SDK |

The coding-agent package has its own AgentSession coordination path. Borrow its
tool and SDK design while adapting to this project's existing Harness/Session
lifecycle, as required by the spec. Its UI/rendering and full application
infrastructure are outside the first SDK's scope.
