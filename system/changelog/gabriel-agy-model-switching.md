- Bump the Antigravity CLI (`agy`) baked into the image from 1.1.22 to 1.4.3. The installer is now `system/scripts/agy_install-1.4.3.sh`, pinned to the 1.4.3 release URLs and sha512 checksums from Google's release manifest. A fresh agent now starts on Gemini 3.8 Flash (High) by default; there is still no model pin of our own.

- Pin mngr to the branch that records agy's reasoning effort in `model_state.json` and confirms agy slash commands as soon as agy handles them, which the chat app's Antigravity model picker relies on.
