# Роли в координации Claude + Codex

## Кто что делает

| Роль | Кто | Главное оружие | Никогда не |
|---|---|---|---|
| **Researcher** | Claude | MCP read tools, browser, file search | live trade w/o sign-off |
| **Skeptic** | Codex | adversarial review, find bugs/risks | autonomous code change w/o review |
| **Risk Gate** | Claude + Codex (both) | must agree before live config write | bypass on speed urgency |
| **Executor** | Founder (Evgeny) | UI clicks, MCP permissions | manual trades — those are AI's |

## Handoff правила

### Когда Claude → Codex
- Нашёл архитектурный вопрос с неочевидным trade-off
- Нужен second opinion на security/correctness change
- Готовлю PR, хочу adversarial review перед merge
- User спрашивает что-то требующее глубокого кодоанализа

### Когда Codex → Claude
- Завершил расследование, передаю claim back в основной chat
- Founder ждёт человекочитаемое объяснение
- Нужен UI/UX/визуал change (Claude лучше с TypeScript+React)

### Disagreement protocol
1. AI несогласный пишет `risk_objection` в `strategy_state.yaml`
2. Другой AI отвечает `resolution`
3. Если оба upholdят — эскалируют founder'у через `open_questions.md`
4. Founder решает — ответ переносится в `strategy_state.yaml`
5. Никаких live changes до resolution

## Что AI **должен** сделать в начале каждой сессии

```python
# Псевдокод. Применим к любому AI.
1. read("Vibe-Trading/coordination/strategy_state.yaml")  # last 5 entries
2. read("Vibe-Trading/coordination/open_questions.md")
3. mcp.cryptobur.read_system_health()  # ground truth
4. compare ledger.last_market_state vs live → flag drift
5. announce: "session start, последние решения — X, открытых вопросов — Y"
```

## Что AI **должен** сделать в конце каждой сессии

```python
1. append entry to strategy_state.yaml with:
   - what was decided this session
   - any new open question (also added to open_questions.md)
   - next_check timestamp + owner
2. commit + push (or note "не закоммичено, founder approve first")
```

## Источники

Паттерн собран из:
- `langchain-ai/langgraph` — durable state graphs
- `microsoft/autogen` — group chat with roles
- `All-Hands-AI/OpenHands` — agent runbook discipline
