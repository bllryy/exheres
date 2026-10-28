import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
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
            if 'linux-cachyos-lts-' in path.read_text():
                continue
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
    current = re.sub(r"-r[1-9][0-9]*$", "", current)
    if not VERSION.fullmatch(current) or not VERSION.fullmatch(upstream):
        return "incomparable"
    a = tuple(map(int, current.split(".")))
    b = tuple(map(int, upstream.split(".")))
    length = max(len(a), len(b))
    a += (0,) * (length - len(a))
    b += (0,) * (length - len(b))
    return "update" if b > a else "ahead" if b < a else "current"


class UpdateError(Exception):
    pass


def replace_once(text, pattern, replacement):
    result, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise UpdateError(f"Recipe template did not match exactly once: {pattern}")
    return result


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



class PreparationError(Exception):
    pass

def confirm(prompt):
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


