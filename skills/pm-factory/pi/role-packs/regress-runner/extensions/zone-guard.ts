/**
 * zone-guard — механическое исполнение зон роли regress_runner (Pi extension).
 *
 * Пак: skills/autonomous-ai-agents/pi/role-packs/regress-runner/
 * Роль: regress_runner (agents/regress_runner_agent.md), режим auto-edit.
 *
 * Что охраняет (fail-closed):
 *   1. write/edit — запись ТОЛЬКО в разрешенных зонах относительно корня
 *      worktree (cwd сессии): test-model/regress/** и tests/**.
 *      Разрешенные пути настраиваются env PI_ZONE_WRITE (разделитель ':'),
 *      по умолчанию: "test-model/regress:tests".
 *   2. bash — запрет `git push` (коммитит/пушит ПМ, не рантайм).
 *   3. bash — docker: только чтение (ps/inspect/logs/images/version/info/
 *      stats/top). Мутации (run/exec/rm/stop/kill/build/pull/push/cp/save/
 *      load/tag/compose up|down|…|system prune…) блокируются — прод не трогаем.
 *
 * Отказ — вежливый: блокируется только вызвавший tool call
 * ({ block: true, reason }), причина пишется в лог-файл
 * test-model/regress/zone-guard.log внутри cwd (best-effort, сама запись лога
 * зоной считается) и в UI-notify, если UI есть (print/RPC без UI — только лог).
 *
 * Формат — из доки pi (pi.dev/docs/latest/extensions): фабрика, получающая
 * ExtensionAPI; jiti грузит TS без компиляции. Перехват — событие "tool_call",
 * gate-контракт: блок = { block: true, reason }; ошибка обработчика тоже
 * блокирует tool call (fail-safe самого pi).
 *
 * Ограничение (честно): bash-канал проверяется эвристикой по строке команды
 * (редиректы/tee/mv мимо зон НЕ парсятся). Механическая гарантия — только для
 * write/edit; bash-политика — best-effort плюс запрет-маркеры push/docker.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { promises as fs } from "node:fs";
import * as path from "node:path";

export default function (pi: ExtensionAPI) {
	const defaultZones = ["test-model/regress", "tests"];
	const writeZones = (process.env.PI_ZONE_WRITE ?? defaultZones.join(":"))
		.split(":")
		.map((z) => z.trim().replace(/^\.\//, "").replace(/\/+$/, ""))
		.filter(Boolean);

	const LOG_REL = path.join("test-model", "regress", "zone-guard.log");

	async function log(cwd: string, line: string): Promise<void> {
		const stamp = new Date().toISOString();
		try {
			await fs.mkdir(path.join(cwd, "test-model", "regress"), { recursive: true });
			await fs.appendFile(path.join(cwd, LOG_REL), `[${stamp}] ${line}\n`, "utf8");
		} catch {
			// лог best-effort: отказ все равно уже вынесен
		}
	}

	/** Разрешен ли путь записи: должен попасть в одну из зон (относительно cwd). */
	function inWriteZone(absPath: string, cwd: string): boolean {
		const rel = path.relative(cwd, path.resolve(cwd, absPath));
		if (rel === "" || rel.startsWith("..") || path.isAbsolute(rel)) return false;
		const norm = rel.split(path.sep).join("/");
		return writeZones.some(
			(z) => norm === z || norm.startsWith(`${z}/`),
		);
	}

	const GIT_PUSH = /(^|[\s;&|(])git(\s+\S+)*\s+push\b/;
	const DOCKER_MUTATING = /\bdocker(?:\s+compose)?\s+(run|exec|rm|rmi|stop|kill|restart|pause|unpause|build|pull|push|cp|save|load|tag|import|export|commit|attach|system|volume|network|secret|config|up|down|create|update|scale|start|swarm|node|stack|prune|login|logout)\b/;
	// чтение docker, которое разрешаем: ps, inspect, logs, images, version, info, stats, top
	const DOCKER_READONLY = /\bdocker(?:\s+compose)?\s+(ps|inspect|logs|images|version|info|stats|top|events)\b/;

	pi.on("tool_call", async (event, ctx) => {
		const cwd = ctx.cwd ?? process.cwd();

		if (event.toolName === "write" || event.toolName === "edit") {
			const target = (event.input?.path as string | undefined) ?? "";
			if (!target) {
				const reason = `${event.toolName}: не указан path — блокирую fail-closed (зона regress_runner: ${writeZones.join(", ")})`;
				await log(cwd, `BLOCK ${event.toolName} :: ${reason}`);
				return { block: true, reason };
			}
			if (!inWriteZone(target, cwd)) {
				const reason =
					`zone-guard: запись «${target}» вне зоны regress_runner. ` +
					`Разрешены только (относительно ${cwd}): ${writeZones.join(", ")}. ` +
					`Прод-код, спеки, PLAN, реестры — не трогаем; коммитит/пушит ПМ.`;
				await log(cwd, `BLOCK ${event.toolName} ${target} :: вне зоны`);
				if (ctx.hasUI) ctx.ui.notify(`zone-guard: отказ — ${target} вне зоны`, "warning");
				return { block: true, reason };
			}
			return undefined;
		}

		if (event.toolName === "bash") {
			const cmd = (event.input?.command as string | undefined) ?? "";
			if (!cmd) return undefined;

			if (GIT_PUSH.test(cmd)) {
				const reason =
					"zone-guard: `git push` запрещен роли regress_runner — пушит оркестратор (ПМ) после приемки артефакта.";
				await log(cwd, `BLOCK bash git-push :: ${cmd.slice(0, 200)}`);
				if (ctx.hasUI) ctx.ui.notify("zone-guard: git push заблокирован", "warning");
				return { block: true, reason };
			}

			if (DOCKER_MUTATING.test(cmd) && !DOCKER_READONLY.test(cmd)) {
				const reason =
					"zone-guard: docker-мутации на проде запрещены. Разрешено только чтение: docker ps/inspect/logs/images/version/info/stats/top.";
				await log(cwd, `BLOCK bash docker-mutation :: ${cmd.slice(0, 200)}`);
				if (ctx.hasUI) ctx.ui.notify("zone-guard: docker-мутация заблокирована", "warning");
				return { block: true, reason };
			}

			return undefined;
		}

		return undefined;
	});
}
