---
name: pi-auditor
description: "Use for large-context audits; manual Pi+Kimi run, read-only."
---

# Pi-аудитор (Kimi K3.1) — ручной вызов

## Когда
Большие прочтения, где flash теряет хвосты: инвентаризации артефактов, usage-аудиты,
покрытие/e2e-аудиты, раскопки «почему красен CI по всей истории», ревизия репо.
НЕ для: ворот флоу, прод-логики, всего что требует записи.

## Запуск (ничего не пишет на диск, вердикт в stdout)
```bash
rm -rf /tmp/pi-audit-<slug> && mkdir -p /tmp/pi-audit-<slug>
git -C <репо> archive origin/main | tar -x -C /tmp/pi-audit-<slug>
cp /home/openclaw/ai-factory/skills/pm-factory/pi/role-packs/auditor/role-agents.md \
   /tmp/pi-audit-<slug>/AGENTS.md
cd /tmp/pi-audit-<slug> && pi --provider kimi --model k3.1-pro --no-session \
   -e /home/openclaw/ai-factory/skills/pm-factory/pi/role-packs/auditor/extensions/auditor-readonly.ts \
   -p "<область аудита> + <критерии вердикта>"
```

## Контракт брифа
1. Область: репо/каталоги/вопрос — конкретно.
2. Критерии вердикта: что считать находкой.
3. Формат: факты → атрибуция (файл:строка/коммит) → severity (blocker/major/minor/info) → рекомендации.

## Факты конфигурации
- Провайдер `kimi` в ~/.pi/agent/models.json (600): baseUrl `https://api.kimi.com/coding/v1` (Kimi For Coding), модели k3.1-pro (262k ctx) / kimi-for-coding.
- `k3.1-pro-highspeed` (1M) — вне тарифа ключа Заказчика (403 upgrade): НЕ использовать.
- Ключ только в models.json; в команды/логи/саммари не повторять.

## Подводные камни
- Guard грузить ТОЛЬКО флагом -e. НЕ копировать auditor-readonly.ts в ~/.pi/agent/extensions/ — он заблокирует запись и regress-runner'у.
- settings.json в ~/.pi/agent один на хост (от regress-runner): роль аудитора задается флагами, не settings.
- pi молчит >3 мин на больших прочтениях — timeout не меньше 240с.
- Вердикт аудита — СЕЛФ-РЕПОРТ: перед доверием ПМ выборочно верифицирует атрибуции (файл:строка) руками.

## Проверка живости (после пауз)
Мини-аудит на skills/pm-factory/pi/role-packs/auditor/ + попытка записи с guard (ожидается отказ, файл не создан).
