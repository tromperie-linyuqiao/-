"""寻路层：房间图 + Dijkstra。纯函数，无 IO 无 astrbot 依赖。

图结构：房间节点 + 楼层枢纽；全部通路由 manor.edges 显式连接，
不添加房间↔楼层免费中转边。
"""
from __future__ import annotations

import heapq
import math
from typing import Any


def build_graph(manor: dict[str, Any]) -> dict[str, list[tuple[str, int]]]:
    """node → [(neighbor, seconds)]。楼层节点只作楼梯枢纽（显式边连接），不作免费中转——
    隐式 0 秒房间↔楼层边会让同层全免费通行（轮 1 实测教训）。"""
    graph: dict[str, list[tuple[str, int]]] = {}
    rooms, floors = manor.get("rooms"), manor.get("floors")
    nodes = list(rooms) if isinstance(rooms, (list, tuple)) else []
    nodes += list(floors) if isinstance(floors, (list, tuple)) else []
    for r in nodes:
        if isinstance(r, dict) and r.get("id"):
            graph.setdefault(str(r["id"]), [])
    for e in manor.get("edges") or []:
        if not isinstance(e, dict):
            continue
        a, b = str(e.get("a") or ""), str(e.get("b") or "")
        if a not in graph or b not in graph:
            continue   # 悬空边不能通过未知节点制造捷径。
        try:
            value = float(e.get("seconds"))
            if isinstance(e.get("seconds"), bool) or not math.isfinite(value) or value < 0:
                continue
            sec = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        graph.setdefault(a, []).append((b, sec))
        graph.setdefault(b, []).append((a, sec))
    return graph


def travel_seconds(manor: dict[str, Any], a: str, b: str) -> int | None:
    """a→b 最短移动秒数；不可达或节点未知返回 None。a==b 返回 0。"""
    if a == b:
        return 0
    graph = build_graph(manor)
    if a not in graph or b not in graph:
        return None
    dist: dict[str, int] = {a: 0}
    pq: list[tuple[int, str]] = [(0, a)]
    seen: set[str] = set()
    while pq:
        d, node = heapq.heappop(pq)
        if node in seen:
            continue
        seen.add(node)
        if node == b:
            return d
        for nb, sec in graph.get(node, []):
            nd = d + sec
            if nd < dist.get(nb, 1 << 30):
                dist[nb] = nd
                heapq.heappush(pq, (nd, nb))
    return None
