#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(readFileSync(join(root, "plugin.json"), "utf8"));
const name = "Ayaneo3Companion";
const build = join(root, "build");
const stage = join(build, name);
const zip = join(root, `${name}-${manifest.version}.zip`);
const files = ["main.py", "plugin.json", "package.json", "README.md", "LICENSE", "NOTICE", "dist", "assets"];
if (!existsSync(join(root, "dist", "index.js"))) throw new Error("dist/index.js is missing");
rmSync(build, { recursive: true, force: true }); rmSync(zip, { force: true }); mkdirSync(stage, { recursive: true });
for (const file of files) cpSync(join(root, file), join(stage, file), {
  recursive: true,
  filter: source => !source.includes("__pycache__") && !source.endsWith(".pyc"),
});
const sevenZip = ["C:\\Program Files\\7-Zip\\7z.exe", "C:\\Program Files (x86)\\7-Zip\\7z.exe"].find(existsSync);
if (sevenZip) execFileSync(sevenZip, ["a", "-tzip", "-mx=9", zip, name], { cwd: build, stdio: "inherit" });
else execFileSync("zip", ["-r", "-9", "-q", zip, name], { cwd: build, stdio: "inherit" });
rmSync(build, { recursive: true, force: true });
console.log(`packaged v${manifest.version} -> ${zip}`);
