# BugVault — Architecture

> **Deployed v2:** `0xD57A790cDc4A6a456A2C064D80153B0cb679d724`. The unique build marker is
> `get_config().challenge_window_seconds == 259200`. CredPass composition is valid only when
> its live `get_stats().oracle` equals this address.

## Why AI adjudication instead of a threshold check

Most oracle-style Intelligent Contracts answer an objective question: did a number cross a line. *"Does this report prove an in-scope vulnerability of at least the required severity"* has no such threshold — it is a judgment call. So the Equivalence Principle used here is not a numeric comparison; it is a **non-strict LLM judgment with exactly one compared field**. The leader fetches the report and asks an LLM to produce:

```json
{"verdict": "VALID|INVALID|INCONCLUSIVE", "severity": "...", "confidence": 0-100, "reasoning": "...≤220 chars"}
```

Every validator independently re-fetches and re-prompts. Only `verdict` is compared — `severity`, `confidence` and `reasoning` are stored as evidence but never compared, because two independent LLM calls will almost never produce identical wording even when they agree completely on the outcome. Comparing the whole object would land every arbitration `UNDETERMINED` regardless of how obvious the report is.

A separate `_coherent` check validates the leader's own report against itself (verdict is one of the three allowed values, severity in the known set, confidence an int 0–100, reasoning under the cap) — a pure function of the leader's own calldata, so every validator computes the identical answer and it can reject a malformed report without ever being itself a source of disagreement.

## Competition model

One bounty ⇒ one reward ⇒ first terminal-valid claim wins. After a win, every other still-`SUBMITTED` claim of that bounty becomes `VOIDED`. A `REJECTED` or `RESOLVED_INVALID` claim is only about that claim — the bounty stays `OPEN` for the next researcher. This makes the escrow atomic (one fixed reward, no per-milestone bookkeeping) while preserving a real market dynamic. Two anti-grief guards keep the market honest: the 8-slot claim cap per bounty, and **one live claim per researcher per bounty** — so no single actor can flood the queue (a `REJECTED` claim never blocks re-entry).

## Claim lifecycle

```
                       create_bounty (payable escrow)
                                 │
                        bounty: OPEN
                                 │
                    submit_claim (researcher(s), ≤ 8)
                                 │
                        claim: SUBMITTED ── approve_claim ──▶ APPROVED ──▶ bounty PAID_OUT
                                 │                              (payout, siblings VOIDED)
                                 ├─ reject_claim ──▶ REJECTED ──┐
                                 │        (challenge window:    │ dispute_claim (CHALLENGE mode,
                                 │         CHALLENGE_WINDOW_     │  within 3d of rejection; bond
                                 │         SECONDS = 3 days)     │  forfeited if AI=INVALID)
                                 │        ── window closes ─▶ REJECTION FINAL
                                 │            (dispute rejected with refund;
                                 │             cancel/reclaim unblocked)
                                 ├─ sponsor silent ≥ REVIEW_TIMEOUT_SECONDS
                                 │        dispute_claim (TIMEOUT mode, bond always returned)
                                 │                              │
                                 ▼                              ▼
                     on-chain arbitration (fetch + LLM + consensus)
                ┌────────────────┬────────────────┬────────────────┐
                ▼                ▼                ▼                ▼
           verdict VALID     INVALID         INCONCLUSIVE     transient
        RESOLVED_VALID    RESOLVED_INVALID   no state change   RETRY_LATER
        payout + bond     challenge: bond    claim untouched    bond back,
        back              → sponsor          bond back          nothing recorded

   bounty-level exits — BOTH blocked while any claim is SUBMITTED or a
   rejection is still inside its challenge window (escrow must back a
   dispute the researcher still has a right to open):
     OPEN ─ cancel_bounty ─▶ CANCELED    (sponsor)
     OPEN ─ reclaim_expired_bounty ─▶ RECLAIMED  (permissionless, after BOUNTY_TTL)
```

## Money model

* **Conservation.** There is no shared risk pool and no multiplier: a bounty never promises more than it holds, so the contract never needs a solvency check. In/out per bounty: `escrow = fee (up front) + reward`; reward → winner or back to sponsor.
* **Auditable accounting.** `total_escrow_locked` / `total_paid_out` / `total_pending_fees` counters and per-researcher `earned` are updated on the exact same code paths that move funds, so `get_platform_stats` always reconciles with on-chain balances.
* **Payable safety.** `gl.vm.UserError` rolls back storage but keeps inbound value — so payable methods must **never raise**. Both payable methods refund-and-`{"ok": false}` on every rejection; an AST test fails the build if a `raise` ever appears inside one.
* **Bonds.** Opening arbitration costs `DISPUTE_BOND` (0.02 GEN). Returned in every outcome except one path: a *challenge* (disputing a sponsor's existing `REJECTED`) that the AI also calls `INVALID` forfeits the bond to the sponsor, compensating a wasted review. A *timeout* escalation (sponsor went silent) is never treated as a bet — the bond comes back whatever the verdict.
* **Dispute opportunity is structural, not polite.** A rejection stamps `rejected_epoch` and stays challengeable for `CHALLENGE_WINDOW_SECONDS` (3d). `cancel_bounty` and `reclaim_expired_bounty` both consult the same `_refund_blocker` — a pending review OR an open challenge window holds the escrow in place. Only after the window lapses is the rejection final (fresh disputes are refused with a refund) and the escrow refundable.
* **Sweep safety.** Outbound transfers commit at finalization, not acceptance; `sweep_fees` therefore refuses to run within `SWEEP_DELAY_SECONDS` of the last outbound transfer.

## Transient failures ≠ verdicts

`_judge` **returns** only genuine business outcomes (the page loaded and was judged; or loaded with nothing readable — that *is* `INCONCLUSIVE`). Anything infrastructural — fetch failure, LLM error, unparsable JSON, coherence failure — **raises** a `gl.vm.UserError` tagged `[TRANSIENT_FETCH]` / `[TRANSIENT_LLM]` / `[LLM_MALFORMED]`. `dispute_claim` / `preview_arbitration` map those to `RETRY_LATER`: bond returned, statuses and evidence fields untouched. A network hiccup must never be written into the permanent record as if it were a judgment about the report.

## Rendering real-world reports

Reports are frequently client-rendered SPAs or hosted PoCs. `_judge` renders with `wait_after_loaded="4s"` so validators judge the painted page, not an empty shell. Content is truncated to `MAX_RENDER_CHARS` before prompting for determinism-friendly prompt sizes.

## Storage schema (BugVault)

```
bounties:        TreeMap[u32, Bounty]        Bounty{ sponsor, title, scope, severity_policy,
sponsor_bounties:TreeMap[Address, DynArray]        reward_amount, fee_withheld, status,
claims:          TreeMap[u32, Claim]               created/settled_epoch, winning_claim_id,
bounty_claims:   TreeMap[u32, DynArray]            claim_count }
researcher_claims:TreeMap[Address, DynArray]
wins / attempts: TreeMap[Address, u32]       Claim{ researcher, report_url, note, status,
counters + total_pending_fees                     submitted_epoch, ai_* evidence, reject_reason,
last_outbound_epoch                               rejected_epoch, challenge_mode }
```

Everything derived (reputation, rates, histories) is **computed from claims at read time** — there is no cached reputation record to expire, revoke or sync. O(1) counters (`claim_count`, totals) replace any per-transaction rescans; pagination over histories is cursor-based with a hard `MAX_SCAN` bound.

## Composability (CredPass)

CredPass calls `has_credibility(researcher, min)` **live on every check** via a synchronous IC-to-IC `view()` call through a `@gl.contract_interface` stub — one-way dependency, the vault never knows the gate exists. Eligibility is therefore always as current as the vault itself; nothing needs caching, expiring or revoking. Programs snapshot their tier→number resolution at creation time (past enrollments are history, not live credentials), and enrollment is self-service: the researcher is the caller, no third party can spend your reputation for you.
