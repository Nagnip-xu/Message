"""Small isolated CPU recognizer. Run with the optional voice Python environment."""
from __future__ import annotations

import argparse
import json
import os
import re
import wave


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("audio")
    parser.add_argument("--model", required=True)
    parser.add_argument("--tokens", required=True)
    args = parser.parse_args()
    if hasattr(os, "nice"):
        os.nice(10)
    import numpy as np
    import sherpa_onnx
    with wave.open(args.audio, "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 16000:
            raise ValueError("Expected mono 16kHz PCM16")
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float32) / 32768
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=args.model, tokens=args.tokens, num_threads=1, use_itn=True,
        language="auto", provider="cpu", debug=False)
    # Bound peak model memory for long recordings. No overlap => no repeated text.
    texts = []
    for start in range(0, len(samples), 16000 * 25):
        chunk = samples[start:start + 16000 * 25]
        if not len(chunk) or float(np.max(np.abs(chunk))) < .001:
            continue
        stream = recognizer.create_stream()
        stream.accept_waveform(16000, chunk)
        recognizer.decode_stream(stream)
        text = re.sub(r"<\|[^|]*\|>", "", stream.result.text).strip()
        if text:
            texts.append(text)
    print(json.dumps({"text": "".join(texts)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
