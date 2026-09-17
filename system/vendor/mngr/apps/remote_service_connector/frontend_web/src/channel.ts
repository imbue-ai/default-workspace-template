// The web user's release channel: which `<channel>-web.json` pin the connector
// leases a template tag from when this browser creates a workspace. A
// per-browser preference held in a cookie on the chrome origin and sent on the
// claim body (the connector never reads the cookie itself). Default stable;
// anything unknown reads as stable, the same rule the connector applies.

export const WEB_CHANNELS = ["stable", "beta", "alpha"] as const;
export type WebChannel = (typeof WEB_CHANNELS)[number];
export const DEFAULT_WEB_CHANNEL: WebChannel = "stable";

const CHANNEL_COOKIE_NAME = "minds_web_channel";
const CHANNEL_COOKIE_MAX_AGE_SECONDS = 365 * 24 * 60 * 60;

export function normalizeWebChannel(
  value: string | null | undefined,
): WebChannel {
  const candidate = (value ?? "").trim().toLowerCase();
  return (WEB_CHANNELS as readonly string[]).includes(candidate)
    ? (candidate as WebChannel)
    : DEFAULT_WEB_CHANNEL;
}

// A hand-edited cookie can hold a malformed percent sequence, which
// decodeURIComponent refuses with a URIError; such a value is just another
// unknown channel, so hand it on undecoded for normalizeWebChannel to reject.
function decodeCookieValue(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch (error) {
    if (error instanceof URIError) return value;
    throw error;
  }
}

export function parseWebChannelCookie(cookieHeader: string): WebChannel {
  for (const pair of cookieHeader.split(";")) {
    const separatorIndex = pair.indexOf("=");
    if (separatorIndex === -1) continue;
    if (pair.slice(0, separatorIndex).trim() !== CHANNEL_COOKIE_NAME) continue;
    return normalizeWebChannel(
      decodeCookieValue(pair.slice(separatorIndex + 1).trim()),
    );
  }
  return DEFAULT_WEB_CHANNEL;
}

export function readWebChannel(): WebChannel {
  return parseWebChannelCookie(document.cookie);
}

export function writeWebChannel(channel: WebChannel): void {
  document.cookie =
    `${CHANNEL_COOKIE_NAME}=${encodeURIComponent(channel)}; Path=/; ` +
    `Max-Age=${CHANNEL_COOKIE_MAX_AGE_SECONDS}; SameSite=Lax; Secure`;
}
