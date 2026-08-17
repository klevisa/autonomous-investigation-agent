"""AIA app · ui — pure data -> HTML. No SQL, no FastAPI. Just turns dicts into pages.

Kept deliberately dependency-free (hand-rolled HTML + a little CSS/JS) so the app image stays tiny and
the rendering reads top-to-bottom. Colors follow a neutral dark dashboard palette; severity/status use
a small, consistent accent set.
"""
import html
import json

# severity/status -> accent color
SEV = {"low": "#3b9c5a", "medium": "#c9911f", "high": "#d1495b", "critical": "#d1495b"}
STATUS = {"new": "#6c7a89", "investigating": "#2f80ed", "investigated": "#3b9c5a",
          "escalated": "#d1495b", "closed": "#6c7a89"}


def _esc(x):
    return html.escape(str(x)) if x is not None else ""


def _chip(text, color):
    return (f'<span style="background:{color}22;color:{color};border:1px solid {color}55;'
            f'padding:2px 9px;border-radius:12px;font-size:12px;font-weight:600;white-space:nowrap">'
            f'{_esc(text)}</span>')


def sev_chip(s):
    return _chip((s or "—").upper(), SEV.get((s or "").lower(), "#6c7a89"))


def status_chip(s):
    return _chip((s or "—").replace("_", " "), STATUS.get((s or "").lower(), "#6c7a89"))


def page(body, active="board"):
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AIA Investigation Console</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
          background:#0e1116; color:#e6e9ef; }}
  header {{ padding:16px 28px; border-bottom:1px solid #232a35; display:flex; align-items:center;
            justify-content:space-between; background:#141922; position:sticky; top:0; z-index:10; }}
  header h1 {{ font-size:17px; margin:0; font-weight:700; letter-spacing:.3px; }}
  header .sub {{ color:#8b95a5; font-size:12px; margin-top:2px; }}
  main {{ padding:24px 28px; max-width:1200px; margin:0 auto; }}
  a {{ color:#5aa2f5; text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  .grid {{ display:grid; grid-template-columns:repeat(5,1fr); gap:12px; margin-bottom:24px; }}
  .tile {{ background:#141922; border:1px solid #232a35; border-radius:12px; padding:16px; }}
  .tile .n {{ font-size:28px; font-weight:700; }}
  .tile .l {{ color:#8b95a5; font-size:12px; text-transform:uppercase; letter-spacing:.5px; margin-top:4px;}}
  table {{ width:100%; border-collapse:collapse; background:#141922; border:1px solid #232a35;
           border-radius:12px; overflow:hidden; }}
  th, td {{ text-align:left; padding:11px 14px; font-size:13px; border-bottom:1px solid #1e242e; }}
  th {{ color:#8b95a5; font-weight:600; text-transform:uppercase; font-size:11px; letter-spacing:.5px; }}
  tr:last-child td {{ border-bottom:none; }}
  tr:hover td {{ background:#182029; }}
  .mono {{ font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size:12px; }}
  .btn {{ background:#2f80ed; color:#fff; border:none; padding:7px 14px; border-radius:8px;
          font-size:13px; font-weight:600; cursor:pointer; }}
  .btn:hover {{ background:#2b74d4; }}
  .btn.ghost {{ background:#1e2632; color:#c8d0dc; border:1px solid #2c3542; }}
  .card {{ background:#141922; border:1px solid #232a35; border-radius:12px; padding:20px; margin-bottom:16px; }}
  .card h3 {{ margin:0 0 12px; font-size:14px; color:#c8d0dc; }}
  .kv {{ display:grid; grid-template-columns:170px 1fr; gap:8px 16px; font-size:13px; }}
  .kv .k {{ color:#8b95a5; }}
  pre {{ background:#0b0e13; border:1px solid #1e242e; border-radius:8px; padding:12px; overflow:auto;
         font-size:12px; color:#b8c2d0; max-height:340px; }}
  .flag {{ background:#d1495b22; border:1px solid #d1495b66; color:#f2a3ad; padding:10px 14px;
           border-radius:10px; font-weight:600; margin-bottom:16px; }}
  .banner {{ padding:10px 14px; border-radius:10px; margin-bottom:16px; }}
  .err {{ background:#d1495b22; border:1px solid #d1495b66; color:#f2a3ad; }}
</style></head><body>
<header>
  <div><h1>🛡️ AIA · Autonomous Investigation Console</h1>
       <div class="sub">Medium-threat triage — the agent flags cases that should escalate to HIGH</div></div>
  <div class="sub"><a href="/" style="color:#8b95a5">↻ refresh</a></div>
</header>
<main>{body}</main>
</body></html>"""


def board(cases, st):
    tiles = "".join(
        f'<div class="tile"><div class="n" style="color:{c}">{st[k]}</div><div class="l">{lbl}</div></div>'
        for k, lbl, c in [("total", "Total", "#e6e9ef"), ("new", "New", "#6c7a89"),
                          ("investigating", "Investigating", "#2f80ed"),
                          ("investigated", "Investigated", "#3b9c5a"),
                          ("escalated", "Escalated ↑", "#d1495b")])
    rows = []
    for c in cases:
        esc_flag = " 🚩" if str(c.get("escalate_to_high")).lower() == "true" else ""
        assessed = sev_chip(c["assessed_severity"]) if c.get("assessed_severity") else '<span style="color:#5a6474">—</span>'
        can_run = (c.get("status") in ("new", "investigated", "escalated"))
        btn = (f'<button class="btn" onclick="run(\'{_esc(c["case_id"])}\',this)">Investigate</button>'
               if can_run else '<span style="color:#5a6474;font-size:12px">running…</span>')
        rows.append(f"""<tr>
          <td class="mono"><a href="/case/{_esc(c['case_id'])}">{_esc(c['case_id'])}</a></td>
          <td>{_esc(c['title'])}{esc_flag}</td>
          <td>{sev_chip(c['severity'])}</td>
          <td>{status_chip(c['status'])}</td>
          <td>{assessed}</td>
          <td class="mono">{_esc(c['account_id'])}</td>
          <td>{btn}</td></tr>""")
    table = f"""<table><thead><tr>
        <th>Case</th><th>Title</th><th>Arrived</th><th>Status</th><th>Assessed</th>
        <th>Account</th><th></th></tr></thead><tbody>{''.join(rows)}</tbody></table>"""
    return f"""<div class="grid">{tiles}</div>{table}
<script>
  async function run(id, btn) {{
    btn.disabled = true; btn.textContent = 'starting…';
    try {{
      const r = await fetch('/api/investigations', {{method:'POST',
        headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{case_id:id}})}});
      const d = await r.json();
      btn.textContent = r.ok ? 'started ✓' : ('error: ' + (d.error||''));
    }} catch(e) {{ btn.textContent = 'error'; }}
    setTimeout(()=>location.reload(), 1200);   // one reload so the just-started case shows 'investigating'
  }}
</script>"""


def case_detail(case, inv, all_invs):
    flag = ('<div class="flag">🚩 Agent recommends ESCALATION to HIGH severity</div>'
            if inv and str(inv.get("escalate_to_high")).lower() == "true" else "")
    head = f"""<p><a href="/">← board</a></p>{flag}
    <div class="card"><h3>{_esc(case['case_id'])} — {_esc(case['title'])}</h3>
      <div class="kv">
        <div class="k">Arrived severity</div><div>{sev_chip(case['severity'])}</div>
        <div class="k">Status</div><div>{status_chip(case['status'])}</div>
        <div class="k">Assessed severity</div><div>{sev_chip(case.get('assessed_severity')) if case.get('assessed_severity') else '—'}</div>
        <div class="k">Account</div><div class="mono">{_esc(case['account_id'])}</div>
        <div class="k">Indicator</div><div class="mono">{_esc(case['indicator_value'])} ({_esc(case['indicator_type'])})</div>
        <div class="k">Description</div><div>{_esc(case['description'])}</div>
      </div>
      <p style="margin-top:16px"><button class="btn" onclick="run('{_esc(case['case_id'])}',this)">Re-investigate</button></p>
    </div>"""

    if not inv:
        body = '<div class="card"><h3>Investigations</h3><p style="color:#8b95a5">None yet. Click Investigate.</p></div>'
    else:
        conf = inv.get("confidence")
        conf_s = f"{float(conf):.0%}" if conf not in (None, "") else "—"
        evidence = json.dumps(inv.get("evidence", {}), indent=2, default=str)
        tools = ", ".join(inv.get("tools_called", [])) or "—"
        body = f"""<div class="card"><h3>Latest investigation · {_esc(inv['investigation_id'] if inv.get('investigation_id') else '')} <span style="color:#5a6474;font-weight:400">({_esc(inv['status'])})</span></h3>
          <div class="kv">
            <div class="k">Assessed severity</div><div>{sev_chip(inv.get('assessed_severity'))}</div>
            <div class="k">Escalate to HIGH</div><div>{'<b style="color:#f2a3ad">YES</b>' if str(inv.get('escalate_to_high')).lower()=='true' else 'no'}</div>
            <div class="k">Recommended play</div><div class="mono">{_esc(inv.get('recommended_play'))}</div>
            <div class="k">Confidence</div><div>{conf_s}</div>
            <div class="k">Summary</div><div>{_esc(inv.get('summary'))}</div>
            <div class="k">Rationale</div><div>{_esc(inv.get('rationale'))}</div>
            <div class="k">Tools called</div><div class="mono">{_esc(tools)}</div>
            <div class="k">Model endpoint</div><div class="mono">{_esc(inv.get('model_endpoint'))}</div>
          </div>
          <h3 style="margin-top:18px">Evidence trail</h3><pre>{_esc(evidence)}</pre>
        </div>"""
        if len(all_invs) > 1:
            hist = "".join(f'<tr><td class="mono">{_esc(i.get("investigation_id"))}</td>'
                           f'<td>{status_chip(i.get("status"))}</td>'
                           f'<td>{sev_chip(i.get("assessed_severity"))}</td>'
                           f'<td class="mono">{_esc(i.get("recommended_play"))}</td>'
                           f'<td class="mono">{_esc(i.get("finished_at"))}</td></tr>' for i in all_invs)
            body += f"""<div class="card"><h3>Investigation history ({len(all_invs)})</h3>
              <table><thead><tr><th>ID</th><th>Status</th><th>Assessed</th><th>Play</th><th>Finished</th>
              </tr></thead><tbody>{hist}</tbody></table></div>"""

    js = f"""<script>
      async function run(id, btn) {{ btn.disabled=true; btn.textContent='starting…';
        const r=await fetch('/api/investigations',{{method:'POST',headers:{{'Content-Type':'application/json'}},
          body:JSON.stringify({{case_id:id}})}}); const d=await r.json();
        btn.textContent = r.ok?'started ✓':('error: '+(d.error||'')); setTimeout(()=>location.reload(),1500); }}
    </script>"""
    return head + body + js


def error(e):
    return f'<div class="banner err">Error: {_esc(str(e))}</div>'


def empty(msg):
    return f'<p style="color:#8b95a5">{_esc(msg)}</p>'
