import { cn } from "~/lib/utils";

/**
 * One button of a segmented strip in the trajectory tab's headers: flat, uppercase, and filled
 * while pressed. Shared by the UI-flows filters and the subagent-mode switch, which sit in the
 * same view, so the two strips cannot come to look like different kinds of control.
 */
export function Toggle({
  label,
  pressed,
  onPressedChange,
  title,
}: {
  label: string;
  pressed: boolean;
  /** Called with the state the click asks for. A strip where exactly one button is pressed sets
   *  its own choice instead and ignores this. */
  onPressedChange: (next: boolean) => void;
  title?: string;
}) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      title={title}
      onClick={() => onPressedChange(!pressed)}
      className={cn(
        "cursor-pointer px-2 py-0.5 text-xs uppercase transition-colors",
        pressed
          ? "bg-muted text-foreground"
          : "text-muted-foreground hover:text-foreground"
      )}
    >
      {label}
    </button>
  );
}
