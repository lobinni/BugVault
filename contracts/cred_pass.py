# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
from datetime import datetime, timezone
import json

# ============================================================================
# CredPass — private bug-bounty programs gated on live on-chain credibility
# ============================================================================
#
# A consumer contract demonstrating GenLayer composability: BugVault is the
# oracle of record for "how many VALID findings has this address earned",
# and CredPass is a separate contract that decides whether that is enough to
# enter a private program — without BugVault needing to know CredPass
# exists. A third platform could read the same oracle and set a different
# bar entirely.
#
# CredPass stores NO eligibility decision: every check re-reads BugVault's
# `has_credibility` view live, at call time, via a synchronous IC-to-IC
# view() call. Nothing is cached, so nothing can go stale — admission keeps
# itself honest with no revoke/lapse machinery. What IS stored is the
# historical enrollment record: raising a program's bar never kicks out
# someone already admitted, it only changes what future enrollments require.
#
# This contract holds no funds, so its write methods revert freely — a
# silent `false` that a caller forgets to check is worse than a stopped
# transaction when the thing being granted is admission, not money.

TIER_APPRENTICE = "APPRENTICE"
TIER_PROVEN = "PROVEN"
TIER_TRUSTED = "TRUSTED"
TIER_ELITE = "ELITE"
TIERS = [TIER_APPRENTICE, TIER_PROVEN, TIER_TRUSTED, TIER_ELITE]

DEFAULT_THRESHOLDS = {TIER_APPRENTICE: 0, TIER_PROVEN: 1, TIER_TRUSTED: 5, TIER_ELITE: 15}

MAX_TITLE_LEN = 80
MAX_ENROLLEES_PER_PROGRAM = 512
MAX_SCAN = 200
MAX_PAGE = 40


def _now_epoch() -> int:
    try:
        return int(datetime.now(timezone.utc).timestamp())
    except Exception:
        return 0


@gl.contract_interface
class _BugVaultOracle:
    """Typed stub for the oracle. Purely for IDE/type-checking convenience —
    at runtime this behaves identically to gl.get_contract_at()."""
    class View:
        def has_credibility(self, researcher: str, min_valid_wins: int) -> bool: ...
        def get_reputation(self, researcher: str) -> str: ...

    class Write:
        pass


@allow_storage
@dataclass
class Program:
    program_id: u32
    admin: Address           # whoever created it — programs are self-service
    title: str
    tier: str
    min_valid_wins: u32      # per-program snapshot — tier edits never move it silently
    active: bool
    created_epoch: u64
    admitted_count: u32


class CredPass(gl.Contract):
    owner: Address
    oracle: Address

    tier_thresholds: TreeMap[str, u32]

    programs: TreeMap[u32, Program]
    next_program_id: u32
    program_enrollees: TreeMap[u32, DynArray[Address]]
    researcher_enrollments: TreeMap[Address, DynArray[u32]]

    count_admitted: u32
    count_denied: u32

    def __init__(self, oracle: str):
        self.owner = gl.message.sender_address
        self.oracle = Address(str(oracle))
        for tier in TIERS:
            self.tier_thresholds[tier] = u32(DEFAULT_THRESHOLDS[tier])
        self.next_program_id = u32(0)
        self.count_admitted = u32(0)
        self.count_denied = u32(0)

    # ── internals ────────────────────────────────────────────────────────

    def _require_owner(self) -> None:
        if str(gl.message.sender_address) != str(self.owner):
            raise gl.vm.UserError("owner only")

    def _oracle(self):
        return _BugVaultOracle(self.oracle)

    def _threshold_for(self, tier: str) -> int:
        found = self.tier_thresholds.get(str(tier))
        if found is None:
            raise gl.vm.UserError("unknown tier: " + str(tier))
        return int(found)

    def _already_enrolled(self, program_id: int, who: Address) -> bool:
        bucket = self.program_enrollees.get(u32(program_id))
        if bucket is None:
            return False
        for addr in bucket:
            if str(addr) == str(who):
                return True
        return False

    def _program_view(self, p: Program) -> dict:
        return {
            "program_id": int(p.program_id),
            "admin": str(p.admin),
            "title": str(p.title),
            "tier": str(p.tier),
            "min_valid_wins": int(p.min_valid_wins),
            "active": bool(p.active),
            "created_epoch": int(p.created_epoch),
            "admitted_count": int(p.admitted_count),
        }

    # ── writes ───────────────────────────────────────────────────────────

    @gl.public.write
    def create_program(self, title: str, tier: str) -> str:
        """Anyone may open a private program; the caller becomes its admin.
        The tier resolves to a numeric bar once, HERE, so later tier edits
        never silently re-gate an existing program."""
        if len(title) == 0 or len(title) > MAX_TITLE_LEN:
            raise gl.vm.UserError("title must be 1.." + str(MAX_TITLE_LEN) + " characters")
        threshold = self._threshold_for(tier)
        pid = int(self.next_program_id) + 1
        self.next_program_id = u32(pid)
        rec = self.programs.get_or_insert_default(u32(pid))
        rec.program_id = u32(pid)
        rec.admin = gl.message.sender_address
        rec.title = title
        rec.tier = tier
        rec.min_valid_wins = u32(threshold)
        rec.active = True
        rec.created_epoch = u64(_now_epoch())
        rec.admitted_count = u32(0)
        return json.dumps({"ok": True, "program_id": pid, "min_valid_wins": threshold})

    @gl.public.write
    def enroll(self, program_id: int) -> str:
        """The caller enrolls THEMSELVES. REVERTS when the live oracle read
        says they do not meet the program's bar — admission is a staffing
        decision, and a silent false someone forgets to check is worse than
        a stopped transaction."""
        p = self.programs.get(u32(int(program_id)))
        if p is None:
            raise gl.vm.UserError("unknown program")
        if not bool(p.active):
            raise gl.vm.UserError("program is closed")
        sender = gl.message.sender_address
        if self._already_enrolled(int(program_id), sender):
            raise gl.vm.UserError("already enrolled")
        # Live read, at call time, every time.
        ok = self._oracle().view().has_credibility(str(sender), int(p.min_valid_wins))
        if not ok:
            self.count_denied = u32(int(self.count_denied) + 1)
            raise gl.vm.UserError(str(sender) + " needs " + str(int(p.min_valid_wins))
                                  + " VALID findings for " + str(p.title) + " — check preview_enrollment first")
        bucket = self.program_enrollees.get_or_insert_default(u32(int(program_id)))
        if len(bucket) >= MAX_ENROLLEES_PER_PROGRAM:
            raise gl.vm.UserError("program is full")
        bucket.append(sender)
        self.researcher_enrollments.get_or_insert_default(sender).append(u32(int(program_id)))
        p.admitted_count = u32(int(p.admitted_count) + 1)
        self.count_admitted = u32(int(self.count_admitted) + 1)
        return json.dumps({"ok": True, "program_id": int(program_id), "researcher": str(sender),
                           "required_valid_wins": int(p.min_valid_wins)})

    @gl.public.write
    def preview_enrollment(self, program_id: int, researcher: str) -> str:
        """Never reverts — degrades and explains instead, so a caller can
        check before committing a transaction to enroll(). Both paths call
        the identical live oracle read, so nobody learns the rule by having
        a transaction reverted on them."""
        p = self.programs.get(u32(int(program_id)))
        if p is None:
            return json.dumps({"ok": False, "reason": "unknown program"})
        if not bool(p.active):
            return json.dumps({"ok": True, "eligible": False, "reason": "program is closed"})
        if self._already_enrolled(int(program_id), Address(str(researcher))):
            return json.dumps({"ok": True, "eligible": False, "reason": "already enrolled"})
        ok = self._oracle().view().has_credibility(str(researcher), int(p.min_valid_wins))
        return json.dumps({"ok": True, "eligible": bool(ok), "researcher": str(researcher),
                           "program_id": int(program_id), "required_valid_wins": int(p.min_valid_wins)})

    @gl.public.write
    def set_program_requirement(self, program_id: int, tier: str) -> str:
        """Admin re-gates FUTURE enrollments. Enrollments already granted are
        a historical record, not a live credential — this never kicks anyone
        out, so no revocation machinery is required."""
        p = self.programs.get(u32(int(program_id)))
        if p is None:
            raise gl.vm.UserError("unknown program")
        if str(p.admin) != str(gl.message.sender_address):
            raise gl.vm.UserError("program admin only")
        threshold = self._threshold_for(tier)
        p.tier = tier
        p.min_valid_wins = u32(threshold)
        return json.dumps({"ok": True, "program_id": int(program_id), "min_valid_wins": threshold})

    @gl.public.write
    def set_program_active(self, program_id: int, active: bool) -> str:
        p = self.programs.get(u32(int(program_id)))
        if p is None:
            raise gl.vm.UserError("unknown program")
        if str(p.admin) != str(gl.message.sender_address):
            raise gl.vm.UserError("program admin only")
        p.active = bool(active)
        return json.dumps({"ok": True, "program_id": int(program_id), "active": bool(active)})

    @gl.public.write
    def set_tier_threshold(self, tier: str, min_valid_wins: int) -> str:
        self._require_owner()
        if tier not in TIERS:
            raise gl.vm.UserError("unknown tier: " + str(tier))
        m = int(min_valid_wins)
        if m < 0:
            raise gl.vm.UserError("min_valid_wins cannot be negative")
        self.tier_thresholds[tier] = u32(m)
        return json.dumps({"ok": True, "tier": tier, "min_valid_wins": m})

    @gl.public.write
    def set_oracle(self, new_oracle: str) -> str:
        self._require_owner()
        self.oracle = Address(str(new_oracle))
        return json.dumps({"ok": True, "oracle": str(new_oracle)})

    @gl.public.write
    def transfer_ownership(self, new_owner: str) -> str:
        self._require_owner()
        self.owner = Address(str(new_owner))
        return json.dumps({"ok": True, "owner": str(new_owner)})

    # ── views ────────────────────────────────────────────────────────────

    @gl.public.view
    def is_eligible(self, program_id: int, researcher: str) -> bool:
        p = self.programs.get(u32(int(program_id)))
        if p is None or not bool(p.active):
            return False
        if self._already_enrolled(int(program_id), Address(str(researcher))):
            return False
        return bool(self._oracle().view().has_credibility(str(researcher), int(p.min_valid_wins)))

    @gl.public.view
    def get_credibility_snapshot(self, researcher: str) -> str:
        """Pass-through to the oracle's own view, so a UI never has to know
        BugVault's address to show a researcher's track record."""
        return self._oracle().view().get_reputation(str(researcher))

    @gl.public.view
    def get_program(self, program_id: int) -> str:
        p = self.programs.get(u32(int(program_id)))
        if p is None:
            return json.dumps({"ok": False, "reason": "unknown program"})
        out = self._program_view(p)
        out["ok"] = True
        return json.dumps(out)

    @gl.public.view
    def get_enrollees(self, program_id: int) -> str:
        bucket = self.program_enrollees.get(u32(int(program_id)))
        rows = []
        if bucket is not None:
            for addr in bucket:
                rows.append(str(addr))
        return json.dumps({"ok": True, "enrollees": rows})

    @gl.public.view
    def get_enrollments_by_researcher(self, researcher: str) -> str:
        bucket = self.researcher_enrollments.get(Address(str(researcher)))
        rows = []
        if bucket is not None:
            for pid in bucket:
                rows.append(int(pid))
        return json.dumps({"ok": True, "program_ids": rows})

    @gl.public.view
    def get_tier_thresholds(self) -> str:
        out = {}
        for tier in TIERS:
            found = self.tier_thresholds.get(tier)
            out[tier] = int(found) if found is not None else 0
        return json.dumps(out)

    @gl.public.view
    def list_programs(self, cursor: int, limit: int) -> str:
        """Newest-first feed over all programs with a hard scan bound;
        next_cursor of -1 means the walk reached program 1."""
        lim = limit
        if lim < 1:
            lim = 1
        if lim > MAX_PAGE:
            lim = MAX_PAGE
        cur = int(cursor)
        if cur < 0:
            cur = 0
        if cur > MAX_SCAN:
            cur = MAX_SCAN
        rows = []
        scanned = 0
        i = int(self.next_program_id) - cur
        while i >= 1 and scanned < MAX_SCAN and len(rows) < lim:
            p = self.programs.get(u32(i))
            scanned += 1
            if p is not None:
                view = self._program_view(p)
                view["ok"] = True
                rows.append(view)
            i -= 1
        nxt = (cur + scanned) if i >= 1 and len(rows) == lim else -1
        return json.dumps({"ok": True, "programs": rows, "next_cursor": nxt})

    @gl.public.view
    def get_stats(self) -> str:
        return json.dumps({
            "ok": True,
            "total_programs": int(self.next_program_id),
            "count_admitted": int(self.count_admitted),
            "count_denied": int(self.count_denied),
            "oracle": str(self.oracle),
        })
