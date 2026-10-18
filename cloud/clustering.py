from collections import Counter, defaultdict

import numpy as np
from embedding import get_encoder
from sklearn.cluster import AgglomerativeClustering
from textnorm import make_id, norm_text

NEGATIVE_SUFFIXES = ("없음", "않음", "안됨", "안 됨", "못함", "불가", "부족")
OPPOSITES = (
    ("높음", "낮음"),
    ("많음", "적음"),
    ("큼", "작음"),
    ("무거움", "가벼움"),
    ("빠름", "느림"),
    ("강함", "약함"),
    ("밝음", "어두움"),
    ("넓음", "좁음"),
    ("두꺼움", "얇음"),
    ("발생", "없음"),
    ("있음", "없음"),
    ("좋음", "나쁨"),
    ("단단함", "물렁함"),
    ("시끄러움", "조용함"),
)


def opposite_status(a, b):
    a, b = norm_text(a), norm_text(b)
    return a.endswith(NEGATIVE_SUFFIXES) != b.endswith(NEGATIVE_SUFFIXES) or any(
        (a.endswith(x) and b.endswith(y)) or (a.endswith(y) and b.endswith(x)) for x, y in OPPOSITES
    )


def cluster_nodes(units, field, encoder, threshold, batch_size, excluded=(), status=False):
    nodes = defaultdict(list)
    for unit in units:
        if unit[field] is not None:
            nodes[norm_text(unit[field])].append(unit)
    for members in nodes.values():
        members.sort(key=lambda u: str(u["unit_id"]))
    keys = sorted(nodes)
    if not keys:
        return []
    labels = []
    for key in keys:
        counts = Counter(u[field] for u in nodes[key])
        labels.append(sorted(counts, key=lambda s: (-counts[s], s))[0])
    vectors = np.asarray(encoder.encode(labels, batch_size=batch_size, show_progress_bar=False), dtype=float)
    lengths = np.linalg.norm(vectors, axis=1, keepdims=True)
    if not np.isfinite(vectors).all() or (lengths == 0).any():
        raise ValueError("임베딩에 비정상 값/영벡터 존재")
    vectors /= lengths
    distances = np.clip(1 - vectors @ vectors.T, 0, 2)
    np.fill_diagonal(distances, 0)
    if status:
        for i in range(len(keys)):
            for j in range(i):
                if opposite_status(labels[i], labels[j]):
                    distances[i, j] = distances[j, i] = 2.0
    blocked = {norm_text(s) for s in excluded}
    active = [i for i, key in enumerate(keys) if key not in blocked]
    groups = [[i] for i, key in enumerate(keys) if key in blocked]
    if len(active) > 1:
        predictions = AgglomerativeClustering(
            n_clusters=None, metric="precomputed", linkage="complete", distance_threshold=threshold
        ).fit_predict(distances[np.ix_(active, active)])
        groups.extend(
            [
                [active[i] for i, label in enumerate(predictions) if label == cluster]
                for cluster in sorted(set(predictions))
            ]
        )
    elif active:
        groups.append(active)
    result = []
    for group in groups:
        chosen = min(
            group,
            key=lambda i: (
                -len({u["review_id"] for u in nodes[keys[i]]}),
                float(distances[i, group].mean()),
                labels[i],
            ),
        )
        result.append({"label": labels[chosen], "units": [u for i in group for u in nodes[keys[i]]]})
    return sorted(result, key=lambda group: group["label"])


def compute_clusters(category_id, units, settings, encoder=None):
    if not units:
        return [], [], []
    encoder = encoder or get_encoder(settings.embedding["model_path"])
    aspects = cluster_nodes(
        units,
        "raw_aspect",
        encoder,
        settings.clustering["aspect_distance_threshold"],
        settings.embedding["batch_size"],
        settings.clustering["excluded_raw_aspects"],
    )
    aspect_rows, state_rows, assignments = [], [], []
    for aspect in aspects:
        aid = make_id("ac", category_id, aspect["label"])
        aspect_rows.append((aid, category_id, aspect["label"]))
        states = cluster_nodes(
            aspect["units"],
            "raw_status",
            encoder,
            settings.clustering["status_distance_threshold"],
            settings.embedding["batch_size"],
            status=True,
        )
        state_map = {}
        for state in states:
            sid = make_id("sc", aid, state["label"])
            state_rows.append((sid, aid, state["label"]))
            state_map.update({u["unit_id"]: sid for u in state["units"]})
        assignments.extend((aid, state_map.get(u["unit_id"]), u["unit_id"]) for u in aspect["units"])
    return aspect_rows, state_rows, assignments
