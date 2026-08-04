# SubjectiveArbitrator

A reusable GenLayer Intelligent Contract primitive for resolving disputes
whose resolution criteria is inherently **subjective** — "was this
deliverable completed per the agreed spec?", "did this contribution meet
the DAO's bar?", "does this refund request have merit?" — questions a
purely on-chain check can't answer because they require weighing evidence
and applying judgment.

Other contracts (DAOs, marketplaces, freelance/escrow platforms) hold
this contract's address, open a case when a dispute arises, and read
`get_case(case_id)` once `status` is `VERDICT_REACHED` or `FINAL` to
decide what to do with funds or reputation.

## Why this is a primitive, not a demo

| Concern | How it's handled |
|---|---|
| Non-determinism | Evidence fetches + the LLM call live inside one closure (`independent_arbitration`), the only place `gl.get_webpage` / `gl.nondet.exec_prompt` are allowed to run. |
| Consensus mechanism | `gl.eq_principle.prompt_comparative` — every validator independently re-weighs both parties' evidence against the stated criteria; the network only finalizes a verdict validators substantively agree on. |
| Structured, two-sided input | A case has an explicit claimant and respondent, each with their own bounded evidence list, plus an explicit `resolution_criteria` string — the model applies a standard the parties agreed to, not one it invents. |
| Deterministic validation | `_validate_evidence` and `_normalize_verdict` are plain, I/O-free Python, independently unit-testable. |
| Due process | A verdict can be appealed exactly once (`APPEAL_LIMIT`); the appeal reruns consensus from scratch with an explicit instruction not to defer to the earlier verdict, then the case is `FINAL`. |

## State design

```
Case
  claimant:            Address
  respondent:           Address
  dispute_summary:      str
  resolution_criteria:  str
  claimant_evidence:    DynArray[str]  # up to MAX_EVIDENCE_PER_PARTY URLs
  respondent_evidence:  DynArray[str]
  status:               u8   # OPEN=0, VERDICT_REACHED=1, APPEALED=2, FINAL=3
  verdict:              str  # "claimant" | "respondent" | "split"
  reasoning:            str
  confidence:           str
  appeal_count:         u32

SubjectiveArbitrator
  cases:       TreeMap[str, Case]  # keyed by "case-<n>"
  case_count:  u256
```

## Case lifecycle

```
open_case (by claimant)
      │
      ▼
    OPEN ──resolve_case──► VERDICT_REACHED ──appeal_case──► APPEALED
                                                                 │
                                                          resolve_case
                                                                 ▼
                                                               FINAL
```

Only one appeal round exists by design — see "Limitations".

## Public interface

- `open_case(respondent: Address, dispute_summary: str, resolution_criteria: str, claimant_evidence: list[str]) -> str` — caller becomes claimant; returns `case_id`.
- `add_respondent_evidence(case_id: str, evidence: list[str]) -> None` — respondent-only, only while `OPEN`.
- `resolve_case(case_id: str) -> None` — runs one consensus round.
- `appeal_case(case_id: str) -> None` — claimant or respondent only, once per case.
- `get_case(case_id: str) -> Case` — view.
- `get_case_count() -> u256` — view.

## Testing

- `test/test_normalize_verdict.py` — pure-Python unit tests of
  `_validate_evidence` / `_normalize_verdict`. No Studio, no network:
  `pytest test/test_normalize_verdict.py`
- `test/test_subjective_arbitrator_integration.py` — full lifecycle
  (open → evidence → resolve → appeal → resolve → FINAL) plus access
  control checks, against a running GenLayer Studio / local validator
  set: `gltest test/test_subjective_arbitrator_integration.py`

## Known limitations (by design)

- No staking, bonding, or slashing — access control is enforced (only
  the respondent adds respondent evidence, only a party can appeal), but
  economic griefing resistance (e.g. requiring a bond to open a case or
  to appeal) is left to the composing contract.
- No timeouts — a respondent who never submits evidence doesn't block
  `resolve_case`; the arbitrator will just note "(no respondent evidence
  submitted)" and weigh accordingly.
- Exactly one appeal is allowed; there is no second-level appellate body.
  If your use case needs a longer appeal chain or a human tiebreaker
  after `FINAL`, add that in the composing contract.
- Evidence items are URLs fetched via `gl.get_webpage`; there's no
  built-in way to submit raw text as evidence (though `dispute_summary`
  / `resolution_criteria` are free text).

## Design lesson: keep the equivalence principle narrow

An earlier version of the `principle` text passed to
`gl.eq_principle.prompt_comparative` also required validators'
*reasoning* to "apply the same resolution criteria to the same
evidence." In testing this produced repeated `Undetermined` consensus
results even when every validator reached the same `verdict` and
`confidence` — the reasoning text is free-form language, so its exact
phrasing differs between independent LLM calls even when the substance
agrees, and the equivalence check (itself LLM-judged) was penalizing
that wording drift.

The fix was to narrow the principle to only require exact agreement on
`verdict` and `confidence` (the fields that actually drive downstream
logic), while explicitly telling the equivalence check that differing
phrasing/emphasis in `reasoning` is acceptable. General takeaway for
anyone building on `prompt_comparative`: only put a field in the
equivalence principle's matching criteria if disagreement on that exact
field should actually block consensus — free-text justification fields
are usually better treated as informational, not part of what must
match.

## Deployed instance (GenLayer Studio)

- Contract address: `0xc992059580FA35baaBE47D6aB5a6ed1845af61A6`
- Explorer: https://explorer-studio.genlayer.com/address/0xc992059580FA35baaBE47D6aB5a6ed1845af61A6

Manually exercised end-to-end on this deployment: `open_case` →
`resolve_case` (VERDICT_REACHED via `prompt_comparative` consensus) →
`appeal_case` (VERDICT_REACHED → APPEALED) → `resolve_case` again
(APPEALED → FINAL), plus access-control checks (only the respondent can
add respondent evidence, only a party to the case can appeal, appeals
are capped at `APPEAL_LIMIT`).

## Dependency pin

The contract pins `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`
in its `Depends` header. Update this hash if you're targeting a
different SDK version.
