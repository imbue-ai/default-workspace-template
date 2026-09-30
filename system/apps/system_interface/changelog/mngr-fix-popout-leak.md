A window dragged back from its own Imbue Studio window onto the desktop now lands at the size it had in that window. Before, it came back about 6% shorter every time.

The chrome measures the drop against the workspace's whole view, taskbar included. The shell read those fractions as fractions of the backdrop above the taskbar, so every return lost the taskbar's share of the window's height. The shell now maps the dropped frame onto its backdrop before placing the window.

A window pulled out with a fast drag no longer shows both in its own window and on the desktop for a few seconds. The release could reach the desktop before the Imbue Studio app's word that the window had gone out, so the desktop settled the drag as a move inside while the app kept the new window open. The desktop now takes that late word: the window goes out from where the drag began, and a window the app brought back in at the last moment comes back.
