/**
 * The chat app's registered name and its create action, which the shell still knows by name.
 *
 * CLEANUP (phase 10 of the workspace app model): the shell special-cases the chat's ``new`` for
 * the provider picker's ``account_id`` and the sign-in chooser; both go when the chat app runs
 * as its own process and the shell stops naming any app.
 */

export const CHAT_APP_NAME = "chat";
export const CHAT_NEW_ACTION = "new";
