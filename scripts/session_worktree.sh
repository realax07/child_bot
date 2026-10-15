#!/usr/bin/env bash
# session_worktree.sh — изоляция параллельных сессий конвейера (E5).
# Ветка ↔ директория 1:1: каждая сессия работает в своём git worktree,
# чужой checkout физически не трогает файлы сессии (прецедент R2: 5 сессий).
#
# Использование:
#   session_worktree.sh create <repo> <session-id> <branch> [base-ref]
#       Создает worktree <repo>-worktrees/<session-id> на ветке <branch>
#       (ветка создается от base-ref, default: origin/main; существующая
#       ветка — подключается как есть).
#   session_worktree.sh remove <repo> <session-id>
#       Убирает worktree сессии (ветку НЕ удаляет — это право ПМ после приемки).
#   session_worktree.sh list <repo>
#       Активные worktree сессий репозитория.
#
# Exit codes: 0 — OK; 1 — ошибка аргументов/состояния; 2 — git-ошибка.
set -euo pipefail

die() { echo "E5-ERROR: $*" >&2; exit 1; }

cmd="${1:-}"; repo="${2:-}"; sid="${3:-}"; branch="${4:-}"; base="${5:-origin/main}"

[ -d "$repo/.git" ] || die "не git-репозиторий: $repo"
wt_root="${repo%/}-worktrees"

case "$cmd" in
  create)
    [ -n "$sid" ] && [ -n "$branch" ] || die "create: нужны <session-id> и <branch>"
    wt="$wt_root/$sid"
    [ -e "$wt" ] && die "worktree уже существует: $wt"
    mkdir -p "$wt_root"
    if git -C "$repo" show-ref --verify --quiet "refs/heads/$branch"; then
      git -C "$repo" worktree add "$wt" "$branch" >&2 || exit 2
    else
      git -C "$repo" fetch origin --quiet >&2 || true
      git -C "$repo" worktree add "$wt" -b "$branch" "$base" >&2 || exit 2
    fi
    # J7: маркер сессии в корне дерева — сабагент сверяет его первым действием
    printf '# SESSION\nsession-id: %s\nbranch: %s\nrepo: %s\ncreated: %s\n' \
      "$sid" "$branch" "$repo" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$wt/SESSION.md"
    echo "$wt"
    ;;
  remove)
    [ -n "$sid" ] || die "remove: нужен <session-id>"
    wt="$wt_root/$sid"
    git -C "$repo" worktree remove --force "$wt" 2>/dev/null || git -C "$repo" worktree prune
    rmdir "$wt_root" 2>/dev/null || true
    echo "removed: $wt"
    ;;
  list)
    git -C "$repo" worktree list --porcelain | awk -v root="$wt_root" '
      /^worktree / { p=$2; if (index(p, root) == 1) print p }'
    ;;
  *) die "команда: create|remove|list" ;;
esac
