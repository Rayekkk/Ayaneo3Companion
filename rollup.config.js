import { existsSync, lstatSync, readFileSync, realpathSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import commonjs from "@rollup/plugin-commonjs";
import { nodeResolve } from "@rollup/plugin-node-resolve";
import replace from "@rollup/plugin-replace";
import typescript from "@rollup/plugin-typescript";
import externalGlobals from "rollup-plugin-external-globals";

const root = realpathSync(dirname(fileURLToPath(import.meta.url)));
const dist = join(root, "dist");
const manifest = JSON.parse(readFileSync(join(root, "plugin.json"), "utf8"));
export default {
  input: join(root, "src/index.tsx"),
  plugins: [
    { name: "clean-plugin-dist", buildStart() {
      if (!existsSync(dist)) return;
      const info = lstatSync(dist);
      if (info.isSymbolicLink() || !info.isDirectory() || realpathSync(dist) !== dist
          || dirname(dist) !== root) throw new Error("Refusing to clean an unexpected dist path");
      rmSync(dist, { recursive: true });
    } },
    typescript(), commonjs(), nodeResolve({ browser: true }),
    externalGlobals({ react: "SP_REACT", "react/jsx-runtime": "SP_JSX",
      "react-dom": "SP_REACTDOM", "@decky/ui": "DFL",
      "@decky/manifest": JSON.stringify(manifest) }),
    replace({ preventAssignment: true,
      "process.env.NODE_ENV": JSON.stringify(process.env.NODE_ENV || "production") }),
  ],
  context: "window",
  external: ["react", "react-dom", "@decky/ui"],
  treeshake: { preset: "smallest",
    moduleSideEffects: id => !["@decky/ui", "@decky/api"].includes(id) },
  output: { dir: dist, format: "esm", sourcemap: true, exports: "default",
    sourcemapPathTransform: path => path.replace(/^\.\.\//,
      `decky://decky/plugin/${encodeURIComponent(manifest.name)}/`) },
};
