- `classifyLink` now calls a custom-scheme link (`slack:`, `vscode:`: any scheme a browser does not load itself) external, and `externalLinkMessageType` names its message `open:<scheme>` (`open:slack`). A file and line or a host and port written without a scheme (`q4.md:12`, `localhost:3000/app`), which parses as a custom-scheme URL, is unroutable, as is any custom-scheme link of that shape (`sms:5551234`; `sms:+15551234` stays a link). The app contract's new `isCustomSchemeUrl` is held to the Imbue Studio desktop app's rule by the shared link vectors.

- A framed page's click on a custom-scheme link now goes to the shell (`shell:open-link`) like a `mailto:` link, instead of being left to the browser.

- `announceReadyToEmbedder` takes `opensCustomSchemeLinks` and sends it with `minds:workspace-ready`.
