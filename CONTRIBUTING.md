# Contributing

This is a personal Exherbo repository for packages I use. Issues and pull requests are welcome on [Codeberg](https://codeberg.org/Gurov/gurov). If you're proposing a new package, open an issue first.

Keep changes focused. Check versions, source URLs, licences, and dependencies against upstream rather than copying them from another package. Put patches in the package's `files/` directory and explain why they're needed.

## Exheres style

- Follow nearby recipes. Use the copyright header, then `require` if needed, source metadata, licence/slot/platform metadata, options, dependencies, build settings, and phases. Skip fields an exlib already supplies. Leave a blank line between sections, not between `LICENCES`, `SLOT`, and `PLATFORMS`.
- Keep a single URL on one line for `HOMEPAGE` or `DOWNLOADS`. Use multiple lines when the value contains multiple URLs or an archive rename.
- Indent with four spaces, including phase bodies. In `DEPENDENCIES`, indent groups four spaces, packages eight, and conditional packages one level deeper. Quote shell values according to whether they need expansion.
- Group dependencies by purpose: `build`, `build+run`, `run`, then `test` or `suggestion` where needed. Leave a blank line between groups; don't add empty groups or unused options.
- Use exlib defaults unless the package needs an override. Add only the phases you need.
- Keep package-specific details such as pinned commits, archive names, slots, and restrictions. Consistency shouldn't change how a package builds.

Before opening a pull request:

1. Check the package metadata and dependency resolution:

   ```sh
   cave show category/package::gurov
   cave resolve category/package::gurov
   ```

2. Build the package and test what changed when possible. Mention anything you couldn't test, or anything that failed.
3. Run `git diff --check` and review the diff.

Replace `category/package` with the package you changed. `cave resolve` without `-x` only shows what would be installed.
