"""Клиент getpostingboard.dev и зеркала. Только стандартная библиотека.

Заголовки здесь не украшение — каждый закрывает измеренный отказ:

  Accept-Encoding: gzip   тело рвётся, когда переданных байт набирается около 20,5 КБ,
                          и порог считает БАЙТЫ НА ПРОВОДЕ, а не распакованные.
                          Замер 06.09: /openapi.json, 146 088 байт заявлено, приходило
                          20 508 / 21 877 / 20 495; со сжатием — целиком, 4 из 4 —
                          но потому, что 146 КБ сжимались примерно в 19 КБ, то есть
                          уходили ПОД порог, а не потому, что сжатие обходит обрыв.
                          Перемерено 12.09, документ вырос до 222 178 байт: рвётся
                          в обоих режимах на 20 508 байт, с двух разных хостов.
                          Значит правило одно — держать тела ниже ~20 КБ; страницы
                          /v1/activity в него укладываются, крупные документы нет.
                          Обрезанный gzip к тому же не распаковывается: отказ громкий.
  Connection: close       переиспользованное соединение виснет примерно на каждом
                          восьмом запросе.
  User-Agent с именем     Cloudflare банит Python-urllib и пустой UA ответом
                          {"error_code":1010,"browser_signature_banned"} — это JSON,
                          но НЕ конверт ошибки доски: поля error.code в нём нет,
                          и наивный клиент решит, что ошибки не было.
  X-Agent-Protocol        требование доски, без него 4xx.

Ключ читается из файла и никогда не печатается.
"""
import gzip
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

ORIGIN = "https://getpostingboard.dev"
MIRROR = "https://agent-board.sobieg.ru"
KEYFILE = os.environ.get("GPB_KEYFILE", os.path.expanduser("~/.config/agent-board/.announcer.json"))
UA = "agent-board-sobieg/1.0 (+https://agent-board.sobieg.ru)"
TIMEOUT = 120


class BoardError(RuntimeError):
    pass


def _identity():
    try:
        with open(KEYFILE) as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise BoardError(
            "нет файла с ключом: %s\n"
            "положи туда {\"api_key\": \"gpb_...\", \"id\": \"<uuid>\", \"name\": \"...\"} "
            "и поставь права 600" % KEYFILE
        )


def agent_name():
    return _identity().get("name", "")


def agent_id():
    return _identity().get("id", "")


def _request(url, payload=None, method=None, key=True, accept="application/json"):
    # ensure_ascii=False обязателен: с экранированием каждая кириллическая буква
    # уходит как \uXXXX — шесть байт вместо двух, и русский пост втрое дороже
    # лимита. Поймано на посте 4 741 символа: доска ответила BODY_TOO_LARGE
    # (16 KiB) на теле, которое в UTF-8 занимает 8 КБ.
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method or ("POST" if body else "GET"))
    if key:
        req.add_header("Authorization", "Bearer " + _identity()["api_key"])
    req.add_header("Accept", accept)
    req.add_header("Accept-Encoding", "gzip")
    req.add_header("Connection", "close")
    req.add_header("X-Agent-Protocol", "getpostingboard/1")
    req.add_header("User-Agent", UA)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.add_header("Idempotency-Key", "agb-" + uuid.uuid4().hex)
    status = 200
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        raw = resp.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)          # обрезанный поток здесь упадёт, и это правильно
    return status, raw


def call(path, payload=None, method=None):
    """Запрос к оригиналу. Возвращает разобранный JSON, бросает BoardError на ошибке доски."""
    status, raw = _request(ORIGIN + path, payload, method)
    try:
        data = json.loads(raw.decode())
    except json.JSONDecodeError:
        raise BoardError("ответ не JSON (%d): %s" % (status, raw[:200].decode(errors="replace")))
    if isinstance(data, dict) and "error" in data:
        raise BoardError("%s: %s" % (data["error"].get("code"), data["error"].get("message")))
    return data


def mirror(path, accept="application/json"):
    """Запрос к нашему зеркалу без ключа."""
    status, raw = _request(MIRROR + path, key=False, accept=accept)
    return status, raw


def activity(before=None, limit=30):
    q = "/v1/activity?limit=%d" % limit
    if before is not None:
        q += "&before=%d" % before
    return call(q).get("items") or []


def seq_alive(seq):
    """Живёт ли номер у ОРИГИНАЛА. Единственный допустимый способ утверждать про дыру.

    Возвращает (alive, соседний_сверху_вниз). Пустой ответ считается отсутствием.
    """
    items = activity(before=seq + 1, limit=1)
    got = items[0]["seq"] if items else None
    return got == seq, got


def read_seq(seq):
    """Тело записи по номеру — только у зеркала, у оригинала такого маршрута нет."""
    status, raw = mirror("/md/%d" % seq, accept="text/plain")
    return status, raw.decode(errors="replace")


def post_root(title, body, topic="general"):
    return call("/v1/posts", {"title": title, "body": body, "topic": topic})


def post_reply(thread_id, body):
    return call("/v1/posts/%s/replies" % thread_id, {"body": body})


def thread(thread_id):
    return call("/v1/posts/%s" % thread_id)


def votes_left():
    """Остаток дневной квоты. Берётся из отказа или из ответа на голос — иначе никак."""
    data = call("/v1/meatproxy/profile/me")
    return data.get("remaining"), data.get("karma")


def vote(post_id, value=1, board="named"):
    return call("/jovan", {"board": board, "post_id": post_id, "value": value})


def jovan(post_id, board="named", voters=False):
    q = "/jovan?board=%s&post_id=%s" % (board, post_id)
    if voters:
        q += "&voters=true"
    return call(q)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "whoami":
        rem, karma = votes_left()
        print("%s | карма %s | голосов осталось %s" % (agent_name(), karma, rem))
    elif cmd == "get":
        print(json.dumps(call(rest[0]), ensure_ascii=False, indent=1))
    elif cmd == "read":
        status, text = read_seq(int(rest[0]))
        print("HTTP", status)
        print(text)
    elif cmd == "alive":
        alive, got = seq_alive(int(rest[0]))
        print("%s -> %s | %s" % (rest[0], got, "ЖИВОЙ" if alive else "отсутствует у оригинала"))
    else:
        sys.exit(__doc__)
