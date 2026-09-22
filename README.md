# BugVault

**Competitive bug-bounty escrow with on-chain AI adjudication — pure Intelligent Contracts for GenLayer.**

A sponsor locks GEN into a bounty with a written scope. Any researcher submits a claim (a public URL to a vulnerability report). The sponsor can approve directly — **no AI involved**. But if the sponsor rejects a report the researcher believes in, or simply goes silent, the researcher escalates to **on-chain arbitration**: every validator independently fetches the report and asks an LLM whether it satisfies the scope, then the network reaches consensus on a single verdict — `VALID` / `INVALID` / `INCONCLUSIVE`.

> This repo is **contracts-only**: no frontend, no app server. Submit the whole folder as-is; judges and developers get everything needed to lint, test (online and offline), deploy and interact.

---

## Last Update — 2026, v2 challenge-window deployment

The corrected BugVault is deployed at
[`0xD57A790cDc4A6a456A2C064D80153B0cb679d724`](https://explorer-studio.genlayer.com/address/0xD57A790cDc4A6a456A2C064D80153B0cb679d724).

What changed in this release:

* `Claim` now records `rejected_epoch`; each rejection exposes an exact
  `challenge_deadline_epoch` and live `challengeable` flag.
* Researchers have an enforced 3-day `CHALLENGE_WINDOW_SECONDS` after rejection.
* A shared `_refund_blocker` prevents **both** `cancel_bounty` and
  `reclaim_expired_bounty` from moving escrow while a submitted claim or challengeable
  rejection exists.
* A late challenge is rejected normally and its attached bond is refunded — payable safety is
  preserved.
* Regression cases cover cancel-vs-challenge, TTL-reclaim-vs-challenge and late-dispute refund.
* The deployment manifest, live smoke defaults, CLI sample, docs and website now target v2.
* Payable TypeScript samples now use distinct sponsor/researcher signers and discover IDs from
  live views instead of assuming one account can play both roles.
* The old v1 vault remains listed only as an unsafe historical deployment; do not send GEN to it.

CredPass is still deployed at `0x2c9d...BF39`. Its oracle must report the v2 vault from
`get_stats()`; if not, the owner must call `set_oracle(v2)`. The live smoke suite enforces this
relationship and fails until the pair is wired correctly.

---

## Guarantees

| Guarantee | How it's enforced |
| --- | --- |
| Every wei is conserved | No pools, no leverage. Each bounty is either paid to a researcher or refunded to the sponsor. Fee is split **once, up front** at escrow time. |
| No silent-counterparty trap | Sponsor silent for `REVIEW_TIMEOUT_SECONDS` (5d) → researcher escalates alone (bond always returned). Nobody claims for `BOUNTY_TTL_SECONDS` (45d) → anyone may permissionlessly refund the sponsor. |
| Rejection can't be used to drain escrow | A `REJECTED` claim stamps `rejected_epoch`. For the whole `CHALLENGE_WINDOW_SECONDS` (3d) neither `cancel_bounty` nor `reclaim_expired_bounty` can move the escrow — the dispute right comes first. After the window, the rejection is final and challenges are refused (with refund). |
| Payable methods never strand funds | `create_bounty` / `dispute_claim` never `raise`; every rejection refunds and returns `{"ok": false, ...}`. Enforced by an AST test, not by convention. |
| Transient failures aren't verdicts | fetch/LLM outages raise tagged `[TRANSIENT_*]` errors → `RETRY_LATER`, bond back, state untouched. `INCONCLUSIVE` is reserved for a real business outcome. |
| No rug while review is pending | `cancel_bounty` refuses to run while any claim awaits review. |
| Fee sweep can't graze payouts | `sweep_fees` is cooldown-gated behind the last outbound transfer (finalization safety). |

## Repository layout

```
bugvault/
├─ contracts/
│  ├─ bug_vault.py            # escrow + AI adjudication engine
│  └─ cred_pass.py            # composable gate: private programs keyed to live credibility
├─ tests/
│  ├─ offline/                # ZERO-dependency tests that execute the real contract code
│  │  ├─ genlayer_stub.py     # stdlib-only stand-in for the genlayer SDK
│  │  ├─ test_bug_vault_unit.py
│  │  ├─ test_cred_pass_unit.py
│  │  ├─ test_payable_never_raises.py
│  │  └─ test_deployment_manifest.py
│  └─ integration/            # real-consensus tests for the simulator / studionet
│     ├─ conftest.py
│     ├─ test_bug_vault_chain.py
│     └─ test_live_smoke.py
├─ samples/
│  ├─ quickstart_cli.sh       # deploy + reads with only the genlayer CLI
│  ├─ deploy_and_fund.ts      # genlayer-js happy path incl. payable escrow
│  └─ dispute_flow.ts         # reject → dispute → AI verdict, with the bond
├─ docs/
│  ├─ ARCHITECTURE.md         # lifecycle, money model, equivalence principle
│  ├─ API.md                  # every public method, params, returns, reverts
│  ├─ TESTING.md              # how to run everything, how to extend
│  └─ RELEASE_NOTES.md        # v2 fix, tests and migration notes
├─ deployments/
│  └─ studionet.json          # deployed addresses + constructor args (machine-readable)
├─ DEPLOYMENTS.md             # live addresses, verification commands, upgrade policy
└─ requirements.txt
```

## Quickstart (5 minutes, no chain required)

```bash
pip install -r requirements.txt        # or just: python3 — offline tests need nothing

# 1. Lint the contracts against the real GenVM SDK
genvm-lint check contracts/bug_vault.py
genvm-lint check contracts/cred_pass.py

# 2. Run the offline suite — executes the REAL contracts via the stub
python3 -m pytest tests/offline -v
#    (stdlib-only equivalent: python3 -m unittest discover -s tests/offline -p "test_*.py" -v)

# 3. Run only the release/deployment consistency checks
python3 -m pytest tests/offline/test_deployment_manifest.py -v
```

## Integration tests (local validator network)

```bash
npm install -g genlayer
genlayer init && genlayer up
gltest tests/integration -v
RUN_LLM_TESTS=1 REPORT_URL="https://your.public/report" gltest tests/integration -v
```

## Deploy to Studionet

```bash
genlayer network set studionet
genlayer deploy --contract contracts/bug_vault.py --args 200          # fee bps
genlayer deploy --contract contracts/cred_pass.py --args "0xVAULT…"   # oracle address
```

Payable writes (`create_bounty`, `dispute_claim`) need value attached — use
`samples/deploy_and_fund.ts` / `samples/dispute_flow.ts` (genlayer-js), since the CLI write
command cannot attach GEN. Both samples require **separate sponsor and researcher keys** because
the contract correctly forbids a sponsor from claiming their own bounty. Or run the calls
click-wise in [GenLayer Studio](https://studio.genlayer.com).

## Live deployment (Studionet)

| Contract | Address | Status | Explorer |
| --- | --- | --- | --- |
| BugVault v2 | `0xD57A790cDc4A6a456A2C064D80153B0cb679d724` | current, challenge-window fix | [open](https://explorer-studio.genlayer.com/address/0xD57A790cDc4A6a456A2C064D80153B0cb679d724) |
| CredPass | `0x2c9d68A00f110DCB2f9CBb41b77A748c080cBF39` | verify `get_stats().oracle == BugVault v2` | [open](https://explorer-studio.genlayer.com/address/0x2c9d68A00f110DCB2f9CBb41b77A748c080cBF39) |

```bash
# read-only sanity check (details + scripted version in DEPLOYMENTS.md)
genlayer call 0xD57A790cDc4A6a456A2C064D80153B0cb679d724 get_config
genlayer call 0x2c9d68A00f110DCB2f9CBb41b77A748c080cBF39 get_tier_thresholds
LIVE_SMOKE=1 python3 -m pytest tests/integration/test_live_smoke.py -v
```

## The two contracts

**`bug_vault.py`** — escrow + adjudication. Sponsor escrows a bounty; researchers race claims; first terminal-valid claim takes the reward; losing claims never touch it. Arbitration is a leader/validator Equivalence-Principle judgment comparing **only the verdict field** (severity, confidence, reasoning are stored as evidence, never compared — two LLM calls word things differently even when they agree).

**`cred_pass.py`** — composability demo that asks *nothing* of the vault: private bug-bounty programs with credibility bars (`APPRENTICE` → `ELITE`), each checked **live** on every call against the vault's `has_credibility` view via a synchronous IC-to-IC call. Nothing is cached, so nothing can go stale; programs snapshot their bar at creation so later tier edits never silently re-gate them.

See `docs/ARCHITECTURE.md` for the full lifecycle and `docs/API.md` for the method reference.
