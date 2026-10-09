/**
 * Per-app origin derivation. Every workspace app owns a full browser
 * origin (no path-prefix proxying), and that origin is a pure function of the
 * workspace's host COORDINATE by ONE rule: prefix the app's unguessable
 * ``<name>-<rand>`` origin LABEL (minted per app in
 * ``system/scripts/forward_port.py``) as a single hostname label onto the
 * coordinate. An app registered as ``foo`` (label ``foo-x7k9q2w1``) lives at:
 *
 * - locally: ``http://foo-x7k9q2w1.host-<32hex>.localhost:8421/``
 * - on legacy shared hostnames (same rule, longer coordinate):
 *   ``https://foo-x7k9q2w1.host-<hex>.<user>.<region>.<domain>/``
 * - on workspace-keyed shared hostnames (the connector's current share-domain
 *   shape, whose coordinate leads with a bare 32-hex share label instead of a
 *   ``host-<hex>`` machine label):
 *   ``https://foo-x7k9q2w1.<share-label>.<user-hash>.<region>.<domain>/``
 *
 * The random suffix is the one hostname component that never leaks via CT, so
 * a share cannot be enumerated from the public cert name. Callers resolve an
 * app's NAME to its LABEL via ``labelForApp`` (the shell's model/pageUrl)
 * before calling this.
 *
 * The base is the workspace host COORDINATE -- the ``host-<hex>`` label and
 * everything after it -- NOT ``window.location.host`` verbatim. The shell does
 * not run at the bare coordinate: locally the forwarder redirects the bare
 * origin to the shell's own label origin, and on a share only ``*.<domain>``
 * is served, so the shell always runs at ``<shell-label>.<coordinate>``.
 * Deriving relative to ``window.location.host`` verbatim would therefore nest
 * every app under the shell's label (``foo.<shell-label>.host-<hex>...``),
 * which routes back to the shell -- a desktop inside a desktop. Stripping to
 * the coordinate first keeps every app origin a single label deep.
 *
 * Nothing about an origin is ever persisted: a desktop's windows carry the app's
 * name and a path, and the URL is re-derived from that name's CURRENT label at
 * render time, so a desktop stays portable across hosts and shares.
 */

import { workspaceHostCoordinate } from "./app_contract";

export { hasWorkspaceCoordinate, workspaceHostCoordinate } from "./app_contract";

/** Derive the origin URL (with trailing slash) whose first hostname label is
 *  ``hostLabel`` (an app's ``<name>-<rand>`` origin label). ``host`` and
 *  ``protocol`` default to the shell's own ``window.location`` but are
 *  parameters so the derivation is unit-testable without a DOM. The app
 *  label is prefixed onto the workspace COORDINATE (``host`` minus any leading
 *  app label), never onto ``host`` verbatim. */
export function deriveAppOrigin(
  hostLabel: string,
  host: string = window.location.host,
  protocol: string = window.location.protocol,
): string {
  return `${protocol}//${hostLabel}.${workspaceHostCoordinate(host)}/`;
}
