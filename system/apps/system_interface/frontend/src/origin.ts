/**
 * Per-service origin derivation. Every workspace service owns a full browser
 * origin (no path-prefix proxying), and that origin is a pure function of the
 * shell's own host:
 *
 * - Local workspace hosts (``agent-<hex>.localhost:8421``) nest the service
 *   as a subdomain label: service ``foo`` lives at
 *   ``http://foo.agent-<hex>.localhost:8421/``.
 * - Shared hosts (``system-interface--<host>--<user>.<domain>``) swap the
 *   text before the FIRST ``--`` token: ``foo`` lives at
 *   ``https://foo--<host>--<user>.<domain>/``.
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
  // Shared (Cloudflare) hosts are ``<name>--<host>--<user>.<domain>``: the
  // sibling service's hostname swaps the text before the first ``--``.
  if (host.includes("--")) {
    return `${protocol}//${serviceName}${host.substring(host.indexOf("--"))}/`;
  }
  // Local workspace hosts (``agent-<hex>.localhost[:port]``) nest the
  // service as a subdomain label.
  return `${protocol}//${serviceName}.${host}/`;
}
