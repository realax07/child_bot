# Ролевой пак Pi: auditor (ручной вызов)

Аудитор с большим контекстом (Kimi K3.1) для разовых аудит-задач ПМ.
**Вне конвейера**: во флоу/ворота не встроен, вызывается только ПМ вручную.

## Сборка
1. Провайдер в `~/.pi/agent/models.json` — блок `"kimi"`: baseUrl
   `https://api.kimi.com/coding/v1` (Kimi For Coding), модель `k3.1-pro`
   (262k ctx; `k3.1-pro-highspeed`/1M требует высший тариф), ключ — chmod 600.
2. role-agents.md → корень клона как `AGENTS.md` (Pi подхватывает безусловно).
3. settings.example.json — справочный, структура единая по всем пакам (J62:
   `zai-glm`/`glm-5.3-flash`, zone-guard); при вызове роль задается флагами —
   большой аудит переопределяет провайдера/модель/ro-guard в CLI
   (`--provider kimi --model k3.1-pro -e …/auditor-readonly.ts`).

## Вызов (не меняя settings regress-runner)
```bash
cd <репо-клон>
pi --provider kimi --model k3.1-pro --no-session \
   -e /путь/к/паку/extensions/auditor-readonly.ts \
   -p "<область аудита и критерии вердикта>"
```
Guard грузится ТОЛЬКО флагом -e (НЕ копировать в ~/.pi/agent/extensions/ —
иначе он заблокирует запись и regress-runner'у). Блокирует write/edit и
bash-мутации (git push/commit, docker-мутации, rm/tee/редиректы, пакетные
менеджеры, systemctl/crontab). Результат — вердикт в stdout; агент ничего
не пишет на диск.
