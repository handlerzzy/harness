# Domain Docs

This repo uses a single-context domain documentation layout:

- `GLOSSARY.md` at the repo root.
- Architecture decision records under `docs/adr/`.

## Before exploring

Read the root glossary and any ADRs relevant to the area being explored.

If these files do not exist, proceed silently. Domain modeling creates
them when terms or decisions are resolved.

## Use the glossary's vocabulary

Use glossary terms in issue titles, proposals, hypotheses, and tests.
Follow any explicit guidance about synonyms.

When a concept is missing, reconsider whether it matches the project's
language. If it represents a real gap, note it for `/domain-modeling`.

## Flag ADR conflicts

When a proposal contradicts an existing ADR, identify the ADR and explain
why the decision should be reopened.
