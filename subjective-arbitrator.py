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
   evidence (up to MAX_EVIDENCE_PER_PARTY URLs) against an explicit
   `resolution_criteria`. Critically, `resolve_case` is gated on the
   respondent explicitly calling `mark_response_ready` first -- a case
   can't be resolved the instant it's opened, off the claimant's
   evidence alone, before the respondent has had a chance to answer.
2. Comparative consensus. `gl.eq_principle.prompt_comparative` re-runs
   the full evidence-weighing step on every validator independently;
   the network only finalizes a verdict the validators substantively
   agree on (same verdict, same confidence tier -- see "Design lesson"
   in the README for why reasoning text is deliberately excluded from
   the match criteria).
3. A bounded appeal lifecycle with a real appeal evidence exchange.
   Either party can appeal a verdict exactly once (`APPEAL_LIMIT`).
   Appealing reopens evidence submission for both sides
   (`add_claimant_evidence` / `add_respondent_evidence`) and resets the
   response-ready gate, so the appeal round isn't silently decided on
   evidence frozen from the first round. The appeal verdict is FINAL.
4. Deterministic post-processing outside the non-deterministic block.
   `_normalize_verdict` and `_validate_evidence` are plain, side-effect
   free Python that run before/after consensus and are independently
   unit-testable.

Reuse pattern
-------------
A DAO, marketplace, or escrow contract holds this contract's address,
calls `open_case` when a dispute arises, lets both sides submit
evidence, calls `resolve_case`, and reads `get_case(case_id)` once
`status` is `VERDICT_REACHED` or `FINAL` to decide what to do with funds
or reputation. Token custody, staking, and slashing are deliberately
left to the composing contract -- see README "Limitations".
"""

from genlayer import *
from dataclasses import dataclass
import json

# --- Tunable constants -----------------------------------------------------

MAX_EVIDENCE_PER_PARTY = 3
MAX_EXCERPT_CHARS = 1500
APPEAL_LIMIT = 1  # each case may be appealed at most once

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
    respondent_ready: bool


def _validate_evidence(evidence: list[str]) -> list[str]:
    """Deterministic validation shared by case creation and evidence
    submission. No I/O -- just format checking."""
    if len(evidence) > MAX_EVIDENCE_PER_PARTY:
        raise Exception(f"at most {MAX_EVIDENCE_PER_PARTY} evidence items per party")
    validated = []
    for item in evidence:
        item = item.strip()
        if not (item.startswith("http://") or item.startswith("https://")):
            raise Exception(f"invalid evidence URL: {item!r}")
        validated.append(item)
    return validated


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


def _normalize_verdict(raw: str) -> dict:
    """Deterministically parse and sanity-check the JSON string that
    validators already reached consensus on. Runs after consensus, does
    no I/O, and is safe to unit test directly."""
    try:
        data = json.loads(_strip_code_fence(raw))
    except (ValueError, TypeError):
        raise Exception("arbitrator: agreed response was not valid JSON")
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

    def __init__(self):
        self.cases = TreeMap()
        self.case_count = u256(0)

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
            respondent_ready=False,
        )
        return case_id

    @gl.public.write
    def add_respondent_evidence(self, case_id: str, evidence: list[str]) -> None:
        """The respondent submits their side's evidence. Allowed while
        the case is OPEN (initial round) or APPEALED (appeal round),
        so the respondent isn't locked out of the appeal evidence
        exchange."""
        case = self.cases.get(case_id)
        if case is None:
            raise Exception("unknown case_id")
        if gl.message.sender_address != case.respondent:
            raise Exception("only the respondent can add respondent evidence")
        if case.status != STATUS_OPEN and case.status != STATUS_APPEALED:
            raise Exception("evidence can only be added while the case is OPEN or APPEALED")

        existing = [e for e in case.respondent_evidence]
        validated_new = _validate_evidence(evidence)
        merged = existing + validated_new
        if len(merged) > MAX_EVIDENCE_PER_PARTY:
            raise Exception(f"at most {MAX_EVIDENCE_PER_PARTY} evidence items per party")

        case.respondent_evidence = merged
        self.cases[case_id] = case

    @gl.public.write
    def add_claimant_evidence(self, case_id: str, evidence: list[str]) -> None:
        """The claimant may add further evidence -- most usefully during
        an APPEALED round, to rebut new respondent evidence. Allowed
        while the case is OPEN or APPEALED, same as the respondent's
        method, for symmetry."""
        case = self.cases.get(case_id)
        if case is None:
            raise Exception("unknown case_id")
        if gl.message.sender_address != case.claimant:
            raise Exception("only the claimant can add claimant evidence")
        if case.status != STATUS_OPEN and case.status != STATUS_APPEALED:
            raise Exception("evidence can only be added while the case is OPEN or APPEALED")

        existing = [e for e in case.claimant_evidence]
        validated_new = _validate_evidence(evidence)
        merged = existing + validated_new
        if len(merged) > MAX_EVIDENCE_PER_PARTY:
            raise Exception(f"at most {MAX_EVIDENCE_PER_PARTY} evidence items per party")

        case.claimant_evidence = merged
        self.cases[case_id] = case

    @gl.public.write
    def mark_response_ready(self, case_id: str) -> None:
        """The respondent explicitly signals that they are done
        submitting evidence for the current round (initial or appeal)
        and the case may now be resolved. Required before `resolve_case`
        can run -- see that method's gate below -- so a case can no
        longer be resolved off of the claimant's evidence alone before
        the respondent has had a real chance to respond."""
        case = self.cases.get(case_id)
        if case is None:
            raise Exception("unknown case_id")
        if gl.message.sender_address != case.respondent:
            raise Exception("only the respondent can mark the case response-ready")
        if case.status != STATUS_OPEN and case.status != STATUS_APPEALED:
            raise Exception("case is not in a round awaiting a response")

        case.respondent_ready = True
        self.cases[case_id] = case

    @gl.public.write
    def resolve_case(self, case_id: str) -> None:
        """Run one consensus round: every validator independently reads
        both parties' evidence, applies the resolution criteria, and the
        network only accepts a verdict they substantively agree on.

        Requires `respondent_ready` to be set for the current round (via
        `mark_response_ready`) so a case can't be resolved the instant
        it's opened, before the respondent has had a chance to submit
        evidence -- otherwise both the initial verdict and, since
        evidence was frozen after OPEN, the appeal verdict too, would be
        decided on the claimant's evidence alone."""
        case = self.cases.get(case_id)
        if case is None:
            raise Exception("unknown case_id")
        if case.status != STATUS_OPEN and case.status != STATUS_APPEALED:
            raise Exception("case is not awaiting resolution")
        if not case.respondent_ready:
            raise Exception(
                "respondent has not marked this round response-ready -- "
                "call mark_response_ready(case_id) first"
            )

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
        case.respondent_ready = False
        self.cases[case_id] = case

    @gl.public.write
    def appeal_case(self, case_id: str) -> None:
        """Either party may appeal a reached verdict exactly once. This
        reopens the case for a fresh, from-scratch consensus round."""
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
        self.cases[case_id] = case

    # -- Read methods ----------------------------------------------------

    @gl.public.view
    def get_case(self, case_id: str) -> Case:
        case = self.cases.get(case_id)
        if case is None:
            raise Exception("unknown case_id")
        return case

    @gl.public.view
    def get_case_count(self) -> u256:
        return self.case_count
