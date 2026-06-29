# SDD progress — Spec A (appliance ops & visibility)

Plan: stremio-images/docs/superpowers/plans/2026-06-28-appliance-ops-visibility.md
Base commits: server(stremio-libtorrent-server)=8c2f9d1  images(stremio-images)=8c2f9d1

- Task 1 (server /cache.json): pending
- Task 2 (server /cache/remove): pending
- Task 3 (config-web /api/logs): pending
- Task 4 (config-web Logs card): pending
- Task 5 (config-web /api/cache): pending
- Task 6 (config-web Cached card): pending
- Task 7 (installer disk size): pending

Task 1: complete (commits 8c2f9d1..d061497, review clean)
Task 2: complete (commits d061497..26e0572, review clean)
Task 3: complete (images c2eddd1..2e85820, review clean, ruff+17 tests green)
Task 4: complete (images 2e85820..ffafd99, review clean, preview verified)
Task 5: complete (images ffafd99..920531a, review clean, 20 tests green)
Task 6: complete (images 920531a..2d5bad7, review clean, preview verified)
Task 7: complete (images 2d5bad7..a197400, review clean, bash -n)
ALL TASKS COMPLETE. Pending: final whole-branch review + push both repos.
PUSHED: server d061497..26e0572 (main), images 2e85820..a197400 (master). Final review READY. Full suites green (server 91, config-web 63).

# SDD progress — Spec B (pinned seeding library)
Bases: server=26e0572  images=c94a99f
- T1 pure foundations: pending
- T2 engine resume: pending
- T3 engine pin+evict: pending
- T4 server pins API: pending
- T5 config-web proxy: pending
- T6 config-web UI: pending
- T7 suggestions tie-in: pending
- R release 0.2.7: pending
- I appliance rebake: pending
B-T1: complete (server 26e0572..cb80d3c, review clean, 5 tests)
NOTE: stremio SSH = stremio.karadimov.info (no alias); host venv python broke (3.12->3.13) — use `uv run` on stremio.
B-T2: complete (server cb80d3c..f68a9c7, review clean, integration 1 passed on stremio). Minors (leave): shutdown stop-seq, wait_for_alert retval ignored.
B-T3: complete (server f68a9c7..c5d65d8, review approved + fix c5d65d8 for re-pin double-count; local 13 + remote 2 pass)
B-T4: complete (server c5d65d8..300a192, review approved; 3+100 tests pass; libtorrent import made optional for app importability).
B-T5: complete (images c94a99f..d44a18c, review clean, 23 tests; 409 surfacing verified)
B-T6: complete (images d44a18c..22ddbc1, review clean, preview verified). Minors (leave, codebase convention): empty-state flicker, unpin no re-enable-on-error.
B-T7: complete (images 22ddbc1..f5fe208, review clean, 8 tests; modified test adjudicated OK)
ALL B CODE TASKS DONE. Pending: final review, push images, release 0.2.7 (B-R), rebake (B-I).
B-T8: complete (server 300a192..afa1b79, review clean, local 22 + remote 3 pass). I-1 idle-pin FIXED.
B-R: complete (0.2.7 tagged 3a00a4b; docker 0.2.7+latest digest 6063805f, smoke-tested green; GitHub release v0.2.7 created). NOTE: stremio host .venv broken (perms on .venv/.lock) - host pytest needs venv recreate; image build unaffected.
B-I: BLOCKED — stremio-build (192.168.5.116) unreachable from this env AND from stremio (No route to host -> builder VM likely powered off). Rebake must run on stremio-build when it is back online. 0.2.7 + config-web code are published/pushed and ready.
B-I: COMPLETE 2026-06-29 (builder back online). Rebaked 20260629 artifacts (img.xz/ova/clonezilla) with server 0.2.7 (digest 6063805f docker-pulled into image) + console-status .deb (Spec A+B UI: Pinned/Cached/Logs cards + desktop launch-flag line all present). NOTE: console-status .deb version still 0.1.0 (content current; label unbumped). Boot smoke-test (run the OVA, check :8090 + /health) left as human step.
