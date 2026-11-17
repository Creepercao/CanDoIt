"""Small deterministic lexical retrieval primitives, without external models."""

import re
import math
from collections import Counter


def tokenize(text):
    """Lowercase identifiers/numbers; Chinese characters plus adjacent bigrams."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    tokens = []
    for match in re.finditer(r"[A-Za-z0-9_]+|[\u3400-\u9fff]+", text):
        term = match[0].lower()
        if term.isascii():
            tokens.append(term)
        else:
            tokens.extend(term)
            tokens.extend(term[index:index + 2] for index in range(len(term) - 1))
    return tokens


class BM25Index:
    """In-memory BM25 with positive IDF and deterministic ID tie-breaking."""

    def __init__(self, documents, k1=1.2, b=.75):
        if type(k1) not in (int, float) or not math.isfinite(k1) or k1 <= 0:
            raise ValueError("k1 must be finite and positive")
        if type(b) not in (int, float) or not math.isfinite(b) or not 0 <= b <= 1:
            raise ValueError("b must be finite and between 0 and 1")
        if not isinstance(documents, list):
            raise ValueError("documents must be an array")
        self.rows = []
        ids = set()
        for document in documents:
            if not isinstance(document, dict) or not isinstance(document.get("id"), str) or not document["id"].strip():
                raise ValueError("documents require non-empty string IDs")
            if document["id"] in ids:
                raise ValueError("document IDs must be unique")
            ids.add(document["id"])
            terms = tokenize(document.get("text"))
            self.rows.append((document["id"], Counter(terms), len(terms)))
        self.k1, self.b = k1, b
        self.average_length = sum(row[2] for row in self.rows) / len(self.rows) if self.rows else 0
        self.df = Counter(term for _, counts, _ in self.rows for term in counts)

    def search(self, query, top_k=5):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if type(top_k) is not int or top_k < 1:
            raise ValueError("top_k must be a positive integer")
        terms = set(tokenize(query))
        if not self.average_length or not terms:
            return []
        results = []
        for identifier, counts, length in self.rows:
            score = 0.0
            for term in sorted(terms):
                frequency = counts[term]
                if not frequency:
                    continue
                idf = math.log1p((len(self.rows) - self.df[term] + .5) / (self.df[term] + .5))
                norm = self.k1 * (1 - self.b + self.b * length / self.average_length)
                score += idf * frequency * (self.k1 + 1) / (frequency + norm)
            if score > 0:
                results.append({"id": identifier, "score": score})
        return sorted(results, key=lambda item: (-item["score"], item["id"]))[:top_k]


def reciprocal_rank_fusion(rankings, top_k=5, rank_constant=60):
    """Fuse ranked ID lists; duplicates within a route count only once."""
    if not isinstance(rankings, list) or any(not isinstance(route, list) for route in rankings):
        raise ValueError("rankings must be an array of ranked ID arrays")
    if type(top_k) is not int or top_k < 1:
        raise ValueError("top_k must be a positive integer")
    if type(rank_constant) is not int or rank_constant < 1:
        raise ValueError("rank_constant must be a positive integer")
    results = {}
    for route_index, route in enumerate(rankings):
        if any(not isinstance(identifier, str) or not identifier.strip() for identifier in route):
            raise ValueError("ranked IDs must be non-empty strings")
        for rank, identifier in enumerate(dict.fromkeys(route), 1):
            item = results.setdefault(identifier, {"id": identifier, "score": 0.0, "ranks": [None] * len(rankings)})
            item["score"] += 1 / (rank_constant + rank)
            item["ranks"][route_index] = rank
    return sorted(results.values(), key=lambda item: (-item["score"], item["id"]))[:top_k]
