# This script will be used by setup.cmd to test the ONNX model installation
import onnxruntime as ort
from pathlib import Path
from tokenizers import Tokenizer

def test_model():
    """Test that the ONNX model and tokenizer are working properly."""
    print("\nTesting model installation...")
    
    weights_dir = Path("weights")
    model_path = weights_dir / "deberta-v3-small-mnli.onnx"
    tokenizer_path = weights_dir / "deberta-v3-small-mnli.tokenizer.json"
    
    # Test loading the model
    try:
        session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        print("✓ Successfully loaded ONNX model")
    except Exception as e:
        print(f"❌ Failed to load ONNX model: {e}")
        return False
    
    # Test loading the tokenizer
    try:
        tokenizer = Tokenizer.from_file(str(tokenizer_path))
        print("✓ Successfully loaded tokenizer")
    except Exception as e:
        print(f"❌ Failed to load tokenizer: {e}")
        return False
    
    # Test basic inference
    try:
        text = "This is a test sentence"
        inputs = tokenizer.encode(text)
        
        model_inputs = {
            "input_ids": [inputs.ids],
            "attention_mask": [inputs.attention_mask]
        }
        
        outputs = session.run(None, model_inputs)
        print("✓ Successfully ran inference")
        print("\nAll tests passed! The model and tokenizer are ready to use.")
        return True
    except Exception as e:
        print(f"❌ Failed to run inference: {e}")
        return False

if __name__ == "__main__":
    test_model()