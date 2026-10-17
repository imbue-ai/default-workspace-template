/** The words and numbers the app shows for sizes, counts, and times. */

const SIZE_UNITS = ["KB", "MB", "GB", "TB"];

/** A byte count the way Finder writes one: decimal units, one decimal place under ten. */
export function formatSize(bytes: number): string {
  if (bytes < 1000) return bytes === 1 ? "1 byte" : `${bytes} bytes`;
  let value = bytes;
  let unit = "";
  for (const next of SIZE_UNITS) {
    value /= 1000;
    unit = next;
    if (value < 1000) break;
  }
  const digits = value < 10 ? 1 : 0;
  return `${value.toFixed(digits)} ${unit}`;
}

export function formatItemCount(count: number): string {
  return count === 1 ? "1 item" : `${count} items`;
}

function startOfDay(time: Date): number {
  return new Date(time.getFullYear(), time.getMonth(), time.getDate()).getTime();
}

/** When something changed: "Today at 2:03 PM", "Yesterday at ...", or the date and time. */
export function formatModified(mtime: number, now: Date = new Date(), locale?: string): string {
  const when = new Date(mtime);
  const clock = when.toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" });
  const dayDelta = Math.round((startOfDay(now) - startOfDay(when)) / 86_400_000);
  if (dayDelta === 0) return `Today at ${clock}`;
  if (dayDelta === 1) return `Yesterday at ${clock}`;
  const date = when.toLocaleDateString(locale, { day: "numeric", month: "short", year: "numeric" });
  return `${date} at ${clock}`;
}
