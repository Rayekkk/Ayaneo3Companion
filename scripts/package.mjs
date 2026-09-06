#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, renameSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(readFileSync(join(root, "plugin.json"), "utf8"));
const packageManifest = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
const packageLock = JSON.parse(readFileSync(join(root, "package-lock.json"), "utf8"));
const name = "Ayaneo3Companion";
const files = [
  "main.py", "safe_settings.py", "quality_runtime.py", "lego_updater.py", "plugin.json", "package.json", "package-lock.json",
  "README.md", "CHANGELOG.md", "LICENSE", "NOTICE", "requirements.txt",
  "bootstrap.sh", "Install AYANEO 3 Companion.desktop", "rollup.config.js",
  "tsconfig.json", "src", "dist", "assets",
];
if (process.argv.slice(2).some(arg => arg !== "--check")) throw new Error("usage: node scripts/package.mjs [--check]");
if (!/^\d+\.\d+\.\d+$/.test(manifest.version) || manifest.name !== "AYANEO 3 Companion"
    || packageManifest.name !== "ayaneo3companion") throw new Error("invalid package identity or stable version");
for (const file of files) if (!existsSync(join(root, file))) throw new Error(`required file missing: ${file}`);
if (!existsSync(join(root, "dist", "index.js"))) throw new Error("dist/index.js is missing");
if (manifest.version !== packageManifest.version || manifest.version !== packageLock.version
    || manifest.version !== packageLock.packages?.[""]?.version
    || packageLock.name !== packageManifest.name || packageLock.packages?.[""]?.name !== packageManifest.name
    || packageLock.packages?.[""]?.license !== packageManifest.license) {
  throw new Error("plugin.json, package.json and package-lock.json versions do not agree");
}
const deckyApiPackage = JSON.parse(readFileSync(join(root, "node_modules", "@decky", "api", "package.json"), "utf8"));
const reactIconsPackage = JSON.parse(readFileSync(join(root, "node_modules", "react-icons", "package.json"), "utf8"));
if (deckyApiPackage.version !== "1.1.3" || reactIconsPackage.version !== "5.7.0") {
  throw new Error("third-party license inventory no longer matches the installed frontend dependencies");
}
function copyPayload(source, destination) {
  cpSync(source, destination, {
    recursive: true,
    filter: path => {
      if (/^(?:__pycache__|\.DS_Store)$/.test(basename(path)) || /\.py[cod]$/.test(path)) return false;
      const info = lstatSync(path);
      if (info.isSymbolicLink() || (!info.isFile() && !info.isDirectory())) {
        throw new Error(`package payload must contain only regular files and directories: ${path}`);
      }
      return true;
    },
  });
}

// A fresh staging directory cannot remove a developer's existing build folder.
const tempRoot = realpathSync(tmpdir());
const build = mkdtempSync(join(tempRoot, "ayaneo3companion-package-"));
const stage = join(build, name);
try {
  mkdirSync(stage);
  for (const file of files) copyPayload(join(root, file), join(stage, file));
  const licenseDir = join(stage, "THIRD_PARTY_LICENSES");
  const sourceDir = join(stage, "THIRD_PARTY_SOURCES", "decky-api-1.1.3");
  mkdirSync(licenseDir, { recursive: true });
  copyPayload(join(root, "node_modules", "@decky", "api", "LICENSE"), join(licenseDir, "decky-api-LGPL-2.1.txt"));
  copyPayload(join(root, "node_modules", "react-icons", "LICENSE"), join(licenseDir, "react-icons-and-icon-packs.txt"));
  copyPayload(join(root, "node_modules", "@decky", "api"), sourceDir);
  if (process.argv.includes("--check")) {
    console.log(`package payload check passed (${files.length} entries)`);
  } else {
    const archiveName = `${name}-${manifest.version}.zip`;
    const archive = join(build, archiveName);
    const destination = join(root, archiveName);
    if (existsSync(destination) && !lstatSync(destination).isFile()) {
      throw new Error(`package destination is not a regular file: ${destination}`);
    }
    const sevenZip = ["C:\\Program Files\\7-Zip\\7z.exe", "C:\\Program Files (x86)\\7-Zip\\7z.exe"].find(existsSync);
    if (sevenZip) execFileSync(sevenZip, ["a", "-tzip", "-mx=9", archive, name], { cwd: build, stdio: "inherit" });
    else execFileSync("zip", ["-r", "-9", "-q", archive, name], { cwd: build, stdio: "inherit" });
    // Publish from the repository filesystem so a failed build keeps the last ZIP.
    const pending = join(root, `.${archiveName}.${process.pid}.pending`);
    if (existsSync(pending)) throw new Error(`temporary package already exists: ${pending}`);
    try {
      cpSync(archive, pending, { errorOnExist: true, force: false });
      renameSync(pending, destination);
    } finally {
      rmSync(pending, { force: true });
    }
    console.log(`packaged v${manifest.version} -> ${destination}`);
  }
} finally {
  // Verify the exact resolved target before recursive cleanup on every host.
  const resolved = realpathSync(build);
  if (resolved !== build || !resolved.startsWith(tempRoot + sep)
      || !basename(resolved).startsWith("ayaneo3companion-package-")) {
    throw new Error("refusing to clean up an unexpected package staging path");
  }
  rmSync(resolved, { recursive: true, force: true });
}
