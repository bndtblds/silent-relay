#!/bin/sh
set -eu

cd "$(dirname "$0")"

runtime_directory=.runtime
marker="$runtime_directory/maintenance.enabled"

case "${1:-}" in
    on)
        mkdir -p "$runtime_directory"
        if [ ! -f "$marker" ]; then
            temporary_marker=$(mktemp "$runtime_directory/.maintenance.enabled.XXXXXX")
            trap 'rm -f "$temporary_marker"' EXIT HUP INT TERM
            mv -f "$temporary_marker" "$marker"
            trap - EXIT HUP INT TERM
        fi
        printf '%s\n' "Maintenance mode is active."
        ;;
    off)
        rm -f "$marker"
        printf '%s\n' "Maintenance mode is inactive."
        ;;
    status)
        if [ -f "$marker" ]; then
            printf '%s\n' "Maintenance mode is active."
            exit 0
        fi
        printf '%s\n' "Maintenance mode is inactive."
        exit 1
        ;;
    *)
        printf '%s\n' "Usage: sh maintenance.sh {on|off|status}" >&2
        exit 2
        ;;
esac
