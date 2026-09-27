"""The tolerance engine (design doc section 11).

A chain is an ordered, signed path of dimensions. The nominals add and subtract
according to direction; the tolerances only ever add. That asymmetry is the
entire subject, and it is why four parts each held to a tenth of a millimetre can
put four tenths of uncertainty onto a gap designed to be two tenths wide.

Three totals, because they answer different questions:

  worst case   every dimension goes wrong at once. Guarantees every assembly
               fits, and is expensive because it forces tight tolerances onto
               parts that did not need them.
  statistical  errors are independent; the root sum of squares. Permits looser
               parts and accepts that a small fraction will not go together.
  Monte Carlo  samples the declared distributions. Agrees with RSS for normal
               parts and is the only one of the three that is right when the
               distributions are not normal, which is the usual case for a
               machined dimension held against a hard limit.

Two things the document does not say, and without which the numbers are
meaningless:

  * **The sigma convention must be recorded.** "+-0.10" might be three sigma
    (the common engineering convention) or one. `dimension.sigma_span` carries
    it, and every report prints it.

    The trap is subtler than it first looks. Reported back at its own span, the
    RSS band is numerically identical under either reading -- the span cancels
    out of `span * sqrt(sum((half_band / span)^2))` -- so two engineers quoting
    "+-0.26" can mean entirely different things and never notice. What differs is
    the risk: under the three-sigma reading each part is held to a third of the
    spread, and this project's demonstration chain goes from failing about one
    assembly in ninety to failing about one in five. That is why the Monte Carlo
    failure rate, not the band, is the number worth arguing about, and why the
    convention is stored per dimension rather than assumed globally.

  * **A one-sided result needs a one-sided statistic.** RSS gives a symmetric
    band around the nominal; a chain whose requirement is "the gap must not go
    negative" is asking for a tail probability, not a band. The Monte Carlo
    method reports that probability directly.

Scope, stated plainly in the report: this is 1-D stacking only. No GD&T, no
bonus tolerance from maximum material condition, no geometric controls. Chains
are authored, never discovered -- automatic discovery needs datum structure that
exchange formats do not carry reliably, and section 2 names it a non-goal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..db.database import Database


@dataclass
class Contributor:
    """One dimension's appearance in a chain."""

    dimension_id: str
    name: str
    part_number: str
    team_id: str
    revision_id: str
    seq: int
    direction: int          # +1 or -1
    nominal: float
    tol_plus: float
    tol_minus: float
    sigma_span: float
    distribution: str
    units: str = "mm"

    @property
    def half_band(self) -> float:
        return 0.5 * (self.tol_plus + self.tol_minus)

    @property
    def bias(self) -> float:
        """A band that is not symmetric moves the mean off the nominal."""
        return 0.5 * (self.tol_plus - self.tol_minus)

    @property
    def sigma(self) -> float:
        """Standard deviation implied by the band and the declared convention.

        For a uniform distribution the band is the full range, so sigma is
        half-width / sqrt(3) and `sigma_span` does not apply; for a triangular
        distribution it is half-width / sqrt(6).
        """
        if self.distribution == "uniform":
            return self.half_band / math.sqrt(3.0)
        if self.distribution == "triangular":
            return self.half_band / math.sqrt(6.0)
        return self.half_band / self.sigma_span

    def sample(self, rng: np.random.Generator, n: int) -> np.ndarray:
        centre = self.nominal + self.bias
        if self.distribution == "uniform":
            return rng.uniform(centre - self.half_band, centre + self.half_band, n)
        if self.distribution == "triangular":
            return rng.triangular(centre - self.half_band, centre, centre + self.half_band, n)
        return rng.normal(centre, self.sigma, n)


@dataclass
class ChainResult:
    chain_id: str
    name: str
    method: str
    nominal: float
    contributors: list[Contributor] = field(default_factory=list)
    # worst case
    worst_low: float = 0.0
    worst_high: float = 0.0
    # statistical
    rss_half_band: float = 0.0
    rss_low: float = 0.0
    rss_high: float = 0.0
    sigma_total: float = 0.0
    sigma_conventions: tuple = ()
    # requirement
    target_low: float | None = None
    target_high: float | None = None
    # monte carlo
    mc_samples: int = 0
    mc_low: float | None = None
    mc_high: float | None = None
    mc_mean: float | None = None
    mc_failure_rate: float | None = None
    teams: tuple = ()
    warnings: list[str] = field(default_factory=list)

    @property
    def cross_team(self) -> bool:
        return len(self.teams) > 1

    def passes(self, method: str | None = None) -> bool:
        """Does the accumulated result satisfy the stated requirement?"""
        method = method or self.method
        if self.target_low is None and self.target_high is None:
            return True
        low, high = self.band(method)
        if self.target_low is not None and low < self.target_low - 1e-12:
            return False
        if self.target_high is not None and high > self.target_high + 1e-12:
            return False
        return True

    def band(self, method: str | None = None) -> tuple[float, float]:
        method = method or self.method
        if method == "worst_case":
            return self.worst_low, self.worst_high
        if method == "monte_carlo" and self.mc_low is not None:
            return self.mc_low, self.mc_high
        return self.rss_low, self.rss_high

    def explain(self, method: str | None = None) -> str:
        """A sentence naming the chain, the total and the requirement it missed."""
        method = method or self.method
        low, high = self.band(method)
        text = (
            f"chain {self.name!r} ({method}): nominal {self.nominal:.3f}, "
            f"accumulated {low:.3f} to {high:.3f} mm"
        )
        if self.target_low is not None or self.target_high is not None:
            lo = "-inf" if self.target_low is None else f"{self.target_low:.3f}"
            hi = "+inf" if self.target_high is None else f"{self.target_high:.3f}"
            text += f"; requirement {lo} to {hi}"
        if not self.passes(method):
            if self.target_low is not None and low < self.target_low:
                text += f" -- short by {self.target_low - low:.3f} mm at the low end"
            if self.target_high is not None and high > self.target_high:
                text += f" -- over by {high - self.target_high:.3f} mm at the high end"
        if self.mc_failure_rate is not None:
            text += f"; Monte Carlo failure rate {self.mc_failure_rate:.3%}"
        if self.cross_team:
            text += f"; crosses teams {', '.join(self.teams)}"
        return text

    def worst_contributor(self) -> Contributor | None:
        """Which dimension to tighten first: the one contributing most variance."""
        if not self.contributors:
            return None
        return max(self.contributors, key=lambda c: c.sigma)


def load_chain(db: Database, chain_ref: str) -> tuple[dict, list[Contributor]]:
    row = db.one("SELECT * FROM chain WHERE chain_id = ? OR name = ?", (chain_ref, chain_ref))
    if row is None:
        raise LookupError(f"no chain named {chain_ref!r}")
    members = db.query(
        """SELECT cm.seq, cm.direction, d.*, p.part_number, p.team_id, r.revision_id
           FROM chain_member cm
           JOIN dimension d ON d.dimension_id = cm.dimension_id
           JOIN revision r  ON r.revision_id = d.revision_id
           JOIN part p      ON p.part_id = r.part_id
           WHERE cm.chain_id = ?
           ORDER BY cm.seq""",
        (row["chain_id"],),
    )
    contributors = [
        Contributor(
            dimension_id=m["dimension_id"], name=m["name"], part_number=m["part_number"],
            team_id=m["team_id"], revision_id=m["revision_id"], seq=m["seq"],
            direction=int(m["direction"]), nominal=float(m["nominal"]),
            tol_plus=float(m["tol_plus"]), tol_minus=float(m["tol_minus"]),
            sigma_span=float(m["sigma_span"]), distribution=m["distribution"], units=m["units"],
        )
        for m in members
    ]
    return dict(row), contributors


def evaluate(
    db: Database,
    chain_ref: str,
    method: str | None = None,
    samples: int = 20000,
    seed: int = 12345,
) -> ChainResult:
    """Total one chain by all three methods; `method` only picks which one the
    pass/fail verdict uses."""
    row, contributors = load_chain(db, chain_ref)
    result = ChainResult(
        chain_id=row["chain_id"], name=row["name"], method=method or row["method"],
        nominal=0.0, contributors=contributors,
        target_low=row["target_low"], target_high=row["target_high"],
    )
    if not contributors:
        result.warnings.append("chain has no members; nothing to total")
        return result

    units = {c.units for c in contributors}
    if len(units) > 1:
        result.warnings.append(f"chain mixes units {sorted(units)}; totals assume they agree")

    nominal = sum(c.direction * c.nominal for c in contributors)
    result.nominal = nominal

    # Worst case: the tolerances add regardless of direction. A negative
    # direction swaps which end of its band is the pessimistic one.
    low = nominal - sum(c.tol_minus if c.direction > 0 else c.tol_plus for c in contributors)
    high = nominal + sum(c.tol_plus if c.direction > 0 else c.tol_minus for c in contributors)
    result.worst_low, result.worst_high = low, high

    # Statistical: variances add. Direction does not matter, because (-1)^2 = 1.
    sigma_total = math.sqrt(sum(c.sigma ** 2 for c in contributors))
    result.sigma_total = sigma_total
    result.sigma_conventions = tuple(sorted({
        f"{c.distribution}/{c.sigma_span:g}sigma" for c in contributors
    }))
    # Reported back at the same span the contributors were declared at, so the
    # statistical band is comparable with the worst case one.
    span = max(c.sigma_span for c in contributors)
    bias = sum(c.direction * c.bias for c in contributors)
    result.rss_half_band = span * sigma_total
    result.rss_low = nominal + bias - result.rss_half_band
    result.rss_high = nominal + bias + result.rss_half_band

    if len({c.sigma_span for c in contributors}) > 1:
        result.warnings.append(
            "contributors declare different sigma conventions "
            f"({', '.join(result.sigma_conventions)}); the statistical total is reported at "
            f"{span:g} sigma"
        )

    rng = np.random.default_rng(seed)
    totals = np.zeros(samples)
    for c in contributors:
        totals += c.direction * c.sample(rng, samples)
    result.mc_samples = samples
    result.mc_mean = float(totals.mean())
    lo_q, hi_q = np.percentile(totals, [0.135, 99.865])   # +-3 sigma equivalent
    result.mc_low, result.mc_high = float(lo_q), float(hi_q)

    failures = np.zeros(samples, dtype=bool)
    if result.target_low is not None:
        failures |= totals < result.target_low
    if result.target_high is not None:
        failures |= totals > result.target_high
    if result.target_low is not None or result.target_high is not None:
        result.mc_failure_rate = float(failures.mean())

    result.teams = tuple(sorted({c.team_id for c in contributors}))
    return result


def evaluate_all(db: Database, method: str | None = None, samples: int = 20000) -> list[ChainResult]:
    """Every chain, cross-team chains first.

    The ranking is the cheap query with the disproportionate payoff from section
    11: a chain inside one team usually has an owner who checks it, and a chain
    crossing a boundary usually does not, because no single person can see the
    whole path.
    """
    rows = db.query("SELECT chain_id FROM chain ORDER BY name")
    results = [evaluate(db, r["chain_id"], method=method, samples=samples) for r in rows]
    results.sort(key=lambda r: (not r.cross_team, r.passes(), r.name))
    return results


def chains_touching(db: Database, revision_ids: set[str]) -> list[str]:
    """Which chains have a member on any of these revisions.

    This is what turns "I changed a part" into "these chains must be re-totalled",
    and it is the join that catches two individually valid commits that jointly
    break a chain.
    """
    if not revision_ids:
        return []
    marks = ",".join("?" * len(revision_ids))
    rows = db.query(
        f"""SELECT DISTINCT c.chain_id FROM chain c
            JOIN chain_member cm ON cm.chain_id = c.chain_id
            JOIN dimension d ON d.dimension_id = cm.dimension_id
            WHERE d.revision_id IN ({marks})""",
        tuple(revision_ids),
    )
    return [r["chain_id"] for r in rows]


def chains_touching_parts(db: Database, part_ids: set[str]) -> list[str]:
    """As above, but by part: a chain is broken by whichever revision of a part is
    current, so the part is the right unit when a commit replaces one."""
    if not part_ids:
        return []
    marks = ",".join("?" * len(part_ids))
    rows = db.query(
        f"""SELECT DISTINCT c.chain_id FROM chain c
            JOIN chain_member cm ON cm.chain_id = c.chain_id
            JOIN dimension d ON d.dimension_id = cm.dimension_id
            JOIN revision r ON r.revision_id = d.revision_id
            WHERE r.part_id IN ({marks})""",
        tuple(part_ids),
    )
    return [r["chain_id"] for r in rows]


def create_chain(
    db: Database,
    name: str,
    members: list[tuple[str, int]],
    target_low: float | None = None,
    target_high: float | None = None,
    method: str = "worst_case",
    owning_team: str | None = None,
    description: str = "",
) -> str:
    """Author a chain: an ordered list of (dimension_id, direction)."""
    from ..db.database import new_id

    chain_id = new_id("chain")
    db.execute(
        """INSERT INTO chain (chain_id, name, description, target_low, target_high, method, owning_team)
           VALUES (?,?,?,?,?,?,?)""",
        (chain_id, name, description, target_low, target_high, method, owning_team),
    )
    for seq, (dimension_id, direction) in enumerate(members, start=1):
        db.execute(
            "INSERT INTO chain_member (chain_id, seq, dimension_id, direction) VALUES (?,?,?,?)",
            (chain_id, seq, dimension_id, int(direction)),
        )
    return chain_id


def rebind_chain_members(db: Database, old_rev: str, new_rev: str) -> int:
    """Point a chain's members at a new revision of the same part.

    Dimensions belong to a revision and revisions are immutable, so a new revision
    carries new dimension rows. Without this a chain would keep totalling the
    superseded geometry and would never notice the change -- which is exactly the
    failure the chain exists to catch.
    """
    moved = 0
    for old in db.query("SELECT dimension_id, name FROM dimension WHERE revision_id = ?", (old_rev,)):
        new = db.one(
            "SELECT dimension_id FROM dimension WHERE revision_id = ? AND name = ?",
            (new_rev, old["name"]),
        )
        if new is None:
            continue
        cur = db.execute(
            "UPDATE chain_member SET dimension_id = ? WHERE dimension_id = ?",
            (new["dimension_id"], old["dimension_id"]),
        )
        moved += cur.rowcount
    return moved
