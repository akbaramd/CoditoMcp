import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { build } from "vite";
import { describe, expect, it } from "vitest";

import { coditoInspector } from "../src/index.js";

type TransformHook = (
  code: string,
  id: string,
) => Promise<{ code: string; map: unknown } | null>;

type CallableTransform = (
  this: unknown,
  code: string,
  id: string,
  options: { moduleType: "js" },
) => unknown | Promise<unknown>;

function transformFor(root: string): TransformHook {
  const plugin = coditoInspector({ projectRoot: root });
  const hook = plugin.transform;
  const handler = (typeof hook === "function" ? hook : hook?.handler) as
    | CallableTransform
    | undefined;
  if (!handler) {
    throw new Error("Expected a Vite transform hook");
  }
  return async (code, id) => {
    const result = await handler.call({}, code, id, {
      moduleType: "js",
    });
    if (!result) {
      return null;
    }
    if (typeof result === "string") {
      return { code: result, map: null };
    }
    return result as { code: string; map: unknown };
  };
}

describe("coditoInspector", () => {
  const root = path.resolve("C:/workspace/app");

  it("is development-server-only", () => {
    expect(coditoInspector().apply).toBe("serve");
  });

  it("does not emit metadata in a real production Vite build", async () => {
    const buildRoot = mkdtempSync(path.join(os.tmpdir(), "codito-inspector-"));
    try {
      writeFileSync(
        path.join(buildRoot, "index.html"),
        '<div id="app"></div><script type="module" src="/src.jsx"></script>',
      );
      writeFileSync(
        path.join(buildRoot, "src.jsx"),
        "const App = () => <button>Save</button>; console.log(App);",
      );
      const result = await build({
        root: buildRoot,
        logLevel: "silent",
        plugins: [coditoInspector({ projectRoot: buildRoot })],
        build: {
          write: false,
          minify: false,
          rolldownOptions: {
            external: ["react/jsx-runtime", "react/jsx-dev-runtime"],
          },
        },
      });
      const outputs = (Array.isArray(result) ? result : [result]) as unknown as Array<{
        output: Array<{ code?: string }>;
      }>;
      const code = outputs.flatMap((output) => output.output).map((item) => item.code ?? "").join("\n");
      expect(code).not.toContain("data-codito-");
    } finally {
      rmSync(buildRoot, { force: true, recursive: true });
    }
  });

  it("adds consumer and host source metadata with one-based coordinates", async () => {
    const transform = transformFor(root);
    const result = await transform(
      [
        "export function Actions() {",
        "  return <Button><span>ویرایش</span></Button>;",
        "}",
      ].join("\n"),
      path.join(root, "src", "Actions.tsx"),
    );

    expect(result?.code).toContain('data-codito-source="src/Actions.tsx:2:10"');
    expect(result?.code).toContain('data-codito-host-source="src/Actions.tsx:2:18"');
    expect(result?.map).toBeTruthy();
  });

  it("overwrites duplicate reserved metadata after later spreads", async () => {
    const transform = transformFor(root);
    const result = await transform(
      [
        "const props = { 'data-codito-source': 'spread.tsx:1:1' };",
        "export const Item = () => <Card data-codito-source=\"first.tsx:1:1\"",
        "  {...props} data-codito-source=\"last.tsx:1:1\"",
        "  data-codito-host-source=\"host.tsx:1:1\" />;",
      ].join("\n"),
      path.join(root, "src", "Item.tsx"),
    );

    expect(result?.code.match(/data-codito-source=/g)).toHaveLength(1);
    expect(result?.code).not.toContain("first.tsx:1:1");
    expect(result?.code).not.toContain("last.tsx:1:1");
    expect(result?.code).not.toContain("host.tsx:1:1");
    expect(result?.code).toMatch(
      /\.\.\.props[\s\S]*data-codito-source="src\/Item\.tsx:2:27"/,
    );
  });

  it("removes direct consumer claims from intrinsic hosts", async () => {
    const transform = transformFor(root);
    const result = await transform(
      'export const Item = () => <button data-codito-source="fake.tsx:1:1" />;',
      path.join(root, "src", "Item.tsx"),
    );

    expect(result?.code).not.toContain("fake.tsx:1:1");
    expect(result?.code).not.toContain("data-codito-source=");
    expect(result?.code).toContain('data-codito-host-source="src/Item.tsx:1:27"');
  });

  it("removes mixed-case reserved claims before HTML normalizes them", async () => {
    const transform = transformFor(root);
    const result = await transform(
      'export const Item = () => <button data-Codito-source="fake.tsx:1:1" />;',
      path.join(root, "src", "Item.tsx"),
    );

    expect(result?.code).not.toContain("fake.tsx:1:1");
    expect(result?.code.toLowerCase()).not.toContain("data-codito-source=");
    expect(result?.code).toContain('data-codito-host-source="src/Item.tsx:1:27"');
  });

  it("emits Unicode-scalar columns rather than JavaScript UTF-16 offsets", async () => {
    const transform = transformFor(root);
    const result = await transform(
      "export const Item = () => <>🙂<Button /></>;",
      path.join(root, "src", "Item.tsx"),
    );

    expect(result?.code).toContain('data-codito-source="src/Item.tsx:1:30"');
  });

  it("skips dependencies and files outside the declared project", async () => {
    const transform = transformFor(root);

    await expect(
      transform("export const X = () => <div />;", path.resolve("C:/other/X.tsx")),
    ).resolves.toBeNull();
    await expect(
      transform(
        "export const X = () => <div />;",
        path.join(root, "node_modules", "pkg", "X.tsx"),
      ),
    ).resolves.toBeNull();
    await expect(
      transform(
        "export const X = () => <div />;",
        path.join(root, "NODE_MODULES", "pkg", "X.tsx"),
      ),
    ).resolves.toBeNull();
  });

  it("leaves non-JSX modules untouched", async () => {
    const transform = transformFor(root);
    await expect(
      transform("export const value = 1;", path.join(root, "src", "value.ts")),
    ).resolves.toBeNull();
  });
});
