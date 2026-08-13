# SubjectiveArbitrator

A reusable GenLayer Intelligent Contract primitive for resolving disputes whose resolution criteria is inherently **subjective** — "was this deliverable completed per the agreed spec?", "did this contribution meet the DAO's bar?", "does this refund request have merit?" — questions a purely on-chain check can't answer because they require weighing evidence and applying judgment.

Other contracts (DAOs, marketplaces, freelance/escrow platforms) hold this contract's address, open a case when a dispute arises, and read `get_case(case_id)` once `status` is `VERDICT_REACHED` or `FINAL` to decide what to do with funds or reputation.

## Why this is a primitive, not a demo

Concern

How it's handled

Non-determinism

Evidence fetches + the LLM call live inside one closure (`independent_arbitration`), the only place `gl.get_webpage` / `gl.nondet.exec_prompt` are allowed to run.

Consensus mechanism

`gl.eq_principle.prompt_comparative` — every validator independently re-weighs both parties' evidence against the stated criteria; the network only finalizes a verdict validators substantively agree on (`verdict` + `confidence` must match; `reasoning` wording may vary — see "Design lesson").

Structured, two-sided input

A case has an explicit claimant and respondent, each with their own bounded evidence list, plus an explicit `resolution_criteria` string — the model applies a standard the parties agreed to, not one it invents.

A real chance to respond

`resolve_case` is gated on the respondent calling `mark_response_ready` first — a case can't be decided off the claimant's evidence alone before the respondent has had a chance to answer. **Liveness fix:** if the respondent never marks the round ready, either party can call `force_resolve(case_id)` after the deterministic response window (`round_start_timestamp + response_timeout_seconds`, based on GenLayer's per-transaction timestamp) elapses — the respondent cannot block resolution forever.

Real appeal evidence exchange

`appeal_case` reopens evidence submission for both sides (`add_claimant_evidence` / `add_respondent_evidence`) and resets the ready gate, so the appeal round isn't silently decided on evidence frozen from the first round. **Liveness fix:** evidence is capped **per round**, not cumulatively — the initial round allows up to `MAX_EVIDENCE_PER_PARTY` items per party and each appeal round grants a fresh `MAX_EVIDENCE_PER_PARTY` budget on top, so either party can always add fresh appeal evidence.

Deterministic validation

`_validate_evidence` and `_normalize_verdict` are plain, I/O-free Python, independently unit-testable.

Due process

A verdict can be appealed exactly once (`APPEAL_LIMIT`); the appeal reruns consensus from scratch with an explicit instruction not to defer to the earlier verdict, then the case is `FINAL`.

## The two review fixes (liveness)

GenLayer review requested two lifecycle fixes for a respondent who is unresponsive (or adversarial):

1. **Respondent can block resolution forever** — only `mark_response_ready` (respondent-only) unlocked `resolve_case`, so an unresponsive respondent could stall the case indefinitely. Fixed by adding a deterministic **response window** and a **`force_resolve` path**: each round records `round_start_timestamp` when it opens (case creation or appeal), configured via the `response_timeout_seconds` constructor arg (default 7 days). Once the window passes, either party may `force_resolve(case_id)`, which runs the identical consensus round. Because GenLayer pins `datetime.now()` to the transaction timestamp, the `now >= deadline` comparison is deterministic across validators.

2. **Cumulative 3-item evidence cap blocked appeal evidence** — the old cap (`MAX_EVIDENCE_PER_PARTY = 3`) was checked against the merged, lifetime totals, so a party that hit 3 items in the initial round had no room for fresh appeal evidence. Fixed by making the cap **per round**: the allowed total is `MAX_EVIDENCE_PER_PARTY × (appeal_count + 1)`, i.e. the initial round allows 3 items per party and the appeal round grants a fresh 3-item budget per party.

## State design

```
Case
  claimant:            Address
  respondent:           Address
  dispute_summary:      str
  resolution_criteria:  str
  claimant_evidence:    DynArray[str]  # per-round budget: 3 initial + 3 appeal
  respondent_evidence:  DynArray[str]
  status:               u8   # OPEN=0, VERDICT_REACHED=1, APPEALED=2, FINAL=3
  verdict:              str  # "claimant" | "respondent" | "split"
  reasoning:            str
  confidence:           str
  appeal_count:         u32
  respondent_ready:     bool  # gates resolve_case for the current round
  round_start_timestamp u256  # window start of the current round (unix secs)

SubjectiveArbitrator
  cases:                  TreeMap[str, Case]  # keyed by "case-<n>"
  case_count:             u256
  response_timeout_seconds u256  # response window length (constructor arg)
```

## Case lifecycle

```
open_case (by claimant)                     add_claimant_evidence
      │                                      add_respondent_evidence
      ▼                                      mark_response_ready (respondent)
    OPEN ───────────────────resolve_case────► VERDICT_REACHED
      ▲              └─force_resolve──────►         │
      │  (blocked until respondent_ready           appeal_case
      │   OR response window elapsed)                    ▼
      └──────────────── evidence reopens ──────── APPEALED
                                                        │
                                  resolve_case ── (blocked until
                                      │         respondent_ready again)
                                      │   or force_resolve after window
                                      ▼
                                     FINAL
```

`resolve_case` always requires `respondent_ready == True` for the round it's resolving; `mark_response_ready` resets to `False` after every resolution, so the appeal round needs its own fresh ready signal. `force_resolve` bypasses the ready gate only after the round's response deadline, so a case can always progress. Only one appeal round exists by design — see "Limitations".

## Public interface

- `open_case(respondent: Address, dispute_summary: str, resolution_criteria: str, claimant_evidence: list[str]) -> str` — caller becomes claimant; returns `case_id`.
- `add_respondent_evidence(case_id: str, evidence: list[str]) -> None` — respondent-only; allowed while `OPEN` or `APPEALED`; each round has its own evidence budget.
- `add_claimant_evidence(case_id: str, evidence: list[str]) -> None` — claimant-only; allowed while `OPEN` or `APPEALED` (mainly for appeal rebuttals).
- `mark_response_ready(case_id: str) -> None` — respondent-only; must be called before `resolve_case` will run for the current round.
- `resolve_case(case_id: str) -> None` — runs one consensus round; reverts if `respondent_ready` is not set.
- `force_resolve(case_id: str) -> None` — **liveness path**: either party may run the same consensus round once `now >= response_deadline`, without needing the respondent's ready signal.
- `appeal_case(case_id: str) -> None` — claimant or respondent only, once per case; reopens evidence and resets the ready gate and the response window.
- `get_case(case_id: str) -> Case` — view.
- `get_case_count() -> u256` — view.
- `get_response_deadline(case_id: str) -> u256` — view: unix timestamp when the current round's response window ends.
- `get_response_timeout() -> u256` — view: configured window length in seconds.

## Testing

- `test/test_normalize_verdict.py` — pure-Python unit tests of `_validate_evidence` / `_normalize_verdict` / `_round_evidence_cap`. No Studio, no network: `pytest test/test_normalize_verdict.py`
- `test/test_subjective_arbitrator_direct.py` — direct-mode tests (no server) for the two liveness fixes (deploy with `response_timeout_seconds=0` to exercise `force_resolve` immediately, or a huge window to prove it is deadline-gated), per-round evidence budget, and access control: `pytest test/test_subjective_arbitrator_direct.py`
- `test/test_subjective_arbitrator_integration.py` — full lifecycle (open → blocked resolve → evidence → ready → resolve → appeal → blocked resolve → evidence → ready → resolve → FINAL) plus `force_resolve` and per-round evidence capacity, against a running GenLayer Studio / local validator set: `gltest test/test_subjective_arbitrator_integration.py`. When no validator network is reachable these tests auto-skip, so plain `pytest test/` still works.

Test-runner notes gathered on Windows:

- Setup: `pip install genlayer-test pytest certifi`. `genlayer-test` pulls `genlayer-py`, which is the SDK imported as `genlayer` in the contract.
- The plugin requires the config env vars it references (e.g. `PRIVATE_KEY_1`) to be set, and `gltest.config.yaml` needs a `networks.default` entry naming the selected network.
- Direct mode downloads the pinned `py-genlayer` runner from a GitHub genvm release into `~/.cache/gltest-direct`. Recent `v0.3.x` rc releases ship the runners tarball as `genvm-runners-all.tar.xz` (not `genvm-universal.tar.xz`); if the loader 404s, fetch that asset under the loader's expected filename. On Windows set `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` to a certifi bundle if certificate verification fails.
- Two small Windows fixes were applied to the installed `gltest` SDK (`gltest.direct.loader` and `gltest.direct.vm`): defer the stdin temp-file unlink until stdin is restored (a file referenced by an open fd cannot be deleted on Windows). No contract change was required.

## Known limitations (by design)

- No staking, bonding, or slashing — access control is enforced, but economic griefing resistance (e.g. requiring a bond to open a case or to appeal) is left to the composing contract.
- The response window is a **relative** timeout measured from round start (`round_start_timestamp + response_timeout_seconds`), which fixes the *respondent-can-block-forever* liveness hole. If a window should also be capped in absolute terms (or triggered by a missed deadline event rather than a pull-based `force_resolve`), a composing contract can add that on top.
- Exactly one appeal is allowed; there is no second-level appellate body. If your use case needs a longer appeal chain or a human tiebreaker after `FINAL`, add that in the composing contract.
- Evidence items are URLs fetched via `gl.get_webpage`; there's no built-in way to submit raw text as evidence (though `dispute_summary` / `resolution_criteria` are free text).

## Design lesson: keep the equivalence principle narrow

An earlier version of the `principle` text passed to `gl.eq_principle.prompt_comparative` also required validators' *reasoning* to "apply the same resolution criteria to the same evidence." In testing this produced repeated `Undetermined` consensus results even when every validator reached the same `verdict` and `confidence` — the reasoning text is free-form language, so its exact phrasing differs between independent LLM calls even when the substance agrees, and the equivalence check (itself LLM-judged) was penalizing that wording drift.

The fix was to narrow the principle to only require exact agreement on `verdict` and `confidence` (the fields that actually drive downstream logic), while explicitly telling the equivalence check that differing phrasing/emphasis in `reasoning` is acceptable. General takeaway for anyone building on `prompt_comparative`: only put a field in the equivalence principle's matching criteria if disagreement on that exact field should actually block consensus — free-text justification fields are usually better treated as informational, not part of what must match.

## Design lesson: gate resolution on a response-ready signal — but make it time-bounded

The first submitted version let anyone call `resolve_case` immediately after `open_case`, and only allowed respondent evidence while the case was `OPEN` — so an eager caller could resolve a case (and, later, its appeal too, since evidence was frozen after the first round) using only the claimant's side of the story. The second version added `respondent_ready`, set via `mark_response_ready` and required by `resolve_case`, and reopened both parties' evidence submission during `APPEALED`. Review then pointed out that a pure ready-gate lets an unresponsive respondent block resolution forever, and a cumulative evidence cap can lock a party out of fresh appeal evidence. The fixes: a deterministic window + `force_resolve` fallback, and per-round evidence budgets. General takeaway: in a primitive with two interested parties, "who can trigger the non-deterministic step", "whose evidence is in scope when it runs", and "what happens if a party never cooperates" need to be designed together — a gate without a liveness fallback, or a cap without a per-round budget, each leaves part of the same class of bug behind.

## Deployed instance (GenLayer Studio)

> **Note:** the contract changed (new `round_start_timestamp` field, new `force_resolve`/`get_response_deadline`/`get_response_timeout` methods, per-round evidence cap). The instance below includes all these changes plus the two review-liveness fixes.

Deployed with `response_timeout_seconds = 604800` (7-day default window). All write methods were re-tested live against this address: every gate (`resolve_case` before ready, evidence caps, claimant-calling-respondent methods, `force_resolve` before deadline) reverts as designed, and the full positive path (ready → resolve → VERDICT_REACHED → appeal → fresh appeal budget → FINAL) verified on-chain.

| Network | Address | Explorer |
|---------|---------|----------|
| Studio | `0xfe059719E5CeAf77E95B739f071D53b8761A7727` | [Explorer](https://explorer-studio.genlayer.com/address/0xfe059719E5CeAf77E95B739f071D53b8761A7727) |

## Dependency pin

The contract pins `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6` in its `Depends` header. Update this hash if you're targeting a different SDK version.
