// JSON client for the accounts surface's API, with one transparent
// browser-session refresh on 401 (the SuperTokens middleware serves the
// refresh route; cookies carry the tokens).

const REFRESH_PATH = "/accounts/auth/session/refresh";

// Injected by vite at build time (see vite.config.ts); "dev" outside a
// `minds env deploy` build.
declare const __MINDS_DEPLOY_ID__: string;

// Canonical client self-identification, recorded in the connector's access
// log (browsers own User-Agent, so a custom header carries it instead).
const CLIENT_ID_HEADER_VALUE = `web/${__MINDS_DEPLOY_ID__}`;

function clientIdHeaders(): Record<string, string> {
  return { "X-Imbue-Client": CLIENT_ID_HEADER_VALUE };
}

export interface AccountsConfig {
  turnstile_site_key: string;
  google_enabled: boolean;
}

export interface Identity {
  signed_in: boolean;
  user_id?: string;
  email?: string;
  email_verified?: boolean;
}

export interface AuthResult {
  status: string;
  message?: string | null;
  user?: { user_id: string; email: string } | null;
}

async function tryRefreshSession(): Promise<boolean> {
  try {
    const resp = await fetch(REFRESH_PATH, {
      method: "POST",
      credentials: "same-origin",
      headers: clientIdHeaders(),
    });
    return resp.ok;
  } catch {
    return false;
  }
}

async function requestOnce(path: string, init: RequestInit): Promise<Response> {
  return fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: { ...clientIdHeaders(), ...(init.headers ?? {}) },
  });
}

// Perform a request; on a 401, refresh the browser session once and retry.
// The retried 401 (or a failed refresh) falls through to the caller, which
// treats it as signed out.
async function request(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const first = await requestOnce(path, init);
  if (first.status !== 401) return first;
  const refreshed = await tryRefreshSession();
  if (!refreshed) return first;
  return requestOnce(path, init);
}

async function postJson(path: string, body: unknown): Promise<Response> {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function fetchConfig(): Promise<AccountsConfig> {
  const resp = await request("/accounts/api/config");
  if (!resp.ok) throw new Error(`config failed (${resp.status})`);
  return (await resp.json()) as AccountsConfig;
}

export async function fetchIdentity(): Promise<Identity> {
  const resp = await request("/accounts/api/me");
  if (resp.status === 401) return { signed_in: false };
  if (!resp.ok) throw new Error(`me failed (${resp.status})`);
  return (await resp.json()) as Identity;
}

export async function signIn(
  email: string,
  password: string,
): Promise<AuthResult> {
  const resp = await postJson("/accounts/api/signin", { email, password });
  return (await resp.json()) as AuthResult;
}

// Marketing-attribution context from the signup page itself: its query
// string (campaign params are extracted server-side), its path, and the
// next= target that classifies which surface sent the user here.
export interface SignupAttribution {
  page_query: string;
  page_path: string;
  next: string;
}

export async function signUp(
  email: string,
  password: string,
  turnstileToken: string,
  attribution: SignupAttribution,
  plan: string,
): Promise<AuthResult> {
  const resp = await postJson("/accounts/api/signup", {
    email,
    password,
    turnstile_token: turnstileToken,
    attribution_page_query: attribution.page_query,
    attribution_page_path: attribution.page_path,
    attribution_next: attribution.next,
    plan,
  });
  return (await resp.json()) as AuthResult;
}

export async function signOut(): Promise<void> {
  const resp = await postJson("/accounts/api/signout", {});
  if (!resp.ok) throw new Error(`signout failed (${resp.status})`);
}

export async function signOutAllDevices(): Promise<void> {
  const resp = await postJson("/accounts/api/signout-all", {});
  if (!resp.ok) throw new Error(`signout-all failed (${resp.status})`);
}

export async function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<{ status: string; message?: string }> {
  const resp = await postJson("/accounts/api/change-password", {
    current_password: currentPassword,
    new_password: newPassword,
  });
  return (await resp.json()) as { status: string; message?: string };
}

export async function sendVerificationEmail(): Promise<{
  sent: boolean;
  already_verified: boolean;
}> {
  const resp = await postJson("/accounts/api/send-verification", {});
  if (!resp.ok) throw new Error(`send-verification failed (${resp.status})`);
  return (await resp.json()) as { sent: boolean; already_verified: boolean };
}

export async function requestPasswordReset(email: string): Promise<void> {
  // The deprecated-path JSON endpoint remains the reset-request API for the
  // hosted page too (always answers OK to avoid account enumeration -- so a
  // non-OK response is a genuine transport/server failure).
  const resp = await postJson("/auth/password/forgot", { email });
  if (!resp.ok)
    throw new Error(`password reset request failed (${resp.status})`);
}

export async function resetPassword(
  token: string,
  newPassword: string,
): Promise<{ status: string; message?: string }> {
  const resp = await postJson("/auth/password/reset", {
    token,
    new_password: newPassword,
  });
  if (!resp.ok) throw new Error(`reset failed (${resp.status})`);
  return (await resp.json()) as { status: string; message?: string };
}

export async function verifyEmailToken(
  token: string,
  tenantId: string,
): Promise<{ status: string }> {
  const resp = await postJson("/accounts/api/verify-email", {
    token,
    tenant_id: tenantId,
  });
  if (!resp.ok) throw new Error(`verify failed (${resp.status})`);
  return (await resp.json()) as { status: string };
}
