# style_guide (the app)

The Style Guide: one page cataloguing the workspace's design system, the shared
library every frontend draws with (`system/libs/workspace_ui`). It is the
workspace's counterpart of Imbue Studio's own styleguide page: a sticky
contents list beside the catalog, in three groups.

- **Foundations**: every color token, in one table of swatch, name (its utility
  and `--c-*` value) and value, grouped as `base.css` groups them; the type
  roles and font stacks, spacing, corner radius, elevation, motion, the
  stacking layers and widths, and the whole icon set.
- **Components**: the Button in every variant, size and state, the text
  fields, the Dropdown and the SegmentedControl, the Menu (with a submenu,
  check rows, a disabled and a danger row), hover tooltips, badges, banners,
  activity dots and spinners, the Modal, the notice and delete-confirmation
  dialogs (a notice with its details), toasts (plain, and lasting with their
  details and buttons), and the surfaces stacked as the apps stack them.
- **Theming**: the theme the page wears, and every part of the theme contract
  (`docs/system/blueprint/workspace-themes/`, section 4.2) with the section
  that shows it, or what draws it when the catalog does not; and a framed
  plain-HTML page (`/page-kit-sample`) wearing the page kit, the shell-built
  `/_static/workspace_theme.css` and `.js` the app serves from its own origin.

Nothing here is a copy. The colors and the scales are read out of the
library's `base.css` at build time (`frontend/src/models/tokens.ts`), so a new
token appears with no edit to this app; the components are the library's own,
so a change to one shows here on the next build. The type roles are the one
hand-kept list, because each is a utility and the Tailwind scanner only emits
a utility it reads as a literal.

The app is themeable in `tokens` mode: it wears the theme the shell names, from
its own origin (`/_static/themes/`), so under a theme every swatch and control
is that theme's, and each value caption shows what the page wears now, with
the standard value struck through beside any value the theme changed. Its
icon under Classic Mac and Windows 2000 is in each theme's `icons/style-guide.png`.

The page is a build of `frontend/`, one of the npm workspace's frontends
(`system/package.json`), into `src/style_guide_app/static/`; the Flask server
in `src/style_guide_app/server.py` serves it at `/`, its bundle under
`/assets/`, and `/api/health`, on port 8060. It stores nothing.

It does not start at boot, and is not listed until it has been started once
by hand: `supervisorctl start style-guide` registers it and starts it. From
then on it is in the launcher, the shell holds its port and starts it on the
next request for its page, and stops it a minute after its last window closes
(`stop_when_no_windows`).
