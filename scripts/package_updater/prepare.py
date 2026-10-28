import base64
import difflib
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

from .common import PreparationError
from .common import confirm
from .common import compare, recipes, unchanged, MAX_VERSION_LENGTH, MAX_SOURCE, KERNEL, UNSUPPORTED, VERSION


def checksum_url(recipe, package, version):
    guards = list(re.finditer(r'\[\[ \$\{checksum%% \*\} == ([0-9a-f]{64}) \]\]', recipe))
    if (len(guards) != 1 or len(re.findall(r'\bsha256sum\b', recipe)) != 1
            or len(re.findall(r'(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])', recipe)) != 1):
        raise PreparationError("Cannot identify exactly one SHA-256 guard")
    downloads = re.findall(r'(?m)^DOWNLOADS="([^"\n]+)"$', recipe)
    if len(downloads) != 1 or "\n" in downloads[0]:
        raise PreparationError("Cannot identify one literal download URL")
    match = re.fullmatch(r'(https://[^\s]+) -> \$\{PNV\}(\.[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)', downloads[0])
    if not match or not re.search(r'checksum=\$\(sha256sum "\$\{FETCHEDDIR\}/\$\{PNV\}'
                                   + re.escape(match.group(2)) + r'"\) \|\| die', recipe):
        raise PreparationError("Checksum source does not match download")
    url = match.group(1).replace("${PV}", version).replace("${PNV}", f"{package}-{version}")
    if "${" in url or not safe_download_url(url):
        raise PreparationError("Unrecognized checksum download URL")
    return url, guards[0], not re.search(r'\$\{PV\}|\$\{PNV\}', match.group(1))


def safe_download_url(url, origin=None):
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        return (parsed.scheme == "https" and bool(host) and not parsed.username
                and not parsed.password and parsed.port is None and not parsed.fragment
                and (not parsed.query or (origin == "github.com" and host == "release-assets.githubusercontent.com"))
                and host != "localhost" and "." in host
                and (origin is None or host == origin or
                     (origin == "github.com" and host == "release-assets.githubusercontent.com")))
    except ValueError:
        return False


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


def numeric_version(version):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)*)(?:-r([0-9]+))?", version)
    if not match:
        return None
    return tuple(map(int, match[1].split("."))) + (int(match[2] or 0),)


def current_version(root, name):
    return recipes(root)[0].get(name)


def official_config(root):
    path = root / "scripts/recipe-origins.json"
    return json.loads(path.read_text()) if path.exists() else {}


def official_files(origin, version):
    repository = origin["repository"]
    if not re.fullmatch(r"https://gitlab\.exherbo\.org/(?:exherbo|exherbo-devs)/[A-Za-z0-9_-]+", repository):
        raise PreparationError("Invalid official repository")
    url = repository + "/-/archive/master/source.tar.gz"
    try:
        with tempfile.TemporaryDirectory(prefix="gurov-official-") as temporary:
            archive = Path(temporary) / "source.tar.gz"
            opener = urllib.request.build_opener(ChecksumRedirect(url))
            with opener.open(url, timeout=30) as response, archive.open("xb") as output:
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 32 * 1024 * 1024:
                        raise PreparationError("Official archive exceeds size limit")
                    output.write(chunk)
            files = {}
            prefix = "packages/" + origin["package"] + "/"
            with tarfile.open(archive) as tar:
                total = 0
                archive_total = 0
                for count, member in enumerate(tar, 1):
                    archive_total += member.size
                    if count > 100000 or archive_total > 256 * 1024 * 1024:
                        raise PreparationError("Official archive exceeds unpacked limits")
                    relative = member.name.partition("/")[2]
                    if not relative.startswith(prefix):
                        continue
                    path = PurePosixPath(relative[len(prefix):])
                    if member.isdir():
                        continue
                    if not member.isfile() or path.is_absolute() or ".." in path.parts:
                        raise PreparationError("Unsafe official archive member")
                    if path.name.endswith(".exheres-0") and version is not None and path.name != origin["package"].split("/")[1] + "-" + version + ".exheres-0":
                        continue
                    total += member.size
                    if total > 8 * 1024 * 1024:
                        raise PreparationError("Official package exceeds size limit")
                    files[str(path)] = tar.extractfile(member).read()
            recipe = origin["package"].split("/")[1] + "-" + str(version) + ".exheres-0"
            if version is not None and recipe not in files:
                raise PreparationError(f"Official recipe not available: {recipe}")
            return files
    except (OSError, urllib.error.URLError, tarfile.TarError, TimeoutError) as error:
        raise PreparationError(f"Cannot fetch official recipe: {error}") from error


def merge_recipe(base, local, incoming):
    if local == base:
        return incoming
    if incoming == base or local == incoming:
        return local
    if None in (base, local, incoming) or b"\0" in base + local + incoming:
        raise PreparationError("Official overlay add/delete or binary conflict")
    with tempfile.TemporaryDirectory(prefix="gurov-merge-") as temporary:
        paths = [Path(temporary) / name for name in ("local", "base", "incoming")]
        for path, data in zip(paths, (local, base, incoming)):
            path.write_bytes(data)
        result = subprocess.run(["git", "merge-file", "-p", *map(str, paths)], capture_output=True, timeout=30)
        if result.returncode:
            raise PreparationError("Official overlay merge conflict; rebase manually")
        return result.stdout


def prepare_official(root, name, version, origin):
    if numeric_version(version) is None or not re.fullmatch(r"[A-Za-z0-9+_.-]+/[A-Za-z0-9+_.-]+", name):
        raise PreparationError("Invalid official package selector or version")
    directory = root / "packages" / name
    for path in origin["baseline"]:
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise PreparationError("Invalid provenance path")
    if not re.fullmatch(r"[0-9a-f]{40}", origin["commit"]):
        raise PreparationError("Missing pinned provenance")
    if directory.is_symlink() or any(p.is_symlink() for p in directory.parents if p != root.parent):
        raise PreparationError("Symlinked package directory")
    snapshot = {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    if any(p.is_symlink() for p in directory.rglob("*")):
        raise PreparationError("Symlinked package payload")
    base = {path: base64.b64decode(data, validate=True) for path, data in origin["baseline"].items()}
    incoming = official_files(origin, version)
    old_recipe = origin["recipe"]
    upstream_package = origin["package"].split("/")[1]
    new_recipe = upstream_package + "-" + version + ".exheres-0"
    package = name.split("/")[1]
    current = current_version(root, name)
    local_recipe = package + "-" + current + ".exheres-0" if current else origin["local_recipe"]
    target = package + "-" + version + ".exheres-0"
    if target != local_recipe and target in snapshot:
        raise PreparationError("Target recipe already exists")
    if origin["mode"] == "derived":
        if hashlib.sha256(snapshot.get(local_recipe, b"")).hexdigest() != origin["local_sha256"]:
            raise PreparationError("Independent tools split changed since provenance review")
        if target == local_recipe:
            raise PreparationError("Target recipe already exists")
        raise PreparationError("Independent systemd tools split requires verified build/install rebase; official systemd recipe is not a replacement")
    local = dict(snapshot)
    if local_recipe in local:
        local_data = local.pop(local_recipe)
        local[old_recipe] = local_data
    if new_recipe != old_recipe:
        incoming[old_recipe] = incoming.pop(new_recipe)
    merged = dict(snapshot)
    for path in base.keys() | incoming.keys():
        value = merge_recipe(base.get(path), local.get(path) if snapshot else base.get(path), incoming.get(path))
        destination = target if path == old_recipe else path
        if value is None:
            merged.pop(destination, None)
        else:
            merged[destination] = value
    if target != local_recipe:
        merged.pop(local_recipe, None)
    recipe_text = merged[target].decode("utf-8")
    for referenced in re.findall(r'\$\{FILES\}/([^"\s]+)', recipe_text):
        referenced = referenced.replace("${PV}", version).replace("${PNV}", package + "-" + version)
        if "$" in referenced or "files/" + referenced not in merged:
            raise PreparationError(f"Local patch requires verified rebase: {referenced}")
    return directory, snapshot, merged


def official_diff(plan):
    directory, before, after = plan
    output = []
    for path in sorted(before.keys() | after.keys()):
        if before.get(path) == after.get(path):
            continue
        output.extend(difflib.unified_diff(before.get(path, b"").decode("utf-8", "replace").splitlines(True),
                                         after.get(path, b"").decode("utf-8", "replace").splitlines(True),
                                         fromfile=str(directory / path), tofile=str(directory / path)))
    return "".join(output)


def write_official(plan):
    directory, before, after = plan
    current = {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    if current != before or any(p.is_symlink() for p in directory.rglob("*")):
        raise PreparationError("Package changed since review")
    for path in sorted(before.keys() | after.keys()):
        if before.get(path) == after.get(path):
            continue
        destination = directory / path
        if path not in after:
            destination.unlink()
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as file:
                temporary = Path(file.name)
                file.write(after[path])
            temporary.chmod(0o644)
            os.replace(temporary, destination)


def prepare_recipe(root, name, version, review=False):
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
    if review:
        print("".join(difflib.unified_diff(original.decode().splitlines(True), recipe.splitlines(True),
                                       fromfile=str(source), tofile=str(target))))
        print(f"Rename {source.name} -> {target.name}")
        if not confirm("Apply this update? [y/N] "):
            return None
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


