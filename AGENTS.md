# AGENTS.md — Palmimo Portal

Palmimo Portal is the device-hosted setup and dashboard web app of the
Palmimo DevKit: a FastAPI backend (`palmimo_portal/`, ports-and-adapters)
plus a React frontend (`frontend/`), shipped as a git checkout on the
device image and self-updating from this repository's GitHub Releases.
Commands, layout, and setup live in [README.md](README.md); releasing in
[doc/releasing.md](doc/releasing.md); the product design docs live in the
internal monorepo, not here.

- Language: Python >=3.12, managed with **uv** (never pip; run everything
  as `uv run ...` from the repository root).
- Frontend: Node per `frontend/.nvmrc`; `make check` is the drift gate for
  the two committed artifact sets (`frontend/openapi.json`,
  `frontend/src/api/generated/`).
- Layering: `api/` → `core/` → `ports.py`; only `adapters/` touches the
  OS. `PALMIMO_ADAPTERS=fake` (default) wires the in-memory fakes.
- Endpoint docstrings feed `openapi.json`: editing one requires
  regenerating (`make check`) and committing the artifacts.
- All prose in the tree is English (enforced by
  `tests/contracts/test_comment_language.py`).

## Prose economy — what belongs in comments, docstrings, and Markdown

The default is code that explains itself through names, types, and
structure. Prose is a budgeted exception: every line of it must carry
something the code cannot. There is no mechanical gate: reviewers hold
changes to these rules, and a PR that meaningfully raises a tree's prose
density should say why in its body.

1. **A docstring earns its length by stating what the signature cannot**:
   an invariant, a security rule, a lock order, a unit, an error contract,
   or a non-obvious *why*. Otherwise it is one line. `Args:`/`Returns:`/
   `Raises:` sections exist only where a caller would otherwise guess
   wrong — never to restate parameter names and types.
2. **Never narrate what the code does.** If a reader can see it in the
   code below, the sentence is dead weight. This includes paraphrasing a
   condition ("if the file is missing, return None") and walking through
   steps the function body already lists.
3. **History and alternatives do not live in the tree.** "Previously…",
   "PR #n changed…", review-iteration notes, and catalogues of rejected
   designs belong in git history, PR bodies, and the design docs. A
   docstring states the current contract only.
4. **A comment marks a trap.** Reserve inline comments for the line that
   looks wrong but is right — the deliberate ordering, the counterintuitive
   constant, the workaround with an upstream cause. Everything routine
   stays bare. No banner/section comments.
5. **One home per fact.** An invariant is stated once, in the module that
   owns it; other sites reference it (`see X`) instead of repeating it.
   When an explanation outgrows a docstring, it moves to `doc/` and the
   docstring keeps one line plus the pointer.
6. **Tests carry their claim in the name.** `test_<subject>_<behavior>
   [_when_<condition>]` is the specification; body comments appear only
   for non-obvious fixture mechanics, never to restate the name.
7. **Markdown owns orientation, not duplication.** README = what this is
   plus the commands; `doc/` pages own one topic each; nothing is said in
   two places. Rationale for a change goes in its PR body, not a new
   document.
