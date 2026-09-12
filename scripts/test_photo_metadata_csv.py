"""Photo metadata CSV: one parse, and the shapes it has to survive.

"TomCatBot Pics.csv" is the record of every photo the bot has ever stored --
twelve thousand rows and four megabytes, and five consumers read all of it
(the labeler's item context, the gallery, the Catabase sync, cat photo counts,
and the sheet mirror). It is parsed with a plain csv.reader that places each
column once from the header row rather than per row.

Two things have to keep holding: the rows a DictReader would have produced, and
the same tolerance for files that do not match the current header exactly --
written by an older version, edited by hand, or truncated.

Run: python scripts/test_photo_metadata_csv.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tomcat.services import local_photos

H = local_photos.CSV_HEADERS
FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def parse(text: str) -> List[dict]:
    """Parse CSV text the way the metadata reader does."""
    path = Path(tempfile.mkdtemp(prefix="photocsv")) / "meta.csv"
    path.write_text(text, encoding="utf-8", newline="")
    return local_photos._read_metadata_rows_locked(path)


def cells(text: str) -> List[List[str]]:
    path = Path(tempfile.mkdtemp(prefix="photocsv")) / "meta.csv"
    path.write_text(text, encoding="utf-8", newline="")
    return local_photos._read_metadata_cells_locked(path)


def line(*values: str) -> str:
    return ",".join(values) + "\n"


def main() -> int:
    print("=" * 70)
    print("photo metadata CSV tests")
    print("=" * 70)

    canonical = line(*H)

    print("\n[1] an ordinary file")
    rows = parse(canonical + line("http://u/1", "2026-01-02T03:04:05", "7", "8", "9",
                                  "10", "1", "0 0 1 1", "3. Mittens", "aj", "hi"))
    check("one row", 1, len(rows))
    check("every header is a key", sorted(H), sorted(rows[0].keys()))
    check("a value lands under its own header", "1", rows[0]["Serial Number"])
    check("the last column", "hi", rows[0]["Comments"])

    print("\n[2] rows that do not have every column")
    rows = parse(canonical + line("http://u/1", "ts", "7"))
    check("the columns present", ("http://u/1", "ts", "7"),
          (rows[0]["Discord URL"], rows[0]["Timestamp"], rows[0]["Author ID"]))
    check("the rest are empty, not missing", "", rows[0]["Comments"])

    print("\n[3] columns beyond the header are dropped")
    rows = parse(canonical + line(*(["x"] * (len(H) + 3))))
    check("the row is header-width", len(H), len(rows[0]))
    check("still keyed by header", "x", rows[0]["Comments"])

    print("\n[4] a header in another order still reads correctly")
    #Rows written before a column was added, or a file somebody re-sorted.
    reordered = ["Serial Number", "Comments", "Discord URL"]
    rows = parse(line(*reordered) + line("42", "a note", "http://u/42"))
    check("serial", "42", rows[0]["Serial Number"])
    check("comment", "a note", rows[0]["Comments"])
    check("url", "http://u/42", rows[0]["Discord URL"])
    check("a column the file does not have", "", rows[0]["Box Cat IDs"])

    print("\n[5] empty and blank-line files")
    check("no header at all", [], parse(""))
    check("header only", [], parse(canonical))
    check("blank lines are skipped", 1,
          len(parse(canonical + "\n" + line(*(["y"] * len(H))) + "\n")))

    print("\n[6] quoting and embedded separators survive")
    rows = parse(canonical + '"http://u/1",ts,7,8,9,10,1,"0,0,1,1","a|b","aj","he said ""hi"""\n')
    check("a quoted comma stays one cell", "0,0,1,1", rows[0]["Box Coordinates"])
    check("escaped quotes", 'he said "hi"', rows[0]["Comments"])
    check("a pipe-joined label list is untouched", "a|b", rows[0]["Box Cat IDs"])

    print("\n[7] the table form is the cells with the header in front")
    text = canonical + line(*(["a"] * len(H))) + line(*(["b"] * len(H)))
    check("header row first", list(H), [list(H), *cells(text)][0])
    check("then one list per row", [["a"] * len(H), ["b"] * len(H)], cells(text))

    print("\n[8] the real file, if it is here")
    real = local_photos.metadata_csv_path()
    if real.exists():
        table = local_photos.read_metadata_table()
        rows = local_photos.read_metadata_rows()
        check("the table carries the header and every row", len(rows) + 1, len(table))
        check("the table's header", list(H), table[0])
        check("row and table agree", [rows[0][header] for header in H], table[1])
    else:
        print(f"  SKIP no {real}")

    print("\n[9] the manual-ref status payload reads module state only")
    from tomcat.handlers import labeler
    #Given a count it must not go looking for one: loading the catalog can pull
    #the CatDatabase sheet, and this runs while an HTTP request waits.
    real_load = labeler._load_profile_catalog

    def explode():
        raise AssertionError("the status payload loaded the profile catalog")

    labeler._load_profile_catalog = explode
    try:
        payload = labeler._manual_ref_cache_status_payload(total_hint=7)
        check("the count it was handed", 7, payload["cats"])
        check("and the total", 7, payload["total"])
        no_hint = labeler._manual_ref_cache_status_payload()
        check("with no count, what the caches hold",
              max(len(labeler._manual_metadata_ref_cache),
                  len(labeler._photo_crop_index_cache)),
              no_hint["cats"])
    finally:
        labeler._load_profile_catalog = real_load

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
