"""Disjoint LinUCB with ridge prior; advisory scoring only, standard library."""
import math


class LinUCB:
    def __init__(self, dimension=4, alpha=1.0):
        if type(dimension) is not int or dimension < 1 or not math.isfinite(alpha) or alpha < 0:
            raise ValueError("invalid model configuration")
        self.dimension, self.alpha, self.arms = dimension, alpha, {}

    def _context(self, x):
        if len(x) != self.dimension or any(not math.isfinite(v) for v in x):
            raise ValueError("invalid feature vector")

    def _arm(self, name):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("agent name required")
        if name not in self.arms:
            self.arms[name] = ([[float(i == j) for j in range(self.dimension)]
                                for i in range(self.dimension)], [0.] * self.dimension)
        return self.arms[name]

    def score(self, name, x):
        self._context(x)
        inverse, b = self._arm(name)
        ax = [sum(a * v for a, v in zip(row, x)) for row in inverse]
        mean = sum(a * v for a, v in zip(ax, b))
        return mean + self.alpha * math.sqrt(max(0., sum(a*v for a,v in zip(x, ax))))

    def update(self, name, x, reward):
        self._context(x)
        if not math.isfinite(reward):
            raise ValueError("reward must be finite")
        inverse, b = self._arm(name)
        ax = [sum(a*v for a,v in zip(row, x)) for row in inverse]
        denominator = 1 + sum(a*v for a,v in zip(x, ax))
        for i in range(self.dimension):
            b[i] += reward * x[i]
            for j in range(self.dimension):
                inverse[i][j] -= ax[i] * ax[j] / denominator

    def recommend(self, candidates, x):
        if not candidates:
            raise ValueError("candidate agents required")
        return max(sorted(set(candidates)), key=lambda name: self.score(name, x))
