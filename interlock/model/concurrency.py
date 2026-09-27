"""Optimistic concurrency (design doc section 10).

Several teams commit at once. The question is which of those commits genuinely
conflict, and the Merkle tree answers it without locking.

Every commit names the root hash it was written against. If the current root
still matches, it lands. If the root has moved, the system does not reject
blindly: it walks the two trees comparing hashes, collects the set of parts that
actually changed, and intersects that with what this commit depends on. An empty
intersection means the two changes are independent and both land.

Two corrections to the document, both of which make the difference between a
mechanism that works and one that rejects everything:

  * **Tree paths are not the dependency set.** Every commit rehashes the entire
    path from its part to the root, so any two commits to the same product share
    the root and several ancestors. Intersecting *paths* would call every pair of
    commits a conflict. The comparison must be over the parts that were
    *directly edited*, which is what `merkle.directly_changed_parts` returns.

  * **Disjoint subtrees are not independent.** Section 10 row 2 says a commit
    touching a different subtree can always be rebased, and row 5 says two
    individually valid commits can jointly break a chain. Both cannot be true.
    Row 5 is right, so the changed set is widened through the things that couple
    parts across the tree -- shared tolerance chains, budget allocations, and
    subscribed contracts -- before the intersection is taken. This is the
    dependency closure.

Even with the closure, a clean rebase is not trusted on its own: the commit
pipeline re-runs every constraint against the merged state afterwards. The
closure decides whether a rebase is worth attempting; the revalidation decides
whether it was correct.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..db.database import Database
from . import merkle


@dataclass
class Verdict:
    can_rebase: bool
    detail: str
    conflicts: list[str] = field(default_factory=list)
    changed_parts: set[str] = field(default_factory=set)
    closure: set[str] = field(default_factory=set)
    other_team: str | None = None
    other_part: str | None = None


def revision_for_root(db: Database, root_hash: str) -> str | None:
    """Find the revision whose subtree hashes to this root.

    The commit log records which commit produced which root, so a base hash can
    be resolved back to the configuration it named even after the ref has moved
    on. Without this, "the root my commit was based on" is a string with nothing
    behind it.
    """
    row = db.one(
        """SELECT r.revision_id FROM revision r
           WHERE r.merkle_hash = ?
           ORDER BY r.revision_index DESC LIMIT 1""",
        (root_hash,),
    )
    return row["revision_id"] if row else None


def dependency_closure(db: Database, part_ids: set[str]) -> tuple[set[str], dict[str, str]]:
    """Widen a set of changed parts to everything coupled to them.

    Three couplings, each of which can carry a break across a tree boundary:

      chains      a part sharing a tolerance chain is affected by a change to
                  any other member, however far apart they sit in the tree;
      budgets     a part under a socket that allocates mass, power or thermal
                  load competes with its siblings for that allocation;
      contracts   a team that subscribed to a contract has declared that it
                  depends on it.

    Returns the widened set and, for each added part, why it was added.
    """
    closure = set(part_ids)
    why: dict[str, str] = {}
    if not part_ids:
        return closure, why

    marks = ",".join("?" * len(part_ids))

    for row in db.query(
        f"""SELECT DISTINCT p2.part_id, c.name AS chain_name
            FROM chain c
            JOIN chain_member cm  ON cm.chain_id = c.chain_id
            JOIN dimension d      ON d.dimension_id = cm.dimension_id
            JOIN revision r       ON r.revision_id = d.revision_id
            JOIN chain_member cm2 ON cm2.chain_id = c.chain_id
            JOIN dimension d2     ON d2.dimension_id = cm2.dimension_id
            JOIN revision r2      ON r2.revision_id = d2.revision_id
            JOIN part p2          ON p2.part_id = r2.part_id
            WHERE r.part_id IN ({marks})""",
        tuple(part_ids),
    ):
        if row["part_id"] not in closure:
            closure.add(row["part_id"])
            why[row["part_id"]] = f"shares tolerance chain {row['chain_name']!r}"

    for row in db.query(
        f"""SELECT DISTINCT p.part_id, s.name AS socket_name
            FROM socket s
            JOIN revision r ON r.revision_id = s.revision_id
            JOIN occurrence o ON o.parent_rev = r.revision_id
            JOIN revision cr ON cr.revision_id = o.child_rev
            JOIN part p ON p.part_id = cr.part_id
            WHERE (s.mass_budget IS NOT NULL OR s.power_budget IS NOT NULL
                   OR s.thermal_budget IS NOT NULL)
              AND r.part_id IN ({marks})""",
        tuple(part_ids),
    ):
        if row["part_id"] not in closure:
            closure.add(row["part_id"])
            why[row["part_id"]] = f"competes for the allocation on socket {row['socket_name']!r}"

    for row in db.query(
        f"""SELECT DISTINCT sub.part_id, sub.team_id
            FROM subscription sub WHERE sub.part_id IN ({marks})""",
        tuple(part_ids),
    ):
        why.setdefault(row["part_id"], f"team {row['team_id']} subscribes to it")

    return closure, why


def analyse(
    db: Database,
    base_root: str,
    current_root: str,
    touched_parts: set[str],
    current_rev: str,
) -> Verdict:
    """Decide whether a commit written against `base_root` can still land.

    Implements section 10's table:

      root unchanged                     -> land immediately
      root moved, disjoint after closure -> land, rebase the hash
      shared part                        -> reject, name both authors
      depended-on contract changed       -> reject, require revalidation
    """
    if base_root == current_root:
        return Verdict(True, "base root matches the current root")

    base_rev = revision_for_root(db, base_root)
    if base_rev is None:
        return Verdict(
            False,
            f"the base root {base_root} names no configuration on record, so what this "
            "commit was written against cannot be established",
            conflicts=["unknown base root"],
        )

    before = merkle.build_from_occurrences(db, base_rev)
    after = merkle.build_from_occurrences(db, current_rev)
    differences = merkle.diff(before, after)
    changed = merkle.directly_changed_parts(differences)

    # What this commit touches, as part ids.
    mine: set[str] = set()
    for number in touched_parts:
        pid = db.scalar("SELECT part_id FROM part WHERE part_number = ?", (number,))
        if pid:
            mine.add(pid)

    closure, why = dependency_closure(db, set(changed))
    overlap = mine & closure
    if not overlap:
        moved = ", ".join(sorted(d.part_number or "?" for d in changed.values())) or "nothing"
        return Verdict(
            True,
            f"the root moved ({base_root} -> {current_root}) but the change is independent: "
            f"another commit touched {moved}, which this commit neither edits nor depends on; "
            "landing and rebasing onto the current root",
            changed_parts={d.part_number for d in changed.values() if d.part_number},
            closure=closure,
        )

    first = sorted(overlap)[0]
    row = db.one(
        """SELECT p.part_number, p.team_id,
                  (SELECT cl.author FROM revision r
                   JOIN commit_log cl ON cl.commit_id = r.commit_id
                   WHERE r.part_id = p.part_id ORDER BY r.revision_index DESC LIMIT 1) AS author
           FROM part p WHERE p.part_id = ?""",
        (first,),
    )
    number = row["part_number"] if row else first
    if first in changed:
        detail = (
            f"conflicting write: {number} was also changed by {row['author']} "
            f"(team {row['team_id']}) since this commit was written against {base_root}"
        )
    else:
        detail = (
            f"indirect conflict: {number} {why.get(first, 'depends on a part that changed')}, "
            f"and that part was changed by {row['author']} (team {row['team_id']}) since "
            f"{base_root}; revalidate against the current root"
        )
    return Verdict(
        False, detail,
        conflicts=[number],
        changed_parts={d.part_number for d in changed.values() if d.part_number},
        closure=closure,
        other_team=row["team_id"] if row else None,
        other_part=number,
    )


def diff_configurations(db: Database, root_a: str, root_b: str) -> list[merkle.Difference]:
    """What changed between two named configurations, by hash descent."""
    rev_a = revision_for_root(db, root_a)
    rev_b = revision_for_root(db, root_b)
    if rev_a is None or rev_b is None:
        missing = root_a if rev_a is None else root_b
        raise LookupError(f"no configuration on record for root {missing}")
    return merkle.diff(
        merkle.build_from_occurrences(db, rev_a),
        merkle.build_from_occurrences(db, rev_b),
    )
