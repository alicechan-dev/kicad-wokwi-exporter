#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Каталог деталей Wokwi из публичных репозиториев на GitHub
=========================================================

Собирает типы деталей, которые можно указать в diagram.json:
  * wokwi/wokwi-elements — веб-компоненты: @customElement('wokwi-…') в src/*.ts
    (а также customElements.define('wokwi-…'));
  * wokwi/wokwi-boards   — платы: каждая лежит в boards/<имя>/board.json и в
    diagram.json называется "board-<имя>". Декораторов @customElement в этом
    репозитории нет, поэтому платы определяются по папкам.

Результат — wokwi_catalog.json в текущей папке: отсортированный список "parts"
и сведения об источниках. Если список не изменился, файл не перезаписывается.

Запросы к API GitHub: по одному на репозиторий (дерево файлов). Содержимое файлов
берётся с raw.githubusercontent.com — это не расходует лимит API. Без токена лимит
API — 60 запросов в час с одного IP; с токеном — 5000. Токен читается из переменной
окружения GITHUB_TOKEN (или GH_TOKEN) и отправляется только на api.github.com.

    python update_wokwi_catalog.py [-o wokwi_catalog.json] [--dry-run] [--force]

Нужен пакет requests (pip install requests).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:  # pragma: no cover - зависит от окружения
    sys.stderr.write("Нужен пакет requests: pip install requests\n")
    sys.exit(2)


API_URL = "https://api.github.com"
RAW_URL = "https://raw.githubusercontent.com"

# Откуда брать детали. elements_dir — где искать .ts с @customElement;
# elements_fallback — если папки нет, искать .ts по всему репозиторию;
# expect — что обязано найтись: если нет, структура репозитория изменилась и
# каталог не перезаписывается.
SOURCES = [
    {"repo": "wokwi/wokwi-elements", "ref": "main", "elements_dir": "src/", "elements_fallback": True,
     "expect": "elements"},
    {"repo": "wokwi/wokwi-boards", "ref": "main", "elements_dir": "src/", "elements_fallback": False,
     "expect": "boards"},
]

DEFAULT_OUTPUT = "wokwi_catalog.json"
REQUEST_TIMEOUT = 30          # секунд на один HTTP-запрос
MAX_ATTEMPTS = 4              # попыток на запрос при сетевых сбоях и ошибках 5xx
API_PAUSE = 0.5               # пауза перед каждым запросом к API, секунд

# Имя веб-компонента: строчные буквы, цифры и обязательный дефис ("wokwi-led")
_ELEMENT_NAME = r"([a-z][a-z0-9]*(?:-[a-z0-9]+)+)"
ELEMENT_PATTERNS = [
    re.compile(r"@customElement\(\s*(['\"`])" + _ELEMENT_NAME + r"\1\s*\)"),
    re.compile(r"customElements\.define\(\s*(['\"`])" + _ELEMENT_NAME + r"\1"),
]
BOARD_JSON = re.compile(r"(?:^|/)boards/([^/]+)/board\.json$")
ANY_BOARD_JSON = re.compile(r"(?:^|/)([^/]+)/board\.json$")
# Файлы, в которых элементы не регистрируются: тесты, истории Storybook, типы
SKIP_TS = re.compile(r"\.(?:d|stories|story|spec|test)\.ts$")
SKIP_DIRS = re.compile(r"(?:^|/)(?:node_modules|dist|build|coverage|__tests__)/")


class CatalogError(Exception):
    """Причина, по которой каталог нельзя обновить (без traceback)."""


# ═══════════════════════════════════════════════════════════════════════════
# GitHub
# ═══════════════════════════════════════════════════════════════════════════

class GitHub:
    """Запросы к GitHub с повторами, паузами и учётом лимита API."""

    def __init__(self, token=None, delay=0.05, max_wait=90, session=None):
        self.token = token
        self.delay = delay            # пауза между загрузками файлов
        self.max_wait = max_wait      # сколько готовы ждать сброса лимита, секунд
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = "update-wokwi-catalog"
        self.api_calls = 0
        self.raw_files = 0
        self.limit = self.remaining = self.reset = None

    # ── API ──────────────────────────────────────────────────────────────────
    def api(self, path, **params):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.remaining == 0:
            self._wait_for_reset()
        time.sleep(API_PAUSE)
        response = self._get(API_URL + path, headers, params, api=True)
        self.api_calls += 1
        return response.json()

    def raw(self, repo, ref, path):
        if self.raw_files and self.delay:
            time.sleep(self.delay)
        response = self._get("%s/%s/%s/%s" % (RAW_URL, repo, ref, path), {}, None, api=False)
        self.raw_files += 1
        return response.text

    def _get(self, url, headers, params, api):
        last_error = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = self.session.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                last_error = "сеть: %s" % exc
                time.sleep(2 ** attempt)
                continue
            if api:
                self._remember_limit(response)
            if response.status_code == 200:
                return response
            if api and response.status_code in (403, 429) and self._is_rate_limited(response):
                self._wait_for_reset(response)
                continue
            if response.status_code == 429:  # raw.githubusercontent.com просит притормозить
                last_error = "HTTP 429"
                time.sleep(min(_int(response.headers.get("Retry-After")) or 2 ** attempt, self.max_wait))
                continue
            if response.status_code >= 500:
                last_error = "HTTP %d" % response.status_code
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 404:
                raise NotFound(url)
            raise CatalogError("%s: HTTP %d %s" % (url, response.status_code, _short(response.text)))
        raise CatalogError("%s: не удалось получить после %d попыток (%s)" % (url, MAX_ATTEMPTS, last_error))

    # ── лимиты ───────────────────────────────────────────────────────────────
    def _remember_limit(self, response):
        h = response.headers
        if "X-RateLimit-Remaining" in h:
            self.limit = _int(h.get("X-RateLimit-Limit"))
            self.remaining = _int(h.get("X-RateLimit-Remaining"))
            self.reset = _int(h.get("X-RateLimit-Reset"))

    @staticmethod
    def _is_rate_limited(response):
        return (response.headers.get("X-RateLimit-Remaining") == "0" or "Retry-After" in response.headers
                or "rate limit" in response.text.lower())

    def _wait_for_reset(self, response=None):
        """Ждёт сброса лимита, если это недолго; иначе — понятная ошибка."""
        retry_after = _int(response.headers.get("Retry-After")) if response is not None else None
        if retry_after is not None:
            wait = retry_after
        elif self.reset:
            wait = max(0, self.reset - int(time.time())) + 1
        else:
            wait = 60
        if wait > self.max_wait:
            when = time.strftime("%H:%M", time.localtime(time.time() + wait))
            hint = ("" if self.token else
                    " Задайте токен в переменной окружения GITHUB_TOKEN — с ним лимит 5000 запросов в час.")
            raise CatalogError("Лимит запросов к API GitHub исчерпан (%s в час), сброс в %s.%s"
                               % (self.limit or "?", when, hint))
        print("  … лимит API GitHub исчерпан, жду %d с" % wait, flush=True)
        time.sleep(wait)
        self.remaining = None

    def limit_text(self):
        if self.remaining is None:
            return "лимит неизвестен"
        when = time.strftime("%H:%M", time.localtime(self.reset)) if self.reset else "?"
        return "осталось %d из %d, сброс в %s" % (self.remaining, self.limit or 0, when)


class NotFound(CatalogError):
    pass


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _short(text, size=200):
    text = " ".join((text or "").split())
    return text[:size] + ("…" if len(text) > size else "")


# ═══════════════════════════════════════════════════════════════════════════
# Разбор репозиториев
# ═══════════════════════════════════════════════════════════════════════════

def fetch_tree(gh, repo, ref):
    """(ветка, sha дерева, пути файлов). Если ветки нет — берёт ветку по умолчанию."""
    try:
        tree = gh.api("/repos/%s/git/trees/%s" % (repo, ref), recursive="1")
    except NotFound:
        ref = gh.api("/repos/%s" % repo).get("default_branch") or ref
        tree = gh.api("/repos/%s/git/trees/%s" % (repo, ref), recursive="1")
    if tree.get("truncated"):
        return ref, tree.get("sha", ""), _fetch_tree_by_dirs(gh, repo, ref)
    return ref, tree.get("sha", ""), _blob_paths(tree)


def _fetch_tree_by_dirs(gh, repo, ref):
    """Дерево слишком большое для одного ответа: обходим нужные папки по отдельности."""
    root = gh.api("/repos/%s/git/trees/%s" % (repo, ref))
    paths = _blob_paths(root)
    for entry in root.get("tree", []):
        if entry.get("type") == "tree" and entry.get("path") in ("src", "boards"):
            sub = gh.api("/repos/%s/git/trees/%s" % (repo, entry["sha"]), recursive="1")
            paths += [entry["path"] + "/" + p for p in _blob_paths(sub)]
    return paths


def _blob_paths(tree):
    return [e["path"] for e in tree.get("tree", []) if e.get("type") == "blob" and isinstance(e.get("path"), str)]


def element_files(paths, elements_dir, fallback):
    """Файлы .ts, в которых могут регистрироваться веб-компоненты."""
    def wanted(p):
        return p.endswith(".ts") and not SKIP_TS.search(p) and not SKIP_DIRS.search(p)
    files = [p for p in paths if p.startswith(elements_dir) and wanted(p)]
    if not files and fallback:
        files = [p for p in paths if wanted(p)]
    return sorted(files)


def extract_elements(source):
    """Имена веб-компонентов, зарегистрированных в тексте файла."""
    return {m.group(2) for rx in ELEMENT_PATTERNS for m in rx.finditer(source)}


def board_ids(paths):
    """Типы плат по папкам boards/<имя>/board.json (или <имя>/board.json, если boards/ переименуют)."""
    names = {m.group(1) for p in paths for m in [BOARD_JSON.search(p)] if m}
    if not names:
        names = {m.group(1) for p in paths for m in [ANY_BOARD_JSON.search(p)] if m}
    return {"board-" + n for n in names}


def scan_source(gh, source, progress=False):
    repo = source["repo"]
    print("  %s: дерево файлов…" % repo, flush=True)
    ref, tree_sha, paths = fetch_tree(gh, repo, source["ref"])
    files = element_files(paths, source["elements_dir"], source["elements_fallback"])
    elements = set()
    for i, path in enumerate(files, 1):
        if progress:
            print("\r  %s: файлы .ts %d/%d" % (repo, i, len(files)), end="", flush=True)
        elements |= extract_elements(gh.raw(repo, ref, path))
    if files and progress:
        print()
    return {"repo": repo, "ref": ref, "tree": tree_sha, "ts_files": len(files), "expect": source.get("expect"),
            "elements": elements, "boards": board_ids(paths)}


# ═══════════════════════════════════════════════════════════════════════════
# Каталог
# ═══════════════════════════════════════════════════════════════════════════

def read_catalog(path):
    """Список деталей прежнего каталога или None, если файла нет."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise CatalogError("%s нельзя прочитать (%s) — исправьте или удалите файл" % (path, exc))
    parts = data.get("parts") if isinstance(data, dict) else data
    if not isinstance(parts, list):
        raise CatalogError('%s: ожидался список или объект с полем "parts"' % path)
    return [p for p in parts if isinstance(p, str)]


def write_catalog(path, results, parts):
    data = {
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {r["repo"]: {"ref": r["ref"], "tree": r["tree"],
                                "parts": len(r["elements"] | r["boards"])} for r in results},
        "count": len(parts),
        "parts": parts,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def check_results(results):
    """Пустой результат там, где детали обязаны быть, почти наверняка значит, что структура
    репозитория изменилась: лучше не перезаписывать каталог, чем «удалить» из него всё."""
    for r in results:
        if r["expect"] == "elements" and not r["elements"]:
            raise CatalogError("%s: не найдено ни одного @customElement в %d файлах .ts — структура "
                               "репозитория изменилась? Каталог не перезаписан." % (r["repo"], r["ts_files"]))
        if r["expect"] == "boards" and not r["boards"]:
            raise CatalogError("%s: не найдено ни одного board.json — структура репозитория изменилась? "
                               "Каталог не перезаписан." % r["repo"])


# ═══════════════════════════════════════════════════════════════════════════
# Вывод
# ═══════════════════════════════════════════════════════════════════════════

class Style:
    def __init__(self, enabled):
        if enabled and os.name == "nt":
            os.system("")  # включает ANSI-последовательности в консоли Windows
        self.enabled = enabled

    def __call__(self, text, code):
        return "\033[%sm%s\033[0m" % (code, text) if self.enabled else text


def print_report(results, parts, previous, out_path, written, gh, style):
    line = "─" * 64
    print()
    print(style(" Каталог деталей Wokwi", "1"))
    print(line)
    for r in results:
        found = []
        if r["elements"]:
            found.append("веб-компонентов: %d (из %d файлов .ts)" % (len(r["elements"]), r["ts_files"]))
        if r["boards"]:
            found.append("плат: %d (board.json)" % len(r["boards"]))
        print(" %-22s %-6s %s" % (r["repo"], r["ref"], "; ".join(found) or "ничего не найдено"))
    print(line)
    print(" Всего в каталоге: %s" % style(str(len(parts)), "1"))
    if previous is None:
        print(" Прежнего каталога нет — все %d деталей новые" % len(parts))
    else:
        added = sorted(set(parts) - set(previous))
        removed = sorted(set(previous) - set(parts))
        print(" Новых: %s   Исчезло: %s   Без изменений: %d" % (
            style(str(len(added)), "32;1" if added else "0"),
            style(str(len(removed)), "31;1" if removed else "0"), len(set(parts) & set(previous))))
        for p in added:
            print("   " + style("+ " + p, "32"))
        for p in removed:
            print("   " + style("− " + p, "31"))
    print(line)
    print(" GitHub API: запросов %d, %s; файлов с raw.githubusercontent.com: %d"
          % (gh.api_calls, gh.limit_text(), gh.raw_files))
    if written:
        print(" Сохранено: %s" % out_path)
    elif previous is not None and set(previous) == set(parts):
        print(" Каталог не изменился — %s не перезаписан (--force, чтобы обновить метаданные)" % out_path)
    else:
        print(" --dry-run: %s не записан" % out_path)


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="Собирает каталог деталей Wokwi из репозиториев на GitHub.")
    ap.add_argument("-o", "--output", default=DEFAULT_OUTPUT, help="файл каталога (по умолчанию %(default)s)")
    ap.add_argument("--dry-run", action="store_true", help="только показать изменения, файл не записывать")
    ap.add_argument("--force", action="store_true", help="записать файл, даже если список не изменился")
    ap.add_argument("--delay", type=float, default=0.05, help="пауза между загрузками файлов, с (%(default)s)")
    ap.add_argument("--max-wait", type=int, default=90,
                    help="сколько ждать сброса лимита API, с; дольше — ошибка (%(default)s)")
    ap.add_argument("--no-color", action="store_true", help="без цвета в выводе")
    args = ap.parse_args(argv)

    out_path = os.path.abspath(args.output)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    gh = GitHub(token, args.delay, args.max_wait)
    interactive = sys.stdout.isatty()
    style = Style(interactive and not args.no_color and "NO_COLOR" not in os.environ)
    print("Источник: GitHub (%s)" % ("с токеном" if token else "без токена — 60 запросов API в час"))
    try:
        previous = read_catalog(out_path)
        results = [scan_source(gh, source, progress=interactive) for source in SOURCES]
        check_results(results)
    except CatalogError as exc:
        print("\nОшибка: %s" % exc, file=sys.stderr)
        return 1
    parts = sorted(set().union(*(r["elements"] | r["boards"] for r in results)))
    changed = previous is None or set(previous) != set(parts)
    written = not args.dry_run and (changed or args.force)
    if written:
        write_catalog(out_path, results, parts)
    print_report(results, parts, previous, out_path, written, gh, style)
    return 0


if __name__ == "__main__":
    sys.exit(main())
