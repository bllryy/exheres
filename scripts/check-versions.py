#!/usr/bin/env python3
"""Compare repository recipe versions with nvchecker results, without writing state."""

import json
import re
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "nvchecker.toml"

UNSUPPORTED = {
    "dev-libs/tlottie": "recipe version is tied to AyuGram's pinned commit and patch",
    "media-libs/nanosvg": "recipe version is tied to a pinned commit",
    "media-libs/tg_owt": "recipe pins four commits, not a release",
    "net-libs/tde2e": "recipe version is tied to a pinned TDLib commit",
    "x11-apps/qt6ct": "Git tag does not verify the generic registry archive",
}
PROXIES = {"app-misc/ente-auth", "net-im/discord", "net-im/viber", "net-vpn/mullvad-vpn", "sys-kernel/kernel-cachyos"}
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")
KERNEL_RELEASE = re.compile(r"[0-9]+(?:\.[0-9]+)*-[0-9]+\Z")


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def recipes(root):
    found = {}
    problems = []
    kernel_versions = []
    for path in sorted((root / "packages").glob("*/*/*.exheres-0")):
        name = f"{path.parent.parent.name}/{path.parent.name}"
        stem = path.name.removesuffix(".exheres-0")
        prefix = path.parent.name + "-"
        if not stem.startswith(prefix) or not stem[len(prefix):]:
            problems.append(f"invalid recipe filename: {path.relative_to(root)}")
            continue
        version = stem[len(prefix):]
        if name == "sys-kernel/kernel-cachyos":
            kernel_versions.append(version)
            if not VERSION.fullmatch(version):
                problems.append(f"invalid kernel version: {version}")
                continue
            if name not in found or version_key(version) > version_key(found[name]):
                found[name] = version
        elif name in found:
            problems.append(f"multiple versions for {name}")
        else:
            found[name] = version
    if len(kernel_versions) > 2:
        problems.append("keep only the latest and previous kernel-cachyos recipes")
    return found, problems


def compare(current, upstream):
    if not VERSION.fullmatch(current) or not VERSION.fullmatch(upstream):
        return "incomparable"
    a = tuple(map(int, current.split(".")))
    b = tuple(map(int, upstream.split(".")))
    length = max(len(a), len(b))
    a += (0,) * (length - len(a))
    b += (0,) * (length - len(b))
    return "newer" if b > a else "behind" if b < a else "current"


def kernel_release(root, version):
    path = root / "packages/sys-kernel/kernel-cachyos" / f"kernel-cachyos-{version}.exheres-0"
    try:
        recipe = path.read_text()
    except OSError:
        return None
    match = re.search(r'^MY_RELEASE="\$\{PV\}-([0-9]+)"$', recipe, re.MULTILINE)
    return f"{version}-{match.group(1)}" if match else None


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


def report(root, config, output, returncode):
    current, problems = recipes(root)
    entries = {name for name in config if name != "__config__"}
    overlap = entries & UNSUPPORTED.keys()
    missing = current.keys() - entries - UNSUPPORTED.keys()
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

    counts = Counter()
    lines = []
    for name in sorted(current):
        installed = current[name]
        if name in UNSUPPORTED:
            status, detail = "unsupported", UNSUPPORTED[name]
        elif name not in entries:
            status, detail = "failed", "no source configured"
        elif name in errors:
            status, detail = "failed", errors[name]
        elif name not in versions or not isinstance(versions[name], str):
            status, detail = "failed", "no version returned"
        else:
            upstream = versions[name]
            if name == "sys-kernel/kernel-cachyos":
                release = kernel_release(root, installed)
                if release and KERNEL_RELEASE.fullmatch(upstream):
                    status = compare(release.replace("-", "."), upstream.replace("-", "."))
                else:
                    status = "incomparable"
                detail = f"{release} -> {upstream}" if release else "unrecognized recipe release"
            else:
                status = compare(installed, upstream)
                detail = f"{installed} -> {upstream}"
            if status == "incomparable":
                status = "failed"
                if name != "sys-kernel/kernel-cachyos" or release:
                    detail = f"unrecognized version {upstream!r}"
            else:
                if name in PROXIES:
                    status = f"proxy-{status}"
                    detail += " (Arch package; not upstream)" if name == "net-im/discord" else " (AUR package; not upstream)"
        counts[status] += 1
        lines.append(f"{status:14} {name:39} {detail}")
    if returncode == 3 and not errors:
        problems.append("nvchecker reported failures without error details")
    lines.append(" ".join(f"{key}={counts[key]}" for key in ("newer", "current", "behind", "proxy-newer", "proxy-current", "proxy-behind", "unsupported", "failed")))
    lines.extend(f"ERROR: {problem}" for problem in problems)
    return "\n".join(lines), bool(problems or counts["failed"])


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
    problems.extend(f"unmapped recipe: {name}" for name in sorted(current.keys() - entries - UNSUPPORTED.keys()))
    problems.extend(f"mapping without recipe: {name}" for name in sorted((entries | UNSUPPORTED.keys()) - current.keys()))
    problems.extend(f"both configured and unsupported: {name}" for name in sorted(entries & UNSUPPORTED.keys()))
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
    text, failed = report(ROOT, config, completed.stdout, completed.returncode)
    print(text)
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
