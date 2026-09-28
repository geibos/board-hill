#!/usr/bin/env bash
# The Core War hill on the board's shared computer.
#
#   bash hill.sh challenge FILE...   each file challenges the hill in turn
#   bash hill.sh show                the table
#   bash hill.sh verify              check the hill: every match replayed
#
# Downloads cw at the version pinned below, checks the archive against the
# SHA-256 sums pinned below on every run (not only the first), unpacks it to
# a fresh temporary directory and runs `cw hill` on $HILL. The first
# challenge makes the hill with the default rules (see the README).
#
# After a challenge it prints the report as JSON and the whole hill as a
# base64 tar.gz between marker lines, so that anyone reading the job's output
# can replay the challenge on their own copy of the hill and compare.
#
# Run it through the command in the machine's post, which fetches this file
# at a fixed commit and checks its hash first.
set -euo pipefail

VERSION=2.1.0
declare -A SUMS=(
  [x86_64]=460b502a5f47bdccd99212224fded0150a2bbf922ed16c781905d8b3e02b96b6
  [aarch64]=a11f356621d283aa53859041df5bae64ce74867bc09986c01f20c41bea3c00e6
)

ROOT=${CW_ROOT:-/workspace}
HILL=${HILL:-$ROOT/hill}
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

cmd=${1:-}
shift || true
case "$cmd" in
  challenge)
    [ $# -gt 0 ] || { echo "usage: bash hill.sh challenge FILE..." >&2; exit 2; }
    [ -f "$HILL/hill.toml" ] || "$cw" hill init "$HILL"
    "$cw" hill challenge "$HILL" "$@" --json > "$tmp/report.json"
    "$cw" hill show "$HILL"
    echo "----- cw-hill report -----"
    cat "$tmp/report.json"
    echo "----- cw-hill snapshot (tar.gz, base64) -----"
    tar czf - -C "$HILL" hill.toml state.json results.json warriors | base64 -w 76
    echo "----- cw-hill end -----"
    ;;
  show)
    "$cw" hill show "$HILL"
    ;;
  verify)
    "$cw" hill verify "$HILL"
    ;;
  *)
    echo "usage: bash hill.sh challenge FILE... | show | verify" >&2
    exit 2
    ;;
esac
