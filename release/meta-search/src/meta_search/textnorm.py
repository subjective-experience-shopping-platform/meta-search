import hashlib
import re
import unicodedata


def norm_text(s):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s).strip()).casefold()


def model_key(brand, model):
    return re.sub(r"[\s_-]", "", norm_text(brand + " " + model))


def make_id(prefix, *parts):
    return prefix + "_" + hashlib.sha1("\x1f".join(norm_text(p) for p in parts).encode()).hexdigest()[:10]
