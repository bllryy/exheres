# bllry

Supplemental, third-party [Exherbo](https://www.exherbo.org/) repository for packages I use. It extends `arbor`; it is not an official Exherbo repository.

## Add the repository

Create `/etc/paludis/repositories/gurov.conf`:

```ini
format = e
location = ${root}/var/db/paludis/repositories/bllry
sync = git+https://codeberg.org/Gurov/exheres.git
sync_options = --branch=main
```

Then sync and install a package:

```sh
cave sync bllry
cave resolve category/package::bllry -x
```

### Forked from [Gurov's exheres](https://codeberg.org/Gurov/exheres)

