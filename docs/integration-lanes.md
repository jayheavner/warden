# Integration lanes: shape-aware `warden land`

Date: 2026-07-16
Status: draft v2 — revised after spec-panel review (see §12)
Builds on: `landd.py` + `warden land` (currently on the `worktree-codex-port`
branch; this design extends that code and lands after or with it).

## 1. Problem

`warden land` v1 hardcodes one integration shape: fast-forward the shared
checkout's HEAD branch. That is correct for a personal repo with no remote
and wrong everywhere else. Real repos on this machine differ:

- **Personal, no remote.** Never a PR. Local fast-forward is the whole job.
- **Personal, own GitHub remote.** Direct push is fine and wanted; landing
  locally without pushing silently forks local main from origin/main.
- **Work, direct-push.** A remote exists and pushing the default branch is
  permitted by both protection and norms.
- **Work, PR-required.** Branch protection (or team norms) forbid direct
  pushes. Advancing local main past origin/main is actively harmful: the
  eventual squash/rebase merge gives the same change a different SHA and
  local main diverges permanently.

One repo's correct behavior is another repo's corruption. Warden must know
which shape it is in — without taxing Jay with per-repo configuration
(policy: [integration-policy-no-merge-tax]).

## 2. Design summary

Each adopted repo resolves to a **lane** at land time. Three lanes:

| Lane | Meaning | What `warden land` does |
|---|---|---|
| `local` | no remote / integration is local-only | ff shared HEAD branch to the session branch (v1 behavior) |
| `push` | remote, direct push appropriate | sync from origin, push session SHA to origin's default branch, then ff the shared checkout to match |
| `pr` | remote integrates via PRs | push the session branch to origin, open a PR as the repo owner via `gh`; never advance local main past origin |

Lane resolution, first match wins:

1. **Declared** — `.warden.json` at the repo root names the lane.
2. **Learned** — a previous land discovered the truth from the remote's own
   rejection and recorded it.
3. **Inferred** — from disk truth at land time:
   - no usable remote → `local`
   - any usable remote → `push`

Rationale (requirements ruling, §12): the governing requirement is that
Warden never manufactures integration work for Jay. A repo wrongly
defaulted to `pr` produces a PR nobody required — which a human must then
merge: tax, requirement violation. A repo wrongly defaulted to `push`
against an actually-protected branch produces one refused push — harmless,
automatic, and the trigger for permanent self-correction. So the default
is `push` everywhere a remote exists, and resolution is **self-correcting
in the only direction that matters**: a policy denial re-dispatches the
same request down the `pr` lane and persists the lesson (§4); every later
land in that repo goes straight to `pr`. The remote is the authority on
its own rules; Warden asks it by acting, not by probing protection APIs
that need working auth.

Accepted residual, stated plainly: a work repo where PRs are required by
*convention only* (no enforced protection) will take direct pushes until
its one-line declaration (§5) exists — the remote accepts them, so there
is nothing to learn from. That is the floor of the config tax, paid only
by the one shape whose rules are invisible on the wire. An earlier draft
routed all org-owned remotes to `pr` to avoid this; rejected because it
violated the no-manufactured-PRs requirement for every org repo where
direct push is legitimate.

No new commands for landing. `warden land [branch]` is still the entire
interface; repos just do the right thing for their shape.

## 3. Lane semantics in detail

Common preconditions (all lanes, unchanged from v1): repo must be adopted
in the registry; branch name validated; branch must exist; shared checkout
must have a non-detached HEAD and a clean tracked tree.

Remoted lanes (`push`, `pr`) share a common prefix — **fetch, sync,
verify** — then diverge only in their final act:

- Fetch: `git fetch origin <default-branch>`.
- Sync: ff the shared HEAD branch to `origin/<default-branch>` if behind.
  Always safe under the invariant: *in remoted repos, the shared HEAD
  branch never holds commits origin doesn't have* (it only ever advances
  to SHAs the remote has already accepted). A non-ff sync means the
  invariant was broken outside Warden (force-push upstream, manual local
  commits) → reject loudly with the state found; a human must look.
- Verify: the session SHA must fast-forward from the synced tip; if not →
  reject: "merge <default-branch> in your worktree, then land again."

`<default-branch>` is resolved from `refs/remotes/origin/HEAD` (set by
clone; repaired via `git remote set-head origin -a` during `warden
refresh`), falling back to the shared checkout's HEAD branch name. This is
also the PR base — never assume the local branch name matches the remote's
default.

### 3.1 `local`

Exactly v1: `git merge --ff-only <sha>` onto the shared HEAD branch.
Diverged → rejected with the merge-in-your-worktree fix. The network is
never touched, even if a remote happens to exist (a declared `local` lane
in a remoted repo means "I push manually" — respected).

### 3.2 `push`

Ordering rule: **push first, mutate the shared checkout only after the
remote accepts.** A rejected push leaves nothing to roll back.

After the common prefix:

1. `git push origin <sha>:refs/heads/<default-branch>`.
2. Outcomes:
   - Accepted → ff shared HEAD branch to `<sha>`. Status: `landed`.
     If this final local ff itself fails (checkout dirtied in the window,
     I/O error), the commit is on origin but not in the shared checkout —
     status `landed-remote-only`, naming the fix (`git -C <repo> merge
     --ff-only <sha>` once the checkout is clean); the next land's sync
     step also heals it.
   - Refused as a **policy denial** (§3.4) → the lane returns a
     `policy-denied` verdict; the executor records the lesson (§4) and
     re-dispatches the request down the `pr` lane as a fresh, ordinary
     execution. Routing lives in the executor, not inside the lane —
     `push` never embeds `pr`.
   - Refused non-ff (a race: someone pushed between fetch and push) →
     reject noting the race: "origin moved during landing; re-land to
     retry against the new tip."
   - Refused otherwise (auth, network, unclassifiable) → reject with
     stderr and **no lesson written**. Repeated identical rejections for
     the same repo are counted in the audit event so a broken-auth repo
     shows an escalating tally rather than an innocent-looking singleton.

### 3.3 `pr`

After the common prefix:

1. `git push origin <branch>` (the session branch itself). If the remote
   branch exists and has diverged → reject; the session owns its branch
   and must reconcile in its own worktree.
2. Open the PR: `gh pr create --base <default-branch> --head <branch>
   --fill`, run demoted to the repo owner (their gh auth), exactly as git
   already runs demoted in landd.
   - Created → status `pr-opened`, with URL.
   - Already exists → status `pr-exists`, with the existing URL
     (`gh pr view <branch> --json url`).
   - `gh` missing / token invalid / remote is not GitHub → status
     `branch-pushed`: the branch is on the remote and the result message
     says exactly that plus what Warden could not do and why. Landing
     degrades to the largest step it can take, never to an error that
     undoes work.
3. The shared HEAD branch is **never** advanced to the session SHA in this
   lane. It only ever tracks origin (common prefix), which is also how it
   catches up after PRs merge. Because pr-lane repos otherwise go stale
   between lands, `warden refresh` also runs the fetch+sync step for every
   remoted repo (staleness fix, §12).

Identity: `gh` acts as the remote host's **active** account. With
multiple accounts configured on one host, the active one may lack access
to a given repo — that failure degrades to `branch-pushed`, and the
message names the account that acted and the other accounts available on
that host (fix: `gh auth switch`, or a future `account` key in
`.warden.json`, reserved but unimplemented in v1). Every `pr`-lane result
names the acting account, so Warden never acts as an identity without
saying which.

Provenance: PRs are opened as Jay with `--fill`; no bot-disclosure footer
is added. The commits themselves carry `Co-Authored-By: Claude` trailers,
which is where provenance already lives in this workflow. (Deliberate
decision; revisit if a team objects.)

Jay confirmed this lane's behavior explicitly (2026-07-16): push the
branch and open the PR in his name; the remote's own process takes over
from there.

### 3.4 Policy-denial classification

A refusal counts as a **policy denial** — the only refusal that triggers
pr-fallback and a persisted lesson — when *both* hold:

1. The push reached the remote and was refused at ref-update time (a
   `! [remote rejected]` ref status from `git push --porcelain`), not a
   transport, auth, or non-ff failure; and
2. the remote's message matches a curated table of real-transcript
   patterns shipped as fixtures (GitHub `GH006`/`protected branch`;
   GitLab protected-branch texts; extended only by adding a captured
   transcript to the fixture table).

Anything ambiguous is refused **without learning** — a wrong ordinary
rejection costs one retry; a wrong lesson silently changes a repo's
behavior forever. The classifier is deliberately under-eager.

### 3.5 Remote selection

"The remote" is: the shared HEAD branch's configured upstream remote if
set, else `origin` if present, else no remote → `local`. Multi-remote
exotica beyond that is out of scope; declare the lane if inference picks
wrong.

## 4. Learned lanes

File: `/Library/Application Support/ClaudeCode/warden/learned.json`
(root-owned, written only by landd via atomic rename; sessions can read,
never write — same trust class as the registry).

```json
{ "version": 1,
  "repos": {
    "/Users/jay/work/acme": { "lane": "pr",
                              "remote_url": "git@github.com:acme/app.git",
                              "learned_from": "GH006 protected branch",
                              "ts": "2026-07-16T21:04:11Z" } } }
```

- Written only on an unambiguous policy denial (§3.4).
- Consulted at resolution step 2. Ignored and dropped if the repo's
  current remote URL no longer matches `remote_url` (the repo moved; the
  lesson may be stale — re-learn).
- Survives `warden refresh` (lessons are remote truth, not disk truth).
- **Unlearn path:** `sudo warden forget <repo>` deletes the repo's entry
  and prints what was forgotten. A wrong lesson (remote outage that
  matched a pattern, protection since removed) must be correctable
  without permanent configuration or hand-editing root-owned files.
- A declaration (§5) always beats a lesson.
- `uninstall.sh` removes the file (it lives in the managed root it
  already deletes; stated here so it stays true).

Known asymmetry (deliberate): lanes only ever *learn* toward `pr` — the
pr lane's operations always succeed, so nothing ever observes "direct
push would have been allowed." The escape hatches for this ratchet are
`warden forget` (drop a wrong lesson) and declaration (override
inference). This is the design's floor: Warden never discovers push
permission by pushing somewhere it wasn't told or taught to.

## 5. Declared lanes

`.warden.json` at the repo root, tracked in the repo:

```json
{ "version": 1, "lane": "pr" }
```

`lane` is the only recognized key in v1; unknown keys and future versions
are read tolerantly (unknown keys ignored). Invalid values → the file is
ignored and resolution falls through to learned/inferred, with the
problem named in the land result.

The daemon reads the file as `git show HEAD:.warden.json` in the shared
checkout — the landed, committed truth. Never the working tree (stray
edits at a repo root must not steer the daemon), and never the requesting
session's worktree.

This exists for exactly one shape inference cannot see: a work repo where
PRs are required by convention but not enforced by the remote (§2's
accepted residual). Everything else self-resolves; most repos never carry
this file.

Trust note: the file is session-writable (a session can land a change to
it). Accepted deliberately — the remote still enforces any real
protection; a wrong declaration produces a loud rejection or a
locally-wrong-but-reported behavior, and the threat model here is session
*accidents* on Jay's own machine, not adversarial sessions.

## 6. Surfaces

- `warden land [branch]` — unchanged invocation. Result lines name the
  lane and, for `pr`, the URL:
  `warden land: pr-opened https://github.com/acme/app/pull/312`.
- `warden forget <repo>` — drops a learned lesson (§4). Requires sudo
  (the file is root-owned), mirroring `warden refresh`.
- `warden status` — gains a lane column per adopted repo, showing the
  *resolved* lane and its provenance: `pr (declared)`, `push (inferred:
  remoted)`, `pr (learned 2026-07-16)`. Read-only; resolution logic
  shared with landd (same module, imported by both).
- Result JSON statuses: `landed`, `landed-remote-only`, `pr-opened`,
  `pr-exists`, `branch-pushed`, `rejected`. Every `rejected` reason still
  names the fix.
- Result files: the daemon writes `<request>.result` and leaves it; a
  sweep in each daemon pass deletes result files older than 7 days, so a
  session that crashed mid-poll can still find its answer later.
- CLI timeout: 90s, justified by the trigger design — the queue dir is a
  launchd `WatchPath` (near-instant wake on request write) with a 60s
  `StartInterval` fallback, so 90s covers one full missed-watch interval
  plus execution. Remoted lanes add network time; the timeout message
  distinguishes "no response yet — remoted lands can be slow, check
  `<request>.result`" from "daemon not loaded" by checking whether the
  launchd job exists.
- Audit: every land result already goes to `logger -t warden`; lane,
  lesson writes, forgets, and repeated-rejection tallies are included in
  the event payload.

## 7. Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `lanes.py` (new) | resolve(repo_root) → (lane, provenance); read declaration (`git show`), learned store, remote config, gh account name | git read-only, learned.json |
| `landd.py` (extended) | executor: runs a lane, routes `policy-denied` verdicts to the pr lane, writes lessons; policy-denial classifier (§3.4); demoted `gh` runner; result sweep | lanes.py, git, gh |
| `bin/warden` | unchanged `land`; `forget`; `status` prints lanes via lanes.py | lanes.py |
| `learned.json` | persistent remote lessons | written by landd only |
| `.warden.json` | per-repo declaration | authored by humans/sessions |

Each unit answerable independently: lanes.py is pure resolution (no
mutation, fully unit-testable); landd owns all mutation and all routing;
the CLI only formats. Lanes are small strategies over a shared
fetch-sync-verify prefix; a future `glab` lane is a new strategy plus
classifier fixtures, touching neither routing nor the invariant.

## 8. Error handling principles

- **Push-first ordering**: the shared checkout is mutated only after the
  remote has accepted the same SHA — no rollback paths, because no
  premature mutation.
- **Degrade, don't undo**: when a later step fails (gh token invalid,
  local ff blocked), earlier completed steps stand and the result names
  precisely how far it got (`branch-pushed`, `landed-remote-only`).
- **Learn conservatively, forget cheaply**: lessons require unambiguous
  policy denials; wrong lessons are one `warden forget` away.
- **Every rejection carries its fix**, v1 discipline continued.
- **Invariant violations reject loudly** rather than "helpfully"
  repairing (Warden never force-anythings a shared checkout).

## 9. Testing

All runnable offline, extending `tests/test_landd.py`. Risk-ordered: the
states the panel doubted, not the mechanisms the design likes.

- **Regression gate, literal**: the existing v1 test file runs unmodified
  against the new landd. If any v1 test needs editing, the gate failed.
- **Classifier**: fixture table of *captured real transcripts* (GitHub
  GH006 to start; GitLab/Gitea rows added from real captures, not
  invented text). Ambiguous-rejection case asserts refusal **with no
  lesson written**. A policy-looking message on a *transport* failure
  (condition 1 of §3.4 unmet) likewise writes no lesson.
- **Lane resolution**: declared/learned/inferred precedence; stale
  remote-URL invalidation; invalid declaration fallthrough; remoted →
  `push` default; no-remote → `local`.
- **Identity reporting**: hosts.yml with multiple accounts on one host
  and multiple hosts (acting account resolved per-host; every pr-lane
  result names it); hosts.yml absent.
- **`push`**: bare-repo origin fixture; happy path; behind-origin sync;
  non-ff race (push to the bare repo between fetch and land);
  `landed-remote-only` (checkout made unmergeable after remote accept —
  e.g. dirty it mid-test via the test's own hook); dirty checkout.
- **Policy fallback**: pre-receive hook in the bare fixture rejecting
  with a *captured* GH006 transcript → assert executor re-dispatches to
  pr behavior + lesson written; second land consults the lesson without
  re-attempting the push; `warden forget` then restores push behavior —
  the round trip must not require editing files by hand.
- **`pr`**: fake `gh` shim on PATH recording argv and returning a canned
  URL; created / already-exists / gh-absent (`branch-pushed`) paths; PR
  base asserted to come from `origin/HEAD`, not the local branch name.
- **Queue**: result-file retention sweep; duplicate processing after a
  simulated crash (request redelivered → idempotent outcome).
- **Selftest**: activation-day checks — `warden status` shows a lane with
  provenance for every adopted repo; a no-remote repo lands byte-identical
  to v1.

## 10. Out of scope (v1 of lanes)

- Post-merge branch cleanup on remotes.
- Draft PRs, reviewers, labels, PR body templates (`--fill` only).
- Non-GitHub PR/MR creation (GitLab etc. get `branch-pushed` + a correct
  message; add a `glab` strategy when a real repo needs it).
- Probing branch-protection APIs — rejected in favor of learning from
  real pushes: works on any host, needs no working auth on the land
  path, and cannot disagree with reality. (The own-account check in §2
  reads gh's config from disk; it makes no network calls either.)
- Learning `pr → push` (deliberate one-way ratchet, §4).
- Per-branch lane maps (different lanes for different target branches).

## 11. Requirements mapping

| Need (Jay, 2026-07-16) | Answered by |
|---|---|
| no repo gets PR ceremony unless its remote's rules or an explicit declaration require it (approved form, 2026-07-16 — replaces the categorical "personal projects never need PR", which fails when a project's shape changes) | the `pr` lane is reachable only by a remote's policy denial or a committed declaration; absent both, no-remote → `local` (network never touched; selftest asserts v1-identical behavior) and remoted → `push` (direct integration, no ceremony) |
| work project, PR required | learned `pr` from the remote's own denial (protection), or declared when only conventional; Warden pushes the branch and opens the PR itself |
| work project, no PR required | inferred `push`; Warden never manufactures a PR nobody required |
| branch protection varies | remote's own denials are the source of truth; lessons persist, `warden forget` corrects them |
| zero involvement from Jay | every lane completes or degrades without a manual step; rejections name fixes addressed to the *session*, never to Jay |
| elegance / no config tax | zero mandatory configuration; the only declaration case is convention-only PR repos (§2 residual) |

## 12. Panel review record (2026-07-16)

Spec panel (Wiegers, Adzic, Cockburn, Fowler, Nygard, Newman, Hohpe,
Crispin) reviewed draft v1. Changes folded into v2:

1. Inference default narrowed: `push` only for own-account remotes;
   unknown/org → `pr` (Cockburn's rogue-push objection). **Reversed in v3
   by requirements ruling** — see below.
2. `warden forget` unlearn path; lessons written only on unambiguous
   ref-update policy denials (Nygard, Wiegers, Crispin).
3. `landed-remote-only` status for the push-accepted/local-ff-failed
   partial state (Wiegers).
4. Classifier contract: real-transcript fixture table, ambiguous →
   refuse without learning (Adzic).
5. Fallback lifted to executor routing; lanes are strategies over a
   shared fetch-sync-verify prefix (Fowler, Hohpe).
6. Contract fixes: version fields in both JSON files; PR base from
   `origin/HEAD`; declaration read via `git show HEAD:`; result-file
   retention + timeout rationale; `warden refresh` syncs remoted repos
   (Newman, Fowler, Hohpe, Cockburn).

Open questions from the panel, resolved from disk the same day: gh is
configured for `jayheavner` but the token is currently invalid — the pr
lane degrades to `branch-pushed` until `gh auth refresh` (the design
needs no change; noted for activation). All five adopted repos are
remoteless or own-account GitHub remotes today, so every current repo
infers `local` or `push` and nothing changes behavior until an org repo
is cloned. No local/remote default-branch divergence exists today; the
`origin/HEAD` fix is kept as cheap insurance.

Post-panel correction (Jay, same day): draft v2 modeled gh as holding a
single account. Wrong — Jay operates multiple GitHub profiles, and
hosts.yml is natively a multi-account, multi-host store even though this
machine currently shows one entry. v2.1 specified pr-lane identity
selection (active account per host, named in every result, degradation
path when it lacks access). Lesson recorded: verify assumptions against
the data model's capacity, not just its current contents.

Requirements ruling (Jay, same day) → v3: the panel's org-remotes-default-
to-`pr` narrowing violated the governing requirement — Warden must never
manufacture integration work for Jay, and an unrequired PR is exactly
that (a human must merge it). Reverted: any remote infers `push`; the
remote's policy denial is the sole automatic router to `pr`; declaration
covers only convention-only PR repos. The rogue-push residual on
convention-only repos is accepted and stated in §2. Requirements outrank
panel safety preferences; hosts.yml account matching is no longer part of
lane resolution and survives only as pr-lane identity reporting.
