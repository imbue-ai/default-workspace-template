A window's page is told whether the Imbue Studio app around the workspace opens its own file picker to share something from your computer.

- The shell keeps what Imbue Studio says with `minds:embedder-capabilities` about `opensLocalFiles` and puts it in every window's `shell:handshake`, so a page opened after Studio spoke still knows. Files shows its "Connect a file or folder…" entry only when it is true.
