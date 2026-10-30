/**
 * The chat page's connection to the shell that frames it, held apart from ``shell.ts`` (which connects it) so a view
 * that only reads it, such as a failure's "Report a bug", imports nothing else of the page's shell wiring.
 */

import type { ShellConnection } from "@imbue/workspace-ui/src/app_contract";

let connection: ShellConnection | null = null;

/** The page's connection, null before ``connectChatToShell`` made it. */
export function getShellConnection(): ShellConnection | null {
  return connection;
}

export function setShellConnection(connected: ShellConnection): void {
  connection = connected;
}
