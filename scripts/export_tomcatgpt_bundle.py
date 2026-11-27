"""Write the TomCatGPT bundle (tomcat/lm/engine.py) from the local research checkpoints.

The bundle holds weights trained on member messages and the private blocklist: it stays under the
git-ignored cache/ folder and is copied to the server directly, never committed.

    python scripts/export_tomcatgpt_bundle.py --research-root ../PyTomCat --out cache/tomcatgpt

Defaults: MuonKahuna (experiments/muon_u1_bo16n/model.pt: best-of-16 picks by the 1.7B judge, plus a capped
push-down on the tries it flagged as contradicting, repeating or copying the chat) as the chat model, judge_mk_v3
as the reviewer, corpus_v2/tokenizer_6000.json, the blocklist folder, and person_names.json from the research name lexicon
(names/name_lexicon.json, scripts/lm_name_finder.py: the unambiguous people's names, for the engine's
NAME_REPEAT_PENALTY; skipped when the lexicon is missing).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import torch


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def model_parts(path: Path):
    saved = torch.load(path, map_location="cpu", weights_only=False)
    if "run_settings" in saved:
        config, untied = saved["run_settings"]["config"], saved["run_settings"]["untied"]
    else:
        config, untied = saved["config"], saved.get("output_embeddings") == "untied"
    if saved.get("reply_format"):
        raise SystemExit(f"{path} uses a reply format the engine does not support")
    return saved, dict(config), bool(untied)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--research-root", type=Path, default=Path("."))
    ap.add_argument("--model", default="cache/tomcat_lm/experiments/muon_u1_bo16n/model.pt")
    ap.add_argument("--judge", default="cache/tomcat_lm/reply_judge/judge_mk_v3.pt")
    ap.add_argument("--tokenizer", default="cache/tomcat_lm/corpus_v2/tokenizer_6000.json")
    ap.add_argument("--blocklist", default="cache/tomcat_lm/blocklist")
    ap.add_argument("--name-lexicon", default="cache/tomcat_lm/names/name_lexicon.json")
    ap.add_argument("--out", type=Path, default=Path("cache/tomcatgpt"))
    args = ap.parse_args()
    root = args.research_root.resolve()
    model_path, judge_path = root / args.model, root / args.judge
    tok_path, block_dir = root / args.tokenizer, root / args.blocklist
    out = args.out
    (out / "blocklist").mkdir(parents=True, exist_ok=True)

    saved, config, untied = model_parts(model_path)
    expected = (saved.get("input_sha256") or {}).get("tokenizer")
    if expected and sha256(tok_path) != expected:
        raise SystemExit("tokenizer does not match the chat model's training tokenizer")
    torch.save({"config": config, "untied": untied, "state_dict": saved["state_dict"], "step": saved.get("step")},
               out / "generator.pt")

    judge = torch.load(judge_path, map_location="cpu", weights_only=False)
    base_path = Path(judge["checkpoint"])
    if not base_path.is_file():  # saved as an absolute path on the training machine
        base_path = root / "cache" / "tomcat_lm" / "experiments" / base_path.parent.name / base_path.name
    base, base_config, base_untied = model_parts(base_path)
    torch.save({"config": base_config, "untied": base_untied, "lm": judge["lm"], "head": judge["head"],
                "hidden": judge["hidden"], "classes": judge.get("classes", 4), "feats": bool(judge.get("feats")),
                "ref": base["state_dict"], "ref_untied": base_untied}, out / "judge.pt")

    shutil.copyfile(tok_path, out / "tokenizer.json")
    for name in ("member_full_names.json", "slur_patterns.json"):
        if (block_dir / name).is_file():
            shutil.copyfile(block_dir / name, out / "blocklist" / name)
    # Name stems lm_sample.BLOCKED_NAMES keeps out of every reply (kept in the private research code, not here).
    sample_src = (root / "scripts" / "lm_sample.py").read_text(encoding="utf-8")
    stems = re.search(r'BLOCKED_NAMES = re\.compile\(r"\(\?<!\[\^\\W_\]\)([^"]+)"', sample_src)
    if not stems:
        raise SystemExit("could not read BLOCKED_NAMES from scripts/lm_sample.py")
    (out / "blocklist" / "name_stems.json").write_text(json.dumps(stems.group(1).split("|")), encoding="utf-8")
    lexicon = root / args.name_lexicon
    if lexicon.is_file():
        names = json.loads(lexicon.read_text(encoding="utf-8"))["names"]
        people = sorted(n for n, v in names.items() if not v["ambiguous"])  # never everyday words like "will"
        (out / "person_names.json").write_text(json.dumps(people), encoding="utf-8")
    files = ["generator.pt", "judge.pt", "tokenizer.json"]
    files += ["person_names.json"] if lexicon.is_file() else []
    files += [
        f"blocklist/{p.name}" for p in sorted((out / "blocklist").glob("*.json"))]
    manifest = {"created": datetime.now().isoformat(timespec="seconds"),
                "sources": {"model": args.model, "judge": args.judge, "judge_base": base_path.name,
                            "judge_base_run": base_path.parent.name, "tokenizer": args.tokenizer},
                "model_step": saved.get("step"),
                "sha256": {name: sha256(out / name) for name in files}}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
