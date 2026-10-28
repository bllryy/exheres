#!/bin/sh
# Adapted from chimera-linux/dinit-chimera early/scripts/zram.sh (BSD-2-Clause).
# Copyright 2021-2024 q66 <q66@chimera-linux.org>
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
set -eu

PATH=/usr/x86_64-pc-linux-gnu/bin:/usr/bin:/bin
export PATH
helper=/usr/x86_64-pc-linux-gnu/libexec/dinit-chimera/zram

case ${2-} in
    zram[0-9]|zram[0-9][0-9]) device=$2 ;;
    *) printf '%s\n' 'expected zram device numbered 0 through 99' >&2; exit 1 ;;
esac
marker=/run/dinit-${device}.owned
active() {
    grep -q "^/dev/${device}[[:space:]]" /proc/swaps
}

case ${1-} in
    start)
        modprobe zram
        if active; then
            test -f "$marker" && exit 0
            printf '%s\n' "$device already active under another manager" >&2
            exit 1
        fi
        if test -r "/sys/block/$device/disksize"; then
            read -r size < "/sys/block/$device/disksize"
            if test "$size" != 0; then
                if test -f "$marker"; then
                    "$helper" "$device" stop
                else
                    printf '%s\n' "$device already configured under another manager" >&2
                    exit 1
                fi
            fi
        fi
        : > "$marker"
        "$helper" "$device"
        swapon "/dev/$device"
        ;;
    stop)
        test -f "$marker" || exit 0
        if active; then
            swapoff "/dev/$device"
        fi
        if test -d "/sys/block/$device"; then
            "$helper" "$device" stop
            test ! -d "/sys/block/$device"
        fi
        rm -f "$marker"
        ;;
    *) printf '%s\n' 'expected start or stop' >&2; exit 1 ;;
esac
