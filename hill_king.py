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
Не совпало — один ответ в тред машины, своя копия не меняется.

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

Настройки — ~/.config/board-hill/config.json:
  {"computer": "<uuid поста машины>", "commit": "<коммит board-corewar>",
   "script_sha256": "<sha256 scripts/hill.sh на этом коммите>",
   "cw": "<путь к cw той же версии>", "topic": "general"}
Состояние и своя копия хилла — ~/.local/state/board-hill/.
Ключ — как у board.py (GPB_KEYFILE); не печатается.
"""
import base64
import gzip
import hashlib
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
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board  # noqa: E402

CONFIG = os.path.expanduser(os.environ.get("HILL_CONFIG", "~/.config/board-hill/config.json"))
STATE_DIR = os.path.expanduser(os.environ.get("HILL_STATE", "~/.local/state/board-hill"))
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
    """Команда, которую объявляет пост машины. Всё, что не она, — не прогон хилла."""
    return re.compile(
        r'^C=%s; curl -fsSLo hill\.sh https://raw\.githubusercontent\.com/geibos/board-corewar/\$C/scripts/hill\.sh'
        r' && echo "%s  hill\.sh" \| sha256sum -c - && bash hill\.sh challenge( [A-Za-z0-9._/-]+)+$'
        % (re.escape(cfg["commit"]), re.escape(cfg["script_sha256"]))
    )


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
        run(cfg, ["hill", "init", ours])
    if open(os.path.join(ours, "hill.toml")).read() != open(os.path.join(theirs, "hill.toml")).read():
        raise ValueError("правила на машине не те, что у нас (hill.toml изменён)")
    return work, ours, theirs


def enter(cfg, work, ours, theirs, challengers):
    """Одна заявка cw hill challenge на своей копии: те же претенденты под
    теми же именами файлов, исходники из снимка, хеш каждого сверен с id."""
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
    if files:
        run(cfg, ["hill", "challenge", ours] + files)


def compare(ours, theirs):
    a, b = load(os.path.join(ours, "state.json")), load(os.path.join(theirs, "state.json"))
    if members(a) != members(b):
        raise ValueError("состав или порядок хилла не совпал с повтором:\nу нас %s\nна машине %s"
                         % ([m[1] for m in members(a)], [m[1] for m in members(b)]))
    ra, rb = load(os.path.join(ours, "results.json")), load(os.path.join(theirs, "results.json"))
    if ra != rb:
        raise ValueError("результаты матчей не совпали с повтором")


def replay(cfg, text, hill):
    """Повторить прогон из вывода на копии своего хилла. Возвращает (копия,
    отчёт) при полном совпадении, иначе бросает ValueError с причиной."""
    report, snap = parse(text)
    work, ours, theirs = prepare(cfg, snap, hill)
    enter(cfg, work, ours, theirs, report["challengers"])
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


def sync_replay(cfg, snap, hill):
    """Догнать хилл машины на своей копии. Новых заявок столько, на сколько
    вырос счётчик next; какие и в каком порядке — из history.jsonl машины
    (ей не верим: неверный порядок или состав не сойдётся при сравнении).
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
        enter(cfg, work, ours, theirs, [c for i, c in tail if i == g])
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


def announce(cfg, j, hill):
    st = load(os.path.join(hill, "state.json"))["members"]
    king = st[0]
    actor = (j.get("actor") or {}).get("name") or "кто-то"
    body = (
        "На хилле Core War новый король: **%s** (%s).\n\n"
        "Вызов бросил %s (задание №%s на машине хилла, пост #%s).\n\n"
        "Верх таблицы:\n%s\n\n"
        "Этот прогон повторён независимо на сервере зеркала: состав, порядок и все матчи совпали. "
        "Проверить самому: `bash hill.sh verify` на машине."
        % (king["name"], king["author"], actor, j.get("number"), cfg.get("machine_seq", cfg["computer"]),
           table(os.path.join(hill, "state.json")))
    )
    title = "Новый король хилла Core War: %s" % king["name"]
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
    return {k: j.get(k) for k in ("job_id", "number", "actor", "started_at", "finished_at")}


def take(cfg, st, hill, copy, j, post):
    """Принять проверенную копию; сменился король — объявить от задания j."""
    if os.path.exists(hill):
        shutil.rmtree(hill)
    shutil.copytree(copy, hill)
    king = load(os.path.join(hill, "state.json"))["members"][0]["id"]
    print("задание %s: принято, король %s" % (j.get("number"), king))
    if king != st["king"] and post:
        r = announce(cfg, j, hill)
        print("  анонс:", r.get("id") or r)
    st["king"] = king
    st.pop("bad", None)


def author_of(cfg, t, pending):
    """Задание, во время которого записана строка истории со временем t."""
    for j in list(pending) + [brief(x) for x in jobs(cfg, 30)]:
        if t is not None and (j.get("started_at") or 0) - 5 <= t <= (j.get("finished_at") or 0) + 5:
            return j
    return pending[-1] if pending else {"job_id": "sync-%s" % t, "number": "?", "actor": None}


def sync(cfg, st, hill, post):
    """Прочитать хилл машины и догнать его. Сбой чтения — повтор через 30 минут."""
    pending = st.get("sync") or []
    try:
        jid, state, out = machine_job(SYNC_CMD, timeout=120)
        if state != "succeeded":
            raise ValueError("задание чтения %s: %s" % (jid, state))
        snap = parse_sync(out)
    except (board.BoardError, OSError, ValueError) as e:
        print("хилл машины не прочитан: %s; повтор через 30 минут" % e)
        st["sync_after"] = time.time() + 1800
        return
    key = digest(snap)
    try:
        got = sync_replay(cfg, snap, hill)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as e:
        print("хилл машины не сходится с повтором: %s" % e)
        if post and st.get("bad") != key:
            complain(cfg, pending, key, str(e))
        st["bad"] = key
        st["sync"] = []
        return
    st["sync"] = []
    if got is None:
        print("хилл машины совпадает с нашей копией")
        return
    copy, times = got
    take(cfg, st, hill, copy, author_of(cfg, times[-1], pending), post)


def once(post=True):
    cfg = config()
    st = state_load()
    hill = os.path.join(STATE_DIR, "hill")
    canon = canonical(cfg)
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
        if (j.get("command") or "") == SYNC_CMD:
            continue
        ok_cmd = canon.match(j.get("command") or "") and (j.get("cwd") or ".") in (".", "", "/workspace")
        # После любого другого задания хилл сверяется по самому хиллу, и все
        # задания за ним тоже: их вывод повторяется от состояния, которого у нас нет.
        if st["sync"] or j.get("state") != "succeeded" or not ok_cmd:
            st["sync"].append(brief(j))
            continue
        try:
            copy, _ = replay(cfg, output(cfg, jid), hill)
        except (ValueError, OSError, KeyError, json.JSONDecodeError) as e:
            print("задание %s: вывод не сошёлся (%s), сверим по хиллу машины" % (j.get("number"), e))
            st["sync"].append(brief(j))
            continue
        take(cfg, st, hill, copy, j, post)
        st["done"] = sorted(done)
        state_save(st)
    st["done"] = sorted(done)
    state_save(st)
    if st["sync"] and not busy and time.time() >= st.get("sync_after", 0):
        sync(cfg, st, hill, post)
        state_save(st)


def status():
    cfg = config()
    canon = canonical(cfg)
    st = state_load()
    print("король у нас:", st["king"])
    for item in sorted(jobs(cfg), key=lambda x: x.get("number") or 0):
        j = job(cfg, item["job_id"])
        cmd = j.get("command") or ""
        mark = "канон" if canon.match(cmd) else "чтение" if cmd == SYNC_CMD else "-"
        seen = "обработано" if item["job_id"] in st["done"] else "новое"
        print("№%s %s %s %s %s" % (j.get("number"), j.get("state"), mark, seen,
                                   (j.get("actor") or {}).get("name")))
    if st.get("sync"):
        print("ждут сверки по хиллу машины:", ", ".join("№%s" % p.get("number") for p in st["sync"]))


def machine_job(command, timeout=900):
    """Одна команда на машине от имени анонсера: взять управление, запустить
    машину, если стоит, выполнить, дождаться, отпустить. Возвращает (id
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
        sync(cfg, st, os.path.join(STATE_DIR, "hill"), post=True)
        state_save(st)
    elif cmd == "status":
        status()
    elif cmd in ("replay", "accept") and len(sys.argv) == 3:
        hill = os.path.join(STATE_DIR, "hill")
        copy, report = replay(config(), open(sys.argv[2]).read(), hill)
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
