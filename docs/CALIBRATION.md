# Cross-tool fingerprint calibration

The design document (section 5) says the rounding precision "is the tuning work
that makes the feature real, and it is exactly the unglamorous effort nobody has
spent." This is that effort, and its result.

**Reproduce:** `.venv/Scripts/python.exe scripts/calibrate_nist.py`
(writes `docs/nist_calibration.json`; needs the NIST archive in `data/external/`).

## The question

The bounded match decides whether two exports describe the same part. Set the
tolerance too tight and the same model from two CAD systems fails to match; set
it too loose and two genuinely different parts collide, which is a correctness
failure. The tolerance was `2e-3` on both components — a guess, with no
measurement behind it.

## The data

The NIST MBE PMI archive ships each test case as several STEP files written by
*different CAD systems*: an AP203 geometry-only export, an AP203 export carrying
graphical PMI, and one or more AP242 exports. Same nominal model, different
exporters. That is the only evidence available here about how far the invariant
vector moves between tools, and it is why the archive is worth the download.

Sixteen cases were read (CTC 01–05, FTC 06–11, STC 06–10), giving 21 comparable
pairs after the kernel refused the shells and compounds each file also contains.

## The result

| Comparison | n | max size error | max shape error |
|---|---|---|---|
| Same tool, different export flavour (203-geo vs 203-pmi) | 5 | 3.7e-6 | 1.5e-5 |
| **Different tools, same model** (203 vs 242) | 9 | **4.5e-5** | **4.3e-4** |
| Different tools, *different* model | 2 | 1.9e-4 | 2.0e-3 |

The third row is the useful one. Two of the sixteen cases turned out not to be
two exports of one model at all:

- **ctc_03** — 139 faces against 120, volumes 331938 vs 332123 mm³ (5.6e-4 apart)
- **ftc_11** — 6 faces against 42, volumes 5129 vs 5135 mm³

Those differences are an order of magnitude beyond anything the other fourteen
cases show, and they are structural rather than numerical. They are different
revisions of the model, not exporter noise, and a fingerprint that called them
the same part would be wrong to do so.

That leaves a clean gap between "same part, different tool" (≤ 4.5e-5 size,
≤ 4.3e-4 shape) and "different part" (≥ 1.9e-4 size, ≥ 2.0e-3 shape). The
tolerance goes in the gap.

## What was set

```python
CHAR_LENGTH_RELATIVE_TOLERANCE      = 1e-4    # unchanged
DIMENSIONLESS_TOLERANCE             = 1e-4    # unchanged
CROSS_TOOL_CHAR_LENGTH_TOLERANCE    = 1e-4    # was 2e-3  (20x tighter)
CROSS_TOOL_DIMENSIONLESS_TOLERANCE  = 1e-3    # was 2e-3   (2x tighter)
```

Both cross-tool tolerances were **tightened**, which was not the expected
outcome. The old size tolerance of 2e-3 was twenty times looser than it needed
to be, and `tests/test_calibration.py` shows what that cost: a plate with one
hole widened by 0.2 mm differs by 2.3e-3 in size, so under the old setting a
part could have been merged with one whose hole was a fifth of a millimetre
bigger. The tightened value rejects it with a factor of twenty to spare.

### The finding worth repeating

Size and shape do **not** need the same slack. Volume survives an exporter round
trip almost exactly — the median cross-tool size error is 1.1e-5, which is a
hundredth of a percent — while the dimensionless components move roughly ten
times further. That is not noise in the same quantity; it is a different
quantity. `char_length` is `V^(1/3)`, and volume is an integral that both kernels
compute over the same nominal solid. Sphericity and the normalised principal
moments carry surface area and the moment integration, both of which depend on
how the *receiving* kernel reconstructed the surfaces from the exchange file.

So the cross-tool class is *not* "the same tolerance, only looser". It is the
same size tolerance and a ten-times-looser shape tolerance, and it is looser only
where the measurement says it has to be.

## The other thing real data was for

Every failure mode the design document's section 17 warns about showed up, plus
one it does not mention.

**A large fraction of real STEP arrives as something that is not a solid.**
Of the 16 cases, 30 of the bodies read were shells, compounds, or solids of
non-positive volume — including a body reported with volume `-101.324`. The
validity gate (built before this run, and not in the design document) refused
every one of them rather than hashing zeros. Without that stage the fingerprints
would have been meaningless and the failure would have been silent.

**A null-named label crashes the interpreter, not the import.** `TDF_Label`'s
`FindAttribute` dereferences without checking when the label carries no name
attribute, and the process dies with an access violation — no Python exception,
no traceback. Files whose every label happens to be named never expose it, which
is why it survived all the synthetic testing; `nist_ctc_02_asme1_ap242-e2.stp`
found it immediately. The reader now tests `IsAttribute` first.

**`STEPCAFControl_Reader.ReadFile` returns `IFSelect_RetDone`, which is 1.** The
original check compared it against 0 and so rejected every file that read
correctly. Same-kernel round trips had never exercised the path.

**Chirality needed a noise floor.** The third-moment skewness threshold was
`5e-6`, far below what discretisation alone produces. On near-symmetric parts the
sign of a near-zero skewness flipped between exporters, the frame's handedness
flipped with it, and the bounded match then *vetoed* two exports of the same part
as a mirrored pair. Mesh discretisation puts about `deflection/char_length`
(~4e-4) of noise on that quantity and real modelling differences are larger, so
the threshold is now `5e-3`. Below it the axis is reported undetermined and
chirality falls back to the hole pattern or to 0, which the match treats as a
wildcard rather than as a contradiction. The deliberately chiral demonstration
parts sit at 1e-2 to 2e-1, well clear of the floor.

## What this does not establish

- **Two exporters, not many.** The archive's AP203 and AP242 files come from
  different CAD systems, but NIST removed the identifying information, so the
  sample is "several unnamed tools", not a characterised matrix of named ones.
- **One reading kernel.** Every file here was read by OCCT. A different importer
  would reconstruct surfaces differently and move the numbers again; this
  calibrates the exporter side only.
- **Nothing about very small or very thin parts.** The NIST models are 100–500 mm
  across. A 3 mm dowel has a much smaller absolute tolerance at the same relative
  setting, and the relationship was not measured.
- **The gap could close.** Two cases landing an order of magnitude clear of the
  rest is a comfortable margin on this sample; it is not a guarantee that no real
  pair of parts sits inside it. The bounded match is a recall mechanism with a
  stated tolerance, not a proof of identity, and section 5's "sensitivity floor"
  caveat stands.

## Source

NIST Model Based Enterprise / PMI Validation and Conformance Testing Project.
Terms as published: the files "can be used without any restrictions"; attribution
is appreciated and the NIST logo may not be used in promotion. See
`docs/LICENCES.md`.
