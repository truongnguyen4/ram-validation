#!/usr/bin/env bash
# limit_ram.sh - simulate a low-RAM Android device by shrinking the RAM that is
# actually usable, at runtime, without reflashing.
#
#   ./limit_ram.sh 4G            # behave like a 4 GB device
#   ./limit_ram.sh 3584M         # sizes in G or M (default G), decimals allowed: 3.5G
#   ./limit_ram.sh status        # show current limit
#   ./limit_ram.sh reset         # give all RAM back (a reboot also resets)
#   ./limit_ram.sh -s SERIAL 4G  # pick a device when several are connected
#
# How it works (needs root: adb root or a rooted build):
#   1. Offline "Movable" memory blocks via /sys/devices/system/memory (real
#      removal: the kernel stops using that DDR).
#   2. Whatever is still missing is pinned in a ramfs balloon at /mnt/ram_limit
#      (ramfs pages are unevictable and cannot go to zram).
#
# Target sizes are physical RAM sizes, like the spec sheet ("4G"). The device's
# physical size is MemTotal rounded up to a whole GB, so carveouts/kernel
# reserved memory stay as on a real device of that size.
#
# Limits:
#   - Boot-time tuning still follows the real RAM size: ActivityManager OOM
#     levels, perf HAL RAM tier, zram disksize, dalvik heap. Only the amount of
#     RAM is simulated, not the 4 GB SKU configuration.
#   - On Qualcomm kernels with the mem-offline driver, offlined Movable blocks do
#     not lower MemTotal (the driver keeps it constant on purpose). The balloon
#     also shows up as used memory (Unevictable). "status" prints the effective
#     total, so subtract accordingly when reading RAM monitors.
#   - Adding mem=4G to boot.img does NOT work on devices whose ABL appends its
#     own mem=... at the end of the cmdline (the last mem= wins).
set -euo pipefail

DEV_SCRIPT=/data/local/tmp/limit_ram_dev.sh
SERIAL=""

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while getopts ":s:h" opt; do
    case $opt in
        s) SERIAL=$OPTARG ;;
        h) usage 0 ;;
        *) usage 1 ;;
    esac
done
shift $((OPTIND - 1))
[[ $# -eq 1 ]] || usage 1
ARG=$1

ADB=(adb)
[[ -n $SERIAL ]] && ADB+=(-s "$SERIAL")
adbsh() { "${ADB[@]}" shell "$@" | tr -d '\r'; }

# --- parse size -> MB --------------------------------------------------------
if [[ $ARG != status && $ARG != reset ]]; then
    if [[ $ARG =~ ^([0-9]+(\.[0-9]+)?)([GgMm])?[Bb]?$ ]]; then
        num=${BASH_REMATCH[1]}
        unit=${BASH_REMATCH[3]:-G}
        case $unit in G|g) mult=1024 ;; *) mult=1 ;; esac
        TARGET_MB=$(awk -v n="$num" -v m="$mult" 'BEGIN { printf "%d", n * m }')
    else
        echo "error: invalid size '$ARG' (examples: 4G, 3.5G, 3072M)" >&2
        exit 1
    fi
    if (( TARGET_MB < 1024 )); then
        echo "error: target must be >= 1G" >&2
        exit 1
    fi
fi

# --- root --------------------------------------------------------------------
"${ADB[@]}" wait-for-device
if [[ $(adbsh id -u) != 0 ]]; then
    "${ADB[@]}" root >/dev/null 2>&1 || true
    "${ADB[@]}" wait-for-device
    if [[ $(adbsh id -u) != 0 ]]; then
        echo "error: adb is not root (needs a userdebug/eng build or adb root)" >&2
        exit 1
    fi
fi

# --- device-side logic -------------------------------------------------------
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
cat >"$tmp" <<'EOF'
#!/system/bin/sh
MNT=/mnt/ram_limit
MEM=/sys/devices/system/memory
REC=/data/local/tmp/ram_limit.blocks   # blocks offlined by this tool
STATE=/data/local/tmp/ram_limit.state  # target / hidden MB
CHUNK_MB=64
FLOOR_MB=250                            # never push MemAvailable below this

mi() { awk -v k="$1:" '$1 == k { print int($2 / 1024) }' /proc/meminfo; }

balloon_mb() {
    ls $MNT 2>/dev/null | awk -F_ '/^b_/ { s += $3 } END { print s + 0 }'
}

block_mb() {
    if [ -r $MEM/block_size_bytes ]; then
        echo $(( $(printf %d 0x$(cat $MEM/block_size_bytes)) / 1048576 ))
    else
        echo 0
    fi
}

do_reset() {
    if grep -q " $MNT " /proc/mounts; then
        rm -f $MNT/b_*
        umount $MNT
    fi
    rmdir $MNT 2>/dev/null
    if [ -f $REC ]; then
        while read -r b; do
            err=""
            for try in 1 2 3 4 5; do
                [ "$(cat $MEM/$b/online)" = 1 ] && break
                err=$(echo online 2>&1 > $MEM/$b/state) && break
                sleep 1
            done
            [ "$(cat $MEM/$b/online)" = 1 ] || echo "WARN: could not online $b: $err"
        done < $REC
        rm -f $REC
    fi
    rm -f $STATE
}

do_status() {
    total=$(mi MemTotal); avail=$(mi MemAvailable)
    bal=$(balloon_mb); bs=$(block_mb)
    nblk=0; [ -f $REC ] && nblk=$(wc -l < $REC)
    target=none; hidden=0
    [ -f $STATE ] && . $STATE
    eff=$(( total - hidden - bal ))
    echo "target             : $target"
    echo "offlined blocks    : $nblk x ${bs} MB = $(( nblk * bs )) MB"
    echo "ramfs balloon      : $bal MB"
    echo "MemTotal (kernel)  : $total MB"
    echo "effective total    : $eff MB   <- MemTotal a real device of this size would show"
    echo "MemAvailable       : $avail MB (balloon already excluded)"
}

do_set() {
    tgt_mb=$1
    do_reset
    total=$(mi MemTotal)
    phys=$(( (total + 1023) / 1024 * 1024 ))
    remove=$(( phys - tgt_mb ))
    echo "physical RAM ~${phys} MB (MemTotal ${total} MB), target ${tgt_mb} MB -> remove ${remove} MB"
    if [ $remove -le 0 ]; then
        echo "nothing to do: device is already <= target"
        return
    fi
    left=$remove

    # 1. offline Movable memory blocks, highest address first
    bs=$(block_mb)
    if [ $bs -gt 0 ]; then
        for n in $(ls -d $MEM/memory* | sed 's/.*memory//' | sort -rn); do
            [ $left -ge $bs ] || break
            d=$MEM/memory$n
            [ "$(cat $d/online 2>/dev/null)" = 1 ] || continue
            grep -q Movable $d/valid_zones 2>/dev/null || continue
            if timeout 30 sh -c "echo offline > $d/state" 2>/dev/null; then
                echo memory$n >> $REC
                left=$(( left - bs ))
            fi
        done
    fi
    offl=$(( remove - left ))
    total2=$(mi MemTotal)
    hidden=$(( offl - (total - total2) ))   # offlined but still counted in MemTotal
    [ $offl -gt 0 ] && echo "offlined ${offl} MB of Movable memory blocks"

    # 2. pin the rest in a ramfs balloon
    if [ $left -gt 0 ]; then
        mkdir -p $MNT
        grep -q " $MNT " /proc/mounts || mount -t ramfs ramfs $MNT || { echo "error: mount ramfs failed"; exit 1; }
        i=0; wait=0
        while [ $left -gt 0 ]; do
            c=$CHUNK_MB; [ $left -lt $c ] && c=$left
            if [ $(mi MemAvailable) -lt $(( FLOOR_MB + c )) ]; then
                # give lmkd a moment to kill cached apps, like on a real small device
                wait=$(( wait + 1 ))
                if [ $wait -gt 10 ]; then
                    echo "WARN: MemAvailable stays below ${FLOOR_MB} MB, stopping with ${left} MB still missing (target too low?)"
                    break
                fi
                sleep 1; continue
            fi
            wait=0
            dd if=/dev/zero of=$MNT/b_${i}_$c bs=1048576 count=$c 2>/dev/null || { echo "WARN: dd failed"; break; }
            left=$(( left - c )); i=$(( i + 1 ))
        done
        echo "ramfs balloon $(balloon_mb) MB"
    fi
    # read by ram-monitor (runs as shell, cannot list /mnt/ram_limit)
    printf 'target=%sM\nhidden=%s\nballoon=%s\n' "$tgt_mb" "$hidden" "$(balloon_mb)" > $STATE
    chmod 644 $STATE
    echo
    do_status
}

case "$1" in
    set)    do_set "$2" ;;
    reset)  do_reset; echo "RAM limit removed"; do_status ;;
    status) do_status ;;
esac
EOF
"${ADB[@]}" push "$tmp" "$DEV_SCRIPT" >/dev/null 2>&1 || { echo "error: adb push failed" >&2; exit 1; }

case $ARG in
    status) adbsh sh "$DEV_SCRIPT" status ;;
    reset)  adbsh sh "$DEV_SCRIPT" reset ;;
    *)      adbsh sh "$DEV_SCRIPT" set "$TARGET_MB" ;;
esac
