# Interlock

A parts database for machinery built by several teams at once, where a part's
identity comes from its geometry, its relationships are checked rather than
typed, and changing it tells you who you just broke.

DBMS Elective Lab, 5th semester. Full write-up: **[`docs/REPORT.md`](docs/REPORT.md)**.

---

## Quick start

```bash
# 1. the whole thing: build the demo assembly, walk all five stages
.venv/Scripts/python.exe -m interlock.cli demo

# 2. browse it
.venv/Scripts/python.exe -m interlock.cli serve        # http://127.0.0.1:8000

# 3. the tests
.venv/Scripts/python.exe -m pytest tests -q            # 164 tests, ~2 min
```

`demo` takes about a minute. It generates a 20-part assembly as STEP, ingests it,
authors every contract, then runs the five stages of the design document's
section 16 — printing what each one proves.

## What `demo` shows

| Stage | Demonstrates |
|---|---|
| One — Identity | Import the same assembly twice from different exports; every shape recognised, root hash identical, nothing stored twice |
| Two — Structure | Explosion with quantity rollup, where-used, mass/power/CG rollup |
| Three — Contracts | An internal change lands silently; one hole moved 2 mm is rejected by the cheap spacing check; the *whole pattern* shifted 2 mm passes steps 1–4 and is caught only by the placement fit |
| Four — Teams | A published limit the part cannot meet is refused naming the constraint; two commits on a stale root — one rebases, one conflicts and names the other author |
| Five — Surfaces | Tolerance chains by three methods (reproducing the document's own published numbers), advisory interference, natural-language tool calls |

## Other commands

```bash
# propose a change to an existing part -- the operation the system exists for
python -m interlock.cli commit BASE-PLATE new_plate.step -m "moved a hole" --dry-run
python -m interlock.cli commit BASE-PLATE new_plate.step -m "moved a hole"

# get geometry back out, and look at it in CAD
python -m interlock.cli export WINCH-100 --open   # rebuild as STEP, open in FreeCAD

python -m interlock.cli bom                    # bill of materials with mass rollup
python -m interlock.cli tree                   # the hashed configuration tree
python -m interlock.cli where-used BOLT-M6X20  # who contains this part
python -m interlock.cli impact BASE-PLATE      # who a contract change would break
python -m interlock.cli chains -v              # every tolerance chain, all three methods
python -m interlock.cli interference           # advisory clash check
python -m interlock.cli log                    # the commit log
python -m interlock.cli why <commit>           # a commit's full validation trace
python -m interlock.cli ask "do any chains fail?"
python -m interlock.cli ingest part.step --team chassis
python -m interlock.cli drain                  # outbox -> document store
python -m interlock.cli stats
```

## Setup

The project venv already exists. From scratch:

```bash
python -m venv .venv --system-site-packages
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

`cadquery-ocp` is the geometry kernel (Open CASCADE — the kernel FreeCAD wraps).
It is only used at ingest; no query ever opens a solid.

### Optional: the natural-language layer

Put an API key in `.env` (gitignored):

```
ANTHROPIC_API_KEY=sk-ant-...
```

Without it the layer falls back to a deterministic keyword router over the same
typed tools, so nothing in the demonstration requires a network.

## Layout

```
interlock/kernel/      the only code allowed to touch a solid modeller
interlock/geometry/    moments, invariants, chirality, fingerprint
interlock/db/          schema (21 tables, 4 triggers, 27 indexes), blob store, document store
interlock/model/       ingest, merkle, contracts, commit pipeline, concurrency, tolerance
interlock/query/       recursive traversals: explosion, where-used, impact, substitution
interlock/nl/          typed read-only tools + model loop + offline router
interlock/web/         review interface
interlock/synthetic/   the demonstration assembly, generated rather than shipped
docs/                  REPORT.md, CALIBRATION.md, LICENCES.md, BUILD_STATE.md
```

## Data

Nothing third-party is committed. `data/external/` is gitignored; the NIST test
models used for calibration are downloaded by hand and their terms are recorded
in [`docs/LICENCES.md`](docs/LICENCES.md). The demonstration assembly is
generated from source.
