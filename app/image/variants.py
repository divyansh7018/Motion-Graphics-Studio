"""The image version graph (Stage F, sections 32, 33).

One imported or generated image is an *original*.  Everything made from it - a
variation, an edit, an upscale, an outpaint - is a **child** that points back at
its parent.  Parents are never overwritten, so the tree can always be walked
back to the file the user started with:

    Source -> Variation -> Edit -> Upscale

The graph is not stored in a separate index.  Each image's own metadata carries
``origin`` and ``parent``, so the lineage survives the project being moved,
copied or reopened, and there is no second index to drift out of step with the
files.  Walking the graph only reads side-cars; it opens no pixels.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .metadata import ImageMetadata, read_metadata

__all__ = ["Origin", "ORIGINS", "ORIGIN_LABELS", "VariantNode", "VersionGraph",
           "origin_for_mode"]


class Origin:
    """What an image *is*, in the graph."""

    ORIGINAL = "original"
    IMPORTED = "imported"
    GENERATED = "generated"
    VARIATION = "variation"
    EDIT = "edit"
    UPSCALE = "upscale"
    OUTPAINT = "outpaint"
    INPAINT = "inpaint"


ORIGINS: tuple[str, ...] = (
    Origin.ORIGINAL, Origin.IMPORTED, Origin.GENERATED, Origin.VARIATION,
    Origin.EDIT, Origin.UPSCALE, Origin.OUTPAINT, Origin.INPAINT,
)

#: What each origin is called in the interface.
ORIGIN_LABELS: dict[str, str] = {
    Origin.ORIGINAL: "Original",
    Origin.IMPORTED: "Imported",
    Origin.GENERATED: "Generated",
    Origin.VARIATION: "Variation",
    Origin.EDIT: "Edited",
    Origin.UPSCALE: "Upscaled",
    Origin.OUTPAINT: "Outpainted",
    Origin.INPAINT: "Inpainted",
}

#: The single child relationship each origin creates.  Used to keep a parent's
#: record honest about what came from it.
CHILD_OF: dict[str, str] = {
    Origin.VARIATION: Origin.ORIGINAL,
    Origin.EDIT: Origin.ORIGINAL,
    Origin.UPSCALE: Origin.ORIGINAL,
    Origin.OUTPAINT: Origin.ORIGINAL,
    Origin.INPAINT: Origin.ORIGINAL,
}


def origin_for_mode(mode: str) -> str:
    """The origin a generation mode produces, for the metadata record."""
    from .provider import GenerationMode

    return {
        GenerationMode.TEXT_TO_IMAGE: Origin.GENERATED,
        GenerationMode.IMAGE_TO_IMAGE: Origin.VARIATION,
        GenerationMode.VARIATION: Origin.VARIATION,
        GenerationMode.INPAINT: Origin.INPAINT,
        GenerationMode.OUTPAINT: Origin.OUTPAINT,
        GenerationMode.UPSCALE: Origin.UPSCALE,
    }.get(str(mode or ""), Origin.GENERATED)


@dataclass
class VariantNode:
    """One image in the graph."""

    path: Path
    metadata: Optional[ImageMetadata] = None
    #: Resolved when the whole graph is built.
    parent: Optional["VariantNode"] = None
    children: list = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.children is None:
            self.children = []
        self.path = Path(self.path)

    # -- facts -------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def origin(self) -> str:
        if self.metadata is None:
            return Origin.ORIGINAL
        return str(self.metadata.origin or Origin.ORIGINAL)

    @property
    def label(self) -> str:
        return ORIGIN_LABELS.get(self.origin, self.origin.title())

    @property
    def parent_path(self) -> str:
        if self.metadata is None:
            return ""
        return str(self.metadata.parent or "")

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    @property
    def is_root(self) -> bool:
        return self.parent is None

    @property
    def depth(self) -> int:
        depth = 0
        node: Optional[VariantNode] = self.parent
        seen = {self.path}
        while node is not None and node.path not in seen:
            seen.add(node.path)
            depth += 1
            node = node.parent
        return depth

    def walk(self) -> list["VariantNode"]:
        """This node and every descendant, parents before children."""
        found = [self]
        for child in self.children:
            found.extend(child.walk())
        return found

    def describe(self, indent: int = 0) -> str:
        state = "" if self.exists else "  (file missing)"
        text = (f"{'  ' * indent}- {self.label}: {self.name}"
                f"{state}\n")
        for child in self.children:
            text += child.describe(indent + 1)
        return text

    def to_dict(self) -> dict:
        return {
            "path": str(self.path), "name": self.name, "origin": self.origin,
            "label": self.label, "parent": self.parent_path,
            "exists": self.exists, "depth": self.depth,
            "children": [child.to_dict() for child in self.children],
        }


class VersionGraph:
    """The relationships between a set of images.

    Built from files, not from a stored index, so it cannot disagree with what
    is actually on disk.
    """

    def __init__(self, paths: Iterable[Any] = ()) -> None:
        self.nodes: dict[str, VariantNode] = {}
        for path in paths:
            self.add(path)
        self._link()

    # -- building ----------------------------------------------------------

    def add(self, path: Any, metadata: Optional[ImageMetadata] = None) -> VariantNode:
        key = str(Path(path))
        node = self.nodes.get(key)
        if node is None:
            node = VariantNode(Path(path),
                               metadata if metadata is not None
                               else read_metadata(path))
            self.nodes[key] = node
        return node

    def _link(self) -> None:
        for node in self.nodes.values():
            node.parent = None
            node.children.clear()
        for node in self.nodes.values():
            parent_key = node.parent_path
            if not parent_key:
                continue
            parent = self.nodes.get(parent_key)
            if parent is None or parent is node:
                continue
            node.parent = parent
            if node not in parent.children:
                parent.children.append(node)

    def relink(self) -> None:
        """Re-attach every parent/child link (call after adding nodes)."""
        self._link()

    # -- queries -----------------------------------------------------------

    def roots(self) -> list[VariantNode]:
        return [node for node in self.nodes.values() if node.parent is None]

    def orphans(self) -> list[VariantNode]:
        """Nodes whose recorded parent is not in this set.

        A moved project or a deleted parent produces these; they are reported
        rather than silently re-rooted, because the file really is missing.
        """
        return [node for node in self.nodes.values()
                if node.parent_path and self.nodes.get(node.parent_path) is None]

    def children_of(self, path: Any) -> list[VariantNode]:
        node = self.nodes.get(str(Path(path)))
        return list(node.children) if node is not None else []

    def siblings(self, path: Any) -> list[VariantNode]:
        node = self.nodes.get(str(Path(path)))
        if node is None:
            return []
        if node.parent is not None:
            return [item for item in node.parent.children if item is not node]
        return [item for item in self.roots() if item is not node]

    def lineage(self, path: Any) -> list[VariantNode]:
        """From this image back to its original, oldest first."""
        chain: list[VariantNode] = []
        seen: set[str] = set()
        node = self.nodes.get(str(Path(path)))
        while node is not None and str(node.path) not in seen:
            seen.add(str(node.path))
            chain.append(node)
            node = node.parent
        chain.reverse()
        return chain

    def next_name(self, path: Any, kind: str) -> str:
        """The next free child name: ``photo_variation_1``, ``_2``, ...

        A name that already exists is never returned, so a new variant cannot
        overwrite a sibling.
        """
        target = Path(path)
        stem = target.stem
        suffix = target.suffix or ".png"
        existing = {Path(key).name.lower() for key in self.nodes}
        label = str(kind or Origin.VARIATION).lower().replace(" ", "_")
        index = 1
        while True:
            candidate = f"{stem}_{label}_{index}{suffix}"
            if candidate.lower() not in existing:
                return candidate
            index += 1

    def counts(self) -> dict:
        counts: dict[str, int] = {}
        for node in self.nodes.values():
            counts[node.origin] = counts.get(node.origin, 0) + 1
        return counts

    def describe(self) -> str:
        roots = self.roots()
        if not roots:
            return "No images in this graph."
        text = ""
        for root in roots:
            text += root.describe()
        orphans = self.orphans()
        if orphans:
            text += (f"\n{len(orphans)} image(s) reference a parent that is not "
                     "here (their original may have been moved or deleted).\n")
            for node in orphans:
                text += f"  - {node.name} -> {Path(node.parent_path).name}\n"
        return text

    def to_dict(self) -> dict:
        return {"roots": [root.to_dict() for root in self.roots()],
                "counts": self.counts(),
                "orphans": [node.to_dict() for node in self.orphans()]}
