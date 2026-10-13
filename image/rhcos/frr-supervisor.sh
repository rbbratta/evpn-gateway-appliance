#!/usr/bin/bash
set -euo pipefail

children=()
stop_children() {
    trap '' TERM INT
    local child
    for child in "${children[@]}"; do
        kill -TERM -- "-$child" 2>/dev/null || true
    done
    (
        sleep 10
        for child in "${children[@]}"; do
            kill -KILL -- "-$child" 2>/dev/null || true
        done
    ) &
    local watchdog=$!
    wait "${children[@]}" 2>/dev/null || true
    kill -KILL -- "-$watchdog" 2>/dev/null || true
    wait "$watchdog" 2>/dev/null || true
}

supervise() {
    # Each child gets a process group so FRR's shell wrapper cannot orphan watchfrr.
    set -m
    trap 'stop_children; exit 0' TERM INT
    "$1" &
    children+=("$!")
    "$2" --metrics-bind-address=127.0.0.1 --metrics-port=7573 &
    children+=("$!")
    set +e
    wait -n "${children[@]}"
    local status=$?
    stop_children
    echo "FRR or its metrics process exited with status $status" >&2
    return 1
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    test -x /usr/lib/frr/docker-start
    test -x /frr-metrics
    test -s /etc/frr/daemons
    for daemon in zebra bgpd bfdd; do
        if grep -q "^${daemon}=" /etc/frr/daemons; then
            sed -i "s/^${daemon}=.*/${daemon}=yes/" /etc/frr/daemons
        else
            printf '%s=yes\n' "$daemon" >> /etc/frr/daemons
        fi
    done
    install -d -m 0775 -o frr -g frr /run/frr /var/log/frr
    supervise /usr/lib/frr/docker-start /frr-metrics
fi
