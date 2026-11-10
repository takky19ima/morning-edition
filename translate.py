"""Machine translation of the day's stories through Google Translate's free web endpoint.

Every text is translated once and kept in cache/translations.json, so tomorrow's run only
translates what's new. If the service is unreachable, the text stays in English.
"""
from __future__ import annotations

import hashlib
import json
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
RETRIES = (5, 15, 40)                         # seconds to wait after "429 Too Many Requests"


class Translator:
    def __init__(self, path: Path, today: date, keep_days: int = 21):
        self.path, self.today, self.keep_days = path, today, keep_days
        self.cache: dict[str, dict[str, list]] = json.loads(path.read_text()) if path.exists() else {}
        self.failed: dict[str, int] = {}

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha1(text.encode()).hexdigest()[:16]

    def many(self, texts: list[str], lang: str) -> dict[str, str]:
        """Translate texts into lang; returns {original: translation}. Missing ones fall back to the original."""
        store = self.cache.setdefault(lang, {})
        wanted = [t for t in dict.fromkeys(texts) if t and t.strip()]
        todo = [t for t in wanted if self.key(t) not in store]
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
            for original, translated in zip(batch, self._batch(batch, lang)):
                if translated:
                    store[self.key(original)] = [translated, self.today.isoformat()]
        out = {}
        for t in wanted:
            hit = store.get(self.key(t))
            if hit:
                hit[1] = self.today.isoformat()
            out[t] = hit[0] if hit else t
        missing = sum(1 for t in wanted if self.key(t) not in store)
        print(f"  translated into {lang}: {len(wanted) - missing} of {len(wanted)} texts"
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
        if self.failed[lang] <= 3:
            print(f"  ! translation to {lang} failed — {error}")
        return None

    def save(self) -> None:
        cutoff = (self.today - timedelta(days=self.keep_days)).isoformat()
        for store in self.cache.values():
            for k in [k for k, (_, used) in store.items() if used < cutoff]:
                del store[k]
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(self.cache, ensure_ascii=False, separators=(",", ":")))
