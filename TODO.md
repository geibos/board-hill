# TODO

Ideas for the hill on the board, not commitments.

## Season two: randomness

Proposed on the board on 2026-09-27 (#62723, corrected in #63650), so that a
warrior cannot be tuned to the known field by trying variants on a copy of
the hill.

1. **Placement from a number nobody knows in advance** — proposed in
   earnest. A match's placement now comes from the two warriors' hashes. The
   job's id would do, but a job cannot see it: its environment has only
   HOME, LANG, LOGNAME, PATH, PIP_USER, PWD, SHELL, SHLVL, TMPDIR and USER
   (checked with a job on 2026-09-28), and the job's number is predictable.
   So the pinned `hill.sh` draws the seed from `/dev/urandom` when it runs
   and prints it; the board keeps the output, a veteran cannot change it,
   and the announcer replays with that seed. Needs `cw hill challenge
   --seed` (in board-corewar's TODO). What is left: challenging with copies
   of one warrior under new names until the luck turns, which the machine's
   log shows.
2. **The final table replayed after the freeze** on a fresh placement (the
   seed from the freeze snapshot and the first post in the machine's thread
   after it). An idea, not a decision; the participants are asked (#62735).
3. **Hidden opponents in the final recount**: five warriors published only
   as SHA-256 before the season, their sources with the result. An idea,
   not a decision; the participants are asked (#62735).

## Hill

4. **Small hills**: nano (core 80) and tiny (core 800). No code in `cw`, the
   rules live in `hill.toml`: a second directory and a switch in `hill.sh`.
5. **The king's key battle on every change of king**: `cw trace` records it
   frame by frame; the announcer would post it with the announcement. Needs
   the replay viewer (board-corewar's TODO).
