/**
 * Per-service origin derivation. Every workspace service owns a full browser
 * origin (no path-prefix proxying), and that origin is a pure function of the
 * workspace's host COORDINATE by ONE rule: prefix the service's unguessable
 * ``<name>-<rand>`` origin LABEL (minted per service in
 * ``system/scripts/forward_port.py``) as a single hostname label onto the
 * coordinate. A service registered as ``foo`` (label ``foo-x7k9q2w1``) lives at:
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
 * app's NAME to its LABEL via ``labelForApp`` (models/Inventory) before
 * calling this.
 *
 * The base is the workspace host COORDINATE -- the ``host-<hex>`` label and
 * everything after it -- NOT ``window.location.host`` verbatim. The shell does
 * not run at the bare coordinate: locally the forwarder redirects the bare
 * origin to the shell's own label origin, and on a share only ``*.<domain>``
 * is served, so the shell always runs at ``<shell-label>.<coordinate>``.
 * Deriving relative to ``window.location.host`` verbatim would therefore nest
 * every service under the shell's label (``foo.<shell-label>.host-<hex>...``),
 * which routes back to the shell -- a dockview inside a dockview. Stripping to
 * the coordinate first keeps every service origin a single label deep.
 *
 * Nothing about an origin is ever persisted: saved layouts carry the
 * ``serviceName`` and the URL is re-derived from that name's CURRENT label at
 * render time, so a layout stays portable across hosts and shares.
 */

/** A label that starts a workspace coordinate: ``host-<hex>`` (``agent-`` is
 *  the legacy spelling of the same coordinate), or a bare 32-hex label -- the
 *  share label leading a workspace-keyed share domain
 *  (``<share-label>.<user-hash>.<region>.<domain>``). Service labels can never
 *  match either form: ``host-``/``agent-`` prefixes are reserved in
 *  forward_port.py, and a minted label is always ``<name>-<rand>`` (the hyphen
 *  plus non-hex name keeps it out of the bare 32-hex shape). */
const WORKSPACE_COORDINATE_LABEL = /^(?:(?:host|agent)-[a-f0-9]+|[a-f0-9]{32})$/i;

/** The workspace coordinate within ``host``: the first coordinate label and
 *  everything after it (``host-<hex>.localhost:8421`` locally,
 *  ``host-<hex>.<user>.<region>.<domain>`` on a legacy share,
 *  ``<share-label>.<user-hash>.<region>.<domain>`` on a workspace-keyed
 *  share), with any leading service label(s) stripped. Returns ``host``
 *  unchanged when it carries no coordinate label (a non-workspace host), so
 *  the derivation degrades safely. */
export function workspaceHostCoordinate(host: string): string {
  const labels = host.split(".");
  const coordinateIndex = labels.findIndex((label) => WORKSPACE_COORDINATE_LABEL.test(label));
  return coordinateIndex < 0 ? host : labels.slice(coordinateIndex).join(".");
}

/** Derive the origin URL (with trailing slash) whose first hostname label is
 *  ``hostLabel`` (a service's ``<name>-<rand>`` origin label). ``host`` and
 *  ``protocol`` default to the shell's own ``window.location`` but are
 *  parameters so the derivation is unit-testable without a DOM. The service
 *  label is prefixed onto the workspace COORDINATE (``host`` minus any leading
 *  service label), never onto ``host`` verbatim. */
export function deriveServiceOrigin(
  hostLabel: string,
  host: string = window.location.host,
  protocol: string = window.location.protocol,
): string {
  return `${protocol}//${hostLabel}.${workspaceHostCoordinate(host)}/`;
}
