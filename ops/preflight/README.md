# World-DB updater pre-flight gate (PRO-179)

`preflight_worlddb.py` is the gate that runs **before** `docker compose up -d ac-worldserver`.
It refuses the boot unless the four `db_world` directories that
`acore_world.updates_include` registers are in a state the updater can be trusted with.

    ops/deploy/worldserver-up.sh              # lease check + gate, then compose up
    python3 ops/preflight/preflight_worlddb.py  # gate only (exit 0 pass, 1 fail, 2 operational error)
    PYTHONPATH=ops/preflight python3 ops/preflight/test_preflight_worlddb.py  # policy-engine tests, no DB needed

`PREFLIGHT_CHECK_ONLY=1 ops/deploy/worldserver-up.sh` runs the gate without touching the
stack — the rehearsal form, for a human on a machine that has the live database. The PR
workflow does **not** run it: `.github/workflows/worlddb-preflight.yml` runs only the
pure-Python checks (see "In CI" below), because the boot gate reads the live schema and no
CI job has one.

The wrapper **verifies** the single-writer
lease at `<compose_root>/.stack/lease` and refuses to boot if it is missing, unreadable or
past its `expectedMinutes` window, but it never **takes** it: the gate is read-only against the
live database plus one scratch schema it drops afterwards, and a gate that could grab the lease
would let a CI run starve the host boot path. Whoever boots the stack takes the lease first
(see `docs/dev-environment.md#single-writer-lease`).

Recognised compose flags (`--force-recreate`, `-f/--file`, `-p/--project-name`, `--profile`,
`--scale`, …) are forwarded to `docker compose up -d` and, where they change what the gate
reads, to the gate as well; `COMPOSE_FILE` is honoured the same way. That closes the
"boot straight past the gate" gap the wrapper would otherwise leave.

Everything after a bare `--` is passed to `docker compose` untouched, which is exactly why the
wrapper refuses it by **shape** rather than by exact token: `-f…`, `-p…`, `--file…`, `--env…`,
`--project…` and `--compose-*` are refused in any form — `-- -f=/tmp/evil.yml`, `-- -f/tmp/evil.yml`
and `-- -p=proj` all fail closed, while `-- --force-recreate` and `-- ac-worldserver` pass through
(rev-3 NB-1). A compose file the gate never read must never reach the boot.

## Checks

| # | Check | Failure mode it prevents |
| - | ----- | ------------------------ |
| 0 | `updates_include` is exactly the four approved rows, `Updates.SourceDirectory` matches the tree | module SQL being registered into the world updater; a source directory that points somewhere else |
| 0b | the worldserver service really mounts `<SourceDirectory>/data/sql` read-only from the tree the gate scanned, and its updater env is exactly `DATABASE_WORLD` | the gate passing on a set of files that is not the set the container will apply; the updater being widened beyond `4` |
| 1 | duplicate basenames across the four dirs (`LOG_FATAL`, `UpdateFetcher.cpp:96`) and case-only collisions (`updates.name` is `utf8mb4_unicode_ci`) | worldserver aborts at boot |
| 2 | prints every `.sql` with no `updates` row — exactly the set the next boot applies | surprises about what is about to run |
| 3 | recomputes `SHA1(ReadSQLUpdate(file))` for every file with a ledger row | a drifted file being silently re-applied (`UpdateFetcher.cpp:350-354`), or a rename the updater will answer by renaming the ledger row (`UpdateFetcher.cpp:335-345`) |
| 4 | write-statement policy for company files | out-of-band content ids, unbounded writes, DDL, ledger tampering |
| 5 | dry run of the pending files on a scratch schema cloned from `acore_world` | a file that only fails after the server is down |

Check 0b reads the resolved compose model with `docker compose config --format json` (no
running container needed) and, if the container is already up, cross-checks `docker inspect`
mounts. It requires: bind mount `Destination == <Updates.SourceDirectory>/data/sql`,
`Source` resolving to `<compose_root>/data/sql`, read-only, no shadowing second mount, and
`AC_UPDATES_ENABLE_DATABASES` set with bit `4` (`DATABASE_WORLD`,
`src/server/database/Database/DatabaseLoader.h:48-51`) and no other bit. `--no-boot-path-check`
skips it for offline use; the wrapper refuses that flag unless `PREFLIGHT_CHECK_ONLY=1`, because
a boot that skips check 0b is a boot the gate did not read.

Why the mount is what makes the gate meaningful: the `.sql` files the updater applies come from
the bind mount, not from the image, so a stale image cannot make the scanned set differ from the
applied set — a stale image only means a stale *binary*. On the dev host the mount comes from the
gitignored root `docker-compose.override.yml` (which `scripts/compose-sync.sh` keeps in sync with
the tracked `wrath-unbound/docker-compose.override.yml`), and it is `:ro`. Check 0b asserts that
at run time instead of trusting the file.

### The two roots, and which one decides what was read (PRO-307 A3)

`--compose-root` is the only root the boot path needs, and both flags below default to it. They
are **not** interchangeable, and only one of them chooses what the gate looked at:

| flag | what it actually steers |
| --- | --- |
| `--git-root` | the tracked tree the gate reads: every `git` probe runs here, and the `$`-relative `updates_include` paths resolve against it (`$/data/sql/db_world` → `<git-root>/data/sql/db_world`). **This is the set of files the scan walks.** |
| `--sql-root` | the directory check 0b compares against the container's `/azerothcore/data/sql` bind, and the base the report prints as "sql root". It does **not** choose the scan. |

So `--git-root /mnt/data/wow-server --sql-root <worktree>/data/sql` is a coherent thing to ask for
only if you read the report carefully: PRO-303 ran exactly that and the banner printed the
*worktree* as "sql root" while the gate counted 3060 files from `/mnt/data/wow-server` — the
trigger file in the other tree was never opened, and the run said `RESULT: PASS`. Check 0b was
still doing its job (it compares `realpath(sql_root)` to the bind, which is why the combination
is not inert); the line that answers "which files did you actually look at" was the one lying.

The report therefore now prints both, each labelled with what it is for:

```
  sql root     : /worktree/data/sql (what check 0b compares to the container's sql bind)
  scanned from : /mnt/data/wow-server (the $-relative updates_include paths the scan walked resolve here)
```

Making the scan follow `--sql-root` instead would be a behaviour change to a gate whose whole
value is that it reads what the boot will apply, and it would need its own test that the scanned
set still equals the applied set; the flags and this section are the fix that was actually made.

## Provenance (who is "company")

Check 4 is enforced on company-owned files. A file is company-owned when **either** its directory
is tagged `company` in `preflight-policy.json` (`custom/db_world`, `updates/pending_db_world`)
**or** its last-commit author address is on a `companyEmailDomains` domain (`paperclip.local`).
The directory tag is a policy statement, so content dropped into a company directory is always
policy-checked — even when it is committed with `--author` crediting an upstream author, which
this repo's `AGENTS.md` requires. `upstream` (and therefore exempt by default) requires **both**
an upstream-tagged directory and a non-company author. Files git cannot attribute — untracked or
unreadable history — are never exempt, and neither is an upstream file with **uncommitted
changes**: provenance is read from HEAD, so a hand edit in the work tree re claims the file for
the policy (`dirty-worktree`) instead of letting the edit ride the exemption.

That work-tree check is `git status --porcelain -z --ignore-submodules=none`, and the `-z` is
load-bearing (PRO-301 review N2, B1). Plain porcelain output C-quotes any path containing `"`, `\`,
a tab or a newline, and octal-quotes any non-ASCII byte — `"\303\251rfar.sql"` for `érfar.sql`,
`"…/we\"ird.sql"` for `we"ird.sql` — and a quoted string never equals the name on disk, so the file
reads as clean and the edit rides the exemption anyway. `core.quotePath=false` is **not** the fix:
it governs only the non-ASCII class, so on git 2.43.0 the plain and unquoted outputs are
byte-identical for every one of those names. `-z` keeps the `XY ` status columns, NUL-terminates each
entry, and emits paths raw even with `core.quotePath=true` — which is the invariant that actually
matters, because it does not depend on the clone's config at all. Under `-z` a rename or copy lists
its original path as the *following* field, not as `"old -> new"`, and both sides count as touched.
The output is decoded with `surrogateescape` so a path that is not valid UTF-8 at all comes back
byte-for-byte. Do not tidy any of those away.

That last choice is what makes such a name *count* as dirty, and it also puts a lone surrogate into
the report — so the reporting layer has to be able to carry it (PRO-308 QA finding B). Whether
`print()` survives one is the operator's locale, not the gate's: on the deploy host a
`UnicodeEncodeError` landed between `render()` and the `--json` write, and the boot was refused with
a traceback, no stated reason and no payload. `_emit()` therefore escapes what the stream's codec
refuses (`backslashreplace`, so the name is still identifiable) and `_write_json_payload()` pins
`ensure_ascii=True`, which is the load-bearing half. The explicit `encoding="utf-8"` is the weaker
half and is best read that way: with `ensure_ascii` on it changes nothing today, it only stops the
payload's byte form from following whoever eventually relaxes that flag.

Escaping the codec is not the whole of it, though. **Evidence before presentation** (PRO-313 review
B1): `main()` writes the `--json` payload *first* and prints afterwards, and each half is held apart
from the other. `_emit()` guards the whole body including `flush()`, naming a refused write on the
fallback stream instead of raising — a pipe closed early is not a verdict; `_render_or_say_why()`
degrades to listing the findings if `render()` itself blows up, and that degraded listing is
itself a guard, so it is held to the rule it exists to enforce: no line of it may raise back into
`main()`. A report that raises while it is being read still prints the two-line
`PRO-179 … / RESULT: …` header instead of losing the verdict (PRO-324 NB1). The same line names the
payload's actual outcome — on disk, not requested, blank, or not written — because `main()` hands the
renderer the claim it actually proved, as the required keyword-only `payload_note`; the renderer
derives nothing about the payload it did not write (PRO-324 NB2, closed on the integration line by
PRO-335 §2 and PRO-341 review N1). The dictionary behind that payload is built *inside* the same
guard as the write, so a report the terminal can still read but the evidence half cannot
(`report.data`, `report.sections`, a finding with no readable `__dict__`) becomes a named
evidence-write failure rather than a traceback out of `main()` (PRO-339 review NB-Rev-2). And a
payload we cannot write refuses the boot with `EXIT_ERROR` rather than letting it through on the
checks' own rc — which bites only an operator who asked for `--json`, since the shipped wrapper does
not pass it (PRO-324 NB4). A finding with no `rule` prints no `None:` in front of its message, on
either path (PRO-324 NB5). The rule behind all of it: the payload is the half nobody can regenerate
once the scratch schema is dropped, and the terminal echo is the half you can re-derive by re-running.
Because that ordering is the last line of defence, the suite does not accept a mere `is_file()` at
presentation time — the ordering pin reads the bytes that are on disk when the presentation half runs
and requires them to equal the final payload (PRO-339 review NB-Rev-1): *complete* before
presentation, not merely *present*. `main()` still carries no bare `print()`.

Guarding the write is not the end of the guard, though (PRO-325 NB1, PRO-335). The bytes a refusing
device would not take are still sitting in the stream's buffer when `main()` returns, and CPython
flushes `sys.stdout` and `sys.stderr` again during shutdown; a failure there exits **120**, a code
this gate does not document. `ops/deploy/worldserver-up.sh` reads any non-zero rc as a refusal, so
the boot was still refused — the damage was that `REFUSING BOOT … (exit 120)` points an operator at
an interpreter accident instead of the finding that refused the boot. So a stream that refused
`_emit()` is retired on the way out: flushed, closed, and replaced on `sys.stdout`/`sys.stderr` by a
`_RetiredStream` that absorbs everything, which is what leaves the interpreter nothing to fail on.
The stand-in also reports `closed = True` (PRO-341 review N3), so CPython's shutdown flush skips it
outright instead of relying on `flush()` staying inert forever.
The exit status stays the three documented above, and `TestTheGateHoldsItsOwnRc` runs the refusals in
subprocesses against a real `/dev/full`, a real early-closed pipe and a real closed fd 1, because
interpreter teardown is not observable from inside a fixture. The same class pins the other PRO-325
observation: with fd 1 closed, `sys.stdout` is `None` and the old label printed the word `None` where
a destination belongs; an absent thing is now named as absent, including on the degraded render line,
which says "no --json payload requested" rather than claiming a payload nobody asked for.
That degraded line draws from **four** wordings, three of which say the evidence is not there.
They are chosen by three names in `main()`, not one: `payload_requested` splits never-requested
from asked-for, `blank_json_target` splits the blank family out of that, and `payload_error` —
which only the payload block in `main()` can set — splits NOT-written from on-disk (PRO-352,
Core's PRO-355 contract A): never requested → `NO_JSON_PAYLOAD_NOTE`; requested but blank →
`BLANK_JSON_TARGET_NOTE`; requested but unwritable → "the --json payload at X was NOT written";
written → "the --json payload at X is on disk". The fourth is not hypothetical — it is what that
line says when `render()` raises *after* the payload landed, which is the shape
`test_the_degraded_verdict_still_points_at_the_payload_it_wrote` holds. The write attempt is
gated on `payload_requested = args.json_out is not None`; the evidence-missing refusal at the
bottom of `main()` is gated on `payload_error`, which is set in exactly two places, both inside
that same block and both on branches that already required `payload_requested` — the failed
write, and the blank-target arm beside it. They still cannot disagree about whether evidence was
asked for — no path reaches a non-`None` `payload_error` with `payload_requested` false — but
they are not the same test, and the NOT-written / on-disk half of the note follows what the
write did, not the flag. Blankness is
`args.json_out.strip()` — not truthiness, and not `== ""` — so `--json ""` and `--json "   "`,
what an operator's `--json "$TARGET"` becomes when `$TARGET` is blank because
the wrapper forwards unrecognised arguments verbatim, are *requested* payloads that touch no
filesystem at all and take the evidence-missing refusal at `EXIT_ERROR`. A gate that tested the flag
for truth instead would accept the whitespace target, since `Path("   ").write_text` succeeds, and
boot on a file literally named `   ` (PRO-341 review B1, closed by PRO-352 — do not restore the
truthiness test to match older prose). And the refusal names the destination an operator can act on: `_emit()` adds what
the stream actually is, so a full device reads `could not write the report to <stdout> (/dev/full)` and
a closed fd 1 reads `<stdout> (nothing is attached to it)` (PRO-341 review N2, N4).
"what the stream actually is" has two sources, and PRO-354 is the lesson that they are not
interchangeable. `_stream_label()` asks the Python object, which works for a stream Python opened
(`open("/dev/full", "w").name` really is `/dev/full`) and is worthless for a descriptor the shell
redirected before the interpreter started — on 3.12.3 `sys.stdout.name` is still the literal
`<stdout>` under `> /dev/full`, so the two compared equal, the parenthetical was suppressed as noise,
and the device name never appeared. `_stream_destination()` therefore falls back to
`os.readlink("/proc/self/fd/N")` when the object's answer is one of the interpreter's own channel
names. An anonymous descriptor (`pipe:[…]`, `socket:[…]`) is not a place anyone can go and fix, so it
stays unnamed and an early-closed pipe still reads plain `<stdout>`; a host without `/proc` gets the
old wording rather than a guess. Three destination shapes are pinned from outside the child: a shell
`1>&-`, a shell `> /dev/full`, and an early-closed pipe the parent hands over with `Popen(stdout=…)`.
Each pins the same property — fd 1 is set before the interpreter is built, never by an `open()`
rebinding inside it — and only the two shell shapes go through the one helper, because no shell
redirect can hand a child a descriptor whose reader is already gone.

### The third tag: the tracked upstream ref (PRO-248)

Directory tag plus author tag is a claim about **history**, and history metadata is cheap to
fabricate:

```
git commit --author="Someone <x@azerothcore.org>" data/sql/updates/db_world/2026_11_04_00.sql
```

That keeps the work tree clean and buys the exemption, which is how a company engineer can hand
themselves a `warn` (`policy-exempt-origin`) instead of a check-4 review. So a **new** file (no row
in `acore_world.updates`) sitting in an `origin: "upstream"` directory is exempt only when it also
exists in the ref the tree is tracking — `upstreamProvenance.verifiedRef`, read with local object
lookups only (`git ls-tree`), never a fetch:

| ref answers | classification | finding |
| --- | --- | --- |
| name is in the ref, bytes match | `upstream` | none — the ordinary `git pull` path |
| name is in the ref modulo case, bytes match | `upstream` | `WARN` `upstream-ref-case-divergence` naming the ref path |
| name is in the ref, bytes do not match | `company` | `FAIL` `unverified-upstream-content` naming `pending_db_world` |
| ref resolved, name absent | `company` | `FAIL` `unverified-upstream-origin` naming `pending_db_world` |
| git could not answer | `company` | `FAIL` `unverified-upstream-origin`, failing closed, naming `refreshCommand` |
| ref does not resolve under `refs/remotes/` | `company` | `FAIL` `upstream-ref-not-remote` naming `refreshCommand` |
| ref resolves but is not a commit (tree, blob, null sha) | `company` | `FAIL` `upstream-ref-not-a-commit` naming `refreshCommand` |
| ref is gone, but this clone still has local history for it | `company` | `FAIL` `upstream-ref-deleted` naming `refreshCommand` and `--allow-missing-upstream-ref` |
| ref is gone with no local trace, **but** this clone's `last-verified` record says a satisfied run verified it at a named commit | `company` | `FAIL` `upstream-ref-attested-missing` naming the attested oid, the timestamp it was verified at, the record path, `refreshCommand` and `pending_db_world` |
| ref is gone and the `last-verified` record for it is not one this gate can trust | `company` | `FAIL` `upstream-ref-attestation-untrusted`, failing closed, naming the record path and why |
| ref resolves, but not to a commit that contains the one recorded for it, and no reflog explains the move | `company` | `FAIL` `upstream-ref-oid-mismatch` naming both oids, the recorded timestamp and `refreshCommand` |
| ref unset | `upstream` | `WARN` `upstream-ref-unverified` naming `refreshCommand` |
| ref is absent, this clone has no trace of it, **and** no `last-verified` record names it | `upstream` | `WARN` `upstream-ref-unverified` naming `refreshCommand` |

#### `missing`, `indeterminate`, `unchecked` (PRO-291, PRO-248 review N1)

"Is the ref there?" is not one question and the gate no longer answers it with one state. The
distinction matters because `unchecked` is the only ref answer that still grants the exemption on a
`WARN`, so anything that can be laundered into `missing` has bought back the pre-PRO-248
directory+author rule.

| state | what git actually said | what the gate does |
| --- | --- | --- |
| `unchecked` | `git rev-parse --verify --quiet <ref>` finds nothing at all, **and** this clone has no local trace of the ref — no reflog, no `FETCH_HEAD` naming a fetched ref, no sibling reflog and no `last-verified` record naming it | `WARN`, exemption kept — a clone that has genuinely never fetched still boots offline, and the report names `refreshCommand`. A plain `git clone` of a remote that never carried the ref is exactly this state, and so is one whose only fetch attempt failed on transport |
| `indeterminate` / `ref-deleted` | the ref is gone **but** this clone has fetch history: `ref`'s own reflog, a `FETCH_HEAD` that names at least one fetched ref, or a sibling ref that still carries a reflog of its own | `FAIL`, failing closed, naming `refreshCommand`; only `--allow-missing-upstream-ref` walks it back to the `WARN` |
| `indeterminate` / `attested-missing` | the ref is gone and none of those three signals is present, **but** this clone's `last-verified` record says a satisfied run of this gate read this very ref at a named commit at a named time | `FAIL`, failing closed, naming the attested oid, the timestamp, the record path and `refreshCommand`. A clone that genuinely never fetched has no attestation to lose, so this is one whose fetch fingerprints were removed after the fact; `--allow-missing-upstream-ref` walks it back to the `WARN` exactly as it does `ref-deleted`, because it is the same claim with a different witness |
| `indeterminate` / `attestation-untrusted` | the ref is gone and a record for it exists that this gate cannot vouch for — unreadable, too large for the bounded read to have seen all of it, holding a line it cannot parse, unversioned or of a version it does not read, or written for another repo or another remote url | `FAIL`, failing closed, naming the record path and the reason. "The record is unreadable" must not be the same sentence as "there is no record", or truncating a file is all it takes to buy back the exemption the record was blocking |
| `indeterminate` / `oid-mismatch` | the ref resolves to a commit, and the commit recorded for it is neither that commit nor an ancestor of it, and the ref carries no reflog of its own | `FAIL`, failing closed, naming both oids. A fetch fast-forwards a remote-tracking ref and a real force-push leaves a reflog, so this is the shape of `git update-ref` pointing the tracked ref at commits of the operator's choosing — and `ls-tree` is never asked of it |
| `indeterminate` / `not-a-commit` | the name resolves to an object that is not a commit — `HEAD^{tree}`, a blob, a mistyped object name, the null sha | `FAIL`, failing closed, naming `refreshCommand`. It is not missing, and calling it `missing` was the false statement that kept the exemption |
| `indeterminate` / other | git errored, or the ref is not under `refs/remotes/` | `FAIL`, failing closed |

Before PRO-291 both of the last two rows were filed as `missing`, so a `verifiedRef` pointed at a
tree object booted a forged-author file on a `WARN` — the reason `git rev-parse --verify <ref>^{commit}`
is no longer the existence test. It is now a two-step question: peel to a commit, and if that fails
ask separately whether the name resolved to *anything* (`rev-parse --verify --quiet <ref>`, with
`cat-file -t` naming what came back). Only a name that resolves to nothing at all may be `missing`.
`git ls-tree` is never asked of a ref the gate does not trust (PRO-300): not of a ref whose recorded
commit git cannot place and whose move no reflog explains, and not of a ref sitting behind a record
this gate could not have written. A recorded move that a surviving reflog *does* explain stays
trustworthy enough to read — that is how an honest force-push still boots — so "never asked" is a
claim about the unexplained shapes, not about every ref with an attestation. None of these probes
fetch.
Every git call the gate makes — the ref probes, the author lookup, the work-tree `git status`, and
the attestation's `rev-parse --git-common-dir`, `config --get remote.<r>.url` and
`merge-base --is-ancestor` (PRO-300) — goes through the one `_git()` helper, so "nothing in here
reaches the network" is a claim about a single call site rather than about most of them (PRO-291
review N5).

The `ref-deleted` discrimination is local history only — three signals, no network. What each one
tests is *fetching*, so each is a file a fetch writes and `git clone` does not:

- the ref's own reflog, at `git rev-parse --git-path logs/<ref>`;
- `FETCH_HEAD`, but only what a fetch actually **wrote** — at least one line opening with a
  fetched object id (`git fetch` writes `<sha>\t\tbranch 'Playerbot' of <url>` per ref). Its
  mere existence is not evidence: `git fetch` opens the file before it has anything to record,
  so a fetch that fails on transport leaves it present and empty. Measured on git 2.43.0,
  `GIT_ALLOW_PROTOCOL=none git fetch origin Playerbot` exits 128 and leaves a 0-byte
  `.git/FETCH_HEAD` (sha256 `e3b0c44298fc…`, the empty digest), and reading `exists()` as
  "this clone has fetched" turned that never-fetched clone into a `upstream-ref-deleted`
  refusal that accused the operator of a deletion they had not made (PRO-307 A1). A file with
  content that parses as no fetched ref is still evidence — the gate does not hand the
  exemption back because a file holds something unexpected — but it is reported as that, and
  never as a fetch the gate observed;
- any sibling ref under the same `refs/remotes/<remote>/` namespace other than `<remote>/HEAD`
  that **carries its own reflog** (`for-each-ref`, not `show-ref` — `show-ref` matches anchored
  whole ref names, so a bare namespace prefix answers "no refs").

`FETCH_HEAD` is also not monotone, which is the second reason it is one of three probes rather
than the answer: one good fetch writes it and the next failed fetch truncates it back to 0
bytes, destroying that signal on its own. Nothing in the gate can tell "fetched, then a failed
fetch" from "attempted one fetch and it failed" from that file alone, which is why the durable
`last-verified` record below exists (PRO-307 A1).

The reflog is what makes the third signal honest, and it is the reason the probe is not simply
"any sibling ref" (PRO-291 review B1). `git clone` populates `refs/remotes/<remote>/*` itself, so a
sibling *ref* says only that the remote had that branch when the clone was made; counting it as
fetch evidence refused a boot to an untouched clone and told the operator their ref had been
deleted. Measured on git 2.43.0, a plain clone and a `--single-branch` clone both leave the
namespace populated and every branch sibling reflog-free; a `git fetch`, a `git pull` or a
`git update-ref` is what writes one.

That set exists because the ref's own reflog alone is not enough: `git update-ref -d` deletes the
reflog along with the ref, so the exact command the review named would otherwise be
indistinguishable from a clone that never fetched. The sibling-reflog probe is what catches it —
**in a clone where something was ever fetched**, which is a clone that has run `refreshCommand` at
least once. `origin/HEAD` is excluded from the probe because a clone writes that one reflog without
anyone ever fetching a branch.

The limit of that sentence belongs in this README rather than left to inference, because what this
section owes the operator is what the gate actually guarantees. In a clone that has **never**
fetched, every sibling reflog is absent by construction, so
`git update-ref -d refs/remotes/origin/Playerbot` there leaves a state identical, in everything the
three signals read, to an honest clone of a remote that never carried the ref — and that honest
clone is the case this section promises boots offline. The three signals cannot separate the two,
and the same holds for an operator who deletes the ref and then removes `FETCH_HEAD` and
`logs/refs/remotes/`. Saying the signals could was the same species of over-claim PRO-248 review N3
removed.

PRO-300 closes that gap by adding a fourth thing for the deletions to have to reach: when the ref
answers the way the exemption is built on — it resolves under `refs/remotes/`, it peels to a commit,
and the gate can list that commit's tree — the run writes down that it did:

```
<git common dir>/paperclip-preflight/last-verified

    version 1
    repo /home/you/azerothcore-wotlk/.git
    remote url https://github.com/azerothcore/azerothcore-wotlk.git
    last-verified refs/remotes/origin/Playerbot = 3f2a91c0d4e51b7c… @ 2026-11-06T09:12:03+00:00
```

Three properties are what make it the missing witness. It lives in the **common dir**, which
`update-ref -d`, `rm FETCH_HEAD` and `rm -rf .git/logs/refs/remotes` all leave alone, and which a
linked worktree shares with its siblings. It is written **only by the path that earned it** —
`missing`, `not-a-commit`, `not-remote`, an untrusted record and a tree whose common dir git will not
name all refuse before the write, so a ref that is not there can never fabricate a record of having
been verified, and no record exists that does not say which tree earned it. And a fresh `git clone`
gets a common dir with **no record in it**, which is the part AC 6 rests on: an untouched clone of a
remote that never carried the ref has nothing to find, so its answer is still `absent` and it still
boots offline on the `WARN`. The location is asked from git (`rev-parse --git-common-dir`) rather
than hardcoded as `.git` for the same reason `_reflog_path()` asks `--git-path` — in a worktree the
admin dir is not `.git` — and when git will not answer there is no state directory at all, never a
guessed one: a probe nobody answers must not become a finding, and an attestation written somewhere
untrustworthy is worse than none. `--preflight-state-dir` relocates it for a test or for someone
running the gate over a tree they do not want written inside; it names a **directory** and the record
is the `last-verified` file inside it, so pointing two trees at one state dir gets the second one
refused rather than trusted — the `repo` line in the record names the other tree. The write is a temp
file plus `os.replace`, because a record truncated by a crash mid-write would then fail closed every
later boot, and a failure to write is an informational note rather than a finding: the record is
evidence *for* a later deletion check and must never become a precondition for booting.

From then on `missing` is not one answer. A ref with a record that has gone is
`indeterminate`/`attested-missing`, not `unchecked`. An ordinary `git pull` between boots is not
suspected: the recorded commit is an ancestor of the one the ref names now, `git merge-base
--is-ancestor` says so, and the record moves forward with the run. A ref whose recorded commit git
cannot place, with no reflog entry to explain the move, is not the history this gate verified, so
`ls-tree` is never asked of it and the boot is refused (`oid-mismatch`); a genuine force-push is
cleared by the entries it leaves in that reflog (PRO-301: an empty trace file explains nothing).

A record this gate could not have written is refused too, and refused on **both** paths — that is the
PRO-300 review's finding, because a record read as merely *absent* on the path that ends in a write
lets one appended junk line buy back the exemption and then stamps the forged oid as verified. On the
satisfied path, where the ref is present and peels to a commit, an unparsable line, a missing or
unknown `version`, no `repo` line at all, a `repo` naming some other common dir, or a `remote url`
that is not this tree's each stop the boot `indeterminate`/`attestation-untrusted`, name which of
those they are, do not read that ref's tree at all, and write nothing — so a record cannot be
laundered by the very boot it just blocked. `repo` is *required* rather than compared-when-present
and required outright, not merely when git will name this tree's common dir — a record of ours always
carries it, because `write_attestation()` refuses to emit one it cannot bind, and requiring it only
when the probe answers is the gap `--preflight-state-dir` could walk through (PRO-300 review finding 5).
Present is not the same as checked, though: a `repo` line the gate has nothing to compare against —
git will not name this tree's common dir while the record is being read from an overridden state
directory — is `untrusted` too, because anyone who can write that file can write any path into it, so
the line is worth exactly what the gate can verify it against (PRO-300 review NB-4).
`remote url` is compared only when both sides can name one, since a tree may legitimately have no
remote and a record written before `git remote add` is not a forgery — and the name it is compared
against is taken from the canonical `refs/remotes/<remote>/<branch>` name `git` resolved for
`verifiedRef`, not from the spelling policy happens to use. A short `verifiedRef: origin/Playerbot`
names the same remote to an operator, and parsing a remote out of it here would have meant guessing;
reading it as unparseable instead wrote no `remote url` line at all, which left a later
`git remote set-url` with nothing to be caught by (PRO-300 review NB-2). Both bindings are written only
when they fit on one line: a `remote.<name>.url`, or a common-dir path, containing a CR or LF would
otherwise spill a second line into the record that the strict read would then read as a binding this
run never made, so such a binding is treated as unavailable and the `remote url` line is omitted
(PRO-300 review finding 2) — but omitted is not the same as absent from the report: a url binding this
gate declined to write is one the record can never enforce, so section 4 says so on the
`upstream-ref-attestation` line rather than leaving the next `git remote set-url` unwatched (PRO-300
review NB-3). The one shape that stays inert on the satisfied path is a record the OS
will not hand over
(unreadable, or not a regular file): that is an environment accident rather than a claim about the
ref, what the exemption rests on there is the tree comparison, and making it fatal would turn the
record into a precondition for booting, which AC 6 forbids. It is still reported, and it still cannot
attest — including on the path where the run goes on to write a record over it, which is the one place
AC 6 was previously being read as a licence for silence (PRO-300 review).

A record that simply holds no `last-verified` line for *this* ref is `absent`, not untrusted, and the
binding lines are settled only after that — a record that says nothing about `ref` says nothing about
`ref` no matter whose common dir it names. Pointing `verifiedRef` at a new name therefore starts a
fresh history instead of failing on a record earned for the old one — the pre-write read asks the strict read's
whole question — one `_record_content_refusal()` and one `_record_binding_refusal()`, not a second and
laxer copy of part of it — so a file this gate could not have written contributes no lines to the
record it replaces it with (PRO-300 review finding 4, and NB-6: it used to ask about the version and
the bindings but not the unparsable lines, so a record the strict read had refused could still have
those lines carried forward into a rewrite this gate signs its own name to) — and a file copied
in from another clone cannot accuse a clone that never carried the ref (PRO-300 review B1: `rm` must
not be the only way out). Section 4 says which of these it used — the summary line carries
`last-verified <oid>@<when>` for the record a satisfied boot relied on, `last-verified refused (record
untrusted: <cause>)` for one that was refused, a separate `upstream-ref-attestation` INFO line names
the record file, and a `WARN` `upstream-ref-attestation-unreadable` names it when the record on disk
could not be read at all and this run wrote a fresh one over it.

What the attestation does **not** guarantee, since a half-told guarantee is worse than none:

- It is a file. Anyone who can `rm` it has deleted the gate's memory of verifying the ref, and the
  next `missing` answer looks like the honest `absent` again. What the record changes is the shape
  of the forgery: `update-ref -d` alone now trips a rule, and getting back to a quiet `WARN` means
  deleting the ref, its fingerprints and this file — inside a checkout whose owner can equally edit
  `preflight-policy.json` outright. Against someone who owns the working tree this is friction, not
  a defense, and it is no wider a privilege than the `update-ref` described under "Honest limit"
  below.
- It does not repair itself. A boot that cannot parse the record keeps the lines it was given rather
  than rewriting them, because unparsable lines are the evidence of the tampering it just refused; so
  the FAIL's remedy is the literal one it prints — `rm <state dir>/last-verified` — and the next
  satisfied boot writes a fresh record naming this tree. Anyone offering "just re-run it" for a
  corrupt record is describing a different design.
- It refuses a **silent** move, not a move. `git update-ref` writes a reflog entry by default, and a
  reflog that holds an entry is exactly what the `oid-mismatch` branch treats as an innocent
  explanation — it is what a real force-push leaves behind too, and an empty one is a file that
  records nothing (see the trace-strength note below). So the attestation catches the `update-ref` whose reflog was
  then removed; the one that leaves its reflog in place stays the deliberate, loud act described
  under "Honest limit" below, where a reviewer running `git reflog refs/remotes/origin/Playerbot`
  sees it. On a tree that really does fetch from the remote the next fetch undoes the move; a tree
  nobody fetches into keeps the moved ref, reflog and all, for as long as it is not fetched. Anything
  stronger would refuse an ordinary force-push-based upstream workflow, which is not this gate's job.
- It signs nothing and authenticates no clock. `attestedWhen` is this host's UTC time at the moment
  a satisfied run wrote the line; the claim it supports is "this gate read this commit through this
  ref here", not "upstream published this then".
- Nothing outside this checkout reads it, and nothing schedules it. `.github/workflows/` does mention
  `ops/preflight` — PRO-285 wired up `.github/workflows/worlddb-preflight.yml`, which runs the engine
  tests, `audit_policy_schema.py --include-modules`, the corpus sweep and the provenance guard — but
  none of those run the gate. No workflow invokes `preflight_worlddb.py`, so no CI run reads this
  record, writes one, or checks that one exists; a record exists only where the boot path in
  `ops/deploy/worldserver-up.sh` has actually run the gate and passed. A clone that has never run the
  gate has no record, and looks exactly like a clone of a remote that never carried the ref. That is
  the honest state, and it is a `WARN`, not a failure.

The three signals are not equally strong, and the report says which one it used. `ref`'s own reflog
**with an entry in it** is a record of *this* ref having been here, so the message says it was
deleted rather than never fetched. A `FETCH_HEAD` that holds something or a sibling reflog with
entries only proves somebody fetched, which leaves "the remote never had this branch" standing; the
gate still fails closed on that, but it says the ref was either deleted or never existed here,
because a boot refusal must not assert a deletion it did not observe.

A trace file that is **empty** is weaker than either, and is reported as weaker (PRO-301 review N1,
N2). `git update-ref` creates `logs/<ref>` and a fetch that changes nothing can leave it empty, and
an empty `FETCH_HEAD` is likewise a file rather than a fetch record, so `os.path.exists()` answers
yes to something that records nothing. Presence is still the fail-closed answer — an empty reflog is
a file a clone that never fetched does not have, so the boot stays refused — but neither half of the
old sentence is owed to it: the gate says every local trace is a file with nothing in it, which
records nothing, rather than claiming a deletion it did not see or a fetch it cannot show. Note the
`fetched` tier can be reached with the own reflog present-but-empty, so the wording is "no reflog
**entry** of its own", not "no reflog of its own" — the evidence list right next to it names the
file.

The three paths `git` reports about the tree — the reflog, `FETCH_HEAD`, and the `last-verified`
record — all go through one bounded reader, the way every `git` call is bounded (PRO-301 review N4,
and PRO-300 review finding 1 for the record, which had been left as a bare `read_text()`): `stat`
first, regular files only, at most one capped chunk. The gate reads other files directly too — the
policy JSON, `worldserver.conf.dist`, the company update files themselves, a referenced SQL file — but
those are paths it was handed, not paths `git` reported about a tree it was not asked to trust, which
is the distinction the probe is drawn around. The file is then opened `O_NOFOLLOW | O_NONBLOCK` and
re-checked with an `fstat()` of the descriptor that was actually opened (PRO-300 review NB-6): a
`stat` by path and an `open()` by path are two lookups of a name somebody else controls, and the
second answer is the only one that describes the bytes. A fifo planted at `.git/logs/<ref>` therefore
answers "nothing was observed" instead of blocking the boot forever with the single-writer lease held
and the worldserver never starting, and a fifo planted at the record path answers `untrusted` /
`unreadable` for the same reason — `open()` on a writer-less fifo never returns, so no `except OSError`
would ever have been reached.

A record larger than that one capped chunk is refused as `unreadable`, not read as though it were the
file (PRO-300 review NB-1). Every question the readers ask of a record — "does it name a version",
"does it hold a `last-verified` line for this ref" — is a question about the bytes they were handed,
and a prefix is not the file: answering `absent` for a truncated record puts "the record says nothing"
within reach of whoever can make the record long, which is the same act as truncating it. `unreadable`
is the right cause because it is the truth the rest of the gate acts on: inert on the satisfied path,
where the tree comparison carries the boot (AC 6), and a refusal on the path where the record is all
that stands between a stripped `.git` and the exemption. A record that cannot be read in full is never
rewritten, either — rebuilding it from the part that could be read would drop every other ref's line
past the cap, and losing an operator's history is not a price the record is worth.

`--allow-missing-upstream-ref` is **off by default on the boot path** and rescues the `ref-deleted`
and `attested-missing` cases only — both answer the same question, "the ref is simply not here",
which is the question the flag exists to waive. It does not rescue `attestation-untrusted`,
`oid-mismatch`, `not-a-commit`, any other `indeterminate` cause, or a ref that was genuinely never
fetched — the `WARN` for that one is already granted without it, and the flag is not a general mute
switch. The two PRO-300 cases it does not reach are not missing-ref problems the operator can waive:
one is the gate refusing to read a record it cannot check, the other a ref that is here but is no
longer the commit the gate verified.

`verifiedRef` is stored fully qualified, as `refs/remotes/origin/Playerbot`. A short
`origin/Playerbot` is ambiguous: git expands `refs/heads/` before `refs/remotes/`, so a single
`git branch origin/Playerbot HEAD` makes rev-parse, `ls-tree` and every object lookup answer for
the shadow branch instead of the remote — and `git rev-parse --symbolic-full-name origin/Playerbot`
then prints **nothing on stdout** (warning on stderr, error on stderr) while still exiting 0. The
remote-tracking test is therefore "exit 0 **and** a name starting with `refs/remotes/`"; anything
else is answered as *not remote-tracking* and refuses the boot, because the person whose forged
`--author` this rule exists to catch can create exactly that shadow branch. `audit_policy_schema.py`
exits 1 when the `verifiedRef` **field** is deleted, empty, or does not start with `refs/remotes/`,
so the qualifier cannot be dropped silently. It also exits 1 on a name carrying a revision selector
(`refs/remotes/origin/Playerbot^{tree}`, `~1`, `@{0}`) or on a bare object name: those pass the
prefix test but name something the gate cannot peel to a commit, and it refuses the boot on
exactly that (`not-a-commit`), so an audit that accepted them would exit 0 on a policy the gate
rejects (PRO-291).

"Deleted" above has always meant a deleted *field*, and a name that is well-formed but no longer
resolvable is not something a text check can see. A well-formed `refs/remotes/...` name whose
object is a tree, and a ref deleted from the clone, both used to audit clean — `0 problem(s)`,
exit 0 — while the gate refused the boot they describe (`PRO-303` measured the first on the
deployhost: `refs/remotes/origin/pro303-drill-tree` made from `git rev-parse 194697d5d^{tree}`;
`PRO-307` A2/A3 the pair). So the auditor now runs at **two depths** and prints which one ran:

- **SHAPE-ONLY** — with no `--repo`, or a `--repo` that is not a git tree: the text checks above,
  and nothing else. The output says so on its own line, because `0 problem(s)` from this pass does
  not mean `verifiedRef` resolves, is a commit, or is even still in the clone.
- **RESOLVED** — when a git repo is available: the shape checks **and** the same two-step peel the
  gate performs. A name that resolves to no object at all exits 1 as the gate's
  `upstream-ref-deleted` (or `upstream-ref-attested-missing`, once a `last-verified` record names
  it), naming `refreshCommand`; a name that resolves to something that is not a commit exits 1 as
  `not-a-commit`, naming the oid and what `cat-file -t` called it.

Neither depth fetches, and a probe git cannot answer is a problem rather than a pass — an audit
that cannot ask the question must not report the answer either (PRO-300's fail-closed rule).

Content **is** consulted. The ref is listed with `git ls-tree -z -r` — NUL-delimited, so a genuine
upstream filename like `2026_11_04_00_érfar.sql` is compared byte-for-byte instead of against git's
quoted `"...\303\251rfar.sql"` — and each company-authored file under an `origin: "upstream"` tag is
re-hashed with `git hash-object` against the blob id from that listing. A verified name whose bytes
were replaced is company content (`unverified-upstream-content`), whether the rewrite was committed
under a forged `--author` or left sitting dirty in the work tree. A case-only twin stays exempt but
is named in a `WARN`, since it is usually the first sign of a rename that the updater will not
recognise.

Honest limit: an operator with write access to the object database can still
`git update-ref refs/remotes/origin/Playerbot HEAD` and fake the third tag, blob ids included. That
is a deliberate, loud, non-default act — `git reflog refs/remotes/origin/Playerbot` shows it to a
reviewer and the next real fetch moves the ref back — where the author forge was a routine commit
command. It is also no wider a privilege than the ones already in the same file: whoever can
`update-ref` can edit `preflight-policy.json` outright.

Be precise about what the audit does and does not pin (PRO-295 review B1).
`audit_policy_schema.py` checks two things, and the CI job runs exactly it: every
`keyColumns`/`keySets`/`bands`/`refs` **column name** in `baseTables` and `customTables`
against the real `CREATE TABLE` bodies in `data/sql/base/db_world` and the module SQL, and the
`upstreamProvenance` block itself — a `verifiedRef` that is present and starts with
`refs/remotes/`, and a non-empty `refreshCommand`. It is a real pin, and it is now run in CI: the
`Write policy — engine tests and schema audit` job of
`.github/workflows/worlddb-preflight.yml` runs `audit_policy_schema.py --include-modules` and fails
the check on any problem (PRO-285), while step 3 of "Adding a table to the policy" below is what
runs it locally before a push. What it does **not** read is `includedDirs[].origin`,
`companyEmailDomains`, or the **thresholds** inside the `bands`/`refs` blocks of
`baseTables`/`customTables`: the auditor matches those blocks' column *names* against the real
`CREATE TABLE` bodies and never compares a value against a bound, so those three are review
material and nothing more (PRO-298 review N6 — the key is named `bands`/`refs`, not anything like
`reservedIdBands`, which does not exist in the policy). Deleting `data/sql/updates/pending_db_world`
from `includedDirs`, widening `companyEmailDomains` to `["*"]`, or lowering a reserved band's bounds
all edit check 4 into compliance rather than failing anything. The two partial tripwires are the falsification
recipes below (each rule is seen to fire on a crafted file, which a widened exemption stops doing)
and the corpus tallies: the sweep records how many files it attributed to `company`, and
`ci_provenance_guard.py` refuses a tally in which no scanned file is company-attributed at all, so
the `includedDirs` edit turns the check red instead of green. The other two edits still need a
reviewer, and are worth a line in any diff that touches `preflight-policy.json`. An upstream
file that trips the reserved
bands stays exempt from check 4, and the corpus `upstream` column records those 444 726 findings as
review material, not boot failures.

So **check 4 covers company content only**, by design. `--strict-all` (policy on everything,
warnings too) is not a usable boot-time setting: against this tree it fails ~754 of 861 upstream
files — 77094 reserved-band, 52967 table-not-allowed, 1666 band-not-literal, 1452 unbounded-write,
25 `DROP`, 4 on `updates`, 2 `EXECUTE` — every one of them upstream content the updater has
always applied. `--policy-all` reports the same set without failing and is what a reviewer runs by
hand; **nothing in CI runs `--policy-all`** — the CI sweep uses `--strict-all` and gates on the
`company` column only (see "In CI" below). The boot gate runs the default company-only mode.
Anything that must hold against upstream too is a review question, not a boot gate.

## Write policy (check 4)

For a file under policy, every statement must satisfy:

- no `ALTER` / `DROP` / `TRUNCATE` / `RENAME` / `GRANT` / `REVOKE` / `LOAD` / `CALL` /
  `LOCK` / `HANDLER` / `PREPARE` / `SET GLOBAL`, and no writes to `updates`,
  `updates_include`, `version`, `dbversion`;
- writes target only tables in `baseTables` of `preflight-policy.json`, or a table
  explicitly whitelisted in `customTables`; `CREATE TABLE` is allowed for whitelisted
  custom tables only;
- every `UPDATE`/`DELETE` writes **one** table. A second table anywhere in the write region —
  `UPDATE creature c JOIN updates u SET ...`, `UPDATE a, b SET ...`, `DELETE a, b FROM a JOIN b` —
  is a `multi-table-write` failure, and every table named in that region is still checked against
  the ledger and the allow-list, so a JOIN cannot smuggle `updates` past check 4;
- **aliases are names, not write targets.** `UPDATE \`creature\` \`c\` SET ...` writes one table; `c`
  is reported as `other names in region: c` and is never tested against the allow-list, so an alias
  cannot manufacture a `table-not-allowed` (rev-3 NB-2). The syntax test for a second table ignores
  what is inside backticks, so an alias spelled like syntax — `UPDATE \`creature_template\` \`join\`
  SET ...`, `DELETE FROM \`creature\` \`using\` WHERE ...` — is still a single-table write (NB-3);
  a real `JOIN` is refused regardless of the quoting;
- every `UPDATE`/`DELETE` is `WHERE`-bounded on a **complete** natural key: every column of one
  of the table's `keySets` groups compared to literals with `=`/`<=>`/`IN`, no `OR` that decides
  which rows are reached. Grouping parentheses do not hide a disjunction
  (`WHERE (source_type=0 OR entryorguid=9002020)` fails), a key bounded only on one side of an
  `OR` does not bound the statement, a column bounded only inside `IN (SELECT ...)` does not
   count at all, and `NOT (entry=...)` bounds nothing — the failure says so, naming the negated
   group as the reason rather than blaming the key columns that are present (rev-3 NB-5).
   `key = 1 AND (x='a' OR x='b')` stays legal.

  Predicates after the key are read up to the first top-level `ORDER BY`/`LIMIT`, so trailing
  clauses cannot make a literal look like a non-literal.
  With no `keySets`, the whole `keyColumns` list is one required group. Bounding on part of a
  composite key is not enough (`DELETE FROM module_string WHERE module='x'` deletes the whole
  module);
- keywords inside string literals are inert: `'Please select your destination, hero;'` in
  `page_text` is data, not a statement separator or an `OR` (the masker blanks literal bodies
  while preserving offsets);
- every row that identifies a company creature/quest sits in the reserved band
  (`creature_template.entry` / `quest_template.ID` >= 900000, `creature.guid` >= 9000000,
  plus the reference columns in `refs`), as a literal in a named column;
- no `INSERT ... SELECT`, no `ON DUPLICATE KEY UPDATE`, no `INSERT SET` syntax.

`INSERT`, `UPDATE` and `DELETE` may all carry a `PARTITION (pN)` clause (`INSERT` puts it before
the column list; both positions are parsed). The clause picks partitions, not rows, so it changes
nothing about which ids a statement touches and every id is still band-checked (rev-3 NB-4).

## Measuring the policy over the whole corpus

`ops/preflight/corpus_sweep.py` runs the same check-4 engine over every `.sql` in the four included
directories (3 060 files / 131 625 statements at the time of writing) and tallies findings per rule,
per provenance class and per directory. It is the only honest way to review a parser change: the
boot gate only policy-checks files the updater has not applied yet, so a regression that starts
refusing upstream syntax is invisible to it until someone tries to boot.

```
python3 ops/preflight/corpus_sweep.py --out /tmp/before.json --measure-only   # baseline capture
# ...change the engine...
python3 ops/preflight/corpus_sweep.py --diff /tmp/before.json   # per-rule + per-rule company deltas
python3 ops/preflight/corpus_sweep.py --rule table-not-allowed  # offending file:line
```

The number that matters is the `company` column: it must stay 0, because a finding in
company-authored SQL is a boot refusal. It is therefore also the exit status — a non-zero company
total, or a `--diff` that shows findings migrating `upstream → company` at constant totals, exits
1, and the failing run prints each company row as `file:line: rule: detail` so the log names the
file and the rule instead of sending the reader to `--rule` (PRO-179 review NB-C, PRO-285).
`--measure-only` returns 0 regardless, which is what a baseline capture needs. The `upstream`
columns are the corpus the gate exempts by default, and are measured with `--strict-all` to see how
much syntax the engine understands; they are **reported, never gated**. `liveDatabase` is read from
the policy file, so `cross-schema-write` is measured against the schema the gate actually uses.

The second invariant the sweep enforces (PRO-179 review rank 3) is printed on its own line:

```
region-scan consultations=1 multi-table-write=1 unmatched=0
```

`_region_tables()` is the report text for a statement the gate has **already refused**; it is not a
second way to pass. Over the whole corpus it must therefore be consulted exactly once per
`multi-table-write` finding, and a non-zero `unmatched` exits 1: either a multi-table write is now
accepted while still consulting the scan — a policy change wearing a report fix — or a refusal
stopped naming the tables it rejected. The unit-test twin of this property is
`TestRev5RegionScanImpliesRefusal`, whose `REJECTED` list deliberately includes an all-custom-table
`JOIN`, because "let it through when every table is one of ours" is the edit that would otherwise
leave every other literal green.

Both name lists a refusal prints — `tables named` and `other names in region` — come out of the
same bare-name scan, so when that scan is what produced them both carry the
`(heuristic — unquoted region)` suffix, and neither carries it when every name was quoted
(PRO-179 review rank 4). A schema is likewise believed only when the writer quoted the half that
claims one, decided per component from the token's own backticks: `` `other`.updates `` names
`other`, `a.b.c` names nothing, and `` `acore_world`.`my.table` `` is one table called `my.table`
(PRO-179 review rank 1).

A rule that reports 0 everywhere proves nothing until it has been seen to fire, so the provenance
rules are falsified the same way — forge the provenance in a scratch work tree, keep the work tree
clean, and sweep **without** `--strict-all`:

```
git worktree add --detach /tmp/poc HEAD
cp ops/preflight/{preflight_worlddb.py,preflight-policy.json,corpus_sweep.py} \
   /tmp/poc/ops/preflight/                                     # the engine under review
cd /tmp/poc
echo "INSERT INTO \`creature_template\` (\`entry\`,\`name\`) VALUES (1234,'x');" \
  > data/sql/updates/db_world/2026_11_05_99.sql
git add -- data/sql/updates/db_world/2026_11_05_99.sql          # `commit -a` never stages a new file
git commit -q --author="Someone <x@azerothcore.org>" -m "add upstream creature_template row"
python3 ops/preflight/corpus_sweep.py --measure-only            # company column must become non-zero
```

The `git add` is not decoration, and an earlier revision of this recipe omitted it: with the file
left **untracked**, what moves the company column is the untracked/unknown-author path, not
`unverified-upstream-origin`, so the number measures a different rule than the one under test.
Staged and committed as written above, the same tree measures:

| engine (crafted file present) | under-write-policy | company findings |
| --- | --- | --- |
| pre-PRO-248 (`2c4c8dbf9`) | 7 | none — the forged `--author` bought the exemption |
| PRO-248 | 8 | `unverified-upstream-origin` 1, `reserved-band` 1 |
| PRO-248, file returned to the ref | 7 | none |

The 1234 is deliberately out of band, so a second rule fires too; with an in-band id the
provenance rule is the *only* thing that fires, which is the case the rule exists for. The content
half is falsified from the other direction — a file that genuinely is in the ref, with its bytes
replaced (`git hash-object` against the ref's blob id):

```
T=$(git ls-files data/sql/updates/db_world | grep '\.sql$' | head -1)   # not head -1: .gitkeep
printf '\nINSERT INTO `creature_template` (`entry`,`name`) VALUES (900200,"x");\n' >> "$T"
git add -- "$T"; git commit -q --author="Someone <x@azerothcore.org>" -m "tighten defaults"
python3 ops/preflight/corpus_sweep.py --measure-only   # unverified-upstream-content company=1
```

That rewrite registers the same single company finding whether it is committed under a forged
author or left dirty in the work tree, and both return to 0 once the file is restored. Two traps in
a detached work tree: `git reset --hard HEAD` does **not** undo the crafted commit (reset to the
base commit), and that reset also reverts `ops/preflight/` — re-copy the engine files after it or
you will sweep the engine you just replaced.

### In CI (`.github/workflows/worlddb-preflight.yml`)

The sweep is wired up (PRO-285). On a pull request touching `ops/preflight/**` or `data/sql/**`,
and on a push to `Playerbot` touching the same paths (PRO-295 review N1 — a merge that never opened
a PR otherwise lands an engine change with no sweep), two jobs run. Both are pure Python against the
checkout: **no MySQL, no Docker, no deploy host**, and nothing in either job shells out to a database.

Neither job is a required status check, and neither should be made one (PRO-295 review N2). Both
triggers are `paths:`-filtered, so a PR outside that scope starts no run at all, and a required check
that never starts sits at "Expected — waiting" forever. Read the check; do not gate a branch on it.

| Step | Command | Verdict |
| --- | --- | --- |
| engine tests | `python3 ops/preflight/test_preflight_worlddb.py` | gates |
| schema audit | `python3 ops/preflight/audit_policy_schema.py --include-modules` | gates |
| corpus sweep | `python3 ops/preflight/corpus_sweep.py --strict-all --out "$RUNNER_TEMP/corpus-sweep.json"` | gates on the `company` column |
| provenance guard | `python3 ops/preflight/ci_provenance_guard.py "$RUNNER_TEMP/corpus-sweep.json"` | gates |

Measured on the runner and on a 6-core box, so the wait is predictable rather than mysterious:
the engine tests are ~2 s and the schema audit ~4 s, the sweep is **4 min 14 s on the runner**
(4 min 33 s locally, both with the default 8 workers) and the guard under a second — the whole
workflow is 5–6 minutes, and `timeout-minutes: 30` on the sweep job is that measurement with room,
not a guess. The sweep's cost is the per-file `git log -1 -- <path>` author lookups, which is also
why it is the step that gets a full checkout.

What a trustworthy tally for this tree looks like, from the head of the branch that added this
section — compare a CI tally against it before reading a green:

```
head=6582d07fe files=3060 under-policy=3060 statements=131625 findings=444726
provenance[company=7 upstream=3053 unknown=0] companyFindings=0 rowsListed=0
history[commits=20276 shallow=False authors=8]
region-scan consultations=1 multi-table-write=1 unmatched=0
```

Read the verdict column literally.

- **gates** means the check is red when the command fails, and the commands are the ones above,
  verbatim, un-wrapped. Reproduce a failure locally by checking out the PR head and running the
  same line; the log names the rule per finding (`data/sql/updates/pending_db_world/x.sql:12:
  table-not-allowed: ...`). One substitution is needed: `$RUNNER_TEMP` is set by Actions and is
  unset in a shell, so locally write the tally somewhere that exists —
  `python3 ops/preflight/corpus_sweep.py --strict-all --out /tmp/corpus-sweep.json` — and pass that
  path to the guard. An unusable `--out` (or a missing `--diff` baseline) is now refused at startup
  with exit 2 instead of after ~4.5 minutes of `git log` (PRO-295 review N3).
- The `upstream` columns **report only**. This tree measures ~444 700 upstream findings and always
  will; a number moving there is information for the reviewer, not a merge failure. That is the same
  decision the boot gate makes about upstream content, written down instead of left to whoever reads
  a log.
- The guard is not a second policy verdict — company findings are `corpus_sweep.py`'s to fail on.
  It fails a run whose provenance was never read out of history. `git_author_origin()` asks
  `git log -1 -- <path>` per file, so the **checkout** decides the answer as much as the corpus
  does, and a shallow clone is worse than unattributable: its grafted root answers for *every*
  path, so each file inherits the tip author and the `unknown` column stays empty to warn nobody.
  Measured on a `--depth 1` clone of this repo, all 3 060 scanned files came back `company` and
  444 726 findings landed in the gated column — a red check with the entire upstream corpus in the
  dock. Give the same checkout an upstream-authored tip, which is what `Playerbot` normally has,
  and all 444 726 move to `upstream` and the column the check gates on reads **0**. So the sweep
  records `shallow`, `commits` and `authors` in its tally and the guard refuses a shallow repo, a
  one-commit history, one distinct author across thousands of files, or a corpus with no upstream
  files at all. The same question is asked of the column that is actually gated: a tally in which
  **no scanned file is company-attributed** is refused too, because `company=0` is what that column
  reports whether the rules are satisfied or the company directories were never in the sweep (delete
  `pending_db_world` from `includedDirs`, or check out a tree without it, and the check goes green on
   an empty verdict — PRO-295 review B1). That refusal reads the shape the sweep really writes:
   `provenanceFiles` is a `dict(Counter(...))`, so a class with no members is an **absent key**, not
   a `0` — drop `pending_db_world` from `includedDirs` and the tally says
   `provenance[upstream=3053]` with no `company` at all (PRO-298 review N5). And it refuses a tally
   that measured nothing: `statements=0`,
   `findings=0`, no rule in `byRule`, or no `companyFindingList` to check the gated count against, each
   on a corpus of thousands of files. Those four are the shapes a dead engine leaves behind — an
   emptied `forbiddenStatements`, a rule registry that failed to load — and a green from a check that
   ran but parsed nothing must not look like a green from a clean corpus (PRO-295 review B2). The
   refusal names the shape of the defect, not a frozen finding count: the measured baseline lives in
   the block above, where its head is pinned, rather than in an error message that goes stale the
   first time the corpus legitimately changes (PRO-298 review N7). It also refuses a tally that does
   not add up against itself — a `findings` total the `byRule` counts contradict, or a rule in `byRule`
   with no `byRuleOrigin` split, or a split whose origins do not sum to its rule's count. The last is
   not bookkeeping: the `company` column is counted out of `byRuleOrigin`, so a rule that is missing
   there shows up in the report and contributes nothing to the exit status (PRO-298 review N8). The
   sweep job also checks out with `fetch-depth: 0`; the guard is the net, not the
   fix.
- The guard runs **after a red sweep too** (`if: ${{ !cancelled() }}`), because a provenance
  collapse and a genuine policy violation look identical in the log: both print a `company` column
  full of rows. The run that just went red is exactly the run where "is this verdict attributable"
  still has to be answered, so the sweep writes the tally before it exits and the guard reads it
  either way.

What a green check does **not** say: it is not a boot rehearsal. None of these steps reads the
`updates` ledger, hashes pending files, replays them into a scratch schema or checks a compose
mount — those belong to the boot gate (see "Dry run (check 5)"). Green here means the write-policy
engine agrees with itself over the corpus in this checkout, and that the corpus was attributable.

### The ref the sweep attributes against, and the clone that has never held it (PRO-430)

`corpus_sweep.py` answers the upstream question the way the boot gate does: it resolves the ref the
policy names with **one** `git ls-tree -z -r <ref>` for the whole run (cached, so the tree is walked
once), then compares each upstream-tagged file against the blob that ref holds with
`git hash-object -- <path>` and reads its author with `git log -1 -- <path>`.
`ci_provenance_guard.py` runs no git at all — it imports `argparse`, `json`, `re`, `sys` and
`pathlib`, reads the JSON tally the sweep wrote with `--out`, and checks that tally's shape and
arithmetic. The ref is consulted by the sweep alone, which is exactly why the `provenance` column has
to be read before the guard's exit code is trusted: a checkout the sweep could not attribute leaves
the guard nothing to fail on except the sweep's own numbers.

```
upstreamProvenance.verifiedRef    = refs/remotes/origin/Playerbot
upstreamProvenance.refreshCommand = git fetch --prune origin Playerbot
```

The ref the sweep needs is that canonical `refs/remotes/...` name — not `Playerbot`, not `HEAD`,
not a local branch of that name — and it has to be a **remote-tracking ref in this clone**, because
both the upstream exemption and the `company` column the check gates on are read out of it.

What a `git clone --single-branch` of some other branch does to that depends on one more thing about
the clone — whether it holds a fetch fingerprint (see "`missing`, `indeterminate`, `unchecked`"
above) — and only one of the two states is the dangerous one:

- **No fetch fingerprint.** A pristine single-branch clone nobody has run `git fetch` or `git pull`
  in yet. `git clone` writes neither a `FETCH_HEAD` nor a reflog for a remote-tracking ref, so the
  answer is `unchecked`, the exemption stands, and the sweep reports the baseline block above (there
  at `head=6582d07fe`, `commits=20276`; the same figures here at `head=34099b85e61a`, where the
  history line reads `commits=20372`).
- **Any completed fetch.** One `git pull`, one `git fetch --prune origin <other-branch>`, or the
  `git fetch --depth <n> origin <branch>` an `actions/checkout` limited-`fetch-depth` job runs
  (measured to write `FETCH_HEAD`, where `git clone --depth 1 --single-branch` writes none — a
  *fetch* is the fingerprint, a partial clone is not). `FETCH_HEAD` now names a fetched ref, so the
  gate can no longer tell "the remote never carried the branch" from "someone deleted it", fails
  closed (`indeterminate` / `ref-deleted`), and every file in an upstream-tagged directory loses the
  exemption in one step.

Measured on a `git clone --local --single-branch --branch <a branch that is not Playerbot>` of this
repo plus one `git fetch --prune origin <that branch>`, then the workflow's own two command lines.
What the guard prints for that run is a single line; the `guard: ` prefix below is literal and the
line is reflowed here for width:

```
guard: head=34099b85e61a files=3060 under-policy=3060 statements=131625 findings=447779
provenance[company=3060 upstream=0 unknown=0] companyFindings=447779 rowsListed=2000+
history[commits=20372 shallow=False authors=8]
```

Read the `history` line before the verdict: 20 372 commits, 8 authors, not shallow, so every
shallow/grafted refusal the guard has passes — and it still exits 2, on the one clause that fits,
printed on stderr as `GUARD FAIL:` followed by "no scanned file was attributed to `upstream` even
though the included directories are mostly upstream archive and updates: every file was given the
same origin, which is what a checkout with no real history looks like. The `company` column in that
tally is not a policy verdict." The sweep steps out of the way first with exit 1 on its non-zero
`company` column, and that `447779` is the number a reader takes for a policy verdict. The same head
with the ref present measures
`findings=444726 provenance[company=7 upstream=3053 unknown=0] companyFindings=0` (with
`refs/remotes/origin/Playerbot` itself at `34099b85e61a` — a pin the prose has to carry, since the
tally JSON records no ref oid) and the guard exits 0, so the difference is exactly one
`upstream-ref-deleted` finding per upstream-tagged file — 3 053 of them, the only rule `byRule` gains
on this path and a rule that never appears on a clone holding the ref. A `company` column that large
is a checkout defect, not 447 779 boot refusals, and refusing to read it as a verdict is the guard's
whole job (PRO-295 review B1, PRO-422 review N4).

Fix the clone. The ref has to be a remote-tracking ref, not merely objects in the object store:

```
git remote set-branches origin Playerbot && git fetch --prune origin      # '*' for every branch
git fetch --prune origin +refs/heads/Playerbot:refs/remotes/origin/Playerbot   # one-off refspec
```

Either one lands `refs/remotes/origin/Playerbot` in the clone above, and both forms were measured
there: each returns `provenance[company=7 upstream=3053 unknown=0] companyFindings=0 rowsListed=0`
at the same head and the same ref tip, the sweep exits 0 and the guard exits 0 with its "the tally is attributable"
line. Note what does not: the policy's own `refreshCommand`, run verbatim in that single-branch clone, fetches the
commits and writes `FETCH_HEAD`, but `remote.origin.fetch` maps only the branch the clone tracks, so
`refs/remotes/origin/Playerbot` is still absent afterwards — and the `FETCH_HEAD` it just wrote is the fingerprint that
keeps the sweep on the fail-closed path. It is the right hint for a clone that tracks every branch and lost the ref, and
not enough for one that never tracked it; the two forms above work for both. `--allow-missing-upstream-ref` is not an
answer here either: it is the boot gate's typed opt-in back to the pre-PRO-248 exemption, and `corpus_sweep.py` never
sets it — its hand-rolled `Args` has no such attribute, so `allow_missing_upstream_ref()` is `False` on every sweep run
— which leaves the fetch as the only way a sweep can speak for the corpus at all.

## Dry run (check 5)

The pending files are replayed, in `std::set`-ordered basename, into
`acore_world_preflight_<pid>_<stamp>` created with `CREATE TABLE ... LIKE`. By default only
the tables the files mention plus their FK ancestors are cloned (`--clone-tables all` for
the full schema) and the written tables are pre-populated server-side so `WHERE` clauses
match real rows; `FOREIGN_KEY_CHECKS` stays on unless a referenced table was too big to
copy (`--data-copy-max-rows`, which then warns under `dry-run-coverage`). The live schema is
only ever read. `--clone-mode full` (mysqldump) and `minimal` (structure only, FK off)
remain for debugging. `--no-dry-run` skips it and says so — check 5 renders a `dry-run-skipped`
warning rather than an `ok`, so a report can never read as though the updater had been replayed.

`--keep-scratch` leaves `acore_world_preflight_<pid>_<stamp>` in place after the run instead of
dropping it, so a failed dry run can be queried afterwards; nothing else reads that schema, and
the next run drops it before creating its own.

The scratch name is validated against a hard-coded prefix (`acore_world_preflight_`) plus the
live database name and the reserved MySQL schemas **before** the `DROP DATABASE` that opens the
dry run. There is no operator-settable scratch prefix.

## Ledger and hash semantics

- `updates.name` is a `utf8mb4_unicode_ci` primary key, so the gate looks files up
  case-insensitively, exactly like the updater.
- An empty `updates.hash` is a warning, not a failure (`hashCheck.emptyLedgerHashIsWarning`):
  the updater re-hashes and re-inserts such a row (`MODE_REHASH`) rather than re-applying it.
- A hash mismatch where the bytes match *another* ledger row is reported as `hash-rename`:
  upstream answers that with `RenameEntry()` (`UpdateFetcher.cpp:335-345`), not a re-apply.
  A mismatch with genuinely new bytes is `hash-mismatch` and is the silent re-apply case
  (`UpdateFetcher.cpp:350-354`).
- Orphaned ledger rows (rows with no file on disk) are listed for the non-`MODULE` ones, but the
  `--clean-dead-ref-max-count` threshold counts all of them, because `UpdateFetcher` counts all
  of them before it deletes anything (`UpdateFetcher.cpp:409-416`).

## Adding a table to the policy

`baseTables` / `customTables` are the only place to extend this; there is no code change.

1. Find the real DDL — `data/sql/base/db_world/<table>.sql`, or the module SQL that creates a
   custom table. `keyColumns` must be the actual primary key columns; use `keySets` when a
   partial key is legitimately selective (e.g. `creature` → `[["guid"], ["id1","map"]]`).
2. Add `bands` for columns that carry a company content id and `refs` for columns that reference
   one.
3. `python3 ops/preflight/audit_policy_schema.py` checks every policy table/column against the
   DDL in `data/sql/base/db_world` **and** the module SQL that creates the `customTables`
   (`modules/`, `wrath-unbound/modules/`, `nodeps/modules/`, discovered with `git ls-files` so a
   clean clone sees them too). `--no-modules` skips the module half and says so per unchecked
   table; it never skips silently. Pass it the **same** `--policy` (and `--repo`) you pass the
   gate: the auditor otherwise reads the policy sitting next to itself, so a gate run with
   `--policy /some/other.json` would be audited against a document it is not enforcing — a
   policy the auditor never opened can pass while the file that decides the boot is not (PRO-248
   review N5). `python3 ops/preflight/audit_policy_schema.py --policy "$POLICY" --repo "$TREE"`.
   Read the `provenance check:` line before believing `0 problem(s)`: `SHAPE-ONLY` means the
   auditor only read the `verifiedRef` *name* and never asked git whether it resolves, is a
   commit, or is still in the clone; `RESOLVED` means it asked, in the `--repo` you named
   (PRO-307 A2/A3 — see "the two depths" above).
4. `PYTHONPATH=ops/preflight python3 ops/preflight/test_preflight_worlddb.py` before committing
   (plain `python3 -m unittest discover -s ops/preflight` fails: the directory is not a package).

## Out of scope

`Updates.EnableDatabases` is `7` in `src/server/apps/worldserver/worldserver.conf.dist:299`; the
container environment sets `AC_UPDATES_ENABLE_DATABASES=4`, which is what the gate validates
against (check 0b). The Playerbots updater (`acore_playerbots`,
`AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES=1`) is a separate updater and is only reported, never
touched.
