"""文本抽取层：她的 RP 移动、他的移动/位置陈述。纯函数。

照抄 vb events 的成熟模式：否定前缀、引号跳过、长房间名占优、邻近窗口配对。
"""
from __future__ import annotations

from typing import Any
import re

from .. import CONFIG

NEGATION_PREFIXES = ("别", "不要", "不准", "不许", "莫", "勿")
MOVE_VERBS = ("去", "到", "回", "进", "走", "搬")          # 他/她的移动动词（"出"歧义多不作单独动词）
STAY_MARKERS = ("我在", "本人在", "待在")                   # 显式位置陈述
PUNCT = "，。！？；、,.;?!…—\"“”"


def _quote_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    l = text.find("\u201c")
    while l >= 0:
        r = text.find("\u201d", l + 1)
        if r < 0:
            break
        spans.append((l, r))
        l = text.find("\u201c", r + 1)
    marks = [i for i, ch in enumerate(text) if ch == '"']
    for i in range(0, len(marks) - 1, 2):
        spans.append((marks[i], marks[i + 1]))
    for left, right in (("「", "」"), ("『", "』")):
        begin = text.find(left)
        while begin >= 0:
            end = text.find(right, begin + 1)
            spans.append((begin, end if end >= 0 else len(text)))
            begin = text.find(left, end + 1) if end >= 0 else -1
    if l >= 0:
        spans.append((l, len(text)))
    if len(marks) % 2:
        spans.append((marks[-1], len(text)))
    return spans


def _negated(text: str, pos: int) -> bool:
    left, _ = _clause(text, pos)
    head = text[max(left, pos - 10):pos]
    return any(n in head for n in (*NEGATION_PREFIXES, "没有", "没打算", "不想", "不会", "不愿", "未曾", "没去")) or bool(re.search(r"(?:不|没|未)\s*$", head))


def _clause(text: str, pos: int) -> tuple[int, int]:
    separators = "，。！？；,\n.;?!"
    left = max((text.rfind(c, 0, pos) for c in separators), default=-1) + 1
    rights = [p for c in separators if (p := text.find(c, pos)) >= 0]
    return left, min(rights) if rights else len(text)


def _subject(text: str, pos: int) -> str | None:
    left, _ = _clause(text, pos)
    matches = list(re.finditer(r"本人|我|她|他|你", text[max(left, pos - 12):pos]))
    return matches[-1].group() if matches else None


def _not_observation(text: str, pos: int) -> bool:
    """询问、假设、愿望与谈论位置不视为已发生的行动。"""
    left, right = _clause(text, pos)
    clause = text[left:right]
    head = text[left:pos]
    return (
        (right < len(text) and text[right] in "？?")
        or any(w in clause for w in ("吗", "是否", "是不是", "会不会", "如果", "假如", "万一", "也许"))
        or any(w in head for w in ("希望", "打算", "准备", "想要", "想着", "想去", "询问", "问她", "问你"))
    )


def _in_quote(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(left <= pos < right for left, right in spans)


def room_words(manor: dict[str, Any]) -> list[tuple[str, str]]:
    """(词, room_id) 全表：label + aliases；长词优先排序。"""
    words: list[tuple[str, str]] = []
    for r in manor.get("rooms") or []:
        rid = str(r.get("id"))
        for w in [str(r.get("label") or "")] + [str(a) for a in (r.get("aliases") or [])]:
            if w:
                words.append((w, rid))
    return sorted(words, key=lambda x: -len(x[0]))



def extract_room_mentions(text: str, manor: dict[str, Any]) -> list[dict[str, Any]]:
    """文本中全部房间提及：[{room, pos, quoted}]（按出现顺序）。"""
    text = str(text or "")
    if not text.strip():
        return []
    quoted = _quote_spans(text)
    mentions: list[dict[str, Any]] = []
    for w, rid in room_words(manor):
        start = 0
        while True:
            pos = text.find(w, start)
            if pos < 0:
                break
            start = pos + 1
            is_q = any(l <= pos < r for l, r in quoted)
            mentions.append({"room": rid, "pos": pos, "len": len(w), "quoted": is_q, "word": w})
    mentions.sort(key=lambda m: m["pos"])
    return mentions


def extract_her_move(text: str, manor: dict[str, Any]) -> dict[str, Any] | None:
    """她的移动：'她…去/回/到 <房间>' 句式（她 + 动词 + 房间词邻近）。RP 覆盖源。"""
    text = str(text or "")
    if "她" not in text:
        return None
    quoted = _quote_spans(text)
    result = None
    mentions = extract_room_mentions(text, manor)
    for vi, ch in enumerate(text):
        if ch not in MOVE_VERBS:
            continue
        if _subject(text, vi) != "她" or _in_quote(vi, quoted):
            continue
        if _negated(text, vi) or _not_observation(text, vi):
            continue
        _, right = _clause(text, vi)
        targets = [m for m in mentions if 0 < m["pos"] - vi <= 4
                   and m["pos"] + m["len"] <= right and not m["quoted"]]
        if targets:
            target = min(targets, key=lambda m: (m["pos"], -m["len"]))
            result = {"room": target["room"], "verb_at": vi, "word": target["word"]}
    return result


def extract_him(text: str, manor: dict[str, Any]) -> dict[str, Any] | None:
    """他的位置：明确第一人称的陈述/移动，按最后一次有效叙述落位。"""
    text = str(text or "")
    if not text.strip():
        return None
    quoted = _quote_spans(text)
    candidates: list[tuple[int, dict[str, Any]]] = []
    mentions = extract_room_mentions(text, manor)
    # 显式陈述："待在"必须属于第一人称，不能把"她待在"记在用户身上。
    for marker in STAY_MARKERS:
        mpos = text.find(marker)
        while mpos >= 0:
            own_subject = marker in ("我在", "本人在") or _subject(text, mpos) in ("我", "本人")
            if own_subject and not _in_quote(mpos, quoted) and not _negated(text, mpos) and not _not_observation(text, mpos):
                start = mpos + len(marker)
                _, right = _clause(text, mpos)
                targets = [m for m in mentions if 0 <= m["pos"] - start <= 4
                           and m["pos"] + m["len"] <= right and not m["quoted"]
                           and not any(w in text[start:m["pos"]] for w in ("想", "问", "找", "说"))]
                if targets:
                    target = min(targets, key=lambda m: (m["pos"], -m["len"]))
                    candidates.append((mpos, {"room": target["room"], "direct": True}))
            mpos = text.find(marker, mpos + 1)
    # 移动动词
    for vi, ch in enumerate(text):
        if ch not in MOVE_VERBS or _in_quote(vi, quoted) or _negated(text, vi) or _not_observation(text, vi):
            continue
        if _subject(text, vi) not in ("我", "本人"):
            continue
        _, right = _clause(text, vi)
        targets = [m for m in mentions if 0 < m["pos"] - vi <= 4
                   and m["pos"] + m["len"] <= right and not m["quoted"]]
        if targets:
            target = min(targets, key=lambda m: (m["pos"], -m["len"]))
            candidates.append((vi, {"room": target["room"], "direct": False}))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def text_has_room_word(text: str, manor: dict[str, Any]) -> bool:
    """门控④用：消息里是否有未引号房间词。"""
    for m in extract_room_mentions(text, manor):
        if not m["quoted"]:
            return True
    return False
