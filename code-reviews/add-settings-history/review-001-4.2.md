Reviewer-Delegation: deleg_1b489e7aa8a747b6

# Code Review: задача 4.2 пакета add-settings-history (review-001)

- **Ревьюируемый дифф:** `main..ce8b728` (9 миноров) + `ce8b728..c3af3d5` (мёртвый DailySummary.total_wake, санкция ПМ п.в техдолга 1.3); worktree deleg-1b489e7aa8a747b6. Коммиты: ce8b728 «dev 4.2: уборка миноров ревью волн 1–3», c3af3d5 «dev 4.2-fix: мёртвый DailySummary.total_wake удален».
- **Зона:** src/handlers/{__init__,events,settings_screen,undo}.py, src/repositories/users.py, src/services/{event,settings,calculation}.py, tests/{test_delete_all,test_settings_history,test_smoke_settings_history,test_daily_summary,test_settings_usage}.py. Санкции ПМ соблюдены: handlers/__init__.py (минор 3.3), calculation.py + тесты total_wake (п.в 1.3).
- **Арбитры:** tasks.md 4.2 («закрытие оставшихся minor-замечаний волн 1–3; grep-контроль owner_users/дублей дефолтов/total_wake»); review-001/002 волн 1–3 (перечни minor); design D5/D8/D12/D21; ТЗ §7.17/§7.19, FR-44.
- **Метод:** самостоятельные прогоны в worktree (регресс ×2, flow_check, openspec --strict, точечные тесты), скрипт-пробы: грамматика парсера (13 входов), day_summary_lines/status/hint побайтно main↔c3af3d5 (5 сценариев), grep-контроль, AST-сверка имён тестов ↔ TC-меток.

## Вердикт: approve

## Замечания

| # | Severity | Файл:место | Замечание | Рекомендация |
|---|----------|------------|-----------|--------------|
| 1 | minor | tests/test_settings_history.py (отсутствует) | Minor 3 review-001-1.1 (регресс-тест сбоя `SettingsHistoryRepo.insert` между stage_upsert и insert истории в `SettingsService.update`) в 4.2 НЕ закрыт: в сьюте такого теста нет — grep по tests/ находит rollback-тест только для пары (test_backfill_pair.py TC-PBACK-011), для settings.update — нет (только `fixed_now` monkeypatch). Пункт не входит и в санкционированный ПМ список 9 миноров — расхождение с формулировкой tasks.md 4.2 «оставшиеся minor-замечания волн 1–3». Сам код try/except+rollback корректен (верифицирован пробой review-001-1.1), поведение при отказе валидации покрыто TC-SETT-013 — не блокирует. | Включить в следующий пакет/микро-фикс: тест с monkeypatch-сбоем `SettingsHistoryRepo.insert` → отказ, settings и история не тронуты (образец — проба review-001-1.1). |
| 2 | info | review-001-1.2 m2, review-002-1.2 m1 | Вне зоны 4.2 и без действий по коду (решения зафиксированы в отчётах): фаза настроек переживает /start — «решение ПМ отдельно» (решение так и не зафиксировано в requirements/design — напоминание оркестратору); ложная кнопка события гасит WAIT_SETTING_VALUE — «принять как есть (safe-direction)». | Оркестратору: закрыть решение по /start-фазе явной записью (design или отказ). |

## Проверка 9 миноров (сверка с review-001/002 волн 1–3)

| # | Минор (источник) | Статус | Факт закрытия |
|---|------------------|--------|---------------|
| 1 | Явный rollback в `delete_all_data` (review-001-3.3 m1) | ЗАКРЫТ | users.py: try/except → `self._conn.rollback()` + raise; тест TC-HELPDEL-016 (`_FailingConn`, сбой 3-го DELETE): после исключения все счётчики на ТОМ ЖЕ соединении равны before — атомарность не зависит от вызывающего. Тест реален: 2 passed локально. |
| 2 | Первый-токен в guard /delete_all (review-001-3.3 m2) | ЗАКРЫТ | handlers/__init__.py: `(text.split() or [""])[0] == "/delete_all"`; TC-HELPDEL-017: `/delete_allxyz` гасит фазу, ровно `/delete_all` — фаза цела. 2 passed. |
| 3 | Грамматика парсера (review-001-1.1 m1) | ЗАКРЫТ | settings.py: минутная часть ≤ 59 в «ЧчММ» (как в «Ч:ММ»); «Nм» до 3 цифр (санкция ПМ). Проба: «5ч75м» reject, «5ч20» reject, «120м»→120, «1000м» reject, «5:20»→320, «5ч20м»→320; регресс старых форматов: «320»→320, «5ч»→300, «20м»→20, «5.5»→330, « 5 : 20 »→320, «999м»→999, «5ч0м»→300. Тесты TC-SETT-031/032 в сьюте. |
| 4 | Имена тестов ↔ TC-метки (review-001-1.1 m2) | ЗАКРЫТ | AST-сверка test_settings_history.py: все `test_tc_sett_NNN_*` несут ту же метку в первой строке докстринга (004, 005, 001, 002, 006, 003 переставлены соответственно; тела без изменений). |
| 5 | Публичные хелперы `local_hhmm`/`settings_day_start` (review-001-1.3 m1) | ЗАКРЫТ | events.py: `_local_hhmm`→`local_hhmm`, `_settings_day_start`→`settings_day_start`; undo.py импортирует публичные имена; коллизия локальной переменной `local_hhmm` в time_choose_callback/undo_done_text устранена переименованием в `now_line`/`removed_line` (семантика та же). |
| 6 | E402-импорт в settings_screen.py (review-001-2.0 m1) | ЗАКРЫТ | `from src.services.state import …` поднят в общий блок импортов; комментарий-обоснование D1/D14 сохранён. |
| 7 | Единый `_ensure_event_step` (note 3 review-002-3.1) | ЗАКРЫТ | event.py: `_ensure_pair_step` удалён, тело перенесено в `_ensure_event_step` (побайтно те же 3 проверки, тот же порядок); `create_event` и оба шага `create_event_pair` вызывают хелпер; проверка шага 2 — после stage_insert засыпания (та же транзакция). Регресс 3.1 green (TC-PBACK-011 в сьюте). |
| 8 | Усиление смок-ассертов (review-001-4.1 m1+m2) | ЗАКРЫТ | test_smoke_settings_history.py: шаг 4 — `hint_before_text` теперь ПОЛНОЕ равенство списка строк (не substring, симметрично hint_after); шаг 12 — `assert len(…replies) == 1` перед разбором replies[0]. |
| 9 | Grep-контроль техдолга 1.3 (tasks.md 4.2) | ЧИСТ | `owner_users` в src/ — только упоминание в докстринге users.py; в scheduler.py SQL отсутствует (grep 0 совпадений); дублей DEFAULT_KEYS в src/ нет; мёртвый total_wake удалён (см. ниже). time.sleep в трёх изменённых тест-файлах — 0. |

## total_wake (c3af3d5)

- **Диф-контроль:** `git diff ce8b728..c3af3d5` — ровно 3 файла: calculation.py (снятие поля `total_wake`, его вычисления `window_start/window_end/covered` и пунктов докстрингов — никакой другой логики не тронуто; `_day_page_end` сохранён, живые вызовы :215/:562/:840/:977 целы), test_daily_summary.py (сняты только ассерты/строки докстрингов про total_wake), test_settings_usage.py (ассерт заменён на grep-контроль отсутствия поля через `dataclass_fields`; импорт DailySummary добавлен). ce8b728 calculation.py не трогал (`--stat` пуст).
- **Живые символы — нулевой дифф вне санкции:** `git diff main..c3af3d5 --stat -- src/services/recommendation.py src/handlers/hint.py src/handlers/history.py src/texts.py` — ПУСТО. `total_wake_at`/`_bug004_total_wake` (calculation.py:457–460/:630), `_PageFacts.total_wake` (history.py:125/:198/:223/:241), `status["total_wake"]` (recommendation.py:255/:314, hint.py:123) — не тронуты.
- **Сводка PB-3/BUG-004 побайтно (скрипт, main ↔ c3af3d5):** 5 сценариев — дневной сон+ночной через полночь, незакрытый ночной сон (BUG-004-заморозка), пустой день (day_summary_lines), неполные данные и бодрствующий ребёнок (status/hint-путь) — дифф вывода ПУСТОЙ (sha256 совпадают попарно). Полный hint-путь бодрствующего дополнительно подтверждён прямым прогоном RecommendationService: state=awake, `status["total_wake"]=7:50:00`, окна и строки §7.6 построены.
- **Семантика:** удалено поле «бодрствование внутри окна страницы» (решение D8 «сумма за день смысла не имеет»); живые символы — иная семантика (текущее бодрствование BUG-004/PB-3, факты страницы, прогноз) — не затронуты.

## Scenario (проверено)

1. Грамматика парсера: 13 входов скриптом (см. таблицу миноров) — новые отказы и старые форматы; TC-SETT-031/032 green в сьюте.
2. /delete_allxyz гасит фазу, /delete_all не гасит, rollback при сбое между DELETE — TC-HELPDEL-016/017: 2 passed.
3. Сводка/подсказка не изменилась: 5 сценариев побайтно main↔c3af3d5 — идентичны.
4. Регресс: `pytest tests/ -q` ДВАЖДЫ — 349 passed ×2 (10.29s / 9.62s).
5. Ворота: `python3 scripts/flow_check.py .` → «flow_check: OK»; `openspec validate add-settings-history --strict` → «is valid».
6. Гигиена: AST-сверка имён↔TC-меток в test_settings_history.py и test_delete_all.py — согласованы; time.sleep — 0.

## Что не проверено

- Живой бот (ручная приемка продакта) — вне зоны ревью.
- Мутационные пробы над c3af3d5 (напр., инъекция сбоя в delete_all_data в рантайме) — покрыты тестом TC-HELPDEL-016 тем же механизмом прокси-соединения.
- Поведение фазы настроек при /start (открытое решение ПМ — замечание info №2).
