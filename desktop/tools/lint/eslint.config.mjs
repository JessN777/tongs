// ESLint for the desktop sources, run from desktop/ by `npm run lint`.
// typescript-eslint's recommended rules without type information: the parser
// reads syntax only, so this package's TypeScript 6 parses the app's
// TypeScript 7 sources, whose syntax is the same.
import { defineConfig, globalIgnores } from "eslint/config";
import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

export default defineConfig(
  globalIgnores(["dist/", "tools/lint/node_modules/"]),
  {
    files: ["**/*.{js,mjs,cjs,ts,tsx,cts,mts}"],
    extends: [js.configs.recommended, tseslint.configs.recommended],
  },
  {
    files: ["src/main/**", "src/preload/**", "scripts/**", "tools/**"],
    languageOptions: { globals: globals.node },
  },
  {
    files: ["src/renderer/**"],
    languageOptions: { globals: globals.browser },
  },
);
