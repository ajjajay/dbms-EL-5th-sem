# Licences and third-party material

Section 17 of the design document: "Demonstration data taken from open hardware
projects carries real terms. Several prohibit commercial use without permission.
Keep the project non-commercial, and check each source's licence individually
rather than assuming they agree."

This project is coursework and is non-commercial.

## Data that is actually used

### NIST MBE PMI test models — downloaded, used for calibration

| | |
|---|---|
| Source | NIST Model Based Enterprise / PMI Validation and Conformance Testing Project |
| Pages | `nist.gov/ctl/smart-connected-systems-division/smart-connected-manufacturing-systems-group/mbe-pmi-validation` and `.../mbe-pmi-0` |
| Files | `NIST-PMI-STEP-Files.zip` (14 MB), `NIST-MTC-Assembly.zip` (16 MB) |
| Retrieved | 2026-09-21 |
| Stored at | `data/external/nist/` — **gitignored, not redistributed** |
| Terms as published | The models "can be used without any restrictions". Attribution is appreciated. The NIST logo may not be used in any promotional material. NIST does not warrant the files and states they are not reference files free of errors. |
| Used for | Cross-tool fingerprint calibration (`docs/CALIBRATION.md`), and as real-world input for the validity gate |

Attribution, as appreciated: the CAD models and derivative STEP files were
produced by the NIST MBE PMI Validation and Conformance Testing Project.

Only the STEP files were read. The `NIST-MTC-Assembly.zip` archive contains
native NX and SolidWorks parts, which OCCT cannot open and which this project
does not use.

## Data that is generated, not downloaded

The demonstration assembly (**WINCH-100**, 20 distinct parts, four notional
teams) is generated from source by `interlock/synthetic/`, using the same OCCT
kernel that ingestion reads with. It carries no third-party licence, it is
deterministic, and it is rebuilt by `python -m interlock.cli demo` rather than
being committed as binary files.

This was a deliberate choice over scoping a real open-hardware assembly. Section
17 prescribes "one assembly of roughly fifteen to thirty parts, split across
three or four notional teams, with its contracts authored properly", and warns
that a large half-specified assembly demonstrates nothing and costs ten times as
much. A generated assembly also lets the demonstration contain, by construction,
the three cases that are otherwise hard to find: a genuinely chiral pair, the
section 11 worked tolerance example with its exact published numbers, and a hole
that can be moved two millimetres on demand.

## Datasets considered and not used

Licence terms below were read from the sources' own pages on 2026-09-21. None of
these were downloaded.

| Dataset | Terms as stated | Why not used |
|---|---|---|
| **Fusion 360 Gallery Assembly** (Autodesk AI Lab) | Non-commercial research only; must not be redistributed | Its value is ground truth for *mate inference*, which section 2 names an explicit non-goal |
| **ABC Dataset** (NYU) | Copyright remains with the creators; governed by Onshape's Terms of Use, which were not read | ~1 M single parts with no assemblies; dedup at scale is not what this project is short of, and the licence position was not established |
| **Prusa CORE One** | Prusa Open Community License: learning and modification permitted, derivatives share-alike, no selling machines or remixes, and it forbids AI data mining | The AI-data-mining clause is at best ambiguous for a project with a language-model layer, and the download is large |
| **OpenArm hardware** | CERN-OHL-S (per a third-party summary that was not verified at source) | Not needed once the synthetic assembly covered the demonstration; the licence was never confirmed from the primary source |

## Software

| Package | Licence | Role |
|---|---|---|
| `cadquery-ocp` (OCCT bindings) | LGPL-2.1 with an Open CASCADE exception | The geometry kernel. Used at ingest only |
| `numpy`, `scipy` | BSD-3-Clause | Moments, eigen-decomposition, Kabsch, assignment |
| `fastapi`, `uvicorn`, `jinja2` | MIT / BSD | The review interface |
| `tinydb` | MIT | The document store |
| `anthropic` | MIT | The optional natural-language layer |
| `pytest` | MIT | Tests |
| SQLite | Public domain | The system of record (Python standard library) |

**The design document names FreeCAD 1.1.3 as the kernel.** That version is
real — `winget` lists `FreeCAD.FreeCAD 1.1.3` — but it was not installed on the
target machine when the project was written, so this project binds to Open
CASCADE directly through `cadquery-ocp`. OCCT is the kernel FreeCAD itself wraps,
so the geometry is the same; only the binding differs. FreeCAD (LGPL-2.1) was
later installed to independently verify the STEP export
(`scripts/verify_with_freecad.py`); it is a verification tool here, not a
dependency, and nothing in `interlock/` imports it.

## Related work cited, not used

**CADGCL** — Qin et al., *The Visual Computer*, 2025, doi `10.1007/s00371-025-03949-y`.
A learned graph-contrastive embedding for CAD model retrieval. Cited as related
work only: learned embeddings rank similarity well but cannot produce an exact,
reproducible key, which is what a primary key has to be. The argument for a
deterministic fingerprint as the primary key is this project's, not the paper's,
and only the paper's own content is attributed to its authors. The citation
details were taken from the publisher's listing and were not independently
verified against the article.

## No credentials in the repository

`ANTHROPIC_API_KEY` is read from `.env`, which `.gitignore` excludes along with
`*.key`, `*.pem`, `credentials.json` and the rest. The natural-language layer
degrades to an offline router when the key is absent, so nothing in the
demonstration requires one.
