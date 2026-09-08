import { describeLintAndFormat } from "@imbue/workspace-ui/src/lint-and-format-checks";

describeLintAndFormat(new URL("..", import.meta.url).pathname);
