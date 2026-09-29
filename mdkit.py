"""Copy-as-Markdown for every dashboard page, and a shared day/week picker.

How it works: every Streamlit call that draws something (metric, dataframe, subheader, caption,
info/warning/error/success, markdown, write, code, table) is ALSO written, as Markdown, into the
current page's buffer. `page("Title")` starts a new page at the top of the current tab; the
previous page's copy box is filled in then. Call `done()` once at the very end of the script.

Nothing here changes what is drawn on screen except the copy box at the top of each tab.
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st
from streamlit.delta_generator import DeltaGenerator

_local = threading.local()


# ---------------------------------------------------------------- Markdown text
def _cell(v) -> str:
    if v is None:
        return ""
    try:
        if v != v:  # NaN
            return ""
    except Exception:
        pass
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        if abs(v) >= 10:
            return f"{v:,.2f}".rstrip("0").rstrip(".")
        return f"{v:.4f}".rstrip("0").rstrip(".") if v != 0 else "0"
    return str(v).replace("|", "/").replace("\n", " ")


def df_to_md(data) -> str:
    try:
        df = data if isinstance(data, pd.DataFrame) else pd.DataFrame(data)
    except Exception:
        return str(data)
    if df is None or len(df) == 0:
        return "_(empty)_"
    if not isinstance(df.index, pd.RangeIndex):
        df = df.reset_index()
    cols = [str(c) for c in df.columns]
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for row in df.itertuples(index=False):
        out.append("| " + " | ".join(_cell(v) for v in row) + " |")
    return "\n".join(out)


def _fmt(kind: str, args, kwargs) -> str | None:
    first = args[0] if args else kwargs.get("body", kwargs.get("data", kwargs.get("label", "")))
    if kind in ("title", "header", "subheader"):
        return ("\n## " if kind != "title" else "# ") + str(first)
    if kind in ("dataframe", "table", "data_editor"):
        return df_to_md(first)
    if kind == "metric":
        label = args[0] if args else kwargs.get("label", "")
        value = args[1] if len(args) > 1 else kwargs.get("value", "")
        delta = args[2] if len(args) > 2 else kwargs.get("delta")
        return f"**{label}:** {value}" + (f" ({delta})" if delta not in (None, "") else "")
    if kind in ("info", "warning", "error", "success"):
        return f"> **{kind.upper()}:** {first}"
    if kind == "caption":
        return f"_{first}_"
    if kind == "code":
        return "```\n" + str(first) + "\n```"
    if kind == "write":
        parts = []
        for a in args:
            parts.append(df_to_md(a) if isinstance(a, (pd.DataFrame, list)) and a is not None and not isinstance(a, str) else str(a))
        return " ".join(parts)
    if kind in ("markdown", "text"):
        return str(first)
    return None


def _wrap(kind: str):
    orig = getattr(DeltaGenerator, kind, None)
    if orig is None or getattr(orig, "_mdkit", False):
        return

    def wrapped(self, *args, **kwargs):
        buf = getattr(_local, "buf", None)
        if buf is not None and not getattr(_local, "inside", False):
            try:
                txt = _fmt(kind, args, kwargs)
                if txt:
                    buf.append(txt)
            except Exception:
                pass
        prev = getattr(_local, "inside", False)
        _local.inside = True
        try:
            return orig(self, *args, **kwargs)
        finally:
            _local.inside = prev

    wrapped._mdkit = True
    setattr(DeltaGenerator, kind, wrapped)


_KINDS = ("title", "header", "subheader", "dataframe", "table", "metric", "info", "warning",
          "error", "success", "caption", "code", "write", "markdown", "text")
for _k in _KINDS:
    _wrap(_k)
    # st.caption, st.dataframe, ... were bound to the main container when streamlit was
    # imported, so they still point at the unwrapped methods. Re-bind them to the wrapped ones.
    _main = getattr(st, "_main", None)
    if _main is not None and hasattr(st, _k):
        setattr(st, _k, getattr(_main, _k))


# ---------------------------------------------------------------- pages
def _finish_current() -> None:
    cur = getattr(_local, "page", None)
    _local.buf = None
    _local.page = None
    if not cur:
        return
    text = "\n\n".join(cur["parts"])
    with cur["slot"]:
        with st.expander("📋 Copy this whole page as Markdown (click the copy icon in the box, then paste)",
                         expanded=False):
            st.code(text, language="markdown")
        st.download_button("Download this page as .md", data=text, file_name=cur["file"],
                           mime="text/markdown", key="md_" + cur["key"])


def page(title: str, version: str = "", now_text: str = "") -> None:
    """Call as the FIRST line inside each `with tab_x:` block."""
    _finish_current()
    key = "".join(ch if ch.isalnum() else "-" for ch in title.lower()).strip("-") or "page"
    slot = st.container()
    parts = [f"# {title}"]
    if version or now_text:
        parts.append(f"_{version}{' · as of ' + now_text if now_text else ''}_")
    _local.page = {"slot": slot, "parts": parts, "key": key, "file": f"{key}.md"}
    _local.buf = parts


def done() -> None:
    """Call once at the end of the script."""
    _finish_current()


# ---------------------------------------------------------------- day / week picker
def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_label(week: str) -> str:
    y, w = week.split("-W")
    mon = date.fromisocalendar(int(y), int(w), 1)
    return f"{week} (Mon {mon:%b %d} – Fri {mon + timedelta(days=4):%b %d})"


def period_picker(dates: list[str], key: str, today: str | None = None) -> dict:
    """Day / Week / All picker. `dates` = every 'YYYY-MM-DD' that has data.
    Returns {"mode", "label", "match": callable(date_str) -> bool}."""
    ds = sorted({str(d)[:10] for d in dates if d}, reverse=True)
    c1, c2 = st.columns([1, 3])
    mode = c1.radio("Show", ["Day", "Week", "All"], horizontal=True, key=f"{key}_mode")
    if mode == "Day":
        default = ds[0] if ds else (today or date.today().isoformat())
        picked = c2.date_input("Day", value=date.fromisoformat(default), key=f"{key}_day")
        day = picked.isoformat()
        return {"mode": mode, "label": day, "match": lambda s, day=day: str(s)[:10] == day}
    if mode == "Week":
        weeks = sorted({iso_week(date.fromisoformat(d)) for d in ds}, reverse=True) or [iso_week(date.today())]
        wk = c2.selectbox("Week", weeks, format_func=week_label, key=f"{key}_week")
        return {"mode": mode, "label": week_label(wk),
                "match": lambda s, wk=wk: bool(s) and iso_week(date.fromisoformat(str(s)[:10])) == wk}
    return {"mode": mode, "label": "all dates", "match": lambda s: True}


# ---------------------------------------------------------------- small formatters
def ct_time(value, tz) -> str:
    """'2026-09-28 22:32:31+00' -> 'Sep 28 5:32:31 PM'. Blank if missing."""
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            from datetime import timezone
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(tz).strftime("%b %d %-I:%M:%S %p")
    except Exception:
        return str(value)


def money(cents) -> float | None:
    if cents is None:
        return None
    try:
        return round(float(cents) / 100.0, 2)
    except (TypeError, ValueError):
        return None
