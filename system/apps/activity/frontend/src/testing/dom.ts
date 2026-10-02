/** DOM lookups the view tests share. */

export function buttonNamed(root: ParentNode, label: string): HTMLButtonElement {
  const button = Array.from(root.querySelectorAll("button")).find((element) => element.textContent?.trim() === label);
  if (button === undefined) throw new Error(`no button named ${label}`);
  return button;
}
