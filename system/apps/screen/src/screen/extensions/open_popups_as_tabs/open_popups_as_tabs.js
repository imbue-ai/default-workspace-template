// A window.open pop-up (the shape of most "Sign in with ..." buttons) becomes a tab of the window that opened it,
// so an agent's tab list and the viewer's one window stay in step. A features string is what asks for a pop-up;
// appending popup=0 overrides it (a later feature wins) while keeping noopener/noreferrer, and the tab keeps
// window.opener, which an OAuth callback page needs to hand its result back and close itself. A Proxy keeps
// window.open's name and length and a native-code toString, though V8 prints any Proxy without its name
// ("function () { [native code] }", not "function open() ...").
window.open = new Proxy(window.open, {
  apply(open, thisArg, args) {
    if (args.length >= 3 && args[2]) {
      args = [args[0], args[1], `${args[2]},popup=0`];
    }
    return Reflect.apply(open, thisArg, args);
  },
});
