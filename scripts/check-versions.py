#!/usr/bin/env python3
"""Compare recipe versions with upstream releases, without writing state."""

import importlib.util
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "nvchecker.toml"
KERNEL = "sys-kernel/kernel-cachyos"
KERNEL_UPDATER = Path(__file__).with_name("update-kernel-cachyos.py")

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

LABELS = {
    "update": "to update",
    "failed": "failed",
    "ahead": "ahead of upstream",
    "unsupported": "not tracked",
    "current": "up to date",
}


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


def results(output):
    versions = {}
    errors = {}
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = event.get("name")
        if not name:
            continue
        if event.get("event") == "updated":
            versions[name] = event.get("version")
        elif event.get("level") == "error":
            errors[name] = event.get("event", "unknown error")
    return versions, errors


def report(root, config, output, returncode, published=None, kernel_error=None):
    current, problems = recipes(root)
    entries = {name for name in config if name != "__config__"}
    tracked = entries | {KERNEL}
    overlap = entries & UNSUPPORTED.keys()
    missing = current.keys() - tracked - UNSUPPORTED.keys()
    if KERNEL in entries:
        problems.append(f"kernel must use published repository index, not nvchecker: {KERNEL}")
    if config.get("__config__", {}).get("oldver") or config.get("__config__", {}).get("newver"):
        problems.append("nvchecker must not write version files")
    extra = (entries | UNSUPPORTED.keys()) - current.keys()
    problems.extend(f"both configured and unsupported: {name}" for name in sorted(overlap))
    problems.extend(f"unmapped recipe: {name}" for name in sorted(missing))
    problems.extend(f"mapping without recipe: {name}" for name in sorted(extra))
    versions, errors = results(output)
    unknown = (versions.keys() | errors.keys()) - entries
    problems.extend(f"unexpected nvchecker result: {name}" for name in sorted(unknown))
    if returncode not in (0, 3):
        problems.append(f"nvchecker exited with status {returncode}")

    rows = {status: [] for status in LABELS}
    for name in sorted(current):
        installed = current[name]
        if name in UNSUPPORTED:
            status, detail = "unsupported", UNSUPPORTED[name]
        elif name == KERNEL:
            release = kernel_release(root, installed)
            if not release:
                status, detail = "failed", "unrecognized recipe release"
            elif kernel_error:
                status, detail = "failed", kernel_error
            elif not isinstance(published, str) or not KERNEL_RELEASE.fullmatch(published):
                status, detail = "failed", f"unrecognized published release {published!r}"
            else:
                status = compare(release.replace("-", "."), published.replace("-", "."))
                detail = f"{release} -> {published}"
        elif name not in entries:
            status, detail = "failed", "no source configured"
        elif name in errors:
            status, detail = "failed", errors[name]
        elif name not in versions or not isinstance(versions[name], str):
            status, detail = "failed", "no version returned"
        else:
            upstream = versions[name]
            status = compare(installed, upstream)
            detail = f"{installed} -> {upstream}"
            if status == "incomparable":
                status, detail = "failed", f"unrecognized version {upstream!r}"
        rows[status].append((name, detail))
    if returncode == 3 and not errors:
        problems.append("nvchecker reported failures without error details")

    lines = [", ".join(f"{len(rows[status])} {label}" for status, label in LABELS.items() if rows[status])]
    shown = [f"{status:6}  {name:39} {detail}" for status in ("update", "failed") for name, detail in rows[status]]
    if shown:
        lines += ["", *shown]
    if problems:
        lines += ["", *(f"ERROR: {problem}" for problem in problems)]
    return "\n".join(lines), bool(problems or rows["failed"])


def main():
    try:
        with CONFIG.open("rb") as file:
            config = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        print(f"Cannot load {CONFIG}: {error}", file=sys.stderr)
        return 1
    if config.get("__config__", {}).get("oldver") or config.get("__config__", {}).get("newver"):
        print("Refusing nvchecker config with version file writes", file=sys.stderr)
        return 1
    current, problems = recipes(ROOT)
    entries = set(config) - {"__config__"}
    problems.extend(f"unmapped recipe: {name}" for name in sorted(current.keys() - entries - UNSUPPORTED.keys() - {KERNEL}))
    problems.extend(f"mapping without recipe: {name}" for name in sorted((entries | UNSUPPORTED.keys()) - current.keys()))
    problems.extend(f"both configured and unsupported: {name}" for name in sorted(entries & UNSUPPORTED.keys()))
    if KERNEL in entries:
        problems.append(f"kernel must use published repository index, not nvchecker: {KERNEL}")
    if problems:
        print("\n".join(f"ERROR: {problem}" for problem in problems), file=sys.stderr)
        return 1
    try:
        completed = subprocess.run(
            ["nvchecker", "--logger", "json", "--failures", "-c", str(CONFIG)],
            text=True, capture_output=True, check=False,
        )
    except OSError as error:
        print(f"Cannot run nvchecker: {error}", file=sys.stderr)
        return 1
    published = kernel_error = None
    if KERNEL in current:
        spec = importlib.util.spec_from_file_location("update_kernel_cachyos", KERNEL_UPDATER)
        updater = importlib.util.module_from_spec(spec)
        bytecode = sys.dont_write_bytecode
        try:
            sys.dont_write_bytecode = True
            spec.loader.exec_module(updater)
        finally:
            sys.dont_write_bytecode = bytecode
        try:
            published, _ = updater.published_release()
        except (updater.UpdateError, OSError, subprocess.SubprocessError) as error:
            kernel_error = str(error)
    text, failed = report(ROOT, config, completed.stdout, completed.returncode, published, kernel_error)
    print(text)
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
