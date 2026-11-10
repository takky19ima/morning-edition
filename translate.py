"""Machine translation of the day's stories.

Google Translate's free web endpoint goes first: it translates best, but it often refuses requests
from GitHub's servers ("429 Too Many Requests"). Whatever it refuses is translated on the build
machine itself by Meta's open NLLB-200 model. Every text is translated once and kept in
cache/translations.json, so tomorrow's run only translates what's new.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ENDPOINT = "https://translate.googleapis.com/translate_a/single"
CODES = {"kk": "kk", "zh": "zh-CN", "ru": "ru"}   # our language id → Google's
BATCH_CHARS = 4500                            # several texts per request, one per line
PAUSE = 0.7                                   # seconds between requests: the free endpoint refuses bursts
RETRIES = (5, 15)                             # seconds to wait after "429 Too Many Requests"
GIVE_UP_AFTER = 2                             # refusals in a row before leaving the rest to the local model

# Meta's NLLB-200 (1.3B, distilled), converted for fast CPU use. CC BY-NC 4.0: fine for a personal paper.
LOCAL_MODEL = "OpenNMT/nllb-200-distilled-1.3B-ct2-int8"
LOCAL_TOKENIZER = ("facebook/nllb-200-distilled-600M", "sentencepiece.bpe.model")   # same for every NLLB-200
NLLB = {"kk": "kaz_Cyrl", "zh": "zho_Hans", "ru": "rus_Cyrl"}
ABBREVIATIONS = {"no", "mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "inc", "ltd", "co", "gov", "gen", "sen", "rep",
                 "u.s", "u.k", "e.g", "i.e", "a.m", "p.m", "approx", "est", "dept", "fig", "vol"}


def sentences(text: str) -> list[str]:
    """Split into sentences, but not after "No.", "U.S.", "Dr." and the like."""
    out: list[str] = []
    for piece in re.split(r"(?<=[.!?…])\s+(?=[\"“‘'(A-Z0-9])", text):
        last = out[-1].rsplit(None, 1)[-1].rstrip(".").lower() if out else ""
        if out and (last in ABBREVIATIONS or len(last) == 1):
            out[-1] += " " + piece
        elif piece.strip():
            out.append(piece)
    return out


class LocalModel:
    """NLLB-200 on the build machine — slower and a little less fluent than Google, but never refuses."""

    def __init__(self):
        self.ready: bool | None = None

    def _load(self) -> bool:
        if self.ready is None:
            try:
                import ctranslate2
                import sentencepiece
                from huggingface_hub import hf_hub_download, snapshot_download
                print("  loading the local translation model…")
                self.sp = sentencepiece.SentencePieceProcessor(model_file=hf_hub_download(*LOCAL_TOKENIZER))
                self.model = ctranslate2.Translator(snapshot_download(LOCAL_MODEL), device="cpu",
                                                    compute_type="int8", intra_threads=os.cpu_count() or 4)
                self.ready = True
            except Exception as ex:
                print(f"  ! local translation model unavailable — {ex}")
                self.ready = False
        return self.ready

    def translate(self, texts: list[str], lang: str) -> list[str | None]:
        if lang not in NLLB or not texts or not self._load():
            return [None] * len(texts)
        pieces, owner = [], []                   # NLLB translates a sentence at a time
        for i, text in enumerate(texts):
            for sentence in sentences(text):
                pieces.append(sentence)
                owner.append(i)
        source = [["eng_Latn", *self.sp.encode(x, out_type=str), "</s>"] for x in pieces]
        prefix = [[NLLB[lang]]]
        out = self.model.translate_batch(source, target_prefix=prefix * len(source), beam_size=4,
                                         max_batch_size=24, max_decoding_length=256)
        target = [r.hypotheses[0][1:] for r in out]
        # NLLB sometimes stops halfway through a sentence; translate those again, made to go on longer
        for j, (src, tgt) in enumerate(zip(source, target)):
            if len(src) > 8 and len(tgt) < 0.5 * len(src):
                again = self.model.translate_batch([src], target_prefix=prefix, beam_size=5, max_decoding_length=256,
                                                   min_decoding_length=int(0.6 * len(src)) + 1)
                target[j] = again[0].hypotheses[0][1:]
        parts: list[list[str]] = [[] for _ in texts]
        for i, tgt in zip(owner, target):
            parts[i].append(self.sp.decode(tgt))
        joiner = "" if lang == "zh" else " "
        return [joiner.join(p) if p else None for p in parts]


class Translator:
    def __init__(self, path: Path, today: date, keep_days: int = 21):
        self.path, self.today, self.keep_days = path, today, keep_days
        self.cache: dict[str, dict[str, list]] = json.loads(path.read_text()) if path.exists() else {}
        self.failed: dict[str, int] = {}
        self.refused_in_a_row = 0
        self.local = LocalModel()

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha1(text.encode()).hexdigest()[:16]

    def many(self, texts: list[str], lang: str) -> dict[str, str]:
        """Translate texts into lang; returns {original: translation}. Missing ones fall back to the original."""
        store = self.cache.setdefault(lang, {})
        wanted = [t for t in dict.fromkeys(texts) if t and t.strip()]
        # new texts, plus ones the local model translated before: Google may do better today
        todo = [t for t in wanted if self.key(t) not in store or store[self.key(t)][2:] == ["model"]]
        batches, current, size = [], [], 0
        for t in todo:
            if current and size + len(t) > BATCH_CHARS:
                batches.append(current)
                current, size = [], 0
            current.append(t)
            size += len(t) + 1
        if current:
            batches.append(current)
        for batch in batches:                    # one request at a time — bursts get refused
            if self.refused_in_a_row >= GIVE_UP_AFTER:
                break                            # Google has stopped answering this machine today
            for original, translated in zip(batch, self._batch(batch, lang)):
                if translated:
                    store[self.key(original)] = [translated, self.today.isoformat()]
        by_model = 0
        rest = [t for t in wanted if self.key(t) not in store]
        for original, translated in zip(rest, self.local.translate(rest, lang)):
            if translated:
                store[self.key(original)] = [translated, self.today.isoformat(), "model"]
                by_model += 1
        out = {}
        for t in wanted:
            hit = store.get(self.key(t))
            if hit:
                hit[1] = self.today.isoformat()
            out[t] = hit[0] if hit else t
        missing = sum(1 for t in wanted if self.key(t) not in store)
        print(f"  translated into {lang}: {len(wanted) - missing} of {len(wanted)} texts"
              + (f" ({by_model} by the local model)" if by_model else "")
              + (f" — {missing} left in English" if missing else ""))
        return out

    def _batch(self, texts: list[str], lang: str) -> list[str | None]:
        joined = self._call("\n".join(texts), lang)
        if joined is None:
            return [None] * len(texts)          # refused even after waiting: leave these in English
        parts = joined.split("\n")
        if len(parts) == len(texts):
            return [p.strip() for p in parts]
        if len(texts) == 1:
            return [joined.strip()]
        half = len(texts) // 2                  # lines got merged or split: try the halves
        return self._batch(texts[:half], lang) + self._batch(texts[half:], lang)

    def _call(self, text: str, lang: str) -> str | None:
        query = urllib.parse.urlencode({"client": "gtx", "sl": "en", "tl": CODES.get(lang, lang), "dt": "t"})
        req = urllib.request.Request(
            f"{ENDPOINT}?{query}", data=urllib.parse.urlencode({"q": text}).encode(),
            headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        )
        for wait in (*RETRIES, None):
            time.sleep(PAUSE)
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    self.refused_in_a_row = 0
                    return "".join(seg[0] for seg in json.load(r)[0] if seg[0])
            except urllib.error.HTTPError as ex:
                if ex.code == 429 and wait:
                    time.sleep(wait)
                    continue
                error = ex
            except Exception as ex:
                error = ex
            break
        self.failed[lang] = self.failed.get(lang, 0) + 1
        self.refused_in_a_row += 1
        if self.failed[lang] <= 3:
            print(f"  ! translation to {lang} failed — {error}")
        return None

    def save(self) -> None:
        cutoff = (self.today - timedelta(days=self.keep_days)).isoformat()
        for store in self.cache.values():
            for k in [k for k, (_, used, *_) in store.items() if used < cutoff]:
                del store[k]
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(self.cache, ensure_ascii=False, separators=(",", ":")))
