# Interlock

**A parts database for machinery built by several teams at once, where a part's
identity comes from its geometry, its relationships are checked rather than
typed, and changing it tells you who you just broke.**

DBMS Elective Lab, 5th semester. Implementation of a 17-section design document.

---

## 1. The problem

Four teams build one machine. Each designs in isolation. The parts meet for the
first time at integration, and something does not fit.

The failures are boringly predictable. A mounting hole moves two millimetres and
nobody downstream is told. The same physical bracket exists in the database under
six part numbers because six people drew it. Two parts are individually within
specification and their combined error is not. The interface document that was
supposed to prevent all of this was written in a word processor and went stale
within a month.

None of this is a modelling problem — the geometry is fine. What is missing is a
system that holds the *relationships between parts* as first-class, checkable
data, and that refuses changes which violate them.

Existing tools split badly. CAD models shapes beautifully and knows nothing about
organisational structure. PLM tracks documents and revisions and knows nothing
about geometry. The gap between them is where the failures live, and it is filled
today by spreadsheets.

### The thesis

Three ideas carry the system. Each is ordinary on its own.

1. **Identity comes from geometry.** A part is identified by a fingerprint
   computed from its shape, not by a filename, a part number, or whoever exported
   it.
2. **Relationships are declared as contracts and checked on write.** A part
   publishes what it connects to. The database validates the connection rather
   than trusting it.
3. **Correctness is enforced in the data layer.** Tolerance chains, mass budgets
   and envelope limits are integrity constraints, not reports someone runs
   afterwards.

---

## 2. What was built

Everything in the design document, including the optional Stage Five, plus five
stages that the document does not have and that turned out to be necessary.

| Module | Lines | What it does |
|---|---|---|
| `db/schema.sql` | 520 | 21 tables, 3 views, 4 triggers, 27 indexes |
| `db/database.py` | 250 | Connection, transactions, content-addressed blob store |
| `db/documents.py` | 230 | Document store behind an interface, plus the outbox |
| `kernel/` | 690 | OCCT adapter; the only code allowed to touch a solid modeller |
| `geometry/` | 1 180 | Moments, invariants, chirality, features, quantisation, fingerprint |
| `model/ingest.py` | 670 | File → rows, two-stage shape lookup |
| `model/merkle.py` | 360 | Assembly hashing and change location by descent |
| `model/contracts.py` | 950 | Declarations, interface location, socket matching |
| `model/commit.py` | 780 | The validation pipeline |
| `model/concurrency.py` | 260 | Compare-and-swap, dependency closure |
| `model/tolerance.py` | 390 | Worst case, RSS, Monte Carlo |
| `model/interference.py` | 240 | Prefilter plus exact test, advisory |
| `query/traversal.py` | 550 | Explosion, where-used, impact, substitution, rollups |
| `nl/` | 800 | Typed read-only tools, model loop, offline router |
| `web/app.py` | 510 | Review interface |
| `synthetic/` | 1 000 | Demonstration assembly generator |
| `cli.py` | 620 | Command line |
| `tests/` | 1 100 | 164 tests |

About 9 800 lines of implementation and 1 100 of tests.

**Run it:**

```
.venv/Scripts/python.exe -m interlock.cli demo     # build everything, walk five stages
.venv/Scripts/python.exe -m interlock.cli serve    # the review interface
.venv/Scripts/python.exe -m pytest tests -q        # 164 tests
```

---

## 3. The data model

The central decision is that **part**, **revision**, **shape** and **occurrence**
are four separate things. Conflating any two is the mistake that makes homemade
parts databases collapse.

| Entity | Means | Key |
|---|---|---|
| `part` | Durable identity — what a part number names | `part_id` |
| `revision` | One frozen state of a part | `revision_id` |
| `shape` | One distinct piece of geometry, shared by any number of revisions | `fingerprint` |
| `occurrence` | One appearance of a child revision inside a parent, with a pose | `occurrence_id` |

Forty identical bolts are **one** part row, **one** shape row, **one** revision
row and **forty** occurrence rows. The demonstration database shows this
directly: 25 parts, 25 revisions, **18** distinct shapes and 78 occurrences.

Two of those numbers are worth pausing on:

- **18 shapes for 19 leaf parts.** `PLATE-A` and `PLATE-B` are geometrically
  identical, so they share one shape row while remaining two separate parts with
  separate part numbers, separate owners and separate dimensions. Deduplication
  collapses the *shape*; it must never collapse the *part*.
- **Quantity, position and context live on the occurrence**, not the part.
  Storing the transform on the part is the tempting shortcut, and it makes both
  a bill of materials and a where-used query impossible.

### Normalisation and the deliberate denormalisation

The schema is in third normal form with two considered exceptions.

**The invariant columns on `shape`** (`char_length`, `sphericity`, `j1..j3`,
`chirality`) are functionally dependent on the fingerprint, which is computed
*from* them. Storing them anyway is the entire reason the cross-tool match is a
range query rather than a hash lookup: a hash cannot express "close enough", and
an index on `char_length` turns what would be a full scan into a handful of
candidates.

**`occurrence.transform_key` and `sort_key`** are derived from the placement
matrix in the same row. They are stored because the Merkle hash is computed over
the quantised form, and recomputing the quantisation on every traversal would
put a floating-point normalisation inside the hot path of every query.

Both are write-once — occurrences are immutable — so there is no update anomaly
to worry about.

### Integrity in the database, not in the application

Four triggers, because a convention nobody enforces is not a guarantee:

```sql
CREATE TRIGGER revision_is_immutable
BEFORE UPDATE OF part_id, revision_index, fingerprint, commit_id, material, density
ON revision
BEGIN SELECT RAISE(ABORT, 'revision rows are immutable: create a new revision'); END;
```

`merkle_hash` and `status` are deliberately excluded — both are assigned as a
commit lands, so the trigger has to permit them while forbidding everything else.

The cycle guard is the one that cannot be a foreign key, because the violation
may only appear after several hops:

```sql
CREATE TRIGGER occurrence_forbids_cycles
BEFORE INSERT ON occurrence
WHEN EXISTS (
    WITH RECURSIVE descendant(rev, depth) AS (
        SELECT NEW.child_rev, 0
        UNION
        SELECT o.child_rev, d.depth + 1 FROM occurrence o
        JOIN descendant d ON o.parent_rev = d.rev WHERE d.depth < 64
    )
    SELECT 1 FROM descendant WHERE rev = NEW.parent_rev
)
BEGIN SELECT RAISE(ABORT, 'cycle rejected: the proposed parent already appears
                            beneath the proposed child'); END;
```

A recursive query *inside a trigger*, with a depth guard that is not optional: a
cycle that somehow got in would otherwise make the guard itself run forever.
Tested at one, two and three hops, and a diamond is correctly not a cycle.

The other two triggers make the commit log append-only and occurrences immutable.

### Indexing

Three indexes carry the load, exactly as section 12 predicts:

```sql
CREATE INDEX idx_occ_down      ON occurrence(parent_rev, child_rev);  -- explosion
CREATE INDEX idx_occ_up        ON occurrence(child_rev, parent_rev);  -- where-used
CREATE INDEX idx_shape_char_length ON shape(char_length);             -- bounded match
```

Measured on the demonstration database:

| Query | Time |
|---|---|
| Full explosion (78 occurrences, depth 3) | 0.5 ms |
| Bill of materials with mass rollup | 0.3 ms |
| Where-used, walking up | 0.3 ms |
| Mass + CG + power rollup (composes transforms) | 2.2 ms |
| Impact (4 joins + upward walk + distinct) | 0.4 ms |
| All tolerance chains (incl. 20 000-sample Monte Carlo) | 12.4 ms |

This is the asymmetry the architecture is built around: everything expensive
happens on write, once, and everything on the read path is cheap. Fingerprinting
a solid costs tens of milliseconds; a query never opens geometry at all.

### The recursive queries

Explosion, with a running quantity product:

```sql
WITH RECURSIVE tree(rev, qty, depth, path) AS (
    SELECT :root, 1, 0, (SELECT part_number FROM ...)
    UNION ALL
    SELECT o.child_rev, t.qty * o.quantity, t.depth + 1,
           t.path || '/' || CASE WHEN o.instance_name <> ''
                                 THEN o.instance_name ELSE p.part_number END
    FROM tree t JOIN occurrence o ON o.parent_rev = t.rev ...
    WHERE t.depth < :guard
)
```

Where-used is the same table read in the other direction, and the bill of
materials is the same traversal with a different aggregate — which is precisely
why extracting mass properties at ingest pays off repeatedly rather than once.

---

## 4. Geometric identity

Section 5 asks one hash to do two incompatible jobs: collapse a part re-saved by
the same tool (where the bytes differ but the numbers are identical) *and* match
the same part exported by a different tool (where the numbers differ in the fifth
decimal and the topology counts differ outright). Hashing is discontinuous;
geometric comparison is continuous. Rounding does not reconcile them — it moves
the failure to the edge of a rounding bucket, where two identical parts still
land on opposite sides.

**Interlock therefore keeps two identities per shape.**

**The strict fingerprint** is a BLAKE2b hash over the rounded invariant vector
plus the face histogram and topology counts. It does what hashes are good at:
constant-time exact deduplication within one tool, including the resave case
ordinary version control gets wrong.

**The bounded match** is a range query over the same quantities stored as
indexed columns, with the discrete counts used as corroboration rather than as a
requirement. It does what the hash cannot: recognise the same part across tools,
within a stated tolerance. It is a database index, not a hash, which is why it
can express "close enough" at all. A hit records a `shape_alias`, so the next
arrival of that export is a single index probe.

### The invariant vector, and three corrections to it

The document proposes hashing volume, area, sorted principal moments, a surface
histogram and topology counts. Three things are wrong with that as stated.

**Mixed dimensions.** Volume scales as L³, area as L², inertia as L⁵. Rounding
them all "to a fixed decimal precision" applies a *different* relative tolerance
to each. The vector is nondimensionalised — characteristic length `V^(1/3)`,
sphericity, and principal second moments normalised by `V^(5/3)` — so one
tolerance means one thing.

**The principal frame is often undefined.** Two equal principal moments make the
eigenvectors arbitrary inside the repeated subspace, and that is the *normal*
case for mechanical parts: washers, shafts, flanges, square plates. Degeneracy is
detected, an attempt is made to break it with the fourth moment (a square plate
has four-fold variation whose maximum picks out a diagonal), and what cannot be
broken is flagged rather than silently used.

**Mirrored parts collide.** Every second-order quantity is mirror-symmetric, so a
left-hand and a right-hand bracket produce identical vectors. The document lists
this as OPEN. Third moments resolve it: reflection preserves the moment along a
reflected axis, so once the eigenvector signs are fixed by skewness the frame's
handedness flips. A hole-pattern determinant is the fallback for parts whose
asymmetry lives entirely in small features. Opposite chirality vetoes a match.

### Assembly hashing

A leaf hashes its geometry; a node hashes the sorted list of its children's
hashes paired with their placements. Sorting makes a node order-independent.
Three additions were required before that property actually holds:

- **Placements must be quantised.** They are floats. An unquantised hash reports
  that every part moved when a re-export perturbs a rotation in the seventh
  decimal, destroying the one property the tree exists to provide. Transforms are
  snapped to common exact values, projected back onto the rotation group, and
  reduced to integers via a canonical quaternion with the sign ambiguity
  resolved. **The grid had to be coarsened by three orders of magnitude** (1e-3 mm
  and 1e-5 rad) — the original 1 nm / 1 nrad grid was *below* exchange-format
  jitter, which defeats the purpose of quantising.
- **The sort must be over something canonical.** Sorting by child name makes the
  hash depend on the exporter's naming.
- **The hash must cover what a configuration *is*, not only its geometry.** A
  leaf hashed on geometry alone cannot see a contract-only change — a mass limit
  tightened, an interface renamed — so the root would stay still while the
  product's public face moved, and concurrency detection built on the root would
  miss exactly the change other teams care about. Each node folds in a digest of
  its contract, material and density.

**Change location** descends only where hashes differ. The document calls this
logarithmic; it is really proportional to the number of changed nodes times their
fan-out, and the property that matters is that a matching subtree is skipped
entirely however large it is.

`diff` matches siblings as a **multiset**, not by name. This was a real bug in
the original implementation: forty bolts sharing an instance name overwrote each
other in a name-keyed dictionary, so a change among repeated parts was silently
lost. Identical children pair off first, then by (part, placement), then by part
alone — so one bolt moved among forty is found and reported as *moved* rather
than as a removal plus an addition.

---

## 5. Contracts, and why placement is not an afterthought

A part has a public face and a private body. Other teams may depend only on the
public face. Changing the body is silent; changing the contract notifies every
dependent team. `contract_hash` drives that classification.

**An interface declaration is a selector, not a coordinate list.** A declaration
says "the four 3.3 mm holes on the underside"; the points are *measured off the
geometry* at ingest. The declaration therefore cannot drift from the part, and a
carried-forward declaration is re-run against changed geometry — which is how
moving a hole becomes a contract change without anyone editing a coordinate.
When a declaration does not match its part, the commit is rejected saying so.
This caught a genuine error in the demonstration assembly itself: a declared
three-hole pattern where the geometry offered two, because the third hole had
merged with an adjacent bore.

**A socket derived from a child's interface is re-derived when that child
changes.** Without this the parent keeps validating against a pattern that no
longer exists — the stale interface document, reimplemented. This was the bug
that made the headline demonstration silently pass.

### Matching, and the step the document dismisses

Section 8 lists five steps and says step five, placement, "is arithmetic and
carries no intellectual weight… treat placement as a consequence."

**That is wrong, and the demonstration proves it.** Consider a bearing mount
whose four holes are all shifted rigidly by two millimetres:

- hole count matches → step 1 passes
- every pairwise spacing is **unchanged** → step 1 passes
- hole diameters are unchanged → step 2 passes
- the envelope is unchanged → step 3 passes
- budgets are unchanged → step 4 passes

Steps one through four — "the actual contribution", per the document — all pass,
and the bolts do not go in. Only measuring the holes against their partners under
the transform the assembly actually applies catches it:

```
FAIL socket 'bearing_fwd' on WINCH-100 is not satisfied by BEARING-BLOCK.foot:
     under the assembly's placement the worst hole is 2.00 mm from its partner;
     the fastener clearance allows 0.60 mm
```

The allowance is computed from the fit class, not guessed: a clearance hole gives
away `radius − fastener_radius` and a tapped hole gives away nothing, so two M6
clearance holes tolerate 0.6 mm of misalignment and two millimetres is a real
failure rather than a rounding matter.

Placement is implemented as **verification**: correspondence search, then Kabsch
alignment with the reflection branch suppressed (so a mirrored pattern fails to
register at all), then a Hungarian assignment of holes to partners measured under
the real transform. The cheap checks still run first — one hole moved is caught
by spacings in microseconds, long before any fit is attempted.

---

## 6. The commit pipeline

Validated as a unit; lands whole or is rejected whole. Cheapest first:

```
concurrency → declaration → apply → classify → envelope → sockets → budgets → chains → swap
  one row      row lookup            hash cmp   one row    joins+fit  traversal  arithmetic  one row
```

**How "whole or nothing" is made true rather than aspirational:** the new
revisions are *written inside the transaction*, the constraints are evaluated
against the database in its proposed state, and a failure raises and rolls the
whole transaction back. The thing validated is exactly the thing that would have
landed — there is no second code path that could disagree with the first. The
geometry kernel runs *before* the transaction opens, because fingerprinting costs
tens of milliseconds and a write transaction is not the place for it.

**Every rejection names the constraint and the other party**, never just
"invalid":

```
[socket] socket.bearing_fwd.pattern: socket 'bearing_fwd' on WINCH-100 is not
satisfied by BEARING-BLOCK.foot: 4 holes; worst pairwise spacing difference
2.000 mm (exceeds the looser tolerance 0.20 mm)   (other party: BEARING-BLOCK, chassis)
```

**Revision immutability has a consequence the document does not draw out.**
An occurrence names a specific child revision and may not be edited, so changing
a leaf forces a new revision of every assembly above it — and of nothing else.
That is section 6's "only the path to the root rehashes", expressed in rows. A
test asserts it exactly: thickening the gearbox cover rehashes
`{WINCH-100, DRIVE-ASSY, GEARBOX, GEARBOX-COVER}` and nothing else.

A second consequence: a chain member names a dimension, which belongs to a
revision. Rebuilding an ancestor must **rebind chain members** onto the new
revision, or the chain quietly keeps totalling superseded geometry and never
notices the change it exists to catch.

### Concurrency

Every commit names the root hash it was written against. If the root still
matches, it lands. If it has moved, the system walks the two trees, collects the
parts that actually changed, and intersects with what this commit depends on.

Two corrections, both load-bearing:

**Tree paths are not the dependency set.** Every commit rehashes the entire path
to the root, so any two commits to the same product share the root and several
ancestors. Intersecting *paths* would call every pair of commits a conflict. The
comparison is over parts that were **directly edited**.

**Disjoint subtrees are not independent.** Section 10's row 2 says a commit in a
different subtree can always be rebased; row 5 says two individually valid
commits can jointly break a chain. Both cannot be true. Row 5 is right, so the
changed set is widened through the things that couple parts across the tree —
shared tolerance chains, budget allocations, subscribed contracts — before the
intersection is taken. Even then a clean rebase is not trusted: the pipeline
re-runs every constraint against the merged state.

Both outcomes are demonstrated:

```
independent:  the root moved (c28d4679 -> 9e9085b0) but the change is independent:
              another commit touched CONTROL-PCB, which this commit neither edits
              nor depends on; landing and rebasing onto the current root
conflicting:  conflicting write: CONTROL-PCB was also changed by dee (team controls)
              since this commit was written against 9e9085b0aefc661f
```

---

## 7. The tolerance engine

A chain is an ordered, signed path of dimensions. Nominals add and subtract;
tolerances only ever add. That asymmetry is the entire subject.

The demonstration assembly contains section 11's worked example built as real
geometry — an M6×32 bolt clamping two 6 mm plates and a 20 mm spacer — so the
engine can be checked against numbers published before it existed:

| | Document says | Computed |
|---|---|---|
| Nominal gap | 0.20 mm | 0.200 |
| Worst case | reaches −0.30 mm | −0.3000 … +0.7000 |
| Statistical | √0.07 = 0.2646, reaching −0.06 | ±0.2646, −0.0646 … +0.4646 |
| Monte Carlo | — | −0.0635 … +0.4634, 1.13 % failure |

**The sigma convention must be recorded, and the trap is subtler than it looks.**
Reported back at its own span, the RSS band is numerically *identical* under a
one-sigma or three-sigma reading — the span cancels out of
`span × √Σ(half_band/span)²` — so two engineers quoting "±0.26" can mean entirely
different things and never notice. What differs is the **risk**: under the
three-sigma reading each part is held to a third of the spread, and this chain
goes from failing about one assembly in ninety to failing about one in five. That
is why `dimension.sigma_span` is stored per dimension and why the Monte Carlo
failure rate, not the band, is the number worth arguing about.

Monte Carlo also handles the case RSS cannot: a requirement like "the gap must
not go negative" asks for a tail probability, not a symmetric band.

**Cross-team chains are ranked first.** A chain inside one team usually has an
owner who checks it; a chain crossing a boundary usually does not, because no
single person can see the whole path. That ranking is one `GROUP BY` with a
disproportionate payoff.

**Scope, stated plainly:** 1-D stacking only. No GD&T, no maximum-material bonus
tolerance, no geometric controls. Chains are authored, never discovered —
automatic discovery needs datum structure that exchange formats do not carry
reliably, and section 2 names it a non-goal.

---

## 8. Polyglot persistence — and the rule that keeps it small

SQLite is the system of record. A document store (TinyDB) holds a deliberately
narrow slice. The rule that decides the split is one sentence:

> **Anything read during commit validation stays in SQL.**

Two stores cannot share one transaction, and section 9 forbids a half-applied
commit. So interface points, budgets, CG windows and chain members — everything a
constraint reads — live in SQL where they can be read inside the transaction that
lands the commit.

An earlier design had put interface point clouds, `cg_window`, datums and budget
attributes in the document store. That was **wrong** and was reversed: socket
matching, CG checks and budget sums all read them during validation.

What is left is genuinely a bad fit for rows: validation traces (variable in
shape — a socket match has different fields from a budget sum — read back whole
for display, never joined), notification detail, the natural-language tool-call
log, and free-form contract metadata like supplier and finish that no constraint
reads.

**Writes go through an outbox.** The commit transaction appends to an `outbox`
table in SQL; a worker drains it afterwards. That ordering is the whole point: the
document store is only written *after* the SQL commit has landed, so a rolled-back
commit leaves no orphaned documents, and a crash between the two leaves
undelivered rows the next drain picks up. Delivery is at-least-once, made
idempotent by a deterministic document key — a test drains twice and asserts no
duplicates.

Geometry blobs are a third store: content-addressed files named by fingerprint,
which is a key-value store by another name. Because the key is semantic and the
payload is not, a revision that changes only metadata stores no new geometry at
all.

TinyDB's limits are real — whole-file rewrite per write, no indexes, no
transactions, unsafe for concurrent writers — and are acceptable *only* because
of how little is stored there, with the outbox worker as its single writer. It
sits behind a `DocumentStore` protocol with a stdlib JSON-lines fallback, so
MongoDB could replace it without touching anything else.

---

## 9. Stage Five surfaces

**Interference** is labelled advisory everywhere, and this is a correction to the
document, which lists it under things "enforced in the data layer". The check is
quadratic and each exact pair test costs a boolean intersection in the kernel. A
write transaction that ran it would hold its locks for minutes. **A check that
cannot run synchronously cannot honestly be called enforcement.** It runs as a
background job against a landed configuration: a bounding-box prefilter over
stored columns (no geometry opened), then an exact intersection only for
survivors. On the demonstration assembly, 120 pairs reduce to 18 after the
prefilter, and all 18 are then cleared by the exact test — parts whose boxes
overlap but whose solids do not, such as the pinion inside the hollow gearbox.
Fasteners are excluded by default: a bolt is *meant* to occupy its hole, and a
report that is ninety per cent false positives is not read.

**Natural language** inverts the tempting design. The model never writes a query.
Recursive traversal with quantity rollup is exactly the shape of query that
generated SQL gets subtly wrong, and a subtly wrong bill of materials is worse
than none. The twelve recursive queries are written as parameterised functions and
exposed as typed tools; the model does intent resolution and nothing else, using
`claude-opus-5` with adaptive thinking.

Three properties beyond what the document asks for:

- **Every tool is read-only.** A language model has no role in deciding whether a
  commit is valid. A test drives every tool with required arguments and asserts
  no row count changes.
- **A dry-run tool** — "would my commit conflict?" — runs the *real* pipeline and
  rolls it back, so the question is answered by the genuine validator rather than
  by a summary of it that could disagree.
- **It degrades to an offline keyword router** over the same tools, so the
  demonstration works with no network and no API key. Blunter answers, identical
  tool surface.

**The review interface** is a reader plus the two things that are hard to see any
other way: why a commit was rejected (the full stage-by-stage trace), and what a
contract change would break. Page rendering never opens geometry — every page is
the query layer's output rendered.

Looking at geometry is served two ways, and the split is the same boundary
again. `/view/{part}` draws a rotatable scene in the browser from the **display
meshes tessellated once at ingest** -- no solid is opened to render it, and one
mesh is sent per distinct shape and instanced by the occurrences that place it,
so 73 placed parts cost 18 meshes. It is accurate in position and dimension and
faceted on curves, which is the right trade for "where is this and what is it".
When the true surfaces matter, `/open/{part}` hands the real B-rep to FreeCAD.

Two routes do touch geometry, and they are the exception that proves the rule:
`/geometry/{part}.step` rebuilds a part or a whole placed configuration from the
content-addressed store and the occurrence table, and `/open/{part}` hands that
file to FreeCAD on this machine. Exporting is not modelling — every solid
written was one the store was given, and the only contribution is the
arrangement, which comes from rows. It doubles as the sharpest test of the store:
a configuration exported, re-ingested and re-hashed comes back with the same
fingerprints and the same root hash. Launching a desktop application is refused
for any client that is not loopback, so binding the server to a wider interface
cannot turn it into a way to start processes.

---

## 10. Corrections to the design document

| # | Document claims | Actual |
|---|---|---|
| 1 | One hash gives exact and tolerant matching | Impossible; two identities — strict hash + bounded range match with aliases |
| 2 | Topology counts are stable across exporters | They are not; corroboration only, never a veto |
| 3 | The principal frame is always defined | Degenerate for symmetric parts; detected, broken by 4th moment, else flagged |
| 4 | Mirrored parts collide (listed OPEN) | Solved: 3rd-moment chirality with a hole-pattern fallback |
| 5 | Merkle siblings stay bit-identical | Only with canonical transform quantisation |
| 6 | Disjoint subtrees are independent | False; expanded through chains, budgets and contracts |
| 7 | Placement is arithmetic, an afterthought | It is the only step that catches a rigidly shifted pattern |
| 8 | Interference is enforced in the data layer | Advisory; a quadratic check cannot run in a write transaction |
| 9 | Matching is "entirely non-geometric" | Needs transform composition; a socket and its filler are in different frames |
| 10 | Shape dedup collapses parts | Shape-level only; part, owner and dimensions stay separate |
| 11 | — (missing) | Validity gate: real STEP is full of shells and zero-volume compounds |
| 12 | — (missing) | The `ref` table: the mutable pointer concurrency actually contends over |
| 13 | Kernel is FreeCAD 1.1.3 | OCCT via `cadquery-ocp` — the kernel FreeCAD wraps. FreeCAD 1.1.3 **does** exist (an earlier note here doubted it); it simply was not installed |
| 14 | All quantities dimensional | Nondimensionalised, so one tolerance is coherent |

**Errors in the document's own text**, found while implementing: §9's
classification table is misaligned (contract unchanged + body unchanged is a
NO-OP, not an internal change — implemented correctly); §11 prints the sum of
squares without the radical (√0.07 = 0.2646 — the value is right, the notation is
not); §15's header says the first four rows were machine-executed but row 4 is
marked EXPECTED while §6 presents that run as real; §10's rows 2 and 5 contradict
each other; and "logarithmic change location" is really proportional to changed
nodes times fan-out.

---

## 11. What is proven, and what is not

**Proven, by tests that re-run** (`pytest tests -q`, 164 tests):

- Mesh moments match the kernel's own mass properties to 1e-6; third moments
  vanish for a symmetric body and flip sign under mirroring
- The fingerprint is invariant to pose, to build order and to re-export with
  shuffled children; it separates a 0.2 mm hole change
- A mirrored bracket gets opposite chirality and a different fingerprint
- Schema triggers catch self-, 2-hop and 3-hop cycles; immutability and
  append-only hold; a failed transaction leaves nothing
- Importing the same assembly twice creates nothing new; one moved bolt among
  forty is found
- Changing a leaf rehashes exactly its path to the root
- The pipeline rejects a missing declaration, a moved hole, a rigidly shifted
  pattern, and an unmeetable mass limit — naming the constraint and the other
  party each time, and writing nothing
- Both concurrency outcomes: independent rebase, and a conflict naming the author
- The tolerance engine reproduces the document's published numbers
- The outbox delivers only after a commit lands, and draining twice does not
  duplicate

**Established on real third-party data** (16 NIST test cases, several CAD
systems — `docs/CALIBRATION.md`): the cross-tool tolerances, which were guesses
and are now measured. Both were **tightened**, the size one by 20×. The run also
exposed three real defects — a status code compared against the wrong constant, a
null-label dereference that killed the interpreter rather than raising, and a
chirality threshold below the noise floor that made two exports of the same part
veto each other as a mirrored pair.

**Audited, not just written.** After the implementation was complete the whole
codebase was swept: `ruff` over every rule class that can indicate a real defect
(`F`, `E9`, `B`, `SIM`, `RUF`) reports **no** undefined names, no syntax errors,
no unused variables and no leaked file handles in `interlock/`; coverage was then
used to find load-bearing code the suite never reached. That found
`canonical_quaternion`, which implements Shepperd's method as four branches
selected by which diagonal entry dominates -- and whose last three branches, used
by every rotation beyond 120 degrees, had never once been executed, because the
demonstration assembly is almost entirely axis-aligned. A sign error there would
have produced silently wrong Merkle hashes. It was tested against 400 random
rotations over the whole rotation group and proved correct, and the test is now
in the suite. Test coverage of `interlock/` is 85%.

**Verified by a second application** (`scripts/verify_with_freecad.py`):
FreeCAD 1.1.3 — a separate program, its own importer, its own OCCT build, and
the exact version the design document names — opens the exported assembly and
independently agrees on every count: 73 placed solids, **18 distinct shapes**
(the deduplication claim, confirmed from outside), 19/19 part names recovered,
zero invalid or open bodies, and a total volume agreeing to 1.45e-14. The same
holds for the re-export and the rejected variants.

That check earned its keep immediately: it found that `shape.volume` was the
*tessellated* volume, so every cylindrical part's mass was about half a percent
light — and mass limits are integrity constraints here. The kernel's exact
volume is now stored alongside in `volume_exact` and is what mass reads;
identity still uses the mesh figure, because the rest of the invariant vector
comes from the same mesh and must stay internally consistent.

**Not proven, and stated rather than hidden:**

- **Cross-tool identity is calibrated, not solved.** The evidence is two unnamed
  exporter families read by one kernel, on parts 100–500 mm across. A different
  *importer* would move the numbers again. FreeCAD reads through OCCT too, so it
  corroborates the export without being a genuinely independent kernel.
- **The fingerprint is not a proof of identity.** Moments to third order do not
  uniquely determine a shape. Topology counts and the face histogram reduce
  collisions within a tool; they cannot eliminate them. Changes below tolerance
  count as the same part, by design.
- **Fully symmetric parts** (a sphere, a plain washer) get chirality 0 and an
  unstable frame. They are flagged, not fixed — there is nothing to fix.
- **`through` on a bore** means "spans the part's full extent along the hole
  axis", which is exact for plates and conservative for an L-bracket. Deciding it
  exactly needs a ray cast; the flag is informational and no constraint reads it.
- **The blob store keeps whichever export arrived first** for a fingerprint. A
  file read back out is geometrically the part but not byte-identical to what a
  later contributor submitted.
- **Scale.** The demonstration is 25 parts. The queries are indexed and the
  traversals are guarded, but nothing here has been run against ten thousand.

---

## 12. Honest assessment of the contribution

Parts management is a crowded field and the individual mechanisms here are not
new. Content-addressed storage, Merkle trees, recursive CTEs, optimistic
concurrency and tolerance stacking are all standard. What is unusual is the
combination: identity derived from geometry, relationships checked rather than
typed, and correctness enforced in the data layer — assembled into one system
where a commit that breaks another team's assumption is refused at write time
with the constraint and the counterparty named.

The parts that took real work were not the parts the design document expected.
Fingerprinting was straightforward; making it survive two exporters was not.
Hashing an assembly was straightforward; making the hash indifferent to
floating-point jitter while still sensitive to a contract change was not. Socket
matching was a join; realising that a rigidly shifted pattern passes every one of
its "intellectually weighty" steps was the finding that justified building the
step the document says to skip.

And the risk section 17 names as the one that matters — the metadata tax — is
real. Twenty parts with properly authored contracts is roughly 250 lines of
declarations. Two hundred parts would be a different project, which is exactly
why the document prescribes scoping to one small assembly and why that advice was
followed.

---

## Appendix — file map

```
interlock/
  kernel/      base.py occt.py                 kernel boundary; nothing else touches a solid
  geometry/    moments.py invariants.py        3rd-order moments, canonical frame, chirality
               features.py quantize.py         bore merging, Kabsch; transform quantisation
               fingerprint.py                  strict hash + bounded match + calibrated tolerances
  db/          schema.sql database.py          21 tables, 4 triggers, 27 indexes; blob store
               documents.py                    document store behind an interface + outbox
  model/       ingest.py merkle.py             file -> rows; assembly hashing and diff
               contracts.py commit.py          declarations, matching; the pipeline
               concurrency.py tolerance.py     compare-and-swap; worst case / RSS / Monte Carlo
               interference.py                 prefilter + exact, advisory
  query/       traversal.py                    explosion, where-used, impact, substitution
  nl/          tools.py agent.py               typed read-only tools; model loop + offline router
  web/         app.py                          review interface
  synthetic/   cad.py assembly.py bootstrap.py demonstration assembly (generated, not shipped)
  cli.py                                       demo, serve, ingest, bom, chains, ask, why, drain
scripts/       calibrate_nist.py               the calibration run
docs/          REPORT.md CALIBRATION.md        this file; the measurement
               LICENCES.md BUILD_STATE.md
tests/         164 tests
```
