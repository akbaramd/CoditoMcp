# `@codito/vite-plugin-inspector`

Development-only source mapping for Codito managed frontend sessions. The plugin
adds a consumer source reference to custom JSX components and a host source
reference to intrinsic DOM elements. It runs only under Vite's `serve` command;
production builds are unchanged.

The `data-codito-*` attribute namespace is reserved. The transform replaces
explicit values in that namespace with the actual development source coordinate.

```ts
// vite.config.ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { coditoInspector } from "@codito/vite-plugin-inspector";

export default defineConfig({
  plugins: [react(), coditoInspector()],
});
```

If Vite's root is below the Codito registered project, pass the project root:

```ts
import { fileURLToPath } from "node:url";

coditoInspector({ projectRoot: fileURLToPath(new URL("../..", import.meta.url)) });
```

Design-system components must forward `data-*` props to their DOM host for the
consumer callsite to remain exact:

```tsx
export function Button(props: React.ComponentProps<"button">) {
  return <button {...props} />;
}
```

The Windows agent treats page metadata as untrusted input. It reports an exact
mapping only after resolving the relative path inside the registered project and
validating the referenced line and column.
