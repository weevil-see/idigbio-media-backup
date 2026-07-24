#!/bin/sh
# Double-click this to run the workflow: download, classify, build the gallery.
#
# A double-click gives no working directory and no terminal. start.py is a menu
# and needs one, so this moves to its own folder and reopens itself in a
# terminal; without one there is nothing to type into, and it says so.
#
# If double-clicking opens this in an editor instead, the file manager needs
# telling to run executable text files (Dolphin: Settings > Configure Dolphin >
# General > Confirmations; GNOME Files: Preferences > Executable Text Files).

cd "$(dirname "$0")" || exit 1

if [ -t 0 ] && [ -t 1 ]; then
    exec python3 start.py "$@"
fi

for term in konsole gnome-terminal xfce4-terminal mate-terminal xterm; do
    command -v "$term" >/dev/null 2>&1 || continue
    case "$term" in
        konsole)        exec "$term" --hold -e python3 start.py "$@" ;;
        gnome-terminal) exec "$term" -- python3 start.py "$@" ;;
        *)              exec "$term" -e python3 start.py "$@" ;;
    esac
done

echo "No terminal found. Run this from one:  python3 start.py" >&2
exit 1
