#!/bin/bash
# Non-root installer checks: syntax, root-guard refusal, truthful --print-plan.
set -u
cd "$(dirname "$0")/.."
fail() { echo "FAIL: $1"; exit 1; }

bash -n install.sh || fail "install.sh syntax"
bash -n uninstall.sh || fail "uninstall.sh syntax"
bash -n bin/warden || fail "bin/warden syntax"
bash -n selftest.sh || fail "selftest.sh syntax"

out=$(./install.sh 2>&1)
[ $? -eq 1 ] || fail "install as non-root must exit 1"
echo "$out" | grep -q "must run as root" || fail "root-guard message"

plan=$(./install.sh --print-plan) || fail "--print-plan must work unprivileged"
for f in guard.py render.py selftest.sh uninstall.sh managed-settings.base.json; do
  echo "$plan" | grep -q "$f" || fail "plan missing $f"
done
echo "$plan" | grep -q "/usr/local/bin/warden" || fail "plan missing warden CLI"

echo "install dry-run tests PASS"
