# claude_long_context_pricing.md

Research note: does the Claude API charge a premium for requests over the
long-context (1M token) threshold, and does `claude_turn_usage.py`'s `PRICES`
table need a `[1m]`-specific entry to bill it correctly?

**Short answer: no premium exists today (2026-07-03).** Every model that
supports the 1M-token context window bills the *entire* request — including
the portion past 200K input tokens — at the model's standard per-token rate.
`claude_turn_usage.py` already strips the `[1m]` suffix and looks up base
rates (`_price()` in `claude_turn_usage.py`), which is the economically
correct thing to do; no change is needed there.

## What `[1m]` means in a Claude Code transcript

Claude Code tags a request with a `[1m]` model-id suffix (e.g.
`claude-fable-5[1m]`, `claude-sonnet-4-6[1m]`) when the session is using the
1-million-token context window instead of the model's default/200K window.
This is a **client-side selection marker only**:

> "Claude Code strips the suffix before sending the model ID to your
> provider."
> — [Claude Code docs, Model configuration](https://code.claude.com/docs/en/model-config)

So the wire-level `model` field the Anthropic API actually receives is the
bare model id (`claude-fable-5`), never the bracketed form. The bracketed
form only ever shows up in Claude Code's own transcript JSONL (which is what
`claude_turn_usage.py` parses), not in anything the API bills against.

## The threshold rule (as documented today)

> "The 1M context window uses standard model pricing with no premium for
> tokens beyond 200K."
> — [Claude Code docs, Model configuration § Extended context](https://code.claude.com/docs/en/model-config)

> "Claude Fable 5, Claude Mythos 5, Claude Mythos Preview, Claude Opus 4.8,
> Opus 4.7, Opus 4.6, Sonnet 5, and Sonnet 4.6 include the full 1M token
> context window at standard pricing. (A 900k-token request is billed at the
> same per-token rate as a 9k-token request.) Prompt caching and batch
> processing discounts apply at standard rates across the full context
> window."
> — [Claude Platform docs, Pricing § Long context pricing](https://platform.claude.com/docs/en/about-claude/pricing#long-context-pricing)

> "For every model with a 1M-token context window, 1M is the default: you
> don't need a beta header, and long-context requests are billed at standard
> pricing."
> — [Claude Platform docs, Context windows](https://platform.claude.com/docs/en/build-with-claude/context-windows)

In other words: there is no ">200K tokens" billing branch left to detect.
Whether the whole request is billed at a premium, or only the tokens past
threshold, or cache-read/cache-creation tokens count toward the threshold —
none of that matters, because **the premium rate no longer exists** for any
currently-documented model. This was a real thing in the past (see
"History" below) and was removed.

## Base == long-context rate (all models, per-MTok, USD)

Every model below carries the 1M context window at **standard** pricing —
the "long context" column is identical to "base" (1.00× multiplier) as of
2026-07-03.

| Model | in | out | read (cache hit) | w5m (5m cache write) | w1h (1h cache write) | Long-context multiplier |
|---|---:|---:|---:|---:|---:|---:|
| `claude-fable-5` | $10.00 | $50.00 | $1.00 | $12.50 | $20.00 | 1.00× (no premium) |
| `claude-mythos-5` | $10.00 | $50.00 | $1.00 | $12.50 | $20.00 | 1.00× (no premium) |
| `claude-opus-4-8` | $5.00 | $25.00 | $0.50 | $6.25 | $10.00 | 1.00× (no premium) |
| `claude-opus-4-7` | $5.00 | $25.00 | $0.50 | $6.25 | $10.00 | 1.00× (no premium) |
| `claude-opus-4-6` | $5.00 | $25.00 | $0.50 | $6.25 | $10.00 | 1.00× (no premium) |
| `claude-sonnet-5` (through 2026-08-31) | $2.00 | $10.00 | $0.20 | $2.50 | $4.00 | 1.00× (no premium) |
| `claude-sonnet-5` (from 2026-09-01) | $3.00 | $15.00 | $0.30 | $3.75 | $6.00 | 1.00× (no premium) |
| `claude-sonnet-4-6` | $3.00 | $15.00 | $0.30 | $3.75 | $6.00 | 1.00× (no premium) |

Notes:
- `claude-sonnet-5` has time-boxed introductory pricing ($2/$10 per MTok)
  through 2026-08-31, reverting to $3/$15 on 2026-09-01. `PRICES` in
  `claude_turn_usage.py` currently hard-codes the **post-introductory**
  $3/$15 row — accurate from 2026-09-01 onward, an overestimate today.
- `claude-haiku-4-5` (200K context only, no 1M variant) is not part of this
  table — it does not support the 1M window at all, so `[1m]` never applies
  to it.
- Cache write/read multipliers (1.25× base for 5m write, 2× base for 1h
  write, 0.1× base for a cache hit) are unchanged above the 200K mark — see
  the "Prompt caching" section of the Pricing page, which states these
  multipliers "apply at standard rates across the full context window."
- Not documented as of 2026-07-03: any distinct long-context rate for
  `claude-haiku-4-5` (it isn't 1M-capable) or for any model not listed above.
  If a `[1m]`-tagged model id ever appears for a model outside this table,
  treat its rate as undocumented rather than assuming it inherits the base
  row.

## History: this premium used to exist

For context on why the `[1m]` suffix and this whole question exist: the
*original* 1M-context beta (Claude Sonnet 4, mid-2025) did charge a real
premium above 200K input tokens:

| | ≤ 200K tokens | > 200K tokens |
|---|---:|---:|
| Input | $3 / MTok | $6 / MTok (2×) |
| Output | $15 / MTok | $22.50 / MTok (1.5×) |

— [Claude Sonnet 4 now supports 1M tokens of context](https://claude.com/blog/1m-context)

That threshold-based premium was removed when 1M context went GA for Opus
4.6 and Sonnet 4.6 on 2026-03-13:

> "Standard pricing applies across the full window — $5/$25 per million
> tokens for Opus 4.6 and $3/$15 for Sonnet 4.6 ... No beta header required.
> Requests over 200K tokens work automatically."
> — [1M context is now generally available for Opus 4.6 and Sonnet 4.6](https://claude.com/blog/1m-context-ga)

Every model that has shipped a 1M context window since (Opus 4.7, Opus 4.8,
Sonnet 5, Fable 5, Mythos 5) launched with this GA no-premium pricing from
day one.

## Source

Fetched 2026-07-03:

- [Claude Platform docs — Pricing](https://platform.claude.com/docs/en/about-claude/pricing) (Model pricing table, Prompt caching, § Long context pricing)
- [Claude Platform docs — Context windows](https://platform.claude.com/docs/en/build-with-claude/context-windows) (§ Context window sizes by model)
- [Claude Platform docs — Models overview](https://platform.claude.com/docs/en/about-claude/models/overview) (latest-models-comparison table, context window sizes)
- [Claude Code docs — Model configuration](https://code.claude.com/docs/en/model-config) (§ Extended context — `[1m]` suffix semantics, suffix stripped client-side, no premium statement)
- [Anthropic blog — Claude Sonnet 4 now supports 1M tokens of context](https://claude.com/blog/1m-context) (historical premium pricing, now superseded)
- [Anthropic blog — 1M context is now generally available for Opus 4.6 and Sonnet 4.6](https://claude.com/blog/1m-context-ga) (announcement removing the premium, 2026-03-13)

## Applying to claude_turn_usage.py

**No `PRICES` change is needed for the long-context premium** — it doesn't
exist for any model the script prices. The existing `_price()` behavior
(strip the `[1m]`/variant suffix, look up the base row) already produces the
correct cost, because the base row *is* the long-context rate. Adding a
separate `"claude-fable-5[1m]"` entry with different numbers would be wrong;
it would need to equal the base row exactly, which is redundant with what
`_price()` already does via suffix-stripping.

Two things worth fixing independently, unrelated to the long-context
question:

1. `PRICES["claude-sonnet-5"]` is hard-coded to the **post-2026-08-31**
   $3/$15 row. Until 2026-09-01 it should be $2.00/$0.20/$2.50/$4.00/$10.00
   (in/read/w5m/w1h/out) per the introductory-pricing note on the Pricing
   page. This is a real (small) overestimate for any `claude-sonnet-5`
   turns costed before that date — worth a `DATE`-gated branch or at least a
   `# TODO: switches to $3/$15 on 2026-09-01` comment if not fixed now.
2. If Anthropic ever reintroduces a long-context premium (as they did once
   before, for Sonnet 4 in 2025, then removed in March 2026), the transcript
   usage `claude_turn_usage.py` reads (`input_tokens`,
   `cache_read_input_tokens`, `cache_creation.ephemeral_{5m,1h}_input_tokens`)
   would need a per-request *total prompt size* check against whatever
   threshold is reintroduced, since Claude Code's JSONL doesn't carry a
   separate "was this request billed at the premium tier" flag — the `[1m]`
   suffix only indicates *context window selected*, not *tokens actually
   sent this request*. A future implementation would need to sum
   `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`
   (all three "count toward the context window" per the docs above) and
   compare against the reintroduced threshold per assistant message, not
   per turn, since a single turn can span multiple API calls at different
   sizes.
