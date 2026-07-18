#!/usr/bin/env python3
"""Print the launching session's own worktree root for a given cwd, or
nothing if the cwd is not inside a linked worktree. Used by the claude
shim to render the per-session Seatbelt allow. Reuses guard.py's
worktree_container so the wall and the guard hook agree byte-for-byte on
what "this session's own worktree" means."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import guard  # noqa: E402


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cwd = argv[0] if argv else os.getcwd()
    wt = guard.worktree_container(guard._resolve(cwd))
    if wt:
        sys.stdout.write(wt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
