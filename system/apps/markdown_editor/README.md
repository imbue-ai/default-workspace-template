# markdown_editor (the app)

The Markdown Editor: one window for reading and reviewing the markdown files
agents write (plans, notes, reports). It shows the file formatted (headings,
lists, tables, task lists, code), and opens in **Suggest** mode: what the person
types and deletes becomes tracked suggestions, and they can comment on any
selection. **Edit** mode changes the file directly; **Read** mode changes
nothing. A suggestion is accepted or rejected by clicking it, or all of them at
once from the toolbar.

Suggestions and comments are saved inside the file as
[CriticMarkup](https://criticmarkup.com), so the agent that reads the file
next sees them in place, with no app or side file to know about:

```
Ship it on {==Friday==}{>>Sam: Monday? Friday is a holiday<<}. We {--might --}{++will ++}cut the old importer.
```

## Opening a file

- From the launcher: Markdown Editor, optionally with a File path.
- By link: `/?path=/home/user/workspace/notes/plan.md` on the app's origin
  (`&mode=edit` or `&mode=read` overrides the saved mode), which an agent can
  hand a person for any plan it writes.
- From the start page (no `?path=`): type a path, or pick one of the recently
  opened files or the workspace's markdown files, newest first.

## Saving

Saving is automatic, a second after the last keystroke. The file is split into
top-level blocks with the editor's own `marked` lexer, and every block the
person did not touch is written back byte for byte, so the editor never
reformats an agent's file; only edited blocks go through the serializer, and
opening and closing a file writes nothing. Blocks the editor cannot represent
(raw HTML, link reference definitions) are shown and kept as source, and YAML
front matter is kept aside untouched.

Every save names the version it started from, and the server refuses it when
the file changed since (an agent rewrote the plan); the page then offers to
reload or keep its own. While the page has nothing unsaved it polls the file
and shows a new version as soon as an agent writes one. Before each save the
server copies the version it replaces to
`data/.apps/markdown-editor/history/<name>-<hash>/`, the 30 newest per file,
so a mistake costs at most one save.

## Files it may open

Any file under `/home/user` (as Files does), never one in a `.secrets` folder.
Only `.md`, `.markdown`, `.mdown`, `.mkd` and `.txt` files are written; any
other text file opens read-only. Files over 5 MB and binary files are refused.

## How it is built

The page is a build of `frontend/`, one of the npm workspace's frontends
(`system/package.json`), into `src/markdown_editor_app/static/`: plain
TypeScript over Tiptap 3 and its official markdown extension, with the open
`@handlewithcare/prosemirror-suggest-changes` plugin for tracked changes.
CriticMarkup is read by a custom tokenizer and written by the marks' own
markdown renderers.

| Part | Where |
| --- | --- |
| Server, routes | `src/markdown_editor_app/server.py` |
| File access, history, the file list | `src/markdown_editor_app/files.py` |
| Editor setup, Suggest mode | `frontend/src/editor.ts` |
| CriticMarkup marks and tokenizer | `frontend/src/critic.ts` |
| Saving without reformatting | `frontend/src/blocks.ts`, `frontend/src/document.ts` |
| The page | `frontend/index.html`, `frontend/src/main.ts`, `frontend/src/styles.css` |

The Flask server serves the page at `/`, its bundle under `/assets/`, the API
under `/api/` (typed through `app_schema`, published at `/api/schema`) and
`/api/health`, on port 8070. The page has a look of its own, so only its
window wears the workspace's theme (`[theming] mode = "none"`).

`frontend/src/corpus.test.ts` checks that every file listed in `$MD_CORPUS`
(one path per line) opens and saves back byte for byte untouched; it is
skipped when the variable is unset.
