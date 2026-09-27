"""The natural-language layer (design doc section 13).

The model does intent resolution over a fixed, typed tool surface and nothing
else. It never writes a query, it never sees the database, and it has no role in
deciding whether a commit is valid. Everything it can reach is in `nl.tools`, and
every tool there is read-only.

Three properties this layer is built to have:

  * **It is optional.** With no API key, no network, or no `anthropic` package
    installed, `Agent` falls back to a deterministic keyword router over the same
    tools. The answers are blunter, but the demonstration runs and the tools are
    exercised by exactly the same code path. Nothing else in Interlock imports
    this module.

  * **Every answer is auditable.** Each tool call, its arguments and the rows it
    returned are recorded in the transcript and written to the document store's
    `nl_tool_call` collection through the outbox. A claim in prose can be traced
    back to the query that produced it, which is the only reason to trust it.

  * **It respects the contract boundary.** The tools expose published contracts,
    never another team's internal features, so the model cannot route around the
    encapsulation that section 8 is built on.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from ..db.database import Database
from ..db.documents import TOPIC_NL_CALL, enqueue
from . import tools as toolkit

MODEL = "claude-opus-5"
MAX_TOKENS = 16000
MAX_TURNS = 12

SYSTEM = """You answer questions about Interlock, a multi-team mechanical parts \
database, by calling the typed tools you have been given.

Rules you must follow:

- Answer only from tool results. If no tool can answer the question, say so \
plainly rather than guessing. You have no knowledge of this database except \
through these tools.
- Never invent part numbers, quantities, masses, hashes or team names. Every \
number in your answer must have come from a tool result.
- Prefer one precise tool call over several vague ones. Read the tool \
descriptions: `explode` with aggregate='bom' answers "what is in it", \
`where_used` answers "who uses this", `impact_of` answers "who do I break".
- When a tolerance chain fails, say which method (worst case, statistical or \
Monte Carlo) was used, and report the sigma convention, because the statistical \
number is meaningless without it.
- You are read-only. You cannot land a commit. `would_my_commit_conflict` runs \
the real validator and rolls it back; report what it says, and be clear that \
nothing was written.
- Be concise and concrete. Engineers are reading this."""


@dataclass
class Call:
    tool: str
    arguments: dict
    result: dict

    def summary(self) -> str:
        return f"{self.tool}({json.dumps(self.arguments, default=str)[:120]})"


@dataclass
class Answer:
    question: str
    text: str
    calls: list[Call] = field(default_factory=list)
    backend: str = "model"        # model | offline
    turns: int = 0
    error: str | None = None

    def transcript(self) -> str:
        lines = [f"Q: {self.question}", f"[{self.backend}]"]
        for c in self.calls:
            lines.append(f"  -> {c.summary()}")
        lines.append(self.text)
        return "\n".join(lines)


def _load_env() -> None:
    """Read .env if present. The key never belongs in the repository."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        env = os.path.join(os.getcwd(), ".env")
        if os.path.exists(env):
            for line in open(env, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Agent:
    def __init__(self, db: Database, pipeline=None, model: str = MODEL, log: bool = True):
        self.db = db
        self.tools = toolkit.build_tools(db, pipeline=pipeline)
        self.model = model
        self.log = log
        self._client = None
        self._why_offline: str | None = None
        self._fallbacks: bool | None = None
        _load_env()

    # ------------------------------------------------------------- availability

    @property
    def online(self) -> bool:
        return self.client is not None

    @property
    def client(self):
        if self._client is not None:
            return self._client
        if not os.environ.get("ANTHROPIC_API_KEY"):
            self._why_offline = "ANTHROPIC_API_KEY is not set"
            return None
        try:
            import anthropic
        except ImportError:
            self._why_offline = "the anthropic package is not installed"
            return None
        try:
            self._client = anthropic.Anthropic()
        except Exception as exc:
            self._why_offline = f"{type(exc).__name__}: {exc}"
            return None
        return self._client

    def status(self) -> str:
        if self.online:
            return f"natural language: online ({self.model}, {len(self.tools)} tools)"
        return (
            f"natural language: offline ({self._why_offline}); "
            f"falling back to the keyword router over the same {len(self.tools)} tools"
        )

    # ------------------------------------------------------------------- ask

    def ask(self, question: str) -> Answer:
        if not self.online:
            return self._offline(question)
        try:
            return self._with_model(question)
        except Exception as exc:
            answer = self._offline(question)
            answer.error = f"model call failed ({type(exc).__name__}: {exc}); answered offline"
            return answer

    def _record(self, answer: Answer) -> None:
        if not self.log or not answer.calls:
            return
        from ..db.database import new_id

        key = new_id("nl")
        try:
            with self.db.transaction():
                enqueue(self.db, TOPIC_NL_CALL, key, {
                    "question": answer.question,
                    "backend": answer.backend,
                    "model": self.model if answer.backend == "model" else None,
                    "turns": answer.turns,
                    "calls": [
                        {"tool": c.tool, "arguments": c.arguments,
                         "result_preview": json.dumps(c.result, default=str)[:2000]}
                        for c in answer.calls
                    ],
                    "answer": answer.text,
                })
        except Exception:
            pass       # the audit log must never break the answer

    def _with_model(self, question: str) -> Answer:
        client = self.client
        answer = Answer(question=question, text="", backend="model")
        schemas = toolkit.schemas(self.tools)
        messages = [{"role": "user", "content": question}]

        for turn in range(MAX_TURNS):
            answer.turns = turn + 1
            response = self._create(client, schemas, messages)

            # A refusal is a 200 with no usable content; check before reading it.
            if response.stop_reason == "refusal":
                answer.text = "The model declined to answer this request."
                answer.error = "stop_reason=refusal"
                break

            if response.stop_reason != "tool_use":
                answer.text = "\n".join(
                    b.text for b in response.content if b.type == "text"
                ).strip()
                break

            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                arguments = dict(block.input or {})
                result = toolkit.call(self.tools, block.name, arguments)
                answer.calls.append(Call(block.name, arguments, result))
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str)[:60000],
                    "is_error": "error" in result,
                })
            messages.append({"role": "user", "content": results})
        else:
            answer.text = answer.text or "Stopped after the maximum number of tool calls."

        self._record(answer)
        return answer

    def _create(self, client, schemas, messages):
        """One Messages call.

        Adaptive thinking, because resolving a vague question onto the right tool
        is exactly the kind of small reasoning step it exists for. Server-side
        refusal fallbacks are requested when the installed SDK understands them,
        and dropped otherwise rather than failing the call -- this layer is
        optional and must degrade rather than break.
        """
        params = dict(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            tools=schemas,
            thinking={"type": "adaptive"},
            messages=messages,
        )
        if self._fallbacks is not False:
            try:
                return client.messages.create(
                    **params,
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except TypeError:
                self._fallbacks = False      # older SDK: stop asking
            except Exception as exc:
                if "fallback" not in str(exc).lower() and "beta" not in str(exc).lower():
                    raise
                self._fallbacks = False
        return client.messages.create(**params)

    # --------------------------------------------------------------- offline

    def _offline(self, question: str) -> Answer:
        """Deterministic keyword routing over the same tools.

        Not a language model and not pretending to be one. It exists so the
        demonstration -- and the tool surface it exercises -- works with no
        network, which section 13 lists as a practical consequence of putting the
        queries behind typed tools in the first place.
        """
        answer = Answer(question=question, text="", backend="offline")
        q = question.lower()
        part = self._guess_part(question)

        def run(name: str, **kwargs) -> dict:
            result = toolkit.call(self.tools, name, kwargs)
            answer.calls.append(Call(name, kwargs, result))
            return result

        if any(w in q for w in ("conflict", "would my commit", "dry run", "dry-run")):
            out = run("would_my_commit_conflict", part=part or "BASE-PLATE")
            answer.text = _render_dry_run(out)
        elif any(w in q for w in ("chain", "tolerance", "stack", "gap", "clearance")):
            out = run("evaluate_chain")
            answer.text = _render_chains(out)
        elif any(w in q for w in ("who uses", "where used", "where is", "contains", "used in")):
            out = run("where_used", part=part or "BOLT-M6X20")
            answer.text = _render_where_used(out)
        elif any(w in q for w in ("break", "impact", "affect", "notify", "depend")):
            out = run("impact_of", part=part or "BASE-PLATE")
            answer.text = _render_impact(out)
        elif any(w in q for w in ("interfere", "clash", "collide", "overlap")):
            out = run("check_interference", revision="main")
            answer.text = out.get("summary", "") + " (advisory)"
        elif any(w in q for w in ("contract", "interface", "socket", "envelope", "publish")):
            out = run("contract_of", part=part or "BEARING-BLOCK")
            answer.text = _render_contract(out)
        elif any(w in q for w in ("mass", "weight", "heavy", "power", "thermal", "centre", "center")):
            out = run("explode", revision="main", aggregate="mass")
            answer.text = (
                f"Mass {out.get('mass_g', 0):.0f} g, power {out.get('power_w', 0):.0f} W, "
                f"thermal {out.get('thermal_w', 0):.0f} W."
            )
        elif any(w in q for w in ("history", "commit", "rejected", "log", "recent")):
            out = run("history", limit=8)
            answer.text = _render_history(out)
        elif any(w in q for w in ("fit", "compatible", "substitute", "replace", "instead")):
            out = run("find_compatible", hole_count=4)
            answer.text = _render_compatible(out)
        elif part:
            out = run("part_summary", part=part)
            answer.text = _render_part(out)
        else:
            out = run("explode", revision="main", aggregate="bom")
            answer.text = _render_bom(out)

        self._record(answer)
        return answer

    def _guess_part(self, question: str) -> str | None:
        """Pick out a part number by matching what is actually in the database."""
        rows = self.db.query("SELECT part_number FROM part")
        upper = question.upper()
        best = None
        for r in rows:
            n = r["part_number"]
            if n in upper and (best is None or len(n) > len(best)):
                best = n
        if best:
            return best
        token = re.search(r"\b([A-Z][A-Z0-9]+(?:-[A-Z0-9]+)+)\b", question.upper())
        return token.group(1) if token else None


# --------------------------------------------------------------- renderers


def _render_bom(out: dict) -> str:
    rows = out.get("bom", [])
    if not rows:
        return "No parts found."
    lines = [f"{len(rows)} distinct parts:"]
    for r in rows[:25]:
        lines.append(
            f"  {r['part_number']:<20} x{r['total_qty']:<4} {r['team_id']:<12} "
            f"{(r.get('total_mass_g') or 0):8.1f} g"
        )
    return "\n".join(lines)


def _render_where_used(out: dict) -> str:
    rows = out.get("used_in", [])
    if not rows:
        return f"{out.get('part')} is not used in any current assembly."
    lines = [f"{out['part']} is used in {len(rows)} assembly/assemblies:"]
    for r in rows:
        lines.append(f"  {r['part']} (team {r['team']}, depth {r['depth']})")
    return "\n".join(lines)


def _render_impact(out: dict) -> str:
    teams = out.get("impacted_teams", [])
    if not teams:
        return f"Changing {out.get('part')}'s contract affects no other team."
    lines = [f"Changing {out['part']}'s contract affects {len(teams)} team(s):"]
    for t in teams:
        lines.append(f"  {t['team']}: {t['reasons'][0]}")
    return "\n".join(lines)


def _render_chains(out: dict) -> str:
    chains = out.get("chains", [])
    if not chains:
        return "No tolerance chains are authored."
    lines = []
    for c in chains:
        mark = "OK  " if c["passes"] else "FAIL"
        lines.append(f"{mark} {c['explanation']}")
        if c.get("sigma_conventions"):
            lines.append(f"       sigma convention: {', '.join(c['sigma_conventions'])}")
    return "\n".join(lines)


def _render_contract(out: dict) -> str:
    if not out.get("interfaces") and out.get("contract") is None:
        return out.get("note", "No contract published.")
    lines = [f"{out['part']} publishes:"]
    if out.get("envelope") and out["envelope"][0]:
        lines.append(f"  envelope {out['envelope']} mm")
    if out.get("mass_max"):
        lines.append(f"  mass limit {out['mass_max']} g")
    for i in out.get("interfaces", []):
        lines.append(
            f"  interface {i['name']}: {i['hole_count']} x "
            f"{(i['hole_radius'] or 0) * 2:.1f} mm holes, {i['fastener']} {i['fit_class']}"
            + ("  [auto-drafted, unreviewed]" if i.get("source") == "auto_drafted" else "")
        )
    for s in out.get("sockets", []):
        lines.append(f"  socket {s['name']} (filled by {s['fills']})")
    return "\n".join(lines)


def _render_part(out: dict) -> str:
    if out.get("found") is False:
        return f"No part {out.get('part')}."
    return (
        f"{out['part_number']} (team {out['team_id']}), revision {out['revision_index']}, "
        f"{out.get('material') or 'no material'}, "
        f"{(out.get('mass_g') or 0):.1f} g, fingerprint {out.get('fingerprint')}"
    )


def _render_history(out: dict) -> str:
    rows = out.get("commits", [])
    if not rows:
        return "No commits."
    lines = []
    for r in rows:
        lines.append(f"  {r['verdict']:<9} {r['author']:<8} {r['message'][:50]}")
        if r.get("reason"):
            lines.append(f"            {r['reason'][:110]}")
    return "\n".join(lines)


def _render_compatible(out: dict) -> str:
    rows = out.get("candidates", [])
    if not rows:
        return "Nothing published an interface matching that description."
    lines = [f"{len(rows)} candidate(s):"]
    for r in rows[:10]:
        lines.append(
            f"  {r['part_number']:<20} {r['interface']:<18} "
            f"{r['hole_count']} x {(r['hole_radius'] or 0) * 2:.1f} mm  team {r['team_id']}"
        )
    return "\n".join(lines)


def _render_dry_run(out: dict) -> str:
    if out.get("available") is False:
        return out.get("note", "No pipeline available.")
    lines = [f"Verdict: {out['verdict']} ({out['note']})"]
    for f in out.get("failures", []):
        lines.append(f"  FAIL {f}")
    for n in out.get("notifications", []):
        lines.append(f"  would notify {n['team']} about {n['part']}")
    return "\n".join(lines)
