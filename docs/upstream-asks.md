# Upstream asks — Claude Code sandbox

**Status: both original asks RETIRED 2026-07-18 — solved on Warden's side by
delivering its own Seatbelt profile instead of using Claude Code's settings
compiler. Kept here as the record of why, and as the re-adoption trigger.**

Warden no longer enables Claude Code's native Bash sandbox
(`sandbox.enabled: false`). It renders its own macOS Seatbelt profile
(`render_seatbelt` in `render.py`) and launches sessions wrapped in it via
`sandbox-exec` (the `claude` shim). Every child process inherits the
profile. This sidesteps two limitations that were fatal when the native
sandbox was the delivery mechanism, and — critically — a third that was not
a bug but a design conflict: the native sandbox has no filesystem-only mode.

## Why the native sandbox was dropped (the disqualifying facts)

1. **No filesystem-only mode.** The native sandbox forces a network proxy
   that MITM-terminates TLS. Go-based CLIs (`gh`, `gcloud`, `terraform`)
   and Node's `fetch` fail x509 verification because Seatbelt blocks their
   path to the system trust store; `curl`/`git`/Python survive. It also
   denies macOS Keychain writes, so in-session `gh auth refresh` fails.
   None of this is configurable off: `allowedDomains: ["*"]` is ignored
   (#56959), `enableWeakerNetworkIsolation` is not wired into Claude Code
   (#28954), `excludedCommands` still gets the proxy env (#30619). Proven
   live 2026-07-18. This directly violates Warden's goal — **block zero
   networking, zero commands.**

2. **allow-within-deny for writes (was ask #1).** The native settings
   compiler honored deny-inside-allow but not allow-inside-deny for writes,
   so "freeze the repo but re-open its worktree" was inexpressible there.
   Raw Seatbelt has always supported it (`tests/lab/EVIDENCE-2026-07-16.txt`,
   66 passes; `probe-write-precedence.sh`). Going direct **uses the
   capability that already existed at the OS layer.**

3. **profile-by-exec-argument / E2BIG (was ask #2).** The native sandbox
   passed the profile as an exec argument; ~400 rules bricked every Bash
   spawn. `sandbox-exec -f <file>` loads the profile from a file — no
   argument-size ceiling. The `WARDEN_MAX_FS_RULES` guard and its whole
   class of failure are gone.

## Re-adoption trigger (when to reconsider the native sandbox)

Only if Claude Code ships a **filesystem-only sandbox mode** — network
isolation opt-out with filesystem rules intact — AND it expresses
allow-within-deny for writes AND loads the profile by file. Until all three
hold, Warden's own profile is strictly better for Warden's goals. If that
mode ships, re-evaluate: a harness-native wall would survive
`sandbox-exec` deprecation, which is the one durability risk below.

## The residual risk Warden's approach carries

`sandbox-exec` is deprecated-but-present on macOS (has been for years; still
ships and works). If Apple removes it, the launcher wrapping breaks and
sessions would start ungoverned — `warden status` and selftest T21 detect
that state (they check the session is actually wrapped), so it fails loud,
not silent. The mitigation if that day comes is the re-adoption trigger
above, or a Network Extension / endpoint-security wall. Tracked here so it
is a watched constraint, not a surprise.
