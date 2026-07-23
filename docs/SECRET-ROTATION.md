# Secret Rotation — Operator SOP

> **Audience:** operators with shell access to the RAG pod + the parser deployment unit.
> **Cadence:** every **90 days** (`Phase 8 Decision §2` locked 2026-07-23). Runs manually — no auto-rotation; the service has no Vault / AWS SM integration yet, and that integration is deferred to a separate effort.
> **Goal:** rotate `PARSER_TOKEN` and `ADMIN_KEY` without downtime, with a validator that catches typo-grade mistakes before they hit production.

This SOP is the operator-facing companion to:

- `scripts/validate_rotation.py` — the offline validator (entry-point shim)
- `rag/ekrs_rag/ops/validate_rotation.py` — the validator implementation (importable, unit-tested under `rag/tests/unit/test_validate_rotation.py`)
- `rag/ekrs_rag/security/parser_token.py` — `PARSER_TOKEN` enforcement
- `rag/ekrs_rag/security_legacy.py` — `ADMIN_KEY` enforcement (`X-Admin-Key`)

---

## 1. Pre-rotation checklist

Complete these **before** you start; failing one means postpone the rotation:

- [ ] **Window confirmed.** The rotation must run during a low-traffic window. The parser emits `/v1/ingestion/notify` and `/v1/ingestion/callback` continuously during normal operation; pick a 5-minute slot when ingestion is paused (e.g., scheduled ingest maintenance) or at >50% below the daily p99 RPS.
- [ ] **Downstream notified.** Parser team + on-call engineer both ack the change window in `#ekrs-ops`.
- [ ] **Audit snapshot taken.** Snapshot `/var/log/ekrs/audit.log` immediately before rotation so the rotation event can be cross-referenced post-hoc: `cp -p /var/log/ekrs/audit.log /tmp/audit-pre-rotation-$(date +%s).log`
- [ ] **Validator available.** `python scripts/validate_rotation.py --help` prints usage; if not, `pip install -e rag` from the repo root first.
- [ ] **Reachable environment.** You can `kubectl exec` into the `rag` pod and you have shell access to whatever runs the parser. Both sides are needed.

---

## 2. Generate the new secret

```bash
# 32+ char random; pipe through stdin (NOT command-line history).
openssl rand -hex 32 | tr -d '\n' > /tmp/new_secret.txt
chmod 600 /tmp/new_secret.txt
```

**Never echo the secret.** Read it as a shell variable, not via `echo $TOKEN` into history. Use the safe-piping pattern (`ghproxy` gets used for `gh` only; for credential rotation, a temp file with `chmod 600` is the convention EKRS has used since Phase 7 push gotchas).

---

## 3. Validate the rotation (offline)

The validator catches the two most common operator mistakes — both of which look like correct rotations to the naked eye:

| Mistake | Validator verdict |
|---------|-------------------|
| Re-entered the old secret by mistake (`--old == --new`) | `reject` ("identical") |
| Typed one character wrong (off-by-one during copy-paste) | `reject` ("typo-grade") |

Run it before touching any deployment unit:

```bash
# Read both secrets from files (NOT from CLI arg) so they don't land in shell history.
OLD=$(cat /tmp/old_secret.txt)
NEW=$(cat /tmp/new_secret.txt)
python scripts/validate_rotation.py \
    --old "$OLD" --new "$NEW" --kind parser
```

A safe rotation prints a single JSON line with `"verdict": "accept"`:

```json
{"rotation_timestamp": "2026-07-23T18:30:12Z", "kind": "parser",
 "old_token_length": 64, "new_token_length": 64,
 "shared_prefix_length": 0, "shared_prefix_ratio": 0.0,
 "verdict": "accept", "reason": "tokens are sufficiently distinct",
 "required_units": ["parser", "rag"]}
```

| Exit code | Meaning |
|-----------|---------|
| `0` | `accept` — proceed with rotation |
| `1` | `reject` — typo-grade; **do not proceed**, regenerate the secret |
| `2` | CLI error (missing flag, invalid choice) |
| `3` | Validation error (token below 32 chars, unknown `--kind`) |

`required_units` tells you which deployment units need their env updated. For `--kind parser` it's both `parser` and `rag`; for `--kind admin` it's `rag` only (admin endpoints live in the RAG service).

---

## 4. Rotation order (no downtime)

The RAG service accepts a **comma-separated list** of valid `PARSER_TOKEN` values (see `rag/ekrs_rag/api/auth.py::_parse_expected_tokens`). Use this overlap window to keep ingestion flowing during rotation.

**Order matters. Always rotate PARSER_TOKEN first, ADMIN_KEY second.**

| Step | Action | Why |
|------|--------|-----|
| 4.1 | Update **parser** env: `PARSER_TOKEN=<OLD>,<NEW>` (old FIRST, then new) | Parser immediately uses `<NEW>` for its next call, but if it falls back to old behaviour it still works. |
| 4.2 | Restart the parser process | Parser picks up the dual-accepting token list. |
| 4.3 | Wait 30 seconds | Let in-flight requests drain. |
| 4.4 | Update **rag** env: same `PARSER_TOKEN=<OLD>,<NEW>` shape | RAG accepts both tokens. |
| 4.5 | Rolling restart: `kubectl rollout restart deploy/rag` | New pods accept old + new tokens; old pods still accept old only — overlap holds. |
| 4.6 | Wait until `kubectl rollout status deploy/rag` reports `successfully rolled out` | Confirms all new pods live. |
| 4.7 | Smoke check: `bash scripts/mock_parser_notify.sh http://localhost:8000` returns `202 Accepted` | Verifies the new token works end-to-end. (The mock uses `$PARSER_TOKEN` so set that to `<NEW>` in your shell before running.) |
| 4.8 | Remove the old token: `PARSER_TOKEN=<NEW>` on both units, restart parser, rolling-restart rag | Now only the new token is accepted. |
| 4.9 | Repeat the same flow for `--kind admin` if rotating `ADMIN_KEY` (deployed on the `rag` unit only). |

### Common ordering mistakes

- **Updating both envs simultaneously then rolling-restarting only rag** → if parser's first request after the restart hits an old rag pod still using `<OLD>` with the parser now on `<NEW>`, you get a 401 storm. Avoid by always rolling the env updates AND the restarts together (step 4.1 → 4.5 as an atomic block).
- **Setting `PARSER_TOKEN=<NEW>` without the overlap comma-list** → during the rollout window, parser calls would 401. The overlap eliminates this entirely.

---

## 5. Rollback

If step 4.7 (smoke check) fails, or if `/v1/ingestion/notify` starts returning 401 in `audit.log` within 60 seconds of the rotation:

| Step | Action |
|------|--------|
| 5.1 | Revert `PARSER_TOKEN` on both units to `<OLD>` only |
| 5.2 | `kubectl rollout restart deploy/rag` (rolling restart back to old) |
| 5.3 | Restart the parser process |
| 5.4 | Open an incident in `#ekrs-ops` and pin the audit snapshot from step 1 — a transient auth failure during a planned rotation is the kind of thing on-call wants to know happened, even if resolved. |

The overlap pattern in step 4 makes rollback trivial: any pod that hasn't rolled yet still accepts `<OLD>`; restoring just means setting `<OLD>` and rolling-restarting.

---

## 6. Post-rotation verification

After the rotation succeeds:

```bash
# 1. Confirm /v1/constraints works end-to-end with the new token
curl -s -X POST http://localhost:8000/v1/constraints \
    -H "X-Parser-Token: $NEW_PARSER_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"query": "maximum operating temperature", "parameters": ["temperature"]}' \
    | jq .trace_id

# 2. Audit log: no qdrant_write_failed events since rotation
grep -c '"event":"qdrant_write_failed"' /var/log/ekrs/audit.log

# 3. Confirm old token no longer accepted
curl -s -o /dev/null -w "%{http_code}" \
    -X POST http://localhost:8000/v1/constraints \
    -H "X-Parser-Token: $OLD_PARSER_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{}'
# Expected: 401 (old token rejected)

# 4. (If ADMIN_KEY rotated) confirm admin endpoints reject the old key
curl -s -o /dev/null -w "%{http_code}" \
    -X POST http://localhost:8000/v1/admin/embedding-cache/flush \
    -H "X-Admin-Key: $OLD_ADMIN_KEY"
# Expected: 401
```

---

## 7. Audit trail

Every successful rotation should be logged externally (out-of-band, not in `audit.log` itself — that log is for application events, not operator actions). EKRS recommends:

- File an ops ticket titled `secret-rotation-YYYY-MM-DD` with the rotation_timestamp from the validator output, the deploy unit(s) affected, and a paste of the smoke-check curl output.
- Cross-link with the audit log snapshot from step 1 (`/tmp/audit-pre-rotation-*.log`) so if a downstream alerts on a post-rotation event, the link is intact.

---

## 8. Threat model (non-goals)

This SOP **does not** protect against:

- A malicious operator with shell access and the ability to write to the audit log itself → assume DBA-class trust is already required to run rotation; this SOP doesn't add more.
- Auto-rotation via Vault / AWS Secrets Manager → Phase 9+ work; the manual cadence above is what Phase 8 ships.
- mTLS service-to-service auth → PD-4 in `ekrs-handbook.md §6.2`.
- Audit-log tampering → the audit log is append-only JSONL signed by the RAG process; rotation cannot forge past entries.

---

## 9. References

- Phase 8 plan doc T8-2: `docs/superpowers/plans/2026-07-23-phase8-scope.md` §"T8-2"
- Phase 8 Decision §2 (cadence locked at 90 days manual rotation)
- Validator source: `rag/ekrs_rag/ops/validate_rotation.py`
- Validator tests: `rag/tests/unit/test_validate_rotation.py` (24 cases)
- Validator entry-point: `scripts/validate_rotation.py`
- `PARSER_TOKEN` enforcement: `rag/ekrs_rag/api/auth.py` (multi-token overlap window)
- `ADMIN_KEY` enforcement: `rag/ekrs_rag/security_legacy.py`
- Mock-notify smoke: `scripts/mock_parser_notify.sh`
- `audit.log` rotation: Phase 5.5 F (`RebuildingRotatingFileHandler` 100MB × 5 gzip)
- Phase 7 credential push gotchas: `~/.claude/projects/-home-pangzy-code-project-EKRS/memory/credential-handling-craft.md`
