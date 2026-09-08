---
name: behaviors
description: The definitional reference for behaviors in this repo - Gherkin .feature files in per-project corpora at <project>/behaviors/ (e.g. apps/minds/behaviors/), their folder and tag organization, coordinates, invariants as Rule blocks with folder scoping, README/sidecar prose files, the witnesses test back-link convention, and the mngr behaviors CLI. Use whenever reading, writing, validating, querying, or otherwise reasoning about behaviors or .feature files in this repo.
---

# behaviors

This skill defines the behavior language: what the artifacts are, where they live, and what their syntax and structure mean.
Processes that create, update, or consume behaviors are out of scope here.

## What a behavior is

A behavior describes the externally observable behavior of one system's surface: the flows a user or client can take (scenarios) and the properties that hold across all flows and states (invariants).

The language's register:

- Behaviors describe observable behavior only.
  How a test drives the system (test clients, waits, selectors, fixtures) never appears.
- Protocol details (paths, status codes, redirect targets) appear only where they are part of the observable contract of the surface being specified.
- Behavior files never reference tests.
  The link runs the other way (see "Tests back-link to behaviors").

Behaviors are a distinct artifact class.
Do not confuse them with the repo-root `specs/` and `blueprint/` directories (design documents and implementation plans) or with `docs/` (user-facing documentation).

## Where behaviors live

Behaviors are organized into corpora, one per independent system.
A corpus root is that system's own `behaviors/` directory - `<project>/behaviors/` - so the corpus, its live-corpus guard test, and its witnesses markers travel with the codebase that owns them, including out of this monorepo if a project is ever spun out.
The first corpus in this repo is `apps/minds/behaviors/`; other systems (e.g. `libs/mngr_forward`) grow their own the same way.

Within a corpus, folders group behaviors by area; a folder holds its required `README.md`, its `.feature` files (one Feature each), and optional per-feature prose sidecars:

```
apps/minds/behaviors/
  README.md                  # context for the whole corpus
  browser-authorization/
    README.md                # context for browser-authorization/ and below
    invariants.feature       # Rules that hold for browser-authorization/ and below
    signin.feature           # one Feature: the sign-in flow
    signin.md                # optional prose sidecar for signin.feature
    session.feature
```

Naming and structure rules:

- Folder names and file basenames are kebab-case; the sole exception is the reserved `README.md`, spelled in the conventional uppercase.
- Every folder contains a `README.md`: the prose context for that folder and everything below it.
  `README` is reserved, so no `.feature` file may be named `README`.
  Every `README.md` opens with this incipit, verbatim, as its first line after the folder's title heading:

  > Understanding this behavior corpus calls for the tmr-behaviors skill; consult it when reading this file.

  Definitions live in the repo's committed glossaries (e.g. `apps/minds/docs/workspace/glossary.md`); a `README.md` references them and weaves any corpus-specific definitions into its prose rather than keeping a glossary section.
- `invariants.feature` is the other reserved filename (see "Invariants and scope").
- Any `.md` file other than `README.md` is the sidecar of the `.feature` file with the same basename in the same folder; `invariants.md` is simply the sidecar of `invariants.feature`.
  A non-`README.md` `.md` file with no matching `.feature` is invalid.
- Every folder has the same semantics, including the corpus root itself.
  Nesting is permitted; depth carries no special meaning.

Relationships between files are positional - expressed by basename and folder, never by links or paths written inside `.feature` files.

## Syntax: what "valid" means

A `.feature` file is syntactically valid if and only if `gherkin-official` (the Cucumber reference parser, classic `.feature` syntax) parses it; the version pinned by `apps/minds` is the arbiter.
The language uses the default English keywords only - no `# language:` headers.
The available constructs are `Feature`, `Background`, `Scenario` (synonym `Example`), `Scenario Outline` with `Examples` tables, `Rule`, the step keywords `Given` / `When` / `Then` / `And` / `But`, data tables, doc strings, and `#` comments.

A representative file:

```gherkin
Feature: Sign-in with a one-time login code
  The desktop client prints a login URL to its terminal at startup.
  Opening that URL is the only way to establish a session in a browser
  that has none.

  Background:
    Given a running desktop client

  @fresh-code
  Scenario: Opening a fresh login URL signs the user in
    Given the user is not signed in
    When the user opens the login URL in a browser
    Then the browser lands on the home page "/"
    And the user is signed in

  @missing-code
  Scenario Outline: Requests without a code are malformed input
    When a request is made to "<path>" with no one-time code parameter
    Then it is rejected as malformed input

    Examples:
      | path          |
      | /login        |
      | /authenticate |
```

Structural content - declarations, steps, tables - is normative.
Description slots (the free prose under `Feature:`, `Rule:`, `Scenario:`, and `Scenario Outline:` headers, before the first step or child) and `.md` files explain, but do not define.
The language does not partition explanatory prose between description slots and prose files; that split is the author's choice.

## Prose style: one sentence per line

All prose in a corpus - description slots and `.md` files - is written one sentence per line.
Sentences within the same paragraph follow one another immediately, with no blank lines in between; a new paragraph is separated by a single blank line.
Structural content (declarations, steps, tables) is unaffected: steps and table rows already occupy one line each.
This rule is the canonical specification of the convention; other skills and guides reference it rather than restating it.

## Prose style: define categories, don't enumerate them

Descriptive prose - every description slot and every `.md` file - defines each category or set by the property that makes it that category: a precise, logically complete requirement.
Descriptive prose must not describe a category by volunteering example members of it.

Enumerated examples rot: the real set drifts from the sample while the prose stands still.
An enumeration also inflicts narrowing bias - a future reader or agent treats the sample list as the definition and embeds that bias downstream.
Behavior prose is consumed by agents as specification, so an illustrative list is silently promoted to a contract.
Defining by property rather than by instance prevents both failure modes.

The single escape hatch: if an example is genuinely load-bearing for comprehension and cannot be replaced by a precise statement, it must be marked unambiguously as a non-exhaustive illustration with the literal phrase `e.g. (wlog)` or the spelled-out `without loss of generality`.
Absent that marker, no enumeration may stand where a category definition belongs.
Elimination is preferred to marking; the marker is a last resort.

This rule governs descriptive prose only.
It does not license editing normative Gherkin steps or `Examples` tables to strip concrete values - those are the observable contract, and enumerating concrete inputs and outputs there is correct and required.

A statement that holds across a folder subtree belongs once, in the most-scoping `README.md`, and is referenced rather than repeated per file.

Calibrate on this pair:

```text
Before: A session may be established from the desktop client, a browser presenting a fresh login URL, or a paired mobile device.
After:  A session may be established by any client surface able to present an unspent one-time code.
```

The Before line lists members and silently claims the list is closed; the After line states the property, so a surface not yet invented is already covered.

## Identity: tags and coordinates

Tags may appear wherever Gherkin permits them: on units - `Scenario`, `Scenario Outline`, and `Rule` - and on `Feature` and `Examples` blocks.
Every unit carries at least one tag.

- The first tag on a unit is its identity.
  A `Scenario Outline` has one identity covering all of its Examples rows.
- Tags after the first are auxiliary labels; they may repeat across units and have no defined semantics.
- Tags on `Feature` and `Examples` blocks have no defined semantics either - in particular, they do not cascade to scenarios - but each claims a coordinate (below) and must be unique like one.
- All tags are short kebab-case names that do not encode anything their location already says.

A unit's coordinate - the stable handle everything outside the behavior uses to refer to it - joins the folder names on the path from the corpus root to the unit's file, then its raw identity tag, with dots:

- `@fresh-code` in `apps/minds/behaviors/browser-authorization/signin.feature` has the coordinate `browser-authorization.fresh-code`.
- `@no-tls` in `apps/minds/behaviors/networking/tunnels/hole-punching.feature` has the coordinate `networking.tunnels.no-tls`.
- `@single-use-codes` in `apps/minds/behaviors/invariants.feature` has the coordinate `single-use-codes` - zero folders on the path, so the coordinate is the raw tag alone.

Counter-example - the common wrong guess: the coordinate of `@fresh-code` above is NOT `browser-authorization.signin.fresh-code`.
File basenames never appear in coordinates; only folders qualify.
This is what lets a scenario move between files in its folder - or a file be renamed or split - without any unit changing identity.

A coordinate is claimed by each unit's identity tag and by each tag on a `Feature` or `Examples` block; auxiliary tags claim nothing.
No coordinate is claimed twice within its corpus - equivalently, all claiming tags are unique within their folder.
Coordinates are corpus-relative: distinct corpora are distinct namespaces.
The `@` sigil is Gherkin tag syntax and stays in the file; coordinates are bare dotted names.

## Invariants and scope

An invariant is a property that must hold across all scenarios, states, and interleavings within its scope - not just the flows spelled out in scenarios.
Invariants are written as `Rule:` blocks:

- The Rule name states the property; the Rule description carries the rationale.
- Identity works exactly as for scenarios: the first tag.
- A Rule may stand alone, or carry illustrating scenarios as children (`Scenario Outline` children included).
  Each child is a unit with its own identity tag.

Nothing in a tag marks a unit as an invariant.
The kind is structural: being a `Rule:` is what makes it an invariant, and tooling reports the unit kind.

Scope is determined entirely by which file the Rule lives in:

- A `Rule:` in an ordinary feature file applies to that file's Feature.
- A `Rule:` in a folder's `invariants.feature` applies to that folder and everything below it.
- This holds at every level: in `apps/minds/behaviors/browser-authorization/invariants.feature` a Rule binds all of `browser-authorization/`; in `apps/minds/behaviors/invariants.feature` it binds the entire corpus.
- Scope never crosses a corpus boundary: an invariant binds only units of its own corpus.
  When systems share a constraint, each corpus states it from its own surface's perspective.

A file-scoped Rule in an ordinary feature file:

```gherkin
Feature: Session lifetime

  @survives-restart
  Scenario: Sessions survive a desktop-client restart
    Given a signed-in user
    When the desktop client is stopped and started again
    Then the user is still signed in

  @installation-bound-tokens
  Rule: Only session tokens minted by this installation are accepted
    A token created under another data directory is treated as signed out.
```

Gherkin nests every scenario that follows a `Rule:` header under that Rule, so file-scoped Rules come after the file's ordinary scenarios.

A subtree-scoped Rule in an `invariants.feature`, with an illustrating child:

```gherkin
Feature: Browser-authorization invariants

  @single-use-codes
  Rule: A one-time code grants at most one session, ever
    Every presentation of an already-spent code is refused, under any interleaving of requests.
    Rationale: the login URL is written in plain text to a terminal; single use bounds that exposure.

    @spent-code-refused
    Example: A spent code cannot sign anyone in again
      Given the login URL has already been used to sign in
      When anyone presents the same code again
      Then authentication is refused
```

## Tests back-link to behaviors

A test that verifies a behavior unit declares it, using the unit's coordinate:

```python
@pytest.mark.witnesses("browser-authorization.fresh-code")
def test_authenticate_with_valid_code_sets_cookie() -> None: ...

@pytest.mark.witnesses("browser-authorization.prefetch", partial="does not assert the code remains unspent")
def test_login_page_redirects_via_script() -> None: ...
```

- `partial=` states what the test does not cover; omit it when the test covers the unit fully.
- A test may carry several `witnesses` markers.
- The marker is registered in the shared pytest settings and usable from any project in the monorepo.
- A project's tests witness that project's corpus.
  Within a corpus/test-tree pairing (see `matrix`), every marker must name a unit of the paired corpus; a marker that resolves to no unit is a broken link, not a coverage gap.

## Tooling

`mngr behaviors` is the CLI over a corpus, named per invocation (run as `uv run mngr behaviors <subcommand> --root <project>/behaviors ...` from the repo root):

- `validate` - parses every behavior file and enforces the rules in this document.
- `list` - emits the corpus as JSONL, one record per unit (scenario, scenario outline, or rule), carrying coordinate, unit kind, location, and the coordinates of the Rules in scope for the unit; structural filters select by folder area, unit kind, tag, name, or step text.
- `matrix` - joins the corpus against the `witnesses` markers in its paired test tree (defaulting to the corpus root's parent, i.e. the owning project), reporting per-unit coverage.

`uv run mngr behaviors --help` is authoritative for invocation detail.
For AST-level needs beyond the CLI, `gherkin-official` (a dependency of `mngr_behaviors`) is importable directly.
