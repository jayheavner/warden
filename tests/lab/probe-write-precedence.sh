#!/bin/bash
# Retirement probe for upstream ask #1 (docs/upstream-asks.md): does Claude
# Code's sandbox now honor an allowWrite nested inside a denyWrite?
#
# Run from a PLAIN terminal (it launches a real headless Claude Code
# session; needs `claude` on PATH). Verdict comes from filesystem truth,
# never from model output:
#
#   RETIRED — deny/open/f exists, deny/f does not: allow-within-deny works;
#             upgrade the renderer to byte-level tree freeze (one root deny
#             + worktree/git allows) and retire the limitations entry.
#   STANDS  — neither file exists: deny still beats every allow beneath it.
#   INVALID — deny/f exists: the probe's project settings never loaded
#             (untrusted dir or sandbox off); no conclusion. Fix the setup.
set -u
command -v claude >/dev/null 2>&1 || { echo "probe: claude CLI not on PATH" >&2; exit 2; }
LAB="$(mktemp -d "${TMPDIR:-/tmp}/warden-precedence.XXXXXX")"
LAB="$(cd "$LAB" && pwd -P)"    # sandbox rules match real paths
mkdir -p "$LAB/deny/open" "$LAB/.claude"
cat > "$LAB/.claude/settings.json" <<EOF
{
  "sandbox": {
    "filesystem": {
      "denyWrite": ["$LAB/deny"],
      "allowWrite": ["$LAB/deny/open"]
    }
  }
}
EOF
(cd "$LAB" && claude -p --allowedTools "Bash" \
  "Run exactly these two commands and then reply done: touch '$LAB/deny/f'; touch '$LAB/deny/open/f'" \
  >/dev/null 2>&1)
CONTROL_WRITTEN=0; NESTED_WRITTEN=0
[ -e "$LAB/deny/f" ] && CONTROL_WRITTEN=1
[ -e "$LAB/deny/open/f" ] && NESTED_WRITTEN=1
rm -rf "$LAB"
if [ "$CONTROL_WRITTEN" -eq 1 ]; then
  echo "INVALID: the denyWrite control was writable — project settings did not load (untrusted dir?) or the sandbox is off. No conclusion."
  exit 2
elif [ "$NESTED_WRITTEN" -eq 1 ]; then
  echo "RETIRED: allow-within-deny for writes WORKS on $(claude --version 2>/dev/null | head -1)."
  echo "  Upstream ask #1 has landed. Upgrade render.py to the byte-level"
  echo "  tree freeze (one deny per repo root + worktree/.git allows) and"
  echo "  retire the git-level-residual entry in docs/limitations.md."
  exit 0
else
  echo "STANDS: a write deny still beats every allow beneath it ($(claude --version 2>/dev/null | head -1))."
  echo "  Update the PROVEN_ON_CLAUDE pin in doctor.py to this version."
  exit 1
fi
