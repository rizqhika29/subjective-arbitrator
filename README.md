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
| Consensus mechanism | `gl.eq_principle.prompt_comparative` — every validator independently re-weighs both parties' evidence against the stated criteria; the network only finalizes a verdict validators substantively agree on (`verdict` + `confidence` must match; `reasoning` wording may vary — see "Design lesson"). |
| Structured, two-sided input | A case has an explicit claimant and respondent, each with their own bounded evidence list, plus an explicit `resolution_criteria` string — the model applies a standard the parties agreed to, not one it invents. |
| A real chance to respond | `resolve_case` is gated on the respondent calling `mark_response_ready` first, for **both** the initial round and the appeal round. A case can't be decided off the claimant's evidence alone before the respondent has had a chance to answer. |
| Real appeal evidence exchange | `appeal_case` reopens evidence submission for both sides (`add_claimant_evidence` / `add_respondent_evidence`) and resets the ready gate, so the appeal round isn't silently decided on evidence frozen from the first round. |
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
  respondent_ready:     bool  # gates resolve_case for the current round

SubjectiveArbitrator
  cases:       TreeMap[str, Case]  # keyed by "case-<n>"
  case_count:  u256
```

## Case lifecycle

```
open_case (by claimant)                     add_claimant_evidence
      │                                      add_respondent_evidence
      ▼                                      mark_response_ready (respondent)
    OPEN ───────────────────resolve_case────► VERDICT_REACHED
      ▲  (blocked until respondent_ready)            │
      │                                          appeal_case
      │                                                ▼
      └──────────────── evidence reopens ──────── APPEALED
                                                        │
                                        resolve_case (blocked until
                                        respondent_ready again)
                                                        ▼
                                                     FINAL
```

`resolve_case` always requires `respondent_ready == True` for the round
it's resolving; `mark_response_ready` resets to `False` after every
resolution, so the appeal round needs its own fresh ready signal. Only
one appeal round exists by design — see "Limitations".

## Public interface

- `open_case(respondent: Address, dispute_summary: str, resolution_criteria: str, claimant_evidence: list[str]) -> str` — caller becomes claimant; returns `case_id`.
- `add_respondent_evidence(case_id: str, evidence: list[str]) -> None` — respondent-only; allowed while `OPEN` or `APPEALED`.
- `add_claimant_evidence(case_id: str, evidence: list[str]) -> None` — claimant-only; allowed while `OPEN` or `APPEALED` (mainly for appeal rebuttals).
- `mark_response_ready(case_id: str) -> None` — respondent-only; must be called before `resolve_case` will run for the current round.
- `resolve_case(case_id: str) -> None` — runs one consensus round; reverts if `respondent_ready` is not set.
- `appeal_case(case_id: str) -> None` — claimant or respondent only, once per case; reopens evidence and resets the ready gate.
- `get_case(case_id: str) -> Case` — view.
- `get_case_count() -> u256` — view.

## Testing

- `test/test_normalize_verdict.py` — pure-Python unit tests of
  `_validate_evidence` / `_normalize_verdict`. No Studio, no network:
  `pytest test/test_normalize_verdict.py`
- `test/test_subjective_arbitrator_integration.py` — full lifecycle
  (open → blocked resolve → evidence → ready → resolve → appeal →
  blocked resolve → evidence → ready → resolve → FINAL) plus access
  control checks, against a running GenLayer Studio / local validator
  set: `gltest test/test_subjective_arbitrator_integration.py`

## Known limitations (by design)

- No staking, bonding, or slashing — access control is enforced, but
  economic griefing resistance (e.g. requiring a bond to open a case or
  to appeal) is left to the composing contract.
- No automatic timeout: if a respondent never calls `mark_response_ready`,
  the case stays stuck in `OPEN` (or `APPEALED`) indefinitely — there is
  an enforceable response-ready *condition*, but not yet a time-based
  fallback that lets the claimant force resolution after a window
  elapses. A composing contract that needs liveness guarantees against
  an unresponsive respondent should add its own timeout/escalation path
  (e.g. tracking block height and calling a future `force_resolve_after`
  variant) on top of this primitive.
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

## Design lesson: gate resolution on a response-ready signal

The first submitted version let anyone call `resolve_case` immediately
after `open_case`, and only allowed respondent evidence while the case
was `OPEN` — so an eager caller could resolve a case (and, later, its
appeal too, since evidence was frozen after the first round) using only
the claimant's side of the story. The fix adds `respondent_ready`, set
via `mark_response_ready` and required by `resolve_case`, and reopens
both parties' evidence submission during `APPEALED`. General takeaway:
in a primitive with two interested parties, "who can trigger the
non-deterministic step" and "whose evidence is actually in scope when it
runs" need to be checked together — gating the trigger without also
reopening the input channel (or vice versa) leaves the same class of bug
in a different spot.

## Deployed instance (GenLayer Studio)

- Contract address: `0x70e193401E833F4413d3D56Ec0e3247C2aF62014`
- Explorer: https://explorer-studio.genlayer.com/address/0x70e193401E833F4413d3D56Ec0e3247C2aF62014

Manually exercised end-to-end on this deployment, including the two
fixes from review: `open_case` → `resolve_case` blocked with
`respondent has not marked this round response-ready` (confirms a case
can no longer be resolved off the claimant's evidence alone) →
`add_respondent_evidence` → `mark_response_ready` → `resolve_case`
(VERDICT_REACHED) → `appeal_case` (APPEALED, `respondent_ready` reset)
→ `resolve_case` blocked again pre-ready → `add_claimant_evidence` +
`add_respondent_evidence` (both sides added fresh evidence during the
appeal round) → `mark_response_ready` → `resolve_case` (FINAL, with
both parties' appeal-round evidence reflected in state).

## Dependency pin

The contract pins `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`
in its `Depends` header. Update this hash if you're targeting a
different SDK version.
