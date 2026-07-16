#!/bin/bash
# Non-root checks for the Codex installer: syntax, root-guard refusal,
# truthful --print-plan, and a full unprivileged render into a temp root.
set -u
cd "$(dirname "$0")/.."
fail() { echo "FAIL: $1"; exit 1; }

bash -n install-codex.sh || fail "install-codex.sh syntax"
bash -n uninstall-codex.sh || fail "uninstall-codex.sh syntax"
bash -n codex-selftest || fail "codex-selftest syntax"
bash -n bin/warden || fail "bin/warden syntax"

out=$(./install-codex.sh 2>&1)
[ $? -eq 1 ] || fail "install-codex as non-root must exit 1"
echo "$out" | grep -q "must run as root" || fail "root-guard message"

plan=$(./install-codex.sh --print-plan) || fail "--print-plan must work unprivileged"
for f in guard.py codex_guard.py render.py landd.py codex-selftest uninstall-codex.sh requirements.base.toml; do
  echo "$plan" | grep -q "$f" || fail "plan missing $f"
done
echo "$plan" | grep -q "/etc/codex" || fail "plan missing /etc/codex"

# unprivileged render sanity: same command the installer runs, temp destination
tmp=$(mktemp -d "${TMPDIR:-/tmp}/warden-test.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
python3 render.py --format codex \
  --scan "$tmp/nonexistent" \
  --base templates/requirements.base.toml \
  --write-settings "$tmp/requirements.toml" \
  --write-registry "$tmp/registry.json" >/dev/null || fail "codex render"
python3 -c "import tomllib,sys; tomllib.loads(open(sys.argv[1]).read())" \
  "$tmp/requirements.toml" || fail "rendered requirements.toml parses"

echo "install-codex dry-run tests PASS"
