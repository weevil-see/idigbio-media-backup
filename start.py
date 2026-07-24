#!/usr/bin/env python3
"""One entry point for the whole workflow.

Finds the dataset, shows what state it is in, and runs the three scripts in the
right order. Each still asks its own questions, so nothing is hidden: this only
saves remembering which script comes next and what it is called.

    python3 start.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# The three build steps, in order. Viewing is deliberately not one of them: it
# is gallery/serve.py's job, which is emitted with the gallery and must keep
# working on its own if that folder is copied elsewhere. This only calls it.
STEPS = [
    ("Download media", "download_media.py",
     "fetch the image files an archive points at"),
    ("Resolve classifications", "fill_taxonomy.py",
     "ask TaxonWorks for the ranks above species"),
    ("Build the gallery", "make_gallery.py",
     "write gallery.html from what is on disk"),
]
VIEW = ("Open the gallery", "serve it over localhost and open the browser")


def datasets():
    """Directories that hold a dwca/, nearest first."""
    found = []
    cwd = os.getcwd()
    for candidate in [cwd, HERE]:
        if os.path.isdir(os.path.join(candidate, "dwca")) and candidate not in found:
            found.append(candidate)
    for parent in [cwd, HERE]:
        try:
            entries = sorted(os.scandir(parent), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and os.path.isdir(os.path.join(entry.path, "dwca")) \
                    and entry.path not in found:
                found.append(entry.path)
    return found


def summarise(root):
    """What has been done for this dataset so far."""
    lines = []

    media = os.path.join(root, "media")
    if os.path.isdir(media):
        files = [e for e in os.scandir(media)
                 if e.is_file() and not e.name.endswith((".csv", ".log", ".part"))]
        size = sum(e.stat().st_size for e in files) / 1e9
        parts = [f"{len(files):,} files, {size:.1f} GB"]
        partial = sum(1 for e in os.scandir(media) if e.name.endswith(".part"))
        if partial:
            parts.append(f"{partial} unfinished")
        lines.append(f"  media      {', '.join(parts)}")
    else:
        lines.append("  media      nothing downloaded yet")

    csv_path = os.path.join(root, "taxonomy", "taxonworks.csv")
    if os.path.exists(csv_path):
        with open(csv_path, encoding="utf-8") as fh:
            rows = max(sum(1 for _ in fh) - 1, 0)
        lines.append(f"  taxonomy   {rows:,} specimens classified")
    else:
        lines.append("  taxonomy   not resolved (optional)")

    page = os.path.join(root, "gallery", "gallery.html")
    if os.path.exists(page):
        import time
        when = time.strftime("%d %b %H:%M", time.localtime(os.path.getmtime(page)))
        lines.append(f"  gallery    built {when}")
    else:
        lines.append("  gallery    not built")
    return "\n".join(lines)


def run(script, root):
    """Run one step, letting it talk to the terminal as usual."""
    print(f"\n{'-' * 62}\n  {script}\n{'-' * 62}", flush=True)
    result = subprocess.run([sys.executable, os.path.join(HERE, script)], cwd=root)
    if result.returncode != 0:
        print(f"\n  {script} exited with {result.returncode}", flush=True)
    return result.returncode == 0


def choose_dataset(found):
    if len(found) == 1:
        return found[0]
    print("Which dataset?")
    for index, path in enumerate(found, 1):
        print(f"  [{index}] {os.path.relpath(path) or '.'}")
    while True:
        try:
            answer = input("Choice [1]: ").strip().lower() or "1"
        except (EOFError, KeyboardInterrupt):
            sys.exit(0)
        if answer in ("q", "quit", "exit"):
            sys.exit(0)
        if answer.isdigit() and 1 <= int(answer) <= len(found):
            return found[int(answer) - 1]
        print("  not a choice -- a number, or q to quit")


def main():
    found = datasets()
    if not found:
        sys.exit(
            "No dataset found.\n"
            "A dataset is a directory containing a dwca/ -- unpack an iDigBio\n"
            "download into one and run this from there, or from the directory\n"
            "above it. See the README for where to get an archive.")

    root = choose_dataset(found)
    print(f"\nDataset: {os.path.relpath(root) or '.'}")
    print(summarise(root))

    while True:
        print("\nWhat next?")
        for index, (title, script, why) in enumerate(STEPS, 1):
            print(f"  [{index}] {title:24} {why}")
        print(f"  [{len(STEPS) + 1}] {VIEW[0]:24} {VIEW[1]}")
        print(f"  [{len(STEPS) + 2}] All three, in order")
        print("  [q] Quit")
        try:
            answer = input("Choice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if answer in ("q", "quit", "exit"):
            return 0
        if answer == str(len(STEPS) + 1):
            launcher = os.path.join(root, "gallery", "serve.py")
            if not os.path.exists(launcher):
                print("  no gallery yet -- build it first")
                continue
            print("\n  Ctrl-C in this window stops the server.", flush=True)
            subprocess.run([sys.executable, launcher], cwd=root)
            continue
        if answer == str(len(STEPS) + 2):
            for _, script, _ in STEPS:
                if not run(script, root):
                    break        # a failed step makes the next one meaningless
        elif answer.isdigit() and 1 <= int(answer) <= len(STEPS):
            run(STEPS[int(answer) - 1][1], root)
        else:
            print("  not a choice")
            continue
        print("\n" + summarise(root))


if __name__ == "__main__":
    sys.exit(main())
