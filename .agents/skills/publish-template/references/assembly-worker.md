# Assembling a template (the background worker's guide)

You are the worker `publish-template` dispatched. Your task file names the
template (`slug`, `title`, `description`), the `BASE_REF` to build on, the
include paths, any opted-in data paths, and the published-version modifications
the user confirmed. Do all of this in one run, with no back-and-forth unless you
are genuinely stuck.

## Contents

- Before anything else
- 1. Assemble
- 2. Apply the published-version modifications
- 3. Write the manifest (`template.md` + `template.toml`)
- 4. Write the README
- 5. Design the thumbnail
- 6. Commit, check, report

## Before anything else

Step 1 resets your checkout to the template base: every tracked file becomes the
base's version (this guide may not exist there), and everything gitignored is
deleted, your task file included. So first, from your worktree root:

```bash
eval "$(uv run .agents/shared/scripts/parse_task_frontmatter.py data/.tasks/launch-task/<slug>/task.md)"
LEAD_WORKTREE="${LEAD_WORK_DIR:-$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")}"
PT="$LEAD_WORKTREE/.agents/skills/publish-template/scripts"
echo "PT=$PT LEAD_WORKTREE=$LEAD_WORKTREE FINISH_REPORT_PATH=$FINISH_REPORT_PATH"
```

`$PT` is the publishing workspace's own copy of this skill's tools. Use it for
every tool after step 1: the base your checkout becomes may carry older copies,
or none. The same goes for this guide: after step 1, re-read it at
`$PT/../references/assembly-worker.md`.

Note the three values that prints. Every command runs in a fresh shell, and after
step 1 the task file is gone, so they cannot be worked out again: set the ones a
command uses at its start (`PT=...; bash "$PT/scan_secrets.sh" ...`).

## 1. Assemble

Run the assembly script from your worktree root, with the values from your task
file:

```bash
bash .agents/skills/publish-template/scripts/build_template.sh \
    --base-ref <BASE_REF> \
    --slug <slug> \
    --title "<title>" \
    --description "<description>" \
    --include <path> [--include <path> ...] \
    [--data-include <path> ...]
```

It resets to the clean base, overlays the selected paths, runs the secret scan,
generates `template.md`, `template.toml`, a placeholder `template.svg` and a
`README.md`, checks the base boots, and commits. On ANY non-zero exit nothing
was committed: report `stuck` quoting its stderr verbatim. Never retry with
different arguments and never assemble anything by hand.

## 2. Apply the published-version modifications

Skip this if the task says "None requested." These are edits the user wants in
the published copy only (a hardcoded channel replaced with a neutral default, an
account name removed); the live workspace keeps its own. Apply each one, then
re-scan every file you changed:

```bash
bash "$PT/scan_secrets.sh" <each modified file>
```

A finding means a modification did not fully remove a credential: fix it or
report `stuck`, never leave it in. A missing or failing scanner is a broken
environment: report `stuck`; there is no weaker fallback.

## 3. Write the manifest (`template.md` + `template.toml`)

`template.md` has `<!-- FILL-IN (publishing agent): ... -->` blocks. Replace every
one with real, specific content, or an explicit "none" sentence -- never leave a
block or a blank section. `template.toml` holds the machine-readable half; the
validator fails the publish if the two disagree.

- **What it is / How it works.** For a reader who has never seen the original
  agent: what it does, what it produces, which supervisord programs run it, which
  ports it uses and how the pieces connect.
- **Publication history.** The publisher's changelog. Replace its block with
  `### v1 (YYYY-MM-DD) -- <one line: what this version publishes>`, today's date.
  Never write into "Adaptation history" -- that is the adopters' log.
- **Requirements** -- what an adopter must deal with before this is theirs. Two
  kinds, both under this one heading:
  - **Activation**, one machine-readable line each, which the adopting agent
    acts on by itself before asking the user anything:
    - `requires_permission: <latchkey scope> / <permission>` for every service
      the code reaches through `latchkey curl` (e.g. `slack-api /
      slack-read-all`), with a matching `[[requirements.permission]]` in the TOML;
    - `requires_llm: <how it reaches Claude, and what an adopter needs>` whenever
      any included code calls an LLM, with a `[requirements.llm]` table. How an
      agent reaches Claude differs by environment: keyed (`ANTHROPIC_API_KEY` ->
      litellm) or keyless (`claude -p` on the subscription), per
      `use-ai-integration`. Name the method the code was built for. If the code
      hardcodes one path (a key, an endpoint, a model), also list switching it as
      an adaptation;
    - `requires_secret:` lines are generated, with their
      `[[requirements.secret]]` entries, from the included apps' `app.toml`
      `[[secrets]]` and skills' `secrets:` front matter. Leave them as they are; a
      wrong one is fixed at its declaration (the `connect-external-service` skill
      documents the shapes), then re-run step 1.

    A missing or vague activation line silently breaks adoption: the adopting
    agent never asks for what it does not see. The publisher confirms this list,
    so make it complete.
  - **Adaptation**, prose bullets: what an adopter must decide or rewire (stubbed
    integrations, hardcoded accounts or channels, data that was left out). Mirror
    each as `[[requirements.adaptation]]` in the TOML.

  If there is nothing of either kind, write exactly: "No requirements -- runs as
  published, with no external permissions or secrets."
- **Environment.** What the included code needs INSTALLED beyond the stock
  workspace, derived from the code (every binary it shells out to, every global
  npm/uv/cargo tool it runs), not from what this machine happens to have. In the
  TOML's `[environment]`: `apt` takes bare package names (the adopter's apt
  snapshot supplies versions), and `npm_global`, `uv_tools` and `cargo_crates` are
  `name = "version"` maps. Anything with no package database (a downloaded
  binary, a browser) ships as a `system/scripts/env.d/<NNNN>-<slug>-<name>.sh`
  unit with `NNNN` >= 2000, added to the recipe's `include` and listed in
  `env_d_units`. Every apt package must resolve in the pinned mirror; the
  validator checks. If nothing is needed, write exactly: "Nothing extra -- runs on
  the stock workspace environment."
- **The recipe** (`[recipe]` in the TOML). `exclude`: every deliberate exclusion --
  paths a reader might expect that were left out, and features stripped from an
  included path. A later update re-runs the recipe, and this is what keeps those
  out. `modification_rules`: one per published-version modification, written as a
  RULE and never as the removed value ("replace the hardcoded team channel with a
  neutral default", never the channel's name) -- the point of the modification
  is that the value does not ship.

**Front matter is YAML.** `title:` and `description:` are the user's words: wrap
any value containing a `"`, a `: `, or a leading `#`/`&`/`*`/`%` in double
quotes, escaping inner quotes (`title: "The \"Daily\" Digest: v2"`).
`build_template.sh` already writes them this way; a hand edit must match, or the
front matter stops agreeing with the TOML.

## 4. Write the README

`README.md` is the repo's landing page. Fill its "Why you care", "How to use it"
and "Ideas for making it yours" blocks per `references/readme-recipe.md` (read it
now, before step 1 removes it, or from `$PT/../references/readme-recipe.md`).
Leave the hero image and the Open in Imbue Studio button exactly as generated,
including the `MINDS_TEMPLATE_REPO_URL` placeholder: the lead fills that in once
the repo exists. Do not show the README to the user; the lead does that.

## 5. Design the thumbnail

`template.svg` is a generic placeholder and must never be published. Replace the
whole file with a bespoke SVG for THIS app: a clean, simple, iconic picture of
what it is or shows (a stylized miniature of its main screen, or its core
object), derived from the code and the manifest. Mock data only, never the user's
real data. It must be inert and self-contained: no `<script>`, no `on*=`
attributes, no `<foreignObject>`, no `href`/`src` pointing outside the file. Keep
the root `viewBox` around 240x160. It is also the README's hero image.

## 6. Commit, check, report

Commit the modifications, manifest, README and thumbnail on your branch
(`mngr/<slug>`). Then run the gate, which must exit 0:

```bash
uv run --no-project --with 'pydantic>=2' python "$PT/validate_template.py" .
```

It checks everything at once -- the TOML schema, markdown/TOML agreement, no
FILL-IN block left, a real and inert thumbnail, every `env.d` unit shipping, every
apt package resolving -- so fix all it reports, commit, and re-run until it is
clean. `git status` must be clean too.

Then report, **by hand**: step 1 replaced `.agents/skills/launch-task/` with the
base's copy, which may predate the launcher's `report` subcommand.

```bash
mkdir -p data/.tasks/launch-task/<slug>/reports
# write the report (frontmatter + body, shapes in
# .agents/shared/references/worker-reporting.md) to
# data/.tasks/launch-task/<slug>/reports/report.md, then:
cp data/.tasks/launch-task/<slug>/reports/report.md "$LEAD_WORKTREE/$FINISH_REPORT_PATH"
```

Valid `name:` values: `question` (a gate, at any point), `done`, `stuck`. A
`done` body names your worktree's absolute path (`git rev-parse
--show-toplevel`) and the branch `mngr/<slug>`; the lead publishes from there. A
`stuck` body quotes the failing script's stderr verbatim.
