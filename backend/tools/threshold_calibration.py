"""Calibrate the semantic thresholds against the provider actually in use.

A similarity threshold is only meaningful relative to the embedding model's own
distribution, so this measures it instead of guessing:

* **paraphrase** pairs — the same information need worded differently.  These
  *should* be accepted (a cache hit / a recalled memory).
* **same_topic** pairs — same domain, **different question**.  These are the
  dangerous false positives: accepting one returns research about something
  else, silently.
* **unrelated** pairs — different domains.

The usable threshold lives between ``max(negative)`` and ``min(positive)``.
Because the two error costs are asymmetric (a false cache hit replaces real work
with the wrong answer; a miss only costs one extra search), overlaps are
resolved strictly.

It also measures the *BGE query instruction prefix* variant, so the decision to
add it can be made on data rather than folklore.

Usage::

    python -m backend.tools.threshold_calibration
    python -m backend.tools.threshold_calibration --prefix off
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── labeled corpus ─────────────────────────────────────────────────────
# Kept in-repo so the calibration is reproducible and reviewable.

PARAPHRASE: list[tuple[str, str]] = [
    ("2024年新能源汽车销量是多少", "去年新能源汽车的销量数据"),
    ("帮我查一下英伟达最新的财报", "英伟达最近一期财务报告"),
    ("Python 怎么高效读取大文件", "用 Python 读取超大文本文件的正确方式"),
    ("中国人口老龄化现状", "国内老龄化的当前情况"),
    ("长三角和珠三角的经济对比", "珠江三角洲与长江三角洲的经济比较"),
    ("RAG 的检索召回率怎么提升", "如何提高检索增强生成的召回效果"),
    ("新能源汽车的补贴政策有哪些变化", "电动车补贴政策的最新调整"),
    ("Transformer 相比 RNN 的优势", "自注意力模型比循环神经网络强在哪里"),
    ("2024 年全球半导体市场规模", "去年全世界芯片行业的市场体量"),
    ("高校毕业生就业率数据", "应届大学生就业情况统计"),
    ("光伏产业链各环节毛利率", "太阳能产业链各段的利润水平"),
    ("如何降低大模型的推理成本", "减少 LLM 推理开销的办法"),
]

SAME_TOPIC: list[tuple[str, str]] = [
    ("2024年新能源汽车销量是多少", "2025年新能源汽车销量预测"),
    ("帮我查一下英伟达最新的财报", "英伟达的股价走势如何"),
    ("Python 怎么高效读取大文件", "Python 怎么高效写入大文件"),
    ("中国人口老龄化现状", "中国人口出生率的现状"),
    ("长三角和珠三角的经济对比", "长三角地区的房价走势"),
    ("RAG 的检索召回率怎么提升", "RAG 的回答准确率怎么评估"),
    ("新能源汽车的补贴政策有哪些变化", "新能源汽车的充电桩建设情况"),
    ("Transformer 相比 RNN 的优势", "Transformer 的训练显存占用"),
    ("2024 年全球半导体市场规模", "2024 年全球半导体产能分布"),
    ("高校毕业生就业率数据", "高校毕业生平均起薪数据"),
    ("光伏产业链各环节毛利率", "光伏产业链的产能利用率"),
    ("如何降低大模型的推理成本", "如何降低大模型的训练成本"),
]

UNRELATED: list[tuple[str, str]] = [
    ("2024年新能源汽车销量是多少", "红烧肉的家常做法"),
    ("帮我查一下英伟达最新的财报", "明天北京的天气怎么样"),
    ("Python 怎么高效读取大文件", "quantum entanglement explained"),
    ("中国人口老龄化现状", "欧冠决赛比分"),
    ("长三角和珠三角的经济对比", "如何练习马拉松配速"),
    ("RAG 的检索召回率怎么提升", "猫咪掉毛严重怎么办"),
    ("新能源汽车的补贴政策有哪些变化", "明清瓷器鉴定要点"),
    ("Transformer 相比 RNN 的优势", "意大利面煮多久合适"),
    ("2024 年全球半导体市场规模", "如何给吉他换弦"),
    ("高校毕业生就业率数据", "深海鱼类的发光机制"),
    ("光伏产业链各环节毛利率", "贝多芬第五交响曲赏析"),
    ("如何降低大模型的推理成本", "冬季滑雪装备怎么选"),
]

# BGE's recommended retrieval instruction for the query side.
BGE_ZH_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


# ── measurement ────────────────────────────────────────────────────────


@dataclass
class Group:
    name: str
    pairs: list[tuple[str, str]]
    scores: list[float] = field(default_factory=list)

    def stats(self) -> dict:
        if not self.scores:
            return {}
        ordered = sorted(self.scores)
        return {
            "n": len(ordered),
            "min": round(ordered[0], 4),
            "p25": round(_percentile(ordered, 0.25), 4),
            "median": round(statistics.median(ordered), 4),
            "p75": round(_percentile(ordered, 0.75), 4),
            "max": round(ordered[-1], 4),
            "mean": round(statistics.fmean(ordered), 4),
        }


def _percentile(ordered: list[float], q: float) -> float:
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


async def measure(groups: list[Group], *, query_prefix: str = "") -> None:
    """Fill ``group.scores`` with the cosine similarity of every pair."""
    from backend.embedding import cosine_similarity, get_embeddings

    # One batched call per side keeps this to two requests.
    lefts = [a for group in groups for a, _ in group.pairs]
    rights = [b for group in groups for _, b in group.pairs]

    if query_prefix:
        lefts = [query_prefix + text for text in lefts]

    left_vectors = await get_embeddings(lefts)
    right_vectors = await get_embeddings(rights)
    if left_vectors is None or right_vectors is None:
        raise RuntimeError("embedding provider unavailable — check API.json / providers.yaml")
    if len(left_vectors) != len(lefts) or len(right_vectors) != len(rights):
        raise RuntimeError(
            f"embedding count mismatch: {len(left_vectors)}/{len(lefts)}, "
            f"{len(right_vectors)}/{len(rights)}"
        )

    cursor = 0
    for group in groups:
        group.scores = []
        for _ in group.pairs:
            group.scores.append(
                cosine_similarity(left_vectors[cursor], right_vectors[cursor])
            )
            cursor += 1


def recommend(positive: Group, negatives: list[Group]) -> dict:
    """Pick a threshold from the observed gap, resolving overlap strictly."""
    if not positive.scores or not any(g.scores for g in negatives):
        return {"threshold": None, "reason": "no measurements"}

    neg_scores = [s for group in negatives for s in group.scores]
    low = max(neg_scores)                      # highest false-positive score
    high = min(positive.scores)                # lowest true-positive score

    result = {
        "gap_low": round(low, 4),
        "gap_high": round(high, 4),
        "separation": round(high - low, 4),
        "worst_negative": _worst_pairs(negatives, low),
        "worst_positive": _worst_pairs([positive], high, highest=False),
    }

    if high > low:
        threshold = round((low + high) / 2, 3)
        result.update({
            "threshold": threshold,
            "note": (
                f"存在 {(high - low):.3f} 的间隔；取中点，"
                f"两侧都留有 {((high - low) / 2):.3f} 余量"
            ),
        })
    else:
        # Overlap: no single value separates cleanly.  Fail strict — a false
        # cache hit silently substitutes the wrong research.
        threshold = round(high, 3)
        result.update({
            "threshold": threshold,
            "note": (
                f"正负样本重叠 {(low - high):.3f}，不存在干净的分割点；"
                f"取最严的可行值 min(positive)={high:.3f}，"
                f"宁可漏命中也不误命中"
            ),
        })
    return result


def _worst_pairs(groups: list[Group], score: float, *, highest: bool = True) -> list[dict]:
    out = []
    for group in groups:
        for (a, b), s in zip(group.pairs, group.scores):
            if abs(s - score) < 1e-9:
                out.append({"group": group.name, "a": a, "b": b, "score": round(s, 4)})
    return out[:3]


def render(result: dict) -> str:
    lines = [
        "",
        "=" * 68,
        f"模式: {'查询侧加 BGE 指令前缀' if result['prefix'] else '不使用前缀（当前行为）'}",
        "=" * 68,
    ]
    for name, stats in result["groups"].items():
        if not stats:
            continue
        lines.append(
            f"  {name:<12} n={stats['n']:<3} "
            f"min={stats['min']:.3f} p25={stats['p25']:.3f} "
            f"中位={stats['median']:.3f} p75={stats['p75']:.3f} max={stats['max']:.3f}"
        )
    lines.append("")
    rec = result["recommendation"]
    if rec.get("threshold") is None:
        lines.append("  无法给出建议：缺少测量数据")
        return "\n".join(lines)

    lines.append(f"  最差的负样本（必须拒绝）: {rec['gap_low']:.3f}")
    for item in rec["worst_negative"]:
        lines.append(f"      · [{item['group']}] {item['a']} ↔ {item['b']}")
    lines.append(f"  最差的正样本（必须接受）: {rec['gap_high']:.3f}")
    for item in rec["worst_positive"]:
        lines.append(f"      · [{item['group']}] {item['a']} ↔ {item['b']}")
    lines.append("")
    lines.append(f"  → 建议阈值 {rec['threshold']}")
    lines.append(f"    {rec['note']}")
    return "\n".join(lines)


# ── entry point ────────────────────────────────────────────────────────


async def calibrate(prefix_mode: str = "both") -> dict:
    modes = {"off": "", "on": BGE_ZH_QUERY_PREFIX}
    if prefix_mode == "both":
        selected = [("off", ""), ("on", BGE_ZH_QUERY_PREFIX)]
    elif prefix_mode == "on":
        selected = [("on", BGE_ZH_QUERY_PREFIX)]
    else:
        selected = [("off", "")]

    report: dict = {
        "embedding_model": os.environ.get("EMBEDDING_MODEL", "BAAI/bge-large-zh-v1.5"),
        "corpus": {
            "paraphrase": len(PARAPHRASE),
            "same_topic": len(SAME_TOPIC),
            "unrelated": len(UNRELATED),
        },
        "runs": [],
    }

    for label, prefix in selected:
        groups = [
            Group("paraphrase", PARAPHRASE),
            Group("same_topic", SAME_TOPIC),
            Group("unrelated", UNRELATED),
        ]
        await measure(groups, query_prefix=prefix)
        entry = {
            "prefix": bool(prefix),
            "prefix_text": prefix,
            "groups": {g.name: g.stats() for g in groups},
            "recommendation": recommend(
                groups[0], [groups[1], groups[2]]
            ),
        }
        entry["pairs"] = {
            g.name: [
                {"a": a, "b": b, "score": round(s, 4)}
                for (a, b), s in zip(g.pairs, g.scores)
            ]
            for g in groups
        }
        report["runs"].append(entry)
        print(render(entry))

    if len(report["runs"]) == 2:
        off, on = report["runs"]
        print("\n" + "=" * 68)
        print("前缀对比（间隔越大越好区分）")
        print("=" * 68)
        for label, entry in (("不加前缀", off), ("加前缀", on)):
            rec = entry["recommendation"]
            print(f"  {label:<8} 间隔={rec['separation']:+.3f}  "
                  f"建议阈值={rec['threshold']}"
                  f"{'  (重叠)' if rec['separation'] <= 0 else ''}")
        delta = on["recommendation"]["separation"] - off["recommendation"]["separation"]
        verdict = (
            "加前缀更好 → 建议启用 EMBEDDING_QUERY_PREFIX"
            if delta > 0.005 else
            "两者接近或前缀更差 → 保持不加前缀"
        )
        print(f"  → {verdict}（间隔差 {delta:+.3f}）")
        report["prefix_verdict"] = {"separation_delta": round(delta, 4), "verdict": verdict}

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", choices=["both", "off", "on"], default="both",
                        help="测量查询侧 BGE 指令前缀的影响")
    parser.add_argument("--json", default="", help="把完整结果写入该路径")
    args = parser.parse_args()

    report = asyncio.run(calibrate(args.prefix))

    target = Path(args.json) if args.json else (
        Path("outputs") / "calibration" / "embedding-thresholds.json"
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n完整结果已写入 {target}")
    except OSError as exc:
        print(f"\n结果写入失败（{exc}）")

    print("\n可粘贴到环境变量的建议值：")
    for entry in report["runs"]:
        label = "加前缀" if entry["prefix"] else "不加前缀"
        print(f"  # {label}")
        print(f"  RESEARCH_CACHE_THRESHOLD={entry['recommendation']['threshold']}")


if __name__ == "__main__":
    main()
