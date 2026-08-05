"""
The dashboard's visual system: palette, Plotly template, and the small amount of
CSS Streamlit's own theming cannot express.

THE PALETTE IS NOT INVENTED HERE. Every hex below is taken verbatim from a
pre-validated reference palette, at the dark surface it was validated against
(`#1a1a19`). Colourblind-safety is a computed property, not an aesthetic
judgement — adjacent categorical pairs have to clear a ΔE floor under simulated
CVD — and the tool that computes it could not be run on this machine. Choosing a
prettier near-black and re-stepping the hues by eye would have produced a palette
whose safety nobody had checked. Using the validated set unchanged, at its
validated surface, is the only option here that keeps the guarantee.

If the surface ever changes, the palette must be re-validated against the new
one. Contrast results are only meaningful against the surface the chart actually
renders on.

WHY ONLY TWO CATEGORICAL SLOTS ARE USED. The only place this dashboard encodes
identity by colour is vendor provenance — Polygon against Yahoo — which is slot
1 and slot 2, a documented adjacent pair. Corporate-action annotations
deliberately carry NO categorical hue: they are chrome, not a series, and they
are distinguished by dash pattern and a text label instead. That sidesteps the
question of whether a fifth hue would survive CVD next to the four already on
screen, and it is the better design regardless — an annotation whose meaning
rests on hue is unreadable the moment it is printed.
"""

from __future__ import annotations

import plotly.graph_objects as go

# --------------------------------------------------------------------------
# Surfaces and ink
# --------------------------------------------------------------------------

SURFACE = "#1a1a19"          # chart surface — the validation surface
PAGE = "#0d0d0d"             # page plane, one step below the chart surface
INK_PRIMARY = "#ffffff"
INK_SECONDARY = "#c3c2b7"
INK_MUTED = "#898781"        # axis labels, ticks
GRIDLINE = "#2c2c2a"         # hairline, one shade off the surface
AXIS = "#383835"
BORDER = "rgba(255,255,255,0.10)"

# --------------------------------------------------------------------------
# Categorical — identity only. Fixed order, never cycled, never by rank.
# --------------------------------------------------------------------------

SERIES_1 = "#3987e5"  # blue   — the price series, and Polygon
SERIES_2 = "#d95926"  # orange — Yahoo

#: Vendor -> colour. Bound to the ENTITY, not to its position in a sorted list,
#: so filtering a vendor out never repaints the survivor. Polygon is slot 1
#: because ADR-0006 makes it primary; that is a meaning, not a preference.
SOURCE_COLOURS = {"polygon": SERIES_1, "yahoo": SERIES_2}

# --------------------------------------------------------------------------
# Status — reserved. Never used for a series, and never used alone: every
# status colour in this dashboard ships beside a glyph and a word, because on
# any surface a colour by itself is not readable by everyone.
# --------------------------------------------------------------------------

STATUS_GOOD = "#0ca30c"
STATUS_WARNING = "#fab219"
STATUS_SERIOUS = "#ec835a"
STATUS_CRITICAL = "#d03b3b"

RUN_STATUS = {
    "SUCCESS": (STATUS_GOOD, "●", "Success"),
    "RUNNING": (STATUS_WARNING, "◐", "Running"),
    "FAILED": (STATUS_CRITICAL, "✕", "Failed"),
}


def status_badge(status: str) -> str:
    """A status as colour + glyph + word. Never colour alone."""
    colour, glyph, label = RUN_STATUS.get(
        (status or "").upper(), (INK_MUTED, "○", status or "unknown")
    )
    return (
        f"<span style='color:{colour};font-weight:600;white-space:nowrap'>"
        f"{glyph}&nbsp;{label}</span>"
    )


# --------------------------------------------------------------------------
# Plotly template
# --------------------------------------------------------------------------

#: Registered once and referenced by name. Thin marks, hairline recessive grid,
#: generous padding — the loud-block look is what makes a dashboard read as
#: childish at scale.
PLOTLY_TEMPLATE = go.layout.Template(
    layout=go.Layout(
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(
            family='system-ui, -apple-system, "Segoe UI", sans-serif',
            color=INK_SECONDARY,
            size=13,
        ),
        title=dict(font=dict(color=INK_PRIMARY, size=15), x=0, xanchor="left"),
        margin=dict(l=8, r=8, t=48, b=8),
        xaxis=dict(
            gridcolor=GRIDLINE,
            zerolinecolor=AXIS,
            linecolor=AXIS,
            tickfont=dict(color=INK_MUTED, size=11),
            # Solid hairlines. A dashed grid reads as "threshold" or
            # "projection" when it is only a grid.
            griddash="solid",
            showspikes=True,
            spikemode="across",
            spikethickness=1,
            spikecolor=INK_MUTED,
            spikedash="dot",
        ),
        yaxis=dict(
            gridcolor=GRIDLINE,
            zerolinecolor=AXIS,
            linecolor=AXIS,
            tickfont=dict(color=INK_MUTED, size=11),
            griddash="solid",
        ),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            font=dict(color=INK_SECONDARY, size=12),
            bgcolor="rgba(0,0,0,0)",
        ),
        hoverlabel=dict(
            bgcolor=PAGE,
            bordercolor=BORDER,
            font=dict(color=INK_PRIMARY, size=12),
        ),
        colorway=[SERIES_1, SERIES_2],
    )
)


# --------------------------------------------------------------------------
# CSS
# --------------------------------------------------------------------------

#: Kept deliberately small, and pinned to `data-testid` attributes rather than
#: to Streamlit's generated class names. Generated class names change between
#: Streamlit releases, so a stylesheet built on them breaks silently on upgrade;
#: the test ids are part of Streamlit's own test surface and are far more
#: stable. Everything expressible in .streamlit/config.toml lives there instead
#: of here for the same reason.
CSS = f"""
<style>
  /* Monospaced, tabular figures in tables and metrics only — columns that
     actually align vertically. Deliberately NOT on the big stat-tile numbers:
     equal-width digits make a large standalone number look loose. */
  [data-testid="stDataFrame"] {{ font-variant-numeric: tabular-nums; }}

  [data-testid="stMetric"] {{
      background: {SURFACE};
      border: 1px solid {BORDER};
      border-radius: 8px;
      padding: 14px 16px;
  }}
  [data-testid="stMetricLabel"] {{
      color: {INK_MUTED};
      text-transform: uppercase;
      letter-spacing: 0.06em;
      font-size: 11px;
      font-weight: 600;
  }}
  [data-testid="stMetricValue"] {{
      color: {INK_PRIMARY};
      font-variant-numeric: proportional-nums;
  }}

  /* The one piece of deliberate ornament: a hairline rule under the page
     title, which is what makes a dense dark layout read as sectioned rather
     than as one undifferentiated field. */
  .fmp-title {{
      border-bottom: 1px solid {BORDER};
      padding-bottom: 10px;
      margin-bottom: 18px;
  }}
  .fmp-title h1 {{
      font-size: 20px; font-weight: 650; color: {INK_PRIMARY};
      margin: 0; letter-spacing: -0.01em;
  }}
  .fmp-title p {{ color: {INK_MUTED}; font-size: 13px; margin: 4px 0 0 0; }}

  .fmp-note {{
      color: {INK_MUTED}; font-size: 12px; line-height: 1.5;
      border-left: 2px solid {AXIS}; padding-left: 10px; margin: 6px 0 14px 0;
  }}
</style>
"""


def page_title(title: str, subtitle: str = "") -> str:
    sub = f"<p>{subtitle}</p>" if subtitle else ""
    return f"<div class='fmp-title'><h1>{title}</h1>{sub}</div>"


def note(text: str) -> str:
    """A quiet caveat. Used for the things this platform refuses to hide."""
    return f"<div class='fmp-note'>{text}</div>"
