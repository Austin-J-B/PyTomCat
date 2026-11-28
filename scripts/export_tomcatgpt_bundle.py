"""Write the TomCatGPT bundle (tomcat/lm/engine.py) from the local research checkpoints.

The bundle holds weights trained on member messages and the private blocklist: it stays under the
git-ignored cache/ folder and is copied to the server directly, never committed.

    python scripts/export_tomcatgpt_bundle.py --research-root ../PyTomCat --out cache/tomcatgpt
    python scripts/export_tomcatgpt_bundle.py --nerman --research-root ../PyTomCat --out cache/tomcatgpt_nerman

--nerman (the dashboard's "Nerman int8", 2026-11-28): experiments/nerman_bo16/model.pt (Nerman stage 2 trained
on its own best-of-16 replies picked by the 1.7B judge) with every Linear layer dynamic int8 except the
feed-forward down projections (scripts/lm_nerman_quant.py "mixed"), stored as plain int8 values, per-channel
scales and zero points so the engine loads them with weights_only; nerman_picker/picker_bo16.pt as the picker
(a head on the chat model's own layers, lm_nerman_picker.py), and nerman/tokenizer_nerman.json.

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

import sys

import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Linear layers made int8 in the "mixed" model: all but the feed-forward down projection, whose input carries
# the outlier channels (lm_nerman_quant.GROUPS minus ff_out).
INT8_LEAVES = ("q", "k", "v", "proj", "head_gate", "gate", "up", "embed_in", "embed_out")
NERMAN_DEFAULTS = {"model": "cache/tomcat_lm/experiments/nerman_bo16/model.pt",
                   "tokenizer": "cache/tomcat_lm/nerman/tokenizer_nerman.json"}


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


def int8_parts(config: dict, state: dict, pad: int):
    """(fp32 state without the int8 layers' weights, {name: {"int8", "scales", "zero_points", "bias"?}}): the
    engine's model with torch's dynamic per-channel int8 on the INT8_LEAVES Linear layers, as
    lm_nerman_quant.int8_model(.., "mixed") builds it, unpacked into plain tensors."""
    from torch.ao.quantization import per_channel_dynamic_qconfig, quantize_dynamic
    from tomcat.lm.engine import build_model
    model = build_model(config, False, state, pad)
    names = [n for n, m in model.named_modules()
             if isinstance(m, torch.nn.Linear) and n.rsplit(".", 1)[-1] in INT8_LEAVES]
    quantize_dynamic(model, {n: per_channel_dynamic_qconfig for n in names}, dtype=torch.qint8, inplace=True)
    int8 = {}
    for name in names:
        layer = model.get_submodule(name)
        weight, bias = layer.weight(), layer.bias()
        if weight.q_per_channel_axis() != 0:
            raise SystemExit(f"{name}: unexpected quantization axis")
        int8[name] = {"int8": weight.int_repr().contiguous(),
                      "scales": weight.q_per_channel_scales().contiguous(),
                      "zero_points": weight.q_per_channel_zero_points().contiguous()}
        if bias is not None:
            int8[name]["bias"] = bias.detach().clone()
    keep = {k: v for k, v in state.items() if k.rpartition(".")[0] not in int8}
    return keep, int8


def export_picker(path: Path, out: Path, model_path: Path, root: Path) -> None:
    saved = torch.load(path, map_location="cpu", weights_only=False)
    if (root / saved["checkpoint"]).resolve() != model_path.resolve():
        raise SystemExit(f"the picker reads {saved['checkpoint']}, not the chat model")
    flags = list(saved["flags"])
    torch.save({"dim": saved["dim"], "hidden": saved["hidden"], "state_dict": saved["state_dict"],
                "layers": list(saved["layers"]), "flags": flags,
                "flag_cutoffs": [float(saved["flag_cutoffs"][f]["cutoff"]) for f in flags],
                "score_sd": float(saved["score_sd"]), "max_tokens": 80}, out / "picker.pt")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--research-root", type=Path, default=Path("."))
    ap.add_argument("--model", default="cache/tomcat_lm/experiments/muon_u1_bo16n/model.pt")
    ap.add_argument("--judge", default="cache/tomcat_lm/reply_judge/judge_mk_v3.pt")
    ap.add_argument("--tokenizer", default="cache/tomcat_lm/corpus_v2/tokenizer_6000.json")
    ap.add_argument("--blocklist", default="cache/tomcat_lm/blocklist")
    ap.add_argument("--name-lexicon", default="cache/tomcat_lm/names/name_lexicon.json")
    ap.add_argument("--out", type=Path, default=Path("cache/tomcatgpt"))
    ap.add_argument("--nerman", action="store_true", help="Nerman int8 + its picker (see above)")
    ap.add_argument("--picker", default="cache/tomcat_lm/nerman_picker/picker_bo16.pt")
    args = ap.parse_args()
    if args.nerman:
        for key, value in NERMAN_DEFAULTS.items():
            if getattr(args, key) == ap.get_default(key):
                setattr(args, key, value)
    root = args.research_root.resolve()
    model_path, judge_path = root / args.model, root / args.judge
    tok_path, block_dir = root / args.tokenizer, root / args.blocklist
    out = args.out
    (out / "blocklist").mkdir(parents=True, exist_ok=True)

    saved, config, untied = model_parts(model_path)
    expected = (saved.get("input_sha256") or {}).get("tokenizer")
    if expected and sha256(tok_path) != expected:
        raise SystemExit("tokenizer does not match the chat model's training tokenizer")
    if args.nerman:
        if untied:
            raise SystemExit("Nerman bundles expect tied output embeddings")
        vocab = json.loads(tok_path.read_text(encoding="utf-8"))["vocab"]
        state, int8 = int8_parts(config, saved["state_dict"], vocab.index("<pad>"))
        torch.save({"config": config, "untied": False, "state_dict": state, "int8": int8, "int8_mode": "mixed",
                    "step": saved.get("step")}, out / "generator.pt")
        export_picker(root / args.picker, out, model_path, root)
        (out / "judge.pt").unlink(missing_ok=True)
        sources = {"model": args.model, "picker": args.picker, "int8": "mixed", "tokenizer": args.tokenizer}
    else:
        torch.save({"config": config, "untied": untied, "state_dict": saved["state_dict"],
                    "step": saved.get("step")}, out / "generator.pt")
        base_path = export_judge(root, judge_path, out)
        (out / "picker.pt").unlink(missing_ok=True)
        sources = {"model": args.model, "judge": args.judge, "judge_base": base_path.name,
                   "judge_base_run": base_path.parent.name, "tokenizer": args.tokenizer}
    write_rest(args, root, tok_path, block_dir, out, saved, sources)


def export_judge(root: Path, judge_path: Path, out: Path) -> Path:
    judge = torch.load(judge_path, map_location="cpu", weights_only=False)
    base_path = Path(judge["checkpoint"])
    if not base_path.is_file():  # saved as an absolute path on the training machine
        base_path = root / "cache" / "tomcat_lm" / "experiments" / base_path.parent.name / base_path.name
    base, base_config, base_untied = model_parts(base_path)
    torch.save({"config": base_config, "untied": base_untied, "lm": judge["lm"], "head": judge["head"],
                "hidden": judge["hidden"], "classes": judge.get("classes", 4), "feats": bool(judge.get("feats")),
                "ref": base["state_dict"], "ref_untied": base_untied}, out / "judge.pt")
    return base_path


def write_rest(args, root: Path, tok_path: Path, block_dir: Path, out: Path, saved: dict, sources: dict) -> None:
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
    files = ["generator.pt", "picker.pt" if args.nerman else "judge.pt", "tokenizer.json"]
    files += ["person_names.json"] if lexicon.is_file() else []
    files += [
        f"blocklist/{p.name}" for p in sorted((out / "blocklist").glob("*.json"))]
    manifest = {"created": datetime.now().isoformat(timespec="seconds"), "sources": sources,
                "model_step": saved.get("step"),
                "sha256": {name: sha256(out / name) for name in files}}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
