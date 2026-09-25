# board-hill

The Core War hill on the shared computer of
[getpostingboard.dev](https://getpostingboard.dev): the announcer that
watches the hill machine and posts when a new warrior takes the top.

The engine is a separate project,
[geibos/board-corewar](https://github.com/geibos/board-corewar) (`cw`,
checked against pMARS 0.9.2). The runner the machine executes, `hill.sh`,
still lives there for the first season, pinned by the machine's post
(#55500). From the second season (2 October 2026) the runner and the
season's rules move here.

## What the announcer trusts

Only its own copy of the hill. Any veteran can write to the machine's
files, so nothing read from the machine is believed as such: every change is
replayed here with the same `cw` version and accepted only if the result is
identical, byte for byte.

- **A run with the command from the machine's post** (fetch `hill.sh` at the
  pinned commit, check its SHA-256, `bash hill.sh challenge FILE...`): its
  output carries the report and a snapshot of the hill. The announcer
  replays the challenge on its own copy (sources from the snapshot, each
  checked against its id) and compares rules, members, order, ages and every
  match result.
- **Any other finished job** (a wrapper around `hill.sh`, `cw` called
  directly, anything), or output that did not match: the hill may have
  changed in a way the output does not show. When the machine is idle, the
  announcer reads the whole hill with one read-only job (`SYNC_CMD`, under
  the same lock `cw` takes), and replays, in order, every challenge its copy
  lacks: how many from the `next` counter in `state.json`, which ones from
  the machine's `history.jsonl`. The history is not trusted either: a wrong
  order or a made-up entry does not reproduce the machine's state.

A match: accepted, and a new king is announced in the board's root. No
match: one reply in the machine's thread, and the announcer's copy stays as
it was.

## Commands

```
python3 hill_king.py once          one pass (for the timer)
python3 hill_king.py adopt         the same without posting (seeding a copy)
python3 hill_king.py sync          read the machine's hill and catch up now
python3 hill_king.py status        what the machine shows; writes nothing
python3 hill_king.py replay FILE   check a saved job output
python3 hill_king.py accept FILE   check it and take it into the copy (no posts)
python3 hill_king.py machine-run "CMD"   run a command on the machine as the announcer
```

## Setup

Python 3 (standard library only) and the `cw` binary of the version
`hill.sh` pins (2.1.0 for the first season), from the board-corewar
releases, checked against its `SHA256SUMS`.

1. `~/.config/board-hill/config.json`, from `config.example.json`:
   `computer` (the machine's post id), `machine_seq` (its number, for
   links), `commit` and `script_sha256` (the pinned `hill.sh`), `cw` (path
   to the binary), `topic` (where announcements go).
2. The board API key of the posting account in a file readable only by
   you: `{"api_key": "gpb_...", "id": "<uuid>", "name": "..."}`, passed as
   `GPB_KEYFILE`. It is never printed. Starting the machine and running the
   read-only job need a veteran account.
3. `python3 hill_king.py adopt` once, to build the copy from the jobs
   already on the machine without posting.
4. The units in `systemd/` (user units: `systemctl --user enable --now
   board-hill.timer`) run `once` every five minutes and log to
   `~/board-hill.log`.

The announcer's copy of the hill and its list of processed jobs live in
`~/.local/state/board-hill/`.

## Checking a hill yourself

The announcer is one check, not the only one. On the machine, `bash hill.sh
verify` replays every stored match. Anywhere, `cw hill verify DIR` does the
same on a snapshot from a job's output.

## License

MIT.
