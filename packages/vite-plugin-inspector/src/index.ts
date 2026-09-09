import path from "node:path";

import { transformAsync, type PluginObj } from "@babel/core";
import * as t from "@babel/types";
import type { Plugin, ResolvedConfig } from "vite";

const SOURCE_ATTRIBUTE = "data-codito-source";
const HOST_SOURCE_ATTRIBUTE = "data-codito-host-source";
const JSX_FILE = /\.[cm]?[jt]sx$/i;

export interface CoditoInspectorOptions {
  /**
   * Root used in emitted source references. Defaults to Vite's resolved root.
   * Set this to the registered Codito project root when Vite runs in a subfolder.
   */
  projectRoot?: string;
}

function cleanModuleId(id: string): string {
  return id.split("?", 1)[0] ?? id;
}

function isInside(root: string, filename: string): boolean {
  const relative = path.relative(root, filename);
  return relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}

function scalarColumn(code: string, line: number, utf16Column: number): number {
  const sourceLine = code.split(/\r\n|\n|\r/)[line - 1] ?? "";
  return Array.from(sourceLine.slice(0, utf16Column)).length + 1;
}

function sourceReference(root: string, filename: string, line: number, column: number): string {
  const relative = path.relative(root, filename).split(path.sep).join("/");
  return `${relative}:${line}:${column}`;
}

function removeReservedAttributes(node: t.JSXOpeningElement): void {
  node.attributes = node.attributes.filter(
    (attribute) =>
      !(
        t.isJSXAttribute(attribute) &&
        t.isJSXIdentifier(attribute.name) &&
        [SOURCE_ATTRIBUTE, HOST_SOURCE_ATTRIBUTE].includes(attribute.name.name.toLowerCase())
      ),
  );
}

function isDependencyPath(filename: string): boolean {
  return filename
    .split(/[\\/]/)
    .some((segment) => segment.toLowerCase() === "node_modules");
}

function isIntrinsicElement(name: t.JSXOpeningElement["name"]): boolean {
  return t.isJSXIdentifier(name) && /^[a-z]/.test(name.name);
}

function sourceMetadataPlugin(projectRoot: string, filename: string, code: string): PluginObj {
  return {
    name: "codito-source-metadata",
    visitor: {
      JSXOpeningElement(nodePath) {
        const location = nodePath.node.loc?.start;
        if (!location || t.isJSXIdentifier(nodePath.node.name, { name: "Fragment" })) {
          return;
        }

        const intrinsic = isIntrinsicElement(nodePath.node.name);
        const attributeName = intrinsic ? HOST_SOURCE_ATTRIBUTE : SOURCE_ATTRIBUTE;
        const reference = sourceReference(
          projectRoot,
          filename,
          location.line,
          scalarColumn(code, location.line, location.column),
        );
        // The data-codito-* namespace is reserved. Remove every direct duplicate,
        // then append the generated value after spreads so checked-in JSX cannot
        // win by attribute order. Values created dynamically at runtime remain a
        // documented cooperative-project limit.
        removeReservedAttributes(nodePath.node);
        nodePath.node.attributes.push(
          t.jsxAttribute(t.jsxIdentifier(attributeName), t.stringLiteral(reference)),
        );
      },
    },
  };
}

/**
 * Adds source metadata only to Vite's development server transform pipeline.
 * It is deliberately excluded from production builds and never instruments
 * dependencies or files outside the configured project root.
 */
export function coditoInspector(options: CoditoInspectorOptions = {}): Plugin {
  let resolvedConfig: ResolvedConfig | undefined;

  return {
    name: "codito:frontend-inspector",
    apply: "serve",
    enforce: "pre",
    configResolved(config) {
      resolvedConfig = config;
    },
    async transform(code, rawId) {
      const filename = path.resolve(cleanModuleId(rawId));
      if (!JSX_FILE.test(filename) || isDependencyPath(filename)) {
        return null;
      }

      const projectRoot = path.resolve(options.projectRoot ?? resolvedConfig?.root ?? process.cwd());
      if (!isInside(projectRoot, filename)) {
        return null;
      }

      const result = await transformAsync(code, {
        ast: false,
        babelrc: false,
        code: true,
        compact: false,
        configFile: false,
        filename,
        parserOpts: {
          plugins: ["jsx", "typescript"],
          sourceType: "module",
        },
        plugins: [sourceMetadataPlugin(projectRoot, filename, code)],
        sourceFileName: path.relative(projectRoot, filename).split(path.sep).join("/"),
        sourceMaps: true,
      });

      if (!result?.code) {
        return null;
      }
      return { code: result.code, map: result.map ?? null };
    },
  };
}

export default coditoInspector;
