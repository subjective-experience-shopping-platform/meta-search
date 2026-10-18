"""The model is baked into the Lambda image. Never download during invocation."""

from functools import lru_cache


@lru_cache(maxsize=1)
def get_encoder(model_path):
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_path, local_files_only=True, device="cpu")
