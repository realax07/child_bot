<!-- review: add-settings-history / задача 2.0 | диф: main..6fbd680 (dev 2.0) | worktree: deleg-2fd1840f41c24618 -->
Reviewer-Delegation: deleg_2fd1840f41c24618

# Code Review 2.0 — кнопки «📊 История за неделю» и «⚙️ Настройки» в reply-клавиатуре

- **Исполнитель:** dev-агент (коммит `6fbd680`, ветка `feature/add-settings-history-2.0`)
- **Диф:** `git diff main..6fbd680` — 6 файлов: `src/services/state.py`, `src/handlers/common.py`, `src/handlers/settings_screen.py`, `tests/test_state.py`, `tests/test_bug_002.py`, `tests/test_smoke_recommendation.py`
- **Спека-арбитр:** openspec/changes/add-settings-history/tasks.md (2.0), ТЗ §6 (состав/порядок меню), §10.3 (callback-контракты), design D14/D21–D22; FR-48, FR-42
- **Зоны:** формальная зона 2.0 — state.py, common.py, test_state.py (+ test_common.py «если есть» — не существует). `settings_screen.py`, `test_bug_002.py`, `test_smoke_recommendation.py` — по САНКЦИИ ПМ (перенос CALLBACK_SETTINGS_SHOW/BTN_SETTINGS в state.py по D14; ассерты состава клавиатуры). Не считаются OUT_OF_ZONE.

## Замечания

| # | Severity | Файл:строка | Суть | Рекомендация |
|---|----------|-------------|------|--------------|
| 1 | minor | src/handlers/settings_screen.py:63 | Импорт `from src.services.state import BTN_SETTINGS, CALLBACK_SETTINGS_SHOW` стоит ПОСЛЕ блока импортов, среди комментариевого блока (E402-стиль). Работает корректно: state.py не импортирует handlers, цикла нет (проверено 3 порядка импортов — см. ниже). | При следующем касании файла (например, уборка 4.2) поднять строку в общий блок импортов `from src.services...`. Не блокирует. |
| 2 | note | scripts/flow_check.py вывод | `FLOW-WARNING: промпты устарели относительно ~/ai-factory (pm_agent.md)` — инфраструктурное, вне зоны задачи и вне диффа; `flow_check: OK` при этом. | Вне зоны 2.0; передать ПМ как фоновое наблюдение по конвейеру. |

Blocker: нет. Major: нет.

## Проверено (с командами и выводом)

1. **Клавиатура, оба состояния, порядок §6, тексты дословно.**
   - `state.REPLY_KEYBOARD` (state.py:64-68): `[[BTN_SLEEP, BTN_WAKE, BTN_HINT], [BTN_HISTORY, BTN_SETTINGS], [BTN_UNDO]]`; `REPLY_BUTTONS` — те же 6 кнопок. Порядок внутри пары (История → Настройки) и позиция после строки события / до «Отменить последнее» соответствует §6 (относительный порядок §6: событие → подсказка → история → настройки; «✏️ Ввести задним числом» из §6 в панели отсутствует — вне зоны 2.0, парный ввод — волна 3.1).
   - `build_menu`: в ОБОИХ ветках (awake/asleep) строка `[(BTN_HISTORY, CALLBACK_HISTORY), (BTN_SETTINGS, CALLBACK_SETTINGS_SHOW)]` между event_row и строкой undo — инвариантно к состоянию. Callback-контракты §10.3 дословно: `history:week`, `settings:show` (state.py:39-40).
   - Тексты дословно §6: `BTN_HISTORY == "📊 История за неделю"`, `BTN_SETTINGS == "⚙️ Настройки"` — ассертятся литералами в test_state.py (TC-STATE-008, строки 288-289).
   - **kind_of_button НЕ классифицирует их как событийные:** `_KIND_BY_STATE_BUTTON` не расширен (только sleep/wake); `kind_of_button(BTN_HISTORY) is None`, `kind_of_button(BTN_SETTINGS) is None` — ассерты TC-STATE-008 (строки 304-305) и TC-STATE-006-строка (test_state.py:193). Нажатие не проходит событийный маршрут → сон не пишется.
2. **Единый источник истины, без циклических импортов.**
   - settings_screen.py импортирует `BTN_SETTINGS, CALLBACK_SETTINGS_SHOW` из `src.services.state` (строковых копий в diff нет: старые определения удалены, `grep` по копиям чист). Направление handler → service (D14/D1) соблюдено; state.py не импортирует ничего из handlers (проверено чтением диффа state.py).
   - Три порядка импортов — все OK, `sys`-чисто:
     `python3 -c "import src.services.state"` → OK; `import src.handlers.settings_screen` → OK; `import src.handlers.common` → OK; дополнительно `import src.main` → OK.
   - Идентичность объектов (не копии): `settings_screen.BTN_SETTINGS is state.BTN_SETTINGS` → `True` (значение `'⚙️ Настройки'`).
3. **No-op маршрута «📊 История за неделю» и целность остальных.**
   - common.py `reply_button_message`: `BTN_HISTORY` распознан как кнопка панели (`text not in state.REPLY_BUTTONS` — гейт теперь покрывает обе новые), ветка `if text == state.BTN_HISTORY: return` — без ответа, без записи, без вызова `kind_of_button`/events. Докстринг прямо описывает no-op до 2.1 — ровно как заявлено в эскалации, принятой ПМ.
   - «Отмена»/существующие кнопки не сломаны: ветки BTN_UNDO→undo, BTN_SLEEP/WAKE→guard D5, BTN_HINT→hint — без изменений в диффе (только гейт упрощён и маршрут Настроек переведён на `state.BTN_SETTINGS`); регресс 307 green ×2 это подтверждает.
   - «⚙️ Настройки» ведёт к тому же экрану §7.16 (settings_screen.reply_settings_button — без изменений).
   - Поведение проверено тестом TC-STATE-009: history → `replies == []` и `SleepEventsRepo(...).list_by_user(1) == []` (0 записей); settings → 1 ответ с заголовком экрана и 0 записей сна; произвольный текст → молча.
4. **Регресс санкционированных ассертов.**
   - TC-BUG002-001: переименован в `..._static_six_buttons`, ассертит 3 строки / 6 кнопок + `resize_keyboard is True` + `one_time_keyboard is not True` — суть хотфикса BUG-002 (постоянство панели) СОХРАНЕНА и усилена.
   - TC-SMOKE2-001 (test_smoke_recommendation.py:319-325): `REPLY_KEYBOARD` == 3 строки, 6 кнопок — консистентно с test_bug_002 и test_state.
   - Состав/порядок клавиатуры ассертится СОГЛАСОВАННО в трёх тест-файлах; расхождений и ослаблений нет (`grep` по остальным тестам: других ассертов REPLY_KEYBOARD/REPLY_BUTTONS/`len(datas)`/`inline_keyboard)==2` нет).
5. **Регресс-прогон (свежий вывод, worktree deleg-2fd1840f41c24618, HEAD = 6fbd680):**
   - `python3 -m pytest tests/ -q` — прогон 1: **307 passed in 7.62s**; прогон 2: **307 passed in 7.54s**.
   - `python3 scripts/flow_check.py .` → `flow_check: OK — порядок артефактов соответствует флоу` (одно FLOW-WARNING вне зоны — см. замечание 2).
6. **Гигиена новых тестов.**
   - TC-метки: TC-STATE-008 (test_state.py:280), TC-STATE-009 (test_state.py:332) — в первой строке докстринга (rule 6).
   - Фиксированные данные: новые тесты не используют `datetime.now`/`utcnow`/`time.sleep`/`date.today` (grep чист по диффу; единственный `datetime.now` в test_bug_002.py:96 — pre-existing на main, в дифф не входит). User_id фиксированы (3001–3003), время в новых тестах не проверяется — время не нужно для клавиатурных ассертов.

## Что НЕ проверено (честно)

- Реальный Telegram-API Roundtrip (нажатие в живом боте) — не проверялось; проверка через фейковые PTB-объекты и регресс-набор, как и все предыдущие ревью пакета.
- Экран/агрегация истории (callback `history:week` остаётся без обработчика до 2.1 — ожидаемо и санкционировано ПМ; на мерже 2.1 колбэк получит обработчик). Устаревший inline-колбэк `history:week` сейчас молчит — соответствует согласованному no-op.
- Ручная приёмка продакта (критерий пакета) — вне ревью кода.

## Вердикт: approve

Задача 2.0 соответствует спеке (tasks.md 2.0, ТЗ §6/§10.3, design D14/D21–D22) и санкциям ПМ: обе кнопки в обоих состояниях build_menu и в статичной reply-панели, тексты и callback-контракты дословно, порядок §6 соблюдён; единый источник констант в state.py без циклических импортов; no-op маршрута Истории ровно как согласовано, остальные маршруты не тронуты; санкционированные ассерты обновлены консистентно без ослабления; регресс 307 passed ×2 и flow_check OK — на свежем выводе. Открытых blocker/major нет; два minor не блокируют.
