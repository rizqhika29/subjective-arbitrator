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

A real chance to respond — the round-closing protocol

`resolve_case` only runs once **both** parties have closed the round (`mark_claimant_ready` + `mark_response_ready`). Closing a round does two things at once: it **freezes the evidence snapshot** (a party that has marked ready can no longer add evidence for that round) and it **guarantees a defined response opportunity** (resolution can never run while the other side still has the floor). On appeal, `appeal_case` resets **both** ready flags, so one side (e.g. the respondent) can no longer close the appeal round before the other has had its chance to add fresh evidence. **Liveness fix:** if one side never closes the round, either party can call `force_resolve(case_id)` after the deterministic response window (`round_start_timestamp + response_timeout_seconds`, based on GenLayer's per-transaction timestamp) elapses — a party cannot block resolution forever.

Real appeal evidence exchange

`appeal_case` reopens evidence submission for both sides (`add_claimant_evidence` / `add_respondent_evidence`) and resets both ready flags, so the appeal round isn't silently decided on evidence frozen from the first round, and neither side can close it before the other responds. **Liveness fix:** evidence is capped **per round**, not cumulatively — the initial round allows up to `MAX_EVIDENCE_PER_PARTY` items per party and each appeal round grants a fresh `MAX_EVIDENCE_PER_PARTY` budget on top, so either party can always add fresh appeal evidence.

Deterministic validation

`_validate_evidence` and `_normalize_verdict` are plain, I/O-free Python, independently unit-testable.

Due process

A verdict can be appealed exactly once (`APPEAL_LIMIT`); the appeal reruns consensus from scratch with an explicit instruction not to defer to the earlier verdict, then the case is `FINAL`.

## The three review fixes (liveness + round-closing)

GenLayer review requested three lifecycle fixes:

1. **A party can block resolution forever** — only `mark_response_ready` (respondent-only) unlocked `resolve_case`, so an unresponsive respondent could stall the case indefinitely. Fixed by adding a deterministic **response window** and a **`force_resolve` path**: each round records `round_start_timestamp` when it opens (case creation or appeal), configured via the `response_timeout_seconds` constructor arg (default 7 days). Once the window passes, either party may `force_resolve(case_id)`, which runs the identical consensus round. Because GenLayer pins `datetime.now()` to the transaction timestamp, the `now >= deadline` comparison is deterministic across validators.

2. **Cumulative 3-item evidence cap blocked appeal evidence** — the old cap (`MAX_EVIDENCE_PER_PARTY = 3`) was checked against the merged, lifetime totals, so a party that hit 3 items in the initial round had no room for fresh appeal evidence. Fixed by making the cap **per round**: the allowed total is `MAX_EVIDENCE_PER_PARTY × (appeal_count + 1)`, i.e. the initial round allows 3 items per party and the appeal round grants a fresh 3-item budget per party.

3. **Marking a round ready did not freeze its evidence, and resolution ran before the other side responded** — the original ready-gate was one-sided (respondent-only) and marking ready did not freeze evidence, so either party could add evidence after the round was marked ready, resolution could run before the other side had a chance to respond, and on appeal the respondent could mark ready before the claimant added fresh evidence. Fixed with a **round-closing protocol**: the round now has two independent ready flags (`claimant_ready`, `respondent_ready`). Marking your side ready freezes **your** evidence for that round (`add_claimant_evidence` / `add_respondent_evidence` revert for a side that has closed), and `resolve_case` requires **both** flags — the round is closed only when both parties have had their defined response opportunity. `appeal_case` resets both flags, so nobody can close the appeal round on the other party before fresh evidence is submitted.

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
  claimant_ready:       bool  # claimant has closed their side of this round
  respondent_ready:     bool  # respondent has closed their side of this round
  round_start_timestamp u256  # window start of the current round (unix secs)

SubjectiveArbitrator
  cases:                  TreeMap[str, Case]  # keyed by "case-<n>"
  case_count:             u256
  response_timeout_seconds u256  # response window length (constructor arg)
```

A round is **closed** only when `claimant_ready and respondent_ready` are both
`True` — that is when the evidence snapshot is frozen and `resolve_case` is
allowed to run.

## Case lifecycle

```
open_case (by claimant)                    add_claimant_evidence
      │                                    add_respondent_evidence
      ▼                                    mark_claimant_ready (claimant)
    OPEN ───────────────────resolve_case──► VERDICT_REACHED
      ▲              └─force_resolve─────►         │
      │    (blocked until BOTH sides              appeal_case
      │     closed, OR response window                  ▼
      │     elapsed)                       └──────────────► APPEALED
      └─────── evidence reopens + both ready flags reset   │
                                                            │
                              resolve_case ── (blocked until
                                  │         BOTH sides close
                                  │         the appeal round) 
                                  │  or force_resolve after window
                                  ▼
                                 FINAL
```

`resolve_case` requires the round to be **closed** (`claimant_ready and
respondent_ready`): both parties must call `mark_claimant_ready` /
`mark_response_ready`. Closing a side freezes that side's evidence for the
round, and `_run_consensus_round` / `appeal_case` reset both flags so every
round (initial and appeal) starts from a clean, two-sided close. `force_resolve`
bypasses the closed-round gate only after the round's response deadline, so a
case can always progress. Only one appeal round exists by design — see
"Limitations".

## Public interface

- `open_case(respondent: Address, dispute_summary: str, resolution_criteria: str, claimant_evidence: list[str]) -> str` — caller becomes claimant; returns `case_id`.
- `add_respondent_evidence(case_id: str, evidence: list[str]) -> None` — respondent-only; allowed while `OPEN` or `APPEALED`; each round has its own evidence budget.
- `add_claimant_evidence(case_id: str, evidence: list[str]) -> None` — claimant-only; allowed while `OPEN` or `APPEALED` (mainly for appeal rebuttals).
- `mark_claimant_ready(case_id: str) -> None` — claimant-only; freezes the claimant's evidence for the current round and is one half of closing it.
- `mark_response_ready(case_id: str) -> None` — respondent-only; freezes the respondent's evidence for the current round and is the other half. A round closes only when both flags are set — so neither party can close the round (e.g. on appeal) before the other has responded.
- `resolve_case(case_id: str) -> None` — runs one consensus round on the frozen snapshot; reverts unless `claimant_ready and respondent_ready` are both set.
- `force_resolve(case_id: str) -> None` — **liveness path**: either party may run the same consensus round once `now >= response_deadline`, without needing either side's ready signal.
- `appeal_case(case_id: str) -> None` — claimant or respondent only, once per case; reopens evidence and resets **both** ready flags and the response window.
- `get_case(case_id: str) -> Case` — view.
- `get_case_count() -> u256` — view.
- `get_response_deadline(case_id: str) -> u256` — view: unix timestamp when the current round's response window ends.
- `get_response_timeout() -> u256` — view: configured window length in seconds.

## Testing

- `test/test_normalize_verdict.py` — pure-Python unit tests of `_validate_evidence` / `_normalize_verdict` / `_round_evidence_cap` / `_round_is_closed`. No Studio, no network: `pytest test/test_normalize_verdict.py`
- `test/test_subjective_arbitrator_direct.py` — direct-mode tests (no server) for the liveness fixes (deploy with `response_timeout_seconds=0` to exercise `force_resolve` immediately, or a huge window to prove it is deadline-gated), per-round evidence budget, access control, and the round-closing protocol (both-ready requirement, evidence freezing on ready, appeal round not closable by one side): `pytest test/test_subjective_arbitrator_direct.py`
- `test/test_subjective_arbitrator_integration.py` — full lifecycle (open → blocked resolve → evidence → both ready → resolve → appeal → blocked resolve → fresh evidence → both ready → resolve → FINAL) plus `force_resolve`, per-round evidence capacity, and ready-freezes-evidence, against a running GenLayer Studio / local validator set: `gltest test/test_subjective_arbitrator_integration.py`. When no validator network is reachable these tests auto-skip, so plain `pytest test/` still works.

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

## Design lesson: gate resolution on a round-closing protocol that is also time-bounded

The first submitted version let anyone call `resolve_case` immediately after `open_case`, and only allowed respondent evidence while the case was `OPEN` — so an eager caller could resolve a case (and, later, its appeal too, since evidence was frozen after the first round) using only the claimant's side of the story. The second version added a single `respondent_ready` gate and reopened both parties' evidence during `APPEALED`. Review then pointed out that a pure ready-gate lets an unresponsive respondent block resolution forever, a cumulative evidence cap can lock a party out of fresh appeal evidence, and — critically — that a one-sided ready-gate didn't *freeze* evidence: a party could mark ready then still add evidence, and resolution could run before the other side responded (on appeal, the respondent could mark ready before the claimant submitted fresh appeal evidence). The fixes: a deterministic window + `force_resolve` fallback, per-round evidence budgets, and a **two-sided round-closing protocol** where marking your side ready freezes your evidence and the round closes only when both parties have marked ready (with `appeal_case` resetting both flags). General takeaway: in a primitive with two interested parties, "who can trigger the non-deterministic step", "whose evidence is in scope when it runs", and "what happens if a party never cooperates" need to be designed together — a gate without a liveness fallback, a cap without a per-round budget, or a one-sided ready flag without a snapshot freeze, each leaves part of the same class of bug behind.

## Deployed instance (GenLayer Studio)

> **Note:** the contract changed (new `claimant_ready` field, new `mark_claimant_ready` method, round-closing protocol that freezes evidence on ready and requires both sides to close the round before resolving). The instance below includes all these changes plus the earlier liveness fixes (`force_resolve` / `get_response_deadline` / `get_response_timeout` / per-round evidence cap).

Deployed with `response_timeout_seconds = 604800` (7-day default window) — this is the production configuration. The live write-method checks were re-run against **this** address:
- `python test/studio_verify_live.py` — round-closing protocol, positive path through `FINAL` (case-0: initial round both-ready → resolve → `VERDICT_REACHED` → appeal → both flags reset → fresh appeal evidence → respondent alone cannot close the appeal round (resolve reverts) → both-ready → `FINAL`).
- `python test/studio_verify_gates.py` — evidence cap (4th item reverts), access control (claimant/respondent cannot call each other's methods), `force_resolve` before the 7-day deadline reverts.

Every gate reverts as designed and the full positive path was verified on-chain. Note that a consensus round can settle `UNDETERMINED` when validators don't reach quorum — that is by design (state is unchanged, `resolve_case` is safely re-callable) and is not a contract fault.

| Network | Address | Explorer |
|---------|---------|----------|
| Studio | `0xb1e420C33b60D57e0C211a8F58B9A3dDD6b88047` | [Explorer](https://explorer-studio.genlayer.com/address/0xb1e420C33b60D57e0C211a8F58B9A3dDD6b88047) |

## Dependency pin

The contract pins `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6` in its `Depends` header. Update this hash if you're targeting a different SDK version.
