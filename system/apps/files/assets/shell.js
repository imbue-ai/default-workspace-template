// The File Viewer's connection to the workspace shell: its pages speak the app contract every app speaks
// (docs/system/blueprint/desktop-interface/contracts.md section 7) -- the location beacon, the shell's window opens,
// and the contract's rule for a clicked link. dufs serves the filesystem root, so the contract module the shell builds
// (and every other app serves) is on this origin at its path in the workspace, served like any file, revalidated on
// every load. The page's other scripts read the connection from ``window.mindsShell``, which stays unset when the
// module cannot load.
import { connectToShell } from "/home/user/workspace/system/apps/system_interface/imbue/system_interface/static/_static/app_contract.js";

window.mindsShell = connectToShell({});
