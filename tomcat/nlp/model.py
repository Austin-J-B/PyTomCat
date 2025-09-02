from __future__ import annotations
from typing import Optional, Tuple, List, Dict, Any
import math

# Optional ONNX + Tokenizers wrapper (zero‑shot via MNLI style).
# If missing, router gracefully falls back to rules and returns None here.

_DEFAULT_LABELS = [
    "none",
    "show_photo",
    "who_is",
    "cv_identify",
    "feed_update",
    "sub_request",
    "sub_accept",
]


class NLPModel:
    def __init__(self, session, tokenizer, labels: List[str]):
        self.session = session
        self.tokenizer = tokenizer
        self.intent_labels = labels or _DEFAULT_LABELS

    @staticmethod
    def maybe_load(settings) -> Optional["NLPModel"]:
        model_path = getattr(settings, "nlp_model_path", None)
        tok_path = getattr(settings, "nlp_tokenizer_path", None)
        if not model_path or not tok_path:
            return None
        try:
            import onnxruntime as ort  # type: ignore
            from tokenizers import Tokenizer  # type: ignore
        except Exception:
            return None
        try:
            sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])  # type: ignore
            tok = Tokenizer.from_file(tok_path)  # type: ignore
            return NLPModel(sess, tok, _DEFAULT_LABELS)
        except Exception:
            return None

    # ---------- public API ----------
    def predict_intent(self, text: str) -> Tuple[str, float]:
        # Zero‑shot over our label set using MNLI: score entailment for each label hypothesis.
        labels = [
            ("show_photo", [
                "The user asks to show a cat photo.",
                "The user requests a photo of a cat.",
                "They want TomCat to send a cat picture.",
            ]),
            ("who_is", [
                "The user asks who a specific cat is.",
                "They want cat information or a profile.",
            ]),
            ("cv_identify", [
                "The user asks to identify a cat in a photo.",
                "They ask to classify the cat in the image.",
            ]),
            ("feed_update", [
                "The user says a feeding occurred or bowl was filled.",
            ]),
            ("sub_request", [
                "The user asks someone to cover a feeding shift.",
            ]),
            ("sub_accept", [
                "The user volunteers to cover a feeding shift.",
            ]),
        ]
        best_label = "none"; best_p = 0.0
        for label, hyps in labels:
            for hyp in hyps:
                p = self._mnli_entailment_prob(text, hyp)
                best_label, best_p = (label, p) if p > best_p else (best_label, best_p)
        return best_label, float(best_p)

    def score_entity(self, text: str, vocab: List[str]) -> Tuple[str, float]:
        best = ""; best_p = 0.0
        for cand in vocab:
            hyp = f"The message is about {cand}."
            p = self._mnli_entailment_prob(text, hyp)
            if p > best_p:
                best, best_p = cand, p
        return best, float(best_p)

    # Basic spam scorer via zero-shot: returns probability that text is spam
    def predict_spam(self, text: str) -> float:
        hyp = "This message is spam."
        return float(self._mnli_entailment_prob(text, hyp))

    # ---------- helpers ----------
    def _mnli_entailment_prob(self, premise: str, hypothesis: str) -> float:
        try:
            enc = self.tokenizer.encode(premise, hypothesis)  # type: ignore
            ids = enc.ids
            attn = enc.attention_mask if hasattr(enc, "attention_mask") else [1] * len(ids)
            import numpy as np  # type: ignore
            ort_inputs: Dict[str, Any] = {}
            for name in [i.name for i in self.session.get_inputs()]:
                if name.lower().endswith("input_ids"):
                    ort_inputs[name] = np.array([ids], dtype=np.int64)
                elif name.lower().endswith("attention_mask"):
                    ort_inputs[name] = np.array([attn], dtype=np.int64)
                elif name.lower().endswith("token_type_ids"):
                    ort_inputs[name] = np.zeros((1, len(ids)), dtype=np.int64)
            outputs = self.session.run(None, ort_inputs)
            logits = None
            for out in outputs:
                if getattr(out, "shape", None) is not None and len(out.shape) == 2 and out.shape[1] in (3,):
                    logits = out
                    break
            if logits is None:
                return 0.0
            # Softmax over 3-way MNLI: [contradiction, neutral, entailment]
            x = logits[0]
            m = float(np.max(x))
            exps = np.exp(x - m)
            probs = exps / float(np.sum(exps))
            entail_p = float(probs[-1])
            return entail_p
        except Exception:
            return 0.0
