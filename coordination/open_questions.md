# Open questions for the founder

Список вопросов которые AI накопил, но не может решить сам — нужен ввод от Evgeny.
Когда founder ответил в чате, AI переносит ответ в `strategy_state.yaml` и удаляет вопрос отсюда.

## Активные (требуют ответа)

### Q1 (HIGH) — Grant operator MCP permission `config.set_risk_param`?

**Контекст:** Bot на Binance.US делает все HOLD из-за `MIN_CONFIDENCE=0.65`,
а post-premortem scores cluster в `0.55-0.60`. Codex рекомендует опустить до `0.60`.
MCP попытка `mcp__cryptobur__config_set_risk_param` → HTTP 403 (нет прав).

**Решение:** founder идёт в `https://cryptobur.com/settings/operator` →
панель "Разрешения оператора" → включает `config.set_risk_param` для Binance.US.

После этого AI может сам править риск-параметры без 403.

**Альтернатива:** founder сам меняет в UI `/settings/bot` → Binance.US tab.

### Q2 (MED) — Vibe-Trading swarm: какой preset запускать автоматически?

**Контекст:** 31 swarm preset доступен, но используется только `research_to_cryptobur`
(1 агент). Нет cron'а — запускается только вручную через MCP.

**Варианты:**
- (a) Cron каждый час: `research_to_cryptobur` по top-3 тикерам ротацией
- (b) Cron каждые 4 часа: `crypto_research_lab` (4 агента) глубокий анализ
- (c) Cron каждый день: `technical_analysis_panel` (6 агентов, TA+Ichimoku+SMC) + publish
- (d) Manual only — AI запускает по запросу founder'а

**Рекомендация Codex:** (a) для начала, потом эскалировать к (b) когда подтвердим что
сигналы качественные.

### Q3 (LOW) — Когда подключаем Schwab?

**Контекст:** Schwab exchange отображается как `disabled` в read_system_health.
Live equity null. agent_model_policy не настроен.

**Вопрос:** оставляем выключенным до beta-launch или включаем сейчас в paper-режиме
для тестирования cross-broker логики?

---

## Закрытые (для истории)

(пусто — это первая итерация ledger'a)
