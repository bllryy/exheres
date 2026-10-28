"""Run with MKINITCPIO_SOURCE pointing at unpacked upstream mkinitcpio v42.2."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


RECIPE = (Path(__file__).resolve().parents[1] /
          'packages/sys-boot/mkinitcpio/mkinitcpio-42.2.exheres-0')
SOURCE = os.environ.get('MKINITCPIO_SOURCE')


@unittest.skipUnless(SOURCE, 'set MKINITCPIO_SOURCE to upstream v42.2 source')
class UdevRulesTest(unittest.TestCase):
    def test_prepared_rule_and_helper_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            shutil.copytree(SOURCE, source)
            script = r'''
set -e
require() { :; }
meson_src_prepare() { :; }
exhost() { printf '%s\n' x86_64-pc-linux-gnu; }
edo() { if [[ $1 != meson ]]; then "$@"; fi; }
PV=42.2
source "$1"
cd "$2"
src_prepare
bash -O extglob -n functions
bash -n install/udev
source functions
fixture=$3
mkdir -p "$fixture/image" "$fixture/helpers"
for helper in ata_id scsi_id custom_id; do
    printf '#!/bin/sh\n' > "$fixture/helpers/$helper"
done
cat > "$fixture/fixture.rules" <<'RULES'
# no execution, only dependency parsing
IMPORT{program}="ata_id --export $devnode"
RUN+="scsi_id --whitelisted"
RUN{program}="custom_id"
ENV{REMOVE_CMD}="/usr/bin/remove-device --flag"
IMPORT{builtin}="blkid"
RUN+="$env{DYNAMIC}"
RULES
# Intercept filesystem lookups, not rule parsing or destination selection.
type() {
    case "$PATH:$2" in
        /usr/x86_64-pc-linux-gnu/lib/udev/rules.d:*:fixture.rules)
            printf '%s\n' "$fixture/fixture.rules" ;;
        /usr/x86_64-pc-linux-gnu/lib/udev:*:ata_id|\
        /usr/x86_64-pc-linux-gnu/lib/udev:*:scsi_id|\
        /usr/x86_64-pc-linux-gnu/lib/udev:*:custom_id)
            printf '%s/helpers/%s\n' "$fixture" "$2" ;;
        *) return 1 ;;
    esac
}
add_file() {
    mkdir -p "$fixture/image${2%/*}"
    cp "$1" "$fixture/image$2"
}
add_binary() { printf '%s\n' "$1" >> "$fixture/binaries"; }
add_udev_rule fixture.rules
cmp "$fixture/fixture.rules" "$fixture/image/etc/udev/rules.d/fixture.rules"
[[ ! -e "$fixture/image/usr/lib/udev/rules.d/fixture.rules" ]]
add_udev_rule "$fixture/fixture.rules"
if add_udev_rule missing.rules; then exit 1; fi
'''
            subprocess.run(['bash', '-c', script, 'test', str(RECIPE),
                            str(source), str(root)], check=True)
            expected = [str(root / 'helpers' / name)
                        for name in ('ata_id', 'scsi_id', 'custom_id')]
            expected.append('/usr/bin/remove-device')
            self.assertEqual((root / 'binaries').read_text().splitlines(), expected * 2)
            hook = (source / 'install/udev').read_text()
            self.assertIn('/usr/x86_64-pc-linux-gnu/lib/udev/ata_id', hook)
            self.assertIn('/usr/x86_64-pc-linux-gnu/lib/udev/scsi_id', hook)
            self.assertIn('/usr/x86_64-pc-linux-gnu/lib/udev/hwdb.bin', hook)
            self.assertIn('/etc/udev/hwdb.bin', hook)


if __name__ == '__main__':
    unittest.main()
