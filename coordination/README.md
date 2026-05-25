# Coordination — общая память между Claude и Codex

Здесь живут артефакты, которые **оба AI** (Claude Code и Codex CLI) читают и пишут,
чтобы не терять контекст между сессиями и не противоречить друг другу при
управлении торговой системой Evgeny (CryptoБур + Vibe-Trading).

Паттерн взят из `langchain-ai/langgraph` (durable shared state) и
`microsoft/autogen` (role-based group chat).

## Файлы

- **`strategy_state.yaml`** — append-only журнал торговых решений и наблюдений.
  Каждая сессия начинается с чтения файла целиком (это часть briefing'а
  любому AI), и заканчивается append'ом одной записи.
- **`open_questions.md`** — список нерешённых вопросов, требующих внимания
  founder'а. AI сюда пишет вопросы, founder отвечает в чате, ответы
  переносятся в strategy_state.yaml.
- **`roles.md`** — кто какую роль занимает (Researcher/Skeptic/Risk Gate/
  Executor) и правила handoff между Claude и Codex.

## Контракт записи в strategy_state.yaml

```yaml
- timestamp: 2026-05-25T04:30:00Z        # ISO 8601 UTC
  author: claude                          # claude | codex | founder
  session_id: optional-uuid               # для трейсинга
  market_state:                           # snapshot ground truth
    binance_equity_usd: 94.78
    alpaca_equity_usd: 101295
    last_cron_tick_age_min: 6
    active_codex_tunnel: true
    notes: "10 подряд HOLD, MIN_CONFIDENCE 0.65 = potential bottleneck"
  proposal:                               # что предлагаешь сделать
    action: "lower MIN_CONFIDENCE Binance.US to 0.60"
    rationale: "post-premortem scores clustered 0.55-0.60, never trigger 0.65 gate"
    impact: "expected +2-4 BUY orders per day on live $94 account"
    risk: "low — capped by MAX_POSITION_PCT=1% + STOP_LOSS=5%"
  risk_objection:                         # если другой AI возражает — сюда
    by: codex                             # null если возражений нет
    text: "premortem ×0.60 is already conservative; lowering gate without
           tightening volatility filter could let high-ATR trash through"
    resolution: "agreed — also raise vol penalty threshold from -0.10 to -0.07"
  decision:                               # что в итоге сделано (или skipped)
    status: applied                       # proposed | applied | rejected | deferred
    by_whom: founder                      # who clicked the trigger
    timestamp: 2026-05-25T04:35:00Z
    evidence_link: "trade_cycle_events row 28442"
  next_check:                             # когда проверить эффект
    when: 2026-05-26T04:30:00Z
    what: "count new BUY orders, compare to baseline 0/24h"
    owner: claude                         # who pulls the metric
```

## Контракт ролей

| Роль | Owner | Что делает | Что НЕ делает |
|---|---|---|---|
| **Researcher** | claude | scan MCP for ground truth, write analysis | live trading decisions |
| **Skeptic** | codex | adversarial review, find bugs/risks | autonomous code changes |
| **Risk Gate** | claude+codex | both must agree before any live config write | bypass without sign-off |
| **Executor** | founder | clicks final approval, owns money | nothing — only authority |

## Когда AI обязан писать в strategy_state.yaml

1. **Перед любым live config change** (risk param, agent routing, secret)
2. **После любого discovery** (нашёл багу, increased latency, weird decision)
3. **В конце сессии** — current state + open questions
4. **При несогласии** с другим AI — fixated в `risk_objection`

## Когда AI **должен** прочитать его

1. **На старте каждой сессии** — first action after auth
2. **Перед принятием решения** по любому trade param
3. **При расхождении** с тем что видишь в MCP live state
