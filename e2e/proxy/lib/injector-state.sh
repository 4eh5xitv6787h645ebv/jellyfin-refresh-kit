#!/bin/sh
# Move independent Jellyfin injectors out of, or back into, the active plugin
# directory. This script is streamed into the throwaway origin with `docker
# exec -i ... sh -s`; accepting a root argument also makes its state machine
# testable without Docker.
set -eu

action="${1:-}"
config_root="${2:-/config}"
case "$action" in
    park|restore) ;;
    *) echo "FATAL: injector-state action must be park or restore" >&2; exit 2 ;;
esac
case "$config_root" in
    /*) ;;
    *) echo "FATAL: injector-state root must be absolute" >&2; exit 2 ;;
esac

plugins="$config_root/plugins"
parked="$config_root/plugins-parked"
[ -d "$plugins" ] || {
    echo "FATAL: active plugin directory is missing: $plugins" >&2
    exit 1
}

park() {
    # Exclusive creation makes an interrupted or concurrent run visible. Never
    # merge a new parking operation into unresolved state from another one.
    if [ -e "$parked" ] || [ -L "$parked" ]; then
        echo "FATAL: refusing pre-existing injector parking state: $parked" >&2
        return 1
    fi
    mkdir -- "$parked"

    for source in "$plugins"/*; do
        [ -d "$source" ] || continue
        name="${source##*/}"
        case "$name" in
            configurations|*"Jellyfin Refresh Kit"*) continue ;;
        esac
        destination="$parked/$name"
        if [ -e "$destination" ] || [ -L "$destination" ]; then
            echo "FATAL: injector parking destination already exists: $destination" >&2
            return 1
        fi
        mv -- "$source" "$destination"
    done
}

restore() {
    if [ ! -e "$parked" ] && [ ! -L "$parked" ]; then
        return 0
    fi
    if [ ! -d "$parked" ] || [ -L "$parked" ]; then
        echo "FATAL: injector parking state is not a directory: $parked" >&2
        return 1
    fi

    # Preflight every destination before moving anything. `mv DIR EXISTING`
    # would otherwise nest a parked plugin inside an active same-name plugin.
    for source in "$parked"/*; do
        if [ ! -e "$source" ] && [ ! -L "$source" ]; then
            continue
        fi
        if [ ! -d "$source" ] || [ -L "$source" ]; then
            echo "FATAL: unexpected entry in injector parking state: $source" >&2
            return 1
        fi
        name="${source##*/}"
        destination="$plugins/$name"
        if [ -e "$destination" ] || [ -L "$destination" ]; then
            echo "FATAL: refusing to overwrite active plugin while restoring: $destination" >&2
            return 1
        fi
    done

    for source in "$parked"/*; do
        [ -e "$source" ] || [ -L "$source" ] || continue
        name="${source##*/}"
        mv -- "$source" "$plugins/$name"
    done
    rmdir -- "$parked"
}

"$action"
