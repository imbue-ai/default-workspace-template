Theme contract 2 (`docs/system/blueprint/brand-design-system/`): the colors are one key per ramp, twelve computed steps per ramp, and roles over them, and the standard look's colors follow the workspace color.

- The color tokens are now keys (`--c-primary-key`, ...), ramp steps (`--c-neutral-1` to `--c-neutral-12`, ...), and roles (`--c-body-surface`, `--c-primary-surface`, `--c-border-focus`, ...). Hover and active are computed from the surface they shade and are no longer tokens; `--state-ratio`, `--state-flare`, and `--state-inactive` tune them, and `--state-ratio: 1` turns them off.
- The validator names the contract-2 token for a contract-1 one, and says to run `workspace-themes migrate <id>` for a contract-1 theme.
- `workspace-themes migrate [<id>...] [--all]` rewrites a contract-1 theme in `themes/` to contract 2 in place. A theme already on contract 2 is left as is.
- Every app that serves themes also serves `/_static/workspace_color.css`: the keys the workspace color stands for, worked out so white text reads on every key and the colors stay inside sRGB. Imbue's own color, or none, keeps the standard keys.
- The type tokens follow the brand's ramp: `--font-size-row` is `--font-size-body-dense`, and `--font-size-title-page`, `--font-size-title`, `--font-size-section`, and `--weight-medium` are new. `migrate` renames `--font-size-row` too.
- `--radius-2xl` is a contract token.
