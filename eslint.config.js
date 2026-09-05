import js from "@eslint/js";
import eslintPluginPrettier from "eslint-plugin-prettier/recommended";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  // `design` holds the design-tool export and its generated runtime, whose
  // first line reads "GENERATED ... do not edit". Linting it produced 247
  // Prettier errors in one vendored file, which is why `npm run lint` failed
  // while `eslint src` was clean — the project's own lint command was red and
  // nobody was running it.
  // .tmp-* are local scratch directories (a fresh clone made to reproduce a
  // CI failure, tooling temp dirs). They are gitignored, so CI never saw them,
  // but locally they put 247 prettier errors from vendored code in front of a
  // clean lint and made it look like the app was broken.
  { ignores: ["dist", ".output", ".vinxi", "design", ".tmp-*/**"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "no-restricted-imports": [
        "error",
        {
          paths: [
            {
              name: "server-only",
              message:
                "TanStack Start does not use the Next.js `server-only` package. Rename the module to `*.server.ts` or mark it with `@tanstack/react-start/server-only`.",
            },
          ],
        },
      ],
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      // Was "off". With it off, seven dead imports accumulated — including
      // three `Clock`s left behind when the history link was refactored — and
      // nothing said so. An unused import is a small thing that means the
      // reader has to check whether it matters.
      //
      // The underscore escape hatch is deliberate: a deliberately unused
      // binding is a real thing, and it should have to say so in its name.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" },
      ],
    },
  },
  eslintPluginPrettier,
);
