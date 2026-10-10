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

### Mirror

A backup mirror is available at [git.coasteen.xyz](https://git.coasteen.xyz/exherbo-overlay.git). Use it if Codeberg is unavailable.

To use the mirror, change the `sync` line in the repository configuration to:

```ini
sync = git+https://git.coasteen.xyz/exherbo-overlay.git
```

The mirror may not always be fully up to date with the primary repository.
