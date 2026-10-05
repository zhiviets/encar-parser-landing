"""Модели из справочника сайта (src/catalog/modelCatalog.js в bn-auto), которых на сайте меньше 3 машин:
сайт отдаёт их по /api/live-listings/wanted, парсер берёт их машины в первую очередь — до нужного числа.
Названия сравниваются без регистра, диакритики и знаков («Citroën» = «Citroen», «Mhero» = «M-Hero»);
у длинных — по началу («Land Cruiser Prado 150/250» = «Land Cruiser Prado»). Так же, как на сайте."""
import json
import re
import unicodedata
import urllib.request

MAKE_ALIAS = {"immotor": "immotors", "gacaion": "aion"}


def key(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9а-яё]", "", s)


def make_key(s) -> str:
    k = key(s)
    return MAKE_ALIAS.get(k, k)


def same_model(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)))


class Wanted:
    """need(марка, модель) — сколько машин модели ещё нужно (0 — не из справочника или уже хватает)."""

    def __init__(self, items=()):
        self.by_make = {}
        for w in items:
            self.by_make.setdefault(make_key(w.get("make")), []).append([key(w.get("model")), int(w.get("need") or 0)])

    def _find(self, make, model):
        md = key(model)
        if not md:
            return None
        return next((w for w in self.by_make.get(make_key(make), []) if same_model(w[0], md)), None)

    def need(self, make, model) -> int:
        w = self._find(make, model)
        return w[1] if w else 0

    def took(self, make, model, n: int = 1):
        w = self._find(make, model)
        if w:
            w[1] = max(0, w[1] - n)

    def __len__(self):
        return sum(len(v) for v in self.by_make.values())


def load(base_url: str, token: str, source: str) -> Wanted:
    """Список с сайта; не получился — пустой (парсер выбирает как обычно)."""
    if not base_url or not token:
        return Wanted()
    try:
        req = urllib.request.Request(f"{base_url.rstrip('/')}/api/live-listings/wanted?source={source}",
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.load(resp)
        wanted = Wanted(data.get("wanted") or [])
        print(f"Справочник сайта: моделей, которых на сайте меньше {data.get('min')} машин, — {len(wanted)}")
        return wanted
    except Exception as error:
        print(f"Справочник моделей с сайта не получен ({error}) — выбираем как обычно")
        return Wanted()
