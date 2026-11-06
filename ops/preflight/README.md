# World-DB updater pre-flight gate (PRO-179)

`preflight_worlddb.py` is the gate that runs **before** `docker compose up -d ac-worldserver`.
It refuses the boot unless the four `db_world` directories that
`acore_world.updates_include` registers are in a state the updater can be trusted with.

    ops/deploy/worldserver-up.sh              # lease check + gate, then compose up
    python3 ops/preflight/preflight_worlddb.py  # gate only (exit 0 pass, 1 fail, 2 operational error)
    PYTHONPATH=ops/preflight python3 ops/preflight/test_preflight_worlddb.py  # policy-engine tests, no DB needed

`PREFLIGHT_CHECK_ONLY=1 ops/deploy/worldserver-up.sh` runs the gate without touching the
stack — that is the CI / merge-gate rehearsal form. The wrapper **verifies** the single-writer
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
| `unchecked` | `git rev-parse --verify --quiet <ref>` finds nothing at all, **and** this clone has no local trace of the ref — no reflog, no `FETCH_HEAD`, no sibling reflog and no `last-verified` record naming it | `WARN`, exemption kept — a clone that has genuinely never fetched still boots offline, and the report names `refreshCommand`. A plain `git clone` of a remote that never carried the ref is exactly this state |
| `indeterminate` / `ref-deleted` | the ref is gone **but** this clone has fetch history: `ref`'s own reflog, a `FETCH_HEAD`, or a sibling ref that still carries a reflog of its own | `FAIL`, failing closed, naming `refreshCommand`; only `--allow-missing-upstream-ref` walks it back to the `WARN` |
| `indeterminate` / `attested-missing` | the ref is gone and none of those three signals is present, **but** this clone's `last-verified` record says a satisfied run of this gate read this very ref at a named commit at a named time | `FAIL`, failing closed, naming the attested oid, the timestamp, the record path and `refreshCommand`. A clone that genuinely never fetched has no attestation to lose, so this is one whose fetch fingerprints were removed after the fact; `--allow-missing-upstream-ref` walks it back to the `WARN` exactly as it does `ref-deleted`, because it is the same claim with a different witness |
| `indeterminate` / `attestation-untrusted` | the ref is gone and a record for it exists that this gate cannot vouch for — unreadable, holding a line it cannot parse, unversioned or of a version it does not read, or written for another repo or another remote url | `FAIL`, failing closed, naming the record path and the reason. "The record is unreadable" must not be the same sentence as "there is no record", or truncating a file is all it takes to buy back the exemption the record was blocking |
| `indeterminate` / `oid-mismatch` | the ref resolves to a commit, and the commit recorded for it is neither that commit nor an ancestor of it, and the ref carries no reflog of its own | `FAIL`, failing closed, naming both oids. A fetch fast-forwards a remote-tracking ref and a real force-push leaves a reflog, so this is the shape of `git update-ref` pointing the tracked ref at commits of the operator's choosing — and `ls-tree` is never asked of it |
| `indeterminate` / `not-a-commit` | the name resolves to an object that is not a commit — `HEAD^{tree}`, a blob, a mistyped object name, the null sha | `FAIL`, failing closed, naming `refreshCommand`. It is not missing, and calling it `missing` was the false statement that kept the exemption |
| `indeterminate` / other | git errored, or the ref is not under `refs/remotes/` | `FAIL`, failing closed |

Before PRO-291 both of the last two rows were filed as `missing`, so a `verifiedRef` pointed at a
tree object booted a forged-author file on a `WARN` — the reason `git rev-parse --verify <ref>^{commit}`
is no longer the existence test. It is now a two-step question: peel to a commit, and if that fails
ask separately whether the name resolved to *anything* (`rev-parse --verify --quiet <ref>`, with
`cat-file -t` naming what came back). Only a name that resolves to nothing at all may be `missing`.
`git ls-tree` is never asked of a ref the gate does not trust — including one whose recorded commit
git cannot place (PRO-300) — and none of these probes fetch.
Every git call the gate makes — the ref probes, the author lookup, the work-tree `git status`, and
the attestation's `rev-parse --git-common-dir`, `config --get remote.<r>.url` and
`merge-base --is-ancestor` (PRO-300) — goes through the one `_git()` helper, so "nothing in here
reaches the network" is a claim about a single call site rather than about most of them (PRO-291
review N5).

The `ref-deleted` discrimination is local history only — three signals, no network. What each one
tests is *fetching*, so each is a file a fetch writes and `git clone` does not:

- the ref's own reflog, at `git rev-parse --git-path logs/<ref>`;
- `FETCH_HEAD`, which `git fetch`/`git pull` writes and `git clone` does not;
- any sibling ref under the same `refs/remotes/<remote>/` namespace other than `<remote>/HEAD`
  that **carries its own reflog** (`for-each-ref`, not `show-ref` — `show-ref` matches anchored
  whole ref names, so a bare namespace prefix answers "no refs").

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
`missing`, `not-a-commit`, `not-remote` and an untrusted record all refuse before the write, so a
ref that is not there can never fabricate a record of having been verified. And a fresh `git clone`
gets a common dir with **no record in it**, which is the part AC 6 rests on: an untouched clone of a
remote that never carried the ref has nothing to find, so its answer is still `absent` and it still
boots offline on the `WARN`. The location is asked from git (`rev-parse --git-common-dir`) rather
than hardcoded as `.git` for the same reason `_reflog_path()` asks `--git-path` — in a worktree the
admin dir is not `.git` — and when git will not answer there is no state directory at all, never a
guessed one: a probe nobody answers must not become a finding, and an attestation written somewhere
untrustworthy is worse than none. `--preflight-state-dir` relocates it for a test or for someone
running the gate over a tree they do not want written inside. The write is a temp file plus
`os.replace`, because a record truncated by a crash mid-write would then fail closed every later
boot, and a failure to write is an informational note rather than a finding: the record is evidence
*for* a later deletion check and must never become a precondition for booting.

From then on `missing` is not one answer. A ref with a record that has gone is
`indeterminate`/`attested-missing`, not `unchecked`. An ordinary `git pull` between boots is not
suspected: the recorded commit is an ancestor of the one the ref names now, `git merge-base
--is-ancestor` says so, and the record moves forward with the run. A ref whose recorded commit git
cannot place, with no reflog to explain the move, is not the history this gate verified, so
`ls-tree` is never asked of it and the boot is refused (`oid-mismatch`); a genuine force-push is
cleared by the reflog it leaves behind. And an attestation the gate cannot trust is never a pass —
an unparsable line, a missing or unknown `version`, a `repo` line naming some other common dir, or a
`remote url` that is not this tree's each refuse the boot with the reason named, because copying a
record in from a clone that really did fetch is the obvious way to fake a satisfied past and those
two lines exist to catch it. A record that simply holds no `last-verified` line for *this* ref is
`absent`, not untrusted: pointing `verifiedRef` at a new name starts a fresh history instead of
failing on a record earned for the old one. Section 4 says which of these it used — the summary line
carries `last-verified <oid>@<when>` for the record a satisfied boot relied on, and a separate
`upstream-ref-attestation` INFO line names the record file.

What the attestation does **not** guarantee, since a half-told guarantee is worse than none:

- It is a file. Anyone who can `rm` it has deleted the gate's memory of verifying the ref, and the
  next `missing` answer looks like the honest `absent` again. What the record changes is the shape
  of the forgery: `update-ref -d` alone now trips a rule, and getting back to a quiet `WARN` means
  deleting the ref, its fingerprints and this file — inside a checkout whose owner can equally edit
  `preflight-policy.json` outright. Against someone who owns the working tree this is friction, not
  a defense, and it is no wider a privilege than the `update-ref` described under "Honest limit"
  below.
- It refuses a **silent** move, not a move. `git update-ref` writes a reflog entry by default, and a
  reflog is exactly what the `oid-mismatch` branch treats as an innocent explanation — it is what a
  real force-push leaves behind too. So the attestation catches the `update-ref` whose reflog was
  then removed; the one that leaves its reflog in place stays the deliberate, loud act described
  under "Honest limit" below, where a reviewer running `git reflog refs/remotes/origin/Playerbot`
  sees it and the next real fetch undoes it. Anything stronger would refuse an ordinary
  force-push-based upstream workflow, which is not this gate's job.
- It signs nothing and authenticates no clock. `attestedWhen` is this host's UTC time at the moment
  a satisfied run wrote the line; the claim it supports is "this gate read this commit through this
  ref here", not "upstream published this then".
- Nothing outside this checkout reads it, and nothing schedules it. There is still no CI job for
  `ops/preflight` — nothing under `.github/workflows/` references it — so a record exists only where
  the boot path in `ops/deploy/worldserver-up.sh` has actually run the gate and passed. A clone that
  has never run the gate has no record, and looks exactly like a clone of a remote that never carried
  the ref. That is the honest state, and it is a `WARN`, not a failure.

The three signals are not equally strong, and the report says which one it used. `ref`'s own reflog
is a record of *this* ref having been here, so the message says it was deleted rather than never
fetched. A `FETCH_HEAD` or a sibling reflog only proves somebody fetched, which leaves "the remote
never had this branch" standing; the gate still fails closed on that, but it says the ref was
either deleted or never existed here, because a boot refusal must not assert a deletion it did not
observe.

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
exits 1 when `verifiedRef` is deleted, empty, or does not start with `refs/remotes/`, so the
qualifier cannot be dropped silently. It also exits 1 on a name carrying a revision selector
(`refs/remotes/origin/Playerbot^{tree}`, `~1`, `@{0}`) or on a bare object name: those pass the
prefix test but name something the gate cannot peel to a commit, and it refuses the boot on
exactly that (`not-a-commit`), so an audit that accepted them would exit 0 on a policy the gate
rejects (PRO-291).

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
`update-ref` can edit `preflight-policy.json` outright (`companyEmailDomains`,
`includedDirs[].origin`, `reservedIdBands`), which is why the policy is pinned by
`audit_policy_schema.py` rather than by this check. That pin is real but manual: step 3 of
"Adding a table to the policy" below is what runs it, and a reviewer re-runs it — there is no
CI job for it, and nothing under `.github/workflows/` references `ops/preflight`. An upstream
file that trips the reserved
bands stays exempt from check 4, and the corpus `upstream` column records those 444 726 findings as
review material, not boot failures.

So **check 4 covers company content only**, by design. `--strict-all` (policy on everything,
warnings too) is not a usable boot-time setting: against this tree it fails ~754 of 861 upstream
files — 77094 reserved-band, 52967 table-not-allowed, 1666 band-not-literal, 1452 unbounded-write,
25 `DROP`, 4 on `updates`, 2 `EXECUTE` — every one of them upstream content the updater has
always applied. `--policy-all` reports the same set without failing and is the form a review
or a sweep runs by hand; the boot gate runs the default company-only mode. Neither form is
wired into CI — nothing under `.github/workflows/` references `ops/preflight`, so the gate is
only as strong as the boot path in `ops/deploy/worldserver-up.sh` that runs it. Anything that
must hold against upstream too is a review question, not a boot gate.

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
1 (PRO-179 review NB-C). `--measure-only` returns 0 regardless, which is what a baseline capture
needs. The `upstream` columns are the corpus the gate exempts by default, and are measured with
`--strict-all` to see how much syntax the engine understands. `liveDatabase` is read from the
policy file, so `cross-schema-write` is measured against the schema the gate actually uses.

**Be honest about the CI claim:** the exit status is *shaped* for CI, but nothing in
`.github/workflows/` calls `corpus_sweep.py` today, so nothing fails a pull request on a parser
regression. A run over the whole corpus costs a few minutes (the per-file `git log` author lookups
dominate), which is the actual reason it is not wired up yet. Until someone adds the workflow,
treat the sweep as a review step you run, not a check that runs you.

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
4. `PYTHONPATH=ops/preflight python3 ops/preflight/test_preflight_worlddb.py` before committing
   (plain `python3 -m unittest discover -s ops/preflight` fails: the directory is not a package).

## Out of scope

`Updates.EnableDatabases` is `7` in `src/server/apps/worldserver/worldserver.conf.dist:299`; the
container environment sets `AC_UPDATES_ENABLE_DATABASES=4`, which is what the gate validates
against (check 0b). The Playerbots updater (`acore_playerbots`,
`AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES=1`) is a separate updater and is only reported, never
touched.
