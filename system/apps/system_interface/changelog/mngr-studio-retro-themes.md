The desktop follows Imbue Studio's theme: when the user picks Classic Mac or Windows 2000 in Studio's Settings > Display, the desktop switches with it.

- Classic Mac: black-and-white windows with pinstriped title bars on the focused window, a 50% gray desktop, Chicago type, and Finder-style icon labels.

- Windows 2000: face-gray beveled windows with the navy caption gradient (gray for a window in the background), a teal desktop, a beveled taskbar with the current window's button pushed in, and Tahoma type.

- Under either theme every app's icon becomes a pixel-art one drawn for that theme; an app without one gets a generic program icon. A wallpaper a desktop chose itself still shows.

- The themes change the title bar's height, so the desktop re-reads its geometry when the theme arrives.

- Nothing changes until Studio sends the theme, which needs an mngr pin whose embed contract carries `minds:ui-theme` (contract version 7).
