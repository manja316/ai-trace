# ai-trace

[![PyPI version](https://badge.fury.io/py/ai-decision-tracer.svg)](https://pypi.org/project/ai-decision-tracer/)
[![Downloads](https://img.shields.io/pypi/dm/ai-decision-tracer)](https://pypi.org/project/ai-decision-tracer/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)

**Zero-dependency local AI agent decision tracer.**

Records every step an AI agent takes — what it saw, what it decided, and why. JSON + Markdown output. No network calls. No cloud. Entirely local.

Part of the **AI Agent Infrastructure Stack**:
- [ai-cost-guard](https://github.com/LuciferForge/ai-cost-guard) — hard budget caps before the LLM call
- [ai-injection-guard](https://github.com/LuciferForge/prompt-shield) — prompt injection scanner
- **ai-trace** — local decision tracer ← you are here

---

## Install

```bash
pip install ai-decision-tracer
```

No dependencies. Pure Python stdlib.

---

## Quickstart

```python
from ai_trace import Tracer

tracer = Tracer("trading_agent", meta={"model": "claude-haiku-4-5"})

with tracer.step("market_scan", symbol="BTCUSDT") as step:
    signal = analyze(market_data)
    step.log(signal=signal, confidence=0.87)

with tracer.step("decision", signal=signal) as step:
    action = decide(signal)
    step.log(action=action, reason="SuperTrend bullish + volume spike")

# Save full trace
tracer.save()             # → traces/trading_agent_20240301_143022.json
tracer.save_markdown()    # → traces/trading_agent_20240301_143022.md
```

---

## Why this exists

My trading bot made a bad trade at 3AM. Lost money. I had logs — thousands of lines of `print()` spam — but I couldn't answer the basic question: **what did the agent see at the moment it decided to enter that position?**

Was it a bad signal? A stale data feed? A prompt injection that slipped past the scanner? Without a structured decision trace, postmortems are guesswork.

`ai-trace` records every step an AI agent takes — what it saw, what it decided, why, and how long it took. JSONL auto-save (survives crashes), Markdown reports (human-readable), and a CLI for quick inspection. No cloud. No external service. Everything stays local.

---

## Features

| Feature | Details |
|---|---|
| **Zero dependencies** | Pure Python 3.8+ stdlib |
| **Context manager** | `with tracer.step("name", **ctx) as step:` |
| **Auto-save** | Appends each step to JSONL as it completes |
| **Atomic writes** | JSON/Markdown via temp file + rename — no partial output |
| **CLI viewer** | `ai-trace view`, `ai-trace tail`, `ai-trace stats` |
| **Error capture** | Full traceback captured on exception, step marked as error |
| **Metadata** | Attach model name, version, run ID to the session |

---

## API

### `Tracer`

```python
Tracer(
    agent: str,           # agent name — used in filenames
    trace_dir: str,       # where to write files (default: "traces")
    auto_save: bool,      # append to JSONL after each step (default: True)
    meta: dict,           # session-level metadata (model, version, etc)
)
```

### `step(name, **context)`

```python
with tracer.step("classify", input_text=text[:50]) as step:
    result = model.classify(text)
    step.log(label=result.label, confidence=result.score)
```

Or manually:
```python
step = tracer.step("scan")
step.start()
step.log(markets_scanned=142)
step.finish()  # or step.fail(reason="timeout")
```

### Save

```python
tracer.save()           # → JSON (all steps + metadata)
tracer.save_markdown()  # → human-readable Markdown summary
tracer.summary()        # → dict: steps, ok, errors, avg_duration_ms
```

---

## CLI

```bash
# List all trace sessions
ai-trace list

# View a specific session
ai-trace view trading_agent_20240301_143022.jsonl

# Live tail the latest trace
ai-trace tail -n 20

# Stats across all sessions
ai-trace stats
```

Custom directory:
```bash
ai-trace --dir /var/log/agent/traces list
```

---

## Output formats

### JSONL (auto-saved, one line per step)
```json
{"name": "market_scan", "context": {"symbol": "BTCUSDT"}, "outcome": "ok", "duration_ms": 142.3, "logs": [{"_t": 1709300422.1, "signal": 0.87}]}
```

### JSON (full session snapshot)
```json
{
  "agent": "trading_agent",
  "session_id": "20240301_143022",
  "meta": {"model": "claude-haiku-4-5"},
  "steps": [...]
}
```

### Markdown (human-readable)
```markdown
# Trace: trading_agent — 20240301_143022

## Summary
| Steps | OK | Errors | Avg duration |
|---|---|---|---|
| 4 | 3 | 1 | 127.4 ms |

## Steps

### 1. ✅ `market_scan` (142.3 ms)
**Context:**
- `symbol`: 'BTCUSDT'

**Logs:**
- `14:30:22.100Z` — `signal=0.87`
```

---

## Real-World Usage: Polymarket Oracle

[polymarket-oracle](https://github.com/LuciferForge/polymarket-oracle) is an autonomous trading bot that uses ai-trace to sign every trade decision with Ed25519 receipts. Real money, real trades, cryptographically provable track record.

```python
from ai_trace import Tracer

oracle = Tracer("polymarket-oracle", sign=True, meta={"version": "4.1"})

# Every trade gets a signed receipt
with oracle.step("trade_executed", market="btc-updown-15m-1773050400") as step:
    step.log(strategy="MOMENTUM", direction="DOWN", entry_price=0.82,
             shares=25, win_rate_est=93.0, ev=2.15)

# Every resolution closes the loop
with oracle.step("trade_resolved", market="btc-updown-15m-1773050400") as step:
    step.log(result="WIN", pnl=4.50, price_open=66200.0, price_close=65900.0)

# Verify the entire chain
oracle.save_receipts()
# → Anyone can verify independently with the public key
```

---

## Signed Receipts (v0.2.0)

Enable Ed25519-signed, hash-chained receipts for tamper-proof audit trails:

```python
tracer = Tracer("agent", sign=True)

with tracer.step("decision") as step:
    step.log(action="approve", reason="policy compliant")

# Save and verify
tracer.save_receipts()
print(tracer.public_key)  # Share this — anyone can verify

# Third-party verification (no private key needed)
from ai_trace import ReceiptBuilder
meta, receipts = ReceiptBuilder.load_receipts("receipts/agent_receipts.json")
result = ReceiptBuilder.verify_chain_from_list(receipts)
assert result["valid"]  # Chain intact, all signatures valid
```

Each receipt contains: step name, context, logs, timestamp, content hash (SHA-256), Ed25519 signature, and a `previous_hash` linking to the prior receipt. Delete or modify any receipt and the chain breaks.

---

## Use with other stack libraries

```python
from ai_cost_guard import CostGuard
from ai_injection_guard import PromptScanner
from ai_trace import Tracer

guard   = CostGuard(weekly_budget_usd=5.00)
scanner = PromptScanner(threshold="MEDIUM")
tracer  = Tracer("agent", meta={"model": "claude-haiku-4-5"})

@guard.protect(model="anthropic/claude-haiku-4-5-20251001")
@scanner.protect(arg_name="prompt")
def call_llm(prompt):
    with tracer.step("llm_call", prompt_len=len(prompt)) as step:
        response = client.messages.create(...)
        step.log(tokens=response.usage.input_tokens)
    return response
```

---

## License

MIT
