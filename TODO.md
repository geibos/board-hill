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
5. **The king's key battle on every change of king**: the announcer would
   link it in the announcement. The viewer exists since 28.09 (the board's
   mirror, `#/hill/<season>/m/<a>/<b>/<round>`, from the copy `publish`
   puts out): what is left is choosing the round and adding the link.

## Season three

6. **Parameters as prime numbers** (the owner's proposal for season three),
   for example a core of 8191 and 509 rounds.
7. **A new version replaces the old one** instead of joining it (the owner's
   decision for season three). The engine side is a hill option in cw
   (board-corewar's TODO). What decides that two warriors are versions of
   one: `;author` is self-declared, so on the board it has to be tied to
   the account that submitted it (the veteran who ran the job, or the author
   of the post a veteran ran it from), or anyone could push someone else's
   warrior off by naming it the same.

## Some season: 94nop, '94 without P-space

8. **A season on the rules of koth.org's "94 No Pspace" hill** (learned of
   on 2026-09-28): `;redcode-94nop`, "disallows use of pspace". Its
   parameters are season one's: core 8000, 80 000 cycles, 8000 processes,
   length 100, distance 100 (koth.org/koth.html, "Hill Information").
   A warrior may not use LDP/STP, so P-space switchers such as Хамелеон are
   out and the field changes without new numbers. Warriors written for it
   also compare directly with koth.org's long-running 94nop hill.
   Needs a hill option in cw that refuses LDP/STP when assembling: the
   engine's README lists "a hill without P-space" under "Not yet". To
   decide: what to do at seeding with last season's warriors that use
   P-space.

## Viewer

9. **Whose code a dying process was running.** The arena says who wrote the
   cell the last process executed. A paper that copies itself over its
   opponent makes that misleading: on 29.09, S11 morph against Постовой,
   round 59 of their hill match, S11's copies overwrote Постовой's scanner
   (steps 352–464), Постовой's process went on executing what was now S11's
   code, wrote a DAT with it and died on it at step ~478. The arena says
   "written by Постовой", a suicide. Show, next to the death, who wrote the
   last few cells the process executed, so that a hijacked process reads as
   one. The events already carry every write and every executed cell.
