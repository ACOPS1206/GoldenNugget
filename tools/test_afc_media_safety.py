#!/usr/bin/env python3
"""Offline test for the AFC media safety guards (no device needed).

Regression cover for a real data-loss report: after Apply Tweaks a user had
lost ~11 GB of photos, videos would not play, and Photos reported "unable to
load a higher quality version of this video".

Two independent causes are pinned here:

1. Completeness was decided by "the media folder is not empty", so a partial
   pull counted as the only copy of the photos and the backup's own media rows
   were deleted (see ``clean_backup_for_restore``).
2. ``_pull_one`` broke out of its read loop on the first empty chunk without
   checking the result against ``st_size``, so a short read was written out as
   a truncated file and reported as a success.

Exercises: the ``.media_state.json`` verification marker, invalidation on
failure/cancellation, atomic + size-checked pull, the pre-apply gate, and the
push side (no host bookkeeping pushed, short writes detected).

Run: python tools/test_afc_media_safety.py
"""
import asyncio
import inspect
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.devicemanagement.device_manager import DeviceManager
from src.exceptions.nugget_exception import NuggetException
from src.restore import afc_media
from src.restore.afc_media import (
    MEDIA_STATE_FILE,
    _invalidate_media_state,
    _pull_one,
    _write_media_state,
    describe_media_store,
    media_store_state,
    media_store_verified,
)
from src.restore import restore as restore_mod

PASS = 0
TMP = tempfile.mkdtemp(prefix="gn_afc_media_")


def check(name, cond, extra=""):
    global PASS
    assert cond, f"FAILED: {name} {extra}"
    PASS += 1
    print(f"  ok: {name}" + (f"  [{extra}]" if extra else ""))


def store(name):
    return os.path.join(TMP, name)


def write_file(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


# --- a fake AFC service that can misbehave exactly like the real one did -----
class FakeAfc:
    """Stand-in for pymobiledevice3's AfcService (pull + push surface only)."""

    def __init__(self, files=None, short_reads=(), drop_after_first_chunk=()):
        self.files = dict(files or {})
        self.short_reads = set(short_reads)
        self.drop_after_first_chunk = set(drop_after_first_chunk)
        self.written = {}
        self.made_dirs = []
        self._pos = {}

    async def listdir(self, path):
        if path == "/":
            trees = {p.split("/")[1] for p in self.files}
            return [".", ".."] + sorted(trees)
        prefix = path.rstrip("/") + "/"
        kids = {p[len(prefix):].split("/")[0]
                for p in self.files if p.startswith(prefix)}
        return [".", ".."] + sorted(kids)

    async def stat(self, path):
        if path in self.written:
            return {"st_size": len(self.written[path]), "st_ifmt": "S_IFREG"}
        if path in self.files:
            return {"st_size": len(self.files[path]), "st_ifmt": "S_IFREG"}
        return {"st_size": 0, "st_ifmt": "S_IFDIR"}

    async def exists(self, path):
        return True

    async def makedirs(self, path):
        self.made_dirs.append(path)

    async def get_file_contents(self, path):
        return self.files[path]

    async def fopen(self, path, mode="r"):
        self._pos[path] = 0
        return path

    async def fread(self, handle, size):
        data = self.files[handle]
        if handle in self.drop_after_first_chunk and self._pos.get(handle, 0) > 0:
            return b""                                  # connection dies
        start = self._pos.get(handle, 0)
        if handle in self.short_reads:                  # device sends a stub
            self._pos[handle] = start + size // 2
            return data[start:start + size // 2]
        if start >= len(data):
            return b""                                  # genuine EOF
        chunk = data[start:start + size]
        self._pos[handle] = start + len(chunk)
        return chunk

    async def fclose(self, handle):
        pass

    async def fwrite(self, handle, chunk):
        self.written[handle] = self.written.get(handle, b"") + chunk

    async def set_file_contents(self, path, data):
        self.written[path] = data


class _Ctx:
    def __init__(self, afc):
        self.afc = afc

    async def __aenter__(self):
        return self.afc

    async def __aexit__(self, *a):
        return False


def use_afc(fake, fn):
    """Run `fn` with AfcService patched to hand back `fake`."""
    real = afc_media.AfcService
    afc_media.AfcService = lambda lockdown_client: _Ctx(fake)
    try:
        return fn()
    finally:
        afc_media.AfcService = real


# =============================================================================
def test_marker_gate():
    print("\nmedia_state.json verification gate")
    empty = store("empty")
    os.makedirs(empty)
    check("empty dir is NOT verified", not media_store_verified(empty))
    check("missing dir is NOT verified", not media_store_verified(store("nope")))
    check("no path at all is NOT verified", not media_store_verified(""))

    good = store("good")
    write_file(os.path.join(good, "DCIM", "100APPLE", "IMG_0001.JPG"), b"x" * 1000)
    _write_media_state(good, files=1, bytes_=1000, skipped=0)
    check("freshly pulled store IS verified", media_store_verified(good))
    check("describe says verified", "verified" in describe_media_store(good))

    # the reported bug: an interrupted pull leaves files behind, so the old
    # "is the folder non-empty" test passed on a fragment
    partial = store("partial")
    write_file(os.path.join(partial, "DCIM", "half.MOV"), b"x" * 10)
    check("partial store with files is NOT verified",
          not media_store_verified(partial))

    # a store that was good and then lost files must stop counting as verified
    shrunk = store("shrunk")
    for i in range(5):
        write_file(os.path.join(shrunk, "DCIM", f"f{i}.MOV"), b"y" * 500)
    _write_media_state(shrunk, files=5, bytes_=2500, skipped=0)
    check("complete store verified", media_store_verified(shrunk))
    os.remove(os.path.join(shrunk, "DCIM", "f3.MOV"))
    os.remove(os.path.join(shrunk, "DCIM", "f4.MOV"))
    check("store that lost files is NOT verified", not media_store_verified(shrunk))

    # extra local files are normal (device-deleted files are never removed)
    extra = store("extra")
    write_file(os.path.join(extra, "DCIM", "a.MOV"), b"z" * 100)
    _write_media_state(extra, files=1, bytes_=100, skipped=0)
    write_file(os.path.join(extra, "DCIM", "old_removed.MOV"), b"z" * 900)
    check("store larger than the marker is still verified",
          media_store_verified(extra))

    zero = store("zero")
    os.makedirs(zero)
    _write_media_state(zero, files=0, bytes_=0, skipped=0)
    check("zero-file store is NOT verified", not media_store_verified(zero))

    corrupt = store("corrupt")
    os.makedirs(corrupt)
    with open(os.path.join(corrupt, MEDIA_STATE_FILE), "w") as f:
        f.write("{not json")
    check("unparsable marker is NOT verified", not media_store_verified(corrupt))

    dead = store("dead")
    write_file(os.path.join(dead, "DCIM", "a.MOV"), b"q" * 100)
    _write_media_state(dead, files=1, bytes_=100, skipped=0)
    check("verified before interruption", media_store_verified(dead))
    _invalidate_media_state(dead, "test")
    check("marker gone after interruption", media_store_state(dead) is None)
    check("interrupted store is NOT verified", not media_store_verified(dead))


# =============================================================================
def test_pull():
    print("\nbackup_media_via_afc: marker only after a clean finish")
    big = b"a" * (9 * 1024 * 1024)          # > MAXIMUM_READ_SIZE -> streaming
    files = {
        "/DCIM/100APPLE/VID_0001.MOV": big,
        "/DCIM/100APPLE/IMG_0001.JPG": b"j" * 100,
    }

    done = store("pulled")
    os.makedirs(done)
    tally = use_afc(FakeAfc(files), lambda: asyncio.run(
        afc_media.backup_media_via_afc(None, done)))
    check("pull reported both files", tally["files"] == 2, tally)
    check("store verified after a clean pull", media_store_verified(done))
    check("big file fully written",
          os.path.getsize(os.path.join(done, "DCIM", "100APPLE",
                                       "VID_0001.MOV")) == len(big))
    check("no .part leftovers", not _has_part(done))

    dropped = store("dropped")
    os.makedirs(dropped)
    _write_media_state(dropped, files=2, bytes_=len(big) + 100, skipped=0)
    fake = FakeAfc(files, drop_after_first_chunk={"/DCIM/100APPLE/VID_0001.MOV"})

    def go():
        try:
            use_afc(fake, lambda: asyncio.run(
                afc_media.backup_media_via_afc(None, dropped)))
            return False
        except NuggetException:
            return True
    check("a dropped connection raises", go())
    check("store left UNVERIFIED after a failed pull",
          not media_store_verified(dropped), describe_media_store(dropped))

    warned = store("warned")
    os.makedirs(warned)
    tally = use_afc(FakeAfc(files, short_reads=set(files)), lambda: asyncio.run(
        afc_media.backup_media_via_afc(None, warned, on_error="warn")))
    check("stubbed reads are reported as failed", bool(tally.get("failed")))
    check("store with holes is NOT verified", not media_store_verified(warned))


def _has_part(root):
    for _d, _dirs, names in os.walk(root):
        for n in names:
            if n.endswith(".part"):
                return True
    return False


# =============================================================================
def test_pull_one():
    print("\n_pull_one: a truncated file never survives")
    small = os.path.join(TMP, "small.jpg")
    asyncio.run(_pull_one(FakeAfc({"/DCIM/IMG.JPG": b"k" * 2048}),
                          "/DCIM/IMG.JPG", small, {"st_size": 2048}))
    check("small file written intact", os.path.getsize(small) == 2048)

    trunc_dst = os.path.join(TMP, "trunc.mov")
    trunc = FakeAfc({"/DCIM/S.MOV": b"m" * (8 * 1024 * 1024)},
                    short_reads={"/DCIM/S.MOV"})
    check("stubbed streaming read raises", _raises(
        trunc, "/DCIM/S.MOV", trunc_dst, {"st_size": 8 * 1024 * 1024}))
    check("no truncated file left on disk", not os.path.exists(trunc_dst))
    check("no .part left behind", not os.path.exists(trunc_dst + ".part"))

    # a one-shot read that comes back smaller than st_size
    lie_dst = os.path.join(TMP, "lie.jpg")
    lie = FakeAfc({"/DCIM/L.JPG": b"l" * 512})
    check("size mismatch raises", _raises(
        lie, "/DCIM/L.JPG", lie_dst, {"st_size": 4096}))
    check("no partial file for the short-read case", not os.path.exists(lie_dst))


def _raises(fake, remote, dst, stat):
    try:
        asyncio.run(_pull_one(fake, remote, dst, stat))
        return False
    except NuggetException:
        return True


# =============================================================================
def test_push():
    print("\nrestore_media_via_afc: no host bookkeeping, short writes caught")
    src = store("pushed")
    write_file(os.path.join(src, "DCIM", "IMG.JPG"), b"p" * 500)
    write_file(os.path.join(src, "DCIM", "VID.MOV"), b"v" * (6 * 1024 * 1024))
    _write_media_state(src, files=2, bytes_=500 + 6 * 1024 * 1024, skipped=0)

    fake = FakeAfc()
    tally = use_afc(fake, lambda: asyncio.run(
        afc_media.restore_media_via_afc(None, src)))
    check("both media files pushed", tally["files"] == 2, tally)
    check("marker NOT pushed to the device",
          not any(MEDIA_STATE_FILE in k for k in fake.written), list(fake.written))
    check("no .part pushed", not any(k.endswith(".part") for k in fake.written))
    check("small file pushed intact",
          fake.written["/DCIM/IMG.JPG"] == b"p" * 500)

    class TruncatingAfc(FakeAfc):
        async def fwrite(self, handle, chunk):
            self.written[handle] = (self.written.get(handle, b"")
                                    + chunk[:len(chunk) // 2])

    short = store("shortpush")
    write_file(os.path.join(short, "DCIM", "BIG.MOV"), b"b" * (6 * 1024 * 1024))

    def go():
        try:
            use_afc(TruncatingAfc(), lambda: asyncio.run(
                afc_media.restore_media_via_afc(None, short)))
            return False
        except NuggetException as e:
            return "written short" in str(e)
    check("short write on the device is detected", go())


# =============================================================================
def test_pre_apply_gate():
    print("\nDeviceManager._require_media_copy: refuse before the wipe")
    good = store("gate_good")
    write_file(os.path.join(good, "DCIM", "a.MOV"), b"g" * 100)
    _write_media_state(good, files=1, bytes_=100, skipped=0)
    partial = store("gate_partial")
    write_file(os.path.join(partial, "DCIM", "a.MOV"), b"g" * 10)

    class Prepared:
        def __init__(self, media_src):
            self.media_src = media_src

    def gate(media_src):
        try:
            DeviceManager._require_media_copy(Prepared(media_src))
            return "ok"
        except NuggetException as e:
            return str(e)

    check("no media_src passes (media rides the backup)", gate("") == "ok")
    check("verified store passes", gate(good) == "ok")
    msg = gate(partial)
    check("partial store ABORTS", msg != "ok")
    check("abort message names the risk",
          "wipe" in msg and "no complete copy" in msg, msg[:80])
    check("missing store ABORTS", gate(store("gate_nope")) != "ok")
    check("a wiped apply cannot be started by a pre-wipe failure",
          "before anything was changed" in msg)


# =============================================================================
def test_restore_call_sites():
    print("\nrestore/worker call sites no longer fail silently")
    src = inspect.getsource(restore_mod)
    check("prune gate uses the marker, not a directory listing",
          "exclude_afc_media_trees=media_store_verified(media_dir)" in src)
    check("Phase 5 raises on a missing/empty media store",
          "is missing or empty" in src)
    check("Phase 5 raises when some files failed to push",
          "AFC media restore failed for" in src)
    check("Phase 5 logs what it is about to push",
          "describe_media_store(media_dir)" in src)

    worker = inspect.getsource(
        sys.modules["src.gui.thread_workers.apply_worker"])
    check("apply_worker prunes with the verified marker",
          "media_has_content = media_store_verified(media_dir)" in worker)
    check("apply_worker aborts on a failed media push",
          "AFC media restore failed for" in worker)


def main():
    test_marker_gate()
    test_pull()
    test_pull_one()
    test_push()
    test_pre_apply_gate()
    test_restore_call_sites()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\nALL {PASS} CHECKS PASSED")


if __name__ == "__main__":
    import src.gui.thread_workers.apply_worker  # noqa: F401  (for the source check)
    main()
