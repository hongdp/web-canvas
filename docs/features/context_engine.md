# Context Engine — one module, per-provider strategies

Status: **proposed**. Consolidates work already landed in
[cache_first_context.md](cache_first_context.md) and supersedes its §7 as the
place where selection, budgeting and layout live.

## 1. Why consolidate

The logic that decides what the model sees is spread across eight files and
three layers, and each turn's prompt is the product of all of them:

| File | Decides |
|---|---|
| `utils/contextSelection.ts` | which chapters are relevant |
| `utils/contextLedger.ts` | what was already sent, and in what order |
| `utils/contextWindow.ts` | how many tokens are available |
| `hooks/chat/dynamicContext.ts` | how blocks are rendered |
| `hooks/chat/wholeBook.ts` | the whole-book escalation ladder |
| `utils/llmContext.ts` | history trimming, artifact stripping |
| `utils/systemPrompt.ts` | system-prompt layering |
| `hooks/useChatLLM.ts` | ~100 lines of inline assembly tying it together |

Nothing here is wrong in isolation. What is missing is a place where the
*whole* decision is made and can be inspected, and — the reason this document
exists — anywhere at all that knows **which provider the prompt is for**.

Providers do not merely differ in cache syntax. Two hazards found while
measuring this app, neither addressable by prompt layout:

1. **grok has a cost cliff that counts cached tokens.** Long-context pricing
   applies when *total* prompt tokens **including cached ones** exceed the
   model's threshold (200K for the grok-4 family), and it doubles input,
   cached and output rates together. An append-only ledger grows monotonically,
   so it will eventually cross that line — and because the cache hit rate stays
   high, nothing looks wrong while the bill doubles.
2. ~~**The local endpoint has exactly one KV slot.**~~ **Resolved 2026-08-17
   by deleting the summarizer.** `llama-server` runs with `--parallel 1`, so
   its prefix cache is a single shared resource, and the background chapter
   summarizer sent its own requests to that same slot — evicting the
   conversation's KV and making the next chat turn pay a full re-prefill with
   no visible cause. Summary *generation* is gone (§3, ollama profile), so
   nothing competes for the slot any more. The constraint itself remains, and
   any future background work against a local endpoint must respect it.

A per-provider strategy is not polish. It is the difference between a design
that works on paper and one that works against a real endpoint.

## 2. Shape

```
                    ┌─────────────────────────────┐
  chat turn  ──────▶│      ContextEngine.plan()   │──────▶  PromptPlan
                    └──────────────┬──────────────┘
                                   │ consults
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
       ProviderProfile        ContextLedger        SelectionScorer
    (caching, limits,        (what was sent,      (which chapters
     cost cliffs, telemetry)  in what order)       are relevant)
```

`plan()` is **pure**: state in, plan out, no I/O, no store reads. That is what
makes the acceptance test possible — plan twice, compare prefixes — and it is
how the current code is already tested.

```ts
interface PromptPlan {
  blocks: PromptBlock[]          // ordered; each knows its volatility
  breakpoints: number[]          // block indices to mark, if the provider marks
  budget: BudgetAccounting       // where every token went
  diagnostics: PlanDiagnostics   // what changed vs the last plan, and why
  consent?: ConsentRequest       // when the user must decide before sending
}

interface PromptBlock {
  kind: 'system' | 'ledger' | 'compaction' | 'history' | 'tail'
  volatility: 'session' | 'append-only' | 'per-turn'
  messages: LLMMessage[]
  tokens: number
}
```

`diagnostics` is the part that does not exist today and should: which block
first differed from the previous turn, and why. Every cache regression in this
app so far has been invisible — including a bug where an unchanged chapter set
was re-emitted in a new order every turn.

## 3. Provider profiles

```ts
interface ProviderCacheProfile {
  /** 'automatic' = exact-prefix matching; 'explicit' = we must mark breakpoints. */
  mode: 'automatic' | 'explicit' | 'none'
  maxBreakpoints: number
  /** Header/field that routes a conversation to the same cache. */
  routingKey?: { kind: 'header'; name: string } | { kind: 'body'; field: string }
  /** Above this many prompt tokens the price tier changes. */
  longContextThreshold?: number
  /** Where the response reports cache hits. */
  telemetry?: { path: string[] }
  /** True when the cache is a single shared slot that other requests evict. */
  exclusiveCache?: boolean
  /** Where the window comes from. */
  window: { discovered: boolean; fallbackTokens: number }
}
```

### grok (xAI) — primary

| | |
|---|---|
| mode | `automatic` — exact-prefix; `cache_control` markers do not exist and `cacheHint` is a no-op |
| routing | `x-grok-conv-id` header (already sent, keyed on `activeBookId`) |
| telemetry | `usage.prompt_tokens_details.cached_tokens` (already parsed) |
| cost cliff | **long-context threshold; cached tokens count toward it** |
| window | table only — xAI reports no window; `/language-models` returns pricing and modalities |

Strategy:

- **Budget against the cliff first, the window second.** The target is the
  largest prompt that stays *under* the threshold; the window is a hard cap
  above it. Crossing is allowed but must be a decision, with the doubled rate
  quoted — the same consent panel the ledger already uses.
- Ledger admission gets a second gate: a chapter that would push the total over
  the threshold is refused (or evicts the coldest ledger member, with consent),
  even when the window has room.
- Nothing to mark: the layout *is* the optimization.

### ollama / llama.cpp (local) — primary

| | |
|---|---|
| mode | `automatic` — llama.cpp reuses the longest matching prefix in the slot's KV |
| routing | none |
| telemetry | none in the response; measured as prefill time (`prompt processing … t = …`) and the backend's own first-token log |
| window | **discovered** — `meta.n_ctx` on `/v1/models`, already wired |
| exclusiveCache | **true** — `--parallel 1` is one slot |

Strategy:

- **Cache arbitration.** With one slot, the prefix belongs to whoever spoke
  last. There is no background work left to arbitrate — the summarizer, the
  only other producer of requests, was deleted — but the rule stands for
  anything added later: hold it while the conversation is warm, route it to a
  different endpoint (a separate Ollama daemon, on `:11434` by default, has
  its own cache), or accept the eviction knowingly.
- Free money the other providers do not have: **the window is enormous**
  (262144) and costs nothing per token. The budget should fill it rather than
  trim, which is what the model-derived budget now does.
- Because there is no cached-token field in the OpenAI-compatible usage block,
  the honest signals are `timings.cache_n` (tokens served from cache) and
  time-to-first-token. Both are now recorded per turn.
- **Reuse here is partial, and that is architectural.** Measured on
  Qwen3.8-Flash-Next (hybrid: Gated DeltaNet + sparse attention, `ssm.*` in the
  GGUF), live against the endpoint, one scenario per row:

  | scenario | cached / total | prefill |
  |---|---|---|
  | cold | 0 / 4558 | 33.3s |
  | second turn, history appended | **3690 / 4582** | 7.8s (**4.3×**) |
  | the identical prompt again | 4522 / 4526 | 0.3s |
  | a chapter appended inside the ledger | 1510 / 4556 | 22.7s |
  | the same chapters reordered | 1510 / 4556 | 22.7s |

  A recurrent state cannot be rewound to an arbitrary token, so a shared prefix
  is not always fully reusable. `--swa-full`, denser context checkpoints
  (`-cms 512 -ctxcp 64`) and splitting the ledger into one message pair per
  chapter were each measured and changed nothing — do not retry them.

  The consequence for this design: the layout still pays here (4.3× on the
  common case of continuing a conversation), but the full win belongs to plain
  transformers. grok caches an exact prefix automatically, so the same layout
  should reuse everything there — which is what the new per-turn telemetry
  exists to confirm.

### anthropic / openai / gemini — secondary

Sketched now so the interface is not shaped by two providers alone:

- **anthropic**: `explicit`, up to 4 breakpoints, 5-minute TTL. As built
  (`wc_providers/anthropic.py`): the system block, the **end of the request**,
  and the two latest `cacheHint` messages (of ledger │ summary │ history).
  The end matters most: an agent step re-sends the turn so far, and without a
  breakpoint there every step paid the full input price for it (2026-10-14:
  cache reads stayed flat from step to step while the prompt kept growing;
  over a long run the input cost about three times what it would have with
  each step's tail cached). Each step's breakpoint is read by the next through
  the API's lookback (up to 20 blocks back). Worth a keepalive ping (aider's `--cache-keepalive-pings`)
  if it ever becomes the main provider.
- **openai**: automatic prefix caching, `prompt_cache_key` for routing, a
  minimum cacheable prefix.
- **gemini**: implicit caching plus explicit cached-content handles with a TTL —
  the only provider where the ledger could be *uploaded once* and referenced,
  which the interface should not preclude.

## 4. What moves

| From | To |
|---|---|
| assembly inline in `useChatLLM.ts` | `context/engine.ts` — `plan()` |
| `utils/contextSelection.ts` | `context/selection.ts` (unchanged logic) |
| `utils/contextLedger.ts` | `context/ledger.ts` (unchanged logic) |
| `utils/contextWindow.ts` | `context/window.ts` + `context/profiles.ts` |
| `hooks/chat/dynamicContext.ts` | `context/render.ts` |
| history trimming in `utils/llmContext.ts` | `context/history.ts` |
| `hooks/chat/wholeBook.ts` | stays; becomes a plan *variant* the engine emits |

`useChatLLM` keeps the streaming, the retries and the store writes. It stops
deciding what the prompt contains.

## 5. Telemetry, because none of this is visible today

`cachedPromptTokens` is parsed on both transports and accumulated into session
stats — and rendered nowhere. Every cache regression so far has been silent.

One record per turn, written wherever the turn ran:

```
provider, model, promptTokens, cachedTokens, hitRate,
firstTokenMs, planDiff (which block first differed), overThreshold
```

For grok that is a real hit rate. For the local endpoint `cachedTokens` is
absent and `firstTokenMs` carries the signal. Both belong in `api-server.log`
next to the existing job lines, and the hit rate belongs in the UI — it is the
only way to notice that a change to the prompt layout undid the work.

## 6. Phasing

1. **Profiles + telemetry.** No behavior change: introduce
   `ProviderCacheProfile`, surface hit rate and first-token time. This makes
   every later step measurable instead of arguable.
2. **Extract `plan()`.** Move assembly out of `useChatLLM` with the current
   behavior intact; the existing prefix test guards it.
3. **grok threshold budgeting.** Admission gate + consent when crossing.
4. **Local cache arbitration.** Hold or reroute background summaries while a
   conversation is warm.
5. Compaction and the immutable prompt log (see cache_first_context.md §11
   "Still not done") land inside the engine rather than beside it.

## 7. Open questions

1. **Is the grok long-context threshold really 200K for `grok-4.20`?** The
   table entry is inferred from the grok-4 family. xAI does not report windows,
   so this needs confirming against the account's own pricing page.
2. **Should summaries move to a second local server permanently?** It may
   serve a different model family, which changes summary quality. Cheaper
   than evicting a 262144-token prefix, but not free.
3. **Does the engine own roleplay too?** `useRoleplayLLM` has its own volatile
   game-state block and the same provider constraints. Probably yes, after (2).
