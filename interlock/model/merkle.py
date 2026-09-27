"""Assembly hashing.

Section 6. A leaf hashes its geometry; a node hashes the sorted list of its
children's hashes paired with their placements; the root names the whole product
configuration in sixteen characters.

Sorting the children makes a node order-independent, so re-exporting an assembly
whose parts come out in a different sequence does not change the hash. The
document stops there. Three further things are required before the property
actually holds on real files:

  * Placements must be quantised. They are floats, and an unquantised hash
    reports that every part moved when a re-export perturbs a rotation in the
    seventh decimal. See geometry.quantize.

  * The sort must be over something canonical. Sorting by child name would make
    the hash depend on the exporter's naming; sorting by the quantised
    (hash, placement) pair does not.

  * The hash must cover what a configuration *is*, not only its geometry. A
    leaf hashed on geometry alone cannot see a contract-only change (a mass limit
    tightened, an interface renamed), so the root would stay put while the
    product's public face moved, and concurrency detection built on the root
    would miss exactly the change other teams care about. Each node therefore
    also folds in a digest of its contract, material and density.

Change location (``diff``) matches children as a multiset rather than by name.
Forty bolts sharing an instance name would overwrite each other in a name-keyed
dictionary and changes among them would be lost.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import numpy as np

from ..geometry.quantize import transform_key

HASH_DIGITS = 16


@dataclass
class MerkleNode:
    """One node of a hashed configuration tree."""

    name: str
    node_hash: str
    fingerprint: str | None = None
    revision_id: str | None = None
    part_id: str | None = None
    part_number: str | None = None
    instance_name: str = ""
    transform_key: str = ""
    quantity: int = 1
    children: list["MerkleNode"] = field(default_factory=list)
    attrs: str = ""          # digest of contract, material, density

    @property
    def is_leaf(self) -> bool:
        return not self.children

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def render(self, indent: int = 0, max_depth: int = 12) -> str:
        if indent > max_depth:
            return ""
        pad = "  " * indent
        label = f"{pad}{self.node_hash}  {self.name}"
        if self.quantity > 1:
            label += f"  x{self.quantity}"
        lines = [label]
        for child in self.children:
            lines.append(child.render(indent + 1, max_depth))
        return "\n".join(l for l in lines if l)


def _digest(*parts: bytes) -> str:
    h = hashlib.blake2b(digest_size=HASH_DIGITS // 2)
    for part in parts:
        h.update(len(part).to_bytes(4, "little"))
        h.update(part)
    return h.hexdigest()


def attributes_digest(contract_hash: str | None, material: str | None, density: float | None) -> str:
    """Digest of everything about a revision that is not geometry or structure."""
    dens = "" if density is None else f"{float(density):.9g}"
    return _digest(
        b"interlock/attrs/v1",
        (contract_hash or "").encode("ascii"),
        (material or "").encode("utf-8"),
        dens.encode("ascii"),
    )


def leaf_hash(fingerprint: str, attrs: str = "") -> str:
    """A leaf's identity is its geometry (plus its declared face), nothing else.

    Not its name, not its part number, not its placement. Two occurrences of the
    same shape hash identically wherever they sit, which is what lets the tree
    say that a subassembly is unchanged.
    """
    return _digest(b"interlock/leaf/v2", fingerprint.encode("ascii"), attrs.encode("ascii"))


def node_hash(children: list[tuple[str, str, int]], attrs: str = "") -> str:
    """Hash an assembly node from its children.

    Each child contributes (subtree hash, quantised placement, quantity). The
    list is sorted so the node does not depend on the order the exporter emitted
    its components in.
    """
    payload = [b"interlock/node/v2", attrs.encode("ascii")]
    for child_hash, placement, quantity in sorted(children):
        payload.append(child_hash.encode("ascii"))
        payload.append(placement.encode("ascii"))
        payload.append(str(quantity).encode("ascii"))
    return _digest(*payload)


def build_from_occurrences(db, revision_id: str, depth_guard: int = 64) -> MerkleNode:
    """Compute the configuration tree for a revision straight from the database.

    Memoised on revision id, so a bolt appearing forty times is hashed once.
    """
    memo: dict[str, MerkleNode] = {}

    def clone(cached: MerkleNode, instance: str, placement: str, quantity: int) -> MerkleNode:
        return MerkleNode(
            name=cached.name,
            node_hash=cached.node_hash,
            fingerprint=cached.fingerprint,
            revision_id=cached.revision_id,
            part_id=cached.part_id,
            part_number=cached.part_number,
            instance_name=instance,
            transform_key=placement,
            quantity=quantity,
            children=cached.children,
            attrs=cached.attrs,
        )

    def build(rev: str, instance: str, placement: str, quantity: int, depth: int) -> MerkleNode:
        if depth > depth_guard:
            raise RecursionError(
                f"assembly deeper than {depth_guard} at {rev}; a cycle escaped the trigger"
            )
        cached = memo.get(rev)
        if cached is not None:
            return clone(cached, instance, placement, quantity)

        row = db.one(
            """SELECT r.revision_id, r.fingerprint, r.is_assembly, r.material, r.density,
                      r.part_id, p.part_number, c.contract_hash
               FROM revision r
               JOIN part p ON p.part_id = r.part_id
               LEFT JOIN contract c ON c.revision_id = r.revision_id
               WHERE r.revision_id = ?""",
            (rev,),
        )
        if row is None:
            raise LookupError(f"no such revision: {rev}")
        attrs = attributes_digest(row["contract_hash"], row["material"], row["density"])

        kids = db.query(
            """SELECT o.child_rev, o.instance_name, o.quantity, o.transform_key
               FROM occurrence o
               WHERE o.parent_rev = ?
               ORDER BY o.sort_key, o.occurrence_id""",
            (rev,),
        )

        if not kids:
            fingerprint = row["fingerprint"]
            if fingerprint is None:
                # An assembly with no components yet, or a part with no geometry.
                # Hash its identity so the node is still well defined.
                h = _digest(b"interlock/empty/v1", rev.encode("ascii"), attrs.encode("ascii"))
            else:
                h = leaf_hash(fingerprint, attrs)
            node = MerkleNode(
                name=row["part_number"],
                node_hash=h,
                fingerprint=fingerprint,
                revision_id=rev,
                part_id=row["part_id"],
                part_number=row["part_number"],
                instance_name=instance,
                transform_key=placement,
                quantity=quantity,
                attrs=attrs,
            )
            memo[rev] = node
            return node

        children = [
            build(k["child_rev"], k["instance_name"], k["transform_key"], int(k["quantity"]), depth + 1)
            for k in kids
        ]
        h = node_hash([(c.node_hash, c.transform_key, c.quantity) for c in children], attrs)
        node = MerkleNode(
            name=row["part_number"],
            node_hash=h,
            fingerprint=row["fingerprint"],
            revision_id=rev,
            part_id=row["part_id"],
            part_number=row["part_number"],
            instance_name=instance,
            transform_key=placement,
            quantity=quantity,
            children=children,
            attrs=attrs,
        )
        memo[rev] = node
        return node

    return build(revision_id, "", transform_key(np.eye(4)), 1, 0)


# ------------------------------------------------------------------- descent


@dataclass
class Difference:
    """One place where two configurations disagree."""

    path: str
    kind: str          # added, removed, changed, moved
    left: str | None
    right: str | None
    detail: str = ""
    part_id: str | None = None
    part_number: str | None = None
    left_rev: str | None = None
    right_rev: str | None = None


def _label(node: MerkleNode) -> str:
    return node.instance_name or node.part_number or node.name


def _pair_children(a_kids: list[MerkleNode], b_kids: list[MerkleNode]):
    """Match two sibling lists as multisets.

    Returns (pairs, only_left, only_right). Identical children pair off first,
    which is what makes forty interchangeable bolts cost nothing; the leftovers
    pair by (part, placement) so an in-place edit is reported as an edit, then by
    part alone so a moved component is reported as moved rather than as a removal
    plus an addition.
    """
    left = list(a_kids)
    right = list(b_kids)
    pairs: list[tuple[MerkleNode, MerkleNode]] = []

    def take(keyfn):
        index: dict[object, list[int]] = {}
        for i, node in enumerate(right):
            index.setdefault(keyfn(node), []).append(i)
        used_right: set[int] = set()
        kept_left: list[MerkleNode] = []
        for node in left:
            bucket = index.get(keyfn(node))
            picked = None
            while bucket:
                candidate = bucket.pop(0)
                if candidate not in used_right:
                    picked = candidate
                    break
            if picked is None:
                kept_left.append(node)
            else:
                used_right.add(picked)
                pairs.append((node, right[picked]))
        remaining_right = [n for i, n in enumerate(right) if i not in used_right]
        return kept_left, remaining_right

    left, right = take(lambda n: (n.node_hash, n.transform_key, n.quantity))
    left, right = take(lambda n: (n.part_id or n.name, n.transform_key))
    left, right = take(lambda n: (n.part_id or n.name, n.instance_name))
    left, right = take(lambda n: (n.part_id or n.name,))
    return pairs, left, right


def diff(a: MerkleNode | None, b: MerkleNode | None, path: str = "") -> list[Difference]:
    """Locate changes by descending only where the hashes differ.

    Section 6 calls this logarithmic. It is really proportional to the number of
    changed nodes times their fan-out: at each differing node the children are
    paired, and only the pairs that disagree are entered. A subtree whose hash
    matches is skipped entirely, however large it is, which is the property that
    matters.
    """
    if a is None and b is None:
        return []
    if a is None:
        return [Difference(path or _label(b), "added", None, b.node_hash,
                           part_id=b.part_id, part_number=b.part_number, right_rev=b.revision_id)]
    if b is None:
        return [Difference(path or _label(a), "removed", a.node_hash, None,
                           part_id=a.part_id, part_number=a.part_number, left_rev=a.revision_id)]

    here = path or _label(a)
    if a.node_hash == b.node_hash:
        if a.transform_key != b.transform_key or a.quantity != b.quantity:
            return [Difference(here, "moved", a.node_hash, b.node_hash,
                               "same content, different placement or quantity",
                               part_id=a.part_id, part_number=a.part_number,
                               left_rev=a.revision_id, right_rev=b.revision_id)]
        return []

    if a.is_leaf or b.is_leaf:
        detail = (
            f"{a.fingerprint} -> {b.fingerprint}"
            if a.fingerprint != b.fingerprint
            else "same geometry, different contract or material"
        )
        return [Difference(here, "changed", a.node_hash, b.node_hash, detail,
                           part_id=b.part_id or a.part_id,
                           part_number=b.part_number or a.part_number,
                           left_rev=a.revision_id, right_rev=b.revision_id)]

    out: list[Difference] = []
    pairs, only_left, only_right = _pair_children(a.children, b.children)
    for x, y in pairs:
        if x.node_hash == y.node_hash and x.transform_key == y.transform_key and x.quantity == y.quantity:
            continue
        out.extend(diff(x, y, f"{here}/{_label(y)}"))
    for x in only_left:
        out.extend(diff(x, None, f"{here}/{_label(x)}"))
    for y in only_right:
        out.extend(diff(None, y, f"{here}/{_label(y)}"))

    if a.attrs != b.attrs or not out:
        # The assembly's own contract or material moved, independent of (or as
        # well as) anything beneath it. That is an edit *to the assembly itself*.
        out.append(Difference(here, "changed", a.node_hash, b.node_hash,
                              "assembly's own contract or material differs",
                              part_id=b.part_id or a.part_id,
                              part_number=b.part_number or a.part_number,
                              left_rev=a.revision_id, right_rev=b.revision_id))
    return out


def directly_changed_parts(differences: list[Difference]) -> dict[str, Difference]:
    """The parts that were edited themselves, as opposed to rehashed because a
    descendant was edited. This is the set concurrency cares about: every commit
    rehashes the whole path to the root, so comparing paths would call every pair
    of commits a conflict."""
    out: dict[str, Difference] = {}
    for d in differences:
        if d.part_id:
            out[d.part_id] = d
    return out


def find_nodes(tree: MerkleNode, part_id: str) -> list[MerkleNode]:
    return [n for n in tree.walk() if n.part_id == part_id]
