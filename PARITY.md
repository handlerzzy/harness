# Upstream parity

This project rewrites the runtime behavior of
`@earendil-works/pi-agent-core` package `0.80.10` at upstream commit
`3da591ab74ab9ab407e72ed882600b2c851fae21`.

## Source map

| Upstream TypeScript | Python implementation | Covered behavior |
| --- | --- | --- |
| `src/agent.ts` | `src/pi_agent_core/agent.py` | Stateful agent, lifecycle, queues, abort, listeners, mutators |
| `src/agent-loop.ts` | `src/pi_agent_core/agent_loop.py` | Prompt/continue loops, streaming, hooks, tool validation and execution |
| `src/proxy.ts` | `src/pi_agent_core/proxy.py` | Proxy request, SSE decoding, partial-message reconstruction, partial JSON |
| `src/types.ts` | `src/pi_agent_core/types.py` | Models, context, tools, loop configuration, event/result contracts |
| `src/index.ts` | `src/pi_agent_core/__init__.py` | Public package exports |
| `src/node.ts` | `src/pi_agent_core/node.py` | Node-style entry point plus local execution environment |
| `src/harness/agent-harness.ts` | `src/pi_agent_core/harness/agent_harness.py` | Hooks, save points, queues, resources, compaction, tree navigation |
| `src/harness/messages.ts` | `src/pi_agent_core/harness/messages.py` | Custom, shell, branch-summary and compaction-summary messages |
| `src/harness/prompt-templates.ts` | `src/pi_agent_core/harness/prompt_templates.py` | Discovery, frontmatter, diagnostics, command parsing, substitution |
| `src/harness/skills.ts` | `src/pi_agent_core/harness/skills.py` | Recursive loading, ignore rules, metadata validation, invocation formatting |
| `src/harness/system-prompt.ts` | `src/pi_agent_core/harness/system_prompt.py` | Skill list formatting and XML escaping |
| `src/harness/types.ts` | `src/pi_agent_core/harness/types.py` | Results, coded errors, resources, storage and execution protocols |
| `src/harness/compaction/branch-summarization.ts` | `src/pi_agent_core/harness/compaction/branch_summarization.py` | Branch collection, preparation and summary generation |
| `src/harness/compaction/compaction.ts` | `src/pi_agent_core/harness/compaction/compaction.py` | Token estimates, cut points, preparation and summary generation |
| `src/harness/compaction/utils.ts` | `src/pi_agent_core/harness/compaction/utils.py` | Conversation serialization and file-operation tracking |
| `src/harness/env/nodejs.ts` | `src/pi_agent_core/harness/env/nodejs.py` | Files, symlinks, shell execution, cancellation, timeouts and temp paths |
| `src/harness/session/jsonl-repo.ts` | `src/pi_agent_core/harness/session/jsonl_repo.py` | Create, list, open, fork and delete JSONL sessions |
| `src/harness/session/jsonl-storage.ts` | `src/pi_agent_core/harness/session/jsonl_storage.py` | Version-3 JSONL metadata, entries, leaf and label restoration |
| `src/harness/session/memory-repo.ts` | `src/pi_agent_core/harness/session/memory_repo.py` | In-memory repository lifecycle and forking |
| `src/harness/session/memory-storage.ts` | `src/pi_agent_core/harness/session/memory_storage.py` | Tree storage, labels, leaf records and path reconstruction |
| `src/harness/session/repo-utils.ts` | `src/pi_agent_core/harness/session/repo_utils.py` | IDs, timestamps, fork selection and result unwrapping |
| `src/harness/session/session.ts` | `src/pi_agent_core/harness/session/session.py` | Session mutation, context transforms/projectors, branching and metadata |
| `src/harness/session/uuid.ts` | `src/pi_agent_core/harness/session/uuid.py` | Monotonic UUIDv7 generation |
| `src/harness/utils/shell-output.ts` | `src/pi_agent_core/harness/utils/shell_output.py` | Binary sanitization and bounded/full streamed capture |
| `src/harness/utils/truncate.ts` | `src/pi_agent_core/harness/utils/truncate.py` | UTF-8/UTF-16-aware head, tail and line truncation |

`abort.py`, `event_stream.py`, `schema.py`, and `_utils.py` provide Python
counterparts for behavior that the TypeScript package imports from
`@earendil-works/pi-ai/compat` or obtains directly from the JavaScript runtime.

## Public API policy

Every executable export from upstream `src/index.ts` is available from
`pi_agent_core`. The package additionally provides idiomatic snake-case names;
camel-case aliases preserve straightforward migration from TypeScript.
`pi_agent_core.node` re-exports the core API and `NodeExecutionEnv`, matching the
upstream `./node` entry point.

## Intentional language adaptations

- TypeScript object-shaped values are represented by dictionaries, while
  stable callable entities such as `AgentTool`, `AgentContext`, `Skill`, and
  `PromptTemplate` also have typed Python classes.
- JavaScript `undefined` has no direct Python value. Optional inputs use
  `None`; serialized data follows the upstream JSON behavior wherever an
  omitted-versus-null distinction affects the wire or persistence contract.
  `JS_UNDEFINED` is available where both values are valid and distinct, such
  as deleting an individual request-header patch key while retaining `None`
  as the upstream `null` header suppressor.
- JavaScript UTF-16 length/slicing, property ordering, number formatting,
  `JSON.stringify`, and numeric schema coercion are implemented explicitly.
- JavaScript promise/event streams are async iterators with an awaitable
  `.result()` in Python.
- Resource discovery uses deterministic UTF-16 ordering instead of the host
  ICU default-locale `localeCompare`; ordinary ASCII resource names sort
  identically while non-ASCII ordering no longer varies by machine locale.
- A background coroutine failure rejects an event stream's `.result()` rather
  than leaving the consumer waiting on an unresolved promise.
- `Agent.continue()` is exposed as `Agent.continue_()` because `continue` is a
  Python keyword. `getattr(agent, "continue")` remains compatible.
- The upstream package delegates direct provider calls to sibling package
  `pi-ai`; provider implementations are not part of `packages/agent`. This port
  preserves that boundary through injected `stream_fn` callables or
  `stream_proxy` rather than silently selecting a provider SDK.
- `NodeExecutionEnv` reproduces the contract with Python filesystem and
  subprocess primitives. Platform-specific error text can differ, while error
  codes and success/failure semantics remain stable.

## Conformance strategy

The tests mirror upstream behavioral cases and add cross-runtime checks for
the boundaries most likely to drift in a language port. Those checks include:

- exact agent lifecycle and tool event ordering;
- live JavaScript `Set`-style listener iteration;
- parallel completion order versus source-order persistence;
- late tool-update suppression and termination patches;
- steering/follow-up timing and save-point snapshots;
- schema default/coercion behavior and TypeBox-compatible failures;
- proxy streaming JSON repair/partial parsing;
- JSONL round trips, session forks, labels and compaction transforms;
- JavaScript number/JSON formatting and UTF-16 string semantics;
- shell output ordering, incremental UTF-8 decoding and process-tree aborts.
