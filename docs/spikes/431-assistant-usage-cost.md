# Spike: what does `AssistantUsageData.cost` denote? (issue #431)

Status: **UNCONFIRMED — the field stays unread.**
Desk research done 2026-08-27 against the pinned SDK sources installed in
this environment: **github-copilot-sdk 1.0.11**
(`github_copilot_sdk-1.0.11.dist-info/METADATA`, "Python SDK for GitHub
Copilot CLI"), read at
`/home/agent/.sbxloop/venv/lib/python3.14/site-packages/copilot/`.

## Why

Run `rrhb28j7n` emitted `cost = 15.0` on *every* turn of *every* session,
independent of token counts. A constant is not a per-turn delta, so summing
it fabricates a figure, and it does not have the shape of a currency amount.
The backend therefore does not read the field (see `usage_from_sdk_sample`
in `packages/sbxloop-worker/src/sbxloop_worker/backends/copilot.py`), and
`run_usage` reports "spend: not reported by the agent backend". This spike
asks whether the SDK itself establishes the unit well enough to change that.

## What was examined

1. `copilot/generated/session_events.py` — the declaration of
   `AssistantUsageData` and every sibling field on it.
2. `copilot/generated/rpc.py` — every other declaration in the SDK whose
   name or docstring contains "cost", to see whether a *sibling* type
   documents a unit that `AssistantUsageData.cost` might share.
3. The package directory for bundled schema or documentation files: the
   distribution ships only `.py` sources, `py.typed`, and dist-info
   metadata. There is **no** JSON schema, `.d.ts`, or prose doc in the
   wheel that describes the field.
4. The field-observation evidence already recorded in this repo:
   `docs/architecture.md` § "What a run costs" and
   `tests/unit/test_usage_spend.py` (constant 15.0 across run `rrhb28j7n`).

## The SDK's declaration of the field

`copilot/generated/session_events.py`, lines 1820–1834 (verbatim):

```text
@dataclass
class AssistantUsageData:
    "LLM API call usage metrics including tokens, costs, quotas, and billing information"
    model: str
    ...
    copilot_usage: AssistantUsageCopilotUsage | None = None
    # Experimental: this field is part of an experimental API and may change or be removed.
    cost: float | None = None
```

and its decoder, same file:

```python
cost = from_union([from_none, from_float], obj.get("cost"))
```

That is the **entire** documentation of the field in the SDK: a bare
`float | None`, decoded from the wire key `"cost"`, carrying an
"Experimental" marker and **no** docstring, no unit, no currency code, no
scale. The type-level docstring says only "tokens, costs, quotas, and
billing information", which describes the dataclass as a whole and
distinguishes none of those four categories per field.

## Neighbouring declarations (circumstantial, not dispositive)

These do carry docstrings, and they are informative about the *vocabulary*
the SDK uses — but none of them is the field in question:

- `AssistantUsageCopilotUsage` (`session_events.py`), the sibling field
  `copilot_usage` on the very same dataclass:
  `"Per-request cost and usage data from the CAPI copilot_usage response field"`,
  whose one required member is `total_nano_aiu: float`. So the SDK's
  per-request *consumption* figure on this dataclass is expressed in
  **nano-AI units**, and it is a different field from `cost`.
- `UsageMetricsModelMetricRequests.cost` (`rpc.py:12106`):
  `"""User-initiated premium request cost (with multiplier applied)"""` —
  i.e. in that type, `cost` means **premium requests**, not money.
- `ModelBilling.multiplier` (`rpc.py:23303`):
  `"""Billing cost multiplier relative to the base rate"""`.
- `UsageGetMetricsResult.total_premium_request_cost` (`rpc.py:24745`):
  `"""Total user-initiated premium request cost across all models (may be fractional due to ..."""`.
- `ModelBillingTokenPrices` (`rpc.py:5510`ff): token prices are documented
  in **"AI Credits cost per billing batch"**, again not a currency.
- `ShutdownModelMetricRequests.cost` (`session_events.py:7624`): another
  bare, undocumented, "Experimental" `float | None` under the docstring
  `"Request count and cost metrics"`.

The consistent pattern is that this SDK uses the word "cost" for
**premium-request counts, multipliers, and AI-credit/nano-AIU units** —
never for a money amount, and no declaration anywhere in the package names a
currency for any field. That is suggestive, and it is consistent with the
observed constant 15.0 looking like a multiplier or quota unit, but it is
*inference by analogy across types*, not a statement about
`AssistantUsageData.cost` itself.

## Conclusion

**UNCONFIRMED.** No citation in github-copilot-sdk 1.0.11 states the unit of
`AssistantUsageData.cost`. The field is declared as an undocumented,
experimental `float | None` and nothing shipped in the package — sources,
type stubs, or metadata — says what it counts. No billing data was available
in this environment to cross-check against, and none is invented here.

The one thing that *is* established, by field observation rather than by
documentation, is what the value is **not**: run `rrhb28j7n` reports the
identical constant on every turn regardless of token counts, so it is not a
per-turn delta and it must never be summed.

Consequences, which hold until a citation says otherwise:

- The field **stays unread**. `usage_from_sdk_sample` must continue not to
  read it, and the wire `Usage` model must continue to carry no spend/cost
  field.
- `run_usage` must keep reporting
  **"spend: not reported by the agent backend"**.

## Conditions for any future surfacing

If a later SDK release, API document, or billing reconciliation establishes
the unit, both of these are mandatory before the value is shown:

1. **Carried non-additively through `Usage.merged`** — last/max wins,
   exactly as `model` already is (`model=other.model or self.model`), never
   summed alongside the token counters. A constant-per-turn value summed
   across turns is a fabricated number.
2. **Never rendered in a currency shape** by the spend/cost line
   (`_cost_line`) — no currency symbol, no currency code, no
   two-decimal-money formatting — unless and until the established unit
   actually *is* a currency. If the unit turns out to be premium requests,
   AI credits, or nano-AIU, it must be labelled with that unit.
