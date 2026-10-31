---
name: design-review
description: Review or create UI via three quality pillars and checklists
license: MIT (distilled from microsoft/skills frontend-design-review, framework by @Quirinevwm)
metadata:
  hermes:
    tags: [design, review, accessibility, ux, audit, qa]
    related_skills: [frontend-design, ui-ux-matrices, claude-design, design-md, dogfood]
---

# Design Review

Ревью UI-реализаций против стандартов качества и дизайн-системы **ИЛИ** создание нового интерфейса с нуля. Два режима, один и тот же критерий качества.

## Два режима

**Режим 1 — ревью существующего.** Оцени реализацию: соответствие токенам/эталону, три столпа, доступность. Это наш случай: самопроверка перед сдачей (ui_designer) и независимое ревью (design_validator).

**Режим 2 — создание нового.** Прежде чем кодить — зафиксируй эстетическую директорию (детали в `frontend-design`):
- **Purpose**: что решает, кто пользователь.
- **Tone**: minimal / maximalist / retro-futuristic / organic / luxury / playful / editorial / brutalist / industrial.
- **Constraints**: без внешних CDN (ОГР-8), WCAG AA, standalone-HTML.
- **Differentiation**: что делает это отличимым и уместным контексту.

## Три столпа качества

### 1. Frictionless insight-to-action
**Оцени:** задача закрывается за ≤3 интеракций? Главное действие очевидно и единственно?
**Red flags:** лишние клики, несколько конкурирующих primary-кнопок, спрятанные действия, тупики. Каждая витрина отвечает «что я могу сделать?» и «что дальше?»; 1–2 primary-действия на вид, secondary — через progressive disclosure.

### 2. Quality is craft
**Оцени:**
- Соответствие дизайн-системе: токены используются (не хардкод), все варианты/состояния реализованы, отклонения задокументированы.
- Эстетическая директория: отличимая типографика, целостная палитра на CSS-переменных, осознанный моушн.
- Доступность: WCAG 2.1 A минимум (Grade C), AA — цель. Клавиатурная навигация полная, focus indicators видимые.

**Red flags:** generic AI-эстетика (см. `frontend-design` §3), хардкод значений вне `:root`, расхождение с мокапом/токенами, поломанный reflow, отсутствие focus-visible.

### 3. Trustworthy building
**Оцени:** AI-генерированный контент помечен дисклеймером; ошибки actionable (что случилось + как чинить).
**Red flags:** немаркированный AI-контент, непрозрачные ошибки без guidance.

## Чек-лист ревью (перед approve)

**Соответствие дизайн-системе / токенам**
- [ ] Токены из единого источника (`design/tokens.html`), не хардкод hex/px вне `:root`
- [ ] Все состояния контрола: default / hover / focus-visible / active / disabled / error
- [ ] Отступы/радиусы/типографика совпадают с эталоном (tokens.html / утвержденный мокап)
- [ ] Отклонения от эталона задокументированы

**Доступность**
- [ ] Контраст ≥4.5:1 текст, ≥3:1 крупный текст/иконки
- [ ] Клавиатурная навигация и видимый focus-visible
- [ ] Приоритет кодируется не только цветом (цвет + иконка/текст)
- [ ] Aria-атрибуты: label у полей, aria-describedby у ошибок, aria-labelledby у модалок

**Типографика и визуальная целостность**
- [ ] Длина строки body ≤80ch; явная шкала размеров и весов
- [ ] Палитра целостна, один акцент действия, семантические статусы — фиксированная палитра
- [ ] Нет generic-дефолтов из `frontend-design` §4 (eyebrow над заголовком — под запретом всегда)

**Столпы**
- [ ] Frictionless: задача ≤3 интеракций, единственное primary-действие
- [ ] Trustworthy: ошибки actionable

## Формат вердикта

| Столп | Статус | Заметки |
|---|---|---|
| Frictionless | 🟢/🟠/⚫ | |
| Quality Craft | 🟢/🟠/⚫ | |
| Trustworthy | 🟢/🟠/⚫ | |

**Вердикт:** approve (все 🟢 или minor 🟠) / needs-work (несколько 🟠) / blocker (любой ⚫). Каждое расхождение: элемент, источник («ожидалось» — tokens.html/мокап/спека), фактически, серьезность (blocker/major/minor).

## Интеграция с конвейером (Флоу 1 / design_validation)

- **ui_designer, самопроверка перед сдачей:** прогони чек-лист по каждому мокапу до передачи Заказчику; generic-эстетика = переделай, а не «отдам как есть».
- **design_validator, ворота design_validation:** этот скилл — основной инструмент ревью. Пирамида источников: `design/README.md` → `design/tokens.html` → утвержденный мокап (as-is из `design/archive/`) → прод-`app.css` → спека. Ось ревью — «соответствие дизайну»; поведение тестируют сьюты. Verdict-таблица — в `test-model/reviews/<change-id>/review-NNN-design.md`.
- Мокап-ревью (до приемки) сверяет с замыслом; прод-ревью сверяет реализацию с утвержденным эталоном — не предлагай редизайн на этапе QA.

## Источники

Дистиллят microsoft/skills `frontend-design-review` (MIT); pillar-фреймворк — @Quirinevwm; creative-часть восходит к anthropics/skills `frontend-design` (Apache-2.0).
