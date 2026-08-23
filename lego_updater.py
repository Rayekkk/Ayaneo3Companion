# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/Ayaneo3Companion

"""Shared update checks and downloads used by Rayek's Decky plugins.

Everything plugin-specific arrives through ``Updater``'s constructor.  The
``lego_`` prefix is intentional: Decky aliases its own ``updater`` module to a
bare name before plugin modules are imported, so a plugin file named
``updater.py`` would resolve to Decky's class instead of this helper.

Nothing here imports ``decky``, which keeps the module independently testable.
"""

import json
import os
import re
import ssl
import tempfile
import urllib.parse
import urllib.request

try:
    import pwd
except ImportError:  # Windows development and unit tests
    pwd = None


ALLOWED_HOSTS = frozenset({
    "api.github.com",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})

MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024

CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/ssl/cert.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
)


def checked_url(url: str) -> str:
    """Reject anything that is not an HTTPS URL on a known GitHub host."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"refusing non-https URL scheme '{parsed.scheme}'")
    if (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError(f"refusing download from untrusted host '{parsed.hostname}'")
    return url


def version_tuple(text: str) -> tuple[int, ...]:
    """Numeric components of a version string, for ordering comparisons."""
    return tuple(int(part) for part in re.findall(r"\d+", text))


def real_user():
    """The plugins run as root, so find the real desktop user."""
    if pwd is None:
        return None
    return next(
        (entry for entry in sorted(pwd.getpwall(), key=lambda item: item.pw_uid)
         if entry.pw_uid >= 1000 and os.path.isdir(entry.pw_dir)),
        None,
    )


def xdg_download_dir(home_dir: str) -> str:
    try:
        with open(os.path.join(home_dir, ".config", "user-dirs.dirs")) as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("XDG_DOWNLOAD_DIR="):
                    value = line.split("=", 1)[1].strip('"')
                    return value.replace("$HOME", home_dir)
    except OSError:
        pass
    return os.path.join(home_dir, "Downloads")


def confined_download_dir(home_dir: str) -> str:
    """Accept the configured download directory only when it stays in HOME."""
    home = os.path.realpath(home_dir)
    configured = xdg_download_dir(home_dir)
    if not os.path.isabs(configured):
        configured = os.path.join(home, configured)
    candidate = os.path.realpath(configured)
    try:
        inside_home = os.path.commonpath((home, candidate)) == home
    except ValueError:
        inside_home = False
    if not inside_home:
        raise ValueError("configured download directory escapes the user's home")
    return candidate


class Updater:
    """Check GitHub releases and download the exact plugin archive."""

    def __init__(self, *, releases_url: str, user_agent: str, log_prefix: str,
                 plugin_dir: str, asset_name_template: str, logger):
        self.releases_url = releases_url
        self.user_agent = user_agent
        self.log_prefix = log_prefix
        self.plugin_dir = plugin_dir
        self.asset_name_template = asset_name_template
        self.logger = logger
        self._ssl_ctx: ssl.SSLContext | None = None

    def _info(self, message: str) -> None:
        self.logger.info(f"{self.log_prefix} {message}")

    def _warning(self, message: str) -> None:
        self.logger.warning(f"{self.log_prefix} {message}")

    def _error(self, message: str) -> None:
        self.logger.error(f"{self.log_prefix} {message}")

    def ssl_context(self) -> ssl.SSLContext:
        if self._ssl_ctx is not None:
            return self._ssl_ctx

        context = ssl.create_default_context()
        if context.cert_store_stats().get("x509_ca"):
            self._info("TLS: using the default trust store")
            self._ssl_ctx = context
            return context

        candidates = list(CA_BUNDLES)
        try:
            import certifi
            candidates.append(certifi.where())
        except Exception:
            pass

        for path in candidates:
            try:
                if not path or not os.path.exists(path):
                    continue
                context.load_verify_locations(cafile=path)
                if context.cert_store_stats().get("x509_ca"):
                    self._info(
                        f"TLS: default store was empty, loaded CA bundle {path} "
                        f"({context.cert_store_stats()['x509_ca']} certs)")
                    self._ssl_ctx = context
                    return context
            except OSError as error:
                self._warning(f"TLS: cannot load {path}: {error}")

        self._error("TLS: no usable CA bundle found, downloads will fail to verify")
        self._ssl_ctx = context
        return context

    def open_url(self, url: str, timeout: int, headers: dict | None = None):
        request = urllib.request.Request(
            checked_url(url),
            headers=headers or {"User-Agent": self.user_agent},
        )
        response = urllib.request.urlopen(
            request, context=self.ssl_context(), timeout=timeout)
        try:
            checked_url(response.geturl())
        except Exception:
            response.close()
            raise
        return response

    def download_to(self, url: str, output, timeout: int) -> int:
        written = 0
        with self.open_url(url, timeout=timeout) as response:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    raise ValueError("download exceeded the size limit")
                output.write(chunk)
        return written

    def plugin_version(self) -> str:
        """Return the version Decky loaded, falling back to plugin.json."""
        version = os.environ.get("DECKY_PLUGIN_VERSION", "")
        if version:
            return version
        try:
            with open(os.path.join(self.plugin_dir, "plugin.json")) as handle:
                return json.load(handle).get("version", "0.0.0")
        except (OSError, ValueError):
            return "0.0.0"

    def check(self) -> dict:
        """Ask GitHub for the latest release. Never raise through the RPC."""
        current = self.plugin_version()
        try:
            with self.open_url(self.releases_url, timeout=10, headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": self.user_agent,
            }) as response:
                data = json.loads(response.read(MAX_DOWNLOAD_BYTES))
            tag = str(data.get("tag_name", ""))
            if not tag:
                return {
                    "current_version": current,
                    "error": data.get("message", "Unexpected GitHub API response"),
                }
            latest = tag.lstrip("vV").split("-")[0]
            current_release = current.split("-")[0]
            latest_tuple = version_tuple(latest)
            current_tuple = version_tuple(current_release)
            available = (latest_tuple > current_tuple) \
                if latest_tuple and current_tuple else latest != current_release
            expected_name = self.asset_name_template.format(version=latest)
            asset = next((item for item in data.get("assets", [])
                          if str(item.get("name", "")) == expected_name), None)
            result = {
                "current_version": current,
                "latest_version": latest,
                "update_available": available,
                "download_url": asset.get("browser_download_url") if asset else None,
                "asset_name": asset.get("name") if asset else None,
            }
            if available and asset is None:
                result["error"] = f"Release asset {expected_name} is missing"
            return result
        except Exception as error:
            self._error(f"check_for_updates: {error}")
            return {"current_version": current, "error": str(error)}

    def download_latest(self) -> dict:
        """Re-check the release and download its backend-owned asset."""
        release = self.check()
        if release.get("error"):
            return {"success": False, "error": release["error"]}
        if not release.get("update_available"):
            return {"success": False, "error": "No newer release is available"}
        url = release.get("download_url")
        name = release.get("asset_name")
        if not url or not name:
            return {"success": False, "error": "The release archive is unavailable"}
        return self._download_asset(url, name)

    def _download_asset(self, download_url: str, asset_name: str) -> dict:
        temp_path = None
        try:
            user = real_user()
            if user is None:
                raise RuntimeError("desktop user not found")
            downloads_dir = confined_download_dir(user.pw_dir)
            created_dir = not os.path.isdir(downloads_dir)
            os.makedirs(downloads_dir, exist_ok=True)
            if created_dir:
                os.chown(downloads_dir, user.pw_uid, user.pw_gid)
            expected_name = self.asset_name_template.format(
                version=self.check_version_from_asset(asset_name))
            if asset_name != expected_name or os.path.basename(asset_name) != asset_name:
                raise ValueError("release asset name does not match the expected plugin archive")
            destination = os.path.join(downloads_dir, asset_name)
            descriptor, temp_path = tempfile.mkstemp(
                prefix=f".{asset_name}.", dir=downloads_dir)
            with os.fdopen(descriptor, "wb") as output:
                written = self.download_to(download_url, output, timeout=60)
                output.flush()
                os.fsync(output.fileno())

            os.chown(temp_path, user.pw_uid, user.pw_gid)
            os.chmod(temp_path, 0o644)
            os.replace(temp_path, destination)
            temp_path = None

            self._info(f"update downloaded to {destination} ({written} bytes)")
            return {"success": True, "path": destination}
        except Exception as error:
            self._error(f"perform_update: {error}")
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            return {"success": False, "error": str(error)}

    def check_version_from_asset(self, asset_name: str) -> str:
        prefix, marker, suffix = self.asset_name_template.partition("{version}")
        if not marker or not asset_name.startswith(prefix) or not asset_name.endswith(suffix):
            return ""
        end = len(asset_name) - len(suffix) if suffix else len(asset_name)
        return asset_name[len(prefix):end]
