/**
 * Per-service origin derivation. Every workspace service owns a full browser
 * origin (no path-prefix proxying), and that origin is a pure function of the
 * shell's own host by ONE rule: prefix the service name as a hostname label.
 * The shell always runs at the bare workspace origin, so service ``foo``
 * lives at:
 *
 * - locally: ``http://foo.host-<32hex>.localhost:8421/`` (the shell is at
 *   ``http://host-<32hex>.localhost:8421/``)
 * - on shared hostnames (future work, same rule with a longer base):
 *   ``https://foo.host-<hex>.<user>.<region>.<domain>/``
 *
 * Because hostnames nest, no special-casing is needed anywhere: the forwarder
 * routes the bare workspace host to the shell, ``<service>.<workspace-host>``
 * to that service, and any deeper labels (``x.<service>.<workspace-host>``)
 * to the same service as its own sub-origin space.
 *
 * Nothing about an origin is ever persisted: saved layouts carry the
 * ``serviceName`` and the URL is re-derived at render time, so a layout stays
 * portable across hosts and shares.
 */

/** Derive the origin URL (with trailing slash) that serves ``serviceName``.
 *  ``host`` and ``protocol`` default to the shell's own ``window.location``
 *  but are parameters so the derivation is unit-testable without a DOM. */
export function deriveServiceOrigin(
  serviceName: string,
  host: string = window.location.host,
  protocol: string = window.location.protocol,
): string {
  return `${protocol}//${serviceName}.${host}/`;
}
