The workspace build now reports the frontend's native-dependency state before building it.

A missing platform binary (lightningcss, rollup) surfaces from vite only as `failed to load config from vite.config.ts`, which says nothing about whether `npm ci` installed the package -- and the host is destroyed on a failed create, so there is nothing left to inspect afterwards. The build now logs the node/npm versions, the architecture, and which native dep packages are present in `node_modules` (or that `node_modules` is absent entirely).
