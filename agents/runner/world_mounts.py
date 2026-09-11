"""Mounting a world's halves into a trajectory's environment sandbox.

THE RUNTIME HALF of a decision made on the server. `internal/archipelago/service.py`
answers "may this run mount, and is there an image" — both are Postgres reads, and
this process has no database — then sends the conclusion in the agent config. So
nothing here decides anything: an empty `world_mounts` means download, which is what
every ordinary case collapses to (flag off, no image, expired, no world).

WHY THIS IS A PORT AND NOT AN IMPORT. `packages/environment_runner/world_mounts.py`
does the same job for playgrounds, and sharing it is impossible rather than merely
awkward: this package cannot import `packages.*` — it reaches the server over HTTP —
and the server-side version's entry point is a database lookup. What is duplicated is
the SHELL work, which is the part that has to agree; the two must stay in step on
three things, each of which has a measured failure behind it:

  * the digest command (`_MANIFEST_SHA_COMMAND`), or a mount verifies against a
    digest computed differently from the one recorded and every mount is refused;
  * the write bits a mounted file must carry, or an image built before hydration
    stamped modes gets served and the app meets a read-only world (#15903);
  * `-type d` on the repair, or the first metadata write to a mounted FILE copies its
    whole contents into the copy-on-write layer — 12.45s and 9.9 GB for one file on a
    real 36.95 GB world.

A MOUNT REPLACES, WHERE A DOWNLOAD MERGES. `populate_data` writes objects into the
directory already at the path, so anything the platform image itself shipped under
`/filesystem` or `/.apps_data` survives; mounting a half replaces that directory
outright, and any image-provided file the world snapshot does not also contain
disappears. `.apps_data` is the riskier half, since service data directories are
commonly seeded at image build. This is a real behavioural difference between a
mounted run and a downloaded one, not a theoretical one, and it is a reason to
confirm mounting against a specific app image before widening the flag — raised in
review, and the same warning the server-side module carries.

A MOUNT IS READ-WRITE, and the trajectory depends on it. It presents as `9p rw`
copy-on-write: the agent's writes land in a per-mount layer that is invisible to any
other mount of the same image, and reads through the mount see the merged view. That
is what makes one image safely shareable across concurrent trajectories, and it is
also why the end-of-run snapshot needs no change — it walks the tree from inside the
sandbox and sees base + writes + deletions, exactly as it would after a download.

NOTHING MAY UNMOUNT BEFORE THAT SNAPSHOT. Unmounting discards the copy-on-write layer
and the original directory reappears, so an unmount between the agent finishing and
the capture would silently throw away everything the run produced. There is no
unmount on this path today, and this comment is the reason there must not be one.

"NEEDS NO CHANGE" IS A CORRECTNESS CLAIM, NOT A COST ONE, and under `adaptive` the
capture is where the cost lands. Raised in review, and it corrects something this
module previously called "checked and clear": what was checked is that the capture SEES
the right bytes, not what it pays to read them.

`.apps_data` is the larger half, and adaptive leaves it a live 9p mount for the whole
run — so unlike `source`, where the copy had already landed it on local disk before the
agent started, every byte now faults over 9p at capture and is compressed there. A
populate win can therefore move into snapshot time rather than disappearing, and the
before/after on `populate_seconds` alone cannot tell those apart.

THE RELIABILITY EDGE MATTERS MORE THAN THE LATENCY, because it does not degrade
gracefully. The trajectory captures via `POST /data/snapshot/s3` with `format="files"`,
and that uploader gives each file `max(300s, size_MiB / 5 MiB/s)` and ABANDONS the
attempt past it (`_upload_timeout_seconds` / `_upload_with_timeout` in
`environment/runner/data/snapshot/main.py`). That floor was calibrated against local
disk: the slowest successful single-file upload measured 143s for a 1.4 GiB DB under
7-way contention, and 300s was chosen as ~2x that worst case. A full-tree read over a
9p mount measured ~4.6x slower than local disk, which is wider than the 2x the floor
carries — so on a large `.apps_data` file the failure mode is a LOST snapshot, not a
slower one. Hence the capture split in the rollout plan: it is a precondition for
flipping the flag, not a nice-to-have.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Protocol

import modal
from loguru import logger

from modal_models import WorldMount


class SupportsMounting(Protocol):
    """The part of `modal.Sandbox` that mounting actually uses.

    Structural rather than nominal for the same reason the server-side siblings are:
    demanding the whole class overstates the requirement and forces every test to
    fabricate a `modal.Sandbox`, which is how a permissive double hides a real shape
    mismatch. Each member is here because the code calls it — `exec` for the checks,
    `mount_image` to place the tree, `unmount_image` to take it back.
    """

    @property
    def exec(self) -> Any: ...

    @property
    def mount_image(self) -> Any: ...

    @property
    def unmount_image(self) -> Any: ...


# Must match `packages/snapshot_images/keys.py::MANIFEST_SHA_COMMAND` exactly. Size
# and relative path only — deliberately blind to modes and ownership, which is why
# the separate write-bit check below is not redundant.
_MANIFEST_SHA_COMMAND = (
    "find \"{mount}\" -type f -printf '%s %P\\n' | LC_ALL=C sort "
    "| sha256sum | cut -d' ' -f1"
)

# The bits a mounted file must already carry. Stated independently of the hydrator's
# `HYDRATED_FILE_MODE` on purpose: derived from it, tightening the stamp would relax
# the check in lockstep and silently disarm the guard that exists to refuse exactly
# that image.
_REQUIRED_FILE_WRITE_BITS = 0o002

# Only `/filesystem` is repaired, because only `/filesystem` is repaired on the
# DOWNLOAD path (`_make_shared_filesystem_path_writable` runs for that root alone).
# Repairing `.apps_data` would make a mounted world differ from a downloaded one,
# which is the one property this whole feature must not change.
_SUBSYSTEMS_NEEDING_REPAIR = frozenset({"filesystem"})

# Mirrors `snapshot_images.service._UNMOUNT_ATTEMPTS` / `_UNMOUNT_RETRY_DELAY_S`.
# Where a source-mounted half is staged before being copied into its real root.
# Outside both subsystem roots deliberately, and that is what makes a stale staging
# mount harmless to the capture: the end-of-run snapshot walks `SNAPSHOT_SUBSYSTEMS`
# only, so nothing here is ever uploaded. The cost of failing to clear one is the
# mount RECORD, which blocks reuse and capture — not a duplicated upload.
#
# `"adaptive"` is a THIRD value rather than a change to either existing one, and the
# `strategy: str` contract is what makes that safe: a runner predating it does not
# recognise the value and downloads the half, which is the correct fallback. Adding a
# value is therefore deployable in either order, unlike the field itself.
_KNOWN_STRATEGIES = frozenset({"destination", "source", "adaptive", "service_state"})

# `"service_state"`: a destination mount of a directory a RUNNING SERVICE owns, which
# is why it cannot be the plain `"destination"` value.
#
# THE TIMING IS THE WHOLE PROBLEM. The start script boots every service and waits for
# its port to be LISTENING before populate runs, and `apply_world_mounts` runs inside
# populate. So by the time any mount happens, mysqld has been up for a while with its
# datadir open. Mounting over that swaps the directory beneath a live process holding
# file handles into it — the mount reports success and the server keeps serving the
# tree it already had.
#
# A NEW VALUE RATHER THAN A FLAG ON `"destination"`, and that is a compatibility
# requirement rather than a style preference. A runner predating this would read
# `"destination"` for `/var/lib/mysql` and do exactly the unsafe thing; reading an
# unknown strategy, it downloads. The `strategy: str` contract makes adding a value
# deployable in either order, which is the property being used here.
_SERVICE_STATE_STRATEGY = "service_state"

# How to stop and start each service that owns one of these directories, keyed by the
# subsystem label the registration uses. Hardcoded rather than derived: the runner
# cannot ask a connector how to stop it, and a wrong guess leaves a live process over
# a swapped directory. Adding a connector means adding a row and knowing its answer.
#
# The password matches the connector's `arco.toml` `mysql-init` step and the default
# its `prebake_db.sh` uses.
_SERVICE_STATE_CONTROL: dict[str, dict[str, str]] = {
    "campaign_database_mysql": {
        "ping": 'mysqladmin -u root -p"${MYSQL_ROOT_PASSWORD:-root_password}" ping',
        "stop": 'mysqladmin -u root -p"${MYSQL_ROOT_PASSWORD:-root_password}" shutdown',
        "start": "mysqld --user=mysql --daemonize || mariadbd-safe &",
        # The account the service runs as, and therefore the one that has to be
        # able to write the mounted tree — `start` above passes `--user=mysql`.
        "user": "mysql",
    },
}

# Hand the mount root to the service's own account.
#
# MODAL IMPOSES `755 root:root` ON THE MOUNT POINT ITSELF, discarding whatever
# the image holds for that one directory. Everything NESTED keeps its modes —
# measured in dev Modal: a tree stamped `757` before imaging comes back `757` at
# every depth and `755` at the root, every time.
#
# That one directory is the one that matters. MariaDB aborts on startup without
# it, and not obscurely:
#
#     [ERROR] mariadbd: Can't create/write to file './ddl_recovery.log'
#             (Errcode: 13 "Permission denied")
#     [ERROR] Aborting
#
# So the producer's directory grant is necessary and NOT sufficient: it fixes
# every nested directory and cannot reach this one, because the mount creates it.
#
# CHOWN RATHER THAN CHMOD, and the difference is a security control rather than
# taste. Both work — measured, mysqld starts and serves on either. `chmod o+rwx`
# leaves the root world-writable at `757 root:root`; `chown` leaves it
# `755 mysql:mysql`, so the service can write and `other` cannot. The connector
# ships an opt-in `PREBAKE_RESTRICT_DATADIR_PERMS` precisely to keep the datadir
# unreadable by the delivered environment's non-root `agent` user, and opening
# the root to everyone would quietly undo it for any world using that path.
_SERVICE_STATE_CHOWN_ROOT_COMMAND = 'chown {user}:{user} "{path}"'

# Can the service's own account actually write the mounted datadir?
#
# THIS ROUTE HAS NO WRITABILITY GATE OTHERWISE, which review found and the code
# confirms: `_mounted_half_is_usable` runs the `_files_are_writable` check only
# when `shared_gid` is set (the `filesystem` route) or `restore` is set (the
# adaptive route). This route passes neither, so it returned on digest
# verification alone and the question was never asked.
#
# It matters here more than the mode-sampling gate would answer. The producer
# stamps `o+w` on FILES ONLY — `_grant_other_write_command` says so, on the
# grounds that "directories are handled by the consumer", which is true of the
# two subsystem roots and was not true of this one. A 9p mount then serves the
# image's own ownership, and `mysqld` runs as `mysql`, not root. This is the
# shape that produced the `EACCES` failures on the `.apps_data` attempt.
#
# A REAL WRITE RATHER THAN A MODE CHECK, because the mode is the inference and
# the write is the fact: uid/gid parity between the capturing sandbox and this
# one is exactly what nothing here can verify, and a probe file settles it
# without needing to. Copy-on-write absorbs it, and the digest has already been
# verified by the time this runs, so nothing downstream re-reads the tree.
#
# Failing it declines the mount — the service restarts on its own datadir and
# the run imports as today. That is the whole point: a decline costs the
# optimisation, where an unwritable mount costs the run.
_SERVICE_STATE_WRITABLE_COMMAND = (
    "su -s /bin/sh -c "
    '\'p="{path}/.mount_write_probe"; touch "$p" && rm -f "$p"\' {user}'
)

# Seconds to wait for the service to stop, and to come back. Stopping a 6.2 GB InnoDB
# flushes; coming back on a mounted datadir that was cleanly shut down should not need
# recovery, which is exactly why the producer refuses to image a running server.
#
# THE STOP BOUND IS GENEROUS BECAUSE EXCEEDING IT IS SILENT. It fails closed — the
# run imports as today — so the cost is a lost optimisation that shows up as nothing
# at all in the metrics. 30s was the first guess and review flagged it as thin for the
# top of the measured size range; there is no reason to be tight here, since the only
# thing a longer bound delays is giving up.
_SERVICE_STATE_STOP_TRIES = 120
_SERVICE_STATE_START_TRIES = 60

_SOURCE_MOUNT_ROOT = "/.world_src"

# `"adaptive"`: decide destination-mount vs download by asking the APP IMAGE what it
# ships, because only this process can see it.
#
# WHY THIS EXISTS. A destination mount is free — Modal swaps the directory and no bytes
# move — but it REPLACES what was there, and a download MERGES. `.apps_data` was
# source-mounted-and-copied to preserve the merge, and that copy turned out to be the
# expensive part: populate p50 went 56s -> 126s on a real campaign, against a fleet
# control that moved the other way, because a serial `tar | tar` over 9p loses to a
# parallel S3 download.
#
# WHAT MADE THE FREE PATH AVAILABLE. Measured across all six platform images one
# campaign actually runs: 108 per-app directories, 107 holding ZERO files (the
# exception is one 0 KB file). App images create the empty per-app directories and
# their `2770` setgid ownership and nothing else. So what a mount would destroy here
# is not content, it is DIRECTORIES — and unlike files those are reconstructible from
# a pre-mount `stat`. That is the "parity holds for FILES and fails for DIRECTORIES"
# finding with the file side finally measured.
#
# NOT ASSUMED, MEASURED PER SANDBOX. Six images are not 404 platforms, and
# `foundry_google_workspace` ships a ~10 GB prebuilt SQLite store on a platform none of
# those six included. So this asks the image in front of it rather than trusting the
# survey, and anything holding files — or any answer it cannot get — downloads.
_ADAPTIVE_SUBSYSTEMS = frozenset({".apps_data"})

# ONE PREFIX ON EVERY ADAPTIVE DECISION EXIT, so a single query answers "did the free
# path engage, and if not why". There are EIGHT ways to end up downloading, in the order
# they can happen:
#
#   BEFORE the mount, and free — a probe and a stat:
#     1. the image bakes content a mount would replace
#     2. the bake probe could not be answered
#     3-5. three shapes of unusable directory report (unparseable line, a name this will
#          not interpolate, non-numeric ids or mode) plus the absent root
#   AFTER the mount, each refusing work already paid for:
#     6. the digest does not match the manifest
#     7. the writability gate refuses the files
#     8. the directory restore fails
#
# With each phrased differently, the rollout question "is adaptive not engaging, or
# engaging and not helping?" had no greppable answer. Those are opposite conclusions with
# opposite next steps. This module emits no metrics (mount metrics live in the trajectory
# runner), so the log line is the interface. Raised in review.
#
# THE LAST THREE WERE THE ONES MISSING, and they are the ones that matter most, because
# they are the only exits that happen after mounting — 8 is the most expensive of all,
# having paid for the mount AND the restore attempt. 6 and 7 were invisible because they
# log from helpers `filesystem` shares, which cannot carry the prefix without changing the
# other half's output; `_restore_subdirs` is adaptive-only, so it carries it directly.
# Each of the three was raised in review against a version of this comment that claimed
# the prefix already covered every exit while the test drove only the free ones.
_ADAPTIVE_LOG = "adaptive .apps_data:"

# TWO ENVIRONMENT ASSUMPTIONS THAT FAIL CLOSED, BOTH LOAD BEARING AND EASY TO MISS.
#
# 1. GNU FIND. `-printf`, `-quit`, and treating `-mindepth`/`-maxdepth` as global options
#    are GNU extensions. On a BusyBox `find` those probes exit non-zero, so
#    `_image_bakes_files` and `_read_subdir_modes` both return `None` and the half
#    downloads — correct, but permanently and for the whole platform.
#    `_COORDINATOR_SYMLINK_PROBE` is DELIBERATELY EXEMPT: it is POSIX-only, so please do not
#    "simplify" it back to `-printf`. Two drafts did, and the second could not even be tested
#    locally — macOS `find` has no `-printf`, so the probe returned rc=1 (a refusal) on every
#    tree while reading as if it worked.
# 2. A PRE-CREATED `/.apps_data`. An image without it makes `find` error, so the root
#    line is missing and the mount is refused. Right for a second reason as well: with no
#    pre-mount metadata there is nothing to restore, and the root would land `root:root`
#    — the `mkdir: Permission denied` failure this path exists to avoid.
#
# Both fall back to downloading, which is the safe direction. The gap is that neither is
# distinguishable from "adaptive is enabled and working", which is why every exit now
# carries `_ADAPTIVE_LOG`. Raised in review to save the next person a bisect.

# Non-empty output means the image ships SOMETHING a mount would replace.
#
# `-mindepth 2`, so the per-app directories themselves (depth 1) do not count — they are
# what the restore puts back — but anything INSIDE one does.
#
# ANY ENTRY, NOT JUST `-type f`, and the first draft had `-type f`. An image shipping an
# empty nested tree (`/.apps_data/<app>/uploads/`) would pass a files-only check, get
# mounted, and lose those directories, because the restore only recreates depth 1. Raised
# in review. The one-off that measured these images counted ALL entries and found zero,
# so this also aligns the check with the evidence behind it rather than a weaker version.
#
# `-print -quit` RATHER THAN `| head -1`, and this is the correction that matters most.
# Piping into `head` makes the exit status `head`'s — essentially always 0 — so `find`'s
# own failure (an unreadable subtree, a truncated walk) was invisible and read as "bakes
# nothing", which mounts over data that exists nowhere else. `-quit` keeps it O(1) while
# leaving the status `find`'s. Stderr is NOT discarded, for the same reason: the earlier
# `2>/dev/null` threw away the only evidence of a partial walk. `_COPY_STAGED_COMMAND`
# already learned this lesson with `pipefail`; this repeated the mistake.
#
# TWO FINDS, because the restore can only reconstruct DEPTH-1 DIRECTORIES — so anything
# else the image ships is unrecoverable and has to force a download:
#
#   1. a depth-1 entry that is NOT a directory (a file or symlink sitting directly in
#      `/.apps_data`). `-mindepth 2` alone missed these, which was a regression I
#      introduced fixing the nested-directory gap: such a file is neither noticed nor
#      restored, so it vanished with every signal reporting a successful mount. Found
#      independently by two reviewers.
#   2. anything at all below depth 1, which covers the nested empty trees the earlier
#      `-type f` check missed.
#
# `-mindepth`/`-maxdepth` are GLOBAL options in GNU find, not per-expression, so these
# cannot be one traversal with an OR — hence two commands rather than a cleverer single
# one. `|| exit 1` on each so a real `find` error still reaches the caller instead of
# reading as "bakes nothing".
#
# `.coordinator` IS EXCLUDED, and without that exclusion this whole route is dead code.
# MEASURED IN PROD after the flag was turned on: 2,612 of 2,612 runs took the "image bakes
# content" exit and downloaded — the free path never engaged once. The single cause was
# `/.apps_data/.coordinator/config`. `start.sh` copies the Environment Coordinator's state
# tree into `/.apps_data` before the runner is up, so `-mindepth 2` always found something
# and the answer was always "download".
#
# The original survey ("108 per-app directories, 107 with zero files") counted APP
# directories and never looked at `.coordinator`, which is not an app. That is the whole
# gap between the design premise and production.
#
# It is excluded rather than tolerated because this route now PRESERVES it explicitly
# (`_COORDINATOR_SEED_*` below) instead of hoping a mount spares it. Excluding it without
# that preservation would silently wipe coordinator state, and `ConfigStore.read` treats a
# missing config as `enabled=False` — so the coordinator would turn ITSELF OFF and the run
# would look complete while producing no VCA output. The exclusion and the copy are one
# change; neither is safe alone.
#
# Measured after excluding it, on a platform carrying 7 entries at depth >= 2: every one
# was under `.coordinator`, and `excl_coord=0`. So this exclusion is the difference between
# never mounting and always mounting.
_BAKED_FILES_PROBE = (
    "find {path} -mindepth 1 -maxdepth 1 ! -type d -print -quit || exit 1; "
    "find {path} -mindepth 2 -not -path '{path}/{coordinator}/*' -print -quit || exit 1"
)

# The source-key family whose snapshot is a SUPERSET of what the app image bakes, and
# therefore the one family for which `_BAKED_FILES_PROBE` asks the wrong question.
#
# WHY THE GENERAL PROBE IS RIGHT EVERYWHERE ELSE AND WRONG HERE. It refuses whenever the
# image ships files under the root, because a destination mount REPLACES that root and a
# WORLD snapshot has no reason to contain what the image baked — those files would exist
# nowhere afterwards. Measured on prod sandboxes 2026-08-19: a Foundry-shaped platform
# bakes 9.4 GB under `/.apps_data` (`campaign_database/data.db`, `foundry_*/*.db`), so
# the route as designed can never mount there, and refusing is CORRECT.
#
# A CONTINUATION INVERTS THE PREMISE. Its parent snapshot was taken from a sandbox built
# from THAT SAME app image, after populate merged the world/task data over it and after
# the run mutated it. So every path the image bakes is already in the parent snapshot —
# in its post-run form, which is the version the branch actually wants. Replacing the
# root loses nothing, and it is strictly MORE correct than today's merge: a file the
# parent deleted stays deleted instead of being resurrected from the image.
#
# Keyed on `source_key` rather than a new wire field, so an older server that cannot
# name the family simply never reaches this branch.
_PARENT_SOURCE_FAMILY = "trajectories/"

# The globs the S3 snapshot omits — and, since #19476, that the parent image is stripped
# of too, precisely so a continuation that MOUNTS sees the same tree as one that
# DOWNLOADS.
#
# FOURTH COPY, AND DUPLICATED FOR TWO MECHANICAL REASONS RATHER THAN LAZINESS. The tuple
# lives in `modal_helpers.SNAPSHOT_EXCLUDE_GLOBS`, and this module cannot import it:
# `modal_helpers` imports `runner.world_mounts` (a cycle), and `agent_sandbox/_vendor`
# copies only `runner/`, so the vendored build could not resolve it either. Same class of
# duplication as the digest command and the write bits, and pinned the same way — by the
# port-in-step test, not by hope. If the two ever drift, the failure is a continuation
# that mounts a tree the downloader would not have produced.
_SNAPSHOT_EXCLUDE_GLOBS: tuple[str, ...] = (
    ".apps_data/*.fts.db",
    ".apps_data/*.fts.db-*",
    ".apps_data/*workspace_docvec_*.db",
    ".apps_data/*workspace_docvec_*.db-*",
    ".apps_data/*workspace_docvec_*.db.srcmeta",
    "*/.cache/*",
)


def _unsnapshotted_files_probe(path: str) -> str:
    """Shell command finding files under `path` that NO snapshot can contain.

    THE NARROW FORM OF THE BAKE PROBE, and the only hole left in the superset argument
    above. The parent snapshot holds everything the image bakes EXCEPT what
    `_SNAPSHOT_EXCLUDE_GLOBS` strips — the FTS/docvec sidecars and `.cache/`. Those are
    baked-but-never-snapshotted, so they are exactly the files a destination mount would
    delete and a download would leave in place. Finding one means the two paths are not
    equivalent for this tree, and the caller downloads.

    `-type f` and `-path`, matching `modal_labs._strip_excluded_command` verbatim, so
    this probe tests the SAME set that build strips. `-path` because `*` crosses `/` in
    both it and the uploader's `fnmatch`, which `-name` cannot express for `*/.cache/*`.
    The globs are relative to the subsystem root's parent, so the translation is exactly
    "prepend a slash"; a pattern for the other root simply never matches.
    """
    predicates = " -o ".join(f'-path "/{glob}"' for glob in _SNAPSHOT_EXCLUDE_GLOBS)
    return f'find "{path}" -type f \\( {predicates} \\) -print -quit || exit 1'


# The coordinator's state directory, relative to `/.apps_data`.
#
# THIRD COPY OF THIS NAME, and that is a real cost worth naming: it is also
# `DEFAULT_COORDINATOR_ROOT` in `environment/runner/coordinator/state/store.py` and
# `COORDINATOR_SNAPSHOT_PREFIX` in `server/packages/virtual_coworker_agents/
# archipelago_constants.py`. This package cannot import either — it reaches the server over
# HTTP — so the name is repeated here and pinned by the port-in-step test rather than left
# to drift. If the coordinator root ever moves, this route is where a stale copy shows up
# as "the free path stopped engaging" rather than as an error.
#
# THE DEFAULT, NOT THE EFFECTIVE VALUE: the store resolves its root from the `COORDINATOR_ROOT`
# environment variable and falls back to this. Nothing in the repo sets that variable today —
# it is read in exactly one place and written in none — so the two are the same value in every
# environment that exists. Raised in review, and worth writing down rather than guarding,
# because the divergence FAILS SAFE IN BOTH DIRECTIONS:
#
#   set elsewhere UNDER `/.apps_data` -> the probe stops excluding it, so its files are seen,
#                                        so the image looks baked and the run downloads
#   set OUTSIDE `/.apps_data`         -> no mount covers it, and there is nothing to preserve
#
# Both give up the free path; neither loses coordinator state. A guard here would have to read
# an environment variable this process does not own, to defend a configuration nothing produces.
_COORDINATOR_DIR = ".coordinator"

# Where the seeded coordinator tree is parked while the mount is in place. Under the
# staging root for the same reason source mounts are: outside both subsystem roots, so the
# end-of-run snapshot never uploads it.
_COORDINATOR_SEED_PATH = f"{_SOURCE_MOUNT_ROOT}/.coordinator_seed"

# Copy the seeded tree aside before the mount replaces it. `cp -a` because ownership and
# modes have to survive — the coordinator runs as `runner` and its own repair is not
# guaranteed to re-fix what a wrong owner breaks.
#
# CHEAP IN A WAY THE `.apps_data` COPY WAS NOT, which is what makes this route viable at
# all: this subtree is a config file plus small JSON records, not the multi-GB half whose
# `tar | tar` cost 70s and started this whole change. `mkdir -p` on the parent so a first
# run with no staging root works.
# `rm -rf {dst}` RUNS UNCONDITIONALLY, OUTSIDE the `if`, and that placement is the fix for a
# data-contamination bug raised in review: with the clear inside the guard, a run with nothing
# to park left an EARLIER attempt's seed sitting at this path, and the restore then poured
# that run's coordinator records into this world's tree — output graded against state that was
# never part of the world. Clearing first is unconditional; the copy stays conditional.
#
# `&&`, NOT `;`, BEFORE THE PARK. With a `;` a failed `rm -rf` was swallowed — the command
# still exited 0, the stale seed survived, the mount proceeded and the restore copied the
# earlier run's records in anyway. That is the contamination this clear exists to close,
# reintroduced by one character. Raised in review immediately after the clear was added.
#
# It also stops a second park nesting: `cp -a src dst` copies INTO `dst`
# when it already exists, which would build `.coordinator_seed/.coordinator` and then
# restore a nested directory plus the PREVIOUS run's records into the world's tree. One
# adaptive plan per sandbox today, so this is a guard rather than a fix — raised in
# review, and cheaper than relying on "it only happens once". The path is built from
# constants, never from anything the wire supplies.
_COORDINATOR_SEED_COMMAND = (
    "set -o pipefail; "
    "mkdir -p {dst_parent} && rm -rf {dst} && "
    "if [ -d {src} ]; then cp -a {src} {dst}; fi"
)

# Put the seeded tree back after the mount, WITHOUT clobbering what the world brought.
#
# `-n` (no-clobber) IS THE SEMANTIC, not a safety flourish. Under a download the world's
# files are extracted over the seeded tree, so for any path present in both the WORLD wins
# and seeded-only paths survive. `cp -Rn` reproduces exactly that: world files stay, seeded
# extras are restored. Without `-n` the seed would overwrite the world's own coordinator
# records — inverting the precedence a downloaded run gives, which is the parity this route
# is measured against.
# `-a` AND `-n` TOGETHER, and the `-a` half was a real bug caught in review: `cp -Rn` is
# `-R` without `-p`, so every restored file and directory came back owned by the copying
# process (root) with setgid stripped and modes filtered through the umask. MEASURED: a tree
# parked as `2770 runner:rg` came back `750 root:root`. The coordinator runs non-root, so it
# could then neither write its state directories nor read a `0600` file — and
# `ConfigStore.read` treats an unreadable config as `enabled=False`, which is the silent
# self-disable this whole preservation exists to prevent. Parking with `cp -a` and restoring
# without it defeated the point.
#
# `cp -an` keeps both properties, verified: `2770 runner:rg` survives, a world file is NOT
# clobbered, and a seeded-only file is still added.
_COORDINATOR_RESTORE_COMMAND = (
    "set -o pipefail; if [ -d {src} ]; then mkdir -p {dst} && cp -an {src}/. {dst}/; fi"
)

# Refuse if the MOUNTED tree carries a symlink anywhere under `.coordinator`.
#
# The tree being copied into is world-authored content, and `_restore_subdirs` already
# refuses a symlinked app directory for exactly this reason — a copy that descends a link
# writes outside `/.apps_data`, as root. `cp -an` happens to refuse the directory case on
# its own (verified: "cannot overwrite non-directory with directory"), but relying on a `cp`
# implementation detail is not this module's stance, and a probe also covers links deeper in
# the tree. Raised in review as inconsistent with the module's own threat model, which it was.
# GATED ON THE SEED TOO, because a link only matters if something is going to be copied
# through it. Raised in review: refusing on a link when NO seed was parked throws the fast
# path away for nothing — that platform pays for the mount, the digest and the directory
# restore and then downloads the whole half, permanently and on every run. The copy is a
# no-op without a seed, so there is nothing to protect in that case.
#
# GUARDED ON THE DIRECTORY EXISTING, because a bare `find` on a missing path exits
# NON-ZERO and this treats a non-zero probe as a refusal. Raised in review as a 🔴, and
# correctly: a platform that never seeded a coordinator would have paid for the mount,
# the digest and the directory restore, then downloaded the whole half anyway — SLOWER
# than before this PR, and precisely on the platforms that already mounted fine. The
# copy itself is already a no-op without a seed, so the absent case just proceeds.
# SEED-DRIVEN, NOT DESTINATION-DRIVEN, and that is the whole shape of this probe. `cp -an
# {src}/. {dst}/` descends only the directories the SEED has, so only their destination
# counterparts can be copied through — a link anywhere else is unreachable by this copy.
# Walking the destination instead refused on links the copy would never touch, and
# `agent_filesystems` is exactly where those live: an AGENT-plane subtree a VCA writes into
# during a run, whose `chown_tree` docstring anticipates "a VCA-planted symlink" by name. One
# such link captured into a world snapshot permanently cost that world the fast path on every
# later run, after paying for the mount, the digest and the directory restore — the same
# slower-than-before regression the absent-directory guard exists for, but per-world and
# silent. Raised in review; the fail direction was safe, the blast radius was not.
#
# It also bounds the cost. The old walk was O(world tree) over 9p with `-quit` helping only
# when a link was actually found; this is O(seed dirs), and the seed is the app image's own
# baked coordinator tree.
#
# POSIX ONLY — no `-printf`, no `read -d`, no `pipefail`. Two earlier drafts reached for GNU
# extensions and both were wrong here: this string runs under whatever `/bin/sh` and `find` the
# APP IMAGE ships, and even the local test could not exercise it (macOS `find` has no
# `-printf`, so the probe returned rc=1 — a refusal — on every tree). `cd {src}` is what makes
# the relative path available without `-printf`, and `${{d#./}}` strips the leading `./`.
#
# `-L {dst}` IS TESTED SEPARATELY because `find .` emits the seed root as `.`, which would
# test `{dst}/.` — and a trailing `/.` resolves a symlink-to-directory, so the root case
# would silently pass.
#
# Every branch ends on a zero-status command: the `sh -c` body closes on `done`, whose status
# is the final `if/fi` — 0 when the condition is false. A bare `[ -L "$t" ] && printf` instead
# would leave the last non-match as the exit status, `find` would report the `-exec` as failed,
# and a non-zero probe is read as a refusal — so every clean tree would refuse. Same shape as
# the `;`-swallows-a-failed-`rm` finding on the seed command.
_COORDINATOR_SYMLINK_PROBE = (
    "if [ ! -d {src} ] || [ ! -d {dst} ]; then exit 0; fi; "
    "if [ -L {dst} ]; then printf '%s\\n' {dst}; exit 0; fi; "
    "cd {src} || exit 1; "
    "find . -type d -exec sh -c "
    '\'for d; do t={dst}/"${{d#./}}"; if [ -L "$t" ]; then printf "%s\\n" "$t"; fi; done\' '
    "sh {{}} + || exit 1"
)


# `%p %U %G %m`: full path, numeric owner, numeric group, octal mode. Numeric so the
# restore needs no passwd lookup, and `%m` because it carries the setgid bit.
#
# `-mindepth 0`, WHICH INCLUDES THE ROOT ITSELF. The first draft used `-mindepth 1` and
# lost it: app images build `/.apps_data` as `runner:runner 0711` (CI asserts that owner
# and mode), the mount replaces the directory with the hydrated image's `root:root`, and
# a populate hook creating a NEW app directory then gets `mkdir: Permission denied` — the
# exact production failure this path exists to avoid. Raised in review and confirmed by
# probing a live image: `ROOT runner:runner 711`.
#
# `%p` not `%f` so the root is addressable — `%f` renders it as `.apps_data`, which is
# not a path the restore could use.
_SUBDIR_STAT = "find {path} -mindepth 0 -maxdepth 1 -type d -printf '%p %U %G %m\\n'"

# App directory names are interpolated into a shell command, so they get the same
# validation `mount_path` and `subsystem` already get. Anything else aborts the mount
# rather than being skipped quietly: a directory we cannot restore is one an app cannot
# write to, and downloading is always correct.
#
# THE CHARACTER CLASS DOES ALL THE SECURITY WORK; THE LENGTH BOUND IS `NAME_MAX`, and it
# used to be 64, which was arbitrary. Raised because the failure here is not one run: an
# app directory this refuses removes the optimisation for EVERY run on that platform,
# permanently, with a log line as the only evidence. Raised in review, and the check
# against the real derivation is what moved it:
#
# `normalize_service_name` (`models/db/mounts.py`) lowercases, replaces every run of
# non-`[a-z0-9]` with `_`, and strips the edges — so a real app directory name is always
# `[a-z0-9_]+`, a strict subset of this class. Unicode, whitespace and shell
# metacharacters cannot survive it (`"Zoho — CRM"` -> `zoho_crm`, and an all-unicode name
# normalizes to the EMPTY string, which creates no depth-1 directory at all). The class is
# therefore unreachable as a refusal for a name this repo produces.
#
# LENGTH WAS THE ONE REACHABLE CASE, and nothing upstream bounded it: the 32-char
# `LINUX_NAME_MAX_LEN` caps the service's linux user and group slug, NOT the directory,
# which is `normalize_service_name(instance_name)` un-truncated. A 66-character instance
# name is an ordinary thing for a human to type, and it would have silently and
# permanently downgraded its whole platform. 255 is `NAME_MAX`, so what this now refuses
# is only what cannot exist on disk — while the class still refuses everything dangerous.
#
# `.` AND `..` ARE EXCLUDED EXPLICITLY, because the class alone admits both and the claim
# above would then be false: `..` reaches `chown {uid}:{gid} "/.apps_data/.."` and
# `chmod {mode} "/.apps_data/.."`, which is the sandbox root, so a mode meant for one app
# directory would land on `/`. GNU `find` cannot report either — `-mindepth 1` never emits
# `.` or `..` — so this is unreachable today and closed anyway, for the reason the plain
# destination `.apps_data` refusal exists: this side obeys whatever the report says, and
# `_validated_mount_path` already rejects `..` for the same interpolation. Raised in
# review, and it is the right kind of finding: the comment claimed a property the regex
# did not have. A lookahead rather than a narrower class, so an ordinary dotted name
# (`.cache`, `a..b`) still passes.
_APP_DIR_RE = re.compile(r"\A(?!\.\.?\Z)[A-Za-z0-9._-]{1,255}\Z")

# The only subsystem names a source mount may name. `subsystem` arrives from the server
# over HTTP like every other field here, and the copy interpolates it into a command run
# under `sh -c` — so it gets the same treatment `mount_path` and `source_key` already
# get, and for the same stated reason. An empty value would target `/`, and one carrying
# shell metacharacters would run whatever it liked inside the sandbox.
_SOURCE_MOUNTABLE_SUBSYSTEMS = frozenset({".apps_data"})

# Copy the CONTENTS in without touching the destination directories themselves.
#
# NOT `cp -a`, and the difference is the whole strategy. GNU `cp -a` implies
# `--preserve=all`, which applies the SOURCE directory's mode and ownership to
# destination directories that already exist — including `{dst}` itself. Directories in
# the MODAL IMAGE arrive `root:root 0755` with no setgid, so `cp -a` would reset
# `/.apps_data` and every `/.apps_data/<app>` to root-owned 0755 and strip exactly the
# `2770` setgid bits this route exists to preserve, while still reporting success and
# dropping the download fallback. Caught in review before it ever ran.
#
# `--no-overwrite-dir` is tar's name for "leave the metadata of directories that already
# exist alone". `-p` keeps FILE modes, which matters in the other direction: hydration
# stamps files `0666` so the app can write them.
#
# `--no-same-owner` because tar extracting as root defaults to `--same-owner` and would
# stamp every file and new directory `root:root` from the archive. Without it the
# destination's setgid bit is overridden and files do NOT inherit the app group, which
# is the property the download path gets for free by creating files in place.
#
# `bash` with `pipefail`, not `sh`: without it the exit status is the SECOND tar's, so a
# failed read of the staged tree would report success — and success here drops the
# populate source. The module already runs its digest check under `bash`.
_COPY_STAGED_COMMAND = (
    "set -o pipefail; tar -C {src} -cf - . | "
    "tar -C {dst} -xpf - --no-overwrite-dir --no-same-owner"
)

# CEILING on the copy, not the whole story. The copy moves DATA, so the shared metadata
# bound does not apply to it: measured world halves reach 36.95 GB.
#
# IT IS ADDITIVE TO THE RUN'S POPULATE BUDGET, NOT ENCLOSED BY IT. An earlier version of
# this comment claimed `environment_populate_deadline_seconds` would pre-empt an
# over-large bound; that is false. `deadline_seconds` only bounds the status polling
# loop in `populate_environment`, whose clock starts AFTER `apply_world_mounts` returns,
# and no caller wraps that function in a timeout. So whatever is spent here is spent on
# top of the configured envelope.
#
# Hence `copy_deadline_s`: the caller passes its populate budget and the effective bound
# is the SMALLER of the two, so a campaign configured below this ceiling is actually
# protected rather than nominally so. This constant only caps the case where no budget
# is supplied.
_COPY_TIMEOUT_S = 1800

# The adaptive path's two O(1) probes — the bake check and the directory read. Both stop
# at the first hit or list one level, so this bounds a WEDGED sandbox rather than slow
# work, and it sits far below `_COPY_TIMEOUT_S` because neither moves data.
#
# THE RESTORE IS NOT O(1) AND NO LONGER SHARES THIS, which it did in the first draft.
# Adding the nested group repair made it walk the whole mounted `.apps_data` subtree per
# app over 9p, so a "handful of directories" stopped describing it — and `.apps_data` is
# the LARGER half on real worlds. Raised in review.
_PROBE_TIMEOUT_S = 60

# The restore, sized for a traversal rather than a lookup.
#
# MEASURED, not guessed: the two live Atlas worlds carry 11 and 143 directories under
# `.apps_data` (max depth 3 and 8), and `_repair_directories` covered 6,607 directories
# in 2.0s — so today's worlds finish in well under a second and this ceiling is slack, not
# a budget anyone spends. It exists for the world that is two orders of magnitude larger.
#
# MIN'D WITH THE CALLER'S POPULATE DEADLINE, exactly as `_copy_staged_tree` is, so a
# campaign configured below this ceiling is actually protected rather than nominally so.
# Getting it wrong is safe but wasteful in the precise way this PR exists to remove: the
# fallback is unmount-and-download, i.e. a mount paid for and then thrown away.
#
# NOT ADDITIVE WITH THE COPY CEILING, and this route LOWERS the worst case rather than
# raising it. Raised in review as 1800s + 600s stacked on one run; the two cannot both be
# paid, because `_SOURCE_MOUNTABLE_SUBSYSTEMS` and `_ADAPTIVE_SUBSYSTEMS` are the same
# single subsystem — `.apps_data` — and it gets one plan carrying one strategy.
# `filesystem` is destination-only on both routes, so nothing else can reach either
# ceiling. Per run, the pre-populate worst case for that half is:
#
#   source    1800  (the `tar | tar` copy)
#   adaptive    60  (bake probe) + 60 (directory stat) + 600 (restore)  = 720
#
# What IS true, and is the pre-existing property `_COPY_TIMEOUT_S` already documents: this
# budget is drawn from the caller's WHOLE populate envelope rather than what remains of
# it, because the populate polling clock does not start until `apply_world_mounts` returns
# (`modal_helpers.populate_environment`). So mount work is additive to the envelope on
# either route — 720s here against 1800s before it.
_RESTORE_TIMEOUT_S = 600

# Mirrors `snapshot_images.service`: an unmount is retried because a single transient
# gRPC error must not escalate to "dirty", which would deny the caller the download
# fallback entirely.
_UNMOUNT_ATTEMPTS = 3
_UNMOUNT_RETRY_DELAY_S = 0.5

# Matches the server's `validate_mount_path` exactly: `\A`/`\Z` rather than `^`/`$`
# (Python's `$` also matches before a trailing newline, so `"/filesystem\n"` would
# pass here and fail there), and `{1,255}` rather than `*` (which accepts a bare `/`,
# and `find "/" -type f` walks the entire filesystem). Both gaps were unreachable —
# the path is `SUBSYSTEM_MOUNT_PATHS[subsystem]` server-side — but this validator
# justifies itself on the value being caller data over HTTP, and under that threat
# model it has to be at least as strict as the server's. Raised in review.
_MOUNT_PATH_RE = re.compile(r"\A/[A-Za-z0-9._/-]{1,255}\Z")

# The S3 key prefix a mounted half replaces, e.g. `worlds/snap_ab12/filesystem/`.
#
# VALIDATED BECAUSE THE MATCH IS `endswith`, and `"anything".endswith("")` is True —
# so a blank key would drop EVERY source, world and task alike, and the run would
# populate nothing while looking like a clean full mount. Anchored to a KNOWN FAMILY
# and a trailing slash so it can neither be blank nor over-match. Caught in review.
#
# THE FAMILIES THE SERVER'S MOUNT RESOLVERS CAN PRODUCE, and not
# `SNAPSHOT_FAMILIES` either. `worlds/` alone was the whole gate until task-only
# trajectories learned to mount, at which point a correct `tasks/<id>/<subsystem>/`
# key was refused here and every task half fell back to download — the feature
# reachable end to end on the server and inert at this line. `trajectories/` was
# added for exactly the same reason a release later: a CONTINUATION populates from
# `trajectories/<parent-snap>/`, so once `resolve_continuation_mounts` existed
# server-side this line was the whole difference between a golden-gen branch
# mounting its parent and downloading it.
#
# **THIS LIST IS THE ENTIRE GATE, AND IT IS EASY TO FORGET.** The server can only
# reach `apply_world_mounts` through a resolver, and every resolver's family must
# appear here or the mount is refused with a WARNING that reads like a corrupt key
# rather than a missing feature. Widened one family at a time, deliberately, so a
# prefix nothing is meant to mount from — `playgrounds/`, `golden-responses/` —
# still declines rather than mounting on the strength of a name.
#
# `service_state` is the THIRD widening, and the first whose key names no S3 prefix at
# all. `service_state/<snap>/campaign_database_mysql/` is what
# `snapshot_prefix("service_state/campaign_database_mysql", snap)` returns for a
# captured MySQL datadir, so it is the string that arrives in `WorldMount.source_key`
# and it must pass here or the datadir mount is refused at this line exactly as the
# task and trajectory families were.
#
# ITS `endswith` DROP IS EXPECTED TO MISS, and that is the one thing to understand
# before reading a log. For every family above, the key naming a source that does not
# exist is the SILENT-LOSS BUG the comment above describes: the mount lands, the drop
# misses, and populate downloads over a read-write mount of itself. A datadir has no
# populate source to drop — nothing downloads `/var/lib/mysql`, it is built in-container
# by a seed import — so there is nothing for the drop to hit and nothing lost by it
# missing. The saving comes from the connector's own `_seed_already_baked` guard
# skipping that import once the datadir it finds is already loaded, not from dropping a
# transfer. So a `service_state` mount that reports success while the source list is
# unchanged is CORRECT, where the identical shape on `worlds/` would be the outage.
_SOURCE_KEY_RE = re.compile(
    r"\A(?:worlds|tasks|trajectories|service_state)"
    r"/[A-Za-z0-9._-]{1,128}/[A-Za-z0-9._-]{1,64}/\Z"
)


def _valid_source_key(source_key: str) -> bool:
    """Whether `source_key` is safe to match sources against with `endswith`."""
    return bool(_SOURCE_KEY_RE.match(source_key))


def _validated_mount_path(mount_path: str) -> str:
    """A mount path safe to interpolate into a shell command.

    The path arrives from the server over HTTP, so it is caller data rather than a
    local constant. Quoting alone would survive a space but not a `"` or `$( )`, and
    this string is run under `bash -lc`.
    """
    if not _MOUNT_PATH_RE.match(mount_path) or ".." in mount_path:
        raise ValueError(f"unsafe mount path {mount_path!r}")
    return mount_path


async def _run(sandbox: SupportsMounting, *argv: str) -> tuple[int, str, str]:
    """Run argv in the sandbox and return (rc, stdout, stderr).

    argv rather than a shell string wherever the caller controls the words, so no
    quoting question arises for a path or a gid.

    """
    proc = await sandbox.exec.aio(*argv)
    out = await proc.stdout.read.aio()
    err = await proc.stderr.read.aio()
    return await proc.wait.aio(), out, err


async def _read_shared_gid(sandbox: SupportsMounting, mount_path: str) -> int | None:
    """The group owning `mount_path` BEFORE anything is mounted over it.

    Order is the whole point. Modal REPLACES a directory that already has content, so
    after mounting this returns the hydrated image's root group (0) and the repair
    would align the tree to a group nothing runs as — while appearing to succeed.
    """
    try:
        rc, out, err = await _run(sandbox, "stat", "-c", "%g", mount_path)
    except Exception as exc:  # noqa: BLE001 — unknown means do not mount
        logger.warning(f"could not stat {mount_path}: {exc!r}")
        return None
    if rc != 0:
        logger.warning(
            f"stat {mount_path} failed rc={rc} stderr={err.strip()[-300:]!r}"
        )
        return None
    try:
        return int(out.strip())
    except ValueError:
        logger.warning(f"non-numeric gid {out.strip()!r} for {mount_path}")
        return None


async def _verify(
    sandbox: SupportsMounting, mount_path: str, manifest_sha: str
) -> bool:
    """Whether the mounted tree matches the digest recorded when it was built.

    NOT OPTIONAL. A mount that "succeeded" but exposes an empty or wrong tree is the
    dangerous case — a consumer that read presence as health would run against it —
    so this recomputes from the mount rather than trusting the pointer that named it.
    Metadata only, so it faults nothing into memory.
    """
    try:
        rc, out, err = await _run(
            sandbox,
            "bash",
            "-lc",
            _MANIFEST_SHA_COMMAND.format(mount=_validated_mount_path(mount_path)),
        )
    except Exception as exc:  # noqa: BLE001 — unverifiable is unusable, not a raise
        logger.warning(f"could not verify the mount at {mount_path}: {exc!r}")
        return False
    if rc != 0:
        logger.warning(
            f"manifest digest failed at {mount_path} "
            f"rc={rc} stderr={err.strip()[-300:]!r}"
        )
        return False
    actual = out.strip()
    if actual != manifest_sha:
        logger.warning(
            f"mounted tree at {mount_path} digests {actual!r}, expected "
            f"{manifest_sha!r}; refusing it and downloading instead"
        )
        return False
    return True


async def _files_are_writable(sandbox: SupportsMounting, mount_path: str) -> bool:
    """Whether the image's FILES carry the write bits an app needs.

    The digest above cannot see this — it covers size and path only — so an image
    built before hydration stamped modes serves `root:root 0644` and verifies
    perfectly. Refused rather than repaired: changing a mounted file's mode copies the
    whole file, which is the cost mounting exists to avoid.
    """
    try:
        rc, out, err = await _run(
            sandbox, "find", mount_path, "-type", "f", "-printf", "%m\\n", "-quit"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"could not sample a file mode under {mount_path}: {exc!r}")
        return False
    text = out.strip()
    if rc != 0 or not text:
        logger.warning(
            f"no file to sample under {mount_path} (rc={rc}, "
            f"stderr={err.strip()[-300:]!r}); treating the image as unusable"
        )
        return False
    try:
        mode = int(text, 8)
    except ValueError:
        logger.warning(f"unreadable file mode {text!r} under {mount_path}")
        return False
    if mode & _REQUIRED_FILE_WRITE_BITS != _REQUIRED_FILE_WRITE_BITS:
        logger.warning(
            f"mounted files under {mount_path} are {mode:04o}, missing "
            f"{_REQUIRED_FILE_WRITE_BITS:04o}; the image predates the mode stamp, so "
            f"downloading rather than serving a read-only world"
        )
        return False
    return True


async def _repair_directories(
    sandbox: SupportsMounting, mount_path: str, shared_gid: int
) -> bool:
    """Give every DIRECTORY under `mount_path` to `shared_gid`, setgid included.

    Directories arrive `root:root 0755` with no setgid, so an app that creates a file
    — a SQLite `-wal` beside its database — fails without this. `-type d` is
    load-bearing: directories hold no contents, so nothing is copied up, while the
    same pass over files would materialize the tree.
    """
    for operands in ((("chgrp", str(shared_gid))), ("chmod", "g+rwxs")):
        argv = ("find", mount_path, "-type", "d", "-exec", *operands, "{}", "+")
        try:
            rc, _out, err = await _run(sandbox, *argv)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"directory repair {operands} failed at {mount_path}: {exc!r}"
            )
            return False
        if rc != 0:
            logger.warning(
                f"directory repair {operands} at {mount_path} "
                f"rc={rc} stderr={err.strip()[-300:]!r}"
            )
            return False
    return True


class WorldMountDirtyError(RuntimeError):
    """A refused image is still mounted and the path cannot be written to.

    Mirrors the server's `"dirty"` outcome, and exists for the same reason: a mount
    is READ-WRITE, so a populate that writes into a path still holding a REFUSED
    image succeeds and produces a tree that is a blend of two worlds, looks complete,
    and cannot be detected downstream. A trajectory graded against that blend is
    wrong in a way nothing flags.

    Raised rather than swallowed because the caller's very next act is to download
    into that path. Failing the run costs one trajectory; continuing costs a silently
    wrong grade.
    """


async def _unmount(sandbox: SupportsMounting, mount_path: str) -> bool:
    """Put the path back, so the caller can download into it. True if it is clear.

    RETRIED, matching the server's `clear_mount`. The two outcomes are wildly
    asymmetric — a cleared path means the caller downloads and proceeds, a stuck one
    means a wrong tree sits where the caller is about to write — and the usual reason
    a single attempt fails is a transient gRPC error. Retrying converts some of the
    unrecoverable case into the safe one. A first version had one un-retried attempt,
    so one blip reached the blend; caught in review.
    """
    last: BaseException | None = None
    for attempt in range(1, _UNMOUNT_ATTEMPTS + 1):
        try:
            await sandbox.unmount_image.aio(mount_path)
            if attempt > 1:
                logger.info(f"unmounted {mount_path} on attempt {attempt}")
            return True
        except Exception as exc:  # noqa: BLE001 — the caller needs a verdict
            last = exc
            if attempt < _UNMOUNT_ATTEMPTS:
                await asyncio.sleep(_UNMOUNT_RETRY_DELAY_S)
    logger.error(
        f"could not unmount {mount_path} after {_UNMOUNT_ATTEMPTS} attempts: {last!r}"
    )
    return False


async def _copy_staged_tree(
    sandbox: SupportsMounting,
    staged_path: str,
    subsystem_root: str,
    *,
    timeout_s: float,
) -> bool:
    """Copy a staged half INTO its subsystem root, leaving that root's own dirs alone.

    THE COPY IS WHAT MAKES A SOURCE-MOUNT EQUIVALENT TO A DOWNLOAD. `populate_data`
    writes objects into the directory the APP IMAGE built; so does this. Every per-app
    directory keeps its owner, its group and its setgid bit, so a file landing in
    `/.apps_data/<app>` inherits that app's group from the setgid parent — which is why
    `--no-same-owner` is not optional — exactly as a downloaded one does, and
    which is why the populate hooks can then `rm` their consumed seed files, the
    operation that fails outright under a destination mount.

    That property comes from `--no-overwrite-dir`, not from copying contents alone: a
    plain `cp -a` rewrites existing destination directories from the source's metadata
    and would strip the very bits this preserves. See `_COPY_STAGED_COMMAND`.

    THE EQUIVALENCE IS EXACT FOR DIRECTORIES THE APP IMAGE ALREADY BUILT, and only
    approximate for the ENTRIES WRITTEN INTO THEM. This copy of the note is deliberately
    self-contained: it is the one that runs in the agent runner, and it is where the next
    reader will meet this `tar` command and want to "fix" it.

    DO NOT ADD A SETGID REPAIR PASS over the destination directories — the
    `chmod g+rwxs` traversal that has now been proposed by three separate reviewers.
    It is wrong twice over.

    First, there is no regression to fix. The download path's setgid repair,
    `_make_shared_filesystem_path_writable` in
    `archipelago/environment/runner/data/populate/utils.py`, is invoked from exactly two
    places and BOTH are gated `if subsystem_root == FILESYSTEM_ROOT`. Nothing repairs
    `.apps_data` on the download path either; its new directories come from a bare
    `os.makedirs`. Any app depending on setgid for a new `.apps_data` directory is
    already broken today, independent of mounts.

    Second, it would assign the WRONG GROUP. That helper takes its group from
    `os.stat(root).st_gid` — the subsystem root's own group. Coherent for `/filesystem`,
    which is a single shared `workspace` group, which is why it is scoped there.
    `/.apps_data` is root-grouped and each `/.apps_data/<app>` carries its OWN per-app
    group, so setgid inherited from the root would put new files in group `root` instead
    of the app's. A directory the app image never created carries nothing saying which
    app group it belongs to, so there is no correct generic repair to make here.

    WHERE THE ROUTES ACTUALLY DIVERGE, measured under GNU tar 1.34 rather than reasoned
    about: EVERY entry tar writes lands `root:root`, because `--no-same-owner` means
    "chown to the extracting process's ids" and tar applies it AFTER creation, overriding
    the group the setgid parent gave it. A shell-created control in the same directory
    came out `root:appgrp`; every tar-written entry came out `root:root`. A world-added
    subdirectory additionally comes out `0755` with no setgid.

    This is harmless for one specific reason, and it is not that the group matches — it
    does not. `rm` is governed by write permission on the CONTAINING directory, which
    keeps its `2770` via `--no-overwrite-dir`, and seeds arrive `HYDRATED_FILE_MODE`
    `0o666`. Verified: a member of `appgrp` could `rm` a copied `root:root 0666` seed.
    That makes the `0o666` guard load-bearing for the populate hooks, not merely
    convenient.
    """
    # THE INVARIANT: `False` means NOTHING IS WRITING, so the caller may safely download
    # into the destination. Every other outcome raises. An earlier version asked whether
    # the local deadline had expired, which answers a different question — `exec`
    # launches the command and the reads and the wait come after it, so ANY failure past
    # the launch leaves a `tar` extracting with nothing to kill it.
    #
    # The cost of getting that wrong is not merely a blend. `populate_environment`
    # downloads the world layer and THEN the task layer into the same tree, and the task
    # layer must win; a straggling `tar` writing world files after the task overlay
    # lands reverses that silently, and the run is graded against the wrong environment.
    #
    # Inlined rather than routed through `_run` so the launch is observable: `_run`
    # returns only after the reads and the wait, by which point "did it start" can no
    # longer be answered.
    launched = False
    deadline = asyncio.timeout(timeout_s)
    try:
        async with deadline:
            proc = await sandbox.exec.aio(
                "bash",
                "-c",
                _COPY_STAGED_COMMAND.format(src=staged_path, dst=subsystem_root),
            )
            # From HERE a process is running in the sandbox and nothing can stop it.
            launched = True
            # BOTH STREAMS CONCURRENTLY. Reading stdout to EOF first deadlocks a
            # chatty copy: tar warning per file across a multi-million-file
            # `.apps_data` fills the stderr pipe buffer, tar blocks writing to it,
            # stdout never reaches EOF, and the only exit is the copy deadline — which
            # is a hard failure on this path, not a fallback. The tar pipeline writes
            # nothing to stdout, so this costs nothing in the ordinary case.
            _out, err = await asyncio.gather(
                proc.stdout.read.aio(), proc.stderr.read.aio()
            )
            rc = await proc.wait.aio()
    except Exception as exc:  # noqa: BLE001 — classified here, never swallowed
        if not launched:
            # `exec` itself failed, so no process exists. The only failure that can be
            # asserted safe, and so the only one that degrades to downloading.
            logger.warning(
                f"could not start the copy of {staged_path} into {subsystem_root}: "
                f"{exc!r}; downloading instead"
            )
            return False
        raise WorldMountDirtyError(
            f"the copy of {staged_path} into {subsystem_root} did not complete "
            f"({exc!r}) and may still be running; refusing to populate over it"
        ) from exc

    if rc != 0:
        # A failure past dispatch that IS safe: the command ran to completion and
        # reported failure, so nothing is still writing.
        #
        # STRICT ON PURPOSE, and it costs a full download on a benign tar warning.
        # Accepting `rc == 1` as "warnings" was tried and REVERTED: with `pipefail`
        # bash reports the RIGHTMOST non-zero status, so a fatal create-side exit (2)
        # paired with a benign extract-side warning (1) arrives as 1 — read as copied,
        # source dropped, `/.apps_data` left incomplete with no fallback. Telling the
        # two apart needs both statuses, and a `PIPESTATUS` wrapper did NOT work when
        # tested (the array is clobbered before it can be read). Until that is solved,
        # over-downloading is the only safe reading.
        logger.warning(
            f"copying {staged_path} into {subsystem_root} failed rc={rc} "
            f"stderr={err.strip()[-300:]!r}"
        )
        return False
    return True


async def _image_bakes_files(sandbox: SupportsMounting, path: str) -> bool | None:
    """Does the APP IMAGE ship files under `path`? `None` when it could not be asked.

    MUST RUN BEFORE THE MOUNT. Afterwards this reports the WORLD's files and answers a
    different question while looking correct — the same ordering hazard
    `_read_shared_gid` documents, and the reason both live next to each other.

    `None` and `True` are handled identically by the caller (download); they are kept
    distinct only so the log can say which happened.
    """
    try:
        rc, out, err = await asyncio.wait_for(
            _run(
                sandbox,
                "sh",
                "-c",
                _BAKED_FILES_PROBE.format(path=path, coordinator=_COORDINATOR_DIR),
            ),
            timeout=_PROBE_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — downloading is always correct
        logger.warning(
            f"{_ADAPTIVE_LOG} {path} could not check whether the image bakes "
            f"content, downloading: {exc!r}"
        )
        return None
    if rc != 0:
        logger.warning(
            f"{_ADAPTIVE_LOG} {path} bake check failed rc={rc}, downloading; "
            f"stderr={err.strip()[-300:]!r}"
        )
        return None
    return bool(out.strip())


async def _image_bakes_unsnapshotted_files(
    sandbox: SupportsMounting, path: str
) -> bool | None:
    """Does the APP IMAGE ship files under `path` that no snapshot carries?

    The PARENT-family counterpart of `_image_bakes_files`, and it exists because that
    one's question — "does the image bake anything here" — is answered `True` on every
    Foundry-shaped platform by the very databases a continuation wants to mount. See
    `_PARENT_SOURCE_FAMILY` for why replacing the root is safe for this family and not
    for a world.

    SAME CONTRACT AS ITS SIBLING, deliberately, so the caller treats them alike:
    `True` and `None` both mean download, kept distinct only so the log says which.
    Runs BEFORE the mount for the same reason — afterwards it would be reporting on
    the mounted tree and answering a different question while looking correct.
    """
    try:
        rc, out, err = await asyncio.wait_for(
            _run(sandbox, "sh", "-c", _unsnapshotted_files_probe(path)),
            timeout=_PROBE_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — downloading is always correct
        logger.warning(
            f"{_ADAPTIVE_LOG} {path} could not check for un-snapshotted baked "
            f"content, downloading: {exc!r}"
        )
        return None
    if rc != 0:
        logger.warning(
            f"{_ADAPTIVE_LOG} {path} un-snapshotted bake check failed rc={rc}, "
            f"downloading; stderr={err.strip()[-300:]!r}"
        )
        return None
    return bool(out.strip())


async def _read_subdir_modes(
    sandbox: SupportsMounting, path: str
) -> list[tuple[str, int, int, str]] | None:
    """The per-app directories under `path`, BEFORE anything is mounted over it.

    Returns `(name, uid, gid, octal_mode)` per directory, or `None` if the tree could
    not be read or holds a name this refuses to interpolate into a shell command.

    ONE `find` RATHER THAN ONE `stat` PER APP: a platform carries up to 33 apps and the
    sandbox round trip dominates, so per-app execs would cost more than the mount saves.
    """
    try:
        rc, out, err = await asyncio.wait_for(
            _run(sandbox, "sh", "-c", _SUBDIR_STAT.format(path=path)),
            timeout=_PROBE_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — downloading is always correct
        logger.warning(
            f"{_ADAPTIVE_LOG} {path} could not read the app directories, "
            f"downloading: {exc!r}"
        )
        return None
    if rc != 0:
        logger.warning(
            f"{_ADAPTIVE_LOG} {path} reading the app directories failed rc={rc}, "
            f"downloading; stderr={err.strip()[-300:]!r}"
        )
        return None
    records: list[tuple[str, int, int, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 4:
            # ABORT, not skip — and this is the case a `continue` here got wrong. A
            # directory name containing whitespace PRODUCES a wrong field count, so
            # skipping the line silently drops the one app whose name is unusual, and
            # that app then cannot write to its own directory. The failure has to be
            # the whole mount, not one app.
            logger.warning(
                f"{_ADAPTIVE_LOG} {path} unparseable app directory line {line!r}, downloading"
            )
            return None
        dir_path, uid, gid, mode = parts
        # The root arrives as `path` itself; everything else must be one child of it.
        # Validated as a whole path rather than a bare name because `%p` renders it that
        # way, and because this is what gets interpolated into the restore command.
        if dir_path != path and (
            not dir_path.startswith(f"{path}/")
            or not _APP_DIR_RE.match(dir_path[len(path) + 1 :])
        ):
            # ABORT, not skip. A directory that cannot be restored is one its app
            # cannot write to, and a half-restored `.apps_data` is the silent-breakage
            # shape this whole path exists to avoid.
            logger.warning(
                f"{_ADAPTIVE_LOG} {path} unusable app directory {line!r}, downloading"
            )
            return None
        if not mode.isdigit():
            logger.warning(
                f"{_ADAPTIVE_LOG} {path} non-numeric mode in {line!r}, downloading"
            )
            return None
        try:
            records.append((dir_path, int(uid), int(gid), mode))
        except ValueError:
            logger.warning(
                f"{_ADAPTIVE_LOG} {path} non-numeric ids in {line!r}, downloading"
            )
            return None
    if not any(d == path for d, _u, _g, _m in records):
        # The ROOT's own metadata is the thing whose loss reproduces the outage, so its
        # absence is a refusal rather than something to proceed without.
        logger.warning(
            f"{_ADAPTIVE_LOG} {path} its own directory was not reported "
            f"(absent root, or a `find` without GNU extensions), downloading"
        )
        return None
    return records


async def _seed_coordinator_aside(
    sandbox: SupportsMounting, path: str, *, deadline_s: float | None = None
) -> bool:
    """Park the seeded `.coordinator` tree outside `path` before the mount replaces it.

    `False` means DOWNLOAD, and the strictness is deliberate: `ConfigStore.read` treats a
    missing config as `enabled=False`, so a coordinator whose state we failed to preserve
    does not fail — it silently turns itself off and the run completes having produced no
    VCA output. That is worse than a slow populate, so an unpreservable tree is a refusal.
    """
    try:
        rc, _out, err = await asyncio.wait_for(
            _run(
                sandbox,
                "bash",
                "-lc",
                _COORDINATOR_SEED_COMMAND.format(
                    src=f"{path}/{_COORDINATOR_DIR}",
                    dst=_COORDINATOR_SEED_PATH,
                    dst_parent=_SOURCE_MOUNT_ROOT,
                ),
            ),
            # A COPY, NOT A PROBE, so it gets the copy ceiling min'd with the run's
            # budget rather than the 60s meant for one-shot checks. Raised in review:
            # under the probe timeout a larger-than-expected coordinator tree would
            # silently stop the fast path AND add its time on top of the populate budget.
            timeout=(
                _RESTORE_TIMEOUT_S
                if deadline_s is None
                else min(_RESTORE_TIMEOUT_S, deadline_s)
            ),
        )
    except Exception as exc:  # noqa: BLE001 — downloading is always correct
        logger.warning(
            f"{_ADAPTIVE_LOG} {path} could not park {_COORDINATOR_DIR} before the "
            f"mount, downloading: {exc!r}"
        )
        return False
    if rc != 0:
        logger.warning(
            f"{_ADAPTIVE_LOG} {path} parking {_COORDINATOR_DIR} failed rc={rc}, "
            f"downloading; stderr={err.strip()[-300:]!r}"
        )
        return False
    return True


async def _restore_coordinator(
    sandbox: SupportsMounting, path: str, *, deadline_s: float | None = None
) -> bool:
    """Put the seeded `.coordinator` tree back over the mount, world files winning.

    Runs AFTER `_restore_subdirs`, which recreates the `.coordinator` directory itself
    from its pre-mount record like any other depth-1 directory — so this only has to
    replace the CONTENTS the mount hid.

    RAISES `WorldMountDirtyError` IF THE COPY LAUNCHED AND THEN STOPPED REPORTING, and returns
    `False` only when nothing is writing. This one copies FILE data into the very path the
    caller downloads into on refusal, so `_restore_subdirs`' "it only touches directory
    metadata and converges anyway" argument does not carry over — an earlier version borrowed
    it and returned `False` on a timeout, which left a `cp` running in the sandbox that could
    land on top of what the download extracts. `-n` narrows that and does not close it: `cp`
    tests for existence and then writes, so a file created in between is still overwritten.
    The visible harm is a run silently grading against the image's DEFAULT coordinator config
    instead of the one the task and world supplied. Raised in review twice.
    """
    # BEFORE copying into it, because the tree at this path is the world's now. A link here
    # would send a root-run copy outside `/.apps_data`; `_restore_subdirs` refuses the same
    # shape at depth 1 and this covers the rest.
    #
    # ON THE COPY BUDGET, NOT THE PROBE CEILING. This used to walk the whole mounted tree over
    # 9p — `-quit` bounded it only when a link was actually found, and finding none is the
    # normal case — so a bigger world timed out and threw the fast path away after the mount
    # was already paid for, the same trap the park had. Narrowing it to the SEED's shape
    # bounds it structurally now (one `[ -L ]` per seed directory, and the seed is the app
    # image's own baked tree), but the budget stays: the stats still cross 9p.
    try:
        rc, out, err = await asyncio.wait_for(
            _run(
                sandbox,
                "sh",
                "-c",
                _COORDINATOR_SYMLINK_PROBE.format(
                    src=_COORDINATOR_SEED_PATH, dst=f"{path}/{_COORDINATOR_DIR}"
                ),
            ),
            timeout=(
                _RESTORE_TIMEOUT_S
                if deadline_s is None
                else min(_RESTORE_TIMEOUT_S, deadline_s)
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"{_ADAPTIVE_LOG} {path} could not check {_COORDINATOR_DIR} for symlinks: "
            f"{exc!r}"
        )
        return False
    if rc != 0:
        logger.warning(
            f"{_ADAPTIVE_LOG} {path} the {_COORDINATOR_DIR} symlink check failed "
            f"rc={rc}; stderr={err.strip()[-300:]!r}"
        )
        return False
    if out.strip():
        logger.warning(
            f"{_ADAPTIVE_LOG} {path} the mounted {_COORDINATOR_DIR} holds a symlink "
            f"({out.strip()[:120]!r}); refusing to copy the seed through it"
        )
        return False

    # SAME CLASSIFICATION AS `_copy_staged_tree`, because this writes FILE DATA into the very
    # path the caller downloads into if we refuse. That function's invariant is the contract
    # here too: `False` means NOTHING IS WRITING, and every other outcome raises. Returning
    # `False` on a timeout was wrong — the copy is still running in the sandbox with nothing
    # able to stop it, and a straggling write can land on top of what the download extracts.
    # Raised in review twice: first as an asymmetry worth documenting, then as the concrete
    # harm — the run ending up on the image's default coordinator config instead of the one
    # the task and world supplied, silently.
    #
    # `-n` narrows it but does not close it: `cp` checks existence and then writes, so a file
    # the download creates in between is still written by the straggler.
    #
    # THE PARK IS DIFFERENT AND KEEPS RETURNING FALSE: it writes to the staging path, which is
    # not a download target, so a straggler there costs nothing.
    #
    # Inlined rather than routed through `_run` so the launch is observable — `_run` returns
    # only after the reads and the wait, by which point "did it start" can no longer be asked.
    launched = False
    try:
        async with asyncio.timeout(
            _RESTORE_TIMEOUT_S
            if deadline_s is None
            else min(_RESTORE_TIMEOUT_S, deadline_s)
        ):
            proc = await sandbox.exec.aio(
                "bash",
                "-lc",
                _COORDINATOR_RESTORE_COMMAND.format(
                    src=_COORDINATOR_SEED_PATH, dst=f"{path}/{_COORDINATOR_DIR}"
                ),
            )
            # From HERE a process is running in the sandbox and nothing can stop it.
            launched = True
            _out, err = await asyncio.gather(
                proc.stdout.read.aio(), proc.stderr.read.aio()
            )
            rc = await proc.wait.aio()
    except Exception as exc:  # noqa: BLE001 — classified here, never swallowed
        if not launched:
            logger.warning(
                f"{_ADAPTIVE_LOG} {path} could not start the {_COORDINATOR_DIR} restore: "
                f"{exc!r}, downloading"
            )
            return False
        raise WorldMountDirtyError(
            f"the {_COORDINATOR_DIR} restore under {path} did not complete ({exc!r}) and "
            f"may still be writing; refusing to populate over it"
        ) from exc

    if rc != 0:
        # Ran to completion and reported failure, so nothing is still writing.
        logger.warning(
            f"{_ADAPTIVE_LOG} {path} restoring {_COORDINATOR_DIR} failed rc={rc}; "
            f"stderr={err.strip()[-300:]!r}"
        )
        return False
    return True


async def _restore_subdirs(
    sandbox: SupportsMounting,
    path: str,
    records: list[tuple[str, int, int, str]],
    *,
    deadline_s: float | None = None,
) -> bool:
    """Put the app directories back the way the app image had them.

    TWO JOBS IN ONE PASS, and the mount is why both are needed. It REPLACES the
    directory, so an app the world snapshot does not carry loses its directory
    entirely (`mkdir`), and one the snapshot DOES carry arrives owned `root:root`
    because hydration flattens ownership (`chown`/`chmod`).

    DIRECTORIES ONLY, which is what makes this affordable: a directory holds no
    contents, so changing its mode or owner copies nothing. The first metadata write to
    a mounted FILE copies that file's whole contents into the copy-on-write layer —
    12.45s for one 9.9 GB file on a real world, versus 2.0s across 6,607 directories.
    """
    if not records:
        return True
    # One command for every directory, for the same round-trip reason as the read. Paths
    # are `_APP_DIR_RE`-validated above and uid/gid/mode are ints and digits, so nothing
    # interpolated here can carry shell metacharacters.
    #
    # `|| exit 1` PER COMMAND, NOT `set -e` WITH `&&` CHAINS, and the difference is not
    # stylistic. Under POSIX errexit a failed non-final command in an `&&` list does NOT
    # stop the shell: the first draft's `set -e; mkdir && chown && chmod;` per app let a
    # failed `mkdir` fall through to the next app and still exit 0, so the caller read
    # the restore as success, dropped the download, and left that directory unrestored —
    # the silent unwritable-apps shape. Verified in a shell before rewriting it: the
    # script printed its later steps and exited 0. Raised in review.
    #
    # `[ ! -L ]` FIRST, AND IT IS A SECURITY CHECK, NOT TIDINESS. The tree being restored
    # into came from a WORLD SNAPSHOT, which is authored content. If the snapshot plants a
    # symlink at `/.apps_data/<app>` pointing at, say, `/etc`, then `mkdir -p` succeeds
    # without replacing it and `chown`/`chmod` FOLLOW IT — handing the app group ownership
    # and `2770` on a directory outside `/.apps_data`, i.e. in-sandbox privilege
    # escalation. Refusing (rather than deleting the link) keeps this a download, which is
    # always correct, and avoids destroying world content on a false positive. Raised by
    # the security reviewer.
    #
    # `find … -type d` FOR THE NESTED REPAIR, and only for app directories, never the
    # root. A directory that exists ONLY in the world snapshot arrives from the hydrated
    # image `root:root 0755` with no setgid, where the download path would have created it
    # under a `2770` parent and inherited the app group — so a non-root app creating a new
    # file inside it hits EACCES, the same class as the original outage. Running it on the
    # ROOT would instead flatten every app directory to the root's group, which is the
    # opposite of the point. `-type d` also excludes symlinks, so the traversal cannot be
    # walked out of the tree either.
    # `chown -h` SO THE CHECK-THEN-USE CANNOT BE WON ON THIS HALF. `[ ! -L "$d" ]` is a
    # separate syscall from the `chown` and `chmod` that follow, so in principle the name
    # could become a symlink in between and the metadata would land on its target.
    # `-h` makes `chown` operate on the LINK rather than what it points at, which removes
    # that half of the window outright and costs nothing on a real directory.
    #
    # `chmod` HAS NO EQUIVALENT — POSIX gives it no `-h`, and shell cannot address an
    # inode it already opened — so a narrow window remains there, and it is bounded rather
    # than dismissed. Three things bound it: the racer must already be executing inside
    # this sandbox; `apply_world_mounts` runs inside `populate_environment` BEFORE the
    # download and before the agent exists, so the agent is not a candidate and only the
    # app image's own entrypoint is; and the window is between two syscalls of one `sh`.
    # A planted symlink — the STATIC version of this, which is what a hostile world
    # snapshot can actually do — is fully closed by the test, and that was the reported
    # attack. Raised in review as the residual, and recorded because a guard whose limits
    # are unwritten reads as a guarantee.
    parts: list[str] = []
    for d, uid, gid, mode in records:
        parts.append(
            f'{{ [ ! -L "{d}" ] && mkdir -p "{d}" && chown -h {uid}:{gid} "{d}" '
            f'&& chmod {mode} "{d}"; }} || exit 1'
        )
        if d == f"{path}/{_COORDINATOR_DIR}":
            # NO GROUP REPAIR ON THE COORDINATOR, and this is a privilege boundary rather
            # than tidiness. `ROOT_ONLY_SUBTREE_NAMES` in
            # `environment/runner/coordinator/state/store.py` deliberately locks `config`,
            # `checkpoint_observations` and `event_occurrences` to root-only — the control
            # and record planes a VCA must not read or tamper with — and taiga re-locks
            # them. A blanket `chgrp <gid> + chmod g+rwxs` over this subtree would hand an
            # in-sandbox principal in that group exactly what the lock excludes.
            #
            # NEWLY REACHABLE BECAUSE OF THIS PR, which is why it is guarded here: before
            # `.coordinator` was excluded from the bake probe, a sandbox with a coordinator
            # tree always downloaded and this loop never ran on it. Raised in review.
            #
            # IT SKIPS THE WHOLE SUBTREE, not only the root-only part, and the reasoning
            # above is narrower than the effect — raised in review. `agent_configs` and
            # `agent_filesystems` are AGENT plane and deliberately outside the root-only
            # lock, so nothing forbids repairing them; they are skipped because this whole
            # tree is the coordinator's to manage. It recreates its own state directories
            # (`mkdir(parents=True, exist_ok=True)`) and re-locks them (`chmod(0o700)` on the
            # run dir), so a blanket repair here would be at best redundant and at worst
            # racing the owner.
            #
            # Nothing needs repairing anyway: the seed comes back via `cp -a`, which carries
            # its own ownership and modes.
            continue
        if d != path:
            # `g+rwxs` AND THE `w` IS THE POINT, not an over-grant — the target is parity
            # with the DOWNLOAD arm, and the download arm is group-writable here.
            #
            # Questioned in review on the reading that a downloaded world-authored
            # directory lands `0o2755`, which would make a mounted run more permissive
            # than a downloaded one and let the same task grade differently per arm. That
            # would be a real defect; it rests on umask 022, and the download does not run
            # under 022 ON THE PLATFORMS THIS IS ENABLED FOR. `download_objects` creates
            # these with a bare `os.makedirs` inside the environment runner, and the
            # runner is launched `umask 0002`, so under a `2770` setgid parent a new
            # directory inherits the app group and lands `0o2775`.
            #
            # THE UMASK IS THE RUNNER'S OWN, so this holds on every island rather than
            # only the ones whose launcher happens to set it. It was per-launcher until
            # PR #18377 — three of six emitted a `start.sh` that set it and three started
            # the runner with an exec-form `CMD` that could not, so this pass was correct
            # parity on the first three and an over-grant on the others. Found by probing
            # a live prod sandbox: it runs the `internal_platform` launcher, not the
            # shared template this comment used to cite. Pinned by
            # `test_the_nested_repair_matches_the_download_arm`.
            #
            # MEASURED, both arms side by side, rather than reasoned from the mode bits:
            #
            #   DOWNLOAD (umask 0002, os.makedirs under 2770)  -> 0o2775 gid=appgrp
            #   MOUNT    (0755 from hydration, then this pass) -> 0o2775 gid=appgrp
            #
            # So this pass is what MAKES the two arms agree, and `g+rxs` would be the
            # thing that breaks parity — mounted `2755` against downloaded `2775`, with an
            # app that creates a file in a world-authored directory failing only on the
            # mounted arm. Exactly the divergence the objection set out to prevent, in
            # reverse.
            #
            # THE FRAGILE PART IS THE COUPLING, NOT THE BITS: nothing near that template
            # says a mode here depends on it. If `runner_launch.sh.template` ever loses
            # `umask 0002`, the download arm becomes `0o2755` and THEN this must drop the
            # `w` to match. Pinned by `test_the_nested_repair_matches_the_download_arm`.
            #
            # The per-app `{gid}` is also why this is not the repair the `_copy_staged_tree`
            # note forbids: that one takes its group from the subsystem ROOT, which is
            # `root` for `.apps_data` and would file new files under group `root`. This
            # takes the group from the app directory's own pre-mount record.
            #
            # A DEPTH-1 DIRECTORY THAT EXISTS ONLY IN THE WORLD IS NOT RESTORED, and that
            # is parity-neutral rather than a gap. Raised in review as the asymmetric case:
            # the records come from a PRE-mount stat, so an app directory the hydrated
            # image carries but the app image does not is never recorded and stays
            # `root:root 0755`. Two things make it harmless, and the second is the one that
            # settles it:
            #
            #   1. it implies an app absent from the booted image, so nothing runs that
            #      needs to write there — the reverse direction (image has it, world does
            #      not) is covered by the `mkdir -p` above;
            #   2. THE DOWNLOAD ARM CANNOT WRITE THERE EITHER. `/.apps_data` is
            #      `runner:runner 0711` and NOT setgid, so there is no group to inherit and
            #      a downloaded world-only directory lands group `root`. Measured:
            #      download `775 root:root` against mount `755 root:root`. The bits differ
            #      by `g+w`; the GROUP is `root` on both, and no app user is in it, so
            #      neither arm is writable by an app. Nothing to diverge on.
            parts.append(
                f'find "{d}" -mindepth 1 -type d '
                f"-exec chgrp {gid} {{}} + -exec chmod g+rwxs {{}} + || exit 1"
            )
    script = "; ".join(parts)
    try:
        rc, _out, err = await asyncio.wait_for(
            _run(sandbox, "sh", "-c", script),
            # The SMALLER of this module's ceiling and the run's remaining populate
            # budget, matching `_copy_staged_tree`.
            timeout=(
                _RESTORE_TIMEOUT_S
                if deadline_s is None
                else min(_RESTORE_TIMEOUT_S, deadline_s)
            ),
        )
    except Exception as exc:  # noqa: BLE001
        # RETURNS FALSE RATHER THAN RAISING `WorldMountDirtyError`, and the asymmetry with
        # `_copy_staged_tree` is deliberate. There too, nothing can kill the command once
        # `exec.aio` has returned, so a timeout may leave it running — but the copy is
        # moving DATA into the path populate is about to download into, which is how two
        # worlds get blended into something that looks complete. This moves only metadata
        # on a handful of directories, and the values it would still be writing are the
        # ones the app image already had, so a straggler converges on the same state a
        # download produces. Small enough to prefer the download fallback over failing the
        # run. Raised in review as a question; recorded rather than changed.
        logger.warning(
            f"{_ADAPTIVE_LOG} could not restore the app directories under {path}, "
            f"downloading: {exc!r}"
        )
        return False
    if rc != 0:
        logger.warning(
            f"{_ADAPTIVE_LOG} restoring the app directories under {path} failed "
            f"rc={rc}, downloading; stderr={err.strip()[-300:]!r}"
        )
        return False
    return True


async def _adaptive_restore_plan(
    sandbox: SupportsMounting, mount: WorldMount, path: str
) -> list[tuple[str, int, int, str]] | None:
    """Directories to restore after mounting, or `None` to DOWNLOAD this half instead.

    THE WHOLE ADAPTIVE DECISION, and it lives here rather than inline because every
    refusal in it has the same consequence — download — so collapsing them to one
    `None` says that once instead of four times, and keeps `apply_world_mounts` inside
    its complexity budget.

    Only this process can make the call: it depends on what the APP IMAGE ships, which
    the server cannot see.
    """
    if mount.subsystem not in _ADAPTIVE_SUBSYSTEMS:
        # Keeps `filesystem` off this route. It is already free, and its
        # one-shared-gid repair is what makes it usable — this path would skip it.
        logger.warning(
            f"{_ADAPTIVE_LOG} {mount.subsystem!r} is not adaptive, downloading"
        )
        return None
    if path != f"/{mount.subsystem}":
        # It resolves to a DESTINATION mount, so the path must be the real root. A
        # staging path here would mount somewhere nothing reads and still return the
        # key, dropping the download.
        logger.warning(
            f"{_ADAPTIVE_LOG} {path} is not the root for {mount.subsystem}, downloading"
        )
        return None
    # WHICH QUESTION TO ASK DEPENDS ON THE FAMILY, and this is the whole of the parent
    # exception. For a world the test is "does the image bake anything here"; for a
    # continuation it is the narrower "does it bake anything NO snapshot carries",
    # because the parent snapshot already holds the rest in its post-run form. See
    # `_PARENT_SOURCE_FAMILY`.
    #
    # FALLS BACK TO THE STRICT PROBE FOR EVERY OTHER FAMILY, including a key this
    # runner does not recognise, so the exception can only ever widen mounting for the
    # one family whose superset property was actually argued.
    is_parent = mount.source_key.startswith(_PARENT_SOURCE_FAMILY)
    baked = await (
        _image_bakes_unsnapshotted_files(sandbox, path)
        if is_parent
        else _image_bakes_files(sandbox, path)
    )
    if baked is not False:
        # `True` — the image ships files a mount would replace, and they exist nowhere
        # else. `None` — it could not be asked, which must not be read as "no files"
        # because that direction loses data. Both download; the log says which.
        logger.info(
            f"{_ADAPTIVE_LOG} {path} downloading — "
            + (
                (
                    "the app image bakes content there that no snapshot carries, "
                    "which a mount would delete"
                    if is_parent
                    else "the app image bakes content there, which a mount would replace"
                )
                if baked
                else "could not determine what the app image bakes there"
            )
        )
        return None
    # Read BEFORE the mount, and a failure has to stop it rather than proceed without
    # the records: mounting first and then finding we cannot restore leaves every app
    # directory `root:root` with no setgid, which is the original outage.
    return await _read_subdir_modes(sandbox, path)


async def _mounted_half_is_usable(
    sandbox: SupportsMounting,
    mount: WorldMount,
    path: str,
    *,
    shared_gid: int | None,
    restore: list[tuple[str, int, int, str]] | None,
) -> bool:
    """Whether a half that is now MOUNTED may be kept, or has to be unmounted and
    downloaded.

    Extracted for the same reason `_adaptive_restore_plan` is: every refusal in here
    has the same consequence, and inlining the three routes' gates put
    `apply_world_mounts` over its complexity ceiling.

    THE TWO EXITS THAT COST SOMETHING. Everything the adaptive decision refuses before
    this point is free — a probe and a stat. These two refuse a mount already made, so
    they are the exits a rollout most needs to be able to see, and both delegate to
    helpers `filesystem` shares which therefore carry no `_ADAPTIVE_LOG` prefix. Adding
    it here rather than in those helpers keeps the other half's log lines unchanged.
    """
    if not await _verify(sandbox, path, mount.manifest_sha):
        if restore is not None:
            # Phrased apart from the writability exit below deliberately: one prefix
            # naming the wrong cause is worse for triage than no prefix, because it
            # reads as evidence.
            logger.info(
                f"{_ADAPTIVE_LOG} {path} downloading — the mounted half did not verify "
                f"against its manifest"
            )
        return False
    if shared_gid is not None:
        return await _files_are_writable(sandbox, path) and (
            await _repair_directories(sandbox, path, shared_gid)
        )
    if restore is None:
        return True
    # THE ADAPTIVE PATH NEEDS THE WRITABILITY GATE TOO, and the first draft skipped it
    # because the gate was tied to `shared_gid`, which only `filesystem` has.
    #
    # AND ONE SAMPLE IS SOUND HERE FOR A REASON THE SOURCE ROUTE CANNOT CLAIM: the bake
    # probe has ALREADY established that the image ships nothing under this root, so
    # every file visible through the mount came from hydration and carries
    # `HYDRATED_FILE_MODE`. The population is uniform, which is what makes a single
    # `-quit` sample representative rather than a guess. Raised in review as the stronger
    # argument, and it is — the contrast with the copy route explains why the gate is
    # NEEDED, not why one sample suffices.
    #
    # A DIRECTORIES-ONLY HALF DOWNLOADS, and that is a real cost accepted rather than an
    # oversight: `_files_are_writable` treats "no file to sample" as unusable, so such a
    # half pays mount + verify + unmount and downloads anyway. Kept because a half with
    # no files has nothing to gain from being mounted, so the wasted work is bounded by
    # how fast an empty half downloads — but the log it emits says "treating the image as
    # unusable" about a perfectly healthy half, which will read as a defect in triage.
    # Flagged in review; the honest fix is in `_files_are_writable`, which `filesystem`
    # shares, so it is not changed from here.
    #
    # The source route documented skipping it as acceptable — `download_objects` repairs
    # modes only under `FILESYSTEM_ROOT`, so a pre-`HYDRATED_FILE_MODE` image lands the
    # same `0644` files whether copied in or downloaded. THAT ARGUMENT DOES NOT CARRY TO
    # A MOUNT: a copy puts writable-by-owner files on local disk, while a mount serves
    # the image's own `root:root 0644` through 9p, which a non-root app cannot write. No
    # directory repair here — the per-app restore does that job, and one shared gid would
    # be wrong for `.apps_data` anyway. Raised in review.
    if await _files_are_writable(sandbox, path):
        return True
    # THE EXIT THE PREFIX CLAIMED AND DID NOT HAVE. `_files_are_writable` logs without
    # the prefix and this path then fell through, so a writability refusal — which
    # includes every directories-only half — read exactly like adaptive never engaging.
    # The claim was in the comment at `_ADAPTIVE_LOG` and the test drove every exit but
    # this one, which is how the gap stayed green. Raised in review.
    logger.info(
        f"{_ADAPTIVE_LOG} {path} downloading — the mounted half is not writable "
        f"(a half with no file to sample reads as unusable here too)"
    )
    return False


async def _service_state(sandbox: SupportsMounting, ping: str) -> bool | None:
    """True up, False down, None could-not-tell.

    THREE-VALUED ON PURPOSE, and an earlier version was not. It collapsed the
    error case to False — and False is the SUCCESS value when confirming a stop,
    so a control-plane blip on `exec` read as a verified shutdown and the mount
    proceeded over a live server. The stop command already fails closed on that
    same exception; the ping did the opposite, which is precisely the silent
    live-datadir swap this route exists to refuse. Raised in review.
    """
    try:
        rc, _out, _err = await _run(sandbox, "bash", "-lc", ping)
    except Exception:  # noqa: BLE001 — cannot tell is its own answer here
        return None
    return rc == 0


async def _await_service(
    sandbox: SupportsMounting, ping: str, *, up: bool, tries: int
) -> bool:
    """Poll until the service is confirmed `up` (or confirmed down). True if it got there.

    `is up` against a three-valued state, so None — could not tell — never
    counts as confirmation in either direction. That is the whole point: an
    unreadable ping must not satisfy "it stopped".
    """
    for _ in range(tries):
        if await _service_state(sandbox, ping) is up:
            return True
        await asyncio.sleep(1)
    return await _service_state(sandbox, ping) is up


async def _give_root_to_service(
    sandbox: SupportsMounting,
    control: dict[str, str],
    path: str,
    label: str,
    image_id: str,
) -> None:
    """Give the mount root to the service's account. Best effort by design.

    Not fatal on failure: `_service_can_write` runs immediately after and is the
    gate. If the chown did not take, that probe declines the mount and the run
    imports as today — so a failure here costs the optimisation, never the run,
    and raising would convert a soft miss into a hard one.
    """
    from loguru import logger  # import-check-ignore

    user = control.get("user")
    if not user:
        return
    try:
        rc, _out, err = await _run(
            sandbox,
            "bash",
            "-lc",
            _SERVICE_STATE_CHOWN_ROOT_COMMAND.format(user=user, path=path),
        )
    except Exception as exc:  # noqa: BLE001 — the probe below is the real gate
        logger.warning(
            f"could not give {path} to {user} for {label} (image {image_id}): {exc!r}"
        )
        return
    if rc != 0:
        logger.warning(
            f"chown of {path} to {user} for {label} (image {image_id}) exited "
            f"{rc} ({err.strip()[-200:]!r}); the writability probe will decide"
        )


async def _service_can_write(
    sandbox: SupportsMounting,
    control: dict[str, str],
    path: str,
    label: str,
    image_id: str,
) -> bool:
    """Whether the service's own account can write the mounted tree.

    See `_SERVICE_STATE_WRITABLE_COMMAND`. Returns False on any doubt, including
    an exec that could not run at all — an unanswered question here has the same
    consequence as a no, and the consequence is a decline rather than a failure.

    `image_id` is carried purely so the decline names WHICH image it refused.
    Without it — the shape this had during the 2026-08-27 rollout — "every image
    is refused" and "the stale images captured before the fix are refused" are
    the same line, and telling them apart cost a Modal experiment that a log
    field would have answered.
    """
    user = control.get("user")
    if not user:
        logger.warning(
            f"no service account known for {label}; cannot confirm the mounted "
            f"tree is writable, so not mounting image {image_id}"
        )
        return False
    try:
        rc, _out, err = await _run(
            sandbox,
            "bash",
            "-lc",
            _SERVICE_STATE_WRITABLE_COMMAND.format(path=path, user=user),
        )
    except Exception as exc:  # noqa: BLE001 — importing is always correct
        logger.warning(
            f"could not test whether {user} can write {path} "
            f"(image {image_id}): {exc!r}"
        )
        return False
    if rc != 0:
        logger.info(
            f"not mounting {label} image {image_id}: {user} cannot write the "
            f"mounted tree at {path} (rc={rc} {err.strip()[-200:]!r}); "
            f"importing instead"
        )
        return False
    return True


async def _apply_service_state_mount(
    sandbox: SupportsMounting, mount: WorldMount, path: str
) -> set[str]:
    """Mount a directory a running service owns; the key iff it mounted and verified.

    Returns a SET rather than a bool so the call site is a single branch —
    `apply_world_mounts` sits on ruff's complexity ceiling, and a second branch
    there would force a refactor of the three strategies this route deliberately
    does not touch.

    KEPT ENTIRELY OUT OF THE MAIN LOOP, deliberately. The three existing strategies
    share a body with five failure exits, and this route has to restart a service on
    every one of them — threading that through would put a new obligation on paths
    that currently have none, in the module where a missed exit is a blended world.
    Here the restart is one `finally`.

    The sequence, and each step is why the next is possible:

    1. STOP the service. Mounting over a live mysqld swaps the directory beneath a
       process holding handles into it: the mount reports success and the server goes
       on serving what it already had, so the import the mount exists to skip runs
       anyway. Verified by polling `ping`, not by the stop command's exit code — a
       shutdown that returns 0 while still flushing would let the mount race it.
    2. MOUNT and verify the digest, exactly as a destination mount does.
    3. START it again, on the mounted datadir, so the world is back in the state the
       start script guaranteed before populate hooks run. The connector's
       `ensure_mysql_running()` then finds it already up, and `_seed_already_baked`
       finds a marker, a matching seed digest and real tables — and skips the import.

    RESTARTING IS UNCONDITIONAL WITH ONE EXCEPTION. If the mount failed, if the digest
    was wrong, if the image could not be read — the service must still come back: this
    runs mid-populate, the remaining hooks and the whole agent phase expect it
    listening, and a world whose database never came back is far worse than one that
    merely imported.

    THE EXCEPTION IS A REFUSED TREE THAT WILL NOT UNMOUNT, and it is the one case that
    raises `WorldMountDirtyError` instead. An earlier version restarted here too, on
    the reasoning that nothing downloads `/var/lib/mysql` so a refused image left
    mounted cannot blend with a download the way `worlds/` would. That reasoning was
    wrong and review caught it: the danger is not a download, it is THE CONNECTOR'S
    OWN GUARD. A refused tree still carries a marker, a matching seed digest and real
    tables, so bringing the service up on it makes `_seed_already_baked` skip the
    import — and the agent runs against a world this function explicitly refused,
    while the empty return value says the opposite. The import decision is made by the
    live datadir, not by what this returns.

    So that case does not restart, and the run stops. Failing one trajectory beats
    grading one against a world nobody chose — the same trade the destination path
    makes, reached through a different mechanism.

    Every other exit returns an empty set and the run imports as it does today. A
    refused image that unmounts CLEANLY is one of those: the datadir is then as the app
    image built it, so the service comes back and nothing is lost but the mount.
    """
    control = _SERVICE_STATE_CONTROL.get(mount.subsystem)
    if control is None:
        logger.warning(
            f"skipping a service-state mount: no stop/start known for "
            f"{mount.subsystem!r}, and mounting over a live service is worse than "
            f"downloading"
        )
        return set()

    was_up = await _service_state(sandbox, control["ping"])
    if was_up is None:
        # Could not tell. Declining costs the optimisation; guessing "down" and
        # mounting over a server that is actually up costs the run's whole point
        # while reporting success.
        logger.warning(
            f"could not determine whether {mount.subsystem} is running; not "
            f"mounting over a service whose state is unknown"
        )
        return set()
    mounted = False
    refused_and_stuck = False
    # SET BEFORE THE STOP IS ISSUED, NOT AFTER, and that is the whole point of it.
    # Once `mysqladmin shutdown` has been sent, this function owes a restart on
    # every path out — including the ones where the stop did not confirm. An
    # earlier version returned from inside the stop block, outside the `finally`,
    # reasoning that a timeout meant the service "never stopped, so it is still
    # serving". That is not sound: the shutdown was already issued, and one that
    # lands after the window leaves mysqld down with nothing here to bring it
    # back. Recovery then rested on the connector's `ensure_mysql_running()`, in
    # another repo, which this module cannot verify. Raised in review, and it was
    # the one exit that broke the invariant the rest of this function keeps.
    stop_issued = False
    try:
        if was_up:
            stop_issued = True
            try:
                await _run(sandbox, "bash", "-lc", control["stop"])
            except Exception as exc:  # noqa: BLE001
                # Still owed a restart: the exception may have been raised after
                # the command reached the server, and there is no way to tell
                # from here which side of that line it fell.
                logger.warning(f"could not stop {mount.subsystem}: {exc!r}")
                return set()
            if not await _await_service(
                sandbox, control["ping"], up=False, tries=_SERVICE_STATE_STOP_TRIES
            ):
                # FAIL CLOSED on the mount — never mount over a service that has
                # not confirmed it stopped. The `finally` below still restarts it,
                # which covers the case this comment used to get wrong: a shutdown
                # that completes after the window.
                logger.warning(
                    f"{mount.subsystem} did not stop within "
                    f"{_SERVICE_STATE_STOP_TRIES}s; not mounting over a service "
                    f"that has not confirmed it stopped"
                )
                return set()

        try:
            image = await modal.Image.from_id.aio(mount.image_id)
            await sandbox.mount_image.aio(path, image)
        except Exception as exc:  # noqa: BLE001 — a mount failure is a cache miss
            logger.warning(
                f"could not mount {mount.image_id} at {path}, importing: {exc!r}"
            )
            return set()
        mounted = True

        # HAND THE ROOT OVER BEFORE ASKING WHETHER IT IS WRITABLE. The mount
        # creates that directory as `root:root`, so the probe below would
        # otherwise fail on every mount no matter what the image contains — as
        # it did in production, twice.
        await _give_root_to_service(
            sandbox, control, path, mount.subsystem, mount.image_id
        )
        if not await _mounted_half_is_usable(
            sandbox, mount, path, shared_gid=None, restore=None
        ) or not await _service_can_write(
            sandbox, control, path, mount.subsystem, mount.image_id
        ):
            if not await _unmount(sandbox, path):
                # NOT a fallback, and not merely logged — see the docstring. The
                # refused tree still carries a marker, a matching seed digest and
                # real tables, so bringing the service up on it makes the
                # connector's guard skip the import and the agent runs against a
                # world we refused. Nothing downstream can tell.
                refused_and_stuck = True
                raise WorldMountDirtyError(
                    f"{path} still holds refused image {mount.image_id} after "
                    f"{_UNMOUNT_ATTEMPTS} unmount attempts; refusing to restart "
                    f"{mount.subsystem} onto it"
                )
            return set()
        return {mount.source_key}
    finally:
        # NOT after a refused-and-stuck mount: restarting is the very thing that
        # would let the guard fire on the tree we just refused.
        if (stop_issued or mounted) and not refused_and_stuck:
            try:
                await _run(sandbox, "bash", "-lc", control["start"])
            except Exception as exc:  # noqa: BLE001
                logger.error(f"could not restart {mount.subsystem}: {exc!r}")
            if not await _await_service(
                sandbox, control["ping"], up=True, tries=_SERVICE_STATE_START_TRIES
            ):
                # The one genuinely bad outcome, and it is louder than a warning
                # because every populate hook and the whole agent phase after this
                # expects the service listening.
                logger.error(
                    f"{mount.subsystem} did not come back within "
                    f"{_SERVICE_STATE_START_TRIES}s after a service-state mount"
                )


async def apply_world_mounts(
    sandbox: SupportsMounting,
    mounts: list[WorldMount],
    *,
    copy_deadline_s: float | None = None,
) -> set[str]:
    """Mount what can be mounted; return the S3 source URLs that no longer need it.

    Raises `WorldMountDirtyError` and NOTHING else. Every other failure — an unusable
    image, a wrong digest, Modal unreachable — leaves the half in the caller's
    download list, because mounting is an optimization on a path that already works
    and must not be the reason a trajectory stops working. An empty return is always
    correct, just slower.

    `"dirty"` cannot take that path: a refused image is still mounted, a mount is
    read-write, so the download the caller is about to do would succeed and blend two
    worlds into a tree that looks complete. That is the one case where continuing
    costs more than stopping.

    A key is returned only for a half that mounted AND verified AND is usable. The
    caller matches it against the END of each source URL — not the subsystem, because
    the task layer contributes a source for the same subsystem and dropping that
    would omit the task's files from a tree that still looks complete; and not a full
    URL, because the bucket name is built from `ENV` here and from `HOSTED_ENV` on
    the server, which disagree for LOCAL.
    """
    mounted: set[str] = set()
    for mount in mounts:
        try:
            path = _validated_mount_path(mount.mount_path)
        except ValueError as exc:
            logger.warning(f"skipping a world mount: {exc!r}")
            continue
        if not _valid_source_key(mount.source_key):
            # Refused BEFORE mounting, not just before matching: a key we would not
            # honour must not produce a mount whose source then stays in the download
            # list, which would write the world through a mount of itself.
            logger.warning(
                f"skipping a world mount: unusable source key "
                f"{mount.source_key!r} for {mount.mount_path}"
            )
            continue

        if mount.strategy not in _KNOWN_STRATEGIES:
            # An unrecognised strategy is a server newer than this runner. Downloading
            # is the only safe reading: mounting by the rules we DO know could put the
            # half somewhere the sender never meant and still drop its source.
            logger.warning(
                f"skipping a world mount: unknown strategy {mount.strategy!r} for "
                f"{mount.mount_path}"
            )
            continue
        if mount.strategy == _SERVICE_STATE_STRATEGY:
            # ITS OWN ROUTE, taken before anything below runs. It has to stop and
            # restart a service around the mount, and every failure exit below would
            # otherwise owe that restart — see `_apply_service_state_mount`.
            mounted |= await _apply_service_state_mount(sandbox, mount, path)
            continue

        # THE ADAPTIVE DECISION. Everything below then treats the result as the
        # destination mount it became, so there is no third code path to keep in step.
        restore: list[tuple[str, int, int, str]] | None = None
        if mount.strategy == "adaptive":
            restore = await _adaptive_restore_plan(sandbox, mount, path)
            if restore is None:
                continue
            # BEFORE the mount, because the mount is what hides it. The bake probe
            # deliberately ignores `.coordinator`, so preserving it here is the other
            # half of that exclusion — see `_BAKED_FILES_PROBE`.
            if not await _seed_coordinator_aside(
                sandbox, path, deadline_s=copy_deadline_s
            ):
                continue
        elif (
            mount.subsystem in _ADAPTIVE_SUBSYSTEMS and mount.strategy == "destination"
        ):
            # A PLAIN DESTINATION MOUNT OF `.apps_data` IS THE OUTAGE ITSELF, so it is
            # refused here rather than trusted not to arrive. It would mount over the
            # real root with `restore` still None — no bake probe, no directory restore,
            # no writability gate (that one is keyed on `restore`) — leaving every app
            # directory `root:root` without setgid.
            #
            # The server was fixed not to be able to express this, but the check belongs
            # here too: this process obeys whatever the wire says, and "the sender would
            # not do that" is the assumption the strategy field exists to avoid relying
            # on. Costs one comparison and closes the shape for any sender. Raised in
            # review as a latent server-side hole.
            logger.warning(
                f"skipping a world mount: {mount.subsystem} may not be plainly "
                f"destination-mounted; it needs the adaptive route"
            )
            continue
        is_source_mount = mount.strategy == "source"
        if is_source_mount:
            # THREE CHECKS, each closing a way the copy could land somewhere it should
            # not. Skipping downloads the half, which is always correct.
            if mount.subsystem not in _SOURCE_MOUNTABLE_SUBSYSTEMS:
                # Also keeps `filesystem` off this route: a source-mounted `filesystem`
                # would skip `_repair_directories`, and `cp -a` does not reproduce what
                # the download path's per-object repair does, so the tree would land
                # unwritable by the app group.
                logger.warning(
                    f"skipping a source mount: {mount.subsystem!r} is not "
                    f"source-mountable"
                )
                continue
            if path != f"{_SOURCE_MOUNT_ROOT}/{mount.subsystem}":
                # The staging path must be somewhere OTHER than the real root. If they
                # coincided, the copy would write into the mount's own copy-on-write
                # layer and the unmount below would then discard it — while the key
                # still went back as mounted, dropping the populate source. The half
                # would exist nowhere and the run would look complete.
                logger.warning(
                    f"skipping a source mount: {path} is not the staging path for "
                    f"{mount.subsystem}"
                )
                continue
        needs_repair = (
            not is_source_mount and mount.subsystem in _SUBSYSTEMS_NEEDING_REPAIR
        )
        shared_gid: int | None = None
        if is_source_mount:
            # The staging root is in no APP IMAGE, and `mount_image` needs the path
            # to exist. Created here so a source-mount needs no coordinated app-image
            # rebuild.
            try:
                rc, _out, err = await _run(sandbox, "mkdir", "-p", path)
            except Exception as exc:  # noqa: BLE001 — downloading is always correct
                logger.warning(f"could not create the staging path {path}: {exc!r}")
                continue
            if rc != 0:
                logger.warning(
                    f"creating the staging path {path} failed rc={rc} "
                    f"stderr={err.strip()[-300:]!r}"
                )
                continue
        if needs_repair:
            # BEFORE the mount, while the environment image's own directory is still
            # the thing at this path.
            shared_gid = await _read_shared_gid(sandbox, path)
            if shared_gid is None:
                logger.warning(
                    f"not mounting {mount.image_id} at {path}: no group to repair to, "
                    f"so the tree would be unwritable"
                )
                continue

        try:
            image = await modal.Image.from_id.aio(mount.image_id)
            await sandbox.mount_image.aio(path, image)
        except Exception as exc:  # noqa: BLE001 — a mount failure is a cache miss
            logger.warning(
                f"could not mount {mount.image_id} at {path}, downloading: {exc!r}"
            )
            continue

        usable = await _mounted_half_is_usable(
            sandbox, mount, path, shared_gid=shared_gid, restore=restore
        )

        if not usable and is_source_mount:
            # NOT the dirty path. A refused destination mount is dangerous because the
            # download about to happen writes to that exact path; the staging path is
            # not a download target, so a stuck mount there costs a duplicate in the
            # end-of-run world snapshot rather than a blend of two worlds.
            if not await _unmount(sandbox, path):
                logger.error(
                    f"could not clear the refused staging mount at {path}; this sandbox "
                    f"stays marked as mounted, so it will not be reused or captured"
                )
            continue

        if not usable:
            # Put the path back so the download about to happen writes into the
            # environment image's own directory rather than through a mount of a
            # world we just refused.
            if not await _unmount(sandbox, path):
                # NOT a fallback. The refused tree is still there, a mount is
                # read-write, and the caller's next act is to download into this
                # exact path — which would succeed and produce a blend of two worlds
                # that looks complete. Leaving the source in place and logging was
                # the first version of this, and it is precisely the write the guard
                # exists to prevent.
                raise WorldMountDirtyError(
                    f"{path} still holds refused image {mount.image_id} after "
                    f"{_UNMOUNT_ATTEMPTS} unmount attempts; refusing to populate "
                    f"over it"
                )
            continue

        if is_source_mount:
            # Copy, then unmount on every path that RETURNS from here. The dirty
            # exits above raise instead, deliberately: a live `tar` may still be
            # reading the staged tree, so tearing it out is the wrong move.
            # and leaving it behind puts a second copy of the half in the end-of-run
            # world snapshot, which walks everything.
            subsystem_root = f"/{mount.subsystem}"
            # `_copy_staged_tree` raises `WorldMountDirtyError` itself when a writer
            # may still be running, and returns False only when nothing is — so there
            # is nothing left to classify here.
            #
            # NO WRITABILITY GATE HERE, deliberately, and the reasoning is the
            # module's existing one about `.apps_data` parity rather than an
            # oversight. `download_objects` repairs modes only for
            # `FILESYSTEM_ROOT`, so a pre-`HYDRATED_FILE_MODE` image lands the
            # same `0644` files whether it is downloaded or copied in — refusing
            # it here would cost every such image a full S3 download and buy no
            # correctness. The gate would matter for a source-mounted
            # `filesystem`, which `_SOURCE_MOUNTABLE_SUBSYSTEMS` makes
            # unreachable; that exclusion is what keeps this safe, not a check.
            copied = await _copy_staged_tree(
                sandbox,
                path,
                subsystem_root,
                # The SMALLER of the run's remaining populate budget and this
                # module's ceiling, so a short campaign deadline is honoured.
                timeout_s=(
                    _COPY_TIMEOUT_S
                    if copy_deadline_s is None
                    else min(_COPY_TIMEOUT_S, copy_deadline_s)
                ),
            )
            if not await _unmount(sandbox, path):
                # Unlike a refused destination mount this is NOT dirty: the staging
                # path is not where anything downloads to, so a stale mount there
                # cannot blend two worlds. It only costs a duplicate in the world
                # snapshot,
                # which is worth a loud log and not worth failing a run over.
                logger.error(
                    f"could not clear the staging mount at {path}; this sandbox stays marked "
                    f"as mounted, so it will not be reused or captured"
                )
            if not copied:
                # The caller must still download this half. Returning the key would
                # drop its populate source and lose the half entirely — silently,
                # because every other signal here says the mount succeeded.
                continue
            logger.info(
                f"source-mounted {mount.image_id} via {path} into {subsystem_root}"
            )
            mounted.add(mount.source_key)
            continue

        if restore is not None and not (
            await _restore_subdirs(sandbox, path, restore, deadline_s=copy_deadline_s)
            and await _restore_coordinator(sandbox, path, deadline_s=copy_deadline_s)
        ):
            # AFTER the mount and BEFORE the key goes back, because the key is what
            # drops the download. A restore that failed leaves app directories the
            # apps cannot write to — the original outage — so the half downloads
            # instead. Unmounted first so populate writes into the image's own
            # directory rather than through a mount we have just refused.
            if not await _unmount(sandbox, path):
                raise WorldMountDirtyError(
                    f"{path} still holds {mount.image_id} after a failed directory "
                    f"restore and {_UNMOUNT_ATTEMPTS} unmount attempts; refusing to "
                    f"populate over it"
                )
            continue

        logger.info(
            f"mounted {mount.image_id} at {path}"
            + (f" (directories repaired to gid {shared_gid})" if needs_repair else "")
            + (
                f" (adaptive: {len(restore)} app directories restored)"
                if restore is not None
                else ""
            )
        )
        mounted.add(mount.source_key)
    return mounted


def live_mount_subsystems(
    mounts: list[WorldMount], mounted_keys: set[str]
) -> frozenset[str]:
    """Which subsystem roots are still 9p mounts once `apply_world_mounts` returns.

    WHY THIS IS NOT `mounted_keys`. That set answers "may the caller stop
    downloading this half", and BOTH strategies answer yes. Only `destination`
    (and an `adaptive` that became one) leaves a mount behind; `source` copies the
    staged tree into the real root and unmounts, so its files are on local disk by
    the time anything reads them. Conflating the two would tell the capture that a
    perfectly ordinary local root is slow.

    WHAT READS IT. The end-of-run snapshot, which walks these roots and uploads
    every file. A read over 9p measured ~4.6x slower than local disk, and the
    uploader ABANDONS a file past its per-attempt budget — so the capture has to
    know which roots it is about to read slowly, and only this process can tell it.
    See `_UPLOAD_MOUNT_SLOWDOWN_FACTOR` in
    `environment/runner/data/snapshot/main.py`.

    Derived from the plan rather than tracked inside the loop deliberately: the
    loop's `mounted` set is what nine tests assert against, and a second return
    value would churn all of them to describe something they are not about.

    A key naming a half that did not mount contributes nothing, so a caller may
    pass its whole plan.
    """
    return frozenset(
        mount.subsystem
        for mount in mounts
        if mount.source_key in mounted_keys and mount.strategy != "source"
    )
