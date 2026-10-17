// Apache Guacamole's keyboard, as neko ships it (client/core/src/keyboard/guacamole.d.ts at v3.2.0): the typed
// surface of guacamole_keyboard.js.
export default class GuacamoleKeyboard {
  constructor(element?: Element | Document);
  // return true to let the browser handle the key, false to prevent default
  onkeydown?: (keysym: number) => boolean;
  onkeyup?: (keysym: number) => void;
  press(keysym: number): boolean;
  release(keysym: number): void;
  reset(): void;
  listenTo(element: Element | Document): void;
}
