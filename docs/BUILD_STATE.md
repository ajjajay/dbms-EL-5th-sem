# Interlock — build state

Spec: `C:\Users\AjayG\Desktop\Waste Paper Bin\TEDX\Video\Interlock.pdf` (17 sections).
Last updated: 2026-09-21 (build complete).

**Status: complete.** Every section of the design document is implemented,
including the optional Stage Five. 164 tests pass. The full write-up is
`docs/REPORT.md`; the tolerance measurement is `docs/CALIBRATION.md`.

```
.venv/Scripts/python.exe -m interlock.cli demo     # build + walk all five stages (~1 min)
.venv/Scripts/python.exe -m interlock.cli serve    # review interface
.venv/Scripts/python.exe -m pytest tests -q        # 164 tests, ~2 min
```

---

## Ground rules (from the user — do not relax)

1. **Build every feature, including Stage Five.** Nothing is cut. Correctness
   fixes to the spec are fine; scope cuts are not. — Done.
2. **Never run any git command without asking first.** No commit, no push, no
   branch, no stash. Current branch is `tryone`; main is `main`.
   **Nothing has been committed. The whole build is uncommitted working tree.**
3. `.gitignore` covers venv, databases, blobs, `data/external/`, and secret-like
   files. Keep API keys in `.env`, which is ignored.
4. The old `.docx` in the repo root is untouched, as instructed.
5. Dataset downloads were approved. NIST was downloaded (30 MB, in
   `data/external/`, gitignored). Nothing else was.
6. **NoSQL only where justifiable.** Held to: the document store carries audit
   traces, notification detail, the NL call log and free-form contract metadata,
   and nothing else. SQL remains the system of record.

---

## Environment

- Project venv at `.venv/` (`--system-site-packages`). Run everything as
  `.venv/Scripts/python.exe ...`.
- Geometry kernel: **`cadquery-ocp` (OCCT)**, not FreeCAD. Import is `import OCP.*`.
- FreeCAD 1.1.3 **does exist** (`winget install FreeCAD.FreeCAD`) — an earlier
  note in this file doubted the spec on that point and was wrong. It was
  installed only to verify the STEP export independently
  (`scripts/verify_with_freecad.py`); it is not a dependency.
- Added during this build: `tinydb`, `anthropic`, `python-dotenv`. See
  `requirements.txt`.
- Python 3.10.7, SQLite from stdlib. Shell is Git Bash on Windows; **large bash
  heredocs mangle backslash escapes** — use the Write tool for anything
  non-trivial.

### OCCT notes worth not re-deriving

- `GProp_GProps.MatrixOfInertia()` is about the **centre of mass**.
- `XCAFDoc_ShapeTool`: `GetComponents_s`, `IsAssembly_s`, `GetLocation_s`,
  `GetReferredShape_s`, `GetShape_s` are static (`_s`); **`GetFreeShapes` is an
  instance method.**
- `TDF_Label` has no `EntryDumpToString`; use `TDF_Tool.Entry_s(label, TCollection_AsciiString())`.
- **`STEPCAFControl_Reader.ReadFile` returns `IFSelect_RetDone`, which is 1, not 0.**
- **`label.FindAttribute(...)` on a label with no name attribute crashes the
  interpreter** (access violation, no Python exception). Always test
  `label.IsAttribute(TDataStd_Name.GetID_s())` first. Real AP242 files hit this.
- Writing assemblies: `XCAFApp_Application.GetApplication_s()` →
  `TDocStd_Document` → `ShapeTool.NewShape()` / `AddShape` / `AddComponent`,
  then `UpdateAssemblies()`, then `STEPCAFControl_Writer.Transfer/Write`.
  Instance names set on the *component* label survive the round trip.
- `BRepMesh_IncrementalMesh(shape, deflection, False, angular, True)` then
  `BRep_Tool.Triangulation_s(face, loc)`; a REVERSED face needs two triangle
  indices swapped to be outward-wound.

---

## What exists (≈9 800 lines + 1 100 of tests)

| Path | State |
|---|---|
| `kernel/base.py, occt.py` | STEP/BREP read, XCAF walk with instance names and shared definition keys, face walk, validity gate |
| `geometry/moments.py` | Mesh moments to 3rd order |
| `geometry/invariants.py` | Invariant vector, canonical frame, degeneracy, chirality |
| `geometry/features.py` | Bore merging, hole patterns, planes, Kabsch, best-registration |
| `geometry/quantize.py` | Canonical transform quantisation |
| `geometry/fingerprint.py` | Strict hash + bounded match + **calibrated** tolerances |
| `db/schema.sql` | 21 tables, 3 views, 4 triggers, 27 indexes |
| `db/database.py` | Connection (thread-safe), transactions, blob + mesh store |
| `db/documents.py` | TinyDB store behind a protocol, JSON-lines fallback, outbox drain |
| `model/ingest.py` | Definition-memoised landing, revision reuse, two-stage shape lookup |
| `model/merkle.py` | Attribute-aware hashing, multiset sibling matching |
| `model/contracts.py` | Selectors, interface location, contract hash, 5-step matching |
| `model/commit.py` | The pipeline, ancestor rebuild, classification, notification |
| `model/concurrency.py` | Compare-and-swap, dependency closure, config diff |
| `model/tolerance.py` | Worst case, RSS, Monte Carlo, chain rebinding |
| `model/interference.py` | Prefilter + exact, advisory |
| `query/traversal.py` | Explosion, BOM, where-used, impact, substitution, rollups, config walk |
| `nl/tools.py, agent.py` | 12 read-only typed tools; `claude-opus-5` loop; offline router |
| `web/app.py` | Review interface, 14 routes + JSON API |
| `synthetic/` | CAD toolkit, WINCH-100 assembly, bootstrap |
| `cli.py` | 15 subcommands |
| `tests/` | 164 tests across 6 files |

---

## Known problems from the previous session — all resolved

1. ~~`merkle.diff()` matches children by instance name~~ — **fixed.** Siblings are
   matched as a multiset (identical first, then by part+placement, then by part).
   Test: one bolt moved among forty is found.
2. ~~`ingest._insert_features` writes `through = int(bore.complete)`~~ — **fixed.**
   `complete` and `through` are now separate columns with separate meanings;
   `through` is decided from the mesh extent along the hole axis.
3. ~~`ingest.py` is untested~~ — **fixed.** Rewritten and covered.
4. ~~Cross-tool tolerance is a guess~~ — **fixed.** Measured against 16 NIST cases;
   both tolerances *tightened* (size by 20×). See `docs/CALIBRATION.md`.
5. Fingerprint is not a proof of identity — **unchanged, and documented as such.**
   Moments to 3rd order do not uniquely determine a shape.
6. Fully symmetric parts get chirality 0 and an unstable frame — **unchanged,
   flagged rather than fixed.** There is nothing to fix.
7. The blob store keeps whichever export arrived first — **unchanged, documented.**

### Additional defects found and fixed during this build

- `ReadFile` status compared against 0 instead of `IFSelect_RetDone` (1) —
  rejected every file that read correctly.
- Null-label dereference killing the interpreter on real AP242 files.
- Chirality skewness threshold (5e-6) below the noise floor, making two exports
  of the same part veto each other as a mirrored pair. Now 5e-3.
- Quantisation grid (1 nm / 1 nrad) below exchange jitter, defeating its own
  purpose. Now 1 µm / 10 µrad.
- `locate_pattern` discarded the declared seat direction by forcing it to agree
  with the arbitrarily-signed canonical hole axis.
- Socket matching composed two different frames (`inv(inst.world) @ target.world`
  where `find_path` already returns a relative transform).
- Assembly attributes double-counted in rollups (a 750 W motor inside a
  subassembly declaring 750 W summed to 1500 W).
- **Derived sockets were not re-derived when their source child changed** — the
  bug that made the headline demonstration silently pass.
- **`shape.volume` held the tessellated volume, so every cylindrical part's mass
  was ~0.5% light.** Found by cross-checking against FreeCAD. The kernel's exact
  volume is now stored in `shape.volume_exact` and every mass path reads it;
  identity still uses the mesh volume so the invariant vector stays consistent.

---

## Decisions made

1. **Document store: TinyDB**, behind a `DocumentStore` protocol, with a stdlib
   JSON-lines fallback. Written only by the outbox drain.
2. **Real assembly: none.** The demonstration assembly is generated
   (`synthetic/assembly.py`) rather than scoped from OpenArm or Prusa. Reasons in
   `docs/LICENCES.md`: section 17 prescribes a small fully-specified assembly, a
   generated one is deterministic and carries no licence, and it can contain by
   construction the three cases that are otherwise hard to find (a chiral pair,
   the section 11 worked example, and a hole that can be moved on demand).
   NIST was downloaded and is used for calibration, which is what it is good for.
3. **Chains are authored**, never discovered. Two are authored in the demo: the
   section 11 bolt-grip stack (cross-team, fails worst-case) and a drum
   centreline height (passes RSS, fails worst case — the two methods disagreeing
   is the point).

## Nothing is open

The "Decisions still open" section of the previous revision is resolved. There
are no known unimplemented features and no known incorrect behaviour. The
limitations that remain are inherent and are listed in `docs/REPORT.md` §11
("What is proven, and what is not") rather than here. One wanted *addition* is
recorded below under "Wanted next"; it is a new feature, not a gap in what the
design document asked for.

---

## Wanted next (idea, not built)

**A single "state of the project" view.** Asked for 2026-09-21. Right now the
interface answers questions one part at a time: this part's contract, this
commit's trace, this chain's total. There is no page that puts a whole tree, a
project or a branch in front of you at once and says *what is finished, what is
outstanding, and whose turn it is*.

What it should show, for a root revision or a ref:

- the combined product — the whole configuration in one place, as the thing it
  would actually be built as;
- **what is not done yet**, per part: no contract declared at all; a contract
  that is `auto_drafted` and still `reviewed = 0`; no material or density, so it
  contributes nothing to the mass rollup; still `draft` rather than `released`;
- **what is waiting on a person**: unacknowledged notifications, failing
  tolerance chains, unfilled or unsatisfied sockets, advisory clashes nobody has
  looked at;
- **grouped by team and by designer**, so someone can open it and see their own
  queue rather than the whole product's.

Worth noting that every input already exists, so this is a query-and-presentation
job rather than new machinery: `contract.provenance` / `contract.reviewed`,
`revision.status`, `q.rollup().massless_parts`, `notification.acknowledged`,
`tolerance.evaluate_all()`, the socket stage of `CommitPipeline.check_sockets`,
and `interference.stored()`. The natural shape is one readiness query behind a
`/status` page and an `interlock status` command, with a completeness figure per
team.

The honest framing for the report, if it gets built: this is the metadata tax
(section 17) made visible. The system already refuses a change that breaks a
declared contract; what it cannot yet do is tell you which parts have not
declared enough for anything to be checked in the first place.

---

## If picking this up again

- Read `docs/REPORT.md` first — it is the complete account.
- `docs/CALIBRATION.md` explains why the tolerance constants are what they are.
  Do not change them without re-running `scripts/calibrate_nist.py`.
- The demo is the fastest way to see everything working:
  `python -m interlock.cli demo`.
- **Still uncommitted.** Ask before any git command.
