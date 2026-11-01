<!-- change: add-settings-history | project: child_bot -->

# Отчет SA: create_change add-settings-history

- **Дата:** 2026-11-01
- **Коммит:** один коммит на main — `[chore] change-пакет add-settings-history (Флоу 1): proposal/design/tasks/specs, волны 1-4` (SHA — в git log; в файле не фиксируется, чтобы не ломать требование «одним коммитом»)

## Сделано

- proposal.md — зачем/что: настройки с UI (§7.16–7.19) и settings_history (§5.1), история за неделю (PB-5, §7.20–7.22), /help (§7.28), /delete_all (§7.26–7.27), PB-1 парный ввод двумя шагами; альтернативы 1–5; anti-scope (Web App, UI роли наблюдателя, миграции данных, settings:reset, отклонённые пресеты); влияние: клавиатура §6 (+2 кнопки), settings.py, техдолг пакета 2 в волнах 1/4
- requirements.md — FR-42…FR-54 (нумерация после FR-41 add-recommendations), группы P (настройки + settings_history), Q (история за неделю), R (PB-1; FR-52 пресеты — ОТКЛОНЕНО продактом, номер не переиспользуется), S (/help, /delete_all). Раздел 3 «Решённые вопросы продакту (ОВ-5 — закрыты 01.11)»; WAITING_PRODUCT в пакете не осталось
- research.md — конспект ТЗ §5/§5.1/§4.8/§4.4/§6/§7.16–7.28/§8.3/§9.4/§10.2–10.5; таблица DEFAULT_KEYS (13 ключей, включая night_wake-независимый target_nap_duration из 1.3); состояние settings_history; что уже покрыто пакетами 1–2 (D14, FR-24, BUG-004, D8/D9, D13); расхождение PLAN↔код
- design.md — D21–D26 (продолжение нумерации D1–D20): D21 SettingsService-UI (валидация по типам DEFAULT_KEYS, settings+history одной операцией, CREATE TABLE settings_history идемпотентно — не миграция; handlers/settings_screen.py; 13-й параметр на экране — допущение к подтверждению ПМ); D22 история (weekly_history — чистая функция CalculationService, ✓/⚠️ по границам, §7.21/7.22 честно, кнопка в клавиатуре state.py/common.py); D23 PB-1 два шага (решение продакта 01.11, единый конфликт-чек пары, незакрытый sleep → только пробуждение); D24 пресеты — ОТМЕНЕНЫ продактом 01.11 (запись решения для трассировки); D25 /delete_all (кнопка «Да, удалить всё» — решение продакта 01.11, DELETE всех данных своего user_id одной транзакцией, включая users); D26 /help. Связи с D1–D20 отдельным разделом
- tasks.md — 4 волны: 1 (settings_history+update; экран/диалог §7.16–7.19; уборка техдолга пакета 2 — дубли хелперов, owner_users→UsersRepo, мёртвая total_wake, nap_window_text, дубль дефолтов), 2 (2.0 кнопки клавиатуры §6 — state.py/common.py в зоне; 2.1 weekly_history+history.py; пресеты отменены — фиксация в шапке волны), 3 (3.1 PB-1 два шага; 3.2 /help; 3.3 /delete_all кнопкой), 4 (smoke «неделя с настройками» + финальная уборка). Зоны — точные пути; тесты — TC-<ДОМЕН>-NNN с трассировкой в docstring
- specs/** — 5 дельт ADDED: settings-ui (экран/валидация/history/изоляция), weekly-history (сводка, ✓/⚠️, неполные данные), paired-backfill (два шага, только пробуждение при незакрытом сне), help-and-delete (/help, /delete_all кнопкой), state-ui (кнопки §6 в обоих состояниях). Спека presets НЕ создаётся — отменена продактом 01.11 (FR-52 ОТКЛОНЕНО в requirements). Дельта diary-events не потребовалась: изменение меню вынесено в state-ui, изменение ввода — в paired-backfill (объяснено в report)
- impact.md — сервисы/контракты/деплой/миграции/тесты; обновляемые тесты (test_state, smoke пакета 1–2, test_settings_usage) и новые файлы

## Решения продакта по ОВ-5 (получены 01.11, внесены ДО коммита)

- **ОВ-5.1 = ДВА ШАГА** диалога парного ввода (как в рекомендации ПМ). WAITING_PRODUCT с волны 3.1 снят; семантика: «Когда заснул?» → «Когда проснулся?», единый конфликт-чек на пару, незакрытое засыпание → только пробуждение. Внесено: requirements FR-51/§3, design D23, tasks 3.1, specs/paired-backfill.
- **ОВ-5.2 = ПРЕСЕТЫ ОТМЕНЕНЫ** («не думаю что пригодится»). Задача пресетов исключена из волны 2; в диалоге ввода остаётся существующий «🕐 Сейчас» (§7.10). FR-52 помечен ОТКЛОНЕНО (номер зарезервирован), спека presets не создаётся, TC-PRESET кейсов нет. Внесено: proposal (alt 5, anti-scope), requirements FR-52/§3, design D24, tasks (волна 2), impact.
- **ОВ-5.3 = ПОДТВЕРЖДЕНИЕ /delete_all ОТДЕЛЬНОЙ КНОПКОЙ «Да, удалить всё»** (текст-пароль отклонён). WAITING_PRODUCT снят. Внесено: requirements FR-54/§3, design D25, tasks 3.3, specs/help-and-delete.
- Итог: раздел ОВ-5 в requirements/proposal — «Решены продактом 01.11»; WAITING_PRODUCT в пакете нет.

## Проверки

- `openspec validate add-settings-history --strict` → «Change 'add-settings-history' is valid» (exit 0, без ошибок и без warnings; тексты Requirements <500 символов, детали в Scenarios)
- `python3 scripts/flow_check.py .` → «flow_check: OK — порядок артефактов соответствует флоу» (exit 0; штатный FLOW-WARNING про устаревшие промпты ~/ai-factory — вне зоны пакета)
- Все файлы внутри зоны openspec/changes/add-settings-history/**

## Находки и эскалации (для ПМ)

- **settings_history фактически НЕ в схеме main:** PLAN и постановка считают таблицу созданной пакетом 1; по коду db.py схема v1 создаёт только users/sleep_events/settings («settings_history и daily_summary НЕ создаются (пакеты 2–3)»). Решено без отступления от anti-scope: CREATE TABLE IF NOT EXISTS по §9.4 в db.py (идемпотентно, данных не лифтуем — отдельной миграции нет). Зафиксировано в research/design/report; ПМ может скорректировать PLAN-формулировку.
- **13-й параметр на экране:** §7.16 и PLAN говорят про 12 параметров, в DEFAULT_KEYS их 13 (target_nap_duration, решение №1 от 26.10). Допущение D21: показывать в группе «Прочее» — иначе настройка недостижима через UI. На подтверждение ПМ (не блокирует волну 1).
- **`settings:reset`** (§10.3) и откат из истории — вне пакета: нет текста §7 и решения продакта.

## Критерии готовности этапа

- [x] proposal/design/tasks/specs/impact созданы по конвенции openspec (по образцу add-recommendations)
- [x] strict-валидация проходит без ошибок и warnings
- [x] трассировка задач на FR-42…FR-54 и решения D21–D26 полная; решения ОВ-5 внесены во все артефакты до коммита
- [x] один коммит на main, не запушен
