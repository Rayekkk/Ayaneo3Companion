#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(readFileSync(join(root, "plugin.json"), "utf8"));
const packageManifest = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
const packageLock = JSON.parse(readFileSync(join(root, "package-lock.json"), "utf8"));
const name = "Ayaneo3Companion";
const build = join(root, "build");
const stage = join(build, name);
const zip = join(root, `${name}-${manifest.version}.zip`);
const files = [
  "main.py", "lego_updater.py", "plugin.json", "package.json", "package-lock.json",
  "README.md", "CHANGELOG.md", "LICENSE", "NOTICE", "requirements.txt",
  "bootstrap.sh", "Install AYANEO 3 Companion.desktop", "rollup.config.js",
  "tsconfig.json", "src", "dist", "assets",
];
if (!existsSync(join(root, "dist", "index.js"))) throw new Error("dist/index.js is missing");
if (manifest.version !== packageManifest.version || manifest.version !== packageLock.version
    || manifest.version !== packageLock.packages?.[""]?.version) {
  throw new Error("plugin.json, package.json and package-lock.json versions do not agree");
}
const deckyApiPackage = JSON.parse(readFileSync(join(root, "node_modules", "@decky", "api", "package.json"), "utf8"));
const reactIconsPackage = JSON.parse(readFileSync(join(root, "node_modules", "react-icons", "package.json"), "utf8"));
if (deckyApiPackage.version !== "1.1.3" || reactIconsPackage.version !== "5.7.0") {
  throw new Error("third-party license inventory no longer matches the installed frontend dependencies");
}
rmSync(build, { recursive: true, force: true }); rmSync(zip, { force: true }); mkdirSync(stage, { recursive: true });
for (const file of files) cpSync(join(root, file), join(stage, file), {
  recursive: true,
  filter: source => !source.includes("__pycache__") && !source.endsWith(".pyc"),
});
const licenseDir = join(stage, "THIRD_PARTY_LICENSES");
const sourceDir = join(stage, "THIRD_PARTY_SOURCES", "decky-api-1.1.3");
mkdirSync(licenseDir, { recursive: true });
cpSync(join(root, "node_modules", "@decky", "api", "LICENSE"), join(licenseDir, "decky-api-LGPL-2.1.txt"));
cpSync(join(root, "node_modules", "react-icons", "LICENSE"), join(licenseDir, "react-icons-and-icon-packs.txt"));
cpSync(join(root, "node_modules", "@decky", "api"), sourceDir, { recursive: true });
const sevenZip = ["C:\\Program Files\\7-Zip\\7z.exe", "C:\\Program Files (x86)\\7-Zip\\7z.exe"].find(existsSync);
if (sevenZip) execFileSync(sevenZip, ["a", "-tzip", "-mx=9", zip, name], { cwd: build, stdio: "inherit" });
else execFileSync("zip", ["-r", "-9", "-q", zip, name], { cwd: build, stdio: "inherit" });
rmSync(build, { recursive: true, force: true });
console.log(`packaged v${manifest.version} -> ${zip}`);
