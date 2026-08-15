# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
SubjectiveArbitrator
======================

A reusable Intelligent Contract primitive for resolving disputes whose
resolution criteria is inherently subjective -- "was this deliverable
satisfactory per the agreed spec?", "did this contributor's work meet
the DAO's bar?", "does this refund request have merit?" -- the kind of
question that can't be answered by a purely on-chain check because it
requires weighing evidence and applying judgment.

Why this is more than "AI decides X"
--------------------------------------
1. Two-sided, structured evidence, with a real chance to respond. A case
   has an explicit claimant and respondent, each submitting their own
   evidence against an explicit `resolution_criteria`. A round only
   resolves once BOTH parties have explicitly closed their side of the
   round (`mark_claimant_ready` + `mark_response_ready`) -- a case can't
   be decided off one side's evidence alone, and the other side always
   keeps a defined window to respond before any verdict is reached.
2. Comparative consensus. `gl.eq_principle.prompt_comparative` re-runs
   the full evidence-weighing step on every validator independently;
   the network only finalizes a verdict the validators substantively
   agree on (same verdict, same confidence tier -- see "Design lesson"
   in the README for why reasoning text is deliberately excluded from
   the match criteria).
3. A bounded appeal lifecycle with a real appeal evidence exchange.
   Either party can appeal a verdict exactly once (`APPEAL_LIMIT`).
   Appealing reopens evidence submission for both sides and resets BOTH
   ready flags, so the appeal round isn't silently decided on evidence
   frozen from the first round, and neither party can close the appeal
   round before the other has had its defined chance to add fresh
   evidence. The appeal verdict is FINAL.
4. Deterministic liveness. Neither party can block a case forever by
   refusing to close the round: each round (initial and appeal) carries a
   `round_start_timestamp`, and either party can call `force_resolve(case_id)`
   after the configured `response_timeout_seconds` window has elapsed
   (the transaction timestamp is deterministic on GenLayer, so the
   deadline comparison is consensus-safe).
5. Per-round appeal evidence capacity. Evidence is capped per round, not
   cumulatively: the initial round allows up to `MAX_EVIDENCE_PER_PARTY`
   items per party, and the appeal round allows a fresh
   `MAX_EVIDENCE_PER_PARTY` items per party on top, so either party can
   always add new appeal evidence regardless of how many items were
   submitted in the first round.
6. A round-closing protocol with a frozen evidence snapshot. Closing a
   round is a two-sided act: each party independently marks their side
   ready, and the round only closes when BOTH have done so. A party that
   has marked ready can no longer add evidence (their side is frozen),
   and resolution is only accepted on a closed round -- so no one can
   slip evidence in after the round is closed, and a verdict can never
   race ahead of the other side's response.
7. Deterministic post-processing outside the non-deterministic block.
   `_normalize_verdict` and `_validate_evidence` are plain, side-effect
   free Python that run before/after consensus and are independently
   unit-testable.

Reuse pattern
-------------
A DAO, marketplace, or escrow contract holds this contract's address,
calls `open_case` when a dispute arises, lets both sides submit
evidence, and then reads `get_case(case_id)` once `status` is
`VERDICT_REACHED` or `FINAL` to decide what to do with funds or
reputation. Token custody, staking, and slashing are deliberately left
to the composing contract -- see README "Limitations".
"""

from genlayer import *
from dataclasses import dataclass
import json
from datetime import datetime, timezone

# --- Tunable constants -----------------------------------------------------

MAX_EVIDENCE_PER_PARTY = 3
MAX_EXCERPT_CHARS = 1500
APPEAL_LIMIT = 1  # each case may be appealed at most once

# Default response window. Either party may call force_resolve(case_id)
# once the current round's deadline (round_start_timestamp + timeout)
# has passed, even if one side never closed the round. Overridable via
# the constructor argument `response_timeout_seconds`.
RESPONSE_TIMEOUT_SECONDS = 604800  # 7 days

# --- Case status enum --------------------------------------------------

STATUS_OPEN = u8(0)              # awaiting first resolution
STATUS_VERDICT_REACHED = u8(1)   # resolved once, appealable
STATUS_APPEALED = u8(2)          # appealed, awaiting re-resolution
STATUS_FINAL = u8(3)             # appeal round resolved; no further rounds

VALID_VERDICTS = ("claimant", "respondent", "split")


@allow_storage
@dataclass
class Case:
    claimant: Address
    respondent: Address
    dispute_summary: str
    resolution_criteria: str
    claimant_evidence: DynArray[str]
    respondent_evidence: DynArray[str]
    status: u8
    verdict: str
    reasoning: str
    confidence: str
    appeal_count: u32
    claimant_ready: bool
    respondent_ready: bool
    round_start_timestamp: u256


def _current_timestamp() -> u256:
    """Deterministic per-transaction Unix timestamp (seconds). GenLayer
    pins the stdlib clock to the transaction datetime, so every
    validator computing the deadline comparison sees the same value."""
    return u256(int(datetime.now(timezone.utc).timestamp()))


def _validate_evidence(evidence: list[str]) -> list[str]:
    """Deterministic validation shared by case creation and evidence
    submission. No I/O -- just format checking. The per-call limit is
    MAX_EVIDENCE_PER_PARTY; the per-round cumulative cap is enforced in
    the write methods via _round_evidence_cap."""
    if len(evidence) > MAX_EVIDENCE_PER_PARTY:
        raise Exception(f"at most {MAX_EVIDENCE_PER_PARTY} evidence items per call")
    validated = []
    for item in evidence:
        item = item.strip()
        if not (item.startswith("http://") or item.startswith("https://")):
            raise Exception(f"invalid evidence URL: {item!r}")
        validated.append(item)
    return validated


def _round_evidence_cap(case: "Case") -> int:
    """Per-round evidence capacity. Round 0 (initial) allows
    MAX_EVIDENCE_PER_PARTY items per party; each appeal round grants a
    fresh MAX_EVIDENCE_PER_PARTY budget on top, so the cap grows with
    the number of rounds instead of locking parties out of the appeal
    evidence exchange."""
    return MAX_EVIDENCE_PER_PARTY * (int(case.appeal_count) + 1)


def _round_is_closed(case: "Case") -> bool:
    """A round is CLOSED only when BOTH parties have marked their side
    ready. Closing freezes the round's evidence snapshot: a party that
    has marked ready can no longer add evidence, resolution is only
    accepted on a closed round, and on appeal neither party can close the
    round before the other has had its defined chance to submit."""
    return bool(case.claimant_ready) and bool(case.respondent_ready)


def _strip_code_fence(raw: str) -> str:
    """Some LLM providers wrap JSON output in a markdown code fence
    (```json ... ```) even when explicitly told to return only JSON.
    Strip that before parsing so a well-formed-but-fenced response
    isn't rejected as invalid JSON."""
    s = raw.strip()
    if s.startswith("```"):
        first_newline = s.find("\n")
        s = s[first_newline + 1 :] if first_newline != -1 else s[3:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    return s


def _normalize_verdict(raw) -> dict:
    """Deterministically parse and sanity-check the agreed consensus
    result. `gl.eq_principle.prompt_comparative` hands back the agreed
    response as a parsed dict; a JSON string is also tolerated to stay
    robust across SDK versions. Runs after consensus, does no I/O, and
    is safe to unit test directly."""
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        try:
            data = json.loads(_strip_code_fence(raw))
        except (ValueError, TypeError):
            raise Exception("arbitrator: agreed response was not valid JSON")
    else:
        raise Exception("arbitrator: agreed response must be a JSON object or string")
    if not isinstance(data, dict):
        raise Exception("arbitrator: agreed response must be a JSON object")

    verdict = str(data.get("verdict", "")).strip().lower()
    confidence = str(data.get("confidence", "")).strip().lower()
    reasoning = str(data.get("reasoning", "")).strip()

    if verdict not in VALID_VERDICTS:
        raise Exception(f"arbitrator: verdict must be one of {VALID_VERDICTS}")
    if confidence not in ("low", "medium", "high"):
        confidence = "medium"
    if not reasoning:
        raise Exception("arbitrator: response is missing 'reasoning'")

    return {"verdict": verdict, "confidence": confidence, "reasoning": reasoning[:600]}


def _coerce_address(value) -> Address:
    """Studio's calldata parser can hand us an address as an `Address`,
    a hex/base64 `str`, or (if the value was typed unquoted, e.g. `0x..`
    without surrounding quotes) as a raw `int`. Normalize all three so
    write methods don't break depending on how the caller encoded the
    argument."""
    if isinstance(value, Address):
        return value
    if isinstance(value, str):
        return Address(value)
    if isinstance(value, int):
        return Address(value.to_bytes(20, "big"))
    return Address(bytes(value))


class SubjectiveArbitrator(gl.Contract):
    cases: TreeMap[str, Case]
    case_count: u256
    response_timeout_seconds: u256

    def __init__(self, response_timeout_seconds: int = RESPONSE_TIMEOUT_SECONDS):
        self.cases = TreeMap()
        self.case_count = u256(0)
        self.response_timeout_seconds = u256(response_timeout_seconds)

    # -- Write methods -------------------------------------------------

    @gl.public.write
    def open_case(
        self,
        respondent: Address,
        dispute_summary: str,
        resolution_criteria: str,
        claimant_evidence: list[str],
    ) -> str:
        """Open a new case. The caller becomes the claimant. Returns the
        new case_id."""
        claimant = gl.message.sender_address
        respondent = _coerce_address(respondent)
        if respondent == claimant:
            raise Exception("respondent must differ from claimant")

        dispute_summary = dispute_summary.strip()
        resolution_criteria = resolution_criteria.strip()
        if not dispute_summary:
            raise Exception("dispute_summary must not be empty")
        if not resolution_criteria:
            raise Exception("resolution_criteria must not be empty")

        validated_evidence = _validate_evidence(claimant_evidence)

        case_id = f"case-{self.case_count}"
        self.case_count = self.case_count + u256(1)

        self.cases[case_id] = Case(
            claimant=claimant,
            respondent=respondent,
            dispute_summary=dispute_summary,
            resolution_criteria=resolution_criteria,
            claimant_evidence=validated_evidence,
            respondent_evidence=[],
            status=STATUS_OPEN,
            verdict="",
            reasoning="",
            confidence="",
            appeal_count=u32(0),
            claimant_ready=False,
            respondent_ready=False,
            round_start_timestamp=_current_timestamp(),
        )
        return case_id

    @gl.public.write
    def add_respondent_evidence(self, case_id: str, evidence: list[str]) -> None:
        """The respondent submits their side's evidence. Allowed while
        the case is OPEN (initial round) or APPEALED (appeal round),
        so the respondent isn't locked out of the appeal evidence
        exchange. Once the respondent has marked their side ready
        (`mark_response_ready`), their evidence for this round is frozen
        and no further items are accepted."""
        case_id = str(case_id)
        case = self.cases.get(case_id)
        if case is None:
            raise Exception("unknown case_id")
        if gl.message.sender_address != case.respondent:
            raise Exception("only the respondent can add respondent evidence")
        if case.status != STATUS_OPEN and case.status != STATUS_APPEALED:
            raise Exception("evidence can only be added while the case is OPEN or APPEALED")
        if case.respondent_ready:
            raise Exception(
                "respondent has already closed this round's evidence -- "
                "no more respondent evidence is accepted until the next round"
            )

        existing = [e for e in case.respondent_evidence]
        validated_new = _validate_evidence(evidence)
        merged = existing + validated_new
        cap = _round_evidence_cap(case)
        if len(merged) > cap:
            raise Exception(
                f"evidence cap reached for this round: at most {cap} "
                f"total items per party across {int(case.appeal_count) + 1} round(s)"
            )

        case.respondent_evidence = merged
        self.cases[case_id] = case

    @gl.public.write
    def add_claimant_evidence(self, case_id: str, evidence: list[str]) -> None:
        """The claimant may add further evidence -- most usefully during
        an APPEALED round, to rebut new respondent evidence. Allowed
        while the case is OPEN or APPEALED. Once the claimant has marked
        their side ready (`mark_claimant_ready`), their evidence for this
        round is frozen and no further items are accepted."""
        case_id = str(case_id)
        case = self.cases.get(case_id)
        if case is None:
            raise Exception("unknown case_id")
        if gl.message.sender_address != case.claimant:
            raise Exception("only the claimant can add claimant evidence")
        if case.status != STATUS_OPEN and case.status != STATUS_APPEALED:
            raise Exception("evidence can only be added while the case is OPEN or APPEALED")
        if case.claimant_ready:
            raise Exception(
                "claimant has already closed this round's evidence -- "
                "no more claimant evidence is accepted until the next round"
            )

        existing = [e for e in case.claimant_evidence]
        validated_new = _validate_evidence(evidence)
        merged = existing + validated_new
        cap = _round_evidence_cap(case)
        if len(merged) > cap:
            raise Exception(
                f"evidence cap reached for this round: at most {cap} "
                f"total items per party across {int(case.appeal_count) + 1} round(s)"
            )

        case.claimant_evidence = merged
        self.cases[case_id] = case

    @gl.public.write
    def mark_claimant_ready(self, case_id: str) -> None:
        """The claimant explicitly signals they are done submitting
        evidence for the current round (initial or appeal). This freezes
        the claimant's evidence snapshot for the round -- `add_claimant_evidence`
        is no longer accepted -- and is one half of closing the round.

        Resolution (`resolve_case`) only runs once BOTH parties have
        marked ready, so a single side closing early (or on appeal, the
        respondent closing before the claimant has added fresh evidence)
        can never let a verdict race ahead of the other side's response.
        If one side never marks ready, `force_resolve(case_id)` is still
        available once the response window elapses."""
        case_id = str(case_id)
        case = self.cases.get(case_id)
        if case is None:
            raise Exception("unknown case_id")
        if gl.message.sender_address != case.claimant:
            raise Exception("only the claimant can mark their side of the round ready")
        if case.status != STATUS_OPEN and case.status != STATUS_APPEALED:
            raise Exception("case is not in a round awaiting a response")

        case.claimant_ready = True
        self.cases[case_id] = case

    @gl.public.write
    def mark_response_ready(self, case_id: str) -> None:
        """The respondent explicitly signals they are done submitting
        evidence for the current round (initial or appeal). This freezes
        the respondent's evidence snapshot for the round --
        `add_respondent_evidence` is no longer accepted -- and is the
        other half of closing the round.

        Resolution (`resolve_case`) only runs once BOTH parties have
        marked ready, so the respondent can no longer close their side
        (e.g. immediately after an appeal) and have the case resolved
        before the claimant has had their defined chance to add fresh
        evidence. If one side never marks ready, `force_resolve(case_id)`
        is still available once the response window elapses."""
        case_id = str(case_id)
        case = self.cases.get(case_id)
        if case is None:
            raise Exception("unknown case_id")
        if gl.message.sender_address != case.respondent:
            raise Exception("only the respondent can mark their side of the round ready")
        if case.status != STATUS_OPEN and case.status != STATUS_APPEALED:
            raise Exception("case is not in a round awaiting a response")

        case.respondent_ready = True
        self.cases[case_id] = case

    @gl.public.write
    def resolve_case(self, case_id: str) -> None:
        """Run one consensus round: every validator independently reads
        both parties' evidence, applies the resolution criteria, and the
        network only accepts a verdict they substantively agree on.

        The round must be CLOSED before this runs: both the claimant
        (`mark_claimant_ready`) and the respondent (`mark_response_ready`)
        have to mark their side ready. Closing is what freezes the round's
        evidence snapshot, so the verdict is produced from exactly the
        evidence both sides committed to, and a case can't be resolved off
        one side's evidence alone before the other side has responded --
        on the initial round or on appeal.

        Liveness fallback: if one side never closes the round, either
        party can call `force_resolve(case_id)` after the response window
        (see `get_response_deadline`) has elapsed."""
        case_id = str(case_id)
        case = self.cases.get(case_id)
        if case is None:
            raise Exception("unknown case_id")
        if case.status != STATUS_OPEN and case.status != STATUS_APPEALED:
            raise Exception("case is not awaiting resolution")
        if not _round_is_closed(case):
            raise Exception(
                "round is not closed -- BOTH sides must mark their "
                "evidence ready before resolve_case may run: claimant "
                "calls mark_claimant_ready(case_id), respondent calls "
                "mark_response_ready(case_id); or wait for the response "
                "window to elapse and call force_resolve(case_id)"
            )

        self._run_consensus_round(case_id)

    @gl.public.write
    def force_resolve(self, case_id: str) -> None:
        """Force-resolution path for liveness. If one side never closes
        the round (never calls `mark_claimant_ready` /
        `mark_response_ready`), either party to the case may call this
        once the current round's response deadline has passed
        (`round_start_timestamp + response_timeout_seconds`, a
        deterministic per-transaction timestamp -- see
        `get_response_deadline`). This runs the exact same consensus
        round as `resolve_case`, so the verdict still requires validator
        agreement on the evidence that is in scope."""
        case_id = str(case_id)
        case = self.cases.get(case_id)
        if case is None:
            raise Exception("unknown case_id")
        if case.status != STATUS_OPEN and case.status != STATUS_APPEALED:
            raise Exception("case is not awaiting resolution")

        sender = gl.message.sender_address
        if sender != case.claimant and sender != case.respondent:
            raise Exception("only a party to the case can force-resolve")

        now = _current_timestamp()
        deadline = case.round_start_timestamp + self.response_timeout_seconds
        if now < deadline:
            raise Exception(
                "response window has not elapsed yet -- cannot force-resolve "
                f"(deadline is block timestamp {int(deadline)}, now is {int(now)})"
            )

        self._run_consensus_round(case_id)

    @gl.public.write
    def appeal_case(self, case_id: str) -> None:
        """Either party may appeal a reached verdict exactly once. This
        reopens the case for a fresh, from-scratch consensus round,
        resets BOTH response-ready flags, and starts a fresh response
        window for the appeal round -- so neither party can close the
        appeal round before the other has had their defined chance to add
        fresh evidence."""
        case_id = str(case_id)
        case = self.cases.get(case_id)
        if case is None:
            raise Exception("unknown case_id")

        sender = gl.message.sender_address
        if sender != case.claimant and sender != case.respondent:
            raise Exception("only a party to the case can appeal")
        if case.status != STATUS_VERDICT_REACHED:
            raise Exception("only a case with a reached verdict can be appealed")
        if case.appeal_count >= u32(APPEAL_LIMIT):
            raise Exception("appeal limit already reached")

        case.appeal_count = case.appeal_count + u32(1)
        case.status = STATUS_APPEALED
        case.claimant_ready = False
        case.respondent_ready = False
        case.round_start_timestamp = _current_timestamp()
        self.cases[case_id] = case

    # -- Internal helpers ------------------------------------------------

    def _run_consensus_round(self, case_id: str) -> None:
        """Shared consensus execution used by both `resolve_case` and
        `force_resolve`. Reads the case's closed-round evidence, runs the
        comparative-equivalence arbitration, and writes the agreed
        verdict back to storage."""
        case = self.cases[case_id]
        dispute_summary = case.dispute_summary
        resolution_criteria = case.resolution_criteria
        claimant_evidence = [e for e in case.claimant_evidence]
        respondent_evidence = [e for e in case.respondent_evidence]
        is_appeal_round = case.status == STATUS_APPEALED

        def independent_arbitration() -> str:
            # Runs independently, in a sandboxed VM, on every validator.
            # Storage is inaccessible here -- only the closed-over locals
            # and the network are available.
            def render(label, urls):
                if not urls:
                    return f"(no {label.lower()} evidence submitted)"
                parts = []
                for url in urls:
                    try:
                        page = gl.get_webpage(url, mode="text")
                    except Exception:
                        page = "(evidence unreachable)"
                    parts.append(f"{label} EVIDENCE ({url}):\n{page[:MAX_EXCERPT_CHARS]}")
                return "\n\n".join(parts)

            evidence_block = (
                render("CLAIMANT", claimant_evidence)
                + "\n\n"
                + render("RESPONDENT", respondent_evidence)
            )

            appeal_note = (
                "\nNOTE: this is an APPEAL of a prior verdict. Re-evaluate the "
                "evidence independently and rigorously -- do not assume the "
                "earlier verdict was correct.\n"
                if is_appeal_round
                else ""
            )

            prompt = f"""You are a neutral arbitrator for a subjective dispute.
Weigh both parties' evidence against the resolution criteria and reach a
verdict. Be fair to both sides; do not favor whichever party submitted
more evidence if the substance doesn't support it.
{appeal_note}
DISPUTE SUMMARY: {dispute_summary}

RESOLUTION CRITERIA: {resolution_criteria}

{evidence_block}

Respond with ONLY a JSON object, no other text, no markdown code
fences, no ```json wrapper -- the response must start with {{ and end
with }}:
{{"verdict": "claimant|respondent|split", "confidence": "low|medium|high", "reasoning": "<2-3 sentences applying the criteria to the evidence>"}}"""

            return gl.nondet.exec_prompt(prompt)

        raw = gl.eq_principle.prompt_comparative(
            independent_arbitration,
            principle=(
                "The 'verdict' field must match exactly (claimant, "
                "respondent, or split) and the 'confidence' tier must "
                "match. The 'reasoning' fields do NOT need to match in "
                "wording, emphasis, or which specific evidence they "
                "quote -- differing phrasing or framing is acceptable as "
                "long as the reasoning is coherent and does not "
                "contradict the stated verdict. Disagreement on the "
                "verdict itself or on the confidence tier means the "
                "results are NOT equivalent."
            ),
        )

        parsed = _normalize_verdict(raw)

        case.verdict = parsed["verdict"]
        case.confidence = parsed["confidence"]
        case.reasoning = parsed["reasoning"]
        case.status = STATUS_FINAL if is_appeal_round else STATUS_VERDICT_REACHED
        case.claimant_ready = False
        case.respondent_ready = False
        self.cases[case_id] = case

    # -- Read methods ----------------------------------------------------

    @gl.public.view
    def get_case(self, case_id: str) -> Case:
        case_id = str(case_id)
        case = self.cases.get(case_id)
        if case is None:
            raise Exception("unknown case_id")
        return case

    @gl.public.view
    def get_case_count(self) -> u256:
        return self.case_count

    @gl.public.view
    def get_response_deadline(self, case_id: str) -> u256:
        """Unix timestamp (transaction-time based) at which the current
        round's response window ends. Once `now >= deadline`, a party may
        call `force_resolve(case_id)` even if one side never closed the
        round."""
        case_id = str(case_id)
        case = self.cases.get(case_id)
        if case is None:
            raise Exception("unknown case_id")
        return case.round_start_timestamp + self.response_timeout_seconds

    @gl.public.view
    def get_response_timeout(self) -> u256:
        """Configured response window length in seconds."""
        return self.response_timeout_seconds
