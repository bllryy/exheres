# Contributing

This is a personal Exherbo repository for packages I use. Issues and pull requests are welcome on [Codeberg](https://codeberg.org/Gurov/gurov). Open an issue before proposing a new package. New contributors must prepare contributions without AI assistance; see [AGENTS.md](AGENTS.md) for the full policy.

Keep changes focused. Verify versions, source URLs, licences, and dependencies against upstream. When equivalent Linux binaries are available as `.deb` and `.rpm`, prefer the `.deb` after checking their contents. Put patches in the package's `files/` directory and explain why they're needed.

## Exheres style

- Follow nearby recipes. Keep the copyright header and group metadata, dependencies, and phases in the same order. Omit fields supplied by an exlib and phases the package does not need.
- Indent with four spaces. Group dependencies by purpose (`build`, `build+run`, `run`, then `test` or `suggestion` if needed). Leave out empty groups and unused options.
- Preserve package-specific pins, archive names, slots, and restrictions. Formatting changes must not change how a package builds.

Before opening a pull request:

1. Check metadata and preview dependency resolution for the package you changed:

   ```sh
   cave show category/package::gurov
   cave resolve category/package::gurov
   ```

2. Build and test what changed when possible. Report failed or skipped checks.
3. Review the diff and run `git diff --check`.

Replace `category/package` with the package you changed. Do not add `-x` to the preview command.
