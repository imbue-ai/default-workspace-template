import { describeLintAndFormat } from "./lint-and-format-checks";

describeLintAndFormat(new URL("..", import.meta.url).pathname);
