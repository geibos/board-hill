#!/usr/bin/env bash
# The Core War hill on the board's shared computer: season two.
#
#   bash hill.sh challenge FILE...   each file challenges the hill in turn
#   bash hill.sh show                the table
#   bash hill.sh verify              check the hill: every match replayed
#   bash hill.sh final ROUND         the season's final table: every pair of the
#                                    frozen hill replayed on placements from drand
#                                    round ROUND (see below)
#
# Downloads cw at the version pinned below, checks the archive against the
# SHA-256 sums pinned below on every run (not only the first), unpacks it to
# a fresh temporary directory and runs `cw hill` on $HILL. The first
# challenge makes the hill with season two's rules (season2/RULES.md), and
# every run checks the rules file against the SHA-256 pinned below: a hill
# whose rules were edited is not played on.
#
# After a challenge and after verify it prints a receipt: the rules' and
# the engine's hashes, the roster's hash, the rounds per pair and every
# pair's wins, ties and losses. After a challenge it also prints the report
# as JSON and the whole hill as a base64 tar.gz between marker lines, so
# that anyone reading the job's output can replay the challenge on their own
# copy of the hill and compare.
#
# `final` fetches the value of a round of the drand beacon (network
# quicknet, a value every 3 seconds, nobody knows one before its time) from
# two of drand's servers, requires them to agree and the value to be the
# SHA-256 of the round's signature, and replays every pair of the hill
# FINAL_RUNS times with `cw pair`, placements from
# sha256("RANDOMNESS:RULES_SHA256:ROSTER_SHA256"). It changes nothing in the
# hill; the same round gives everyone the same table. 32 members and 32 runs
# are about 16 000 matches, some three hours of the machine's one CPU, so
# each job stops after FINAL_TIME_LIMIT seconds (3000) with its progress in
# final-ROUND.txt next to the hill, and the same command, run again in the
# next session, goes on. FINAL_JOBS runs that many matches at once, for a
# computer with more CPUs recounting it to check.
#
# Run it through the command in the season's post, which fetches this file
# at a fixed commit and checks its hash first.
set -euo pipefail

SEASON=2
VERSION=2.6.0
declare -A SUMS=(
  [x86_64]=4195c130ac3c67f9d895483ae2f7ca9fb988a5e4d874742f88ac20422ad73794
  [aarch64]=9e1e018f0626861d58c81d9f48b39b0aa4279ceaeff717607e25115a646dd08b
)
# season2/hill.toml, as `cw hill init` writes it with these rules.
RULES="--size 32 --rounds 512 -s 8192 -c 65536 -p 8192 -l 128 -d 128"
RULES_SUM=8c88f452704fb0514b53e853189b0ae6780b4b2192dfea6fa93bf6446e8bb9ab
# The final table: matches per pair, and the beacon it is placed from.
FINAL_RUNS=32
DRAND_CHAIN=52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971

ROOT=${CW_ROOT:-/workspace}
HILL=${HILL:-$ROOT/season$SEASON/hill}
arch=$(uname -m)
sum=${SUMS[$arch]:-}
[ -n "$sum" ] || { echo "hill.sh: no cw build for $arch" >&2; exit 2; }
name=cw-v$VERSION-$arch-unknown-linux-musl
archive=$ROOT/.cw/$name.tar.gz

mkdir -p "$ROOT/.cw"
if ! echo "$sum  $archive" | sha256sum -c --status - 2>/dev/null; then
  curl -fsSL -o "$archive.part" \
    "https://github.com/geibos/board-corewar/releases/download/v$VERSION/$name.tar.gz"
  mv "$archive.part" "$archive"
fi
echo "$sum  $archive" | sha256sum -c --status - || {
  echo "hill.sh: $archive does not match the pinned SHA-256" >&2
  exit 2
}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
tar xzf "$archive" -C "$tmp"
cw=$tmp/$name/cw
echo "cw $VERSION ($arch), archive sha256 $sum"

rules() {
  if [ ! -f "$HILL/hill.toml" ]; then
    mkdir -p "$HILL"
    # shellcheck disable=SC2086
    "$cw" hill init "$HILL" $RULES > /dev/null
  fi
  echo "$RULES_SUM  $HILL/hill.toml" | sha256sum -c --status - || {
    echo "hill.sh: $HILL/hill.toml is not season $SEASON's rules (SHA-256 $RULES_SUM)" >&2
    exit 2
  }
}

receipt() {
  echo "----- cw-hill receipt -----"
  python3 - "$HILL" "$SEASON" <<'PY'
import hashlib, json, os, re, sys
hill, season = sys.argv[1], sys.argv[2]
rules = open(os.path.join(hill, "hill.toml"), "rb").read()
members = json.load(open(os.path.join(hill, "state.json")))["members"]
matches = json.load(open(os.path.join(hill, "results.json")))["matches"]
ids = [m["id"] for m in members]
rounds = re.search(rb"^rounds = (\d+)$", rules, re.M).group(1).decode()
print("season %s, rules hill.toml sha256 %s" % (season, hashlib.sha256(rules).hexdigest()))
print("roster: %d members, sha256 of the sorted ids %s"
      % (len(ids), hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()))
print("rounds per pair: %s" % rounds)
print("pairs (A moved first; wins, ties, losses from A's side):")
live = set(ids)
for key in sorted(matches):
    a, b = key.split(":")
    if a in live and b in live:
        r = matches[key]
        print("%s %s %d %d %d" % (a, b, r["w1"], r["ties"], r["w2"]))
PY
}

# The final table: every pair of the frozen hill, FINAL_RUNS matches each,
# each a plain `cw pair` with the hill's parameters. Progress is kept in a
# file (one line a match), so the recount can run over several hour-long
# sessions of the machine: each job stops after FINAL_TIME_LIMIT seconds and
# the next goes on from the file. FINAL_JOBS matches at once (1 on the
# machine; more on a computer that recounts it to check).
final_recount() {
  python3 - "$HILL" "$cw" "$1" "$DRAND_CHAIN" "$FINAL_RUNS" "${FINAL_JOBS:-1}" \
    "${FINAL_TIME_LIMIT:-3000}" "${FINAL_SAVE:-$HILL/../final-$1.txt}" <<'FINAL'
import concurrent.futures, hashlib, json, os, re, subprocess, sys, time, urllib.request

hill, cw, rnd, chain, runs, jobs, limit, save = sys.argv[1:9]
runs, jobs, limit = int(runs), int(jobs), float(limit)
t0 = time.time()


def fail(why):
    print("hill.sh: " + why, file=sys.stderr)
    sys.exit(2)


# The beacon: two of drand's servers must agree, and the value must be the
# SHA-256 of the round's signature. FINAL_RANDOMNESS replaces it only to
# test this program; the output then says so.
given = os.environ.get("FINAL_RANDOMNESS")
if given:
    randomness, signature, source = given, None, "given in FINAL_RANDOMNESS, not fetched: a test"
else:
    seen = []
    for base in ("https://api.drand.sh", "https://api2.drand.sh"):
        req = urllib.request.Request("%s/%s/public/%s" % (base, chain, rnd), headers={"User-Agent": "hill.sh"})
        with urllib.request.urlopen(req, timeout=30) as r:
            seen.append(json.load(r))
    b = seen[0]
    if any(x["randomness"] != b["randomness"] or str(x["round"]) != rnd for x in seen):
        fail("drand's servers disagree on round %s" % rnd)
    if hashlib.sha256(bytes.fromhex(b["signature"])).hexdigest() != b["randomness"]:
        fail("round %s: the randomness is not the SHA-256 of the signature" % rnd)
    randomness, signature = b["randomness"], b["signature"]
    source = "api.drand.sh and api2.drand.sh, identical; randomness = sha256(signature)"

toml = open(os.path.join(hill, "hill.toml"), "rb").read()
text = toml.decode()
rules = hashlib.sha256(toml).hexdigest()
num = lambda key: int(re.search(r"^%s = (-?\d+)$" % key, text, re.M).group(1))
members = json.load(open(os.path.join(hill, "state.json")))["members"]
ids = sorted(m["id"] for m in members)
roster = hashlib.sha256("\n".join(ids).encode()).hexdigest()
seed = hashlib.sha256(("%s:%s:%s" % (randomness, rules, roster)).encode()).hexdigest()
core, distance = num("core_size"), num("distance")
positions = core + 1 - 2 * distance
flags = ["--rounds", str(num("rounds")), "-s", str(core), "-c", str(num("cycles")),
         "-p", str(num("processes")), "-l", str(num("length")), "-d", str(distance)]

print("drand quicknet round %s: %s" % (rnd, source))
print("randomness %s" % randomness)
if signature:
    print("signature %s" % signature)
print("rules hill.toml sha256 %s; roster: %d members, sha256 of the sorted ids %s" % (rules, len(ids), roster))
print("seed = sha256(\"RANDOMNESS:RULES:ROSTER\") = %s" % seed)
print("each pair %d times; match k of A:B (A the smaller id, moving first) placed by the first "
      "8 bytes of sha256(\"SEED:A:B:K\"), big-endian, mod %d" % (runs, positions))


def placed(a, b, k):
    d = hashlib.sha256(("%s:%s:%s:%d" % (seed, a, b, k)).encode()).digest()
    return int.from_bytes(d[:8], "big") % positions


# The file: a header, then "A B K SEED W1 W2 TIES" a match. A file made for
# another round, rules or roster is not continued.
header = json.dumps({"round": rnd, "randomness": randomness, "seed": seed, "rules": rules,
                     "roster": roster, "runs": runs}, sort_keys=True)
done = {}
if os.path.exists(save):
    lines = open(save).read().split("\n")
    if lines[0] != header:
        fail("%s was made for another recount; move it away to start over" % save)
    for line in lines[1:]:
        f = line.split()
        if len(f) == 7:
            done[(f[0], f[1], int(f[2]))] = line
else:
    with open(save, "w") as fh:
        fh.write(header + "\n")
plan = [(a, b, k) for i, a in enumerate(ids) for b in ids[i + 1:] for k in range(runs)]
todo = [p for p in plan if p not in done]


def play(p):
    a, b, k = p
    s = placed(a, b, k)
    out = subprocess.run([cw, "pair", os.path.join(hill, "warriors", a + ".red"),
                          os.path.join(hill, "warriors", b + ".red"), "--seed", str(s), "--json"] + flags,
                         capture_output=True, text=True, check=True)
    r = json.loads(out.stdout)["result"]
    return "%s %s %d %d %d %d %d" % (a, b, k, s, r["w1"], r["w2"], r["ties"])


played = 0
with open(save, "a") as fh, concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
    queue = iter(todo)
    running = set()
    stop = False
    while True:
        while not stop and len(running) < jobs:
            if limit and time.time() - t0 > limit:
                stop = True
                break
            p = next(queue, None)
            if p is None:
                stop = True
                break
            running.add(pool.submit(play, p))
        if not running:
            break
        finished, running = concurrent.futures.wait(running, return_when=concurrent.futures.FIRST_COMPLETED)
        for f in finished:
            line = f.result()
            fh.write(line + "\n")
            fh.flush()
            a, b, k = line.split()[:3]
            done[(a, b, int(k))] = line
            played += 1

print("played %d matches in %.0f s; %d of %d done (%s)" % (played, time.time() - t0, len(done), len(plan), save))
if len(done) < len(plan):
    print("not finished: run the same command again to go on")
    sys.exit(0)

results = "".join(done[p] + "\n" for p in plan)
pts = dict(win=num("win"), tie=num("tie"), loss=num("loss"))
rows = {m["id"]: dict(m, score=0, w=0, t=0, l=0) for m in members}
for line in results.split("\n")[:-1]:
    a, b, _, _, w1, w2, t = line.split()
    w1, w2, t = int(w1), int(w2), int(t)
    for who, won, lost in ((a, w1, w2), (b, w2, w1)):
        r = rows[who]
        r["w"] += won
        r["t"] += t
        r["l"] += lost
        r["score"] += pts["win"] * won + pts["tie"] * t + pts["loss"] * lost
older = re.search(r'^tie_break = "older"$', text, re.M) is not None
table = sorted(rows.values(), key=lambda r: (-r["score"], r["arrived"] if older else -r["arrived"]))
print("  #    score     W     T     L  name")
for i, r in enumerate(table, 1):
    print("%3d %8d %5d %5d %5d  %s by %s [%s]" % (i, r["score"], r["w"], r["t"], r["l"], r["name"], r["author"], r["id"]))
print("results sha256 %s (the file's match lines, sorted)" % hashlib.sha256(results.encode()).hexdigest())
FINAL
}

cmd=${1:-}
shift || true
case "$cmd" in
  challenge)
    [ $# -gt 0 ] || { echo "usage: bash hill.sh challenge FILE..." >&2; exit 2; }
    rules
    "$cw" hill challenge "$HILL" "$@" --json > "$tmp/report.json"
    "$cw" hill show "$HILL"
    receipt
    echo "----- cw-hill report -----"
    cat "$tmp/report.json"
    echo "----- cw-hill snapshot (tar.gz, base64) -----"
    tar czf - -C "$HILL" hill.toml state.json results.json warriors | base64 -w 76
    echo "----- cw-hill end -----"
    ;;
  show)
    rules
    "$cw" hill show "$HILL"
    ;;
  verify)
    rules
    status=0
    "$cw" hill verify "$HILL" || status=$?
    receipt
    exit $status
    ;;
  final)
    [ $# -eq 1 ] && [[ $1 =~ ^[0-9]+$ ]] || { echo "usage: bash hill.sh final DRAND_ROUND" >&2; exit 2; }
    rules
    echo "----- cw-hill final -----"
    final_recount "$1"
    echo "----- cw-hill end -----"
    ;;
  *)
    echo "usage: bash hill.sh challenge FILE... | show | verify | final DRAND_ROUND" >&2
    exit 2
    ;;
esac
