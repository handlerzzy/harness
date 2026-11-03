# Issue tracker: Local Markdown

Issues and specs for this repo live as Markdown files in `.scratch/`.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`.
- The spec is `.scratch/<feature-slug>/spec.md`.
- Implementation issues live at
  `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`.
  Use one file per ticket.
- Record triage state as a `Status:` line near the top of each issue.
  See `triage-labels.md` for the role strings.
- Append comments and conversation history under `## Comments`.

## When a skill says "publish to the issue tracker"

Create the spec or issue file at the appropriate path above,
creating directories as needed.

## When a skill says "fetch the relevant ticket"

Read the referenced file. Resolve an issue number within the relevant
feature directory; ask for the feature if the number is ambiguous.

## Wayfinding operations

Used by `/wayfinder`. The map is a file with one child file per ticket.

- Map: `.scratch/<effort>/map.md`, containing Notes,
  Decisions-so-far, and Fog.
- Child ticket: `.scratch/<effort>/issues/NN-<slug>.md`,
  numbered from `01`, with the question in the body.
  A `Type:` line records `research`, `prototype`, `grilling`, or `task`.
- Blocking: a `Blocked by: NN, NN` line near the top.
  A ticket is unblocked when every listed ticket is resolved.
- Frontier: scan for open, unblocked, unclaimed tickets;
  first by number wins.
- Claim: set `Status: claimed` and save before starting work.
- Resolve: append the answer under `## Answer`, set `Status: resolved`,
  then append a gist and link to Decisions-so-far in `map.md`.

Wayfinding uses `claimed` and `resolved` as workflow status values.
The five triage roles apply when triaging issues.
