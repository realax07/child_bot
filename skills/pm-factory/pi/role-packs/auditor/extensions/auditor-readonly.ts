// Auditor read-only guard: отказ любых записей и мутаций. fail-closed.
export default function (pi: ExtensionAPI) {
  const DENY_MUTATIONS = /(\bgit\s+(push|reset|checkout|rebase|merge|commit|clean|rm)\b|\bdocker\s+(run|exec|rm|rmi|stop|kill|restart|build|compose|system\s+prune|volume|network\s+(rm|prune|create))\b|(^|\s|;)\s*(rm|mv|dd|mkfs|truncate|chmod|chown|tee)\s|>>|>[^&]|npm\s+(install|i|uninstall)|pip\s+(install|uninstall)|apt(-get)?\s+install|systemctl|crontab)/;
  pi.on("tool_call", async (ev: any) => {
    const name = String(ev?.name || ev?.tool || "");
    const args = ev?.args ?? ev?.input ?? {};
    const deny = (what: string) => ({ block: true, reason: `Аудитор read-only: ${what} запрещен. Вердикт — текстом в ответ.` });
    if (name === "write" || name === "edit" || name === "apply_patch" || name === "multiedit") {
      return deny("запись/правка файлов");
    }
    if (name === "bash" || name === "shell") {
      const cmd = String(args?.command ?? args?.cmd ?? "");
      if (DENY_MUTATIONS.test(cmd)) return deny("мутация в bash: " + cmd.slice(0, 120));
    }
    return;
  });
}
