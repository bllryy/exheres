import argparse
import difflib
import subprocess
import sys
import tomllib

from .common import PreparationError, confirm
from .checks import preview
from .common import UpdateError, ROOT, KERNEL, UNSUPPORTED
from .kernel import display_release, render_recipe, update
from .prepare import current_version, numeric_version, official_config, official_diff, official_files, prepare_official, prepare_recipe, write_official

__doc__ = "Preview package releases with a short-lived cache for GitHub checks."


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="refresh GitHub version checks")
    parser.add_argument("--verbose", action="store_true", help="show every package and cache age")
    parser.add_argument("--apply", nargs="+", metavar="PACKAGE", help="prepare selected updates")
    parser.add_argument("--all-eligible", action="store_true", help="prepare all reported updates")
    parser.add_argument("--check", action="store_true", help="check only, including on a terminal")
    parser.add_argument("--interactive", action="store_true", help="select updates and confirm reviewed diffs")
    args = parser.parse_args(argv)
    interactive = args.interactive or (sys.stdin.isatty() and sys.stdout.isatty() and not (args.check or args.apply or args.all_eligible))
    if args.check and (args.interactive or args.apply or args.all_eligible):
        parser.error("--check cannot be combined with apply or interactive")
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
    origins = official_config(ROOT)
    for name, origin in origins.items() if versions.pop("official", False) else []:
        try:
            files = official_files(origin, None)
            prefix = origin["package"].split("/")[1] + "-"
            available = [p.removeprefix(prefix).removesuffix(".exheres-0") for p in files
                         if "/" not in p and p.startswith(prefix) and p.endswith(".exheres-0")]
            available = [v for v in available if numeric_version(v) is not None]
            if not available:
                raise PreparationError("No numeric official recipe")
            latest = max(available, key=numeric_version)
            current = current_version(ROOT, name)
            versions[name] = latest
            a, b = numeric_version(current) if current else None, numeric_version(latest)
            status = "update" if a is None or b > a else "ahead" if a > b else "current"
            rows[name] = (status, f"{current} -> {latest} (official {origin['mode']})")
        except PreparationError as error:
            rows[name] = ("failed", str(error))
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
    if interactive:
        choices = sorted(name for name, (status, _) in rows.items() if status == "update")
        for number, name in enumerate(choices, 1):
            print(f"{number}. {name}")
        try:
            answer = input("Select numbers, 'all', or Enter to cancel: ").strip()
            selected = set(choices) if answer == "all" else {choices[int(n) - 1] for n in answer.split()
                                                              if n.isdecimal() and 1 <= int(n) <= len(choices)}
            if answer and answer != "all" and any(not n.isdecimal() or not 1 <= int(n) <= len(choices) for n in answer.split()):
                print("Invalid selection; nothing applied", file=sys.stderr)
                return 1
        except (EOFError, KeyboardInterrupt):
            return 0
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
                if interactive:
                    paths = [path for path in (ROOT / "packages" / KERNEL).glob("*.exheres-0")
                             if 'linux-cachyos-lts-' not in path.read_text()]
                    if len(paths) != 1:
                        raise PreparationError("Expected one active EEVDF LTO kernel recipe")
                    old = paths[0]
                    release_preview, hashes = kernel[0]
                    print("".join(difflib.unified_diff(old.read_text().splitlines(True),
                                                   render_recipe(old.read_text(), release_preview, hashes).splitlines(True),
                                                   fromfile=str(old), tofile=f"kernel-cachyos-{display_release(release_preview)}.exheres-0")))
                    if not confirm("Apply kernel update? [y/N] "):
                        continue
                release = update(ROOT, published=kernel[0])
                if release is not None:
                    print(f"Draft: update kernel-cachyos to {display_release(release)}")
            else:
                version = versions.get(name)
                if version is None:
                    raise PreparationError("no validated version")
                if name in origins:
                    plan = prepare_official(ROOT, name, version, origins[name])
                    print(official_diff(plan))
                    if not confirm("Apply official update preserving local changes? [y/N] "):
                        continue
                    write_official(plan)
                    print(f"Draft: update {name.split('/')[1]} to {version}")
                else:
                    suggestion = prepare_recipe(ROOT, name, version, review=True) if interactive else prepare_recipe(ROOT, name, version)
                    if suggestion:
                        print(f"Draft: {suggestion}")
        except (PreparationError, UpdateError, OSError, subprocess.SubprocessError) as error:
            print(f"Cannot prepare {name}: {error}", file=sys.stderr)
            failed = True
    return int(failed)


