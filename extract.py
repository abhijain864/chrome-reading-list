#!/usr/bin/env python3
"""
chrome-reading-list — extract Google Chrome's Reading List (and bookmarks) to
Markdown + JSON, straight from the local profile files. No extension, no API.

Chrome stores the Reading List in a *Snappy-compressed LevelDB* under each
profile's ``Sync Data/LevelDB`` directory (keys look like
``reading_list-dt-<url>``). Bookmarks live in a plain ``Bookmarks`` JSON file.

This script:
  * auto-discovers every Chrome profile on the machine,
  * safely copies the (possibly locked) LevelDB while Chrome is running,
  * decompresses the SSTable data blocks with a built-in, pure-Python Snappy
    decoder (so there are NO third-party dependencies),
  * parses the ``ReadingListSpecifics`` protobuf for title / url / timestamps,
  * also reads the Bookmarks JSON,
  * writes ``output/reading_list.{md,json}`` and ``output/bookmarks.{md,json}``.

Usage:
    python extract.py                 # all profiles, default Chrome location
    python extract.py --user-data DIR # point at a custom "User Data" folder
    python extract.py --bookmarks     # also export bookmarks
    python extract.py --quiet         # no per-entry console output

Tested on Windows; profile auto-discovery also handles macOS/Linux paths.
"""
from __future__ import annotations
import argparse, datetime, glob, json, os, shutil, struct, sys, tempfile

# --------------------------------------------------------------------------- #
#  Pure-Python Snappy raw-block decompressor (no dependencies)
# --------------------------------------------------------------------------- #
def snappy_decompress(data: bytes) -> bytes:
    """Decode a raw Snappy block (the format LevelDB uses for compressed blocks)."""
    # preamble: uncompressed length as a varint
    length = 0; shift = 0; i = 0
    while True:
        b = data[i]; i += 1
        length |= (b & 0x7f) << shift
        if not (b & 0x80):
            break
        shift += 7
    out = bytearray()
    n = len(data)
    while i < n:
        tag = data[i]; i += 1
        kind = tag & 0x03
        if kind == 0:                                   # literal
            ln = tag >> 2
            if ln >= 60:
                nbytes = ln - 59
                ln = int.from_bytes(data[i:i + nbytes], "little"); i += nbytes
            ln += 1
            out += data[i:i + ln]; i += ln
        else:                                           # copy
            if kind == 1:
                ln = ((tag >> 2) & 0x07) + 4
                offset = ((tag >> 5) << 8) | data[i]; i += 1
            elif kind == 2:
                ln = (tag >> 2) + 1
                offset = int.from_bytes(data[i:i + 2], "little"); i += 2
            else:                                       # kind == 3
                ln = (tag >> 2) + 1
                offset = int.from_bytes(data[i:i + 4], "little"); i += 4
            start = len(out) - offset
            for j in range(ln):                         # byte-wise: copies may overlap
                out.append(out[start + j])
    return bytes(out)


# --------------------------------------------------------------------------- #
#  Minimal LevelDB SSTable reader
# --------------------------------------------------------------------------- #
def _uvarint(b: bytes, i: int):
    shift = 0; val = 0
    while True:
        c = b[i]; i += 1
        val |= (c & 0x7f) << shift
        if not (c & 0x80):
            return val, i
        shift += 7


def _read_block(data: bytes, off: int, size: int) -> bytes:
    raw = data[off:off + size]
    comp = data[off + size]                # 1 compression byte (+ 4-byte CRC follows)
    return snappy_decompress(raw) if comp == 1 else raw


def _parse_block(blk: bytes):
    n = len(blk)
    nrest = struct.unpack("<I", blk[-4:])[0]
    end = n - 4 * (nrest + 1)
    i = 0; key = b""; out = []
    while i < end:
        shared, i = _uvarint(blk, i)
        nonshared, i = _uvarint(blk, i)
        vlen, i = _uvarint(blk, i)
        key = key[:shared] + blk[i:i + nonshared]; i += nonshared
        val = blk[i:i + vlen]; i += vlen
        out.append((key, val))
    return out


def sstable_entries(path: str):
    """Yield (key, value) pairs from a LevelDB .ldb SSTable file."""
    data = open(path, "rb").read()
    footer = data[-48:]
    _mo, i = _uvarint(footer, 0)           # metaindex handle (skipped)
    _ms, i = _uvarint(footer, i)
    io, i = _uvarint(footer, i)            # index handle
    is_, i = _uvarint(footer, i)
    for k, v in _parse_block(_read_block(data, io, is_)):
        j = 0
        bo, j = _uvarint(v, j); bs, j = _uvarint(v, j)
        try:
            for entry in _parse_block(_read_block(data, bo, bs)):
                yield entry
        except Exception:
            continue


# --------------------------------------------------------------------------- #
#  ReadingListSpecifics protobuf
#  fields: 1 entry_id, 2 title, 3 url, 4 creation_us, 5 update_us, 7 status
# --------------------------------------------------------------------------- #
_STATUS = {0: "UNSEEN", 1: "UNREAD", 2: "READ"}


def _us_to_iso(us: int):
    try:  # Reading List timestamps are microseconds since the Unix epoch
        return (datetime.datetime(1970, 1, 1) +
                datetime.timedelta(microseconds=us)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def _parse_specifics(v: bytes):
    n = len(v)

    def parse_from(start):
        r = {}; i = start
        while i < n:
            tag = v[i]; field = tag >> 3; wt = tag & 7; i += 1
            if wt == 2:
                ln, i = _uvarint(v, i); s = v[i:i + ln]; i += ln
                if field == 1:
                    r["entry_id"] = s.decode("latin1", "replace")
                elif field == 2:
                    r["title"] = s.decode("utf-8", "replace")
                elif field == 3:
                    r["url"] = s.decode("latin1", "replace")
                else:
                    break
            elif wt == 0:
                val, i = _uvarint(v, i)
                if field == 4:
                    r["created"] = _us_to_iso(val)
                elif field == 5:
                    r["updated"] = _us_to_iso(val)
                elif field == 7:
                    r["status"] = _STATUS.get(val, val)
                elif field > 9:
                    break
            else:
                break
        return r

    # locate the start of the submessage: first length-prefixed http string
    for i in range(n - 1):
        if v[i] == 0x0A:
            ln, j = _uvarint(v, i + 1)
            if 4 < ln < 4000 and v[j:j + 4] == b"http":
                rec = parse_from(i)
                if rec.get("title") or rec.get("url"):
                    return rec
    return None


# --------------------------------------------------------------------------- #
#  Profile discovery
# --------------------------------------------------------------------------- #
def default_user_data_dir():
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Google", "Chrome", "User Data"),                  # Windows
        os.path.join(home, "Library", "Application Support",
                     "Google", "Chrome"),                              # macOS
        os.path.join(home, ".config", "google-chrome"),                # Linux
    ]
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return candidates[0]


def find_profiles(user_data: str):
    profiles = []
    for name in os.listdir(user_data):
        p = os.path.join(user_data, name)
        if name == "Default" or name.startswith("Profile "):
            if os.path.isdir(p):
                profiles.append((name, p))
    return sorted(profiles)


# --------------------------------------------------------------------------- #
#  Extractors
# --------------------------------------------------------------------------- #
def extract_reading_list(profile_dir: str):
    src = os.path.join(profile_dir, "Sync Data", "LevelDB")
    if not os.path.isdir(src):
        return []
    tmp = tempfile.mkdtemp(prefix="crl_")
    try:
        for f in glob.glob(os.path.join(src, "*.ldb")):  # work on a copy (DB may be locked)
            try:
                shutil.copy2(f, tmp)
            except Exception:
                pass
        entries = {}
        for f in glob.glob(os.path.join(tmp, "*.ldb")):
            try:
                pairs = list(sstable_entries(f))
            except Exception:
                continue
            for k, val in pairs:
                if k.startswith(b"reading_list-dt-"):
                    url = k[len(b"reading_list-dt-"):].decode("latin1", "replace")
                    rec = _parse_specifics(val)
                    if rec is None:                      # tombstone / deleted
                        continue
                    rec.setdefault("url", url)
                    if url not in entries or len(rec) >= len(entries[url]):
                        entries[url] = rec
        return [r for r in entries.values() if r.get("title")]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def extract_bookmarks(profile_dir: str):
    path = os.path.join(profile_dir, "Bookmarks")
    if not os.path.isfile(path):
        return []
    data = json.load(open(path, encoding="utf-8"))
    out = []

    def walk(node, folder):
        for c in node.get("children", []):
            if c.get("type") == "folder":
                walk(c, folder + " / " + c.get("name", ""))
            elif c.get("type") == "url":
                out.append({"folder": folder, "title": c.get("name"), "url": c.get("url")})

    roots = data.get("roots", {})
    for key in ("bookmark_bar", "other", "synced"):
        if key in roots:
            walk(roots[key], roots[key].get("name", key))
    return out


# --------------------------------------------------------------------------- #
#  Writers
# --------------------------------------------------------------------------- #
def write_reading_list(rows, outdir):
    rows = sorted(rows, key=lambda r: r.get("created") or "")
    json.dump(rows, open(os.path.join(outdir, "reading_list.json"), "w",
              encoding="utf-8"), indent=2, ensure_ascii=False)
    with open(os.path.join(outdir, "reading_list.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# Chrome Reading List ({len(rows)} entries)\n\n")
        for r in reversed(rows):  # newest first
            line = f"- [{r.get('title')}]({r.get('url')})"
            if r.get("created"):
                line += f"  _(added {r['created']})_"
            fh.write(line + "\n")


def write_bookmarks(rows, outdir):
    json.dump(rows, open(os.path.join(outdir, "bookmarks.json"), "w",
              encoding="utf-8"), indent=2, ensure_ascii=False)
    by_folder = {}
    for r in rows:
        by_folder.setdefault(r["folder"], []).append(r)
    with open(os.path.join(outdir, "bookmarks.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# Chrome Bookmarks ({len(rows)} entries)\n\n")
        for folder in sorted(by_folder):
            fh.write(f"## {folder}\n\n")
            for r in by_folder[folder]:
                fh.write(f"- [{r.get('title')}]({r.get('url')})\n")
            fh.write("\n")


# --------------------------------------------------------------------------- #
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Extract Chrome Reading List + bookmarks.")
    ap.add_argument("--user-data", help="Path to Chrome 'User Data' directory")
    ap.add_argument("--bookmarks", action="store_true", help="Also export bookmarks")
    ap.add_argument("--outdir", default="output", help="Output directory (default: ./output)")
    ap.add_argument("--quiet", action="store_true", help="Suppress per-entry listing")
    args = ap.parse_args()

    user_data = args.user_data or default_user_data_dir()
    if not os.path.isdir(user_data):
        sys.exit(f"Chrome User Data directory not found: {user_data}")
    os.makedirs(args.outdir, exist_ok=True)

    profiles = find_profiles(user_data)
    print(f"Chrome User Data: {user_data}")
    print(f"Profiles found:   {', '.join(n for n, _ in profiles) or '(none)'}\n")

    all_rl, all_bm = [], []
    for name, pdir in profiles:
        rl = extract_reading_list(pdir)
        for r in rl:
            r["profile"] = name
        all_rl += rl
        note = ""
        if args.bookmarks:
            bm = extract_bookmarks(pdir)
            for r in bm:
                r["profile"] = name
            all_bm += bm
            note = f", {len(bm)} bookmarks"
        print(f"  [{name}] {len(rl)} reading-list entries{note}")

    write_reading_list(all_rl, args.outdir)
    if args.bookmarks:
        write_bookmarks(all_bm, args.outdir)

    print(f"\nTotal: {len(all_rl)} reading-list entries"
          + (f", {len(all_bm)} bookmarks" if args.bookmarks else ""))
    print(f"Wrote: {os.path.abspath(args.outdir)}\\reading_list.md (+ .json)"
          + (", bookmarks.md (+ .json)" if args.bookmarks else ""))

    if not args.quiet and all_rl:
        print("\nMost recent 15:")
        for r in sorted(all_rl, key=lambda x: x.get("created") or "", reverse=True)[:15]:
            print(f"  {r.get('created','?')[:10]}  {str(r.get('title',''))[:70]}")


if __name__ == "__main__":
    main()
