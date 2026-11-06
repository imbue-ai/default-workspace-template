/**
 * The shared stylesheet's source, read from disk: under vitest a stylesheet import (``?raw`` included) comes back
 * empty, since the test runner does not process CSS.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

export const BASE_CSS = readFileSync(join(__dirname, "../../../../../libs/workspace_ui/src/base.css"), "utf8");
