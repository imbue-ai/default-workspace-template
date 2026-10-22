New `import-chats` skill: bring the user's Claude and ChatGPT conversations into the workspace with datalib.

- The agent connects each account through the usual permission request, with desktop proxying on (both sites refuse datacenter addresses): Claude through the built-in `claude-ai` connection, ChatGPT through a custom connection that signs in at chatgpt.com and keeps the access token its page fetches (the same sign-in the datalib app uses). After the user approves and signs in, everything else happens without them.

- A script does the import itself, in the background. It installs datalib through the datalib skill's installer, leaving the Datalib app out of the launcher (datalib is still unannounced), and adds the Claude and ChatGPT sources to the datalib skill's store, keeping whatever else is configured there. A first import only downloads: each source's render and keyword-index steps are added turned off, which saves most of a first sync's time and memory, and the skill turns them on only when the user asks for search. The script follows datalib's live event stream and records each source's progress (conversations downloaded, and fetched of those to fetch) for the Getting Started card. A download that datalib stops partway (ChatGPT's rate limit, say) counts as imported with what it got, and the next update fetches the rest.

- After each sync the script rewrites one index per source from the downloaded records (`data/.skills/import-chats/claude-chats.md`, `chatgpt-chats.md`): every conversation by title, most recently updated first and grouped by month, Claude projects in their own section, each linked to the original and, once search is on, to its page in the workspace. The skill shows the agent how to find and read conversations straight from the downloaded records.

- The datalib skill's installer takes `--without-app`, which installs the binaries, guide and runtime but adds no Datalib app program; the chat import uses it.

- `find-transcripts` now points at `import-chats` for Claude and ChatGPT chats instead of saying they cannot be reached.
