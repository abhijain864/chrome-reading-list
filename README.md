# chrome-reading-list

Export your **Google Chrome Reading List** (and bookmarks) to clean Markdown +
JSON, straight from the local profile files — **no extension, no API, no
`pip install`.**

Chrome has no server-side / OAuth API for the Reading List. The only official
programmatic interface is the in-browser `chrome.readingList` extension API.
But the data also lives on disk, so this script reads it directly.

## Why this exists

- **Bookmarks** are easy: Chrome keeps them in a plain-text JSON file
  (`<profile>/Bookmarks`).
- **Reading List** is not: it's stored inside a **Snappy-compressed LevelDB**
  (`<profile>/Sync Data/LevelDB/`), under keys like `reading_list-dt-<url>`,
  with each entry serialized as a `ReadingListSpecifics` protobuf.

This tool unpacks that LevelDB and protobuf with **only the Python standard
library** — it ships a tiny pure-Python Snappy decoder and a minimal LevelDB
SSTable reader, so there's nothing to install.

## Usage

```bash
python extract.py                 # all profiles, default Chrome location
python extract.py --bookmarks     # also export bookmarks
python extract.py --user-data DIR # custom "User Data" folder
python extract.py --quiet         # don't print the per-entry listing
```

Output is written to `./output/`:

| File | Contents |
|------|----------|
| `reading_list.md`   | Markdown, newest first, with the date each item was added |
| `reading_list.json` | structured: `title`, `url`, `status`, `created`, `updated`, `profile` |
| `bookmarks.md` / `.json` | only with `--bookmarks`, grouped by folder |

It auto-discovers every profile (`Default`, `Profile 1`, …) and works while
Chrome is **running** — the locked LevelDB is copied before reading.

## How it works

1. **Locate profiles** under Chrome's `User Data` dir (Windows / macOS / Linux).
2. **Copy** each profile's `Sync Data/LevelDB/*.ldb` to a temp dir (the live DB
   is locked while Chrome runs).
3. **Read the SSTable**: footer → index block → data blocks, decompressing each
   Snappy-compressed block with the built-in decoder.
4. **Filter** keys beginning `reading_list-dt-` and parse the
   `ReadingListSpecifics` protobuf (`entry_id`, `title`, `url`,
   `creation_time_us`, `update_time_us`, `status`).
5. **Bookmarks**: just parse the `Bookmarks` JSON tree.

### Notes / limitations

- **Timestamps** are microseconds since the **Unix epoch** (not the Windows
  1601 epoch that some other Chrome data uses).
- **`status`** (UNSEEN / UNREAD / READ) is best-effort — the field number can
  vary between Chrome versions, so treat read-state as unreliable; titles,
  URLs and dates are solid.
- Deleted entries that linger as sync **tombstones** are skipped.
- Reading List data only appears for profiles where Chrome Sync stored it.

## Requirements

Python 3.8+. No third-party packages.

## License

MIT
