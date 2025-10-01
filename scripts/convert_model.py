# This script will be used by setup.cmd to convert the DeBERTa model to ONNX format
from transformers import AutoModelForSequenceClassification, AutoTokenizer
from pathlib import Path
import torch
import shutil

def convert_model():
    """Download DeBERTa-v3-small model and convert to ONNX format."""
    print("Downloading DeBERTa-v3-small model...")
    
    # Create weights directory if it doesn't exist
    weights_dir = Path("weights")
    weights_dir.mkdir(exist_ok=True)
    
    # Download model and tokenizer
    model_name = "microsoft/deberta-v3-small"
    try:
        model = AutoModelForSequenceClassification.from_pretrained(
            model_name,
            use_safetensors=True,
        )
    except Exception as exc:
        fallback_reasons = (
            "dictionary update sequence element",
            "Can't load the model",
        )
        if not any(reason in str(exc) for reason in fallback_reasons):
            raise
        # Some Windows setups inject malformed HF headers, which breaks the
        # safetensors auto-conversion path. Fall back to the PyTorch weights.
        print("Safetensors load failed; retrying with standard PyTorch weights...")
        model = AutoModelForSequenceClassification.from_pretrained(
            model_name,
            use_safetensors=False,
        )
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    
    # Save tokenizer
    tokenizer.save_pretrained(weights_dir)
    
    # Prepare dummy input for ONNX export
    dummy_input = tokenizer("This is a test", return_tensors="pt")
    
    # Export to ONNX
    torch.onnx.export(
        model,
        (dummy_input["input_ids"], dummy_input["attention_mask"]),
        str(weights_dir / "deberta-v3-small-mnli.onnx"),
        input_names=["input_ids", "attention_mask"],
        output_names=["logits"],
        dynamic_axes={
            "input_ids": {0: "batch_size", 1: "sequence_length"},
            "attention_mask": {0: "batch_size", 1: "sequence_length"},
            "logits": {0: "batch_size"}
        },
        opset_version=12
    )
    
    # Rename tokenizer file
    tokenizer_path = weights_dir / "tokenizer.json"
    if tokenizer_path.exists():
        shutil.move(str(tokenizer_path), str(weights_dir / "deberta-v3-small-mnli.tokenizer.json"))
    
    # Clean up unnecessary files (will be handled by setup.cmd)
    print("Conversion complete! Files have been placed in the weights directory.")

if __name__ == "__main__":
    convert_model()
