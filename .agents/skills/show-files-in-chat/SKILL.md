---
name: show-files-in-chat
description: Show a file to the user in chat. Display an image inline (chart, plot, screenshot, diagram, rendered figure, photo), or link any file or folder (PDF, CSV, log, zip, spreadsheet, a project folder, ...) so a click opens it in the File Viewer. Use whenever you have a file on disk you want the user to see or download, or want to embed an image from a public URL.
metadata:
  author: imbue
---

# Showing a file in chat

Your chat replies are rendered as markdown, so you can put a file in front of the
user directly -- there is no upload step and no external hosting. The chat serves
a file at its absolute on-disk path, so the path you write in the markdown
doubles as the URL the user's browser fetches for an image, and names the file a
link opens. Images render inline; a link to any other file or folder opens it in
the File Viewer, inside the workspace.

## Where to put the file

A file you show or share lives in a sensible, visible home under `data/` --
there is no special chat directory. Put it where it topically belongs: the
relevant project folder if one exists (e.g. `data/my-project/`), else
`data/documents/` for files and `data/images/` for standalone images (create
either with `mkdir -p` if it does not exist). Everything under `data/` is
gitignored and persists via the restic host backup.

## Show an image inline

1. Write the image to its home (see above), giving it a unique, descriptive
   filename, e.g. `revenue-by-quarter-2026.png`. Served image URLs are cached
   immutably, so reusing a filename would leave the user looking at the stale
   image.

2. Reference it by its **absolute** on-disk path with markdown image syntax:

   ```
   ![Revenue by quarter](/home/user/workspace/data/images/revenue-by-quarter-2026.png)
   ```

   The path must be absolute (start with `/`). A relative path such as
   `![x](data/images/x.png)` will not render.

Supported inline image formats: `.png`, `.jpg` / `.jpeg`, `.gif`, `.webp`, `.avif`, `.bmp`, `.ico`, `.svg`.

## Link a file or folder

For anything that is not an image -- a PDF, CSV, log, zip, spreadsheet, etc. --
write the file to its home (see "Where to put the file" above; any path works)
and reference its **absolute** path with an ordinary markdown link (not image
syntax):

```
[Q4 report (PDF)](/home/user/workspace/data/documents/q4-report.pdf)
```

Clicking the link opens the file in a File Viewer window on the user's desktop
(or raises the one already showing it): text as read-only source, PDFs, images,
audio and video shown, anything else offered as a download there. A link to a
folder opens that folder's listing the same way:

```
[the project folder](/home/user/workspace/data/my-project)
```

A `file:///` URL (`[the plan](file:///home/user/workspace/data/plan.md)`) is the
same link. Use a clear label that says what the file is.

## Embed an image from a public URL

If the image already lives at a public URL, embed that URL directly -- no local
file needed:

```
![alt text](https://example.com/image.png)
```

Use the public-URL form for images you are referencing from the web, and the
local absolute-path form for files you produced on this machine.

## Notes

- Both images and file links require an **absolute** path (starting with `/`)
  that points at a file that actually exists on disk; a link to a missing path
  opens the File Viewer's "not found" page.
- If an image shows a broken-image icon, the usual cause is a relative or
  mistyped path, or an extension that is not one of the inline image formats
  above (a non-image extension is treated as a download, not an inline image).
- For a local web app or service, link its localhost URL instead
  (`[the preview](http://localhost:3000/)`). Clicking it opens the app's own
  window when it is an app registered with the workspace (the port it
  registered in `data/.state/apps.toml`), and otherwise opens it in the
  workspace's browser.
