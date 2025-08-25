# tomcat/nlp/model.py
from __future__ import annotations
from typing import Optional, Tuple, List

# Optional tiny ONNX wrapper. If onnxruntime or model path missing, returns None.

class NLPModel:
    def __init__(self, session, intent_labels: list[str]):
        self.session = session
        self.intent_labels = intent_labels

    @staticmethod
    def maybe_load(settings) -> Optional["NLPModel"]:
        try:
            import onnxruntime as ort  # type: ignore
        except Exception:
            return None
        model_path = getattr(settings, "nlp_model_path", None)
        if not model_path:
            return None
        try:
            sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
            # intent labels baked into your model metadata; else hardcode
            labels = ["none","show_photo","who_is","cv_identify","feed_update","sub_request","sub_accept"]
            return NLPModel(sess, labels)
        except Exception:
            return None

    # Stubs that you can wire when you have a real tokenizer and inputs
    def predict_intent(self, text: str) -> Tuple[str, float]:
        # TODO: tokenize text -> run session -> softmax -> label
        return ("none", 0.0)

    def score_entity(self, text: str, vocab: List[str]) -> Tuple[str, float]:
        # TODO: encode (text, candidate) pairs -> score -> best
        return ("", 0.0)
