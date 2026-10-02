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

# These recipe versions do not have a verified, comparable upstream feed.
UNSUPPORTED = {
    "app-misc/ente-auth": "auth release tag does not verify the AppImage asset",
    "media-libs/nanosvg": "recipe version is tied to a pinned commit",
    "media-libs/tg_owt": "recipe pins four commits, not a release",
    "media-libs/zita-convolver": "upstream tarball listing not verified",
    "net-firewall/ufw": "upstream download listing not verified",
    "net-libs/tde2e": "recipe version is tied to a pinned TDLib commit",
    "sys-kernel/kernel-cachyos": "kernel and headers must both exist at matching binary release",
    "x11-apps/qt6ct": "Git tag does not verify the generic registry archive",
}
PROXIES = {"net-im/discord", "net-im/viber", "net-vpn/mullvad-vpn"}
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")


def recipes(root):
    found = {}
    problems = []
    for path in sorted((root / "packages").glob("*/*/*.exheres-0")):
        name = f"{path.parent.parent.name}/{path.parent.name}"
        stem = path.name.removesuffix(".exheres-0")
        prefix = path.parent.name + "-"
        if not stem.startswith(prefix) or not stem[len(prefix):]:
            problems.append(f"invalid recipe filename: {path.relative_to(root)}")
            continue
        if name in found:
            problems.append(f"multiple versions for {name}")
        found[name] = stem[len(prefix):]
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
            status = compare(installed, upstream)
            if status == "incomparable":
                status, detail = "failed", f"unrecognized version {upstream!r}"
            else:
                detail = f"{installed} -> {upstream}"
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
