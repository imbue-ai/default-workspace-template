/** Dropping a file onto the viewer (docs/system/specs/screen/spec.md section 4.3): neko's upload route takes the
 *  files and the pointer position and drops them on the Screen there (the host's session alone may). */

import type { Point } from "./view_transform";

export const UPLOAD_MAX_BYTES = 32 * 1024 * 1024;

export async function uploadDroppedFiles(files: File[], at: Point, token: string): Promise<boolean> {
  if (files.length === 0) return false;
  const form = new FormData();
  form.set("x", String(at.x));
  form.set("y", String(at.y));
  for (const file of files) form.append("files", file, file.name);
  const response = await fetch("/neko/api/room/upload/drop", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: form,
  });
  return response.ok;
}
