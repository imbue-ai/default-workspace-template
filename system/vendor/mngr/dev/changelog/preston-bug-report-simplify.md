Ignore `apps/minds/package-lock.json` alongside the other minds lockfiles the repo already ignores, so a local `npm install` in the Electron app dir cannot sweep a generated lockfile into a commit.
