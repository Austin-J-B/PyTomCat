"""Cat-database query regression: a parsed question in, a sentence out.

run_cat_query answers "how many cats are orange", "which cats live at west
hall", "who has the most photos" and so on against the CatDatabase table. Every
answer reports the filters it applied and phrases them the same way, which is
now built once per call rather than at each of the four exits — so this test
pins the counts, the name lists, and the wording.

Run: python scripts/test_cat_query.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tomcat.services import cat_query

HEADER = ["Full Name", "Location", "TNRd", "Physical Description",
          "Birthday Estimate", "Number of Pics", "Recently Seen?", "Last Seen Date"]
ROWS = [
    ["1. Microwave", "West Hall", "Yes", "orange tabby", "2019", "12", "Yes", "2026-09-01"],
    ["2. Twix", "HOP", "No", "black and white", "2021", "3", "No", "2024-01-01"],
    ["3. Eraser", "West Hall", "Yes", "tuxedo", "2019", "12", "Yes", "2026-08-01"],
    ["4. Rolo", "Lot 50", "", "brown", "2020", "1", "Yes", "2026-07-01"],
    ["5. Faye", "", "Yes", "grey", "", "7", "", ""],
]

#Serve a fixed five-cat table instead of the real CatDatabase snapshot.
cat_query._load_rows = lambda: (HEADER, list(ROWS))
cat_query._load_photo_counts = lambda: {}

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def ask(**query: Any) -> Dict[str, Any]:
    return cat_query.run_cat_query(query)


def main() -> int:
    print("=" * 70)
    print("cat database query regression tests")
    print("=" * 70)

    print("\n[1] counting")
    result = ask(op="count_all_cats")
    check("all cats counted", 5, result["count"])
    check("names withheld for a count", [], result["names"])
    result = ask(op="count_by_filters", location="West Hall")
    check("count at a station", 2, result["count"])
    check("filters echoed back", "West Hall", result["filters"]["location"])
    check("wording", "There are 2 cats at West Hall in the catabase.", result["message"])

    print("\n[2] listing names")
    result = ask(op="list_names_by_filters", location="West Hall")
    check("names at a station", ["Microwave", "Eraser"], result["names"])
    check("wording", "The cats at West Hall are: Microwave, Eraser.", result["message"])
    result = ask(op="list_names_by_filters", color_family="orange")
    check("by colour", ["Microwave"], result["names"])
    result = ask(op="list_names_by_filters", birth_year=2019)
    check("by birth year", ["Microwave", "Eraser"], result["names"])

    print("\n[3] filters only consider recently-seen cats unless told otherwise")
    #Twix is marked not recently seen and Faye's recency cell is blank, so
    #neither appears without an explicit scope. This default is what keeps
    #"which cats are orange" from listing cats nobody has seen in two years.
    check("TNR'd, default scope", ["Microwave", "Eraser"],
          ask(op="list_names_by_filters", tnrd=True)["names"])
    check("TNR'd, every cat", ["Microwave", "Eraser", "Faye"],
          ask(op="list_names_by_filters", tnrd=True, recent_scope="all")["names"])
    check("not TNR'd, default scope", ["Rolo"],
          ask(op="list_names_by_filters", tnrd=False)["names"])
    check("not TNR'd, every cat", ["Twix", "Rolo"],
          ask(op="list_names_by_filters", tnrd=False, recent_scope="all")["names"])

    print("\n[4] photo counts")
    check("at least seven, every cat", ["Microwave", "Eraser", "Faye"],
          ask(op="list_names_by_filters", photo_count_min=7, recent_scope="all")["names"])
    check("at most three, every cat", ["Twix", "Rolo"],
          ask(op="list_names_by_filters", photo_count_max=3, recent_scope="all")["names"])
    #A reversed range is a parse slip, not a request for nothing.
    check("a reversed range is swapped", ["Twix", "Rolo", "Faye"],
          ask(op="list_names_by_filters", photo_count_min=7, photo_count_max=1,
              recent_scope="all")["names"])

    print("\n[5] most and fewest photos, including ties")
    result = ask(op="list_names_by_filters", photo_count_extreme="max")
    check("tied for most", ["Microwave", "Eraser"], result["names"])
    check("tie wording",
          "The cats tied for the highest photo count (12) are: Microwave, Eraser.",
          result["message"])
    result = ask(op="list_names_by_filters", photo_count_extreme="min")
    check("fewest is a single cat", ["Rolo"], result["names"])
    check("single wording", "Rolo has the lowest photo count (1) in the catabase.",
          result["message"])
    result = ask(op="list_names_by_filters", photo_count_extreme="max", location="HOP")
    check("extreme within a filter", ["Twix"], result["names"])
    check("filter named in the wording",
          "Twix has the highest photo count (3) at HOP in the catabase.",
          result["message"])

    print("\n[6] recency scope")
    #Photo extremes look at every cat; other filters default to active ones.
    result = ask(op="list_names_by_filters", location="West Hall", recent_scope="inactive")
    check("inactive at a station", [], result["names"])
    result = ask(op="list_names_by_filters", tnrd=False, recent_scope="inactive")
    check("an inactive cat is found", ["Twix"], result["names"])
    result = ask(op="list_names_by_filters", recent_scope="all", photo_count_extreme="max")
    check("extremes span every cat", ["Microwave", "Eraser"], result["names"])

    print("\n[7] nothing matched, and nothing understood")
    result = ask(op="list_names_by_filters", location="Lot 50", color_family="orange")
    check("no matches", 0, result["count"])
    check("says so plainly", True, result["message"].startswith("I couldn't find any"))
    result = ask(op="list_names_by_filters", source_text="what about the thing")
    check("an unparsed question is admitted", "I wasn't able to understand that query.",
          result["message"])
    check("no filters claimed", {}, result["filters"])
    #No filters and no source text is a bare "how many cats" in disguise.
    result = ask(op="list_names_by_filters")
    check("empty filters fall back to a count", "count_all_cats", result["op"])

    print("\n[8] every answer reports the filters it applied")
    result = ask(op="list_names_by_filters", location="West Hall", tnrd=True,
                 color_family="tabby", birth_year=2019, photo_count_min=1,
                 photo_count_max=99, recent_scope="all")
    check("filters round-trip", {
        "location": "West Hall", "tnrd": True, "color_family": "tabby",
        "birth_year": 2019, "photo_count_min": 1, "photo_count_max": 99,
        "photo_count_extreme": None, "recent_scope": "all",
    }, result["filters"])
    #The dict must be a copy: callers log and mutate it.
    result["filters"]["location"] = "mutated"
    check("callers get their own dict", "West Hall",
          ask(op="list_names_by_filters", location="West Hall")["filters"]["location"])

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
