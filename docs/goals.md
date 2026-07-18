# Warden goals — phase 1 (aligned 2026-07-17)

**The model.** Agent sessions on one Mac are a dev team sharing one box.
Git's team discipline — private branches, protected trunk, integration only
by merge — applied to agents, with worktrees playing the role of branches
because everyone shares one filesystem. A developer is never blocked from
his own machine; he's blocked from other people's branches and from
committing straight to trunk.

## Goals

1. **The boundary marker is `.git`.** A folder inside a git repo is project
   territory and governed. A vanilla folder is just the machine: freely
   writable, not Warden's business.
2. **One session, one repo, one worktree.** A session is pegged to one repo
   through the worktree it starts in — the only place in project territory
   it can write.
3. **Writes never cross three lines:** another session's worktree (another
   dev's branch — read freely, write never); trunk (any repo's shared
   checkout — read-only; moved only by `warden land`, executed by the root
   daemon); any other repo (a session writing in a repo it isn't pegged to
   is malfunctioning — denied, audited, told why; no grants, no lanes, no
   accommodation mechanisms, ever). Multi-repo workflows are solved above
   Warden by permission-scoped agents; there is no such thing as one task
   spanning repos.
4. **The machine is not the protected object.** Everything outside repo
   territory stays writable. Policy derives only from repo structure on
   disk — Warden never enumerates what tools may write, so a new tool never
   requires a Warden change.
5. **Enforcement is structural, layered, non-optional for sessions:**
   Warden's own macOS Seatbelt profile (applied at session launch by the
   `claude` shim), non-removable hooks, the git-ref hook, root-owned
   rendered policy. Convention and prose count for nothing.

   **Warden blocks zero networking and zero commands** (invariant, selftest
   T20). Every command runs and reaches the network exactly as an
   ungoverned shell would; the wall denies only filesystem *writes* to
   protected surfaces. Warden delivers its own Seatbelt profile rather than
   enabling Claude Code's native sandbox precisely because the native
   sandbox cannot be filesystem-only — it forces a network proxy that
   breaks `gh`/Node TLS and denies keychain writes, with no off switch
   (proven 2026-07-18; see [upstream-asks.md](upstream-asks.md)).

   **No enforcement mechanism is adopted without its full side-effect
   surface inventoried against these goals** (the rule learned from the
   native-sandbox episode: it was adopted as a filesystem firewall, but its
   network/credential side effects were discovered incident by incident in
   production).
6. **Fail-closed for the repos, never fail-broken for the machine.** Walls
   state their rule and the sanctioned path when they fire; every denial is
   explainable in one command (`warden doctor`); the disable failsafe
   always works from a plain terminal.
7. **Humans are exempt.** Warden governs agent sessions only.
8. **Codex is first-class.** Same model, own managed delivery layer.
9. **Everything attributable.** Every judgment carries session, tool,
   target, rule, timestamp in the audit trail.

## Not goals in this phase

RBAC on this box, network policy, machine hardening, multi-repo workflow
support, or any mechanism that relaxes isolation.

## Future phases (recorded, not designed)

Phase 1 discipline is limited to git repos, which makes Warden suitable for
admin users only. Later phases extend the discipline to other parts of the
system, take more advantage of the sandboxes, and introduce per-user
permission levels (RBAC) — different users, different permissions —
delivered when the Enterprise-side policy channel is ready.
