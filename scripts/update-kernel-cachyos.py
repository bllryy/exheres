#!/usr/bin/env python3
"""Prepare a CachyOS kernel recipe from the latest AUR package release."""

import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
AUR_URL = "https://aur.archlinux.org/rpc/v5/info/linux-cachyos"
MIRROR = "https://mirror.cachyos.org/repo/x86_64_v3/cachyos-v3"
RECIPE = re.compile(r"kernel-cachyos-([0-9]+(?:\.[0-9]+)*)(?:-r([1-9][0-9]*))?\.exheres-0\Z")
RELEASE = re.compile(r"([0-9]+(?:\.[0-9]+)*)-([1-9][0-9]*)\Z")
MAX_DOWNLOAD = 1_500_000_000


class UpdateError(Exception):
    pass


def release_key(value):
    match = RELEASE.fullmatch(value)
    if not match:
        raise UpdateError(f"Invalid upstream release: {value!r}")
    return tuple(map(int, match.group(1).split("."))), int(match.group(2))


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


def latest_aur_release():
    try:
        with urllib.request.urlopen(AUR_URL, timeout=20) as response:
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise UpdateError(f"AUR request failed (rate limit or network issue): {error}") from error
    if not isinstance(data, dict) or data.get("resultcount") != 1 or not isinstance(data.get("results"), list) or len(data["results"]) != 1:
        raise UpdateError("AUR did not return exactly one linux-cachyos result")
    result = data["results"][0]
    if not isinstance(result, dict) or result.get("Name") != "linux-cachyos" or not isinstance(result.get("Version"), str):
        raise UpdateError("AUR returned an unexpected package or version")
    release_key(result["Version"])
    return result["Version"]


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
                    paths = set()
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
                        paths.add(str(path))
                        if member.isfile():
                            regular.add(str(path))
                        if str(path) == ".PKGINFO":
                            if not member.isfile() or member.size > 64_000:
                                raise UpdateError(f"Invalid .PKGINFO in {archive.name}")
                            metadata = tar.extractfile(member).read().decode("utf-8")
                # tar can stop at the end-of-archive marker before zstd reaches EOF.
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


def update(root=ROOT):
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
    upstream = latest_aur_release()
    if release_key(upstream) == release_key(current):
        print(f"Already current: {upstream}")
        return
    if release_key(upstream) < release_key(current):
        raise UpdateError(f"AUR release {upstream} is older than recipe release {current}")
    version = upstream.rsplit("-", 1)[0]
    if version == current.rsplit("-", 1)[0] and release_key(upstream)[1] != release_key(current)[1] + 1:
        raise UpdateError("Upstream pkgrel skipped a release; review missing kernel revisions")
    revision = recipe_key(latest)[1] + 1 if version == current.rsplit("-", 1)[0] else 0
    suffix = f"-r{revision}" if revision else ""
    target = package_dir / f"kernel-cachyos-{version}{suffix}.exheres-0"
    if target.exists():
        raise UpdateError(f"Refusing to overwrite {target}")

    hashes = {}
    with tempfile.TemporaryDirectory(prefix="kernel-cachyos-") as temporary:
        for name in ("linux-cachyos", "linux-cachyos-headers"):
            directory = Path(temporary) / name
            directory.mkdir()
            archive = directory / f"{name}-{upstream}-x86_64_v3.pkg.tar.zst"
            hashes[name] = download(f"{MIRROR}/{archive.name}", archive)
            inspect_archive(archive, name, upstream)

    content = render_recipe(template, upstream, hashes)
    with tempfile.TemporaryDirectory(prefix=".kernel-recipe-", dir=package_dir) as temporary:
        staged = Path(temporary) / target.name
        staged.write_text(content)
        result = subprocess.run(["bash", "-n", str(staged)], capture_output=True, text=True, check=False)
        if result.returncode:
            raise UpdateError(f"Generated recipe failed bash -n: {result.stderr.strip()}")
        # Publish a complete file without replacing one another process created.
        os.link(staged, target)
        try:
            latest.unlink()
        except OSError as error:
            target.unlink()
            raise UpdateError(f"Could not remove old recipe: {error}") from error
    print(f"Created {target.relative_to(root)} for upstream {upstream}")
    print(f"Removed {latest.relative_to(root)}")
    for name, digest in hashes.items():
        print(f"{name}: SHA-256 {digest} (verify against independently published hashes)")


if __name__ == "__main__":
    try:
        update()
    except (UpdateError, OSError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
