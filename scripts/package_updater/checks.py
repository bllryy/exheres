import hashlib
import json
import os
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

from .common import UpdateError, compare, recipes, ROOT, CONFIG, CACHE, TTL, MAX_VERSION_LENGTH, SCHEMA, KERNEL, UNSUPPORTED, VERSION, KERNEL_RELEASE
from .kernel import display_release, kernel_release, published_release
from .prepare import official_config


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
    except (OSError, UnicodeError) as error:
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
            detail = event.get("error")
            message = event.get("event")
            if isinstance(detail, str) and detail:
                error = detail
            elif error is None:
                error = message if isinstance(message, str) and message else "unknown error"
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
    origins = official_config(root)
    if versions_out is not None:
        versions_out["official"] = True
    problems.extend(f"unmapped recipe: {name}" for name in sorted(current.keys() - entries - UNSUPPORTED.keys() - origins.keys() - {KERNEL}))
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





