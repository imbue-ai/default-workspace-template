// The in-browser DEK store: unlock-at-sign-in state for the account's
// data-encryption key. The unwrapped DEK lives in sessionStorage by default
// (prompt returns in a new tab/session); "remember password" persists it in
// IndexedDB instead (never the password itself). The server only ever sees
// the wrapped bundle.

import {
  type KeyBundle,
  base64ToBytes,
  bytesToBase64,
  generateDek,
  unwrapBundle,
  wrapDekToBundle,
} from "./crypto/secretbox";
import {
  deleteKeyBundle,
  fetchKeyBundle,
  putKeyBundle,
  putKeyBundleIfAbsent,
  scrubSyncedSecrets,
} from "./api";
import { openStore } from "./idb";

const SESSION_KEY = "minds-web-dek";
const IDB_STORE = "dek";
const IDB_KEY = "dek";

let cachedDek: Uint8Array | null = null;

export function currentDek(): Uint8Array | null {
  if (cachedDek !== null) return cachedDek;
  const stored = sessionStorage.getItem(SESSION_KEY);
  if (stored !== null) {
    cachedDek = base64ToBytes(stored);
    return cachedDek;
  }
  return null;
}

export async function loadRememberedDek(): Promise<Uint8Array | null> {
  if (currentDek() !== null) return currentDek();
  const store = await openStore(IDB_STORE);
  const remembered = await store.get<string>(IDB_KEY);
  if (remembered === undefined) return null;
  cachedDek = base64ToBytes(remembered);
  sessionStorage.setItem(SESSION_KEY, remembered);
  return cachedDek;
}

async function storeDek(dek: Uint8Array, remember: boolean): Promise<void> {
  cachedDek = dek;
  const encoded = bytesToBase64(dek);
  sessionStorage.setItem(SESSION_KEY, encoded);
  const store = await openStore(IDB_STORE);
  if (remember) {
    await store.put(IDB_KEY, encoded);
  } else {
    await store.delete(IDB_KEY);
  }
}

export async function forgetDek(): Promise<void> {
  cachedDek = null;
  sessionStorage.removeItem(SESSION_KEY);
  const store = await openStore(IDB_STORE);
  await store.delete(IDB_KEY);
}

export type UnlockOutcome = "unlocked" | "no_bundle";

// Unlock a returning account: fetch the bundle and unwrap it with the
// password. Throws WrongPasswordOrCorruptDataError on a bad password.
export async function unlockWithPassword(
  password: string,
  remember: boolean,
): Promise<UnlockOutcome> {
  const bundle = await fetchKeyBundle();
  if (bundle === null) return "no_bundle";
  const dek = await unwrapBundle(bundle, password);
  await storeDek(dek, remember);
  return "unlocked";
}

// First-time setup: mint the account's first DEK and push its bundle. The
// push is create-only (exactly one of two racing tabs/devices wins); on
// KeyBundleExistsError the freshly minted DEK is discarded, never stored --
// keeping it would let this tab encrypt secrets no stored bundle can ever
// recover. The caller falls back to unlocking with the winner's password.
export async function setInitialPassword(
  password: string,
  remember: boolean,
): Promise<void> {
  const dek = generateDek();
  const bundle = await wrapDekToBundle(dek, password, 1);
  await putKeyBundleIfAbsent(bundle);
  await storeDek(dek, remember);
}

// Change the master password: re-wrap the already-unlocked DEK and push the
// new bundle (same key epoch family; the secrets themselves are untouched).
export async function changePassword(
  currentBundle: KeyBundle,
  oldPassword: string,
  newPassword: string,
  remember: boolean,
): Promise<void> {
  const dek = await unwrapBundle(currentBundle, oldPassword);
  const bundle = await wrapDekToBundle(
    dek,
    newPassword,
    currentBundle.key_epoch,
  );
  await putKeyBundle(bundle);
  await storeDek(dek, remember);
}

// Clear the master password entirely: delete the bundle locally and
// server-side and scrub every synced secrets blob (they are unreadable
// without the DEK anyway; scrubbing makes that state explicit).
export async function clearPasswordAndSecrets(): Promise<void> {
  await deleteKeyBundle();
  await scrubSyncedSecrets();
  await forgetDek();
}
