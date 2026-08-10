/// <reference types="vite/client" />

// Vite's own ambient types, for `import.meta.glob` (service-marks.ts globs the
// vendored brand marks) and the asset module declarations that type an imported
// image or a `?url` import as a string. These already cover `*.png` and the
// rest, so nothing may be re-declared here -- a second `export default` for the
// same module is an error.
