#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { cpSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { crc32, deflateRawSync } from "node:zlib";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(readFileSync(join(root, "plugin.json"), "utf8"));
const packageManifest = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
const packageLock = JSON.parse(readFileSync(join(root, "package-lock.json"), "utf8"));
const name = "Ayaneo3Companion";
const files = [
  "main.py", "safe_settings.py", "quality_runtime.py", "lego_updater.py", "fan_backend.py", "module_runtime.py", "diagnostics.py", "system_process.py", "input_backend.py", "display_backend.py", "audio_backend.py", "power_backend.py", "controller_backend.py", "ec_backend.py", "companion_common.py", "plugin.json", "package.json", "package-lock.json",
  "README.md", "CHANGELOG.md", "LICENSE", "NOTICE", "requirements.txt",
  "bootstrap.sh", "bootstrap_installer.py", "Install AYANEO 3 Companion.desktop", "rollup.config.js",
  "tsconfig.json", "src", "dist", "assets",
];
if (process.argv.slice(2).some(arg => arg !== "--check")) throw new Error("usage: node scripts/package.mjs [--check]");
if (!/^\d+\.\d+\.\d+$/.test(manifest.version) || manifest.name !== "AYANEO 3 Companion"
    || packageManifest.name !== "ayaneo3companion") throw new Error("invalid package identity or stable version");
for (const file of files) if (!existsSync(join(root, file))) throw new Error(`required file missing: ${file}`);
if (!existsSync(join(root, "dist", "index.js"))) throw new Error("dist/index.js is missing");
for (const file of ["assets/ayaneo.ayaneo3.oled.lua", "assets/ayaneo3-companion.yaml"]) {
  const path = join(root, file);
  if (!existsSync(path) || !lstatSync(path).isFile() || lstatSync(path).size === 0) {
    throw new Error(`required runtime asset missing or empty: ${file}`);
  }
}
if (manifest.version !== packageManifest.version || manifest.version !== packageLock.version
    || manifest.version !== packageLock.packages?.[""]?.version
    || packageLock.name !== packageManifest.name || packageLock.packages?.[""]?.name !== packageManifest.name
    || packageLock.packages?.[""]?.license !== packageManifest.license) {
  throw new Error("plugin.json, package.json and package-lock.json versions do not agree");
}
const deckyApiPackage = JSON.parse(readFileSync(join(root, "node_modules", "@decky", "api", "package.json"), "utf8"));
if (deckyApiPackage.version !== "1.1.3") {
  throw new Error("third-party license inventory no longer matches the installed frontend dependencies");
}
function fail(message) {
  throw new Error(message);
}

function listTree(base, prefix = "") {
  const entries = [];
  for (const item of readdirSync(join(base, prefix), { withFileTypes: true })) {
    const name = prefix ? `${prefix}/${item.name}` : item.name;
    if (item.isDirectory()) entries.push(`${name}/`, ...listTree(base, name));
    else if (item.isFile()) entries.push(name);
    else fail(`unsupported payload entry: ${name}`);
  }
  return entries;
}

// Same tree + same timestamp -> byte-identical ZIP: sorted names, one fixed DOS time (UTC),
// fixed modes (0644 files, 0755 directories). Deflate output is fixed for a given Node/zlib.
// From LegionGo2Companion.
function writeZip(base, top, zipPath, epochSeconds) {
  const when = new Date(epochSeconds * 1000);
  const dosTime = (when.getUTCHours() << 11) | (when.getUTCMinutes() << 5) | (when.getUTCSeconds() >> 1);
  const dosDate = ((when.getUTCFullYear() - 1980) << 9) | ((when.getUTCMonth() + 1) << 5) | when.getUTCDate();
  const names = [`${top}/`, ...listTree(base, top)].sort();
  if (names.length > 0xffff) fail("too many ZIP entries");
  const locals = [];
  const central = [];
  let offset = 0;
  for (const name of names) {
    const isDir = name.endsWith("/");
    const data = isDir ? Buffer.alloc(0) : readFileSync(join(base, name));
    const packed = isDir ? data : deflateRawSync(data, { level: 9 });
    const method = !isDir && packed.length < data.length ? 8 : 0;
    const body = method ? packed : data;
    const crc = crc32(data);
    const nameBytes = Buffer.from(name, "utf8");
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(method, 8);
    local.writeUInt16LE(dosTime, 10);
    local.writeUInt16LE(dosDate, 12);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(body.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    const header = Buffer.alloc(46);
    header.writeUInt32LE(0x02014b50, 0);
    header.writeUInt16LE((3 << 8) | 20, 4);
    header.writeUInt16LE(20, 6);
    header.writeUInt16LE(method, 10);
    header.writeUInt16LE(dosTime, 12);
    header.writeUInt16LE(dosDate, 14);
    header.writeUInt32LE(crc, 16);
    header.writeUInt32LE(body.length, 20);
    header.writeUInt32LE(data.length, 24);
    header.writeUInt16LE(nameBytes.length, 28);
    header.writeUInt32LE((isDir ? 0o40755 : 0o100644) * 0x10000 + (isDir ? 0x10 : 0), 38);
    header.writeUInt32LE(offset, 42);
    locals.push(local, nameBytes, body);
    central.push(header, nameBytes);
    offset += local.length + nameBytes.length + body.length;
  }
  const directory = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(names.length, 8);
  end.writeUInt16LE(names.length, 10);
  end.writeUInt32LE(directory.length, 12);
  end.writeUInt32LE(offset, 16);
  writeFileSync(zipPath, Buffer.concat([...locals, directory, end]));
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
    // The last commit's time: the same tree packaged twice gives a byte-identical ZIP.
    const committed = Number(execFileSync("git", ["log", "-1", "--format=%ct", "HEAD"], { cwd: root, encoding: "utf8" }).trim());
    writeZip(build, name, archive, committed);
    // Publish from the repository filesystem so a failed build keeps the last ZIP.
    const pending = join(root, `.${archiveName}.${process.pid}.pending`);
    if (existsSync(pending)) throw new Error(`temporary package already exists: ${pending}`);
    try {
      cpSync(archive, pending, { errorOnExist: true, force: false });
      renameSync(pending, destination);
    } finally {
      rmSync(pending, { force: true });
    }
    // sha256sum format, so `sha256sum -c` verifies the downloaded ZIP.
    const digest = createHash("sha256").update(readFileSync(destination)).digest("hex");
    writeFileSync(`${destination}.sha256`, `${digest}  ${archiveName}
`);
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
