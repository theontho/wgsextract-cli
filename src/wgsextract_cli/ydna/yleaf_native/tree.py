# SPDX-License-Identifier: GPL-3.0-or-later
# Derived from genid/Yleaf v4.1.6, yleaf/tree.py (see provenance.json).
# Copyright Bram van Wersch, Erasmus MC Department of Genetic Identification.
# Modified 2026-11-01 by WGS Extract contributors: Python typing and formatting.
"""Read Yleaf's JSON haplogroup tree into parent-linked nodes."""

import json
from pathlib import Path


class Tree:
    ROOT_KEY = 'ROOT (Y-Chromosome "Adam")'

    def __init__(self, file: Path | str) -> None:
        self.node_mapping: dict[str, Node] = {}
        with open(file, encoding="utf-8") as stream:
            tree_dict: dict[str, list[str]] = json.load(stream)
        root_children = tree_dict.get(self.ROOT_KEY, ["A00", "A0-T"])
        base_node = Node(self.ROOT_KEY, None, 0, root_children)
        self.node_mapping[self.ROOT_KEY] = base_node
        self._recursive_read(tree_dict, base_node, 0)

    def _recursive_read(
        self, tree_dict: dict[str, list[str]], node: "Node", depth: int
    ) -> None:
        depth += 1
        for node_name in tree_dict[node.name]:
            children = tree_dict.get(node_name, [])
            new_node = Node(node_name, node, depth, children)
            self.node_mapping[node_name] = new_node
            self._recursive_read(tree_dict, new_node, depth)

    def get(self, node_name: str) -> "Node":
        return self.node_mapping[node_name]


class Node:
    __slots__ = "_name", "_parent", "_depth", "_children"

    def __init__(
        self, name: str, parent_node: "Node | None", depth: int, children: list[str]
    ) -> None:
        self._name = name
        self._parent = parent_node
        self._depth = depth
        self._children = children

    @property
    def name(self) -> str:
        return self._name

    @property
    def parent(self) -> "Node | None":
        return self._parent

    @property
    def depth(self) -> int:
        return self._depth

    @property
    def children(self) -> list[str]:
        return self._children
