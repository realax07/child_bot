/**
 * zone-guard — зоны записи и запреты роли для исполнителя Pi (extension).
 *
 * Эталон один: skills/pm-factory/pi/zone-guard.ts (J64.10). Пакетный раннер
 * (scripts/package_runner.py) подключает его прямо из репо фабрики
 * (`pi --no-extensions -e <фабрика>/skills/pm-factory/pi/zone-guard.ts`:
 * автозагрузка расширений из ~/.pi и .pi/ проекта выключена — действует
 * только эталон); нет файла — STOP стадии, sha256 guard'а пишется в отчет
 * стадии. Копия в
 * ~/.pi/agent/extensions/zone-guard.ts нужна только для ручных запусков по
 * README паков; ставит ее factory_bootstrap (расхождение с репо — WARN).
 *
 * Настройка — env процесса pi (раннер ставит все три):
 *   PI_ZONE_WRITE      зоны записи через ':' — префикс-форма относительно
 *                      cwd (корня worktree): `src`, `tests/unit`,
 *                      `code-reviews/x/review-*.md` (glob-хвост раннер
 *                      срезает, J61). Не задан или пуст — запись запрещена
 *                      целиком (fail-closed).
 *   PI_ZONE_ROLE       роль политики (agents/roles.json: dev, devops,
 *                      regress_runner, …) — выбирает bash-правила.
 *   PI_ZONE_GUARD_LOG  файл лога отказов — ВНЕ worktree (раннер: state
 *                      фабрики). Не задан — ~/.pi/agent/zone-guard.log.
 *
 * Что охраняет (fail-closed):
 *   1. write/edit — только внутри зон PI_ZONE_WRITE; путь без path, вне
 *      worktree (symlink раскрываются realpath), в .git/ (никакой зоной, даже
 *      `*`/`**`, не открывается) или вне зон — отказ.
 *   2. bash — `git push` запрещен всем ролям (пушит ПМ после приемки).
 *   3. bash — docker-мутации запрещены всем ролям, кроме devops (решение
 *      Заказчика 2026-11-11, J64.10): разрешено только чтение
 *      (ps/inspect/logs/images/version/info/stats/top/events/ls/df/…), любая
 *      иная подкоманда, в т.ч. `compose -f x up`, `container rm` — отказ.
 *
 * Отказ: { block: true, reason } модели + строка в лог + ui.notify, если UI
 * есть. Ошибка обработчика тоже блокирует tool call (fail-safe самого pi).
 *
 * Ограничение (честно): bash-канал — эвристика по вхождениям слов git/docker
 * (редиректы/tee/mv мимо зон, переменные-команды `$CMD`, команды, которые
 * исполняют другие: `rg --pre`, `ack --pager`, `find -exec` с текстом из
 * переменной, — НЕ разбираются). Механическая граница записи —
 * сверка git diff с зоной на finish стадии у раннера (J63); guard — ранняя
 * подсказка агенту, его обход стоит итераций, а не дыры в зоне.
 *
 * Формат — доки pi (pi.dev/docs/latest/extensions): фабрика, получающая
 * ExtensionAPI; jiti грузит TS без компиляции; перехват — событие
 * "tool_call", блок = { block: true, reason }.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { promises as fs } from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

/** Роли, которым разрешены docker-мутации (J64.10). */
const DOCKER_MUTATION_ROLES = new Set(["devops"]);

export default function (pi: ExtensionAPI) {
	const writeZones = (process.env.PI_ZONE_WRITE ?? "")
		.split(":")
		.map((z) => z.trim().replace(/^\.\//, "").replace(/\/+$/, ""))
		.filter(Boolean);
	const role = (process.env.PI_ZONE_ROLE ?? "").trim() || "unknown";
	const logFile = (process.env.PI_ZONE_GUARD_LOG ?? "").trim() ||
		path.join(os.homedir(), ".pi", "agent", "zone-guard.log");

	async function log(line: string): Promise<void> {
		const stamp = new Date().toISOString();
		try {
			await fs.mkdir(path.dirname(logFile), { recursive: true });
			await fs.appendFile(logFile, `[${stamp}] role=${role} ${line}\n`, "utf8");
		} catch {
			// лог best-effort: отказ все равно уже вынесен
		}
	}

	/** Сегмент зоны с glob ('*', '?') — сопоставление внутри одного сегмента. */
	function segMatch(zoneSeg: string, seg: string): boolean {
		if (!/[*?]/.test(zoneSeg)) return zoneSeg === seg;
		const re = new RegExp(
			"^" + zoneSeg.replace(/[.+^${}()|[\]\\]/g, "\\$&")
				.replace(/\*/g, "[^/]*").replace(/\?/g, "[^/]") + "$",
		);
		return re.test(seg);
	}

	/** Путь в зоне: сегменты зоны — префикс сегментов пути. */
	function inZone(norm: string, zone: string): boolean {
		const zs = zone.split("/");
		const ps = norm.split("/");
		if (ps.length < zs.length) return false;
		return zs.every((z, i) => segMatch(z, ps[i]));
	}

	/** Путь с раскрытыми symlink, включая висячие (readlink) и несуществующий
	 * хвост: ближайший существующий предок через realpath, затем остаток. */
	async function realResolve(abs: string, depth = 0): Promise<string | null> {
		if (depth > 40) return null;   // петля symlink — отказ (fail-closed)
		let cur = abs;
		const tail: string[] = [];
		for (;;) {
			try {
				return path.join(await fs.realpath(cur), ...tail.reverse());
			} catch {
				try {
					const st = await fs.lstat(cur);
					if (st.isSymbolicLink()) {   // висячий symlink: цель — по readlink
						const to = path.resolve(path.dirname(cur), await fs.readlink(cur));
						return realResolve(path.join(to, ...tail.reverse()), depth + 1);
					}
				} catch {
					// компонента нет — поднимаемся к предку
				}
				const parent = path.dirname(cur);
				if (parent === cur) return abs;   // корень ФС: остаток не существует
				tail.push(path.basename(cur));
				cur = parent;
			}
		}
	}

	/** Путь записи в зоне: без `..` (ядро раскрывает его ПОСЛЕ symlink —
	 * лексическая нормализация тут врет), относительно worktree (symlink
	 * раскрыты), не в .git/ (никакой зоной не открывается), сегменты зоны —
	 * префикс пути. */
	async function inWriteZone(target: string, cwd: string): Promise<boolean> {
		if (target.split(/[\\/]+/).includes("..")) return false;
		const root = await realResolve(path.resolve(cwd));
		const real = await realResolve(path.resolve(cwd, target));
		if (root === null || real === null) return false;
		const rel = path.relative(root, real);
		if (rel === "" || rel.startsWith("..") || path.isAbsolute(rel)) return false;
		const norm = rel.split(path.sep).join("/");
		if (norm.toLowerCase() === ".git" || norm.toLowerCase().startsWith(".git/")) return false;
		return writeZones.some((z) => inZone(norm, z));
	}

	// bash — эвристика (best-effort, fail-closed): проверяется КАЖДОЕ вхождение
	// слова git/docker в строке — после начала, пробела, разделителя ; & | ( {
	// или кавычки/backtick (обертки sudo -u x, timeout, xargs, if/then, $(…),
	// sh -c "…" не прячут вызов). Токены после вхождения (до конца звена)
	// дают подкоманду: флаги и значения флагов пропускаются. Так `git commit
	// -m push-fix` — не push, а `echo git push` — отказ (безопасная ложная
	// блокировка).
	const WORD = (name: string) =>
		new RegExp(`(?:^|[\\s;&|({}\`"'!])(?:\\S*/)?${name}(?=$|[\\s;&|)}\`"'])`, "g");
	const GIT_WORD = WORD("git");
	const DOCKER_WORD = WORD("docker");
	const GIT_VALUE_FLAGS = new Set(["-C", "-c", "--git-dir", "--work-tree", "--namespace"]);
	const DOCKER_GROUPS = new Set(["compose", "container", "image", "volume", "network",
		"buildx", "builder", "context", "plugin", "system", "swarm", "node", "stack",
		"service", "secret", "config", "trust", "manifest"]);
	const DOCKER_VALUE_FLAGS = new Set(["-f", "--file", "-p", "--project-name", "--env-file",
		"--profile", "--project-directory", "-H", "--host", "--context", "--config", "-c", "-l",
		"--log-level"]);
	// Чтение — разрешено всем; любая иная подкоманда docker — мутация
	// (fail-closed: неизвестное не пропускается). Чтение в том же или соседнем
	// звене мутацию НЕ снимает (`docker ps && docker rm x` — отказ).
	const DOCKER_READONLY = new Set(["ps", "inspect", "logs", "images", "version", "info",
		"stats", "top", "events", "ls", "df", "history", "port", "diff", "search", "help"]);
	const COMPOSE_READONLY = new Set([...DOCKER_READONLY, "config"]);

	/** Токены от позиции до конца звена (; & | перевод строки, закрывающие
	 * скобка/backtick); кавычки с токенов снимаются, звено на них НЕ рвется:
	 * `git -C "$WT" push`, `git -C'/a b' push` — push (ревью J64.10 r3). */
	function tokensAfter(cmd: string, from: number): string[] {
		const rest = cmd.slice(from);
		const end = rest.search(/[;&|\n)}`]/);
		return shellWords(end < 0 ? rest : rest.slice(0, end));
	}

	/** Слова строки с учетом кавычек: `-C '/a b' push` → [-C, /a b, push]. */
	function shellWords(text: string): string[] {
		const out: string[] = [];
		let cur = "";
		let quote = "";
		let has = false;
		for (const ch of text) {
			if (quote) {
				if (ch === quote) quote = "";
				else cur += ch;
			} else if (ch === '"' || ch === "'") {
				quote = ch;
				has = true;
			} else if (/\s/.test(ch)) {
				if (cur || has) out.push(cur);
				cur = "";
				has = false;
			} else {
				cur += ch;
			}
		}
		if (cur || has) out.push(cur);
		return out;
	}

	// Звено, где слово — аргумент поиска/вывода (`grep docker README.md`,
	// `rg -n 'git push'`), вызовом не считается. Начало звена — после ; & | (
	// { перевода строки или backtick, поэтому `echo $(git push)` — отказ.
	const TEXT_COMMANDS = new Set(["grep", "egrep", "fgrep", "rg", "ag", "ack", "echo",
		"printf", "git-grep"]);

	function inTextCommand(cmd: string, at: number): boolean {
		const head = cmd.slice(0, at);
		const start = Math.max(...[";", "&", "|", "(", "{", "\n", "`"].map((c) => head.lastIndexOf(c))) + 1;
		const first = head.slice(start).trim().split(/\s+/)[0] ?? "";
		return TEXT_COMMANDS.has(first.replace(/^.*\//, ""));
	}

	function subcommand(tokens: string[], from: number, valueFlags: Set<string>): [string, number] {
		for (let i = from; i < tokens.length; i++) {
			const t = tokens[i];
			if (t.startsWith("-")) {
				if (valueFlags.has(t)) i++;   // значение флага — следующий токен
				continue;
			}
			return [t, i];
		}
		return ["", tokens.length];
	}

	function* occurrences(re: RegExp, raw: string): Generator<string[]> {
		const cmd = raw.replace(/\\\r?\n/g, " ");   // `\` + перевод строки — продолжение
		// текст в интерпретатор (`echo git push | sh`) — исключение текстовых
		// команд не действует (ревью J64.10 r4)
		const piped = /\|\s*(?:\S*\/)?(?:sh|bash|zsh|dash|ksh|fish)\b/.test(cmd);
		re.lastIndex = 0;
		for (let m = re.exec(cmd); m; m = re.exec(cmd)) {
			if (piped || !inTextCommand(cmd, m.index + 1)) {
				yield tokensAfter(cmd, m.index + m[0].length);
			}
			if (m[0].length === 0) re.lastIndex++;
		}
	}

	function gitPush(cmd: string): boolean {
		for (const tk of occurrences(GIT_WORD, cmd)) {
			if (subcommand(tk, 0, GIT_VALUE_FLAGS)[0] === "push") return true;
		}
		return false;
	}

	function dockerMutation(cmd: string): boolean {
		for (const tk of occurrences(DOCKER_WORD, cmd)) {
			let [verb, at] = subcommand(tk, 0, DOCKER_VALUE_FLAGS);
			let readonly = DOCKER_READONLY;
			if (DOCKER_GROUPS.has(verb)) {
				if (verb === "compose") readonly = COMPOSE_READONLY;
				[verb] = subcommand(tk, at + 1, DOCKER_VALUE_FLAGS);
			}
			if (verb !== "" && !readonly.has(verb)) return true;
		}
		return false;
	}

	async function block(kind: string, detail: string, reason: string, ctx: any) {
		await log(`BLOCK ${kind} ${detail}`);
		if (ctx?.hasUI) ctx.ui.notify(`zone-guard: отказ — ${kind}`, "warning");
		return { block: true, reason };
	}

	pi.on("tool_call", async (event: any, ctx: any) => {
		const cwd = ctx?.cwd ?? process.cwd();

		if (event.toolName === "write" || event.toolName === "edit") {
			const target = (event.input?.path as string | undefined) ?? "";
			const zones = writeZones.join(", ") || "— (PI_ZONE_WRITE не задан: запись запрещена)";
			if (!target) {
				return block(event.toolName, ":: нет path",
					`${event.toolName}: не указан path — блокирую fail-closed (зона роли ${role}: ${zones})`, ctx);
			}
			if (!(await inWriteZone(target, cwd))) {
				return block(event.toolName, `${target} :: вне зоны`,
					`zone-guard: запись «${target}» вне зоны роли ${role}. ` +
					`Разрешены только (относительно ${cwd}): ${zones}. ` +
					`Остальное — через ПМ; коммитит/пушит ПМ.`, ctx);
			}
			return undefined;
		}

		if (event.toolName === "bash") {
			const cmd = (event.input?.command as string | undefined) ?? "";
			if (!cmd) return undefined;
			if (gitPush(cmd)) {
				return block("bash git-push", `:: ${cmd.slice(0, 200)}`,
					`zone-guard: \`git push\` запрещен роли ${role} — пушит ПМ после приемки.`, ctx);
			}
			if (!DOCKER_MUTATION_ROLES.has(role) && dockerMutation(cmd)) {
				return block("bash docker-mutation", `:: ${cmd.slice(0, 200)}`,
					`zone-guard: docker-мутации запрещены роли ${role} (разрешены только devops). ` +
					`Чтение: docker ps/inspect/logs/images/version/info/stats/top.`, ctx);
			}
			return undefined;
		}

		return undefined;
	});
}
