#!/usr/bin/env python3
"""Preview package releases with a short-lived cache for GitHub checks."""

import argparse
import hashlib
import posixpath
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "nvchecker.toml"
CACHE = ROOT / ".update-packages-cache.json"
TTL = 12 * 60 * 60
MAX_VERSION_LENGTH = 128
SCHEMA = 1
MAX_SOURCE = 1_500_000_000
KERNEL = "sys-kernel/kernel-cachyos"

UNSUPPORTED = {
    "dev-libs/tlottie": "recipe version is tied to AyuGram's pinned commit and patch",
    "media-libs/nanosvg": "recipe version is tied to a pinned commit",
    "media-libs/tg_owt": "recipe pins four commits, not a release",
    "net-libs/tde2e": "recipe version is tied to a pinned TDLib commit",
    "x11-apps/qt6ct": "Git tag does not verify the generic registry archive",
}
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")
KERNEL_RELEASE = re.compile(r"[0-9]+(?:\.[0-9]+)*-[0-9]+\Z")
KERNEL_RECIPE = re.compile(r"([0-9]+(?:\.[0-9]+)*)(?:-r([1-9][0-9]*))?\Z")


CDN = "https://cdn.cachyos.org/repo/x86_64_v3/cachyos-v3"
INDEX = f"{CDN}/cachyos-v3.db"
RECIPE = re.compile(r"kernel-cachyos-([0-9]+(?:\.[0-9]+)*)(?:-r([1-9][0-9]*))?\.exheres-0\Z")
RELEASE = re.compile(r"([0-9]+(?:\.[0-9]+)*)-([1-9][0-9]*)\Z")
MAX_DOWNLOAD = 1_500_000_000
MAX_INDEX = 10_000_000
MAX_INDEX_UNPACKED = 50_000_000


def recipes(root):
    found = {}
    problems = []
    kernel_count = 0
    for path in sorted((root / "packages").glob("*/*/*.exheres-0")):
        name = f"{path.parent.parent.name}/{path.parent.name}"
        stem = path.name.removesuffix(".exheres-0")
        prefix = path.parent.name + "-"
        if not stem.startswith(prefix) or not stem[len(prefix):]:
            problems.append(f"invalid recipe filename: {path.relative_to(root)}")
            continue
        version = stem[len(prefix):]
        if name == "sys-kernel/kernel-cachyos":
            kernel_count += 1
            if not KERNEL_RECIPE.fullmatch(version):
                problems.append(f"invalid kernel version: {version}")
                continue
            found[name] = version
        elif name in found:
            problems.append(f"multiple versions for {name}")
        else:
            found[name] = version
    if kernel_count > 1:
        problems.append("keep only one kernel-cachyos recipe")
    return found, problems


def compare(current, upstream):
    if not VERSION.fullmatch(current) or not VERSION.fullmatch(upstream):
        return "incomparable"
    a = tuple(map(int, current.split(".")))
    b = tuple(map(int, upstream.split(".")))
    length = max(len(a), len(b))
    a += (0,) * (length - len(a))
    b += (0,) * (length - len(b))
    return "update" if b > a else "ahead" if b < a else "current"


def kernel_release(root, version):
    path = root / "packages/sys-kernel/kernel-cachyos" / f"kernel-cachyos-{version}.exheres-0"
    try:
        recipe = path.read_text()
    except OSError:
        return None
    match = re.search(r'^MY_RELEASE="\$\{PV\}-([1-9][0-9]*)"$', recipe, re.MULTILINE)
    return f"{KERNEL_RECIPE.fullmatch(version).group(1)}-{match.group(1)}" if match else None


class UpdateError(Exception):
    pass


class LimitedReader:
    def __init__(self, stream, limit):
        self.stream = stream
        self.remaining = limit

    def read(self, size=-1):
        data = self.stream.read(min(size, self.remaining + 1) if size >= 0 else self.remaining + 1)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise UpdateError("Decompressed repository index exceeds size limit")
        return data


def release_key(value):
    match = RELEASE.fullmatch(value)
    if not match:
        raise UpdateError(f"Invalid upstream release: {value!r}")
    return tuple(map(int, match.group(1).split("."))), int(match.group(2))


def display_release(release):
    return release.removesuffix("-1")


def recipe_key(path):
    match = RECIPE.fullmatch(path.name)
    if not match:
        raise UpdateError(f"Unexpected kernel recipe: {path.name}")
    return tuple(map(int, match.group(1).split("."))), int(match.group(2) or 0)


def recipe_release(path):
    match = re.search(r'^MY_RELEASE="\$\{PV\}-([1-9][0-9]*)"$', path.read_text(), re.MULTILINE)
    if not match:
        raise UpdateError(f"Missing literal MY_RELEASE in {path.name}")
    version = RECIPE.fullmatch(path.name).group(1)
    return f"{version}-{match.group(1)}"


def published_release():
    try:
        request = urllib.request.Request(INDEX, headers={"User-Agent": "kernel-cachyos-updater/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read(MAX_INDEX + 1)
    except (urllib.error.URLError, TimeoutError) as error:
        raise UpdateError(f"Cannot download repository index: {error}") from error
    if len(data) > MAX_INDEX:
        raise UpdateError("Repository index exceeds size limit")

    packages = {}
    try:
        with tempfile.TemporaryDirectory(prefix="kernel-index-") as temporary:
            index = Path(temporary) / "cachyos-v3.db"
            index.write_bytes(data)
            with index.open("rb") as source, subprocess.Popen(
                ["zstd", "-dc"], stdin=source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            ) as decompressor:
                try:
                    stream = LimitedReader(decompressor.stdout, MAX_INDEX_UNPACKED)
                    with tarfile.open(fileobj=stream, mode="r|") as tar:
                        for member in tar:
                            match = re.fullmatch(
                                r"(linux-cachyos(?:-headers)?)-([0-9]+(?:\.[0-9]+)*-[1-9][0-9]*)/desc",
                                member.name,
                            )
                            if not match:
                                continue
                            if not member.isfile() or member.size > 64_000:
                                raise UpdateError(f"Invalid repository entry: {member.name}")
                            name, release = match.groups()
                            fields = {}
                            metadata = tar.extractfile(member).read().decode("utf-8")
                            for field in ("NAME", "VERSION", "FILENAME", "SHA256SUM", "ARCH"):
                                values = re.findall(rf"(?m)^%{field}%\n([^\n]+)$", metadata)
                                if len(values) != 1:
                                    raise UpdateError(f"Invalid {field} in repository entry: {member.name}")
                                fields[field] = values[0]
                            filename = f"{name}-{release}-x86_64_v3.pkg.tar.zst"
                            if (
                                fields["NAME"], fields["VERSION"], fields["FILENAME"], fields["ARCH"]
                            ) != (name, release, filename, "x86_64_v3") or not re.fullmatch(
                                r"[0-9a-f]{64}", fields["SHA256SUM"]
                            ):
                                raise UpdateError(f"Mismatched repository entry: {member.name}")
                            if (name, release) in packages:
                                raise UpdateError(f"Duplicate repository entry: {member.name}")
                            packages[name, release] = fields["SHA256SUM"]
                    for _ in iter(lambda: stream.read(1024 * 1024), b""):
                        pass
                    if decompressor.wait() != 0:
                        raise UpdateError("Cannot decompress repository index")
                except BaseException:
                    decompressor.kill()
                    decompressor.wait()
                    raise
    except (tarfile.TarError, UnicodeError, OSError, BrokenPipeError) as error:
        raise UpdateError(f"Cannot inspect repository index: {error}") from error
    releases = {release for name, release in packages if name == "linux-cachyos"} & {
        release for name, release in packages if name == "linux-cachyos-headers"
    }
    if not releases:
        raise UpdateError("Repository index has no matching kernel and headers")
    release = max(releases, key=release_key)
    return release, {name: packages[name, release] for name in ("linux-cachyos", "linux-cachyos-headers")}


def download(url, destination):
    digest = hashlib.sha256()
    size = 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "kernel-cachyos-updater/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response, destination.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise UpdateError(f"Archive exceeds size limit: {url}")
                digest.update(chunk)
                output.write(chunk)
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise UpdateError(f"Cannot download {url}: {error}") from error
    if not size:
        raise UpdateError(f"Empty archive: {url}")
    return digest.hexdigest()


def inspect_archive(archive, name, release):
    try:
        with archive.open("rb") as source, subprocess.Popen(
            ["zstd", "-dc"], stdin=source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        ) as decompressor:
            try:
                with tarfile.open(fileobj=decompressor.stdout, mode="r|") as tar:
                    regular = set()
                    metadata = None
                    for member in tar:
                        path = PurePosixPath(member.name.removeprefix("./"))
                        if path.is_absolute() or ".." in path.parts or str(path) == ".":
                            raise UpdateError(f"Unsafe archive member: {member.name!r}")
                        if member.issym() or member.islnk():
                            target = member.linkname
                            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(str(path)), target))
                            if target.startswith("/") or resolved == ".." or resolved.startswith("../"):
                                raise UpdateError(f"Unsafe archive link: {member.name!r}")
                        if member.isfile():
                            regular.add(str(path))
                        if str(path) == ".PKGINFO":
                            if not member.isfile() or member.size > 64_000:
                                raise UpdateError(f"Invalid .PKGINFO in {archive.name}")
                            metadata = tar.extractfile(member).read().decode("utf-8")
                for _ in iter(lambda: decompressor.stdout.read(1024 * 1024), b""):
                    pass
                if decompressor.wait() != 0:
                    raise UpdateError(f"Cannot decompress {archive.name}")
            except BaseException:
                decompressor.kill()
                decompressor.wait()
                raise
    except (tarfile.TarError, UnicodeError, OSError) as error:
        raise UpdateError(f"Cannot inspect {archive.name}: {error}") from error
    if metadata is None:
        raise UpdateError(f"{archive.name} has no .PKGINFO")
    fields = {}
    for line in metadata.splitlines():
        if " = " in line:
            key, value = line.split(" = ", 1)
            fields.setdefault(key, []).append(value)
    expected = {"pkgname": name, "pkgver": release, "arch": "x86_64_v3"}
    for key, value in expected.items():
        if fields.get(key) != [value]:
            raise UpdateError(f"{archive.name}: expected {key} = {value!r}, got {fields.get(key)!r}")
    module = f"usr/lib/modules/{release}-cachyos"
    if name == "linux-cachyos":
        required = (f"{module}/vmlinuz",)
        prefixes = (f"{module}/kernel/",)
    else:
        required = (f"{module}/build/.config", f"{module}/build/Module.symvers", f"{module}/build/System.map")
        prefixes = (f"{module}/vdso/",)
    if any(path not in regular for path in required) or any(
        not any(path.startswith(prefix) for path in regular) for prefix in prefixes
    ):
        raise UpdateError(f"{archive.name}: expected kernel/module/header paths are missing")


def replace_once(text, pattern, replacement):
    result, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise UpdateError(f"Recipe template did not match exactly once: {pattern}")
    return result


def render_recipe(template, release, hashes):
    pkgrel = release_key(release)[1]
    text = replace_once(template, r'^MY_RELEASE="\$\{PV\}-[1-9][0-9]*"$', f'MY_RELEASE="${{PV}}-{pkgrel}"')
    for variable, digest in (("MY_KERNEL", hashes["linux-cachyos"]), ("MY_HEADERS", hashes["linux-cachyos-headers"])):
        pattern = rf'("\${{{variable}}}"\) expected=)[0-9a-f]{{64}}( ;;)'
        text = replace_once(text, pattern, lambda match: f"{match.group(1)}{digest}{match.group(2)}")
    return text


def unchanged(path, original, identity):
    if path.is_symlink() or path.read_bytes() != original:
        return False
    current = path.stat()
    return (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)


def recipe_dirty(root, source, target):
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", str(source.relative_to(root)), str(target.relative_to(root))],
            cwd=root, capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise UpdateError(f"Cannot check kernel recipe status: {error}") from error
    return bool(result.stdout)


def update(root=ROOT, published=None):
    package_dir = root / "packages/sys-kernel/kernel-cachyos"
    recipes = list(package_dir.glob("*.exheres-0"))
    if len(recipes) != 1:
        raise UpdateError(f"Expected exactly one kernel recipe, found {len(recipes)}")
    latest = recipes[0]
    current = recipe_release(latest)
    if recipe_key(latest)[1] != release_key(current)[1] - 1:
        raise UpdateError(f"Recipe revision does not match upstream pkgrel: {latest.name}")
    template = latest.read_text()
    if not re.search(r'^SLOT="0"$', template, re.MULTILINE):
        raise UpdateError(f"Kernel recipe must use SLOT=0: {latest.name}")
    original = latest.read_bytes()
    identity = latest.stat()
    if (root / ".git").exists() and recipe_dirty(root, latest, latest):
        raise UpdateError(f"Kernel recipe has local changes: {latest}")
    upstream, published_hashes = published if published is not None else published_release()
    release_key(upstream)
    if (not isinstance(published_hashes, dict) or set(published_hashes) != {"linux-cachyos", "linux-cachyos-headers"}
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in published_hashes.values())):
        raise UpdateError("Invalid published kernel hashes")
    if release_key(upstream) == release_key(current):
        print(f"Already current: {upstream}")
        return
    if release_key(upstream) < release_key(current):
        raise UpdateError(f"Published release {upstream} is older than recipe release {current}")
    version = upstream.rsplit("-", 1)[0]
    if version == current.rsplit("-", 1)[0] and release_key(upstream)[1] != release_key(current)[1] + 1:
        raise UpdateError("Upstream pkgrel skipped a release; review missing kernel revisions")
    revision = recipe_key(latest)[1] + 1 if version == current.rsplit("-", 1)[0] else 0
    suffix = f"-r{revision}" if revision else ""
    target = package_dir / f"kernel-cachyos-{version}{suffix}.exheres-0"
    if target.exists() or target.is_symlink():
        raise UpdateError(f"Refusing to overwrite {target}")
    if (root / ".git").exists() and recipe_dirty(root, latest, target):
        raise UpdateError(f"Kernel recipe has local changes: {latest}")
    if not unchanged(latest, original, identity):
        raise UpdateError("Kernel recipe changed while fetching index")

    hashes = {}
    with tempfile.TemporaryDirectory(prefix="kernel-cachyos-") as temporary:
        for name in ("linux-cachyos", "linux-cachyos-headers"):
            directory = Path(temporary) / name
            directory.mkdir()
            archive = directory / f"{name}-{upstream}-x86_64_v3.pkg.tar.zst"
            hashes[name] = download(f"{CDN}/{archive.name}", archive)
            if hashes[name] != published_hashes[name]:
                raise UpdateError(f"{archive.name}: SHA-256 differs from repository index")
            inspect_archive(archive, name, upstream)

    content = render_recipe(template, upstream, hashes)
    with tempfile.TemporaryDirectory(prefix=".kernel-recipe-", dir=package_dir) as temporary:
        staged = Path(temporary) / target.name
        staged.write_text(content)
        result = subprocess.run(["bash", "-n", str(staged)], capture_output=True, text=True, check=False)
        if result.returncode:
            raise UpdateError(f"Generated recipe failed bash -n: {result.stderr.strip()}")
        if not unchanged(latest, original, identity) or target.exists() or target.is_symlink():
            raise UpdateError("Kernel recipe changed while preparing update")
        if (root / ".git").exists() and recipe_dirty(root, latest, target):
            raise UpdateError("Kernel recipe changed while preparing update")
        try:
            os.link(staged, target)
        except FileExistsError as error:
            raise UpdateError(f"Refusing to overwrite {target}") from error
        published_identity = target.stat()
        try:
            if not unchanged(latest, original, identity):
                raise UpdateError("Kernel recipe changed during publication")
            latest.unlink()
        except (OSError, UpdateError) as error:
            try:
                current_target = target.stat()
                if (current_target.st_ino, current_target.st_dev) == (published_identity.st_ino, published_identity.st_dev):
                    target.unlink()
            except FileNotFoundError:
                pass
            raise UpdateError(f"Could not replace old recipe: {error}") from error
    print(f"Created {target.relative_to(root)} for upstream {upstream}")
    print(f"Removed {latest.relative_to(root)}")
    for name, digest in hashes.items():
        print(f"{name}: SHA-256 {digest} (verify against independently published hashes)")
    return upstream


def fingerprint(entry, global_config):
    settings = {"entry": entry, "global": global_config}
    return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_cache(path):
    try:
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, UnicodeError, ValueError):
        return {}
    if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != SCHEMA or not isinstance(data.get("entries"), dict):
        return {}
    return data["entries"]


def cached_version(record, entry, global_config, now):
    if not isinstance(record, dict):
        return None
    version, stamp = record.get("version"), record.get("time")
    if (record.get("config") != fingerprint(entry, global_config) or not isinstance(version, str)
            or len(version) > MAX_VERSION_LENGTH or not VERSION.fullmatch(version)
            or type(stamp) not in (int, float)
            or not 0 <= now - stamp < TTL):
        return None
    return version, now - stamp


def save_cache(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".update-packages-cache-", delete=False) as file:
            temporary = Path(file.name)
            os.chmod(temporary, 0o600)
            json.dump({"schema": SCHEMA, "entries": entries}, file, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def check_entry(name, config_path):
    try:
        completed = subprocess.run(
            ["nvchecker", "--logger", "json", "--failures", "--tries", "1", "--entry", name,
             "-c", str(config_path)],
            text=True, capture_output=True, check=False, timeout=90,
        )
    except subprocess.TimeoutExpired:
        return None, "nvchecker timed out", True
    except OSError as error:
        return None, f"cannot run nvchecker: {error}", True
    if completed.returncode not in (0, 3):
        return None, f"nvchecker exited with status {completed.returncode}: {completed.stderr.strip()}", True
    version = None
    error = None
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            error = "malformed nvchecker event"
            continue
        if not isinstance(event, dict):
            error = "malformed nvchecker event"
            continue
        if event.get("level") == "error" and event.get("name") != name:
            error = "unassigned nvchecker error"
        if event.get("name") != name:
            continue
        if event.get("level") == "error":
            error = event.get("event") if isinstance(event.get("event"), str) and event.get("event") else "unknown error"
        elif event.get("event") == "updated":
            candidate = event.get("version")
            if (event.get("level") != "info" or not isinstance(candidate, str)
                    or len(candidate) > MAX_VERSION_LENGTH or not VERSION.fullmatch(candidate)
                    or version is not None):
                error = "invalid version event"
            else:
                version = candidate
    if error:
        return None, error, False
    if version is None:
        return None, completed.stderr.strip() or "no valid version returned", False
    return version, None, False


def preview(root=ROOT, config_path=CONFIG, cache_path=CACHE, refresh=False, now=None, versions_out=None, kernel_out=None):
    now = time.time() if now is None else now
    with config_path.open("rb") as file:
        config = tomllib.load(file)
    if not isinstance(config.get("__config__", {}), dict):
        raise ValueError("invalid nvchecker global configuration")
    if {"oldver", "newver"} & config.get("__config__", {}).keys():
        raise ValueError("refusing nvchecker config with version file writes")
    current, problems = recipes(root)
    entries = set(config) - {"__config__"}
    problems.extend(f"unmapped recipe: {name}" for name in sorted(current.keys() - entries - UNSUPPORTED.keys() - {KERNEL}))
    problems.extend(f"mapping without recipe: {name}" for name in sorted((entries | UNSUPPORTED.keys()) - current.keys()))
    problems.extend(f"both configured and unsupported: {name}" for name in sorted(entries & UNSUPPORTED.keys()))
    if KERNEL in entries:
        problems.append("kernel must use published repository index, not nvchecker")
    if problems:
        raise ValueError("; ".join(problems))

    cache = load_cache(cache_path)
    updated = dict(cache)
    rows = {}
    global_failure = False
    for name in sorted(entries):
        entry = config[name]
        age = None
        version = None
        if isinstance(entry, dict) and entry.get("source") == "github" and not refresh:
            hit = cached_version(cache.get(name), entry, config.get("__config__", {}), now)
            if hit:
                version, age = hit
        error = None
        if version is None:
            version, error, fatal = check_entry(name, config_path)
            global_failure |= fatal
            if (version is not None and isinstance(entry, dict) and entry.get("source") == "github"):
                updated[name] = {"version": version, "time": now,
                                 "config": fingerprint(entry, config.get("__config__", {}))}
        if error:
            rows[name] = ("failed", error)
        else:
            if versions_out is not None:
                versions_out[name] = version
            status = compare(current[name], version)
            rows[name] = ("failed", f"unrecognized version {version!r}") if status == "incomparable" else (
                status, f"{current[name]} -> {version}" + (f" (GitHub cache: {int(age // 60)}m old)" if age is not None else ""))

    for name in sorted(current.keys() - entries):
        if name in UNSUPPORTED:
            rows[name] = ("untracked", UNSUPPORTED[name])
        elif name == KERNEL:
            release = kernel_release(root, current[name])
            if release is None:
                rows[name] = ("failed", "unrecognized recipe release")
                continue
            try:
                snapshot = published_release()
                published, _ = snapshot
                if not KERNEL_RELEASE.fullmatch(published):
                    raise ValueError(f"unrecognized published release {published!r}")
                status = compare(release.replace("-", "."), published.replace("-", "."))
                if kernel_out is not None:
                    kernel_out.append(snapshot)
                rows[name] = (status, f"{display_release(release)} -> {display_release(published)}")
            except (UpdateError, OSError, subprocess.SubprocessError, ValueError) as error:
                rows[name] = ("failed", str(error))
    if updated != cache:
        save_cache(cache_path, updated)
    return rows, global_failure


class PreparationError(Exception):
    pass


def checksum_url(recipe, package, version):
    guards = list(re.finditer(r'\[\[ \$\{checksum%% \*\} == ([0-9a-f]{64}) \]\]', recipe))
    if (len(guards) != 1 or len(re.findall(r'\bsha256sum\b', recipe)) != 1
            or len(re.findall(r'(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])', recipe)) != 1):
        raise PreparationError("Cannot identify exactly one SHA-256 guard")
    downloads = re.findall(r'(?m)^DOWNLOADS="([^"\n]+)"$', recipe)
    if len(downloads) != 1 or "\n" in downloads[0]:
        raise PreparationError("Cannot identify one literal download URL")
    match = re.fullmatch(r'(https://[^\s]+) -> \$\{PNV\}(\.[A-Za-z0-9]+)', downloads[0])
    if not match or not re.search(r'checksum=\$\(sha256sum "\$\{FETCHEDDIR\}/\$\{PNV\}'
                                   + re.escape(match.group(2)) + r'"\) \|\| die', recipe):
        raise PreparationError("Checksum source does not match download")
    url = match.group(1).replace("${PV}", version).replace("${PNV}", f"{package}-{version}")
    if "${" in url or not safe_download_url(url):
        raise PreparationError("Unrecognized checksum download URL")
    return url, guards[0], not re.search(r'\$\{PV\}|\$\{PNV\}', match.group(1))


def safe_download_url(url, origin=None):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    return (parsed.scheme == "https" and bool(host) and not parsed.username
            and not parsed.password and parsed.port is None and not parsed.fragment
            and (not parsed.query or (origin == "github.com" and host == "release-assets.githubusercontent.com"))
            and host != "localhost" and "." in host
            and (origin is None or host == origin or
                 (origin == "github.com" and host == "release-assets.githubusercontent.com")))


class ChecksumRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin):
        self.origin = urllib.parse.urlsplit(origin).hostname

    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not safe_download_url(newurl, self.origin):
            raise PreparationError(f"Unexpected checksum redirect: {newurl}")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def download_checksum(url):
    digest = hashlib.sha256()
    opener = urllib.request.build_opener(ChecksumRedirect(url))
    try:
        with tempfile.TemporaryDirectory(prefix="package-checksum-") as temporary:
            destination = Path(temporary) / "archive"
            with opener.open(urllib.request.Request(url, headers={"User-Agent": "gurov-package-updater/1.0"}), timeout=30) as response:
                if not safe_download_url(response.geturl(), urllib.parse.urlsplit(url).hostname):
                    raise PreparationError("Unexpected checksum download origin")
                length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
                if length is not None and (not length.isdecimal() or int(length) > MAX_SOURCE):
                    raise PreparationError("Invalid checksum source length")
                size = 0
                with destination.open("xb") as output:
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_SOURCE:
                            raise PreparationError("Checksum source exceeds size limit")
                        digest.update(chunk)
                        output.write(chunk)
                if size == 0 or (length is not None and size != int(length)):
                    raise PreparationError("Incomplete checksum source")
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        raise PreparationError(f"Cannot download checksum source: {error}") from error
    return digest.hexdigest()


def prepare_recipe(root, name, version):
    if name == "net-im/element-desktop":
        raise PreparationError("Element source layout and Electron pin require manual review")
    if name == KERNEL or name in UNSUPPORTED or not re.fullmatch(r'[A-Za-z0-9+_.-]+/[A-Za-z0-9+_.-]+', name):
        raise PreparationError("Unsupported package selector")
    if not VERSION.fullmatch(version) or len(version) > MAX_VERSION_LENGTH:
        raise PreparationError("Invalid package version")
    package = name.split("/")[1]
    directory = root / "packages" / name
    target = directory / f"{package}-{version}.exheres-0"
    candidates = list(directory.glob("*.exheres-0"))
    if len(candidates) == 1 and candidates[0] == target:
        raise PreparationError("Package does not have a newer numeric version")
    sources = [path for path in candidates if path != target]
    if len(sources) != 1:
        raise PreparationError("Expected exactly one source recipe")
    source = sources[0]
    if source.is_symlink() or not source.is_file():
        raise PreparationError("Source recipe is not a regular file")
    prefix = f"{package}-"
    if not source.name.startswith(prefix):
        raise PreparationError("Unexpected source recipe filename")
    current = source.name.removeprefix(prefix).removesuffix(".exheres-0")
    if compare(current, version) != "update":
        raise PreparationError("Package does not have a newer numeric version")
    if target.exists() or target.is_symlink():
        raise PreparationError(f"Target already exists: {target}")
    git_name = ["git", "ls-files", "--error-unmatch", "--", str(source.relative_to(root))]
    try:
        subprocess.run(git_name, cwd=root, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise PreparationError(f"Source recipe is not tracked: {source}") from error

    def check_status():
        try:
            dirty = subprocess.run(["git", "status", "--porcelain", "--", str(source.relative_to(root)),
                                    str(target.relative_to(root))], cwd=root, text=True, capture_output=True, check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise PreparationError(f"Cannot check recipe status: {error}") from error
        if dirty.stdout:
            raise PreparationError("Recipe has local changes; review manually")

    check_status()
    identity = source.stat()
    original = source.read_bytes()
    if not unchanged(source, original, identity):
        raise PreparationError("Recipe changed while reading baseline")
    recipe = original.decode("utf-8")
    if re.search(r'\$\{FILES\}/[^"\s]*\$\{(?:PV|PNV)\}', recipe):
        raise PreparationError("Version-dependent files require manual review")
    versionless = False
    if re.search(r'(?i)sha[-_]?256|(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])', recipe):
        url, guard, versionless = checksum_url(recipe, package, version)
        digest = download_checksum(url)
        recipe = recipe[:guard.start(1)] + digest + recipe[guard.end(1):]
    with tempfile.TemporaryDirectory(prefix=".package-recipe-", dir=directory) as temporary:
        staged = Path(temporary) / target.name
        staged.write_bytes(recipe.encode("utf-8"))
        syntax = subprocess.run(["bash", "-n", str(staged)], text=True, capture_output=True, check=False)
        if syntax.returncode:
            raise PreparationError(f"Generated recipe failed bash -n: {syntax.stderr.strip()}")
        if not unchanged(source, original, identity) or target.exists() or target.is_symlink():
            raise PreparationError("Recipe changed while preparing update")
        check_status()
        try:
            os.link(staged, target)
        except FileExistsError as error:
            raise PreparationError(f"Target already exists: {target}") from error
        published = target.stat()
        try:
            if not unchanged(source, original, identity):
                raise PreparationError("Recipe changed during publication")
            check_status_after_publish = subprocess.run(
                ["git", "status", "--porcelain", "--", str(source.relative_to(root))],
                cwd=root, text=True, capture_output=True, check=True)
            if check_status_after_publish.stdout:
                raise PreparationError("Recipe changed during publication")
            source.unlink()
        except (OSError, PreparationError, subprocess.SubprocessError) as error:
            try:
                current_target = target.stat()
                if (current_target.st_ino, current_target.st_dev) == (published.st_ino, published.st_dev):
                    target.unlink()
            except FileNotFoundError:
                pass
            raise PreparationError(f"Could not replace old recipe: {error}") from error
    if versionless:
        print(f"Note: {package} download URL does not include version; verify archive identity before commit")
    return f"update {package} to {version}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="refresh GitHub version checks")
    parser.add_argument("--verbose", action="store_true", help="show every package and cache age")
    parser.add_argument("--apply", nargs="+", metavar="PACKAGE", help="prepare selected updates")
    parser.add_argument("--all-eligible", action="store_true", help="prepare all reported updates")
    args = parser.parse_args(argv)
    if args.apply and args.all_eligible:
        parser.error("choose --apply or --all-eligible")
    if args.apply and len(args.apply) != len(set(args.apply)):
        parser.error("duplicate package selector")
    versions = {}
    kernel = []
    try:
        rows, fatal = preview(refresh=args.refresh, versions_out=versions, kernel_out=kernel)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"Cannot preview updates: {error}", file=sys.stderr)
        return 1
    labels = {"update": "to update", "failed": "failed", "ahead": "ahead of upstream",
              "untracked": "not tracked", "current": "up to date"}
    counts = {status: sum(row_status == status for row_status, _ in rows.values()) for status in labels}
    print(", ".join(f"{counts[status]} {label}" for status, label in labels.items() if counts[status]) or "No packages found")
    shown = rows.items() if args.verbose else ((name, row) for name, row in rows.items() if row[0] in ("update", "failed"))
    shown = sorted(shown)
    if shown:
        print()
        for name, (status, detail) in shown:
            print(f"{status:9} {name:39} {detail}")
    selected = set(args.apply or []) if not args.all_eligible else {
        name for name, (status, _) in rows.items() if status == "update"
    }
    invalid = selected - rows.keys()
    if invalid:
        print(f"Unknown packages: {', '.join(sorted(invalid))}", file=sys.stderr)
        return 1
    unsupported = selected & UNSUPPORTED.keys()
    if unsupported:
        print(f"Not eligible for automatic updates: {', '.join(sorted(unsupported))}", file=sys.stderr)
        return 1
    if fatal and selected:
        print("Refusing apply after nvchecker invocation failure", file=sys.stderr)
        return 1
    failed = fatal or any(status == "failed" for status, _ in rows.values())
    for name in sorted(selected):
        status, detail = rows.get(name, ("failed", "no recipe"))
        if status != "update":
            print(f"skip {name}: {status} ({detail})")
            failed = failed or status == "failed"
            continue
        try:
            if name == KERNEL:
                if not kernel:
                    raise PreparationError("no validated kernel index snapshot")
                release = update(ROOT, published=kernel[0])
                if release is not None:
                    print(f"Draft: update kernel-cachyos to {display_release(release)}")
            else:
                version = versions.get(name)
                if version is None:
                    raise PreparationError("no validated version")
                print(f"Draft: {prepare_recipe(ROOT, name, version)}")
        except (PreparationError, UpdateError, OSError, subprocess.SubprocessError) as error:
            print(f"Cannot prepare {name}: {error}", file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
