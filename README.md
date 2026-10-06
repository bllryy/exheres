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

Then sync and install a package:

```sh
cave sync gurov
cave resolve category/package::gurov -x
```

## Check for updates

Install `nvchecker`, then run from repository root:

```sh
python3 scripts/check-versions.py
```

Some versions come from Arch or AUR, so an update there is only a hint. Check downloaded assets before updating recipes.
