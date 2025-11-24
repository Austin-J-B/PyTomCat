#!/usr/bin/env python3
"""Downloads and converts the DeBERTa model to ONNX format."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Use PyTorch standard imports
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

ROOT = Path(__file__).resolve().parent.parent
WEIGHTS_DIR = ROOT / "weights"
ONNX_PATH = WEIGHTS_DIR / "deberta-v3-small-mnli.onnx"
TOKENIZER_PATH = WEIGHTS_DIR / "deberta-v3-small-mnli.tokenizer.json"
MODEL_NAME = "microsoft/deberta-v3-small"


def convert_model() -> None:
    if ONNX_PATH.exists() and TOKENIZER_PATH.exists():
        print("ONNX model already exists.")
        return

    print(f"Downloading {MODEL_NAME} model...")
    
    # 1. Load Model & Tokenizer
    try:
        tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
        model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME)
    except Exception as e:
        print(f"Error downloading model: {e}")
        sys.exit(1)

    model.eval()

    # 2. Create Dummy Input
    text = "This is a test sentence."
    inputs = tokenizer(text, return_tensors="pt")
    input_ids = inputs["input_ids"]
    attention_mask = inputs["attention_mask"]

    # 3. Export to ONNX
    print("Exporting to ONNX (Opset 14)...")
    WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    
    with torch.no_grad():
        torch.onnx.export(
            model,
            (input_ids, attention_mask),
            str(ONNX_PATH),
            input_names=["input_ids", "attention_mask"],
            output_names=["logits"],
            dynamic_axes={
                "input_ids": {0: "batch_size", 1: "sequence_length"},
                "attention_mask": {0: "batch_size", 1: "sequence_length"},
                "logits": {0: "batch_size"},
            },
            # Opset 14 is the stability sweet spot for DeBERTa
            opset_version=14, 
            do_constant_folding=True,
        )

    # 4. Save Tokenizer
    print("Saving tokenizer...")
    tokenizer.save_pretrained(WEIGHTS_DIR)
    
    # Copy JSON to the specific path expected by Tomcat
    src_json = WEIGHTS_DIR / "tokenizer.json"
    if src_json.exists():
        shutil.copy(src_json, TOKENIZER_PATH)
        print(f"Saved tokenizer to {TOKENIZER_PATH}")
    
    print(f"✅ Model converted successfully: {ONNX_PATH}")


if __name__ == "__main__":
    convert_model()