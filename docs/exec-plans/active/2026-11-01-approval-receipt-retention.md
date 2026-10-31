# Approval receipt retention implementation plan

> **For agentic workers:** Use superpowers:executing-plans task by task.

**Goal:** Keep a consumed approval receipt unusable throughout its accepted lifetime.
**Architecture:** Preserve JWT acceptance and the unique JTI claim; delay cleanup beyond expiration plus configured clock leeway and bounded replica-clock allowance. Existing stored expiration timestamps retain their meaning.
**Tech Stack:** Python, PyJWT, SQLAlchemy, pytest.
**Spec:** Accepted scope below, approved in the project audit conversation on 2026-11-01.

## Accepted scope and global constraints

- Do not shorten legitimate approval validity or change receipt format/bindings.
- Do not add database columns or dependencies.
- Cleanup must retain consumed receipts during JWT leeway, including an allowance for replica clock differences. Retention settings must be consistent across gateway replicas.
- Existing raw expiration rows must receive the protection too; document rolling-upgrade limitations.

## Review focus

- Cleanup inside the accepted clock-skew interval must not reopen a used JTI.
- Fully expired records must eventually be removed.
- Concurrent claims must allow exactly one caller.
- Valid, mismatched, and expired receipts must keep their established outcomes.
- Zero skew and pre-existing rows must behave consistently.

## Task 1: Align cleanup with acceptance

**Files:** `app/mcp/approval_receipts.py`, `test/test_approval_receipts.py`, `docs/security-model.md`.
**Interfaces:** Preserve `claim(jti, expires_at)` and `cleanup_expired()`.

- [x] Add tests using a real isolated SQLAlchemy store and controlled clock, including validation after cleanup within leeway and concurrent claims.
- [x] Run `.venv/bin/python -m pytest test/test_approval_receipts.py`; expect the cleanup/replay case to fail before implementation.
- [x] Change only cleanup retention and its explanatory comment.
- [x] Run receipt/tool-call regression tests, `task validate`, and `task unit-test`; document outcomes.
- [x] Document the retention/rollout invariant and commit as `fix: retain consumed approval receipts through clock skew`.

## Validation evidence

`task validate`: passed with 922 unit/process tests and 52 keyless evaluations. Targeted receipt tests: 18 passed. An initial sandbox run could not bind process-test ports; after granting loopback access one unrelated TLS drain test failed intermittently, passed in isolation, and the final full suite passed.

Implementation verified; awaiting integration. No database migration or dependency change.
