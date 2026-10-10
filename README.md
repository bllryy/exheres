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

### Check for updates

Install `nvchecker`, then run from repository root:

```sh
python3 scripts/check-versions.py
```

Some versions come from Arch or AUR, so an update there is only a hint. Check downloaded assets before updating recipes.

To prepare the latest CachyOS kernel recipe separately (requires `zstd` and network access):

```sh
python3 scripts/update-kernel-cachyos.py
```

The updater checks AUR once, downloads and inspects both x86-64-v3 archives, pins their SHA-256 hashes, then replaces the previous recipe so only one remains. It makes no changes if AUR is unavailable, the release is unchanged, or either archive fails validation. Review the generated recipe and independently verify the archive hashes before installing; hashes computed from downloads alone do not authenticate upstream assets.

The kernel now uses `SLOT="0"`: future CachyOS upgrades replace the installed CachyOS package, so keep another working kernel as a boot fallback. Existing installs in old versioned slots are **not** removed by this repository change. On systems migrating from versioned slots, `cave resolve world -c` may still report those installed slots as unsuitable until they are removed. After syncing the updated repository, preview `cave resolve 'sys-kernel/kernel-cachyos:0::gurov'` before installing; ensure another working kernel is available. Verify the new boot entry and initramfs, reboot into the new kernel, and only then preview removal of old slots with `cave uninstall '=sys-kernel/kernel-cachyos-7.2.8:7.2.8::installed'` and `cave uninstall '=sys-kernel/kernel-cachyos-7.2.9:7.2.9::installed'` (adjust versions to match `cave print-ids --matching 'sys-kernel/kernel-cachyos::installed'`). Check remaining boot entries before removing stale ones. Do not uninstall the running kernel; the updater never uninstalls packages or touches `/boot`.
