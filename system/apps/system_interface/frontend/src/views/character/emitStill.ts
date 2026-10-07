/**
 * Prints the character's still to stdout, for regenerating the served asset.
 * The command is in `stillFrame.ts`; this file is only its entry point.
 */
import { characterStillSvg } from "./stillFrame";

process.stdout.write(`${characterStillSvg()}\n`);
