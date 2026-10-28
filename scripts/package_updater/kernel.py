import hashlib
import posixpath
import os
import re
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

from .common import UpdateError, recipe_dirty, recipes, replace_once, unchanged, ROOT, KERNEL_RECIPE, CDN, INDEX, RECIPE, RELEASE, MAX_DOWNLOAD, MAX_INDEX, MAX_INDEX_UNPACKED


def kernel_release(root, version):
    path = root / "packages/sys-kernel/kernel-cachyos" / f"kernel-cachyos-{version}.exheres-0"
    try:
        recipe = path.read_text()
    except OSError:
        return None
    match = re.search(r'^MY_RELEASE="\$\{PV\}-([1-9][0-9]*)"$', recipe, re.MULTILINE)
    return f"{KERNEL_RECIPE.fullmatch(version).group(1)}-{match.group(1)}" if match else None


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
                                r"(linux-cachyos-eevdf-lto(?:-headers)?)-([0-9]+(?:\.[0-9]+)*-[1-9][0-9]*)/desc",
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
    releases = {release for name, release in packages if name == "linux-cachyos-eevdf-lto"} & {
        release for name, release in packages if name == "linux-cachyos-eevdf-lto-headers"
    }
    if not releases:
        raise UpdateError("Repository index has no matching kernel and headers")
    release = max(releases, key=release_key)
    return release, {name: packages[name, release] for name in ("linux-cachyos-eevdf-lto", "linux-cachyos-eevdf-lto-headers")}


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
    module = f"usr/lib/modules/{release}-cachyos-eevdf-lto"
    if name == "linux-cachyos-eevdf-lto":
        required = (f"{module}/vmlinuz",)
        prefixes = (f"{module}/kernel/",)
    else:
        required = (f"{module}/build/.config", f"{module}/build/Module.symvers", f"{module}/build/System.map")
        prefixes = (f"{module}/vdso/",)
    if any(path not in regular for path in required) or any(
        not any(path.startswith(prefix) for path in regular) for prefix in prefixes
    ):
        raise UpdateError(f"{archive.name}: expected kernel/module/header paths are missing")


def render_recipe(template, release, hashes):
    pkgrel = release_key(release)[1]
    text = replace_once(template, r'^MY_RELEASE="\$\{PV\}-[1-9][0-9]*"$', f'MY_RELEASE="${{PV}}-{pkgrel}"')
    for variable, digest in (("MY_KERNEL", hashes["linux-cachyos-eevdf-lto"]), ("MY_HEADERS", hashes["linux-cachyos-eevdf-lto-headers"])):
        pattern = rf'("\${{{variable}}}"\) expected=)[0-9a-f]{{64}}( ;;)'
        text = replace_once(text, pattern, lambda match: f"{match.group(1)}{digest}{match.group(2)}")
    return text


def update(root=ROOT, published=None):
    package_dir = root / "packages/sys-kernel/kernel-cachyos"
    recipes = [path for path in package_dir.glob("*.exheres-0")
               if 'linux-cachyos-lts-' not in path.read_text()]
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
    if (not isinstance(published_hashes, dict) or set(published_hashes) != {"linux-cachyos-eevdf-lto", "linux-cachyos-eevdf-lto-headers"}
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
        for name in ("linux-cachyos-eevdf-lto", "linux-cachyos-eevdf-lto-headers"):
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


