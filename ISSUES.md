# ISSUES (sweep 12 Nov 2026, branch sweep/failclosed-20261112)

Base: local main was STALE (28b0601, before PR #2). `git fetch` showed origin/main = 25b80fe (PR #2 merged). Branch fast-forwarded to it.

Checks (on 25b80fe): `make test` 182 OK; `make mutants` 62/64 killed. After: `make test` 184 OK; `make mutants` 64/64 killed.

- [L] PR #1 (Greptile audit, 7 findings) is fully covered by merged PR #2 — evidence: PR #2 body maps each of the 7 to a fix and a test; tests/test_greptile_audit.py is on main and `BaselineError` is in failclosed/mutate.py. Uncovered findings: none. Close #1 (Needs Aaron 1).
- [L] The 2 surviving mutants (evals.py:154, ledger.py:55, `sort_keys=True -> False`) are killable — tests/test_stable_bytes.py pins canonical key order; both flipped to red when I set sort_keys=False, then green again. FIXED.
- [L] `.mutants-allow` still lists those 2 entries and README still says "2 known equivalent mutants / 62/64". They are now stale, and the README section argues they cannot be killed. I left them: removing them breaks tests/test_mutate.py (asserts a non-empty allow-list) and the README test, which assumes a survivor — a design choice. README only changed 182 -> 184 tests.
- [L] Local checkout `~/failclosed` main is 1 commit behind origin (I fetched; no merge there).

## Needs Aaron
1. Close audit PR #1 (covered by #2): `gh pr close 1 --repo aaronsiebold/failclosed --comment "All 7 findings fixed in #2"`. Recommend: do it.
2. Retire the 2 stale allow-list entries and rewrite README's mutation section to 64/64 (test_mutate allow-list non-empty assertion + README test need updating). Recommend: later, optional.
3. Merge sweep/failclosed-20261112 into main and push (2 files + ISSUES.md).
