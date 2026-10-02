# gurov

Supplemental, third-party [Exherbo](https://www.exherbo.org/) repository for packages I use. It extends `arbor`; it is not an official Exherbo repository.

## Add the repository

Create `/etc/paludis/repositories/gurov.conf`:

```ini
format = e
location = ${root}/var/db/paludis/repositories/gurov
sync = git+https://codeberg.org/Gurov/gurov.git
sync_options = --branch=main
```

Then sync and inspect a package before installing it:

```sh
cave sync gurov
cave show category/package::gurov
cave resolve category/package::gurov -x
```

Replace `category/package` with a package in [`packages/`](packages/). If you already have the repository configured, only run the sync and package commands.

## Check for updates

Install `nvchecker`, then run from repository root:

```sh
python3 scripts/check-versions.py
```

The script locates its config and recipes relative to itself. Output lists each recipe with its current version and checked version. `newer` means upstream has a later numeric release; `behind` means the recipe version is ahead of the checked release. `proxy-newer`, `proxy-current`, and `proxy-behind` use Arch or AUR package versions as **secondary signals**, not proof of an upstream update or availability of the exact downloaded artifact. `unsupported` lists recipes without a trustworthy comparable feed. `failed` means a check did not produce a usable result; it never means current. The command exits nonzero for failures or missing mappings, but not for updates or documented unsupported packages. Checks are network-dependent and do not edit recipes or persist versions. Verify assets, checksums, patches, and dependencies manually before updating a recipe.

## Notes

Recipes here may depend on packages from `arbor` or other repositories. Availability and build results depend on your Exherbo setup; review each recipe and its licenses before installing. Some packages need additional system configuration after installation. Installing a firewall recipe, for example, does not mean you should enable the firewall without first allowing any remote access you need.

See [AGENTS.md](AGENTS.md) for contributor and AI-assistance guidelines.
