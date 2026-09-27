"""The review interface (design doc section 3, "Clients: web review").

A reader for the database, plus the two things that are hard to see any other
way: why a commit was rejected, and what a contract change would break. It holds
no state of its own and never opens geometry -- every page here is the query
layer's output rendered, which is the whole point of having reduced geometry to
columns at ingest.

Run:  .venv/Scripts/python.exe -m interlock.cli serve
"""

from __future__ import annotations

import json
import os

from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from jinja2 import DictLoader, Environment, select_autoescape

from ..db.database import Database, json_loads
from ..model import export, interference, merkle, tolerance
from ..model.commit import CommitPipeline
from ..nl.agent import Agent
from ..query import traversal as q

BASE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{% block title %}Interlock{% endblock %}</title>
<style>
:root{--bg:#fbfaf8;--fg:#1a1a1a;--dim:#6b6b6b;--line:#e2ded8;--card:#fff;
--accent:#2f5d8a;--bad:#a8332b;--good:#2f6b46;--warn:#8a6d1f;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#16150f;--fg:#ece8e0;
--dim:#9a958c;--line:#33302a;--card:#1e1c16;--accent:#8fb4d8;--bad:#e08b83;--good:#8fc7a4;--warn:#d6b95f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}
header{border-bottom:1px solid var(--line);padding:14px 16px;display:flex;gap:18px;align-items:baseline;flex-wrap:wrap}
header a{color:var(--fg);text-decoration:none;font-weight:500}
header a:hover{color:var(--accent)}
.brand{font-weight:700;letter-spacing:-.02em;font-size:17px}
.root{margin-left:auto;font-family:var(--mono);font-size:12px;color:var(--dim)}
main{max-width:1100px;margin:0 auto;padding:22px 16px 64px}
h1{font-size:22px;margin:.2em 0 .6em;letter-spacing:-.02em}
h2{font-size:16px;margin:1.6em 0 .5em;letter-spacing:-.01em}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
tr:hover td{background:color-mix(in srgb,var(--accent) 5%,transparent)}
code,.mono{font-family:var(--mono);font-size:12.5px}
a{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:14px 16px;margin:12px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:11px 13px}
.stat b{display:block;font-size:20px;letter-spacing:-.02em}
.stat span{color:var(--dim);font-size:12px}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11.5px;font-weight:600;
border:1px solid currentColor}
.ok{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}.dim{color:var(--dim)}
pre{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto;font-size:12.5px}
input,textarea,button{font:inherit;padding:8px 11px;border:1px solid var(--line);border-radius:7px;
background:var(--card);color:var(--fg)}
button{cursor:pointer;font-weight:600}
button:hover{border-color:var(--accent);color:var(--accent)}
.muted{color:var(--dim);font-size:13.5px}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
.btn{display:inline-block;padding:7px 13px;border:1px solid var(--line);border-radius:7px;
background:var(--card);color:var(--fg);text-decoration:none;font-size:13.5px;font-weight:600}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.flash{border:1px solid var(--good);color:var(--good);border-radius:8px;padding:9px 13px;margin:10px 0;font-size:14px}
.flash.bad{border-color:var(--bad);color:var(--bad)}
.tree{font-family:var(--mono);font-size:12.5px;white-space:pre;overflow:auto}
</style></head><body>
<header>
<a class="brand" href="/">Interlock</a>
<a href="/parts">Parts</a><a href="/commits">Commits</a><a href="/chains">Chains</a>
<a href="/tree">Tree</a><a href="/interference">Interference</a><a href="/ask">Ask</a>
<span class="root">{{ ref_name }} @ {{ root_hash or 'no ref' }}</span>
</header><main>
{% if flash %}<div class="flash {{ 'bad' if flash_bad else '' }}">{{ flash }}</div>{% endif %}
{% block body %}{% endblock %}</main></body></html>"""

PAGES = {
    "base.html": BASE,
    "index.html": """{% extends "base.html" %}{% block body %}
<h1>{{ root_part or 'Interlock' }}</h1>
<p class="muted">Identity from geometry, relationships checked on write, correctness in the data layer.</p>
<div class="actions">
<a class="btn" href="/view/{{ root_rev }}">View in 3D</a>
<a class="btn" href="/open/{{ root_rev }}">Open the assembly in FreeCAD</a>
<a class="btn" href="/geometry/{{ root_rev }}.step">Download STEP</a>
</div>
<div class="grid">
{% for label, value in stats %}<div class="stat"><b>{{ value }}</b><span>{{ label }}</span></div>{% endfor %}
</div>
<h2>Rollup</h2>
<div class="card">
Mass <b>{{ '%.0f'|format(rollup.mass_g) }} g</b> &middot;
power <b>{{ '%.0f'|format(rollup.power_w) }} W</b> &middot;
thermal <b>{{ '%.0f'|format(rollup.thermal_w) }} W</b>
{% if rollup.cg %}<br><span class="muted">Centre of gravity
({{ '%.1f'|format(rollup.cg[0]) }}, {{ '%.1f'|format(rollup.cg[1]) }},
{{ '%.1f'|format(rollup.cg[2]) }}) mm</span>{% endif %}
{% if rollup.massless_parts %}<br><span class="warn">No density on:
{{ rollup.massless_parts|join(', ') }}</span>{% endif %}
</div>
<h2>Bill of materials</h2>
<table><tr><th>Part</th><th>Team</th><th>Qty</th><th>Unit</th><th>Total</th></tr>
{% for r in bom %}<tr>
<td><a href="/part/{{ r.part_number }}">{{ r.part_number }}</a></td><td>{{ r.team_id }}</td>
<td>{{ r.total_qty }}</td>
<td class="mono">{{ '%.1f'|format(r.unit_mass_g or 0) }} g</td>
<td class="mono">{{ '%.1f'|format(r.total_mass_g or 0) }} g</td></tr>{% endfor %}</table>
{% endblock %}""",

    "view.html": """{% extends "base.html" %}{% block body %}
<h1>{{ title }}</h1>
<p class="muted">Drag to rotate &middot; scroll to zoom &middot; right-drag to pan &middot;
click a part to identify it. Rendered from the display meshes stored at ingest;
no solid is opened to draw this.</p>
<div class="actions">
<a class="btn" href="/part/{{ title }}">Part page</a>
<a class="btn" href="/open/{{ revision }}">Open in FreeCAD</a>
<a class="btn" href="/geometry/{{ revision }}.step">Download STEP</a>
<a class="btn" href="?fasteners={{ 0 if fasteners else 1 }}">
{{ 'Hide' if fasteners else 'Show' }} fasteners</a>
</div>
<div id="stage" style="position:relative;height:70vh;min-height:420px;border:1px solid var(--line);
border-radius:9px;overflow:hidden;background:var(--card)">
  <div id="status" style="position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;color:var(--dim);font-size:14px;text-align:center;padding:24px">
  loading geometry&hellip;</div>
  <div id="label" style="position:absolute;left:12px;top:12px;font-family:var(--mono);
  font-size:12.5px;color:var(--fg);background:var(--bg);border:1px solid var(--line);
  border-radius:6px;padding:6px 10px;display:none"></div>
  <div id="legend" style="position:absolute;right:12px;bottom:12px;font-size:12px;
  background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px 10px"></div>
</div>
<script type="importmap">
{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js",
"three/addons/":"https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"}}
</script>
<script type="module">
const status = document.getElementById('status');
const TEAM = {chassis:0x4a7fb5, drivetrain:0xc2703d, standards:0x8a8a8a, controls:0x5f9e6e};
let THREE, OrbitControls;
try {
  THREE = await import('three');
  ({OrbitControls} = await import('three/addons/controls/OrbitControls.js'));
} catch (e) {
  status.innerHTML = 'The 3D library could not be loaded (it comes from a CDN, so this '
    + 'needs a network connection).<br>The STEP download and the FreeCAD button both '
    + 'work offline.';
  throw e;
}

const res = await fetch('/api/mesh/{{ revision }}?fasteners={{ 1 if fasteners else 0 }}');
if (!res.ok) { status.textContent = 'No geometry to display.'; throw new Error('no mesh'); }
const data = await res.json();

const stage = document.getElementById('stage');
const scene = new THREE.Scene();
const dark = matchMedia('(prefers-color-scheme: dark)').matches;
scene.background = new THREE.Color(dark ? 0x1e1c16 : 0xffffff);

const camera = new THREE.PerspectiveCamera(45, stage.clientWidth / stage.clientHeight,
                                           data.span / 500, data.span * 50);
const renderer = new THREE.WebGLRenderer({antialias:true});
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(stage.clientWidth, stage.clientHeight);
stage.appendChild(renderer.domElement);

scene.add(new THREE.AmbientLight(0xffffff, 0.55));
const key = new THREE.DirectionalLight(0xffffff, 1.6);
key.position.set(1, 0.6, 1.4); scene.add(key);
const fill = new THREE.DirectionalLight(0xffffff, 0.5);
fill.position.set(-1, -0.4, -0.8); scene.add(fill);

// One BufferGeometry per distinct shape, reused by every instance that placed it --
// the occurrence model, on the GPU.
const geoms = {};
for (const [fp, m] of Object.entries(data.shapes)) {
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(m.v, 3));
  g.setIndex(m.f);
  g.computeVertexNormals();
  geoms[fp] = g;
}

const centre = new THREE.Vector3(...data.centre);
const root = new THREE.Group();
const seen = new Set();
for (const inst of data.instances) {
  const g = geoms[inst.s];
  if (!g) continue;
  const colour = TEAM[inst.team] ?? 0x999999;
  const mesh = new THREE.Mesh(g, new THREE.MeshLambertMaterial({color: colour}));
  mesh.matrixAutoUpdate = false;
  mesh.matrix.fromArray(inst.m);
  mesh.userData = inst;
  root.add(mesh);
  seen.add(inst.team);
}
root.position.sub(centre);
scene.add(root);

camera.position.set(data.span * 0.9, -data.span * 1.1, data.span * 0.8);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.target.set(0, 0, 0);
camera.up.set(0, 0, 1);              // Z up, as the model is built
controls.update();

document.getElementById('legend').innerHTML = [...seen].sort()
  .map(t => `<span style="display:inline-block;width:10px;height:10px;border-radius:2px;
  background:#${(TEAM[t] ?? 0x999999).toString(16).padStart(6,'0')};margin-right:5px"></span>${t}`)
  .join('<br>');

const label = document.getElementById('label');
const ray = new THREE.Raycaster();
let picked = null;
renderer.domElement.addEventListener('click', (ev) => {
  const r = renderer.domElement.getBoundingClientRect();
  const pointer = new THREE.Vector2(
    ((ev.clientX - r.left) / r.width) * 2 - 1,
    -((ev.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(pointer, camera);
  const hit = ray.intersectObjects(root.children, false)[0];
  if (picked) picked.material.emissive.setHex(0x000000);
  if (!hit) { label.style.display = 'none'; picked = null; return; }
  picked = hit.object;
  picked.material = picked.material.clone();
  picked.material.emissive.setHex(0x333311);
  const d = picked.userData;
  label.innerHTML = `<b>${d.part}</b> &middot; ${d.team}<br>`
    + `<span style="opacity:.7">${d.path}</span><br>`
    + `<a href="/part/${d.part}">open its page &rarr;</a>`;
  label.style.display = 'block';
});

addEventListener('resize', () => {
  camera.aspect = stage.clientWidth / stage.clientHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(stage.clientWidth, stage.clientHeight);
});

status.style.display = 'none';
(function loop(){ requestAnimationFrame(loop); controls.update(); renderer.render(scene, camera); })();
</script>
{% endblock %}""",

    "parts.html": """{% extends "base.html" %}{% block body %}
<h1>Parts</h1>
<table><tr><th>Part</th><th>Team</th><th>Rev</th><th>Material</th><th>Mass</th>
<th>Contract</th><th>Fingerprint</th></tr>
{% for p in parts %}<tr>
<td><a href="/part/{{ p.part_number }}">{{ p.part_number }}</a></td>
<td>{{ p.team_id }}</td><td>{{ p.revision_index }}</td><td>{{ p.material or '' }}</td>
<td class="mono">{{ '%.1f'|format(p.mass_g or 0) }} g</td>
<td>{% if p.provenance == 'auto_drafted' %}<span class="pill warn">auto-drafted</span>
{% elif p.contract_hash %}<span class="pill ok">declared</span>
{% else %}<span class="pill bad">none</span>{% endif %}</td>
<td class="mono dim">{{ (p.fingerprint or '')[:12] }}</td></tr>{% endfor %}</table>
{% endblock %}""",

    "part.html": """{% extends "base.html" %}{% block body %}
<h1>{{ p.part_number }}</h1>
<p class="muted">{{ p.description }} &middot; team {{ p.team_id }} &middot;
revision {{ p.revision_index }} ({{ p.status }}) &middot; {{ p.material or 'no material' }}</p>
<div class="grid">
<div class="stat"><b>{{ '%.1f'|format(p.mass_g or 0) }} g</b><span>mass</span></div>
<div class="stat"><b>{{ '%.0f'|format(p.volume or 0) }}</b><span>volume mm&sup3;</span></div>
<div class="stat"><b>{{ p.n_faces or 0 }}</b><span>faces</span></div>
<div class="stat"><b>{{ p.chirality }}</b><span>chirality</span></div>
</div>
<div class="actions">
<a class="btn" href="/view/{{ p.revision_id }}">View in 3D</a>
<a class="btn" href="/open/{{ p.revision_id }}">Open in FreeCAD</a>
<a class="btn" href="/geometry/{{ p.revision_id }}.step">Download STEP</a>
</div>
<h2>Identity</h2>
<div class="card mono">fingerprint {{ p.fingerprint }}<br>merkle {{ p.merkle_hash }}
{% if p.bbox_dx %}<br>bounds {{ '%.1f'|format(p.bbox_dx) }} &times;
{{ '%.1f'|format(p.bbox_dy) }} &times; {{ '%.1f'|format(p.bbox_dz) }} mm{% endif %}
{% if aliases %}<br><span class="muted">{{ aliases }} cross-tool alias(es) recorded</span>{% endif %}
</div>
<h2>Published contract {% if contract and contract.provenance == 'auto_drafted' %}
<span class="pill warn">auto-drafted, unreviewed</span>{% endif %}</h2>
{% if contract %}<div class="card">
{% if contract.envelope_dx %}Envelope {{ contract.envelope_dx }} &times; {{ contract.envelope_dy }}
&times; {{ contract.envelope_dz }} mm<br>{% endif %}
{% if contract.mass_max %}Mass limit {{ contract.mass_max }} g<br>{% endif %}
{% if attributes %}Attributes <code>{{ attributes }}</code><br>{% endif %}
<span class="mono dim">contract hash {{ contract.contract_hash }}</span>
</div>{% else %}<p class="muted">This revision publishes no contract.</p>{% endif %}
{% if interfaces %}<h2>Interfaces (what it offers)</h2>
<table><tr><th>Name</th><th>Kind</th><th>Holes</th><th>Fastener</th><th>Fit</th><th>Source</th></tr>
{% for i in interfaces %}<tr><td>{{ i.name }}</td><td>{{ i.kind }}</td>
<td>{{ i.hole_count }} &times; {{ '%.1f'|format((i.hole_radius or 0) * 2) }} mm</td>
<td>{{ i.fastener or '' }}</td><td>{{ i.fit_class }}</td>
<td>{% if i.source == 'auto_drafted' %}<span class="warn">auto</span>{% else %}declared{% endif %}</td>
</tr>{% endfor %}</table>{% endif %}
{% if sockets %}<h2>Sockets (what it requires)</h2>
<table><tr><th>Name</th><th>Fills</th><th>Holes</th><th>Mass</th><th>Power</th><th>Derivation</th></tr>
{% for s in sockets %}<tr><td>{{ s.name }}</td><td class="mono">{{ s.fills }}</td>
<td>{{ s.hole_count or '-' }}</td><td>{{ s.mass_budget or '-' }}</td>
<td>{{ s.power_budget or '-' }}</td><td class="dim">{{ s.derivation }}</td></tr>{% endfor %}</table>{% endif %}
{% if dimensions %}<h2>Dimensions</h2>
<table><tr><th>Name</th><th>Nominal</th><th>Tolerance</th><th>Convention</th></tr>
{% for d in dimensions %}<tr><td>{{ d.name }}</td><td class="mono">{{ d.nominal }} {{ d.units }}</td>
<td class="mono">+{{ d.tol_plus }} / -{{ d.tol_minus }}</td>
<td class="dim">{{ d.distribution }}, {{ d.sigma_span }}&sigma;</td></tr>{% endfor %}</table>{% endif %}
<h2>Where used</h2>
{% if used %}<table><tr><th>Assembly</th><th>Team</th><th>Depth</th></tr>
{% for u in used %}<tr><td><a href="/part/{{ u.part_number }}">{{ u.part_number }}</a></td>
<td>{{ u.team_id }}</td><td>{{ u.depth }}</td></tr>{% endfor %}</table>
{% else %}<p class="muted">Not used in the current configuration.</p>{% endif %}
<h2>Impact of a contract change</h2>
{% if impact %}<table><tr><th>Team</th><th>Why</th></tr>
{% for t in impact %}<tr><td>{{ t.team_id }}</td><td class="muted">{{ t.reasons|join('; ') }}</td></tr>
{% endfor %}</table>
{% else %}<p class="muted">No other team depends on this part.</p>{% endif %}
{% if children %}<h2>Contains</h2>
<table><tr><th>Instance</th><th>Part</th><th>Qty</th></tr>
{% for c in children %}<tr><td class="mono">{{ c.instance_name }}</td>
<td><a href="/part/{{ c.part_number }}">{{ c.part_number }}</a></td><td>{{ c.quantity }}</td></tr>
{% endfor %}</table>{% endif %}
<h2>Revisions</h2>
<table><tr><th>Rev</th><th>Status</th><th>Commit</th><th>Author</th><th>When</th></tr>
{% for r in revisions %}<tr><td>{{ r.revision_index }}</td><td>{{ r.status }}</td>
<td><a href="/commit/{{ r.commit_id }}" class="mono">{{ r.commit_id[:10] }}</a></td>
<td>{{ r.author }}</td><td class="dim">{{ r.created_at }}</td></tr>{% endfor %}</table>
{% endblock %}""",

    "commits.html": """{% extends "base.html" %}{% block body %}
<h1>Commit log</h1>
<p class="muted">Append-only. A rejection names the constraint it violated and the other party.</p>
<table><tr><th>Commit</th><th>Verdict</th><th>Author</th><th>Team</th><th>Message</th><th>Root</th></tr>
{% for c in commits %}<tr>
<td><a href="/commit/{{ c.commit_id }}" class="mono">{{ c.commit_id[:10] }}</a></td>
<td>{% if c.verdict == 'landed' %}<span class="pill ok">landed</span>
{% elif c.verdict == 'rejected' %}<span class="pill bad">rejected</span>
{% else %}<span class="pill dim">{{ c.verdict }}</span>{% endif %}</td>
<td>{{ c.author }}</td><td>{{ c.team_id or '' }}</td>
<td>{{ c.message }}{% if c.reason %}<br><span class="bad" style="font-size:12.5px">{{ c.reason }}</span>{% endif %}</td>
<td class="mono dim">{{ (c.new_root or '')[:10] }}</td></tr>{% endfor %}</table>
{% endblock %}""",

    "commit.html": """{% extends "base.html" %}{% block body %}
<h1>Commit <span class="mono">{{ c.commit_id[:12] }}</span></h1>
<p class="muted">{{ c.author }} ({{ c.team_id }}) &middot; {{ c.created_at }} &middot;
{{ c.message }}</p>
<div class="card">
Verdict {% if c.verdict == 'landed' %}<span class="pill ok">landed</span>
{% else %}<span class="pill bad">{{ c.verdict }}</span>{% endif %}
<br><span class="mono dim">{{ c.parent_root or 'none' }} &rarr; {{ c.new_root or 'not applied' }}</span>
{% if c.reason %}<br><br><b class="bad">{{ c.reason }}</b>{% endif %}
</div>
{% if classes %}<h2>Classification</h2>
<table><tr><th>Part</th><th>Contract</th><th>Body</th><th>Class</th></tr>
{% for k in classes %}<tr><td>{{ k.part_number }}</td>
<td>{{ 'changed' if k.contract_changed else 'unchanged' }}</td>
<td>{{ 'changed' if k.body_changed else 'unchanged' }}</td>
<td>{% if k.classification == 'breaking' %}<span class="pill bad">breaking</span>
{% elif k.classification == 'internal' %}<span class="pill ok">internal</span>
{% else %}<span class="pill dim">no-op</span>{% endif %}</td></tr>{% endfor %}</table>{% endif %}
<h2>Validation trace</h2>
<table><tr><th></th><th>Stage</th><th>Constraint</th><th>Detail</th><th>Other party</th></tr>
{% for s in steps %}<tr>
<td>{% if s.passed %}<span class="ok">ok</span>{% else %}<span class="bad">FAIL</span>{% endif %}</td>
<td>{{ s.stage }}</td><td class="mono" style="font-size:12px">{{ s.constraint_name or '' }}</td>
<td>{{ s.detail }}</td>
<td class="dim">{{ s.other_part or '' }}{% if s.other_team %}<br>{{ s.other_team }}{% endif %}</td>
</tr>{% endfor %}</table>
{% if notes %}<h2>Notifications</h2>
<table><tr><th>Team</th><th>Part</th><th>Reason</th><th>Acknowledged</th></tr>
{% for n in notes %}<tr><td>{{ n.team_id }}</td><td>{{ n.part_number }}</td>
<td class="muted">{{ n.reason }}</td>
<td>{% if n.acknowledged %}<span class="ok">yes</span>{% else %}
<form method="post" action="/acknowledge/{{ n.notification_id }}" style="display:inline">
<button>acknowledge</button></form>{% endif %}</td></tr>{% endfor %}</table>{% endif %}
{% endblock %}""",

    "chains.html": """{% extends "base.html" %}{% block body %}
<h1>Tolerance chains</h1>
<p class="muted">Cross-team chains first: a chain inside one team usually has an owner
who checks it, and one crossing a boundary usually does not. 1-D stacking only; no GD&amp;T
or maximum-material bonus tolerance.</p>
{% for c in chains %}
<div class="card">
<h2 style="margin-top:0">{{ c.name }}
{% if c.passes %}<span class="pill ok">passes</span>{% else %}<span class="pill bad">fails</span>{% endif %}
{% if c.cross_team %}<span class="pill warn">crosses {{ c.teams|length }} teams</span>{% endif %}
</h2>
<p class="muted">{{ c.description }}</p>
<table><tr><th>Method</th><th>Low</th><th>High</th><th>Verdict</th></tr>
<tr><td>worst case</td><td class="mono">{{ '%.4f'|format(c.worst_low) }}</td>
<td class="mono">{{ '%.4f'|format(c.worst_high) }}</td>
<td>{% if c.worst_ok %}<span class="ok">ok</span>{% else %}<span class="bad">fails</span>{% endif %}</td></tr>
<tr><td>statistical (RSS)</td><td class="mono">{{ '%.4f'|format(c.rss_low) }}</td>
<td class="mono">{{ '%.4f'|format(c.rss_high) }}</td>
<td>{% if c.rss_ok %}<span class="ok">ok</span>{% else %}<span class="bad">fails</span>{% endif %}</td></tr>
<tr><td>Monte Carlo</td><td class="mono">{{ '%.4f'|format(c.mc_low) }}</td>
<td class="mono">{{ '%.4f'|format(c.mc_high) }}</td>
<td>{% if c.mc_failure_rate is not none %}{{ '%.3f'|format(c.mc_failure_rate * 100) }}% fail{% endif %}</td></tr>
</table>
<p class="muted">Nominal {{ '%.3f'|format(c.nominal) }} mm &middot; requirement
{{ c.target_low if c.target_low is not none else '-inf' }} to
{{ c.target_high if c.target_high is not none else '+inf' }} &middot;
sigma convention {{ c.sigma_conventions|join(', ') }}</p>
<table><tr><th>Seq</th><th>Dimension</th><th>Part</th><th>Team</th><th>Dir</th><th>Nominal</th><th>Tolerance</th></tr>
{% for m in c.members %}<tr><td>{{ m.seq }}</td><td>{{ m.name }}</td>
<td><a href="/part/{{ m.part_number }}">{{ m.part_number }}</a></td><td>{{ m.team_id }}</td>
<td>{{ '+' if m.direction > 0 else '-' }}</td>
<td class="mono">{{ m.nominal }}</td>
<td class="mono">+{{ m.tol_plus }} / -{{ m.tol_minus }}</td></tr>{% endfor %}</table>
{% for w in c.warnings %}<p class="warn">{{ w }}</p>{% endfor %}
</div>{% endfor %}
{% if not chains %}<p class="muted">No chains authored. Chains are authored, never discovered.</p>{% endif %}
{% endblock %}""",

    "tree.html": """{% extends "base.html" %}{% block body %}
<h1>Configuration tree</h1>
<p class="muted">Each node's hash covers its children's hashes, their quantised placements
and its own contract, so two configurations are identical exactly when sixteen characters
match. Click a row to collapse it &middot; drag the background to pan &middot; click a part
name to open it.</p>
<div class="actions">
<button id="expand" class="btn">Expand all</button>
<button id="collapse" class="btn">Collapse to depth 2</button>
<button id="zoomout" class="btn">&minus;</button>
<button id="zoomin" class="btn">+</button>
<input id="filter" placeholder="filter by part name" style="min-width:220px">
<span id="count" class="muted" style="align-self:center"></span>
</div>
<div id="stage" style="position:relative;height:72vh;min-height:420px;border:1px solid var(--line);
border-radius:9px;overflow:auto;background:var(--card);cursor:grab">
  <div id="canvas" style="padding:16px 20px;transform-origin:0 0;width:max-content"></div>
</div>
<style>
.node{display:flex;align-items:center;gap:8px;padding:2px 6px;border-radius:5px;
font-family:var(--mono);font-size:12.5px;white-space:nowrap}
.node:hover{background:color-mix(in srgb,var(--accent) 9%,transparent)}
.node.hit{background:color-mix(in srgb,var(--warn) 22%,transparent)}
.twist{width:13px;text-align:center;color:var(--dim);cursor:pointer;user-select:none}
.twist.leaf{opacity:.25;cursor:default}
.dot{width:9px;height:9px;border-radius:2px;flex:none}
.hash{color:var(--dim)}
.qty{color:var(--warn);font-weight:600}
.kids{margin-left:15px;border-left:1px dotted var(--line);padding-left:9px}
.kids.hidden{display:none}
.node a{text-decoration:none;font-weight:600}
</style>
<script>
const TEAM = {chassis:'#4a7fb5', drivetrain:'#c2703d', standards:'#8a8a8a', controls:'#5f9e6e'};
const canvas = document.getElementById('canvas');
const stage = document.getElementById('stage');
let total = 0;

function draw(node, depth) {
  total++;
  const row = document.createElement('div');
  row.className = 'node';
  const kids = document.createElement('div');
  kids.className = 'kids' + (depth >= 2 && node.kids.length ? ' hidden' : '');

  const twist = document.createElement('span');
  twist.className = 'twist' + (node.kids.length ? '' : ' leaf');
  twist.textContent = node.kids.length ? (kids.classList.contains('hidden') ? '▸' : '▾') : '·';
  if (node.kids.length) twist.onclick = (e) => {
    e.stopPropagation();
    const hidden = kids.classList.toggle('hidden');
    twist.textContent = hidden ? '▸' : '▾';
  };
  row.appendChild(twist);

  const dot = document.createElement('span');
  dot.className = 'dot';
  dot.style.background = TEAM[node.team] || '#999';
  dot.title = node.team || '';
  row.appendChild(dot);

  const hash = document.createElement('span');
  hash.className = 'hash';
  hash.textContent = node.h;
  row.appendChild(hash);

  const link = document.createElement('a');
  link.href = '/part/' + encodeURIComponent(node.part);
  link.textContent = node.inst || node.part;
  link.title = node.part;
  row.appendChild(link);

  if (node.inst && node.inst !== node.part) {
    const real = document.createElement('span');
    real.className = 'muted';
    real.textContent = node.part;
    row.appendChild(real);
  }
  if (node.q > 1) {
    const q = document.createElement('span');
    q.className = 'qty';
    q.textContent = '×' + node.q;
    row.appendChild(q);
  }
  if (!node.kids.length && !node.fp) {
    const no = document.createElement('span');
    no.className = 'muted';
    no.textContent = '(no geometry)';
    row.appendChild(no);
  }
  row.dataset.part = node.part.toLowerCase();

  const wrap = document.createElement('div');
  wrap.appendChild(row);
  for (const k of node.kids) kids.appendChild(draw(k, depth + 1));
  wrap.appendChild(kids);
  return wrap;
}

fetch('/api/tree/{{ revision }}').then(r => r.json()).then(root => {
  canvas.appendChild(draw(root, 0));
  document.getElementById('count').textContent = total + ' nodes';
});

document.getElementById('expand').onclick = () => {
  canvas.querySelectorAll('.kids').forEach(k => k.classList.remove('hidden'));
  canvas.querySelectorAll('.twist:not(.leaf)').forEach(t => t.textContent = '▾');
};
document.getElementById('collapse').onclick = () => {
  canvas.querySelectorAll('.kids .kids').forEach(k => k.classList.add('hidden'));
  canvas.querySelectorAll('.kids .twist:not(.leaf)').forEach(t => t.textContent = '▸');
};

let scale = 1;
const zoom = (by) => {
  scale = Math.min(2.2, Math.max(0.45, scale + by));
  canvas.style.transform = `scale(${scale})`;
};
document.getElementById('zoomin').onclick = () => zoom(0.15);
document.getElementById('zoomout').onclick = () => zoom(-0.15);

document.getElementById('filter').addEventListener('input', (e) => {
  const q = e.target.value.trim().toLowerCase();
  let hits = 0;
  canvas.querySelectorAll('.node').forEach(n => {
    const hit = q && n.dataset.part.includes(q);
    n.classList.toggle('hit', !!hit);
    if (hit) { hits++;
      for (let p = n.parentElement; p && p !== canvas; p = p.parentElement) {
        if (p.classList.contains('kids')) p.classList.remove('hidden');
      }
    }
  });
  document.getElementById('count').textContent =
    q ? `${hits} of ${total} nodes match` : `${total} nodes`;
});

// Drag the background to pan, which is what a big tree actually needs.
let dragging = false, sx = 0, sy = 0, sl = 0, st = 0;
stage.addEventListener('mousedown', (e) => {
  if (e.target.closest('a, button, input, .twist')) return;
  dragging = true; sx = e.clientX; sy = e.clientY;
  sl = stage.scrollLeft; st = stage.scrollTop;
  stage.style.cursor = 'grabbing'; e.preventDefault();
});
addEventListener('mousemove', (e) => {
  if (!dragging) return;
  stage.scrollLeft = sl - (e.clientX - sx);
  stage.scrollTop = st - (e.clientY - sy);
});
addEventListener('mouseup', () => { dragging = false; stage.style.cursor = 'grab'; });
</script>
{% endblock %}""",

    "interference.html": """{% extends "base.html" %}{% block body %}
<h1>Interference <span class="pill warn">advisory</span></h1>
<p class="muted">{{ summary }}</p>
<div class="card muted">This is advice, never a veto. The check is quadratic and each exact
test costs a boolean intersection in the kernel, so it cannot run inside a write transaction,
and a check that cannot run synchronously cannot honestly be called enforcement.
Fasteners are excluded: a bolt is meant to occupy its hole.</div>
{% if clashes %}<table><tr><th>Kind</th><th>A</th><th>B</th><th>Volume</th><th>Teams</th></tr>
{% for c in clashes %}<tr>
<td>{% if c.kind == 'clash' %}<span class="pill bad">clash</span>
{% else %}<span class="pill warn">clearance</span>{% endif %}</td>
<td class="mono">{{ c.path_a }}</td><td class="mono">{{ c.path_b }}</td>
<td class="mono">{{ '%.1f'|format(c.volume) }} mm&sup3;</td>
<td class="dim">{{ c.team_a }} / {{ c.team_b }}</td></tr>{% endfor %}</table>
{% else %}<p class="ok">No interference found.</p>{% endif %}
{% endblock %}""",

    "ask.html": """{% extends "base.html" %}{% block body %}
<h1>Ask</h1>
<p class="muted">{{ status }}</p>
<form method="post" action="/ask">
<input name="question" value="{{ question or '' }}" placeholder="what breaks if I move a hole on the base plate?"
style="width:70%" autofocus>
<button>Ask</button></form>
<div class="card muted" style="margin-top:14px">The model chooses which typed tool to call and
with what arguments. It never writes a query, and every tool it can reach is read-only, so the
answer is auditable: the calls it made are listed below it.</div>
{% if answer %}
<h2>Tool calls</h2>
{% if answer.calls %}<table><tr><th>Tool</th><th>Arguments</th></tr>
{% for c in answer.calls %}<tr><td class="mono">{{ c.tool }}</td>
<td class="mono dim">{{ c.arguments }}</td></tr>{% endfor %}</table>
{% else %}<p class="muted">No tool was called.</p>{% endif %}
<h2>Answer</h2><pre>{{ answer.text }}</pre>
{% if answer.error %}<p class="warn">{{ answer.error }}</p>{% endif %}
{% endif %}
<h2>Try</h2><ul class="muted">
<li>what is in the winch?</li><li>who uses BOLT-M6X20?</li>
<li>do any tolerance chains fail, and by how much?</li>
<li>who do I break if I change BASE-PLATE's contract?</li>
<li>what does BEARING-BLOCK publish?</li></ul>
{% endblock %}""",
}


def create_app(db_path: str | os.PathLike | None = None, ref: str = "main") -> FastAPI:
    db = Database(db_path) if db_path else Database()
    env = Environment(loader=DictLoader(PAGES), autoescape=select_autoescape(["html"]))
    app = FastAPI(title="Interlock")
    pipeline = CommitPipeline(db)
    agent = Agent(db, pipeline=pipeline)

    def head():
        row = db.get_ref(ref)
        return row

    def render(name: str, **ctx) -> HTMLResponse:
        row = head()
        ctx.setdefault("ref_name", ref)
        ctx.setdefault("root_hash", row["root_hash"] if row else None)
        ctx.setdefault("flash", None)
        ctx.setdefault("flash_bad", False)
        return HTMLResponse(env.get_template(name).render(**ctx))

    def local_only(request: Request) -> str | None:
        """Launching a desktop application is a local convenience, not a service.

        The review server binds to loopback by default, but that is a default,
        not a guarantee -- so the check is on who is actually asking. Anything
        that is not this machine gets the file to download instead.
        """
        client = request.client.host if request.client else ""
        if client in ("127.0.0.1", "::1", "localhost"):
            return None
        return (f"opening a CAD application is only available to the machine running "
                f"the server; {client or 'this client'} can download the STEP instead")

    @app.get("/", response_class=HTMLResponse)
    def index(flash: str | None = None, bad: int = 0):
        row = head()
        if row is None:
            return HTMLResponse(
                "<h1>Interlock</h1><p>No configuration loaded. Run "
                "<code>python -m interlock.cli demo</code> first.</p>"
            )
        root = row["revision_id"]
        s = db.stats()
        stats = [
            ("parts", s["parts"]), ("revisions", s["revisions"]), ("distinct shapes", s["shapes"]),
            ("occurrences", s["occurrences"]), ("contracts", s["contracts"]),
            ("chains", s["chains"]), ("commits", s["commits"]),
            ("blob MB", f"{s['blob_bytes'] / 1e6:.1f}"),
        ]
        return render(
            "index.html", stats=stats, bom=q.bill_of_materials(db, root),
            rollup=q.rollup(db, root),
            root_rev=root, flash=flash, flash_bad=bool(bad),
            root_part=db.scalar(
                "SELECT p.part_number FROM revision r JOIN part p ON p.part_id = r.part_id "
                "WHERE r.revision_id = ?", (root,)),
        )

    @app.get("/parts", response_class=HTMLResponse)
    def parts():
        rows = db.query(
            """SELECT p.part_number, p.team_id, r.revision_index, r.material, r.fingerprint,
                      s.volume * r.density AS mass_g, c.contract_hash, c.provenance
               FROM part p
               JOIN revision r ON r.part_id = p.part_id
               JOIN (SELECT part_id, MAX(revision_index) t FROM revision GROUP BY part_id) x
                    ON x.part_id = r.part_id AND x.t = r.revision_index
               LEFT JOIN shape s ON s.fingerprint = r.fingerprint
               LEFT JOIN contract c ON c.revision_id = r.revision_id
               ORDER BY p.team_id, p.part_number""")
        return render("parts.html", parts=[dict(r) for r in rows])

    @app.get("/part/{number}", response_class=HTMLResponse)
    def part(number: str, flash: str | None = None, bad: int = 0):
        rev = q.resolve_revision(db, number)
        p = dict(db.one(
            """SELECT r.*, p.part_number, p.team_id, p.description, p.part_id,
                      s.volume, s.n_faces, s.chirality, s.bbox_dx, s.bbox_dy, s.bbox_dz
               FROM revision r JOIN part p ON p.part_id = r.part_id
               LEFT JOIN shape s ON s.fingerprint = r.fingerprint
               WHERE r.revision_id = ?""", (rev,)))
        p["mass_g"] = (p["volume"] or 0) * (p["density"] or 0)
        contract = db.one("SELECT * FROM contract WHERE revision_id = ?", (rev,))
        impact = q.impact(db, number)
        return render(
            "part.html", p=p, flash=flash, flash_bad=bool(bad),
            contract=dict(contract) if contract else None,
            attributes=json.dumps(json_loads(contract["attributes"], {})) if contract else "",
            aliases=db.scalar("SELECT COUNT(*) FROM shape_alias WHERE fingerprint = ?",
                              (p["fingerprint"],)) if p["fingerprint"] else 0,
            interfaces=[dict(r) for r in db.query(
                "SELECT * FROM interface WHERE revision_id = ? ORDER BY name", (rev,))],
            sockets=[dict(r) for r in db.query(
                "SELECT * FROM socket WHERE revision_id = ? ORDER BY name", (rev,))],
            dimensions=[dict(r) for r in db.query(
                "SELECT * FROM dimension WHERE revision_id = ? ORDER BY name", (rev,))],
            used=q.where_used(db, number),
            impact=list(impact.values()),
            children=[dict(r) for r in db.query(
                """SELECT o.instance_name, o.quantity, p.part_number FROM occurrence o
                   JOIN revision r ON r.revision_id = o.child_rev
                   JOIN part p ON p.part_id = r.part_id
                   WHERE o.parent_rev = ? ORDER BY o.occurrence_id""", (rev,))],
            revisions=[dict(r) for r in db.query(
                """SELECT r.revision_index, r.status, r.commit_id, cl.author, cl.created_at
                   FROM revision r LEFT JOIN commit_log cl ON cl.commit_id = r.commit_id
                   WHERE r.part_id = ? ORDER BY r.revision_index DESC""", (p["part_id"],))],
        )

    @app.get("/commits", response_class=HTMLResponse)
    def commits():
        return render("commits.html", commits=[dict(r) for r in db.query(
            "SELECT * FROM commit_log ORDER BY created_at DESC, rowid DESC LIMIT 200")])

    @app.get("/commit/{commit_id}", response_class=HTMLResponse)
    def commit(commit_id: str):
        c = db.one("SELECT * FROM commit_log WHERE commit_id = ?", (commit_id,))
        if c is None:
            return HTMLResponse("<h1>No such commit</h1>", status_code=404)
        return render(
            "commit.html", c=dict(c),
            steps=[dict(r) for r in db.query(
                "SELECT * FROM validation WHERE commit_id = ? ORDER BY validation_id", (commit_id,))],
            classes=[dict(r) for r in db.query(
                """SELECT cc.*, p.part_number FROM change_classification cc
                   JOIN part p ON p.part_id = cc.part_id WHERE cc.commit_id = ?""", (commit_id,))],
            notes=[dict(r) for r in db.query(
                """SELECT n.*, p.part_number FROM notification n
                   JOIN part p ON p.part_id = n.part_id WHERE n.commit_id = ?""", (commit_id,))],
        )

    @app.post("/acknowledge/{notification_id}")
    def acknowledge(notification_id: int):
        from fastapi.responses import RedirectResponse

        row = db.one("SELECT commit_id FROM notification WHERE notification_id = ?",
                     (notification_id,))
        db.execute(
            """UPDATE notification SET acknowledged = 1, acknowledged_at = datetime('now'),
               acknowledged_by = 'web' WHERE notification_id = ?""", (notification_id,))
        return RedirectResponse(f"/commit/{row['commit_id']}" if row else "/commits", 303)

    @app.get("/chains", response_class=HTMLResponse)
    def chains():
        out = []
        for r in tolerance.evaluate_all(db):
            row = db.one("SELECT description FROM chain WHERE chain_id = ?", (r.chain_id,))
            out.append({
                "name": r.name, "description": row["description"] if row else "",
                "passes": r.passes(), "cross_team": r.cross_team, "teams": list(r.teams),
                "worst_low": r.worst_low, "worst_high": r.worst_high,
                "worst_ok": r.passes("worst_case"),
                "rss_low": r.rss_low, "rss_high": r.rss_high, "rss_ok": r.passes("rss"),
                "mc_low": r.mc_low or 0, "mc_high": r.mc_high or 0,
                "mc_failure_rate": r.mc_failure_rate,
                "nominal": r.nominal, "target_low": r.target_low, "target_high": r.target_high,
                "sigma_conventions": list(r.sigma_conventions),
                "members": [vars(m) for m in r.contributors], "warnings": r.warnings,
            })
        return render("chains.html", chains=out)

    @app.get("/tree", response_class=HTMLResponse)
    def tree(revision: str | None = None):
        row = head()
        if row is None and not revision:
            return HTMLResponse("<h1>No configuration</h1>")
        return render("tree.html", revision=revision or row["revision_id"])

    @app.get("/api/tree/{revision}")
    def api_tree(revision: str):
        """The hashed configuration tree as nested JSON.

        Built by the same `build_from_occurrences` the concurrency machinery
        uses, so what is drawn here is the tree the system actually reasons
        about, not a second rendering of the same rows.
        """
        try:
            rev = q.resolve_revision(db, revision)
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

        teams: dict[str, str] = {}

        def team_of(part_id: str | None) -> str:
            if not part_id:
                return ""
            if part_id not in teams:
                teams[part_id] = db.scalar(
                    "SELECT team_id FROM part WHERE part_id = ?", (part_id,)) or ""
            return teams[part_id]

        def pack(node) -> dict:
            return {
                "h": node.node_hash,
                "part": node.part_number or node.name,
                "inst": node.instance_name,
                "q": node.quantity,
                "team": team_of(node.part_id),
                "rev": node.revision_id,
                "fp": node.fingerprint,
                "kids": [pack(c) for c in node.children],
            }

        return JSONResponse(pack(merkle.build_from_occurrences(db, rev)))

    @app.get("/interference", response_class=HTMLResponse)
    def interference_page(exact: bool = False):
        row = head()
        if row is None:
            return HTMLResponse("<h1>No configuration</h1>")
        rep = interference.check(db, row["revision_id"], exact=exact, store=False)
        return render("interference.html", summary=rep.summary(), clashes=rep.clashes)

    @app.get("/ask", response_class=HTMLResponse)
    def ask_get():
        return render("ask.html", status=agent.status(), answer=None, question=None)

    @app.post("/ask", response_class=HTMLResponse)
    def ask_post(question: str = Form(...)):
        return render("ask.html", status=agent.status(),
                      answer=agent.ask(question), question=question)

    # A small JSON surface, so the "CAD plugin" client of section 3 has something
    # to talk to without scraping HTML.
    @app.get("/api/bom")
    def api_bom(revision: str = "main"):
        return JSONResponse(q.bill_of_materials(db, q.resolve_revision(db, revision)))

    @app.get("/api/where-used/{part}")
    def api_where_used(part: str):
        return JSONResponse([vars(r) for r in q.where_used(db, part)])

    @app.get("/api/chains")
    def api_chains():
        return JSONResponse([
            {"name": r.name, "passes": r.passes(), "explanation": r.explain()}
            for r in tolerance.evaluate_all(db)
        ])

    @app.get("/view/{revision}", response_class=HTMLResponse)
    def view(revision: str, fasteners: int = 1):
        """An orbit view of a part or a whole configuration, in the browser."""
        try:
            rev = q.resolve_revision(db, revision)
        except LookupError:
            return HTMLResponse("<h1>No such part</h1>", status_code=404)
        title = db.scalar(
            """SELECT p.part_number FROM revision r JOIN part p ON p.part_id = r.part_id
               WHERE r.revision_id = ?""", (rev,)) or revision
        return render("view.html", revision=revision, title=title,
                      fasteners=bool(fasteners))

    @app.get("/api/mesh/{revision}")
    def api_mesh(revision: str, fasteners: int = 1):
        """Display meshes plus placements, for the browser viewer.

        The meshes were tessellated once at ingest and stored beside the blobs
        precisely so a viewer would never have to open a solid. Distinct shapes
        are sent once and referenced by the instances that use them, which is the
        occurrence model doing the same work for the wire that it does for the
        database: 73 placed parts here cost 18 meshes.
        """
        import numpy as np

        from ..model.interference import is_fastener

        try:
            rev = q.resolve_revision(db, revision)
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

        shapes: dict[str, dict] = {}
        instances = []
        lo = np.full(3, np.inf)
        hi = np.full(3, -np.inf)

        for inst in q.configuration(db, rev):
            if inst.is_assembly or not inst.fingerprint:
                continue
            if not fasteners and is_fastener(inst.part_number):
                continue
            if inst.fingerprint not in shapes:
                mesh = db.read_mesh(inst.fingerprint)
                if mesh is None:
                    continue
                vertices, triangles = mesh
                shapes[inst.fingerprint] = {
                    "v": [round(float(c), 3) for c in np.asarray(vertices).reshape(-1)],
                    "f": [int(i) for i in np.asarray(triangles).reshape(-1)],
                }
            elif shapes[inst.fingerprint] is None:
                continue

            corners = np.asarray(shapes[inst.fingerprint]["v"], float).reshape(-1, 3)
            placed = (inst.world[:3, :3] @ corners.T).T + inst.world[:3, 3]
            lo = np.minimum(lo, placed.min(axis=0))
            hi = np.maximum(hi, placed.max(axis=0))

            instances.append({
                "s": inst.fingerprint,
                "m": [round(float(v), 6) for v in inst.world.T.reshape(-1)],  # column-major
                "part": inst.part_number,
                "team": inst.team_id,
                "path": inst.path,
            })

        if not instances:
            return JSONResponse({"error": "nothing to display"}, status_code=404)
        centre = ((lo + hi) / 2.0).tolist()
        span = float((hi - lo).max())
        return JSONResponse({
            "shapes": shapes, "instances": instances,
            "centre": centre, "span": span,
            "bounds": {"min": lo.tolist(), "max": hi.tolist()},
        })

    @app.get("/geometry/{revision}.step")
    def geometry(revision: str):
        """The stored geometry, rebuilt as STEP.

        Not a modelling operation: the solids are the ones the blob store was
        given, arranged by the occurrence table. Cached on the Merkle hash, so an
        unchanged configuration is written once.
        """
        try:
            path = export.export_cached(db, revision)
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        return FileResponse(path, media_type="application/step", filename=path.name)

    @app.get("/open/{revision}")
    def open_in_cad(revision: str, request: Request):
        """Export the revision and hand it to FreeCAD on this machine."""
        back = request.headers.get("referer") or "/"
        refused = local_only(request)
        if refused:
            return RedirectResponse(f"{back}{'&' if '?' in back else '?'}"
                                    f"flash={refused}&bad=1", 303)
        try:
            path = export.export_cached(db, revision)
        except LookupError as exc:
            return RedirectResponse(f"{back}{'&' if '?' in back else '?'}"
                                    f"flash={exc}&bad=1", 303)
        started, message = export.open_in_cad(path)
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}flash={message}"
                                + ("" if started else "&bad=1"), 303)

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        return "ok"

    return app
