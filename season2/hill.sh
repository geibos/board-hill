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
# FINAL_RUNS times with `cw hill recount`, placements from
# sha256("RANDOMNESS:RULES_SHA256:ROSTER_SHA256"). It changes nothing; run it
# on the frozen hill, anywhere: the same round gives everyone the same table.
# It is heavy: 32 members and 32 runs are about 16 000 matches, some three
# hours of processor time, so not a job for the hill's one-CPU machine.
#
# Run it through the command in the season's post, which fetches this file
# at a fixed commit and checks its hash first.
set -euo pipefail

SEASON=2
VERSION=2.5.0
declare -A SUMS=(
  [x86_64]=b740023258e63aa08d9ad39f92ed773341a74b66641b1b0e6e04b9c65692b1c6
  [aarch64]=a70b06a4668903cf1782c8df83efe6cd9eb270706ec37399b6f629c28b1faf81
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

final_seed() {
  python3 - "$HILL" "$1" "$DRAND_CHAIN" <<'PY'
import hashlib, json, os, sys, urllib.request
hill, rnd, chain = sys.argv[1], sys.argv[2], sys.argv[3]
seen = []
for base in ("https://api.drand.sh", "https://api2.drand.sh"):
    req = urllib.request.Request("%s/%s/public/%s" % (base, chain, rnd), headers={"User-Agent": "hill.sh"})
    with urllib.request.urlopen(req, timeout=30) as r:
        seen.append(json.load(r))
b = seen[0]
if any(x["randomness"] != b["randomness"] or x["round"] != b["round"] for x in seen):
    sys.exit("hill.sh: drand's servers disagree on round %s" % rnd)
if str(b["round"]) != rnd:
    sys.exit("hill.sh: asked for round %s, got %s" % (rnd, b["round"]))
if hashlib.sha256(bytes.fromhex(b["signature"])).hexdigest() != b["randomness"]:
    sys.exit("hill.sh: round %s: the randomness is not the SHA-256 of the signature" % rnd)
rules = hashlib.sha256(open(os.path.join(hill, "hill.toml"), "rb").read()).hexdigest()
ids = [m["id"] for m in json.load(open(os.path.join(hill, "state.json")))["members"]]
roster = hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()
text = hashlib.sha256(("%s:%s:%s" % (b["randomness"], rules, roster)).encode()).hexdigest()
print("drand quicknet round %s, from api.drand.sh and api2.drand.sh, identical" % rnd, file=sys.stderr)
print("randomness %s = sha256(signature)" % b["randomness"], file=sys.stderr)
print("signature %s" % b["signature"], file=sys.stderr)
print("rules hill.toml sha256 %s, roster: %d members, sha256 of the sorted ids %s" % (rules, len(ids), roster), file=sys.stderr)
print("seed = sha256(\"RANDOMNESS:RULES:ROSTER\") = %s, %s runs a pair" % (text, os.environ.get("FINAL_RUNS", "?")), file=sys.stderr)
print(text)
PY
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
    text=$(FINAL_RUNS=$FINAL_RUNS final_seed "$1" 2>&1 >"$tmp/seed") || { echo "$text" >&2; exit 2; }
    echo "$text"
    "$cw" hill recount "$HILL" --seed "$(cat "$tmp/seed")" --runs $FINAL_RUNS --jobs 0 --json > "$tmp/final.json"
    python3 - "$tmp/final.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
print("  #    score     W     T     L  age  name")
for s in r["standings"]:
    print("%3d %8d %5d %5d %5d %4d  %s by %s [%s]" % (s["place"], s["score"], s["wins"], s["ties"],
                                                     s["losses"], s["age"], s["name"], s["author"], s["id"]))
PY
    echo "----- cw-hill final report -----"
    cat "$tmp/final.json"
    echo "----- cw-hill end -----"
    ;;
  *)
    echo "usage: bash hill.sh challenge FILE... | show | verify | final DRAND_ROUND" >&2
    exit 2
    ;;
esac
