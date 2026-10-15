// The File Viewer's connection to the workspace shell, through the app contract every app speaks
// (docs/system/blueprint/desktop-interface/contracts.md section 7). dufs cannot serve the module at
// ``/_static/app_contract.js`` as other apps do, but it serves the filesystem root, so the module the shell builds is on
// this origin at its path in the workspace, revalidated on every load like any file (an asset under dufs's prefix would
// be cached for a year). The page's other scripts read the connection from ``window.mindsShell``, which stays unset
// when the module cannot load.
import { connectToShell } from "/home/user/workspace/system/apps/system_interface/imbue/system_interface/static/_static/app_contract.js";

window.mindsShell = connectToShell({});
