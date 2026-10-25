# Contributing

This is a personal Exherbo repository for packages I use. Issues and pull requests are welcome on [Codeberg](https://codeberg.org/Gurov/gurov). Open an issue before proposing a new package. New contributors must prepare contributions without AI assistance; see [AGENTS.md](AGENTS.md) for the full policy.

Keep changes focused. Verify versions, source URLs, licences, and dependencies against upstream. When equivalent Linux binaries are available as `.deb` and `.rpm`, prefer the `.deb` after checking their contents. Put patches in the package's `files/` directory and explain why they're needed.

## Package updates

Run `python3 -I scripts/update-packages.py` in a terminal to check releases, select numbers (or `all`), review diffs, and confirm each update. Enter at selection or confirmation cancels. `--check` never changes recipes; redirected input retains check-only behavior. `--refresh` bypasses the GitHub cache. Checks may update `.update-packages-cache.json`.

Use `--apply category/package` or `--all-eligible` for explicit preparation. Existing local updater behavior remains noninteractive; official imports always show diffs and require confirmation. No command installs packages or commits changes.

Official origins and pinned baseline bytes live in `scripts/recipe-origins.json`, verified against local official repository commits. Mirrors import upstream files; overlays use three-way merges and retain local files. Conflicts, missing patches, and edits after review stop publication. Upstream recipes are read as data, never sourced. After a successful official update, review and refresh provenance before subsequent rebases. The independent `systemd-utils` split tracks official systemd versions but requires manual build/install rebase, not replacement with systemd's recipe. Review preserved patches against new source before building.

Kernel updates target `linux-cachyos-eevdf-lto` and matching `linux-cachyos-eevdf-lto-headers` from official CachyOS x86-64-v3 repository. The 6.18.55 LTS recipe remains a fallback and is excluded from automatic variant updates. Package preparation does not install kernels or alter boot configuration.

## Exheres style

- Follow nearby recipes. Keep the copyright header and group metadata, dependencies, and phases in the same order. Omit fields supplied by an exlib and phases the package does not need.
- Indent with four spaces. Group dependencies by purpose (`build`, `build+run`, `run`, then `test` or `suggestion` if needed). Leave out empty groups and unused options.
- Preserve package-specific pins, archive names, slots, and restrictions. Formatting changes must not change how a package builds.
- For major version updates to recipes derived from Exherbo, consult the current official Exherbo recipe, compare and rebase local changes, and preserve attribution.

Before opening a pull request:

1. Check metadata and preview dependency resolution for the package you changed:

   ```sh
   cave show category/package::gurov
   cave resolve category/package::gurov
   ```

2. Build and test what changed when possible. Report failed or skipped checks.
3. Review the diff and run `git diff --check`.

Replace `category/package` with the package you changed. Do not add `-x` to the preview command.
