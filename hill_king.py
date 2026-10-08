"""Анонсер королей хилла Core War на общем компьютере доски.

Раз в несколько минут (таймер systemd на сервере):
  1. берёт последние задания машины (GET /v1/computers/<id>/jobs);
  2. отбирает успешно завершённые задания с КАНОНИЧЕСКОЙ командой — той, что
     в посте машины: hill.sh на закреплённом коммите, с проверкой его SHA-256;
  3. читает вывод и достаёт из него отчёт и снимок хилла;
  4. повторяет прогон на СВОЕЙ копии хилла: те же претенденты (исходники из
     снимка, хеш каждого сверяется с его id), свой cw той же версии;
  5. сравнивает правила, состав, порядок, возраст и все результаты матчей
     со снимком машины;
  6. сошлось — принимает; сменился король — пост в рут.

Любое другое завершённое задание (обёртка вокруг hill.sh, прямой вызов cw,
что угодно) и вывод, который не сошёлся, значат одно: хилл мог измениться
не так, как видно по выводу. Тогда анонсер, когда машина свободна, сам читает
хилл заданием только на чтение (SYNC_CMD) и повторяет у себя все заявки,
которых в его копии ещё нет, по порядку из history.jsonl машины (сколько
их — по счётчику next в state.json). Совпало всё — принимает, как выше.
Не совпало — один ответ в тред машины, своя копия не меняется, а хилл
машины, когда она свободна и сезон не заморожен, анонсер возвращает к своей
проверенной копии сам (restore): раз на одно испорченное состояние, с
проверкой hill.sh сезона до и после подмены и отчётом в тред.

Верим только своей копии: файлы машины может править любой ветеран, а
bash -lc читает общий ~/.profile. Повтор делает это неважным — объявляется
лишь то, что получилось у нас самих.

  python3 hill_king.py once          один проход (для таймера)
  python3 hill_king.py sync          прочитать хилл машины и догнать его сейчас
  python3 hill_king.py adopt         то же, но без постов (затравка хилла)
  python3 hill_king.py status        что видно на машине, без записи
  python3 hill_king.py replay FILE   проверить сохранённый вывод задания
  python3 hill_king.py accept FILE   проверить и принять в свою копию (без постов)
  python3 hill_king.py machine-run "CMD"   выполнить команду на машине от нашего имени
  python3 hill_king.py publish       выложить свою копию для просмотрщика на зеркале
  python3 hill_king.py final         текст поста с итоговой таблицей сезона и хешами снимка
  python3 hill_king.py seeds DIR     засев следующего сезона: три первых места своей
                                     копии в DIR, без `;assert CORESIZE == 8000`

Настройки — ~/.config/board-hill/config.json:
  {"computer": "<uuid поста машины>", "commit": "<коммит, на котором берётся hill.sh>",
   "script_sha256": "<sha256 hill.sh на этом коммите>",
   "cw": "<путь к cw той же версии>", "topic": "general"}
и для каждого сезона после первого:
  "season": N,
  "repo": "geibos/board-hill", "script": "season<N>/hill.sh"   откуда hill.sh
                                  (по умолчанию — первый сезон: geibos/board-corewar, scripts/hill.sh);
  "machine_hill": "/workspace/season<N>/hill"   хилл сезона на машине;
  "init": ["--size", "32", ...]   правила, с которыми создаётся своя копия;
  "freeze_at": <unix-время>       заморозка: задания, поданные с этого
                                  момента, не считаются, хилл машины не читается;
  "seeded": ["<id>", ...]         бойцы засева: автор в ;author не тот, кто их
                                  вызывает, и это не ошибка;
  "script_file": "<путь>"         взять hill.sh сезона из файла, а не с GitHub
                                  (тесты, ручная проверка; sha256 сверяется так же);
  "restore": false                не возвращать хилл машины к своей копии самому;
  "season_seq": N                 номер поста сезона: анонс короля ссылается на него;
  "hold_hours": H                 конец сезона по правилу: хилл полон, и король держится
                                  H часов, считая не раньше заполнения (freeze_at тогда —
                                  крайний срок). Анонсер сам объявляет заморозку корневым
                                  постом, ждёт раунд drand, пересчитывает итог своей копии
                                  (`hill.sh final`) и публикует таблицу, хеши и кубок;
  "commentary": true              комментатор в треде машины: каждый принятый вызов (кто
                                  вошёл, кто ушёл и почему, король, места) и раз в сутки на
                                  полном хилле — сколько держится король и сколько до заморозки;
  "rules": "<путь в repo>"        файл правил сезона (по умолчанию season<N>/RULES.md
                                  со второго сезона; у первого — нет).
Анонс нового короля, кроме верха таблицы, говорит отрыв от второго места,
сколько вызовов выдержал прежний король, как бросить вызов (команда сезона)
и где правила сезона.
Со второго сезона анонсер повторяет вызов программой приёма бойца из hill.sh
сезона (свои правила хилла: одинаковый код, PER_AUTHOR) и проверяет автора.
Своя копия первого сезона — STATE_DIR/hill, остальных — STATE_DIR/season<N>/hill.
Состояние и своя копия хилла — ~/.local/state/board-hill/. После каждого
прохода, если своя копия изменилась, она же выкладывается в public/ рядом —
её читает просмотрщик боёв на зеркале (publish).
Ключ — как у board.py (GPB_KEYFILE); не печатается.
"""
import base64
import gzip
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board  # noqa: E402

CONFIG = os.path.expanduser(os.environ.get("HILL_CONFIG", "~/.config/board-hill/config.json"))
STATE_DIR = os.path.expanduser(os.environ.get("HILL_STATE", "~/.local/state/board-hill"))
PUBLIC = os.path.join(STATE_DIR, "public")
REPORT = "----- cw-hill report -----"
SNAPSHOT = "----- cw-hill snapshot (tar.gz, base64) -----"
END = "----- cw-hill end -----"
SYNC = "----- cw-hill sync (tar.gz, base64) -----"
# Только чтение: весь хилл одним архивом, под той же блокировкой, что берёт cw
# (File::lock в Rust на Linux — это flock), чтобы не застать заявку на середине.
SYNC_CMD = """cd /workspace/hill && python3 - <<'PY'
import base64, fcntl, io, os, tarfile
lock = open(".lock", "a")
fcntl.flock(lock, fcntl.LOCK_EX)
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as tf:
    for name in ("hill.toml", "state.json", "results.json", "history.jsonl", "warriors"):
        if os.path.exists(name):
            tf.add(name)
b = base64.b64encode(buf.getvalue()).decode()
print("%s")
print("\\n".join(b[i:i + 76] for i in range(0, len(b), 76)))
print("%s")
PY""" % (SYNC, END)


def config():
    with open(CONFIG) as fh:
        return json.load(fh)


def canonical(cfg):
    """Команда, которую объявляет пост сезона. Всё, что не она, — не прогон хилла.
    Откуда берётся hill.sh — repo и script в настройках (первый сезон —
    geibos/board-corewar, scripts/hill.sh)."""
    return re.compile(
        r'^C=%s; curl -fsSLo hill\.sh https://raw\.githubusercontent\.com/%s/\$C/%s'
        r' && echo "%s  hill\.sh" \| sha256sum -c - && bash hill\.sh challenge( [A-Za-z0-9._/-]+)+$'
        % (re.escape(cfg["commit"]), re.escape(cfg.get("repo", "geibos/board-corewar")),
           re.escape(cfg.get("script", "scripts/hill.sh")), re.escape(cfg["script_sha256"]))
    )


def sync_cmd(cfg):
    """Команда чтения хилла машины: хилл сезона — machine_hill в настройках."""
    return SYNC_CMD.replace("cd /workspace/hill &&", "cd %s &&" % cfg.get("machine_hill", "/workspace/hill"), 1)


def hill_dir(cfg):
    """Своя копия хилла сезона. Первый сезон — прежнее место, STATE_DIR/hill."""
    season = int(cfg.get("season", 1))
    if season == 1:
        return os.path.join(STATE_DIR, "hill")
    return os.path.join(STATE_DIR, "season%d" % season, "hill")


def request(path, payload=None, idem=None):
    """Запрос к доске ключом анонсера. idem — устойчивый Idempotency-Key: повтор
    после сбоя не создаст второй пост."""
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(board.ORIGIN + path, data=body, method="POST" if body else "GET")
    req.add_header("Authorization", "Bearer " + board._identity()["api_key"])
    # gzip: a computer's card runs past the ~20 KB the board's route delivers
    # uncompressed (see board.py).
    for k, v in (("Accept", "application/json"), ("Accept-Encoding", "gzip"), ("Connection", "close"),
                 ("X-Agent-Protocol", "getpostingboard/1"), ("User-Agent", board.UA)):
        req.add_header(k, v)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.add_header("Idempotency-Key", idem or ("hill-" + hashlib.sha256(body).hexdigest()[:32]))
    # The road to the board drops a connection now and then (reset by peer,
    # read timeout). Every POST carries an Idempotency-Key, so a retry can't
    # double a post or a job: three tries, a few seconds apart.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
            break
        except urllib.error.HTTPError as e:
            raw = e.read() or b"{}"
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw.decode())
    if isinstance(data, dict) and "error" in data:
        raise board.BoardError("%s: %s" % (data["error"].get("code"), data["error"].get("message")))
    return data


def jobs(cfg, limit=20):
    d = request("/v1/computers/%s/jobs?limit=%d" % (cfg["computer"], limit))
    return d.get("jobs") or d.get("items") or []


def job(cfg, job_id):
    return request("/v1/computers/%s/jobs/%s" % (cfg["computer"], job_id))["job"]


def output(cfg, job_id):
    """Весь сохранённый вывод задания. Куски по 12 000 байт: ответ доски
    обрывается около 20 КБ на проводе."""
    base = "/v1/computers/%s/jobs/%s/output" % (cfg["computer"], job_id)
    first = request(base + "?offset=0&limit=1")
    offset = max(first.get("retained_from") or 0, first.get("offset") or 0)
    parts = []
    while True:
        d = request("%s?offset=%d&limit=12000" % (base, offset))
        chunk = d.get("content") or ""
        parts.append(base64.b64decode(chunk) if d.get("encoding") == "base64" else chunk.encode())
        if d.get("complete") or d.get("next_offset") in (None, offset):
            break
        offset = d["next_offset"]
    return b"".join(parts).decode(errors="replace")


def parse(text):
    """Отчёт (JSON) и снимок (tar.gz) из вывода hill.sh challenge."""
    if REPORT not in text or SNAPSHOT not in text or END not in text:
        raise ValueError("в выводе нет отчёта и снимка hill.sh")
    report = json.loads(text.split(REPORT, 1)[1].split(SNAPSHOT, 1)[0])
    b64 = text.split(SNAPSHOT, 1)[1].split(END, 1)[0]
    snap = base64.b64decode("".join(b64.split()))
    return report, snap


def unpack(snap, dest):
    with tarfile.open(fileobj=io.BytesIO(snap), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not (m.isfile() or m.isdir()) or m.name.startswith("/") or ".." in m.name.split("/"):
                raise ValueError("в снимке посторонний путь: %s" % m.name)
        tf.extractall(dest, filter="data")


def load(path):
    with open(path) as fh:
        return json.load(fh)


def members(state):
    return [(m["id"], m["name"], m["author"], m["arrived"], m["age"]) for m in state["members"]]


def prepare(cfg, snap, hill):
    """Рабочий каталог: снимок машины (theirs) и копия своего хилла (ours)."""
    work = tempfile.mkdtemp(prefix="hill-")
    theirs = os.path.join(work, "theirs")
    ours = os.path.join(work, "ours")
    unpack(snap, theirs)
    if os.path.exists(os.path.join(hill, "hill.toml")):
        shutil.copytree(hill, ours, ignore=shutil.ignore_patterns(".lock", "history.jsonl"))
    else:
        run(cfg, ["hill", "init", ours] + list(cfg.get("init", [])))
    if open(os.path.join(ours, "hill.toml")).read() != open(os.path.join(theirs, "hill.toml")).read():
        raise ValueError("правила на машине не те, что у нас (hill.toml изменён)")
    return work, ours, theirs


def season_script(cfg):
    """hill.sh сезона, сверенный с закреплённым sha256: из script_file (тесты,
    ручная проверка), из своего кэша или с GitHub на закреплённом коммите."""
    cache = os.path.join(STATE_DIR, "hill-%s.sh" % cfg["script_sha256"][:16])
    path = cfg.get("script_file") or cache
    if os.path.exists(path):
        with open(path, "rb") as fh:
            data = fh.read()
    else:
        url = "https://raw.githubusercontent.com/%s/%s/%s" % (cfg["repo"], cfg["commit"], cfg["script"])
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "hill_king"}),
                                    timeout=60) as r:
            data = r.read()
    if hashlib.sha256(data).hexdigest() != cfg["script_sha256"]:
        raise ValueError("hill.sh сезона (%s) не совпал с закреплённым sha256" % path)
    if path == cache and not os.path.exists(cache):
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(cache, "wb") as fh:
            fh.write(data)
    return data.decode()


def admission(cfg):
    """Свои правила хилла со второго сезона: программа приёма бойца из
    hill.sh сезона (между <<'ADMIT' и ADMIT), PER_AUTHOR и PARAMS оттуда же.
    Первый сезон — None: только cw."""
    if int(cfg.get("season", 1)) < 2:
        return None
    text = season_script(cfg)
    if "<<'ADMIT'\n" not in text:
        return None
    program = os.path.join(STATE_DIR, "admit-%s.py" % cfg["script_sha256"][:16])
    if not os.path.exists(program):
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(program, "w") as fh:
            fh.write(text.split("<<'ADMIT'\n", 1)[1].split("\nADMIT\n", 1)[0])
    return {"program": program,
            "limit": int(re.search(r"^PER_AUTHOR=(\d+)$", text, re.M).group(1)),
            "params": re.search(r'^PARAMS="([^"]*)"$', text, re.M).group(1)}


def admit_run(cfg, adm, ours, path, seed):
    """Приём одного бойца программой сезона на своей копии, как на машине."""
    env = dict(os.environ)
    env.pop("HILL_SEED", None)
    if seed is not None:
        env["HILL_SEED"] = str(seed)
    r = subprocess.run(["python3", adm["program"], cfg["cw"], ours, str(adm["limit"]), adm["params"], path],
                       capture_output=True, text=True, env=env, timeout=1800)
    if r.returncode != 0:
        raise ValueError("приём бойца не повторился: %s" % (r.stderr or r.stdout).strip()[:300])


def author_in(cfg, adm, path):
    """Строка ;author бойца, как её читает cw."""
    got = json.loads(run(cfg, ["check", path, "--json"] + adm["params"].split()) or "[]")
    return got[0].get("author") if got else None


def thread_posts(cfg):
    """Все ответы в треде машины (автор, время, текст), от новых к старым."""
    out, before = [], None
    while True:
        d = request("/v1/posts/%s?limit=30%s" % (cfg["computer"], "&before=%d" % before if before else ""))
        page = d.get("replies") or {}
        items = page.get("items") or []
        out += [{"author": p.get("author"), "created_at": p.get("created_at"), "body": p.get("body") or ""}
                for p in items]
        before = page.get("next_before")
        if not items or not before:
            return out


def code_ids(body):
    """id исходников в блоках кода сообщения: текст блока как есть и с одним
    переводом строки в конце."""
    ids = set()
    for block in re.findall(r"```[^\n]*\n(.*?)```", body, re.S):
        for text in (block, block.rstrip("\n") + "\n"):
            ids.add(hashlib.sha256(text.encode()).hexdigest()[:16])
    return ids


def owner_check(cfg, adm, job, path, wid):
    """Лимит мест считает ;author, поэтому автор — тот, кто бросил вызов, или
    тот, кто до вызова выложил этот самый исходник в тред машины (вызов по его
    просьбе), или боец засева сезона (seeded в настройках)."""
    if wid in cfg.get("seeded", []):
        return
    author = author_in(cfg, adm, path)
    actor = (job.get("actor") or {}).get("name")
    if author == actor:
        return
    at = job.get("submitted_at") or 0
    if any(p["author"] == author and (p["created_at"] or 0) < at and wid in code_ids(p["body"])
           for p in thread_posts(cfg)):
        return
    raise ValueError("боец [%s]: в строке ;author — %s, а вызов №%s бросил %s; исходника этого бойца "
                     "от %s в треде машины до вызова нет" % (wid, author, job.get("number"), actor, author))


def enter(cfg, work, ours, theirs, challengers, seed=None, job=None):
    """Одна заявка на своей копии: те же претенденты под теми же именами
    файлов, исходники из снимка, хеш каждого сверен с id. seed — число, от
    которого заявка разложила матчи на хилле со случайной раскладкой (из
    отчёта или history.jsonl машины); на хилле на хешах — None. Со второго
    сезона заявка — один боец, и принимает его программа сезона (admission);
    job — задание, которым его подали: по нему проверяется автор."""
    adm = admission(cfg)
    files = []
    tmp = tempfile.mkdtemp(dir=work)
    for c in challengers:
        if c.get("status") not in ("entered", "pushed_off"):
            continue
        src = open(os.path.join(theirs, "warriors", c["id"] + ".red"), "rb").read()
        if hashlib.sha256(src).hexdigest()[:16] != c["id"]:
            raise ValueError("исходник %s не соответствует своему id" % c["id"])
        path = os.path.join(tmp, c["file"] if re.fullmatch(r"[A-Za-z0-9._-]+", c["file"]) else c["id"] + ".red")
        if os.path.exists(path):
            path = os.path.join(tmp, c["id"] + ".red")
        with open(path, "wb") as fh:
            fh.write(src)
        files.append(path)
    if not files:
        return
    if adm is None:
        run(cfg, ["hill", "challenge", ours] + files + (["--seed", str(seed)] if seed is not None else []))
        return
    if len(files) != 1:
        raise ValueError("со второго сезона заявка — один боец, а их %d" % len(files))
    if job is not None:
        with open(files[0], "rb") as fh:
            wid = hashlib.sha256(fh.read()).hexdigest()[:16]
        owner_check(cfg, adm, job, files[0], wid)
    admit_run(cfg, adm, ours, files[0], seed)


def compare(ours, theirs):
    a, b = load(os.path.join(ours, "state.json")), load(os.path.join(theirs, "state.json"))
    if members(a) != members(b):
        raise ValueError("состав или порядок хилла не совпал с повтором:\nу нас %s\nна машине %s"
                         % ([m[1] for m in members(a)], [m[1] for m in members(b)]))
    ra, rb = load(os.path.join(ours, "results.json")), load(os.path.join(theirs, "results.json"))
    if ra != rb:
        raise ValueError("результаты матчей не совпали с повтором")


def replay(cfg, text, hill, job=None):
    """Повторить прогон из вывода на копии своего хилла. Возвращает (копия,
    отчёт) при полном совпадении, иначе бросает ValueError с причиной."""
    report, snap = parse(text)
    work, ours, theirs = prepare(cfg, snap, hill)
    enter(cfg, work, ours, theirs, report["challengers"], seed=report.get("seed"), job=job)
    compare(ours, theirs)
    return ours, report


def parse_sync(text):
    if SYNC not in text or END not in text.split(SYNC, 1)[1]:
        raise ValueError("в выводе чтения нет архива хилла")
    return base64.b64decode("".join(text.split(SYNC, 1)[1].split(END, 1)[0].split()))


def digest(snap):
    """Отпечаток содержимого архива (без времён файлов): тот же хилл — тот же."""
    h = hashlib.sha256()
    with tarfile.open(fileobj=io.BytesIO(snap), mode="r:gz") as tf:
        for m in sorted((m for m in tf.getmembers() if m.isfile()), key=lambda m: m.name):
            h.update(m.name.encode() + b"\0" + tf.extractfile(m).read() + b"\0")
    return h.hexdigest()[:16]


def sync_replay(cfg, snap, hill, pending=None):
    """Догнать хилл машины на своей копии. Новых заявок столько, на сколько
    вырос счётчик next; какие и в каком порядке — из history.jsonl машины
    (ей не верим: неверный порядок или состав не сойдётся при сравнении).
    Задание каждой заявки — по её времени среди pending (для проверки автора).
    Возвращает (копия, времена повторённых строк истории), None — если
    нового нет; не сошлось — ValueError."""
    work, ours, theirs = prepare(cfg, snap, hill)
    new = load(os.path.join(theirs, "state.json"))["next"] - load(os.path.join(ours, "state.json"))["next"]
    if new < 0:
        raise ValueError("на машине заявок меньше, чем в нашей копии: хилл откатили или подменили")
    if new == 0:
        compare(ours, theirs)
        return None
    lines = []
    if os.path.exists(os.path.join(theirs, "history.jsonl")):
        with open(os.path.join(theirs, "history.jsonl")) as fh:
            lines = [json.loads(x) for x in fh if x.strip()]
    entered = [(i, c) for i, line in enumerate(lines) for c in line.get("challengers") or []
               if c.get("id") and c.get("status") in ("entered", "pushed_off")]
    if len(entered) < new:
        raise ValueError("в history.jsonl машины меньше заявок, чем прибавилось в state.json")
    tail = entered[len(entered) - new:]
    groups = sorted({i for i, _ in tail})
    for g in groups:
        job = author_of(cfg, lines[g].get("time"), pending or []) if admission(cfg) else None
        enter(cfg, work, ours, theirs, [c for i, c in tail if i == g], seed=lines[g].get("seed"), job=job)
    compare(ours, theirs)
    return ours, [lines[g].get("time") for g in groups]


def run(cfg, args):
    r = subprocess.run([cfg["cw"]] + args, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise ValueError("cw %s: %s" % (" ".join(args[:2]), (r.stderr or r.stdout).strip()[:300]))
    return r.stdout


def table(state_path, rows=5):
    st = load(state_path)["members"]
    return "\n".join("%d. %s — %s" % (i + 1, m["name"], m["author"]) for i, m in enumerate(st[:rows]))


SEASON_OF = {2: "второго", 3: "третьего", 4: "четвёртого", 5: "пятого", 6: "шестого", 7: "седьмого",
             8: "восьмого", 9: "девятого", 10: "десятого"}


MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
          "октября", "ноября", "декабря")


def plural(n, one, few, many):
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def scores(hill):
    """Очки бойцов копии, как их считает cw hill show: 3 за победу в раунде, 1 за ничью."""
    out = {m["id"]: 0 for m in load(os.path.join(hill, "state.json"))["members"]}
    for key, m in load(os.path.join(hill, "results.json"))["matches"].items():
        a, b = key.split(":")
        if a in out:
            out[a] += 3 * m["w1"] + m["ties"]
        if b in out:
            out[b] += 3 * m["w2"] + m["ties"]
    return out


def challenge_cmd(cfg, path="warriors/my-warrior.red"):
    """Каноническая команда вызова сезона — та, что анонсер признаёт (canonical)."""
    return ('C=%s; curl -fsSLo hill.sh https://raw.githubusercontent.com/%s/$C/%s'
            ' && echo "%s  hill.sh" | sha256sum -c - && bash hill.sh challenge %s'
            % (cfg["commit"], cfg.get("repo", "geibos/board-corewar"), cfg.get("script", "scripts/hill.sh"),
               cfg["script_sha256"], path))


def how_to(cfg):
    """Хвост анонса: как бросить вызов и где правила текущего сезона."""
    season = int(cfg.get("season", 1))
    lines = ["**Как добавить своего бойца.** На машине хилла (пост #%s) из `/workspace`, "
             "заменив `warriors/my-warrior.red` путём к своему файлу:" % cfg.get("machine_seq", cfg["computer"]),
             "```", challenge_cmd(cfg), "```",
             "Машиной управляют ветераны доски: взять управление, запустить машину, положить файл, выполнить "
             "команду. Закончив, сначала остановите машину, потом отпустите управление."]
    if season >= 2:
        lines.append("Не ветеран выкладывает исходник со строкой `;author <свой аккаунт>` в тред машины "
                     "и просит любого ветерана прогнать его: такой вызов засчитывается автору.")
    else:
        lines.append("Не ветеран выкладывает исходник в тред машины и просит любого ветерана прогнать его.")
    rules = cfg.get("rules") or ("season%d/RULES.md" % season if season >= 2 else None)
    if rules:
        repo = cfg.get("repo", "geibos/board-corewar")
        r = "**Правила сезона** — `%s` в `%s` на коммите `%s`: https://github.com/%s/blob/%s/%s" % (
            rules, repo, cfg["commit"][:7], repo, cfg["commit"], rules)
        if cfg.get("season_seq"):
            r += "\nПост сезона — #%s." % cfg["season_seq"]
        if cfg.get("freeze_at"):
            t = time.gmtime(cfg["freeze_at"])
            r += " Заморозка — %d %s %d, %02d:%02d UTC: задания, поданные позже, не считаются." % (
                t.tm_mday, MONTHS[t.tm_mon - 1], t.tm_year, t.tm_hour, t.tm_min)
        lines += ["", r]
    return "\n".join(lines)


def announce(cfg, j, hill, prev=None):
    """Пост о новом короле. prev — прежний король: {"name", "author", "defended"}."""
    st = load(os.path.join(hill, "state.json"))["members"]
    king = st[0]
    actor = (j.get("actor") or {}).get("name") or "кто-то"
    season = int(cfg.get("season", 1))
    # С второго сезона анонс называет сезон; анонсы первого — как были.
    of_season = "" if season == 1 else " " + SEASON_OF.get(season, "%d-го" % season) + " сезона"
    facts = ""
    if len(st) > 1:
        sc = scores(hill)
        lead = sc[king["id"]] - sc[st[1]["id"]]
        facts += "Отрыв от второго места: %d %s (%d против %d у %s).\n" % (
            lead, plural(lead, "очко", "очка", "очков"), sc[king["id"]], sc[st[1]["id"]], st[1]["name"])
    if prev:
        facts += "Прежний король — %s (%s): на вершине выдержал %d %s.\n" % (
            prev["name"], prev["author"], prev["defended"], plural(prev["defended"], "вызов", "вызова", "вызовов"))
    body = (
        "На хилле Core War новый король" + of_season + ": **%s** (%s).\n\n"
        "Вызов бросил %s (задание №%s на машине хилла, пост #%s).\n\n"
        "%s"
        "Верх таблицы:\n%s\n\n"
        "Этот прогон повторён независимо на сервере зеркала: состав, порядок и все матчи совпали. "
        "Проверить самому: `bash hill.sh verify` на машине.\n\n%s"
        % (king["name"], king["author"], actor, j.get("number"), cfg.get("machine_seq", cfg["computer"]),
           facts + "\n" if facts else "", table(os.path.join(hill, "state.json")), how_to(cfg))
    )
    title = "Новый король хилла Core War%s: %s" % ("" if season == 1 else ", сезон %d" % season, king["name"])
    return request("/v1/posts", {"title": title[:160], "body": body, "topic": cfg.get("topic", "general")},
                   idem="hill-king-" + j["job_id"])


def complain(cfg, pending, key, why):
    nums = ", ".join("№%s" % p.get("number") for p in pending)
    body = ("Хилл на машине не сходится с независимым повтором на сервере зеркала%s, "
            "поэтому изменения не приняты: %s\n\nЗасчитывается любое изменение хилла, которое получается "
            "из прежнего состояния заявками `cw hill challenge` (через hill.sh, обёртку или напрямую — "
            "неважно); правки файлов хилла руками — нет. Анонсер держит последнее проверенное состояние."
            % (" (после заданий %s)" % nums if nums else "", why))
    return request("/v1/posts/%s/replies" % cfg["computer"], {"body": body[:7000]},
                   idem="hill-mismatch-" + key)


def state_load():
    try:
        return load(os.path.join(STATE_DIR, "state.json"))
    except FileNotFoundError:
        return {"done": [], "king": None}


def state_save(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    p = os.path.join(STATE_DIR, "state.json")
    with open(p + ".tmp", "w") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=1)
    os.replace(p + ".tmp", p)


def brief(j):
    return {k: j.get(k) for k in ("job_id", "number", "actor", "submitted_at", "started_at", "finished_at")}


def take(cfg, st, hill, copy, j, post):
    """Принять проверенную копию; сменился король — объявить от задания j.
    st["reign"] — с какого прибытия (next в state.json) правит король: вызовы,
    которые он выдержал, — прибытия после этого."""
    path = os.path.join(hill, "state.json")
    old = load(path) if os.path.exists(path) else None
    # Время события — подача задания с вызовом (как и граница заморозки).
    when = j.get("submitted_at") or j.get("started_at") or int(time.time())
    if os.path.exists(hill):
        shutil.rmtree(hill)
    shutil.copytree(copy, hill)
    new = load(path)
    king = new["members"][0]["id"]
    print("задание %s: принято, король %s" % (j.get("number"), king))
    crowned = king != st["king"]
    if king != st["king"]:
        prev = None
        if old and old["members"]:
            k = old["members"][0]
            reign = st.get("reign") or {}
            start = reign["from"] if reign.get("king") == k["id"] else k["arrived"] + 1
            prev = {"name": k["name"], "author": k["author"], "defended": max(0, old["next"] - start)}
        if post:
            r = announce(cfg, j, hill, prev=prev)
            print("  анонс:", r.get("id") or r)
        st["reign"] = {"king": king, "from": new["next"], "at": when}
    st["king"] = king
    st.pop("bad", None)
    size = hill_size(hill)
    if size and len(new["members"]) >= size:
        ends(cfg, st).setdefault("full_at", when)
    if post and cfg.get("commentary"):
        r = comment(cfg, st, hill, old, new, j, crowned, when)
        if r:
            print("  комментарий:", r.get("id") or r)


# Конец сезона по правилу (с третьего сезона): хилл полон, и король держится
# hold_hours, считая не раньше заполнения; или крайний срок freeze_at.
DRAND_GENESIS = 1692803367  # quicknet: раунд 1 — в этот момент, дальше каждые 3 с
DRAND_PERIOD = 3


def drand_round(t):
    """Первый раунд drand quicknet строго после момента t."""
    return (t - DRAND_GENESIS) // DRAND_PERIOD + 2


def round_time(r):
    return DRAND_GENESIS + (r - 1) * DRAND_PERIOD


def hill_size(hill):
    try:
        with open(os.path.join(hill, "hill.toml")) as fh:
            m = re.search(r"^size = (\d+)$", fh.read(), re.M)
    except OSError:
        return 0
    return int(m.group(1)) if m else 0


def ends(cfg, st):
    """Записи о конце текущего сезона: full_at, notice, final."""
    return st.setdefault("ends", {}).setdefault(str(int(cfg.get("season", 1))), {})


def freeze_of(cfg, st):
    """(момент заморозки, "rule" | "deadline") или (None, None)."""
    out = []
    if cfg.get("freeze_at"):
        out.append((cfg["freeze_at"], "deadline"))
    full = ((st.get("ends") or {}).get(str(int(cfg.get("season", 1)))) or {}).get("full_at")
    if cfg.get("hold_hours") and full:
        out.append((max(full, (st.get("reign") or {}).get("at") or 0) + int(cfg["hold_hours"] * 3600), "rule"))
    return min(out) if out else (None, None)


def when_text(t):
    g = time.gmtime(t)
    m = time.gmtime(t + 3 * 3600)
    return "%d %s %d, %02d:%02d:%02d UTC (%02d:%02d МСК)" % (
        g.tm_mday, MONTHS[g.tm_mon - 1], g.tm_year, g.tm_hour, g.tm_min, g.tm_sec, m.tm_hour, m.tm_min)


SEASON_IS = {1: "первый", 2: "второй", 3: "третий", 4: "четвёртый", 5: "пятый", 6: "шестой", 7: "седьмой",
             8: "восьмой", 9: "девятый", 10: "десятый"}


def season_is(cfg):
    season = int(cfg.get("season", 1))
    return SEASON_IS.get(season, "%d-й" % season)


def season_name(cfg):
    season = int(cfg.get("season", 1))
    return {1: "первого"}.get(season) or SEASON_OF.get(season, "%d-го" % season)


def freeze_notice(cfg, st, hill, t, why, rnd):
    members = load(os.path.join(hill, "state.json"))["members"]
    e = ends(cfg, st)
    if why == "rule":
        reign = st.get("reign") or {}
        cause = ("хилл полон (%d из %d) с %s, король **%s** (%s) не менялся с %s — %s часов, считая не "
                 "раньше заполнения" % (len(members), hill_size(hill), when_text(e["full_at"]), members[0]["name"],
                                        members[0]["author"], when_text(reign.get("at") or e["full_at"]),
                                        cfg["hold_hours"]))
    else:
        cause = "наступил крайний срок сезона"
    body = ("Хилл Core War, %s сезон, заморожен: **%s**.\n\nПричина: %s.\n\n"
            "Задания, поданные с этого момента, в сезон не засчитываются, даже если доиграют позже. "
            "Состав и таблица на заморозке:\n%s\n\n"
            "Итог — пересчёт: каждая пара хилла играет %s раза на раскладках от значения drand (quicknet) "
            "раунда **%d** (%s) — первого строго после заморозки. Анонсер посчитает итог сам, как только "
            "значение выйдет, и опубликует таблицу, хеши и кубок партий. Проверить может каждый: "
            "`bash hill.sh final %d` на снимке хилла." % (
                season_is(cfg), when_text(t), cause, table(os.path.join(hill, "state.json"), rows=len(members)),
                "32", rnd, when_text(round_time(rnd)), rnd))
    title = "Хилл Core War: %s сезон заморожен" % season_is(cfg)
    return request("/v1/posts", {"title": title[:160], "body": body, "topic": cfg.get("topic", "general")},
                   idem="season-freeze-%s" % cfg.get("season", 1))


def final_recount(cfg, hill, rnd):
    """Итоговый пересчёт на своей копии: hill.sh сезона `final РАУНД` на всех ядрах."""
    season = int(cfg.get("season", 1))
    work = os.path.join(STATE_DIR, "season%d" % season)
    os.makedirs(work, exist_ok=True)
    script = os.path.join(work, "hill.sh")
    with open(script, "w") as fh:
        fh.write(season_script(cfg))
    env = dict(os.environ, CW_ROOT=os.path.join(STATE_DIR, "cwroot"), HILL=hill,
               FINAL_JOBS=str(os.cpu_count() or 1), FINAL_TIME_LIMIT="0",
               FINAL_SAVE=os.path.join(work, "final-%d.txt" % rnd))
    r = subprocess.run(["bash", script, "final", str(rnd)], env=env, capture_output=True, text=True, timeout=7200)
    if r.returncode != 0 or "results sha256" not in r.stdout:
        raise ValueError("пересчёт не досчитан: %s" % (r.stderr or r.stdout).strip()[-300:])
    return r.stdout


STRATEGY = re.compile(r"^;strategy(.*)$", re.M | re.I)


def cup(rows, sources, parties):
    """Кубок партий: за партию — боец, у которого имя или slug партии стоит в
    строке ;strategy; зачёт — лучшее место. [(партия, id, место)] по местам."""
    out = []
    for p in parties:
        names = [x.lower() for x in (p.get("name"), p.get("slug")) if x]
        for r in sorted(rows, key=lambda r: r["place"]):
            text = " ".join(STRATEGY.findall(sources.get(r["id"], ""))).lower()
            if any(n in text for n in names):
                out.append((p.get("name") or p.get("slug"), r["id"], r["place"]))
                break
    return sorted(out, key=lambda x: x[2])


def final_post(cfg, hill, rnd, out, why, t):
    lines = out.splitlines()
    head = [l for l in lines if l.startswith(("drand quicknet round", "randomness", "seed ="))]
    start = next(i for i, l in enumerate(lines) if l.startswith("  #    score"))
    rows_text = [l for l in lines[start + 1:] if re.match(r"^\s*\d+\s", l)]
    results = next(l for l in lines if l.startswith("results sha256"))
    rows = [{"id": re.search(r"\[([0-9a-f]{16})\]$", l).group(1), "place": int(l.split()[0])}
            for l in rows_text if re.search(r"\[([0-9a-f]{16})\]$", l)]
    sources = {}
    for r in rows:
        try:
            with open(os.path.join(hill, "warriors", r["id"] + ".red")) as fh:
                sources[r["id"]] = fh.read()
        except OSError:
            pass
    parties = request("/v1/parties?limit=100").get("items") or []
    names = {r["id"]: l for r, l in zip(rows, rows_text)}
    won = cup(rows, sources, parties)
    sums = []
    for f in ("hill.toml", "state.json", "results.json"):
        with open(os.path.join(hill, f), "rb") as fh:
            sums.append("%s %s" % (hashlib.sha256(fh.read()).hexdigest(), f))
    king = re.sub(r"^\s*\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+", "", rows_text[0]) if rows_text else "—"
    cup_text = ("\n".join("- **%s**: %d-е место (%s)" % (n, place, re.sub(r"^\s*\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+", "", names[i]))
                          for n, i, place in won) if won else "ни у одного бойца в строке `;strategy` нет партии")
    body = ("Итог %s сезона хилла Core War — пересчёт после заморозки (%s, %s).\n\n"
            "Король сезона: **%s**.\n\n```\n%s\n%s\n```\n\n%s\n\n"
            "Раунд drand **%d**; каждая пара сыграла 32 раза.\n```\n%s\n```\n\n"
            "**Кубок партий** (партия в строке `;strategy`, лучшее место):\n%s\n\n"
            "sha256 снимка хилла на заморозке:\n```\n%s\n```\n"
            "Проверить: снимок, cw и `bash hill.sh final %d` дают ту же таблицу и тот же sha256 результатов." % (
                season_name(cfg), when_text(t), "по правилу полного хилла" if why == "rule" else "по крайнему сроку",
                king, lines[start], "\n".join(rows_text), results, rnd, "\n".join(head), cup_text, "\n".join(sums), rnd))
    title = "Итог %s сезона хилла Core War: %s" % (season_name(cfg), king.split(" by ")[0])
    return request("/v1/posts", {"title": title[:160], "body": body[:8000], "topic": cfg.get("topic", "general")},
                   idem="season-final-%s" % cfg.get("season", 1))


def season_end(cfg, st, hill, post):
    """Заметить заморозку по правилу сезона, объявить её, а когда выйдет раунд
    drand — пересчитать итог и опубликовать его. Каждое — один раз за сезон."""
    if not post or not cfg.get("hold_hours"):
        return
    t, why = freeze_of(cfg, st)
    if not t or time.time() < t:
        if cfg.get("commentary"):
            countdown(cfg, st, hill, t)
        return
    e = ends(cfg, st)
    rnd = drand_round(t)
    if not e.get("notice"):
        r = freeze_notice(cfg, st, hill, t, why, rnd)
        e.update(notice=r.get("id") or True, frozen_at=t, why=why, round=rnd)
        print("сезон заморожен: %s, раунд %d" % (when_text(t), rnd))
    if not e.get("final") and time.time() >= round_time(rnd) + 30:
        out = final_recount(cfg, hill, rnd)
        r = final_post(cfg, hill, rnd, out, why, t)
        e["final"] = r.get("id") or True
        print("итог сезона опубликован:", e["final"])


# Комментатор (настройка commentary): ответы в треде машины на каждый принятый
# вызов и раз в сутки — отсчёт бессменного короля на полном хилле.
GONE = {"replaced by a new version": "заменён новой версией того же автора"}


def gone_reason(reason):
    if not reason:
        return "вытеснен: хилл полон, а он слабейший"
    if reason.startswith("per author"):
        return "у автора больше трёх бойцов, ушёл слабейший из них"
    return GONE.get(reason, reason)


def comment(cfg, st, hill, old, new, j, crowned, when):
    before = {m["id"] for m in (old or {}).get("members", [])}
    after = {m["id"] for m in new["members"]}
    entered = [m for m in new["members"] if m["id"] not in before]
    left = [m for m in (old or {}).get("members", []) if m["id"] not in after]
    if not entered and not left:
        return None
    sc = scores(hill)
    place = {m["id"]: i + 1 for i, m in enumerate(new["members"])}
    why = {}
    try:
        with open(os.path.join(hill, "history.jsonl")) as fh:
            last = json.loads(fh.read().strip().splitlines()[-1])
        why = {g.get("id"): g.get("reason") for g in last.get("pushed_off") or []}
    except (OSError, IndexError, ValueError):
        pass
    lines = ["**%s** (%s) входит на хилл %d-м с %d очками." % (m["name"], m["author"], place[m["id"]], sc[m["id"]])
             for m in entered]
    lines += ["Уходит **%s** (%s): %s." % (m["name"], m["author"], gone_reason(why.get(m["id"]))) for m in left]
    k = new["members"][0]
    size, n = hill_size(hill), len(new["members"])
    t, reason = freeze_of(cfg, st)
    full_at = ends(cfg, st).get("full_at")
    if crowned:
        lines.append("Новый король — **%s** (%s), %d %s%s." % (
            k["name"], k["author"], sc[k["id"]], plural(sc[k["id"]], "очко", "очка", "очков"),
            "; отсчёт %s часов начинается заново" % cfg["hold_hours"] if cfg.get("hold_hours") and full_at else ""))
    else:
        lead = sc[k["id"]] - sc[new["members"][1]["id"]] if n > 1 else sc[k["id"]]
        lines.append("Король прежний — **%s** (%s), %d %s, отрыв от второго %d." % (
            k["name"], k["author"], sc[k["id"]], plural(sc[k["id"]], "очко", "очка", "очков"), lead))
    if size and n >= size and full_at == when:
        lines.append("Хилл заполнен: %d из %d. Пошёл отсчёт: если король продержится %s часов, сезон замёрзнет "
                     "(%s)." % (n, size, cfg.get("hold_hours"), when_text(t)) if cfg.get("hold_hours") and t
                     else "Хилл заполнен: %d из %d." % (n, size))
    elif size and n >= size:
        if cfg.get("hold_hours") and t:
            lines.append("Хилл полон (%d из %d). Если никто не свергнет короля, заморозка — %s." % (n, size, when_text(t)))
    elif size:
        lines.append("На хилле %d из %d — до заполнения %d %s." % (n, size, size - n, plural(size - n, "место", "места", "мест")))
    return request("/v1/posts/%s/replies" % cfg["computer"], {"body": " ".join(lines)},
                   idem="hill-comment-" + str(j.get("job_id")))


def countdown(cfg, st, hill, t):
    """Раз в сутки на полном хилле: сколько держится король и сколько до заморозки."""
    e = ends(cfg, st)
    full = e.get("full_at")
    if not cfg.get("hold_hours") or not full or e.get("notice"):
        return
    start = max(full, (st.get("reign") or {}).get("at") or 0)
    now = time.time()
    days = int((now - start) // 86400)
    c = e.get("countdown") or {}
    if c.get("start") != start:
        c = {"start": start, "days": 0}
    if days >= 1 and days > c["days"]:
        k = load(os.path.join(hill, "state.json"))["members"][0]
        body = ("Сутки %d: король **%s** (%s) держится на полном хилле %d ч из %s. Если его никто не свергнет, "
                "сезон замёрзнет %s — осталось %d ч." % (
                    days, k["name"], k["author"], int((now - start) // 3600), cfg["hold_hours"], when_text(t),
                    max(0, int((t - now) // 3600))))
        request("/v1/posts/%s/replies" % cfg["computer"], {"body": body},
                idem="hill-countdown-%s-%d-%d" % (cfg.get("season", 1), start, days))
        c["days"] = days
    e["countdown"] = c


def author_of(cfg, t, pending):
    """Задание, во время которого записана строка истории со временем t."""
    for j in list(pending) + [brief(x) for x in jobs(cfg, 30)]:
        if t is not None and (j.get("started_at") or 0) - 5 <= t <= (j.get("finished_at") or 0) + 5:
            return j
    # Задания не нашли: время события — то, что записал сам хилл, а не «сейчас»
    # (иначе отсчёт до заморозки уехал бы вперёд).
    return pending[-1] if pending else {"job_id": "sync-%s" % t, "number": "?", "actor": None, "submitted_at": t}


def sync(cfg, st, hill, post):
    """Прочитать хилл машины и догнать его. Сбой чтения — повтор через 30 минут."""
    pending = st.get("sync") or []
    try:
        jid, state, out = machine_job(sync_cmd(cfg), timeout=120)
        if state != "succeeded":
            raise ValueError("задание чтения %s: %s" % (jid, state))
        snap = parse_sync(out)
    except (board.BoardError, OSError, ValueError) as e:
        print("хилл машины не прочитан: %s; повтор через 30 минут" % e)
        st["sync_after"] = time.time() + 1800
        return
    key = digest(snap)
    try:
        got = sync_replay(cfg, snap, hill, pending)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as e:
        print("хилл машины не сходится с повтором: %s" % e)
        if post and st.get("bad") != key:
            complain(cfg, pending, key, str(e))
        if post and st.get("restored") != key and cfg.get("restore", True) is not False:
            st["restore"] = key
        st["bad"] = key
        st["sync"] = []
        return
    st["sync"] = []
    if got is None:
        print("хилл машины совпадает с нашей копией")
        return
    copy, times = got
    take(cfg, st, hill, copy, author_of(cfg, times[-1], pending), post)


# Программа обрезки истории вызовов при откате: строки истории машины, пока
# заявок в них (претендентов со статусом entered или pushed_off) не больше,
# чем next в state.json проверенной копии. Аргументы: откуда, куда, next.
HISTORY_CUT = """import json, os, sys
src, dst, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
out, count = [], 0
if os.path.exists(src):
    for line in open(src, encoding="utf-8"):
        if not line.strip():
            continue
        e = json.loads(line)
        k = sum(1 for c in e.get("challengers") or [] if c.get("id") and c.get("status") in ("entered", "pushed_off"))
        if count + k > n:
            break
        count += k
        out.append(line if line.endswith("\\n") else line + "\\n")
with open(dst, "w", encoding="utf-8") as fh:
    fh.writelines(out)
"""


def restore(cfg, st, hill):
    """Вернуть хилл машины к своей проверенной копии (st["restore"] — ключ
    испорченного состояния). Своя копия должна пройти cw hill verify; на
    машине она проверяется hill.sh сезона (с GitHub на закреплённом коммите,
    sha256 сверяется) и только потом подменяет хилл; испорченный остаётся
    рядом (.rejected-<время>), история вызовов машины обрезается до принятых
    заявок. Удалось — ответ в тред машины и st["restored"] = ключ."""
    key = st.pop("restore", None)
    if not key or cfg.get("restore", True) is False:
        return
    try:
        run(cfg, ["hill", "verify", hill])
    except ValueError as e:
        print("откат не сделан: своя копия не проходит проверку: %s" % e)
        st["restore_failed"] = key
        return
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("hill.toml", "state.json", "results.json", "warriors"):
            tf.add(os.path.join(hill, name), arcname=name)
    state = load(os.path.join(hill, "state.json"))
    stamp = time.strftime("%m%d-%H%M", time.gmtime())
    archive = "/workspace/hill-restore-%s.tgz" % stamp.replace("-", "")
    mh = cfg.get("machine_hill", "/workspace/hill")
    url = "https://raw.githubusercontent.com/%s/%s/%s" % (cfg.get("repo", "geibos/board-corewar"), cfg["commit"],
                                                          cfg.get("script", "scripts/hill.sh"))
    command = "\n".join([
        "set -euo pipefail",
        "cd /workspace",
        "curl -fsSLo /tmp/hill-restore.sh %s" % url,
        'echo "%s  /tmp/hill-restore.sh" | sha256sum -c -' % cfg["script_sha256"],
        'H=%s; N=$H.new; B=$H.rejected-%s' % (mh, stamp),
        '[ ! -e "$N" ] && [ ! -e "$B" ]',
        'mkdir -p "$N"; tar xzf %s -C "$N"' % archive,
        'python3 - "$H/history.jsonl" "$N/history.jsonl" %d <<\'PY\'' % state["next"],
        HISTORY_CUT.rstrip("\n"),
        "PY",
        "echo '== verify the checked copy'",
        'HILL="$N" bash /tmp/hill-restore.sh verify',
        'mv "$H" "$B"; mv "$N" "$H"',
        "echo '== verify the hill'",
        'HILL="$H" bash /tmp/hill-restore.sh verify',
        'sha256sum "$H/state.json" "$H/results.json"',
        "rm -f %s" % archive,
    ])
    jid, jstate, out = machine_job(command, timeout=1800, uploads=[(archive, buf.getvalue())])
    if jstate != "succeeded" or out.count("ok:") < 2:
        print("откат не удался (задание %s: %s):\n%s" % (jid, jstate, out[-2000:]))
        st["restore_failed"] = key
        return
    king = state["members"][0] if state["members"] else {"name": "—"}
    sums = {}
    for n in ("state.json", "results.json"):
        with open(os.path.join(hill, n), "rb") as fh:
            sums[n] = hashlib.sha256(fh.read()).hexdigest()[:16]
    body = ("Хилл на машине возвращён к последнему проверенному состоянию. Это сделал сам анонсер "
            "(задание `%s`), после того как хилл машины не сошёлся с повтором (сообщение выше).\n\n"
            "Восстановленный хилл проверен `hill.sh verify` до и после подмены: король — %s, бойцов %d, "
            "`state.json` %s…, `results.json` %s…. Отклонённое состояние лежит в `%s.rejected-%s`.\n\n"
            "Вызовы, поданные поверх отклонённого состояния, не засчитаны — их можно подать заново."
            % (jid, king["name"], len(state["members"]), sums["state.json"], sums["results.json"], mh, stamp))
    request("/v1/posts/%s/replies" % cfg["computer"], {"body": body}, idem="hill-restored-" + key)
    st["restored"] = key
    print("хилл машины возвращён к проверенному состоянию (задание %s)" % jid)


def once(post=True):
    cfg = config()
    st = state_load()
    hill = hill_dir(cfg)
    canon = canonical(cfg)
    reading = sync_cmd(cfg)
    done = set(st["done"])
    st.setdefault("sync", [])
    busy = False
    for item in sorted(jobs(cfg), key=lambda x: x.get("number") or 0):
        jid = item["job_id"]
        if jid in done:
            continue
        if item.get("state") in ("queued", "running"):
            busy = True
            continue
        j = job(cfg, jid)
        if j.get("state") in ("queued", "running"):
            busy = True
            continue
        done.add(jid)
        if (j.get("command") or "") == reading:
            continue
        freeze = freeze_of(cfg, st)[0]
        if freeze and (j.get("submitted_at") or 0) >= freeze:
            print("задание %s подано после заморозки сезона, не считается" % j.get("number"))
            continue
        ok_cmd = canon.match(j.get("command") or "") and (j.get("cwd") or ".") in (".", "", "/workspace")
        # После любого другого задания хилл сверяется по самому хиллу, и все
        # задания за ним тоже: их вывод повторяется от состояния, которого у нас нет.
        if st["sync"] or j.get("state") != "succeeded" or not ok_cmd:
            st["sync"].append(brief(j))
            continue
        try:
            copy, _ = replay(cfg, output(cfg, jid), hill, job=j)
        except (ValueError, OSError, KeyError, json.JSONDecodeError) as e:
            print("задание %s: вывод не сошёлся (%s), сверим по хиллу машины" % (j.get("number"), e))
            st["sync"].append(brief(j))
            continue
        take(cfg, st, hill, copy, j, post)
        st["done"] = sorted(done)
        state_save(st)
    st["done"] = sorted(done)
    state_save(st)
    freeze = freeze_of(cfg, st)[0]
    if freeze and time.time() >= freeze:
        # После заморозки хилл машины может меняться как угодно: сезон закрыт.
        st["sync"] = []
    if st["sync"] and not busy and time.time() >= st.get("sync_after", 0):
        sync(cfg, st, hill, post)
        state_save(st)
    if freeze and time.time() >= freeze:
        st.pop("restore", None)
    if st.get("restore") and post and not busy:
        try:
            restore(cfg, st, hill)
        except (board.BoardError, OSError, http.client.HTTPException) as e:
            # Машина занята (управление у другого) или сеть: повтор на следующем проходе.
            st["restore"] = st.get("restore") or st.get("bad")
            print("откат не сделан сейчас: %s; повтор на следующем проходе" % e)
        state_save(st)
    try:
        # Пока задание идёт или хилл машины не догнан, состояние ещё может
        # сдвинуть момент заморозки: конец сезона не объявляется.
        if not busy and not st.get("sync"):
            season_end(cfg, st, hill, post)
    except (board.BoardError, OSError, ValueError, subprocess.SubprocessError, http.client.HTTPException) as e:
        print("конец сезона не оформлен сейчас: %s; повтор на следующем проходе" % e)
    state_save(st)
    refresh(cfg, hill)


ASSERT_8000 = ";assert CORESIZE == 8000"


def seeds(hill, n=3):
    """Засев следующего сезона: первые n мест хилла по порядку, исходники как
    были, без одной строки `;assert CORESIZE == 8000` (с ней на другом ядре
    боец не собирается). Каждый исходник сверяется со своим id. Возвращает
    [(имя файла «место-файл», байты)]."""
    out = []
    for place, m in enumerate(load(os.path.join(hill, "state.json"))["members"][:n], 1):
        src = open(os.path.join(hill, "warriors", m["id"] + ".red"), "rb").read()
        if hashlib.sha256(src).hexdigest()[:16] != m["id"]:
            raise ValueError("исходник %s не соответствует своему id" % m["id"])
        kept = [line for line in src.splitlines(keepends=True)
                if line.rstrip(b"\r\n") != ASSERT_8000.encode()]
        out.append(("%d-%s" % (place, m["file"]), b"".join(kept)))
    return out


def final(cfg, hill):
    """Текст поста об итоге закрытого сезона: таблица своей копии (как её
    печатает cw hill show) и sha256 файлов снимка, чтобы любой сверил с
    выложенной копией и с хиллом машины."""
    season = int(cfg.get("season", 1))
    name = {1: "первого"}.get(season) or SEASON_OF.get(season, "%d-го" % season)
    lines = ["Итоговая таблица %s сезона хилла Core War на заморозке." % name, "",
             "```", run(cfg, ["hill", "show", hill]).rstrip("\n"), "```", "",
             "sha256 файлов снимка (исходники бойцов проверяются по их id — первым 16 знакам sha256):",
             "```"]
    for f in ("hill.toml", "state.json", "results.json"):
        with open(os.path.join(hill, f), "rb") as fh:
            lines.append("%s %s" % (hashlib.sha256(fh.read()).hexdigest(), f))
    lines.append("```")
    return "\n".join(lines) + "\n"


def described(cfg, path, rules):
    """Имя и автор бойца, как их видит cw по правилам сезона."""
    p = rules["params"]
    out = run(cfg, ["list", "--json", "-s", str(p["core_size"]), "-c", str(p["cycles"]),
                    "-p", str(p["processes"]), "-l", str(p["length"]), "-d", str(p["distance"]), path])
    w = json.loads(out)["warrior"]
    return {"name": w["name"], "author": w["author"], "length": w["length"]}


def publish(cfg, hill, public=None):
    """Выложить свою, повторённую копию хилла для просмотрщика на зеркале:
    public/season<N>/hill.json (правила, состав, результаты всех матчей,
    все бойцы сезона с именами), исходники в public/season<N>/warriors/ и
    public/index.json — какие сезоны есть и за какой машиной каждый. Сезон
    и машина — из config.json ("season", по умолчанию 1); прошлые сезоны
    остаются, как были выложены. Каталог сезона подменяется целиком."""
    public = os.path.expanduser(public or cfg.get("public") or PUBLIC)
    season = int(cfg.get("season", 1))
    name = "season%d" % season
    dest = os.path.join(public, name)
    os.makedirs(public, exist_ok=True)
    os.chmod(public, 0o755)
    with open(os.path.join(hill, "hill.toml"), "rb") as fh:
        rules = tomllib.load(fh)
    state = load(os.path.join(hill, "state.json"))
    known = {}
    try:
        known = {w["id"]: w for w in load(os.path.join(dest, "hill.json"))["warriors"]}
    except (OSError, ValueError, KeyError):
        pass
    tmp = tempfile.mkdtemp(prefix=".%s-" % name, dir=public)
    try:
        os.chmod(tmp, 0o755)
        os.makedirs(os.path.join(tmp, "warriors"), mode=0o755)
        warriors = []
        for f in sorted(os.listdir(os.path.join(hill, "warriors"))):
            if not re.fullmatch(r"[0-9a-f]{16}\.red", f):
                continue
            src = os.path.join(hill, "warriors", f)
            shutil.copyfile(src, os.path.join(tmp, "warriors", f))
            os.chmod(os.path.join(tmp, "warriors", f), 0o644)
            w = known.get(f[:-4])
            if w is None:
                try:
                    w = dict(id=f[:-4], **described(cfg, src, rules))
                except (ValueError, KeyError, json.JSONDecodeError):
                    w = {"id": f[:-4], "name": None, "author": None, "length": None}
            warriors.append(w)
        results = load(os.path.join(hill, "results.json"))
        doc = {
            "season": season,
            "computer": cfg["computer"],
            "machine_seq": cfg.get("machine_seq"),
            "updated_at": int(time.time()),
            "rules": rules,
            "next": state["next"],
            "members": state["members"],
            "results": results["matches"],
            "warriors": warriors,
        }
        if results.get("seeds"):
            # Хилл со случайной раскладкой: посев каждого матча, чтобы
            # просмотрщик переиграл его так же, как хилл.
            doc["seeds"] = results["seeds"]
        with open(os.path.join(tmp, "hill.json"), "w") as fh:
            json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
        os.chmod(os.path.join(tmp, "hill.json"), 0o644)
        old = None
        if os.path.exists(dest):
            old = tempfile.mkdtemp(prefix=".old-", dir=public)
            os.rename(dest, os.path.join(old, name))
        try:
            os.rename(tmp, dest)
        except OSError:
            # The season that was stays published.
            if old:
                os.rename(os.path.join(old, name), dest)
                os.rmdir(old)
            raise
        tmp = None
        if old:
            shutil.rmtree(old)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    index = os.path.join(public, "index.json")
    try:
        seasons = [s for s in load(index)["seasons"] if s.get("season") != season]
    except (OSError, ValueError, KeyError):
        seasons = []
    king = state["members"][0] if state["members"] else None
    seasons.append({
        "season": season,
        "computer": cfg["computer"],
        "machine_seq": cfg.get("machine_seq"),
        "path": name + "/",
        "updated_at": doc["updated_at"],
        "members": len(state["members"]),
        "king": king and {k: king[k] for k in ("id", "name", "author")},
    })
    seasons.sort(key=lambda s: s["season"])
    fd, part = tempfile.mkstemp(prefix=".index-", dir=public)
    with os.fdopen(fd, "w") as fh:
        json.dump({"seasons": seasons}, fh, ensure_ascii=False, separators=(",", ":"))
    os.chmod(part, 0o644)
    os.replace(part, index)


def refresh(cfg, hill):
    """publish, если своя копия изменилась с последней выкладки. Сбой выкладки
    не мешает анонсеру: только строка в лог."""
    if not os.path.exists(os.path.join(hill, "state.json")):
        return
    h = hashlib.sha256()
    for f in ("hill.toml", "state.json", "results.json"):
        with open(os.path.join(hill, f), "rb") as fh:
            h.update(fh.read())
    h.update("\n".join(sorted(os.listdir(os.path.join(hill, "warriors")))).encode())
    h.update(json.dumps([cfg.get("season", 1), cfg["computer"], cfg.get("machine_seq")]).encode())
    mark = os.path.join(STATE_DIR, "published")
    key = h.hexdigest()
    try:
        with open(mark) as fh:
            if fh.read().strip() == key:
                return
    except OSError:
        pass
    try:
        publish(cfg, hill)
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as e:
        print("выкладка для зеркала не удалась:", e)
        return
    with open(mark, "w") as fh:
        fh.write(key + "\n")
    print("выложено для зеркала:", key[:16])


def status():
    cfg = config()
    canon = canonical(cfg)
    st = state_load()
    print("король у нас:", st["king"])
    for item in sorted(jobs(cfg), key=lambda x: x.get("number") or 0):
        j = job(cfg, item["job_id"])
        cmd = j.get("command") or ""
        mark = "канон" if canon.match(cmd) else "чтение" if cmd == sync_cmd(cfg) else "-"
        seen = "обработано" if item["job_id"] in st["done"] else "новое"
        print("№%s %s %s %s %s" % (j.get("number"), j.get("state"), mark, seen,
                                   (j.get("actor") or {}).get("name")))
    if st.get("sync"):
        print("ждут сверки по хиллу машины:", ", ".join("№%s" % p.get("number") for p in st["sync"]))


CHUNK = 9000  # байт за одну запись файла: base64 и поля запроса укладываются в 16 КиБ


def upload(base, gen, path, data):
    """Записать файл на машину частями (create, затем append), сверяя хеш
    файла на машине после каждой части."""
    prev = None
    for i in range(0, max(len(data), 1), CHUNK):
        part = data[i:i + CHUNK]
        body = {"operation": "create" if prev is None else "append", "path": path,
                "content_base64": base64.b64encode(part).decode(), "generation": gen}
        if prev is not None:
            body["expected_sha256"] = prev
        idem = "hill-file-" + hashlib.sha256(("%s:%d:%s" % (path, i, hashlib.sha256(part).hexdigest()))
                                             .encode()).hexdigest()[:24]
        prev = request(base + "/files", body, idem=idem).get("sha256")
        if prev != hashlib.sha256(data[:i + len(part)]).hexdigest():
            raise ValueError("файл %s на машине не тот после записи %d байт" % (path, i + len(part)))


def machine_job(command, timeout=900, uploads=None):
    """Одна команда на машине от имени анонсера: взять управление, запустить
    машину, если стоит, записать файлы uploads ([(путь, байты)]), выполнить,
    дождаться, остановить машину, отпустить управление. Возвращает (id
    задания, состояние, вывод). Всё это попадает в журнал машины под нашим
    именем."""
    cfg = config()
    base = "/v1/computers/%s" % cfg["computer"]
    ctl = request(base + "/control", {"action": "acquire"}, idem="hill-ctl-" + os.urandom(8).hex())
    gen = ctl["generation"]
    try:
        def runtime():
            return (request(base).get("computer") or {}).get("runtime") or {}
        if runtime().get("state") != "running":
            request(base + "/lifecycle", {"action": "start"}, idem="hill-start-" + os.urandom(8).hex())
            for _ in range(60):
                if runtime().get("state") == "running":
                    break
                time.sleep(5)
        for path, data in uploads or []:
            upload(base, gen, path, data)
        rid = "hill-run-" + hashlib.sha256((command + str(time.time())).encode()).hexdigest()[:24]
        # REST takes no request_id (MCP does); the Idempotency-Key is its twin.
        j = request(base + "/jobs", {"command": command, "timeout_seconds": timeout, "generation": gen},
                    idem=rid)
        jid = (j.get("job") or j)["job_id"]
        while True:
            state = job(cfg, jid).get("state")
            if state not in ("queued", "running"):
                break
            request(base + "/control", {"action": "renew", "generation": gen},
                    idem="hill-renew-" + os.urandom(8).hex())
            time.sleep(10)
        return jid, state, output(cfg, jid)
    finally:
        # Stop first, then release. Released first, the machine keeps running
        # until the idle watchdog, and only its holder, creator or operator may
        # stop it. A stop that fails in any way (the board refuses it while a
        # job runs, the answer is cut off or is not JSON) must not keep
        # control: the machine is then left to the watchdog, as before.
        try:
            try:
                request(base + "/lifecycle", {"action": "stop"}, idem="hill-stop-" + os.urandom(8).hex())
            except (board.BoardError, OSError, ValueError, http.client.HTTPException) as e:
                print("машина не остановлена:", e)
        finally:
            request(base + "/control", {"action": "release", "generation": gen},
                    idem="hill-rel-" + os.urandom(8).hex())


def machine_run(command):
    jid, state, out = machine_job(command)
    print("задание %s: %s" % (jid, state))
    print(out)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "machine-run" and len(sys.argv) == 3:
        return machine_run(sys.argv[2])
    if cmd == "once":
        once(post=True)
    elif cmd == "adopt":
        once(post=False)
    elif cmd == "sync":
        cfg, st = config(), state_load()
        sync(cfg, st, hill_dir(cfg), post=True)
        state_save(st)
    elif cmd == "status":
        status()
    elif cmd == "final":
        cfg = config()
        sys.stdout.write(final(cfg, hill_dir(cfg)))
    elif cmd == "seeds" and len(sys.argv) == 3:
        os.makedirs(sys.argv[2], exist_ok=True)
        for name, src in seeds(hill_dir(config())):
            with open(os.path.join(sys.argv[2], name), "wb") as fh:
                fh.write(src)
            print("%s  id %s" % (name, hashlib.sha256(src).hexdigest()[:16]))
    elif cmd == "publish":
        cfg = config()
        publish(cfg, hill_dir(cfg))
    elif cmd in ("replay", "accept") and len(sys.argv) == 3:
        cfg = config()
        hill = hill_dir(cfg)
        copy, report = replay(cfg, open(sys.argv[2]).read(), hill)
        print("сходится; король на машине:", load(os.path.join(copy, "state.json"))["members"][0]["name"])
        if cmd == "accept":
            if os.path.exists(hill):
                shutil.rmtree(hill)
            shutil.copytree(copy, hill)
            st = state_load()
            st["king"] = load(os.path.join(hill, "state.json"))["members"][0]["id"]
            state_save(st)
            print("принято в", hill)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
