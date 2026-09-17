# Vendored from harbor

This directory is a verbatim copy of `apps/viewer` from the harbor repository. It is the
frontend that `just minds-evals-view` builds and serves, so that eval results can be rendered
with annotations the stock viewer knows nothing about.

| | |
|---|---|
| Upstream | https://github.com/harbor-framework/harbor |
| Tag | `v0.22.0` |
| Commit | `4407eb5227a2ff4f0d3f16b2eb48849382fdf276` |
| Path | `apps/viewer` |
| License | Apache-2.0 (see the upstream `LICENSE`) |

## The version must match the pinned backend

`just minds-evals-view` serves this build against harbor's own API, so the two halves have to
agree. The tag above must equal the harbor tag in `apps/minds_evals/pyproject.toml`
(`[tool.uv.sources]`): a newer frontend calls endpoints an older backend does not serve -- the
viewer's interaction tab, for one, went in with 0.22 and has nothing behind it on 0.21.
**Bumping harbor means re-vendoring this tree at the same tag.**

## What the copy drops

Upstream's `CLAUDE.md` is pruned on the way in (`PRUNE` in `vendor_viewer.sh`). It describes
harbor's own repository, and Claude Code reads every `CLAUDE.md` beneath a checkout as project
instructions, so left here it would point agents at `harbor view --dev` and `--build`. In this
repository the viewer is built with `apps/minds_evals/scripts/build_viewer.sh` and served with
`just minds-evals-view`.

## Re-vendoring

`apps/minds_evals/scripts/vendor_viewer.sh <tag>` replaces this tree with a fresh extraction and
rewrites the tag and commit above. Run it, then re-apply whatever local changes the diff below
reports as lost.

## Our local changes

Everything here except this file starts out identical to upstream, so the delta is whatever a
diff against a fresh extraction reports:

```
scripts/vendor_viewer.sh --diff v0.22.0
```

Keep that delta small and keep upstream's layout: same paths, same component structure. Prefer
adding files over editing them where the choice exists, since added files never conflict. Those
habits are what keep the option open of pushing this tree back into a real harbor fork later --
at that point it is a directory copy plus one line in `[tool.uv.sources]`, rather than a merge.
