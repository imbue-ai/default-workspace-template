/**
 * Per-service origin derivation. Every workspace service owns a full browser
 * origin (no path-prefix proxying), and that origin is a pure function of the
 * shell's own host by ONE rule: prefix the service's unguessable ``<name>-<rand>``
 * origin LABEL (minted per service in ``system/scripts/forward_port.py``) as a
 * hostname label. The shell always runs at the bare workspace origin, so a
 * service registered as ``foo`` (label ``foo-x7k9q2w1``) lives at:
 *
 * - locally: ``http://foo-x7k9q2w1.host-<32hex>.localhost:8421/`` (the shell is
 *   at ``http://host-<32hex>.localhost:8421/``)
 * - on shared hostnames (same rule with a longer base):
 *   ``https://foo-x7k9q2w1.host-<hex>.<user>.<region>.<domain>/``
 *
 * The random suffix is the one hostname component that never leaks via CT, so
 * a share cannot be enumerated from the public cert name. Callers resolve a
 * service NAME to its LABEL via ``labelForService`` (AgentManager) before
 * calling this; ``deriveServiceOrigin`` itself is a pure function of whatever
 * hostname label it is handed.
 *
 * Because hostnames nest, no special-casing is needed anywhere: the forwarder
 * routes the bare workspace host to the shell, ``<label>.<workspace-host>``
 * to that service, and any deeper labels (``x.<label>.<workspace-host>``)
 * to the same service as its own sub-origin space.
 *
 * Nothing about an origin is ever persisted: saved layouts carry the
 * ``serviceName`` and the URL is re-derived from that name's CURRENT label at
 * render time, so a layout stays portable across hosts and shares.
 */

/** Derive the origin URL (with trailing slash) whose first hostname label is
 *  ``hostLabel`` (a service's ``<name>-<rand>`` origin label). ``host`` and
 *  ``protocol`` default to the shell's own ``window.location`` but are
 *  parameters so the derivation is unit-testable without a DOM. */
export function deriveServiceOrigin(
  hostLabel: string,
  host: string = window.location.host,
  protocol: string = window.location.protocol,
): string {
  return `${protocol}//${hostLabel}.${host}/`;
}
