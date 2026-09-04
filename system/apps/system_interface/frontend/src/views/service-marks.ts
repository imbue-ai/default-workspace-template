/**
 * The bundled brand mark for a permission scope's service.
 *
 * The artwork is minds' own service-icon set, fetched at build time from the mngr
 * commit pyproject.toml pins rather than copied into this repo a second time
 * (system/scripts/fetch_mngr_assets.sh, run by this package's prebuild), and the
 * same fetch already supplies this frontend with the embed contract (see the
 * alias in vite.config.ts). Globbing it means the two surfaces cannot drift, and
 * the marks become ordinary build assets -- so the card needs no network round
 * trip and looks the same embedded in the minds app and opened directly in a
 * browser.
 *
 * A scope is a latchkey service name plus a transport suffix (`slack-api`,
 * `google-gmail-api`, `github-rest-api`), and the mark files are keyed by that
 * same service name, so the service is one of the scope's hyphen-prefixes.
 * Trying the longest first is the rule the backend's own scope resolver uses
 * (`candidate_services` in latchkey_endpoints.py); it is what separates
 * `github-rest-api` -> `github` from `github-rest`.
 */

// The `-on-dark` files are vendor-published white variants for minds' dark
// theme. This UI has one (light) theme, so they are excluded by the glob rather
// than filtered afterwards -- a matched file is imported, and so emitted into
// the build, whether or not the map ends up keeping it.
//
// `no-inline` keeps each mark an emitted file instead of a data URI folded into
// the bundle: only the one mark a card actually shows is ever fetched.
const MARK_MODULES = import.meta.glob<string>(
  [
    "../../../../../vendor/mngr-assets/apps/minds/imbue/minds/desktop_client/static/service_icons/*.svg",
    "!**/*-on-dark.svg",
  ],
  { eager: true, query: "?url&no-inline", import: "default" },
);

const MARK_URL_BY_SERVICE: Record<string, string> = Object.fromEntries(
  Object.entries(MARK_MODULES).map(([filePath, url]): [string, string] => [
    filePath.slice(filePath.lastIndexOf("/") + 1, -".svg".length),
    url,
  ]),
);

/** How many marks the build found. Zero means the glob stopped matching (a
 *  moved vendor tree), which would otherwise surface only as every card
 *  quietly showing the cube. */
export const BUNDLED_MARK_COUNT = Object.keys(MARK_URL_BY_SERVICE).length;

/** The brand mark URL for `scope`'s service, or null when none is bundled. */
export function serviceMarkUrl(scope: string): string | null {
  const parts = scope.split("-");
  for (let count = parts.length; count > 0; count--) {
    const url = MARK_URL_BY_SERVICE[parts.slice(0, count).join("-")];
    if (url !== undefined) return url;
  }
  return null;
}
