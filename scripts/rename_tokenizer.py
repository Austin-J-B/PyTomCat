from pathlib import Path

weights_dir = Path("weights")
source = weights_dir / "tokenizer.json"
target = weights_dir / "deberta-v3-small-mnli.tokenizer.json"

if source.exists():
    source.rename(target)
    print(f"Renamed {source} to {target}")