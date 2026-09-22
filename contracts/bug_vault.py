# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
from datetime import datetime, timezone
import json

# ============================================================================
# BugVault — competitive bug-bounty escrow with on-chain AI adjudication
# ============================================================================
#
# A sponsor locks a reward into a bounty with a written scope. Any security
# researcher may submit a claim: a public URL pointing at a vulnerability
# report. The sponsor can approve a claim directly (fast path, no AI) — but
# if the sponsor rejects a claim the researcher disputes, or the sponsor
# simply never reviews it, the researcher can escalate to on-chain
# arbitration: every validator independently fetches the report and asks an
# LLM whether it demonstrates a real vulnerability inside the written scope,
# then the network reaches consensus on exactly one thing — the verdict
# (VALID / INVALID / INCONCLUSIVE).
#
# The question here is genuinely subjective — "does this report prove a
# vulnerability matching this scope and severity bar" has no numeric
# threshold — so the Equivalence Principle is a non-strict LLM judgment with
# a single compared field (verdict). Severity, confidence and reasoning are
# stored as evidence but never compared, because two independent LLM calls
# almost never produce identical wording even when they fully agree on the
# outcome. Comparing the whole object would make every arbitration land
# UNDETERMINED regardless of how obvious the report is.
#
# Competition model: multiple researchers race for ONE reward. The first
# claim that reaches a terminal valid outcome (direct approval or an AI
# verdict of VALID) takes the bounty; every other still-pending claim is
# voided. A rejected or invalidated claim never touches the reward — the
# bounty stays open for the next contender.
#
# Money-safety rule (load-bearing, not optional): a `@gl.public.write.payable`
# method must NEVER raise. `gl.vm.UserError` rolls back contract STORAGE but
# does not return the value that rode in with the call — the GEN would sit in
# the contract, unaccounted for. So every rejection of a payable call refunds
# the sender and returns a normal `{"ok": false, ...}` response instead of
# raising, and callers must read `ok`. The AST test in
# tests/offline/test_payable_never_raises.py enforces this mechanically.
#
# Two-sided "nobody can trap the other side's money" guarantee:
#   - a sponsor who goes silent after a submission does not freeze the
#     researcher forever: after REVIEW_TIMEOUT_SECONDS the researcher may
#     escalate to arbitration unilaterally;
#   - a sponsor who abandons a bounty nobody claims does not lose the escrow:
#     after BOUNTY_TTL_SECONDS anyone may permissionlessly send the reward
#     back to the sponsor (permissionless on purpose — a release path only
#     one party can trigger is not a guarantee).
#
# There is no shared risk pool and no payout multiplier: every wei escrowed
# for a bounty is either paid to a researcher or refunded to the sponsor,
# minus a platform fee withheld once, up front, at escrow time. This contract
# never has to check solvency, because it never promises more than it holds.

BOUNTY_OPEN = "OPEN"
BOUNTY_PAID_OUT = "PAID_OUT"
BOUNTY_CANCELED = "CANCELED"
BOUNTY_RECLAIMED = "RECLAIMED"

CLAIM_SUBMITTED = "SUBMITTED"
CLAIM_APPROVED = "APPROVED"                # terminal — sponsor approved directly
CLAIM_REJECTED = "REJECTED"                # sponsor rejected; researcher may still dispute
CLAIM_RESOLVED_VALID = "RESOLVED_VALID"    # terminal — AI arbitration says VALID
CLAIM_RESOLVED_INVALID = "RESOLVED_INVALID"  # terminal — AI arbitration says INVALID
CLAIM_VOIDED = "VOIDED"                    # terminal — another claim won the bounty

VERDICT_VALID = "VALID"
VERDICT_INVALID = "INVALID"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"

SEVERITIES = ["NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"]

BPS_DENOM = 10000
DEFAULT_FEE_BPS = 200       # 2.0%, withheld once at escrow time
MAX_FEE_BPS = 800           # 8% hard ceiling, enforced on the setter

DISPUTE_BOND = 2 * 10**16   # 0.02 GEN, required to open arbitration
MIN_REWARD = 10**15         # 0.001 GEN net reward after the fee
MAX_REWARD = 100 * 10**18   # 100 GEN gross escrow ceiling

MAX_CLAIMS_PER_BOUNTY = 8
MAX_TITLE_LEN = 120
MAX_SCOPE_LEN = 900
MAX_POLICY_LEN = 400
MAX_URL_LEN = 400
MAX_NOTE_LEN = 400
MAX_REASON_LEN = 300
MAX_RENDER_CHARS = 6000
MAX_REASONING_CHARS = 220

# A sponsor that never approves, rejects, or cancels leaves a claim hanging
# indefinitely. After this long since submission, the researcher may escalate
# to arbitration on their own — the bond is always returned in this mode.
REVIEW_TIMEOUT_SECONDS = 5 * 86400

# A rejection is NOT final the moment the sponsor sends it: the researcher
# gets this long from the rejection to invoke arbitration. Both escrow-refund
# paths (cancel_bounty and reclaim_expired_bounty) stay blocked for the WHOLE
# window — otherwise the sponsor could reject a claim and immediately drain
# the escrow before the researcher ever got the dispute heard.
CHALLENGE_WINDOW_SECONDS = 3 * 86400

# A bounty nobody claims within this window is refundable to the sponsor by
# anyone, permissionlessly. It is a TTL on the escrow, not on the research.
BOUNTY_TTL_SECONDS = 45 * 86400

# Outbound transfers apply on finalization, not acceptance, so a payout still
# sits in the on-chain balance briefly after the receipt says it succeeded.
# Sweeping fee revenue within this window of the last transfer would risk
# sweeping money that is, for a few more blocks, still committed.
SWEEP_DELAY_SECONDS = 3600

# Real reports are frequently client-rendered pages (hosted PoCs, SPA write-
# ups), not static HTML. Without a render delay the validators can capture
# the page before its own scripts have painted, and would judge an empty
# shell instead of the actual report.
RENDER_WAIT_AFTER_LOADED = "4s"

MAX_SCAN = 200
MAX_PAGE = 40

# ── Nondeterministic work lives at module level and captures only plain
# primitives passed in as arguments — never `self`, never a storage-backed
# dataclass. A closure over a storage object drags it into pickling for the
# sub-VM and fails the leader before any fetch even happens.
#
# Error taxonomy: _judge RAISES for anything that looks like infrastructure
# trouble rather than a fact about the report itself, so a timeout and a
# genuine "judged but undecidable" never look the same to a caller.
ERR_TRANSIENT_FETCH = "[TRANSIENT_FETCH]"
ERR_TRANSIENT_LLM = "[TRANSIENT_LLM]"
ERR_LLM_MALFORMED = "[LLM_MALFORMED]"

_EQ_PRINCIPLE = (
    "Compare two vulnerability assessments of the same report. Treat them as "
    "equivalent if and only if their `verdict` fields are exactly equal "
    "(VALID, INVALID, or INCONCLUSIVE). Ignore severity labels, numeric "
    "confidence scores, and any difference in reasoning phrasing — two "
    "assessments that agree on the verdict but word the explanation "
    "differently are equivalent."
)


def _clamp(value: int, low: int, high: int) -> int:
    if value < low:
        return low
    if value > high:
        return high
    return value


def _now_epoch() -> int:
    """Seconds since epoch, taken from the transaction's own deterministic
    clock. Every validator re-executing this transaction sees the identical
    value. 0 means "clock unavailable"; every caller refuses a time-based
    decision on 0 rather than reading a parse failure as 1970."""
    try:
        return int(datetime.now(timezone.utc).timestamp())
    except Exception:
        return 0


def _clean_json(text: str):
    """LLMs sometimes wrap JSON in markdown fences or add stray prose even
    when JSON is requested. Extract the outermost {...} span and parse just
    that."""
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1 or last < first:
        return None
    try:
        return json.loads(text[first:last + 1])
    except Exception:
        return None


def _as_confidence(x) -> int:
    """Tolerant numeric coercion for the LLM's confidence field: accepts
    ints, int-strings and float-like values; anything else maps to -1 so the
    coherence check (not a parse crash) rejects it."""
    try:
        return int(x)
    except (TypeError, ValueError):
        try:
            return int(float(str(x)))
        except (TypeError, ValueError):
            return -1


def _coherent(report) -> bool:
    """Self-consistency check on one adjudication report, run against the
    leader's own output. A pure function of calldata, so every validator
    computes the identical answer and it can reject a malformed report
    without ever being itself a source of disagreement."""
    if not isinstance(report, dict):
        return False
    if str(report.get("verdict", "")) not in (VERDICT_VALID, VERDICT_INVALID, VERDICT_INCONCLUSIVE):
        return False
    if str(report.get("severity", "")) not in SEVERITIES:
        return False
    try:
        conf = int(report.get("confidence", -1))
    except Exception:
        return False
    if conf < 0 or conf > 100:
        return False
    if len(str(report.get("reasoning", ""))) > MAX_REASONING_CHARS:
        return False
    return True


def _judge(scope: str, severity_policy: str, report_url: str, note: str) -> dict:
    """Fetch the report and ask an LLM whether it proves an in-scope
    vulnerability. Returns a small self-describing dict ONLY on a genuine
    business outcome — the page loaded and either does or does not satisfy
    the scope, or loaded with nothing readable on it. Anything that looks
    like infrastructure trouble is raised as a tagged gl.vm.UserError so the
    caller can tell a network hiccup apart from an actual verdict."""
    try:
        rendered = gl.nondet.web.render(report_url, mode="text", wait_after_loaded=RENDER_WAIT_AFTER_LOADED)
    except Exception:
        raise gl.vm.UserError(ERR_TRANSIENT_FETCH + " could not render the report URL")
    body = rendered[:MAX_RENDER_CHARS].strip()
    if len(body) == 0:
        # A fact about the report, not about the network — safe to return.
        return {"verdict": VERDICT_INCONCLUSIVE, "severity": "NONE", "confidence": 0,
                "reasoning": "page loaded but contained no readable content"}
    prompt = (
        "You are a strict security adjudicator on a bug-bounty platform.\n"
        "Decide whether the report below demonstrates a genuine, reproducible\n"
        "security vulnerability inside the engagement scope.\n\n"
        "=== ENGAGEMENT SCOPE ===\n" + scope + "\n\n"
        "=== REQUIRED SEVERITY POLICY ===\n" + severity_policy + "\n\n"
        "=== RESEARCHER NOTE ===\n" + (note if len(note) > 0 else "(none)") + "\n\n"
        "=== REPORT CONTENT (truncated) ===\n" + body + "\n\n"
        "Rules:\n"
        "- VALID only if the report identifies a concrete vulnerability that is\n"
        "  in scope AND plausibly meets the severity policy.\n"
        "- INVALID if it is out of scope, not a vulnerability, pure speculation,\n"
        "  a duplicate of public knowledge with no novel PoC, or unreadable noise\n"
        "  that still fails to show any vulnerability.\n"
        "- INCONCLUSIVE only when content is readable but genuinely insufficient\n"
        "  to decide either way.\n"
        "Answer with EXACTLY one JSON object, no other text:\n"
        '{"verdict":"VALID|INVALID|INCONCLUSIVE","severity":"NONE|LOW|MEDIUM|HIGH|CRITICAL",'
        '"confidence":0-100,"reasoning":"<=' + str(MAX_REASONING_CHARS) + ' chars"}'
    )
    try:
        raw = gl.nondet.exec_prompt(prompt)
    except Exception:
        raise gl.vm.UserError(ERR_TRANSIENT_LLM + " adjudication call failed")
    parsed = _clean_json(str(raw))
    if parsed is None:
        raise gl.vm.UserError(ERR_LLM_MALFORMED + " adjudicator output was not JSON")
    out = {
        "verdict": str(parsed.get("verdict", "")),
        "severity": str(parsed.get("severity", "")),
        "confidence": _as_confidence(parsed.get("confidence", "")),
        "reasoning": str(parsed.get("reasoning", ""))[:MAX_REASONING_CHARS + 1],
    }
    if not _coherent(out):
        raise gl.vm.UserError(ERR_LLM_MALFORMED + " adjudicator output failed coherence checks")
    return out


def _adjudicate(scope: str, severity_policy: str, report_url: str, note: str) -> dict:
    """Consensus wrapper: the leader fetches and judges; every validator
    independently re-fetches and re-prompts. Only the `verdict` field is
    compared (see _EQ_PRINCIPLE). Returns the leader's report dict. Whatever
    the leader raises propagates to the caller, which classifies it via the
    ERR_* tags."""
    def leader_fn():
        return _judge(scope, severity_policy, report_url, note)
    return gl.eq_principle.prompt_comparative(leader_fn, _EQ_PRINCIPLE)


@allow_storage
@dataclass
class Claim:
    claim_id: u32
    bounty_id: u32
    researcher: Address
    report_url: str
    note: str
    status: str              # CLAIM_* — reputation is computed, never stored
    submitted_epoch: u64
    ai_verdict: str          # last recorded verdict (evidence only)
    ai_severity: str
    ai_confidence: i32
    ai_reasoning: str
    reject_reason: str
    rejected_epoch: u64      # when the sponsor rejected — challenge window runs from here
    challenge_mode: bool     # True = disputing a sponsor rejection; False = silent-sponsor escalation


@allow_storage
@dataclass
class Bounty:
    bounty_id: u32
    sponsor: Address
    title: str
    scope: str
    severity_policy: str
    reward_amount: u256      # net of the platform fee — exactly what a winner receives
    fee_withheld: u256
    status: str              # BOUNTY_*
    created_epoch: u64
    settled_epoch: u64
    winning_claim_id: u32    # 0 while undecided
    claim_count: u32         # for O(1) checks and stats — no scans needed


class BugVault(gl.Contract):
    owner: Address
    treasury: Address
    platform_fee_bps: u32
    paused: bool

    next_bounty_id: u32
    next_claim_id: u32
    bounties: TreeMap[u32, Bounty]
    claims: TreeMap[u32, Claim]
    bounty_claims: TreeMap[u32, DynArray[u32]]
    sponsor_bounties: TreeMap[Address, DynArray[u32]]
    researcher_claims: TreeMap[Address, DynArray[u32]]

    wins: TreeMap[Address, u32]        # claims that ended APPROVED or RESOLVED_VALID
    attempts: TreeMap[Address, u32]    # every claim ever submitted
    earned: TreeMap[Address, u256]     # lifetime GEN paid out per researcher

    total_bounties: u32
    total_claims: u32
    total_paid_out: u256
    total_escrow_locked: u256   # net rewards currently sitting in open bounties
    total_pending_fees: u256
    last_outbound_epoch: u64

    def __init__(self, platform_fee_bps: int):
        self.owner = gl.message.sender_address
        self.treasury = gl.message.sender_address
        fee = int(platform_fee_bps)
        if fee < 0 or fee > MAX_FEE_BPS:
            raise gl.vm.UserError("platform_fee_bps out of range 0.." + str(MAX_FEE_BPS))
        self.platform_fee_bps = u32(fee)
        self.paused = False
        self.next_bounty_id = u32(0)
        self.next_claim_id = u32(0)
        self.total_bounties = u32(0)
        self.total_claims = u32(0)
        self.total_paid_out = u256(0)
        self.total_escrow_locked = u256(0)
        self.total_pending_fees = u256(0)
        self.last_outbound_epoch = u64(0)

    # ── internals ────────────────────────────────────────────────────────

    def _is_owner(self) -> bool:
        return str(gl.message.sender_address) == str(self.owner)

    def _require_owner(self) -> None:
        if not self._is_owner():
            raise gl.vm.UserError("owner only")

    def _send(self, to: Address, amount: int) -> None:
        if amount <= 0:
            return
        gl.transfer(to, amount)
        self.last_outbound_epoch = u64(_now_epoch())

    def _wins_of(self, who: str) -> int:
        found = self.wins.get(Address(str(who)))
        return int(found) if found is not None else 0

    def _attempts_of(self, who: str) -> int:
        found = self.attempts.get(Address(str(who)))
        return int(found) if found is not None else 0

    def _claim_view(self, c: Claim) -> dict:
        return {
            "claim_id": int(c.claim_id),
            "bounty_id": int(c.bounty_id),
            "researcher": str(c.researcher),
            "report_url": str(c.report_url),
            "note": str(c.note),
            "status": str(c.status),
            "submitted_epoch": int(c.submitted_epoch),
            "ai_verdict": str(c.ai_verdict),
            "ai_severity": str(c.ai_severity),
            "ai_confidence": int(c.ai_confidence),
            "ai_reasoning": str(c.ai_reasoning),
            "reject_reason": str(c.reject_reason),
            "rejected_epoch": int(c.rejected_epoch),
            "challenge_deadline_epoch": (int(c.rejected_epoch) + CHALLENGE_WINDOW_SECONDS)
                if int(c.rejected_epoch) > 0 else 0,
            "challengeable": self._challengeable(c, _now_epoch()),
        }

    def _bounty_view(self, b: Bounty) -> dict:
        return {
            "bounty_id": int(b.bounty_id),
            "sponsor": str(b.sponsor),
            "title": str(b.title),
            "scope": str(b.scope),
            "severity_policy": str(b.severity_policy),
            "reward_amount": int(b.reward_amount),
            "fee_withheld": int(b.fee_withheld),
            "status": str(b.status),
            "created_epoch": int(b.created_epoch),
            "settled_epoch": int(b.settled_epoch),
            "winning_claim_id": int(b.winning_claim_id),
            "claim_count": int(b.claim_count),
        }

    def _page_ids(self, ids: DynArray, cursor: int, limit: int) -> dict:
        """Newest-first pagination with a hard scan bound."""
        lim = _clamp(int(limit), 1, MAX_PAGE)
        rows = []
        scanned = 0
        i = len(ids) - 1 - _clamp(int(cursor), 0, MAX_SCAN)
        while i >= 0 and scanned < MAX_SCAN and len(rows) < lim:
            rows.append(int(ids[i]))
            scanned += 1
            i -= 1
        return {"ids": rows, "next_cursor": (int(cursor) + scanned) if i >= 0 and scanned > 0 else -1}

    def _challengeable(self, c: Claim, now: int) -> bool:
        """A rejected claim stays disputable for CHALLENGE_WINDOW_SECONDS.
        While this is true the escrow MUST NOT leave the bounty — it backs a
        dispute the researcher still has a right to open."""
        return (str(c.status) == CLAIM_REJECTED and now > 0
                and int(c.rejected_epoch) > 0
                and now - int(c.rejected_epoch) < CHALLENGE_WINDOW_SECONDS)

    def _refund_blocker(self, bounty_id: int, now: int) -> str:
        """Empty string if the escrow may leave, otherwise the reason it must
        stay. Blocks on pending reviews AND on rejections that are still
        inside their challenge window."""
        bucket = self.bounty_claims.get(u32(bounty_id))
        if bucket is None:
            return ""
        for cid in bucket:
            other = self.claims.get(cid)
            if other is None:
                continue
            if str(other.status) == CLAIM_SUBMITTED:
                return "a claim still awaits review — approve or reject it first"
            if self._challengeable(other, now):
                return ("a rejected claim is still challengeable until epoch "
                        + str(int(other.rejected_epoch) + CHALLENGE_WINDOW_SECONDS))
        return ""

    def _record_win(self, researcher: Address, amount: int) -> None:
        self.wins[researcher] = u32(self._wins_of(str(researcher)) + 1)
        prev = self.earned.get(researcher)
        self.earned[researcher] = u256((int(prev) if prev is not None else 0) + amount)

    def _void_siblings(self, b: Bounty, winner_claim_id: int) -> None:
        bucket = self.bounty_claims.get(u32(int(b.bounty_id)))
        if bucket is None:
            return
        for cid in bucket:
            other = self.claims.get(cid)
            if other is not None and int(other.claim_id) != winner_claim_id and str(other.status) == CLAIM_SUBMITTED:
                other.status = CLAIM_VOIDED

    def _settle_winner(self, b: Bounty, c: Claim, mode: str) -> None:
        b.status = BOUNTY_PAID_OUT
        b.settled_epoch = u64(_now_epoch())
        b.winning_claim_id = u32(int(c.claim_id))
        self._void_siblings(b, int(c.claim_id))
        self._record_win(c.researcher, int(b.reward_amount))
        self.total_paid_out = u256(int(self.total_paid_out) + int(b.reward_amount))
        self.total_escrow_locked = u256(int(self.total_escrow_locked) - int(b.reward_amount))
        self._send(c.researcher, int(b.reward_amount))

    # ── payable writes (must never raise — refund + {"ok": false} instead) ──

    @gl.public.write.payable
    def create_bounty(self, title: str, scope: str, severity_policy: str) -> str:
        """Lock a reward for a scoped engagement. The fee is split once, up
        front; `reward_amount` is exactly what a winning researcher receives.
        The ONLY method `set_paused` blocks — money already inside must
        always keep moving."""
        value = int(gl.message.value)
        sender = gl.message.sender_address
        if self.paused:
            self._send(sender, value)
            return json.dumps({"ok": False, "reason": "contract is paused", "refunded": value})
        if value <= 0:
            return json.dumps({"ok": False, "reason": "no value attached", "refunded": 0})
        if len(title) == 0 or len(title) > MAX_TITLE_LEN:
            self._send(sender, value)
            return json.dumps({"ok": False, "reason": "title must be 1.." + str(MAX_TITLE_LEN) + " chars", "refunded": value})
        if len(scope) < 20 or len(scope) > MAX_SCOPE_LEN:
            self._send(sender, value)
            return json.dumps({"ok": False, "reason": "scope must be 20.." + str(MAX_SCOPE_LEN) + " chars", "refunded": value})
        if len(severity_policy) > MAX_POLICY_LEN:
            self._send(sender, value)
            return json.dumps({"ok": False, "reason": "severity_policy too long", "refunded": value})
        if value > MAX_REWARD:
            self._send(sender, value)
            return json.dumps({"ok": False, "reason": "escrow exceeds MAX_REWARD", "refunded": value})
        fee = (value * int(self.platform_fee_bps)) // BPS_DENOM
        reward = value - fee
        if reward < MIN_REWARD:
            self._send(sender, value)
            return json.dumps({"ok": False, "reason": "net reward below MIN_REWARD", "refunded": value})
        bid = int(self.next_bounty_id) + 1
        self.next_bounty_id = u32(bid)
        rec = self.bounties.get_or_insert_default(u32(bid))
        rec.bounty_id = u32(bid)
        rec.sponsor = sender
        rec.title = title
        rec.scope = scope
        rec.severity_policy = severity_policy
        rec.reward_amount = u256(reward)
        rec.fee_withheld = u256(fee)
        rec.status = BOUNTY_OPEN
        rec.created_epoch = u64(_now_epoch())
        rec.settled_epoch = u64(0)
        rec.winning_claim_id = u32(0)
        rec.claim_count = u32(0)
        self.sponsor_bounties.get_or_insert_default(sender).append(u32(bid))
        self.total_bounties = u32(int(self.total_bounties) + 1)
        self.total_escrow_locked = u256(int(self.total_escrow_locked) + reward)
        self.total_pending_fees = u256(int(self.total_pending_fees) + fee)
        return json.dumps({"ok": True, "bounty_id": bid, "reward_amount": reward, "fee_withheld": fee})

    @gl.public.write.payable
    def dispute_claim(self, claim_id: int) -> str:
        """Open on-chain arbitration over a claim. Exactly DISPUTE_BOND must
        be attached. Two entry modes: CHALLENGE (the sponsor rejected, the
        researcher disagrees — bond forfeited to the sponsor if the AI also
        says INVALID; must be invoked inside CHALLENGE_WINDOW_SECONDS of the
        rejection, after which the rejection is final) and TIMEOUT (the
        sponsor went silent for REVIEW_TIMEOUT_SECONDS — the bond is always
        returned, win or lose)."""
        value = int(gl.message.value)
        sender = gl.message.sender_address
        c = self.claims.get(u32(int(claim_id)))
        if c is None:
            self._send(sender, value)
            return json.dumps({"ok": False, "reason": "unknown claim", "refunded": value})
        if str(c.researcher) != str(sender):
            self._send(sender, value)
            return json.dumps({"ok": False, "reason": "only the claim's researcher can dispute", "refunded": value})
        if value != DISPUTE_BOND:
            self._send(sender, value)
            return json.dumps({"ok": False, "reason": "exactly DISPUTE_BOND=" + str(DISPUTE_BOND) + " must be attached", "refunded": value})
        b = self.bounties.get(u32(int(c.bounty_id)))
        if b is None or str(b.status) != BOUNTY_OPEN:
            self._send(sender, value)
            return json.dumps({"ok": False, "reason": "bounty is no longer open", "refunded": value})
        now = _now_epoch()
        if now == 0:
            self._send(sender, value)
            return json.dumps({"ok": False, "reason": "clock unavailable", "refunded": value})
        if str(c.status) == CLAIM_REJECTED:
            if now - int(c.rejected_epoch) >= CHALLENGE_WINDOW_SECONDS:
                self._send(sender, value)
                return json.dumps({"ok": False, "reason": "challenge window has closed — the rejection is final", "refunded": value})
            challenge_mode = True
        elif str(c.status) == CLAIM_SUBMITTED and now - int(c.submitted_epoch) >= REVIEW_TIMEOUT_SECONDS:
            challenge_mode = False
        else:
            self._send(sender, value)
            return json.dumps({"ok": False, "reason": "claim is not disputable yet (sponsor still within review window)", "refunded": value})

        c.challenge_mode = challenge_mode
        try:
            report = _adjudicate(str(b.scope), str(b.severity_policy), str(c.report_url), str(c.note))
        except gl.vm.UserError as e:
            # Infrastructure trouble is NOT a verdict: return the bond and
            # leave status and ai_* exactly as they were. Retryable later.
            self._send(sender, value)
            return json.dumps({"ok": True, "verdict": "RETRY_LATER", "detail": str(e)[:120], "bond_returned": value})
        except Exception as e:
            self._send(sender, value)
            return json.dumps({"ok": False, "verdict": "RETRY_LATER", "detail": str(e)[:120], "refunded": value})

        verdict = str(report.get("verdict", ""))
        c.ai_verdict = verdict
        c.ai_severity = str(report.get("severity", "NONE"))
        c.ai_confidence = i32(int(report.get("confidence", 0)))
        c.ai_reasoning = str(report.get("reasoning", ""))[:MAX_REASONING_CHARS]

        if verdict == VERDICT_VALID:
            c.status = CLAIM_RESOLVED_VALID
            self._settle_winner(b, c, "ARBITRATION")
            self._send(sender, value)  # bond back, always
            return json.dumps({"ok": True, "verdict": VERDICT_VALID, "bounty_status": BOUNTY_PAID_OUT,
                               "reward_paid": int(b.reward_amount), "bond_returned": value})
        if verdict == VERDICT_INVALID:
            c.status = CLAIM_RESOLVED_INVALID
            if challenge_mode:
                # Failed challenge of a decision the sponsor already made —
                # the bond compensates the sponsor for the wasted review.
                bond_note = "bond_forfeited_to_sponsor"
                self._send(b.sponsor, value)
            else:
                bond_note = "bond_returned"
                self._send(sender, value)
            return json.dumps({"ok": True, "verdict": VERDICT_INVALID, "bond": bond_note})
        # INCONCLUSIVE: nothing becomes permanent. The claim stays open and
        # can be re-arbitrated later; the bond always comes back.
        self._send(sender, value)
        return json.dumps({"ok": True, "verdict": VERDICT_INCONCLUSIVE, "claim_status": CLAIM_SUBMITTED, "bond_returned": value})

    # ── plain writes (hold no incoming funds — reverting is safe) ────────

    @gl.public.write
    def submit_claim(self, bounty_id: int, report_url: str, note: str) -> str:
        sender = gl.message.sender_address
        b = self.bounties.get(u32(int(bounty_id)))
        if b is None:
            raise gl.vm.UserError("unknown bounty")
        if str(b.status) != BOUNTY_OPEN:
            raise gl.vm.UserError("bounty is not open")
        if str(b.sponsor) == str(sender):
            raise gl.vm.UserError("the sponsor cannot claim their own bounty")
        if len(report_url) < 10 or len(report_url) > MAX_URL_LEN:
            raise gl.vm.UserError("report_url must be 10.." + str(MAX_URL_LEN) + " chars")
        if len(note) > MAX_NOTE_LEN:
            raise gl.vm.UserError("note too long")
        if int(b.claim_count) >= MAX_CLAIMS_PER_BOUNTY:
            raise gl.vm.UserError("claim cap reached for this bounty")
        # One live claim per researcher per bounty: without this, a single
        # griefer could flood all 8 slots and lock out every other hunter.
        # A REJECTED claim never blocks a fresh attempt.
        existing = self.bounty_claims.get(u32(int(bounty_id)))
        if existing is not None:
            for cid in existing:
                other = self.claims.get(cid)
                if (other is not None and str(other.researcher) == str(sender)
                        and str(other.status) == CLAIM_SUBMITTED):
                    raise gl.vm.UserError("you already have a live claim on this bounty — wait for the review or dispute it")
        cid = int(self.next_claim_id) + 1
        self.next_claim_id = u32(cid)
        rec = self.claims.get_or_insert_default(u32(cid))
        rec.claim_id = u32(cid)
        rec.bounty_id = u32(int(bounty_id))
        rec.researcher = sender
        rec.report_url = report_url
        rec.note = note
        rec.status = CLAIM_SUBMITTED
        rec.submitted_epoch = u64(_now_epoch())
        rec.ai_verdict = ""
        rec.ai_severity = ""
        rec.ai_confidence = i32(0)
        rec.ai_reasoning = ""
        rec.reject_reason = ""
        rec.rejected_epoch = u64(0)
        rec.challenge_mode = False
        self.bounty_claims.get_or_insert_default(u32(int(bounty_id))).append(u32(cid))
        self.researcher_claims.get_or_insert_default(sender).append(u32(cid))
        b.claim_count = u32(int(b.claim_count) + 1)
        self.total_claims = u32(int(self.total_claims) + 1)
        self.attempts[sender] = u32(self._attempts_of(str(sender)) + 1)
        return json.dumps({"ok": True, "claim_id": cid})

    @gl.public.write
    def approve_claim(self, claim_id: int) -> str:
        """Fast path, no AI: the sponsor accepts the report, the researcher
        is paid, every other pending claim is voided."""
        sender = gl.message.sender_address
        c = self.claims.get(u32(int(claim_id)))
        if c is None:
            raise gl.vm.UserError("unknown claim")
        b = self.bounties.get(u32(int(c.bounty_id)))
        if b is None or str(b.status) != BOUNTY_OPEN:
            raise gl.vm.UserError("bounty is not open")
        if str(b.sponsor) != str(sender):
            raise gl.vm.UserError("sponsor only")
        if str(c.status) != CLAIM_SUBMITTED:
            raise gl.vm.UserError("claim is not pending review")
        c.status = CLAIM_APPROVED
        self._settle_winner(b, c, "DIRECT")
        return json.dumps({"ok": True, "bounty_status": BOUNTY_PAID_OUT, "reward_paid": int(b.reward_amount)})

    @gl.public.write
    def reject_claim(self, claim_id: int, reason: str) -> str:
        sender = gl.message.sender_address
        c = self.claims.get(u32(int(claim_id)))
        if c is None:
            raise gl.vm.UserError("unknown claim")
        b = self.bounties.get(u32(int(c.bounty_id)))
        if b is None or str(b.status) != BOUNTY_OPEN:
            raise gl.vm.UserError("bounty is not open")
        if str(b.sponsor) != str(sender):
            raise gl.vm.UserError("sponsor only")
        if str(c.status) != CLAIM_SUBMITTED:
            raise gl.vm.UserError("claim is not pending review")
        if len(reason) > MAX_REASON_LEN:
            raise gl.vm.UserError("reason too long")
        now = _now_epoch()
        if now == 0:
            raise gl.vm.UserError("clock unavailable")
        c.status = CLAIM_REJECTED
        c.reject_reason = reason
        c.rejected_epoch = u64(now)
        return json.dumps({"ok": True, "claim_status": CLAIM_REJECTED,
                           "challenge_deadline_epoch": now + CHALLENGE_WINDOW_SECONDS,
                           "note": "researcher may dispute with a " + str(DISPUTE_BOND)
                                   + " wei bond until the challenge window closes"})

    @gl.public.write
    def cancel_bounty(self, bounty_id: int) -> str:
        """Sponsor exits and gets the full net reward back — but NEVER while
        a claim is still waiting for review, and NEVER while a rejection is
        still inside its challenge window. A sponsor who could drain the
        escrow ahead of a pending dispute would hold the same leverage as one
        who could deny a payout directly, just through a slower route."""
        sender = gl.message.sender_address
        b = self.bounties.get(u32(int(bounty_id)))
        if b is None:
            raise gl.vm.UserError("unknown bounty")
        if str(b.sponsor) != str(sender):
            raise gl.vm.UserError("sponsor only")
        if str(b.status) != BOUNTY_OPEN:
            raise gl.vm.UserError("bounty already settled")
        now = _now_epoch()
        if now == 0:
            raise gl.vm.UserError("clock unavailable")
        blocker = self._refund_blocker(int(bounty_id), now)
        if blocker:
            raise gl.vm.UserError("cannot cancel: " + blocker)
        b.status = BOUNTY_CANCELED
        b.settled_epoch = u64(_now_epoch())
        self.total_escrow_locked = u256(int(self.total_escrow_locked) - int(b.reward_amount))
        self._send(b.sponsor, int(b.reward_amount))
        return json.dumps({"ok": True, "bounty_status": BOUNTY_CANCELED, "reward_refunded": int(b.reward_amount)})

    @gl.public.write
    def reclaim_expired_bounty(self, bounty_id: int) -> str:
        """Permissionless: after BOUNTY_TTL_SECONDS with no pending claims,
        anyone may return the escrow to the sponsor."""
        b = self.bounties.get(u32(int(bounty_id)))
        if b is None:
            raise gl.vm.UserError("unknown bounty")
        if str(b.status) != BOUNTY_OPEN:
            raise gl.vm.UserError("bounty already settled")
        now = _now_epoch()
        if now == 0:
            raise gl.vm.UserError("clock unavailable")
        if now - int(b.created_epoch) < BOUNTY_TTL_SECONDS:
            raise gl.vm.UserError("bounty is not expired yet")
        blocker = self._refund_blocker(int(bounty_id), now)
        if blocker:
            raise gl.vm.UserError("cannot reclaim: " + blocker)
        b.status = BOUNTY_RECLAIMED
        b.settled_epoch = u64(now)
        self.total_escrow_locked = u256(int(self.total_escrow_locked) - int(b.reward_amount))
        self._send(b.sponsor, int(b.reward_amount))
        return json.dumps({"ok": True, "bounty_status": BOUNTY_RECLAIMED, "reward_refunded": int(b.reward_amount)})

    @gl.public.write
    def preview_arbitration(self, claim_id: int) -> str:
        """Judge the report exactly as dispute_claim would, without bonding,
        mutating, or settling anything. A write because nondeterministic
        work must run under consensus; a no-op for state."""
        sender = gl.message.sender_address
        c = self.claims.get(u32(int(claim_id)))
        if c is None:
            raise gl.vm.UserError("unknown claim")
        b = self.bounties.get(u32(int(c.bounty_id)))
        if b is None:
            raise gl.vm.UserError("unknown bounty")
        if str(sender) != str(b.sponsor) and str(sender) != str(c.researcher):
            raise gl.vm.UserError("sponsor or researcher only")
        try:
            report = _adjudicate(str(b.scope), str(b.severity_policy), str(c.report_url), str(c.note))
        except gl.vm.UserError as e:
            return json.dumps({"ok": True, "verdict": "RETRY_LATER", "detail": str(e)[:120]})
        except Exception as e:
            return json.dumps({"ok": True, "verdict": "RETRY_LATER", "detail": str(e)[:120]})
        return json.dumps({"ok": True, "verdict": str(report.get("verdict", "")),
                           "severity": str(report.get("severity", "")),
                           "confidence": int(report.get("confidence", 0)),
                           "reasoning": str(report.get("reasoning", ""))})

    @gl.public.write
    def set_paused(self, paused: bool) -> str:
        """Gates NEW escrow entering only. Every settlement path — submit,
        approve, reject, arbitrate, cancel, reclaim — keeps working while
        paused. Freezing settlement would be the same leverage as freezing
        payouts, via a slower route."""
        self._require_owner()
        self.paused = bool(paused)
        return json.dumps({"ok": True, "paused": self.paused})

    @gl.public.write
    def set_platform_fee_bps(self, new_fee_bps: int) -> str:
        self._require_owner()
        fee = int(new_fee_bps)
        if fee < 0 or fee > MAX_FEE_BPS:
            raise gl.vm.UserError("fee out of range 0.." + str(MAX_FEE_BPS))
        self.platform_fee_bps = u32(fee)
        return json.dumps({"ok": True, "platform_fee_bps": fee})

    @gl.public.write
    def set_treasury(self, new_treasury: str) -> str:
        self._require_owner()
        self.treasury = Address(str(new_treasury))
        return json.dumps({"ok": True, "treasury": str(new_treasury)})

    @gl.public.write
    def sweep_fees(self) -> str:
        """Move accumulated fee revenue to the treasury. Delay-gated behind
        the last outbound transfer so a sweep can never graze funds that are
        committed but not yet finalized on-chain."""
        self._require_owner()
        now = _now_epoch()
        if now == 0:
            raise gl.vm.UserError("clock unavailable")
        if now - int(self.last_outbound_epoch) < SWEEP_DELAY_SECONDS:
            raise gl.vm.UserError("sweep cooldown — try later")
        amt = int(self.total_pending_fees)
        if amt <= 0:
            return json.dumps({"ok": True, "swept": 0})
        self.total_pending_fees = u256(0)
        self._send(self.treasury, amt)
        return json.dumps({"ok": True, "swept": amt})

    @gl.public.write
    def transfer_ownership(self, new_owner: str) -> str:
        self._require_owner()
        self.owner = Address(str(new_owner))
        return json.dumps({"ok": True, "owner": str(new_owner)})

    # ── views ────────────────────────────────────────────────────────────

    @gl.public.view
    def get_config(self) -> str:
        return json.dumps({
            "owner": str(self.owner),
            "treasury": str(self.treasury),
            "platform_fee_bps": int(self.platform_fee_bps),
            "max_fee_bps": MAX_FEE_BPS,
            "paused": bool(self.paused),
            "dispute_bond": DISPUTE_BOND,
            "min_reward": MIN_REWARD,
            "max_reward": MAX_REWARD,
            "max_claims_per_bounty": MAX_CLAIMS_PER_BOUNTY,
            "review_timeout_seconds": REVIEW_TIMEOUT_SECONDS,
            "challenge_window_seconds": CHALLENGE_WINDOW_SECONDS,
            "bounty_ttl_seconds": BOUNTY_TTL_SECONDS,
        })

    @gl.public.view
    def get_bounty(self, bounty_id: int) -> str:
        b = self.bounties.get(u32(int(bounty_id)))
        if b is None:
            return json.dumps({"ok": False, "reason": "unknown bounty"})
        out = self._bounty_view(b)
        out["ok"] = True
        return json.dumps(out)

    @gl.public.view
    def get_claim(self, claim_id: int) -> str:
        c = self.claims.get(u32(int(claim_id)))
        if c is None:
            return json.dumps({"ok": False, "reason": "unknown claim"})
        out = self._claim_view(c)
        out["ok"] = True
        return json.dumps(out)

    @gl.public.view
    def get_claims_by_bounty(self, bounty_id: int, cursor: int, limit: int) -> str:
        bucket = self.bounty_claims.get(u32(int(bounty_id)))
        if bucket is None:
            return json.dumps({"ok": True, "claims": [], "next_cursor": -1})
        page = self._page_ids(bucket, int(cursor), int(limit))
        rows = []
        for cid in page["ids"]:
            c = self.claims.get(u32(cid))
            if c is not None:
                rows.append(self._claim_view(c))
        return json.dumps({"ok": True, "claims": rows, "next_cursor": page["next_cursor"]})

    @gl.public.view
    def get_bounties_by_sponsor(self, sponsor: str, cursor: int, limit: int) -> str:
        bucket = self.sponsor_bounties.get(Address(str(sponsor)))
        if bucket is None:
            return json.dumps({"ok": True, "bounties": [], "next_cursor": -1})
        page = self._page_ids(bucket, int(cursor), int(limit))
        rows = []
        for bid in page["ids"]:
            b = self.bounties.get(u32(bid))
            if b is not None:
                rows.append(self._bounty_view(b))
        return json.dumps({"ok": True, "bounties": rows, "next_cursor": page["next_cursor"]})

    @gl.public.view
    def get_claims_by_researcher(self, researcher: str, cursor: int, limit: int) -> str:
        bucket = self.researcher_claims.get(Address(str(researcher)))
        if bucket is None:
            return json.dumps({"ok": True, "claims": [], "next_cursor": -1})
        page = self._page_ids(bucket, int(cursor), int(limit))
        rows = []
        for cid in page["ids"]:
            c = self.claims.get(u32(cid))
            if c is not None:
                rows.append(self._claim_view(c))
        return json.dumps({"ok": True, "claims": rows, "next_cursor": page["next_cursor"]})

    @gl.public.view
    def get_reputation(self, researcher: str) -> str:
        """Computed, never stored: reputation is whatever the claim history
        says right now, so there is nothing to expire, revoke, or sync."""
        wins = self._wins_of(researcher)
        attempts = self._attempts_of(researcher)
        got = self.earned.get(Address(str(researcher)))
        rate_bps = (wins * BPS_DENOM) // attempts if attempts > 0 else 0
        return json.dumps({
            "ok": True,
            "researcher": str(researcher),
            "valid_wins": wins,
            "attempts": attempts,
            "win_rate_bps": rate_bps,
            "total_earned": int(got) if got is not None else 0,
        })

    @gl.public.view
    def has_credibility(self, researcher: str, min_valid_wins: int) -> bool:
        """Tiny, stable surface designed for other contracts to compose on."""
        return self._wins_of(researcher) >= int(min_valid_wins)

    @gl.public.view
    def list_bounties(self, cursor: int, limit: int, status_filter: str) -> str:
        """Global newest-first feed over ALL bounties, optionally filtered by
        status (\"\" = everything). Walks ids backwards with a hard scan
        bound; next_cursor of -1 means the walk reached bounty 1."""
        lim = _clamp(int(limit), 1, MAX_PAGE)
        filt = str(status_filter)
        rows = []
        scanned = 0
        i = int(self.next_bounty_id) - _clamp(int(cursor), 0, MAX_SCAN)
        while i >= 1 and scanned < MAX_SCAN and len(rows) < lim:
            b = self.bounties.get(u32(i))
            scanned += 1
            if b is not None and (filt == "" or str(b.status) == filt):
                rows.append(self._bounty_view(b))
            i -= 1
        nxt = (int(cursor) + scanned) if i >= 1 and len(rows) == lim else -1
        return json.dumps({"ok": True, "bounties": rows, "next_cursor": nxt})

    @gl.public.view
    def get_platform_stats(self) -> str:
        return json.dumps({
            "ok": True,
            "total_bounties": int(self.total_bounties),
            "total_claims": int(self.total_claims),
            "total_paid_out": int(self.total_paid_out),
            "total_escrow_locked": int(self.total_escrow_locked),
            "total_pending_fees": int(self.total_pending_fees),
        })
