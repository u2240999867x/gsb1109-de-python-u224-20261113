"""Dinic maximum-flow implementation (no third-party dependencies)."""

from collections import deque


class Dinic(object):
    """Max-flow over an integer graph.

    Vertices are integer indices. Edge capacities are non-negative ints.
    """

    def __init__(self, vertex_count):
        self.vertex_count = vertex_count
        self.graph = [[] for _ in range(vertex_count)]

    def add_edge(self, src, dst, capacity):
        if capacity <= 0:
            return None
        forward = [dst, capacity, len(self.graph[dst])]
        backward = [src, 0, len(self.graph[src])]
        self.graph[src].append(forward)
        self.graph[dst].append(backward)
        return forward

    def _bfs_level(self, source, sink):
        level = [-1] * self.vertex_count
        level[source] = 0
        queue = deque([source])
        while queue:
            vertex = queue.popleft()
            for edge in self.graph[vertex]:
                target, capacity, _ = edge
                if capacity > 0 and level[target] < 0:
                    level[target] = level[vertex] + 1
                    queue.append(target)
        self.level = level
        return level[sink] >= 0

    def _dfs_flow(self, vertex, sink, pushed):
        if pushed == 0:
            return 0
        if vertex == sink:
            return pushed
        while self.ptr[vertex] < len(self.graph[vertex]):
            edge = self.graph[vertex][self.ptr[vertex]]
            target, capacity, reverse_index = edge
            if capacity > 0 and self.level[target] == self.level[vertex] + 1:
                flow = self._dfs_flow(target, sink, min(pushed, capacity))
                if flow:
                    edge[1] -= flow
                    self.graph[target][reverse_index][1] += flow
                    return flow
            self.ptr[vertex] += 1
        return 0

    def max_flow(self, source, sink):
        total = 0
        infinity = float('inf')
        while self._bfs_level(source, sink):
            self.ptr = [0] * self.vertex_count
            while True:
                pushed = self._dfs_flow(source, sink, infinity)
                if not pushed:
                    break
                total += pushed
        return total
