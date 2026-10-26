# tests/smoke_cross — cross-domain smoke (эшелон 2, J54)

Смоук полного контура: роутинг/авторизация/health/представитель сервиса.
Гоняется `flowctl regress --change C --tier smoke` ПЕРЕД code_review и
перед merge (2–3 мин). Список сьютов проекта — манифест
`test-model/smoke-cross/manifest.json` (`{"suites": ["tests/...", ...]}`);
пустой манифест → WARN и exit 0. Требования к тестам: быстрые,
стендонезависимые где возможно, детерминированные при `--xdist`
(session-фикстуры не делятся между воркерами).
