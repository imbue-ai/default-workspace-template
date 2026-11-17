/* The full-width dismissable banner box: a bordered strip pinned above its
 * pane's content, message left, actions right; tones swap only the colour
 * tokens, so the box geometry cannot drift between banners. The marker is a
 * bare class (like `btn`) so tests can find each banner. */

export type BannerTone = "neutral" | "warning";

const BANNER_TONES: Record<BannerTone, string> = {
  neutral: "border-default bg-body-surface-secondary text-body-content-secondary",
  warning: "border-warn-4 bg-warn-subtle-surface text-body-content",
};

export function bannerClass(marker: string, tone: BannerTone): string {
  return (
    `${marker} flex flex-none items-center justify-between gap-3 border-b px-2.5 py-1.5 ` +
    `text-(length:--font-size-body) leading-[1.4] ${BANNER_TONES[tone]}`
  );
}
