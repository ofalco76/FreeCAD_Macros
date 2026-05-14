# ui/model_viewer_v2.py
"""
3D Viewer 2.0 — KModelViewerV2Dialog

New-generation 3D model viewer for LS-DYNA .k files.

Design references:
  - Open Step Viewer 27.3: fixed per-tab toolbars, industrial/sober style,
    expandable model tree panel.
  - Autodesk Viewer: clean layout, right-side properties panel, splitter flexibility.

Layout:
  ┌──────────────────────────────────────────────────────────────┐
  │  [File] [View]  tabs                                         │
  │  ───────── Contextual toolbar for active tab ────────────────│
  ├──────────────┬──────────────────────────┬────────────────────┤
  │ Left Panel   │   3D Viewport            │  Properties Panel  │
  │  Tab: Files  │   (QtInteractor)         │  (selected item)   │
  │  Tab: Model  │                          │                    │
  │  Navigator   │                          │                    │
  ├──────────────┴──────────────────────────┴────────────────────┤
  │  Status Bar                                                  │
  └──────────────────────────────────────────────────────────────┘

Panels are separated by QSplitter for user-adjustable sizing.
The classic viewer (KModelViewerDialog) is preserved and unaffected.

Phase 1 — Base structure:
  - Window chrome, QTabWidget ribbon, contextual toolbars (placeholders),
    three-panel splitter layout, status bar.
  - No VTK rendering yet; viewport shows a placeholder until Phase 2.
"""
from __future__ import annotations

import colorsys
import logging
import numbers
import os
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QTimer, QObject, QEvent
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QProxyStyle,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QStyle,
    QTabWidget,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from ui.styles import get_app_theme as _get_app_theme

logger = logging.getLogger(__name__)


def _intensify_rgb(rgb) -> tuple[float, float, float]:
    """Boost saturation/value of an RGB triple so it reads as 'highlighted'
    while preserving the original hue (each part keeps its identity color).
    """
    try:
        r, g, b = float(rgb[0]), float(rgb[1]), float(rgb[2])
    except Exception:
        return (1.0, 0.55, 0.0)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    # Push saturation hard so the part really pops; clamp value so very dark
    # base colors still read distinctly above the unhighlighted neighbours.
    s = min(1.0, s * 2.2 + 0.55)
    v = min(1.0, max(0.6, v * 1.15))
    nr, ng, nb = colorsys.hsv_to_rgb(h, s, v)
    return (nr, ng, nb)


def _icon_from_text(text: str, size: int = 20, fg: str = "#333",
                    bg: str = "transparent") -> QIcon:
    """Create a tiny QIcon with *text* rendered in the centre."""
    px = QPixmap(QSize(size, size))
    px.fill(QColor(bg) if bg != "transparent" else QColor(0, 0, 0, 0))
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor(fg))
    p.setFont(QFont("Segoe UI", int(size * 0.48), QFont.Weight.Bold))
    p.drawText(px.rect(), Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return QIcon(px)


class _LargeIconMenuStyle(QProxyStyle):
    """Force 32 px icons inside QMenu."""
    def pixelMetric(self, metric, option=None, widget=None):
        if metric == QStyle.PixelMetric.PM_SmallIconSize:
            return 32
        return super().pixelMetric(metric, option, widget)

_large_icon_menu_style = _LargeIconMenuStyle()

# Icons directory (shared with other MV components)
_ICONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "resources", "icons",
)

# ---------------------------------------------------------------------------
#  Visual constants — light palette (matches main_window / Modern Header theme)
#  These drive the ID-scoped rules in _STYLESHEET.
#  The overall app theme (QGroupBox, QTabWidget, etc.) is applied via
#  get_app_theme() in __init__ and can be changed at runtime via _apply_theme().
# ---------------------------------------------------------------------------
_CLR_BG_WINDOW       = "#f5f5f5"   # window background
_CLR_BG_PANEL        = "#ffffff"   # panel backgrounds
_CLR_BG_TOOLBAR      = "#e8e8e8"   # toolbar band (matches main_window)
_CLR_BG_TAB_ACTIVE   = "#e8e8e8"   # selected tab
_CLR_BG_TAB_INACTIVE = "#d8d8d8"   # idle tab
_CLR_BORDER          = "#cccccc"   # borders / dividers
_CLR_ACCENT          = "#003366"   # ITP Blue (Modern Header)
_CLR_TEXT            = "#333333"   # primary text
_CLR_TEXT_DIM        = "#888888"   # secondary / dim text
_CLR_HEADER          = "#003366"   # panel header bg (ITP Blue)

_FONT_UI    = "Segoe UI"
_FONT_SIZE  = 9             # pt

# Splitter initial proportions (left : center : right)
_SPLIT_LEFT    = 220
_SPLIT_CENTER  = 700
_SPLIT_RIGHT   = 240

# ---------------------------------------------------------------------------
#  Global stylesheet — light theme, flat toolbar buttons (Open Step Viewer style)
#  ID-scoped selectors ensure ribbon/side-tabs survive any applied app theme.
# ---------------------------------------------------------------------------
_STYLESHEET = f"""
/* ── Window ─────────────────────────────────────────────── */
KModelViewerV2Dialog {{
    background-color: {_CLR_BG_WINDOW};
    color: {_CLR_TEXT};
    font-family: {_FONT_UI};
    font-size: {_FONT_SIZE}pt;
}}

/* ── Tab ribbon ─────────────────────────────────────────── */
QTabWidget#ribbon_tabs::pane {{
    border: none;
    background: {_CLR_BG_TOOLBAR};
    margin-top: 0px;
    padding: 0px;
}}
QTabWidget#ribbon_tabs > QTabBar::tab {{
    background: {_CLR_BG_TAB_INACTIVE};
    color: {_CLR_TEXT};
    font-family: {_FONT_UI};
    font-size: {_FONT_SIZE}pt;
    font-weight: bold;
    padding: 6px 18px;
    border: 1px solid #b8b8b8;
    border-bottom: none;
    min-width: 80px;
}}
QTabWidget#ribbon_tabs > QTabBar::tab:selected {{
    background: {_CLR_BG_TAB_ACTIVE};
    color: {_CLR_ACCENT};
    border-bottom: 2px solid {_CLR_ACCENT};
}}
QTabWidget#ribbon_tabs > QTabBar::tab:hover:!selected {{
    background: #e0e0e0;
    color: {_CLR_TEXT};
}}

/* ── Contextual toolbar band — flat buttons, no borders ─── */
QToolBar#ribbon_tb {{
    background: {_CLR_BG_TOOLBAR};
    border: none;
    border-bottom: 1px solid {_CLR_BORDER};
    spacing: 2px;
    padding: 3px 6px;
}}
QToolBar#ribbon_tb QToolButton {{
    background: transparent;
    border: none;
    border-radius: 4px;
    color: {_CLR_TEXT};
    font-size: {_FONT_SIZE}pt;
    padding: 2px 6px;
    min-width: 40px;
    min-height: 54px;
}}
QToolBar#ribbon_tb QToolButton:hover {{
    background: #d0d8e8;
}}
QToolBar#ribbon_tb QToolButton:pressed,
QToolBar#ribbon_tb QToolButton:checked {{
    background: #b8c8e0;
    border: 1px solid #8898b8;
}}
QToolBar#ribbon_tb QLabel {{
    color: {_CLR_TEXT_DIM};
    font-size: 8pt;
    padding: 0 4px;
}}
/* combo-button popup arrow stays clean */
QToolBar#ribbon_tb QToolButton::menu-indicator {{
    subcontrol-position: right center;
    subcontrol-origin: padding;
    width: 10px;
    left: -2px;
}}
QToolBar#ribbon_tb QToolButton[popupMode="1"] {{
    padding-right: 14px;
}}

/* ── Splitter handles ───────────────────────────────────── */
QSplitter::handle {{
    background: {_CLR_BORDER};
    width: 3px;
    height: 3px;
}}
QSplitter::handle:hover {{
    background: {_CLR_ACCENT};
}}

/* ── Side panels ────────────────────────────────────────── */
QTabWidget#side_tabs::pane {{
    border: 1px solid {_CLR_ACCENT};
    background: {_CLR_BG_PANEL};
}}
QTabWidget#side_tabs > QTabBar::tab {{
    background: #e0e0e0;
    color: {_CLR_TEXT};
    font-size: 8pt;
    padding: 4px 10px;
    border: 1px solid {_CLR_ACCENT};
    border-bottom: none;
}}
QTabWidget#side_tabs > QTabBar::tab:selected {{
    background: {_CLR_ACCENT};
    color: #ffffff;
    font-weight: bold;
}}
QTabWidget#side_tabs > QTabBar::tab:hover:!selected {{
    background: #b0c4de;
}}

/* ── Lists and Trees ────────────────────────────────────── */
QListWidget, QTreeWidget {{
    background: {_CLR_BG_PANEL};
    color: {_CLR_TEXT};
    border: 1px solid {_CLR_ACCENT};
    font-size: {_FONT_SIZE}pt;
    font-family: {_FONT_UI};
}}
QListWidget::item:hover, QTreeWidget::item:hover {{
    background: #e5f3ff;
}}
QListWidget::item:selected {{
    background: #0078d4;
    color: #ffffff;
}}
QTreeView, QTreeWidget {{
    show-decoration-selected: 0;
    selection-background-color: #0078d4;
    selection-color: #ffffff;
}}

/* ── Properties panel ───────────────────────────────────── */
QFrame#props_panel {{
    background: {_CLR_BG_PANEL};
    border-left: 1px solid {_CLR_BORDER};
}}
QLabel#props_content {{
    color: {_CLR_TEXT};
    font-size: {_FONT_SIZE}pt;
    padding: 8px;
}}

/* ── Viewport placeholder ───────────────────────────────── */
QLabel#viewport_placeholder {{
    background: #ececec;
    color: {_CLR_TEXT_DIM};
    font-size: 13pt;
    font-family: {_FONT_UI};
}}

/* ── Status bar ─────────────────────────────────────────── */
QStatusBar {{
    background: {_CLR_BG_TOOLBAR};
    color: {_CLR_TEXT_DIM};
    font-size: 8pt;
    border-top: 1px solid {_CLR_BORDER};
}}

/* ── Loading overlay ─────────────────────────────────────── */
QWidget#loading_overlay {{
    background: #f0f0f0;
}}
QLabel#loading_label {{
    color: {_CLR_TEXT};
    font-size: 14px;
    background: transparent;
}}
QProgressBar {{
    background: #e0e0e0;
    border: 1px solid {_CLR_BORDER};
    border-radius: 4px;
    text-align: center;
    color: {_CLR_TEXT};
}}
QProgressBar::chunk {{
    background: qlineargradient(
        x1:0, y1:0.5, x2:1, y2:0.5,
        stop:0    rgba(0, 51, 102, 0),
        stop:0.5  {_CLR_ACCENT},
        stop:1    rgba(0, 51, 102, 0)
    );
    border-radius: 3px;
}}

/* ── Panel header (ITP Blue matching Modern Header) ──────── */
QLabel#panel_header {{
    background: {_CLR_HEADER};
    color: #ffffff;
    font-weight: bold;
    font-size: {_FONT_SIZE}pt;
    padding: 5px 8px;
    border-bottom: 1px solid #002244;
}}

/* ── Toolbar combo boxes ─────────────────────────────────── */
QToolBar#ribbon_tb QComboBox {{
    background: white;
    color: {_CLR_TEXT};
    border: 1px solid #bbbbbb;
    border-radius: 3px;
    padding: 2px 6px;
    min-width: 80px;
}}
QToolBar#ribbon_tb QComboBox::drop-down {{
    border: none;
}}

/* ── Activity bar (left vertical strip) ─────────────────── */
QWidget#activity_bar {{
    background: {_CLR_ACCENT};
    border-right: 1px solid #002244;
    min-width: 34px;
    max-width: 34px;
}}
QWidget#activity_bar QToolButton {{
    background: transparent;
    color: #ffffff;
    border: none;
    border-radius: 3px;
    padding: 6px 3px;
    min-width: 28px;
    min-height: 28px;
    font-size: 11pt;
    font-weight: bold;
}}
QWidget#activity_bar QToolButton:hover {{
    background: rgba(255,255,255,0.18);
}}
QWidget#activity_bar QToolButton:checked {{
    background: rgba(255,255,255,0.28);
    border-left: 3px solid #ffffff;
}}
QWidget#activity_bar QFrame#act_sep {{
    background: rgba(255,255,255,0.25);
    max-height: 1px;
    margin: 2px 4px;
}}
"""

# ---------------------------------------------------------------------------
#  Dark stylesheet — original industrial/sober palette (selectable via Theme)
# ---------------------------------------------------------------------------
_STYLESHEET_DARK = """
/* ── Window ─────────────────────────────────────────────── */
KModelViewerV2Dialog {
    background-color: #2b2b2b;
    color: #e0e0e0;
    font-family: Segoe UI;
    font-size: 9pt;
}
/* ── Tab ribbon ─────────────────────────────────────────── */
QTabWidget#ribbon_tabs::pane {
    border: none;
    background: #3a3a3a;
    margin-top: 0px;
    padding: 0px;
}
QTabWidget#ribbon_tabs > QTabBar::tab {
    background: #383838; color: #e0e0e0;
    font-family: Segoe UI; font-size: 9pt; font-weight: bold;
    padding: 6px 18px; border: 1px solid #555555;
    border-bottom: none; min-width: 80px;
}
QTabWidget#ribbon_tabs > QTabBar::tab:selected {
    background: #4a4a4a; color: #5b9bd5;
    border-bottom: 2px solid #5b9bd5;
}
QTabWidget#ribbon_tabs > QTabBar::tab:hover:!selected { background: #424242; }
/* ── Contextual toolbar band — flat buttons ────────────── */
QToolBar#ribbon_tb {
    background: #3a3a3a; border: none;
    border-bottom: 1px solid #555555; spacing: 2px; padding: 2px 6px;
}
QToolBar#ribbon_tb QToolButton {
    background: transparent; border: none; border-radius: 4px;
    color: #e0e0e0; font-size: 9pt;
    padding: 2px 6px; min-width: 40px; min-height: 54px;
}
QToolBar#ribbon_tb QToolButton:hover { background: #505050; }
QToolBar#ribbon_tb QToolButton:pressed,
QToolBar#ribbon_tb QToolButton:checked { background: #3d6a96; border: 1px solid #5b9bd5; }
QToolBar#ribbon_tb QLabel { color: #888888; font-size: 8pt; padding: 0 4px; }
QToolBar#ribbon_tb QToolButton::menu-indicator {
    subcontrol-position: right center; subcontrol-origin: padding;
    width: 10px; left: -2px;
}
QToolBar#ribbon_tb QToolButton[popupMode="1"] { padding-right: 14px; }
/* ── Splitter ─────────────────────────────────────────── */
QSplitter::handle { background: #555555; width: 3px; height: 3px; }
QSplitter::handle:hover { background: #5b9bd5; }
/* ── Side panels ─────────────────────────────────────── */
QTabWidget#side_tabs::pane { border: none; background: #313131; }
QTabWidget#side_tabs > QTabBar::tab {
    background: #383838; color: #e0e0e0; font-size: 8pt;
    padding: 4px 10px; border: 1px solid #555555; border-bottom: none;
}
QTabWidget#side_tabs > QTabBar::tab:selected {
    background: #313131; color: #5b9bd5; font-weight: bold;
}
/* ── Lists and Trees ─────────────────────────────────── */
QListWidget, QTreeWidget {
    background: #313131; color: #e0e0e0; border: none;
    font-size: 9pt; font-family: Segoe UI;
}
QListWidget::item:hover, QTreeWidget::item:hover { background: #404040; }
QListWidget::item:selected { background: #5b9bd5; color: #fff; }
QTreeView, QTreeWidget {
    show-decoration-selected: 0;
    selection-background-color: #5b9bd5;
    selection-color: #ffffff;
}
/* ── Properties panel ────────────────────────────────── */
QFrame#props_panel { background: #313131; border-left: 1px solid #555555; }
QLabel#props_content { color: #e0e0e0; font-size: 9pt; padding: 8px; }
/* ── Viewport placeholder ────────────────────────────── */
QLabel#viewport_placeholder {
    background: #1e1e1e; color: #888888; font-size: 13pt; font-family: Segoe UI;
}
/* ── Status bar ──────────────────────────────────────── */
QStatusBar { background: #3a3a3a; color: #888888; font-size: 8pt; border-top: 1px solid #555555; }
/* ── Loading overlay ─────────────────────────────────── */
QWidget#loading_overlay { background: #1a1a1a; }
QLabel#loading_label { color: #e0e0e0; font-size: 14px; background: transparent; }
QProgressBar {
    background: #404040; border: 1px solid #555555;
    border-radius: 4px; text-align: center; color: #e0e0e0;
}
QProgressBar::chunk {
    background: qlineargradient(
        x1:0, y1:0.5, x2:1, y2:0.5,
        stop:0    rgba(91, 155, 213, 0),
        stop:0.5  rgba(91, 155, 213, 255),
        stop:1    rgba(91, 155, 213, 0)
    );
    border-radius: 3px;
}
/* ── Panel header ────────────────────────────────────── */
QLabel#panel_header {
    background: #3c3c3c; color: #5b9bd5; font-weight: bold;
    font-size: 9pt; padding: 5px 8px; border-bottom: 1px solid #555555;
}
/* ── Toolbar combo boxes ─────────────────────────────── */
QToolBar#ribbon_tb QComboBox {
    background: #404040; color: #e0e0e0; border: 1px solid #555555;
    border-radius: 3px; padding: 2px 6px; min-width: 80px;
}
QToolBar#ribbon_tb QComboBox::drop-down { border: none; }
/* ── Activity bar (left vertical strip) ─────────────────── */
QWidget#activity_bar {
    background: #1a1a2e;
    border-right: 1px solid #444444;
    min-width: 34px;
    max-width: 34px;
}
QWidget#activity_bar QToolButton {
    background: transparent;
    color: #cccccc;
    border: none;
    border-radius: 3px;
    padding: 6px 3px;
    min-width: 28px;
    min-height: 28px;
    font-size: 11pt;
    font-weight: bold;
}
QWidget#activity_bar QToolButton:hover { background: rgba(255,255,255,0.12); }
QWidget#activity_bar QToolButton:checked {
    background: rgba(255,255,255,0.18);
    border-left: 3px solid #5b9bd5;
}
QWidget#activity_bar QFrame#act_sep {
    background: rgba(255,255,255,0.18);
    max-height: 1px;
    margin: 2px 4px;
}
/* ── Config tab (left panel) — light text on dark ────────── */
QWidget#cfg_panel QLabel,
QWidget#cfg_panel QCheckBox,
QWidget#cfg_panel QSpinBox { color: #e0e0e0; }
"""

# ---------------------------------------------------------------------------
#  Per-theme visual overrides for the viewer
#  These are appended on top of _STYLESHEET so they override specific rules
#  and make each theme visually distinct inside the viewer dialog.
# ---------------------------------------------------------------------------
_VIEWER_THEMES = {
    # ── Classic: raised / bevelled toolbar buttons (matches _base_stylesheet) ─
    "Classic": """
QToolBar#ribbon_tb QToolButton {
    background-color: #f0f0f0;
    border-top: 1px solid #ffffff;
    border-left: 1px solid #ffffff;
    border-bottom: 1px solid #909090;
    border-right: 1px solid #909090;
    border-radius: 2px;
    color: #333333;
    padding: 2px 6px;
    min-width: 40px;
    min-height: 54px;
}
QToolBar#ribbon_tb QToolButton:hover {
    background-color: #e0e8f8;
    border-top: 1px solid #ffffff;
    border-left: 1px solid #ffffff;
    border-bottom: 1px solid #8888aa;
    border-right: 1px solid #8888aa;
}
QToolBar#ribbon_tb QToolButton:pressed,
QToolBar#ribbon_tb QToolButton:checked {
    background-color: #c8c8c8;
    border-top: 1px solid #909090;
    border-left: 1px solid #909090;
    border-bottom: 1px solid #ffffff;
    border-right: 1px solid #ffffff;
}
QToolBar#ribbon_tb { background: #e8e8e8; }
QLabel#panel_header {
    background: #d8d8d8;
    color: #333333;
    border-bottom: 1px solid #aaaaaa;
}
QTabWidget#side_tabs > QTabBar::tab:selected {
    background: #e8e8e8;
    color: #333333;
    font-weight: bold;
}
""",

    # ── Modern: ITP Blue accent, flat buttons, white panels ──────────────
    "Modern": """
QToolBar#ribbon_tb { background: #f0f4f8; }
QToolBar#ribbon_tb QToolButton { color: #003366; }
QToolBar#ribbon_tb QToolButton:hover { background: #cfdce8; }
QToolBar#ribbon_tb QToolButton:pressed,
QToolBar#ribbon_tb QToolButton:checked {
    background: #003366;
    color: #ffffff;
    border: 1px solid #002244;
}
QLabel#panel_header {
    background: #003366;
    color: #ffffff;
    border-bottom: 1px solid #002244;
}
QTabWidget#side_tabs > QTabBar::tab:selected {
    background: #003366;
    color: #ffffff;
    font-weight: bold;
}
QTabWidget#ribbon_tabs > QTabBar::tab:selected {
    color: #003366;
    border-bottom: 2px solid #003366;
}
""",

    # ── Modern Header: same as _STYLESHEET defaults (ITP Blue, white bg) ─
    "Modern Header": "",

    # ── Dark: handled separately via _STYLESHEET_DARK ────────────────────
    "Dark (Viewer)": "",
}


def _panel_header(text: str, parent: QWidget | None = None) -> QLabel:
    lbl = QLabel(text, parent)
    lbl.setObjectName("panel_header")
    lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return lbl


class _ViewerClickFilter(QObject):
    """Qt event filter on the VTK interactor widget for click-to-select.

    We sit *above* VTK in the event stack: a Qt MousePress arrives here
    before being translated into a VTK ``LeftButtonPressEvent``. We track
    press → release positions; if the cursor barely moved we treat it as a
    click and call the dialog's pick / deselect routines.

    Left-click events are NEVER consumed so PyVista's trackball style still
    rotates on drag. Ctrl+Right-Click events ARE consumed (press + release)
    to suppress VTK's default right-button dolly while we use the gesture
    for individual-part deselection; plain right-button drag still works
    for dolly because we only intercept when Control is held at press time.
    """

    def __init__(self, dialog: "KModelViewerV2Dialog") -> None:
        super().__init__(dialog)
        self._dlg = dialog
        self._press_pos: tuple[int, int] | None = None
        self._rmb_press_pos: tuple[int, int] | None = None
        self._rmb_ctrl: bool = False

    def eventFilter(self, obj, event) -> bool:
        et = event.type()
        if et == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            p = event.position() if hasattr(event, "position") else event.pos()
            self._press_pos = (int(p.x()), int(p.y()))
        elif et == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            if self._press_pos is not None:
                p = event.position() if hasattr(event, "position") else event.pos()
                rx, ry = int(p.x()), int(p.y())
                px, py = self._press_pos
                self._press_pos = None
                if abs(rx - px) <= 3 and abs(ry - py) <= 3:
                    additive = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                    try:
                        self._dlg._do_viewport_pick(rx, ry, additive=additive)
                    except Exception as exc:
                        logger.debug("viewport click handler failed: %s", exc)
        elif et == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.RightButton:
            ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if ctrl:
                p = event.position() if hasattr(event, "position") else event.pos()
                self._rmb_press_pos = (int(p.x()), int(p.y()))
                self._rmb_ctrl = True
                return True  # block VTK's right-button dolly start
            self._rmb_press_pos = None
            self._rmb_ctrl = False
        elif et == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.RightButton:
            if self._rmb_ctrl and self._rmb_press_pos is not None:
                p = event.position() if hasattr(event, "position") else event.pos()
                rx, ry = int(p.x()), int(p.y())
                px, py = self._rmb_press_pos
                self._rmb_press_pos = None
                self._rmb_ctrl = False
                if abs(rx - px) <= 3 and abs(ry - py) <= 3:
                    try:
                        self._dlg._do_viewport_deselect(rx, ry)
                    except Exception as exc:
                        logger.debug("viewport deselect handler failed: %s", exc)
                return True  # consume so VTK never sees the right-button release
        return False  # never consume — VTK still receives the event


# ===========================================================================
#  KModelViewerV2Dialog — Phase 2 (full VTK rendering)
# ===========================================================================

class KModelViewerV2Dialog(QDialog):
    """3D Model Viewer 2.0 — industrial ribbon + three-panel layout + full VTK.

    Ribbon tabs: File | View
    Body:  [Left: Files + Navigator] | [Viewport: QtInteractor] | [Properties]

    Background .k parsing runs in viewer_v2_loader.LoadWorker (independent).
    All render/mesh/entity/tree logic is self-contained in this module.
    """

    def __init__(self, file_path: Path | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(
            f"3D Viewer 2.0  —  {file_path.name}" if file_path else "3D Viewer 2.0"
        )
        self.resize(1280, 760)
        self.setMinimumSize(900, 560)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        # Apply combined stylesheet: ribbon/panel overrides + per-theme overrides
        self._current_theme = "Modern Header"
        self.setStyleSheet(_STYLESHEET + _VIEWER_THEMES.get(self._current_theme, ""))

        # ── VTK state ──────────────────────────────────────────────────────
        self._file_path         = file_path
        self._polydata          = None
        self._part_names:  dict = {}
        self._keyword_entities: dict = {}
        self._actors:      list = []
        self._part_actors: dict = {}
        self._part_polydata: dict = {}
        self._entity_actors: dict = {}
        self._highlight_fe_actors: dict = {}
        self._highlighted_pids: set = set()
        self._per_part_style: dict = {}
        self._per_part_fx: dict = {}
        self._per_part_mesh: dict = {}
        # Material highlighting state
        self._materials: dict = {}            # mid -> {"title": str, "parts": set[pid]}
        self._part_to_mid: dict = {}          # pid -> mid
        self._part_base_color: dict = {}      # pid -> (r,g,b) base color (0..1)
        self._material_selected_mids: set = set()  # transient preview
        self._material_checked_mids: set = set()   # permanent (checkbox)
        self._part_user_visible: dict = {}    # pid -> bool (FEM Parts intent)
        self._pre_dim_opacity: dict = {}      # pid -> float (opacity snapshot before navigator-selection dim)
        self._phantom_enabled: bool = True    # Config: dim non-selected parts on selection
        self._phantom_opacity: float = 0.10   # Config: opacity applied to non-selected parts
        self._icon_text_enabled: bool = True  # Config: show icon + text vs icons-only in ribbons
        self._custom_edge_color: tuple | None = None  # (r,g,b) floats 0..1, None = auto (white on dark / black on light)
        self._custom_bg_color: str | None = None      # hex like "#aabbcc" if user picked a custom background
        self._ribbon_toolbars: list = []      # collected for icon/text style toggles
        self._ribbon_combo_buttons: list = []  # custom QToolButtons added via addWidget
        self._material_fe_actors: dict = {}   # pid -> vtkActor (orange feature edges)
        self._style_fe_actors: dict = {}      # pid -> vtkActor (per-part Wireframe boundary edges)
        self._wire_overlay_actor = None       # single combined Wireframe edge actor (global mode)
        # Optional second PyDyna polydata kept around for legacy code paths
        # (feature-edge experiments). Geometry already comes from PyDyna via
        # viewer_v2_loader, so this is normally unused.
        self._pydyna_polydata = None
        self._pydyna_polydata_failed = False
        self._has_parts      = False
        self._n_parts        = 0
        self._edges_visible  = True
        self._mesh_visible   = True
        self._axes_visible   = True
        self._bounds_visible = False
        self._parallel       = False
        self._current_bg     = "white"
        self._prev_bg        = "white"
        self._theme_is_dark  = False
        self._current_style  = "Surface+Edges"
        self._left_panel_open = True
        self._left_panel_size = _SPLIT_LEFT
        self._silhouette_actor     = None
        self._load_worker          = None
        self.plotter               = None   # QtInteractor (lazy)

        # ── Root layout ────────────────────────────────────────────────────
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_ribbon())
        root.addWidget(self._build_body(), stretch=1)

        self._status = QStatusBar()
        self._status.setSizeGripEnabled(True)
        self._status.showMessage("3D Viewer 2.0  —  ready")
        root.addWidget(self._status)

        # Esc clears the navigator selection so toolbar actions revert to global scope.
        self._esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._esc_shortcut.activated.connect(self._clear_tree_selection)

        if file_path:
            QTimer.singleShot(0, lambda: self._start_load(file_path))

    # =========================================================================
    #  Ribbon
    # =========================================================================

    def _build_ribbon(self) -> QWidget:
        container = QWidget()
        container.setFixedHeight(96)
        vlay = QVBoxLayout(container)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)

        self._ribbon_tabs = QTabWidget()
        self._ribbon_tabs.setObjectName("ribbon_tabs")
        self._ribbon_tabs.setDocumentMode(True)
        self._ribbon_tabs.setFixedHeight(96)

        for tab_label, builder in [
            ("File",       self._build_tb_file),
            ("View",       self._build_tb_view),
        ]:
            tb = builder()
            tb.setObjectName("ribbon_tb")
            tb.setMovable(False)
            tb.setFloatable(False)
            tb.setIconSize(QSize(32, 32) if tab_label == "View" else QSize(32, 32))
            tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            self._ribbon_toolbars.append(tb)
            page = QWidget()
            page.setFixedHeight(66)
            pl = QHBoxLayout(page)
            pl.setContentsMargins(0, 0, 0, 0)
            pl.setSpacing(0)
            pl.addWidget(tb)
            self._ribbon_tabs.addTab(page, tab_label)

        vlay.addWidget(self._ribbon_tabs)
        # Open on the "View" ribbon tab — it carries the most-used controls
        # (camera presets, render style, mesh toggle).
        self._ribbon_tabs.setCurrentIndex(1)
        return container

    # ── Tab: File ─────────────────────────────────────────────────────────

    def _build_tb_file(self) -> QToolBar:
        tb = QToolBar()
        self._a_open       = self._tb_act(tb, "Open",       "Ctrl+O",       "Import a .k model file",   self._action_import)
        _open_ico = os.path.join(_ICONS_DIR, "import_file.ico")
        if os.path.exists(_open_ico):
            self._a_open.setIcon(QIcon(_open_ico))
        
        tb.addSeparator()
        # Screenshot — assign imagen.ico (image/photo icon)
        self._a_screenshot = self._tb_act(tb, "Screenshot", "Ctrl+Shift+S", "Save viewport as PNG",     self._action_screenshot)
        _ss_ico = os.path.join(_ICONS_DIR, "imagen.ico")
        if os.path.exists(_ss_ico):
            self._a_screenshot.setIcon(QIcon(_ss_ico))
        tb.addSeparator()

        # ── Theme combo button ────────────────────────────────────────────
        self._theme_btn = QToolButton()
        # Use the appearance/palette icon for Theme; fall back to no icon
        _theme_ico = os.path.join(_ICONS_DIR, "theme2_viewer_v2.ico")
        if os.path.exists(_theme_ico):
            self._theme_btn.setIcon(QIcon(_theme_ico))
        self._theme_btn.setText("Theme")
        self._theme_btn.setToolTip("Select GUI theme")
        self._theme_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self._theme_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._theme_btn.setMinimumWidth(56)

        theme_menu = QMenu(self._theme_btn)
        for _name in ("Classic", "Modern", "Modern Header"):
            _act = theme_menu.addAction(_name)
            _act.triggered.connect(lambda checked=False, n=_name: self._apply_theme(n))
        theme_menu.addSeparator()
        _dark_act = theme_menu.addAction("Dark (Viewer)")
        _dark_act.triggered.connect(lambda: self._apply_theme("Dark (Viewer)"))
        self._theme_btn.setMenu(theme_menu)
        # Clicking the button face directly re-applies the current theme
        self._theme_btn.clicked.connect(lambda: self._apply_theme(self._current_theme))
        tb.addWidget(self._theme_btn)
        self._ribbon_combo_buttons.append(self._theme_btn)

        tb.addSeparator()
        self._tb_act(tb, "Close", "Ctrl+W", "Close viewer", self.close)
        return tb

    # ── Tab: View ─────────────────────────────────────────────────────────

    def _build_tb_view(self) -> QToolBar:  # noqa: PLR0915
        tb = QToolBar()

        # ── Render style dropdown ─────────────────────────────────────────
        _style_defs = [
            ("Surface+Edges", "surface_wire_MV.ico"),
            ("Surface",       "surface_MV.ico"),
            ("Wireframe",     "wire_MV.ico"),
            ("Points",        "points_MV.ico"),
        ]
        self._v2_style_menu = QMenu()
        self._v2_style_menu.setStyle(_large_icon_menu_style)
        _style_menu = self._v2_style_menu
        self._style_actions: dict[str, QAction] = {}
        self._style_group = QActionGroup(self)
        self._style_group.setExclusive(True)
        _style_dflt_icon = None
        for _lbl, _ico_name in _style_defs:
            _p = os.path.join(_ICONS_DIR, _ico_name)
            _icon = QIcon(_p) if os.path.exists(_p) else QIcon()
            if _style_dflt_icon is None:
                _style_dflt_icon = _icon
            _act = _style_menu.addAction(_icon, _lbl)
            _act.setCheckable(True)
            _act.setToolTip(f"Render style: {_lbl}")
            self._style_group.addAction(_act)
            self._style_actions[_lbl] = _act
        self._style_actions["Surface+Edges"].setChecked(True)
        self._v2_style_btn = QToolButton()
        self._v2_style_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._v2_style_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self._v2_style_btn.setIconSize(QSize(32, 32))
        self._v2_style_btn.setMinimumWidth(64)
        self._v2_style_btn.setMenu(_style_menu)
        if _style_dflt_icon:
            self._v2_style_btn.setIcon(_style_dflt_icon)
        self._v2_style_btn.setText("Style")
        self._v2_style_btn.setToolTip("Select render style")
        for _lbl, _ico_name in _style_defs:
            _p = os.path.join(_ICONS_DIR, _ico_name)
            _ico = QIcon(_p) if os.path.exists(_p) else QIcon()
            self._style_actions[_lbl].triggered.connect(
                lambda checked=False, l=_lbl, ic=_ico: [
                    self._v2_style_btn.setIcon(ic),
                    self._v2_style_btn.setText(l),
                    self._set_render_style(l),
                ]
            )
        self._v2_style_btn.clicked.connect(lambda: self._set_render_style("Surface+Edges"))
        tb.addWidget(self._v2_style_btn)
        self._ribbon_combo_buttons.append(self._v2_style_btn)

        tb.addSeparator()

        # ── View direction dropdown ───────────────────────────────────────
        _view_defs = [
            ("Front",  "front_view_MV.ico",  "Front view (–Y)",  lambda: self.plotter.view_vector((0, 1, 0), (0, 0, 1))),
            ("Back",   "back_view_MV.ico",   "Back view (+Y)",   lambda: self.plotter.view_vector((0, -1, 0), (0, 0, 1))),
            ("Left",   "left_view_MV.ico",   "Left view (–X)",   lambda: self.plotter.view_vector((1, 0, 0), (0, 0, 1))),
            ("Right",  "right_view_MV.ico",  "Right view (+X)",  lambda: self.plotter.view_vector((-1, 0, 0), (0, 0, 1))),
            ("Top",    "top_view_MV.ico",    "Top view (–Z)",    lambda: self.plotter.view_vector((0, 0, 1), (0, 1, 0))),
            ("Bottom", "bottom_view_MV.ico", "Bottom view (+Z)", lambda: self.plotter.view_vector((0, 0, -1), (0, 1, 0))),
            ("Iso",    "iso_view_MV.ico",    "Isometric view",   lambda: self.plotter.view_isometric()),
        ]
        self._v2_view_menu = QMenu()
        self._v2_view_menu.setStyle(_large_icon_menu_style)
        _view_menu = self._v2_view_menu
        _view_dflt_icon = None
        for _lbl, _ico_name, _tip, _fn in _view_defs:
            _p = os.path.join(_ICONS_DIR, _ico_name)
            _icon = QIcon(_p) if os.path.exists(_p) else QIcon()
            if _lbl == "Iso":
                _view_dflt_icon = _icon
            _act = _view_menu.addAction(_icon, _lbl)
            _act.setToolTip(_tip)
            _act.triggered.connect(
                lambda checked=False, fn=_fn, ic=_icon, l=_lbl: [
                    self._vtk_guarded(fn)(),
                    self._v2_view_btn.setIcon(ic),
                    self._v2_view_btn.setText(l),
                ]
            )
        self._v2_view_btn = QToolButton()
        self._v2_view_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._v2_view_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self._v2_view_btn.setIconSize(QSize(32, 32))
        self._v2_view_btn.setMinimumWidth(56)
        self._v2_view_btn.setMenu(_view_menu)
        if _view_dflt_icon:
            self._v2_view_btn.setIcon(_view_dflt_icon)
        self._v2_view_btn.setText("View")
        self._v2_view_btn.setToolTip("Select camera view direction")
        self._v2_view_btn.clicked.connect(self._vtk_guarded(lambda: self.plotter.view_isometric()))
        tb.addWidget(self._v2_view_btn)
        self._ribbon_combo_buttons.append(self._v2_view_btn)

        tb.addSeparator()

        # ── Zoom dropdown ─────────────────────────────────────────────────
        _zoom_defs = [
            ("Zoom In",  "zoom_in_MV.ico",  "Zoom in",  lambda: (self.plotter.camera.Zoom(1.3), self.plotter.render())),
            ("Zoom Out", "zoom_out_MV.ico", "Zoom out", lambda: (self.plotter.camera.Zoom(0.7), self.plotter.render())),
        ]
        self._v2_zoom_menu = QMenu()
        self._v2_zoom_menu.setStyle(_large_icon_menu_style)
        _zoom_menu = self._v2_zoom_menu
        _zoom_dflt_icon = None
        for _lbl, _ico_name, _tip, _fn in _zoom_defs:
            _p = os.path.join(_ICONS_DIR, _ico_name)
            _icon = QIcon(_p) if os.path.exists(_p) else QIcon()
            if _zoom_dflt_icon is None:
                _zoom_dflt_icon = _icon
            _act = _zoom_menu.addAction(_icon, _lbl)
            _act.setToolTip(_tip)
            _act.triggered.connect(self._vtk_guarded(_fn))
        self._v2_zoom_btn = QToolButton()
        self._v2_zoom_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._v2_zoom_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self._v2_zoom_btn.setIconSize(QSize(32, 32))
        self._v2_zoom_btn.setMinimumWidth(56)
        self._v2_zoom_btn.setMenu(_zoom_menu)
        if _zoom_dflt_icon:
            self._v2_zoom_btn.setIcon(_zoom_dflt_icon)
        self._v2_zoom_btn.setText("Zoom")
        self._v2_zoom_btn.setToolTip("Zoom in / out")
        self._v2_zoom_btn.clicked.connect(
            self._vtk_guarded(lambda: (self.plotter.camera.Zoom(1.3), self.plotter.render()))
        )
        tb.addWidget(self._v2_zoom_btn)
        self._ribbon_combo_buttons.append(self._v2_zoom_btn)

        # ── Fit All ───────────────────────────────────────────────────────
        _fit_p = os.path.join(_ICONS_DIR, "fit_MV.ico")
        _fit_icon = QIcon(_fit_p) if os.path.exists(_fit_p) else QIcon()
        self._a_fit = QAction(_fit_icon, "Fit All", self)
        self._a_fit.setToolTip("Reset camera to fit entire model")
        self._a_fit.triggered.connect(self._action_fit_all)
        tb.addAction(self._a_fit)

        # ── Zoom Window ───────────────────────────────────────────────────
        _zw_p = os.path.join(_ICONS_DIR, "zoom_window_MV.ico")
        _zw_icon = QIcon(_zw_p) if os.path.exists(_zw_p) else QIcon()
        self._a_zoom_window = QAction(_zw_icon, "Zoom Window", self)
        self._a_zoom_window.setCheckable(True)
        self._a_zoom_window.setToolTip(
            "Click two points in the viewer to zoom into that rectangle"
        )
        self._a_zoom_window.toggled.connect(self._action_toggle_zoom_window)
        tb.addAction(self._a_zoom_window)

        tb.addSeparator()

        # ── Axes toggle ───────────────────────────────────────────────────
        _axes_p = os.path.join(_ICONS_DIR, "axes_MV.ico")
        _axes_icon = QIcon(_axes_p) if os.path.exists(_axes_p) else QIcon()
        self._a_axes = QAction(_axes_icon, "Axes", self)
        self._a_axes.setCheckable(True)
        self._a_axes.setChecked(True)
        self._a_axes.setToolTip("Toggle orientation axes widget")
        self._a_axes.triggered.connect(self._action_toggle_axes)
        tb.addAction(self._a_axes)

        # ── Grid toggle ───────────────────────────────────────────────────
        _grid_p = os.path.join(_ICONS_DIR, "ruler_MV.ico")
        _grid_icon = QIcon(_grid_p) if os.path.exists(_grid_p) else QIcon()
        self._a_grid = QAction(_grid_icon, "Grid", self)
        self._a_grid.setCheckable(True)
        self._a_grid.setChecked(False)
        self._a_grid.setToolTip("Toggle bounding grid with coordinates")
        self._a_grid.triggered.connect(self._action_toggle_grid)
        tb.addAction(self._a_grid)

        # ── Parallel/Perspective toggle ───────────────────────────────────
        _persp_p = os.path.join(_ICONS_DIR, "perspective_MV.ico")
        _ortho_p = os.path.join(_ICONS_DIR, "orthogonal_MV.ico")
        self._v2_persp_icon = QIcon(_persp_p) if os.path.exists(_persp_p) else QIcon()
        self._v2_ortho_icon = QIcon(_ortho_p) if os.path.exists(_ortho_p) else QIcon()
        self._a_proj = QAction(self._v2_persp_icon, "Perspective", self)
        self._a_proj.setCheckable(True)
        self._a_proj.setChecked(False)
        self._a_proj.setToolTip("Toggle parallel / perspective projection")
        self._a_proj.triggered.connect(self._action_toggle_projection)
        tb.addAction(self._a_proj)

        tb.addSeparator()

        # ── Background dropdown ───────────────────────────────────────────
        _bg_defs = [
            ("White",      "backgroun_white_MV.ico",         "white"),
            ("Light Gray", "backgroun_grey_MV.ico",          "lightgray"),
            ("Dark Gray",  "backgroun_dark_grey_MV.ico",     "#3c3c3c"),
            ("Black",      "backgroun_black_MV.ico",         "black"),
            ("Grad Blue",  "backgroun_gradient_MV.ico",      "__gradient_blue"),
            ("Grad Gray",  "backgroun_gradient_grey_MV.ico", "__gradient_gray"),
        ]
        self._v2_bg_menu = QMenu()
        self._v2_bg_menu.setStyle(_large_icon_menu_style)
        _bg_menu = self._v2_bg_menu
        _bg_dflt_icon = None
        for _lbl, _ico_name, _bg_key in _bg_defs:
            _p = os.path.join(_ICONS_DIR, _ico_name)
            _icon = QIcon(_p) if os.path.exists(_p) else QIcon()
            if _bg_dflt_icon is None:
                _bg_dflt_icon = _icon
            _act = _bg_menu.addAction(_icon, _lbl)
            _act.setToolTip(f"Background: {_lbl}")
            _act.triggered.connect(
                lambda checked=False, bk=_bg_key, ic=_icon, l=_lbl: [
                    self._set_background(bk),
                    self._v2_bg_btn.setIcon(ic),
                    self._v2_bg_btn.setText(l),
                ]
            )
        self._v2_bg_btn = QToolButton()
        self._v2_bg_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._v2_bg_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self._v2_bg_btn.setIconSize(QSize(32, 32))
        self._v2_bg_btn.setMinimumWidth(56)
        self._v2_bg_btn.setMenu(_bg_menu)
        if _bg_dflt_icon:
            self._v2_bg_btn.setIcon(_bg_dflt_icon)
        self._v2_bg_btn.setText("BG Color")
        self._v2_bg_btn.setToolTip("Background color")
        self._v2_bg_btn.clicked.connect(lambda: self._set_background("white"))
        tb.addWidget(self._v2_bg_btn)
        self._ribbon_combo_buttons.append(self._v2_bg_btn)

        tb.addSeparator()

        # ── Opacity combo ─────────────────────────────────────────────────
        tb.addWidget(QLabel(" Opacity: "))
        self._v2_opacity_combo = QComboBox()
        self._v2_opacity_combo.addItems(["100%", "90%", "75%", "50%", "25%"])
        self._v2_opacity_combo.setFixedWidth(72)
        self._v2_opacity_combo.setToolTip("Mesh opacity")
        self._v2_opacity_combo.currentTextChanged.connect(self._set_opacity)
        tb.addWidget(self._v2_opacity_combo)

        tb.addSeparator()

        # ── Mesh toggle ───────────────────────────────────────────────────
        _mesh_p = os.path.join(_ICONS_DIR, "mesh_MV.ico")
        _mesh_icon = QIcon(_mesh_p) if os.path.exists(_mesh_p) else QIcon()
        self._v2_a_mesh = QAction(_mesh_icon, "Mesh", self)
        self._v2_a_mesh.setCheckable(True)
        self._v2_a_mesh.setChecked(True)
        self._v2_a_mesh.setToolTip("Toggle mesh wireframe")
        self._v2_a_mesh.triggered.connect(self._action_toggle_mesh)
        tb.addAction(self._v2_a_mesh)

        tb.addSeparator()

        # ── Visual effects dropdown ───────────────────────────────────────
        _fx_defs = [
            ("Silhouette",     "Si", "Add silhouette contour around model"),
            ("PBR (Metallic)", "PB", "Physically-based metallic rendering"),
            ("Smooth Shading", "Sm", "Enable smooth (Phong) shading"),
            ("Anti-aliasing",  "AA", "Multi-sample anti-aliasing"),
            ("SSAO",           "AO", "Screen-space ambient occlusion (shadows in crevices)"),
            ("Depth Peeling",  "Dp", "Accurate transparency (depth peeling)"),
        ]
        self._v2_fx_menu = QMenu()
        self._v2_fx_menu.setStyle(_large_icon_menu_style)
        _fx_menu = self._v2_fx_menu
        self._v2_fx_actions: dict[str, QAction] = {}
        for _lbl, _abbr, _tip in _fx_defs:
            _ico_path = os.path.join(
                _ICONS_DIR,
                f"{_lbl.lower().replace(' ', '_').replace('(', '').replace(')', '')}_MV.ico"
            )
            _ico = QIcon(_ico_path) if os.path.exists(_ico_path) else _icon_from_text(_abbr)
            _act = _fx_menu.addAction(_ico, _lbl)
            _act.setCheckable(True)
            _act.setChecked(False)
            _act.setToolTip(_tip)
            _act.toggled.connect(self._make_fx_callback(_lbl))
            self._v2_fx_actions[_lbl] = _act
        _fx_p = os.path.join(_ICONS_DIR, "fx.ico")
        _fx_icon = QIcon(_fx_p) if os.path.exists(_fx_p) else QIcon()
        self._v2_fx_btn = QToolButton()
        self._v2_fx_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._v2_fx_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self._v2_fx_btn.setIconSize(QSize(32, 32))
        self._v2_fx_btn.setMinimumWidth(48)
        self._v2_fx_btn.setIcon(_fx_icon)
        self._v2_fx_btn.setText("FX")
        self._v2_fx_btn.setMenu(_fx_menu)
        self._v2_fx_btn.setToolTip("Visual effects")
        self._v2_fx_btn.clicked.connect(lambda: self._v2_fx_btn.showMenu())
        tb.addWidget(self._v2_fx_btn)
        self._ribbon_combo_buttons.append(self._v2_fx_btn)

        tb.addSeparator()

        # ── Orbit (continuous rotation) ───────────────────────────────────────
        _orbit_p = os.path.join(_ICONS_DIR, "orbit.ico")
        _orbit_icon = QIcon(_orbit_p) if os.path.exists(_orbit_p) else QIcon()
        self._a_orbit = QAction(_orbit_icon, "Orbit", self)
        self._a_orbit.setCheckable(True)
        self._a_orbit.setChecked(False)
        self._a_orbit.setToolTip("Toggle continuous orbit rotation")
        self._a_orbit.triggered.connect(self._action_toggle_orbit)
        tb.addAction(self._a_orbit)
        self._orbit_timer = QTimer(self)
        self._orbit_timer.setInterval(33)   # ~30 fps
        self._orbit_timer.timeout.connect(self._orbit_step)

        tb.addSeparator()

        # ── Properties panel toggle ──────────────────────────────────────────
        _props_p = os.path.join(_ICONS_DIR, "property.ico")
        _props_icon = QIcon(_props_p) if os.path.exists(_props_p) else _icon_from_text("Pr")
        self._a_props = QAction(_props_icon, "Properties", self)
        self._a_props.setCheckable(True)
        self._a_props.setChecked(True)
        self._a_props.setToolTip("Show / hide the Properties panel")
        self._a_props.triggered.connect(self._action_toggle_properties_panel)
        tb.addAction(self._a_props)

        return tb

    # ── Tab: Configuration ────────────────────────────────────────────────

    def _build_tb_configuration(self) -> QToolBar:
        tb = QToolBar()
        self._tb_act(tb, "Clear Cache", None, "Delete cached VTK data for this file", self._action_clear_cache)
        tb.addSeparator()
        self._tb_act(tb, "About", None, "About 3D Viewer 2.0", self._action_about)
        return tb

    @staticmethod
    def _tb_act(tb: QToolBar, label: str, shortcut: str | None, tooltip: str, slot) -> QAction:
        act = QAction(label)
        if shortcut:
            act.setShortcut(shortcut)
        act.setToolTip(tooltip)
        act.triggered.connect(slot)
        tb.addAction(act)
        return act

    def _vtk_guarded(self, fn):
        def _wrapper():
            if self.plotter is not None:
                fn()
                self.plotter.render()
        return _wrapper

    # =========================================================================
    #  Body — three-panel splitter
    # =========================================================================

    def _build_body(self) -> QWidget:
        """Build the body area: activity bar + three-panel splitter."""
        body = QWidget()
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)

        body_lay.addWidget(self._build_activity_bar())
        body_lay.addWidget(self._build_splitter(), stretch=1)
        return body

    def _build_splitter(self) -> QSplitter:
        spl = QSplitter(Qt.Orientation.Horizontal)
        spl.setHandleWidth(4)
        spl.setChildrenCollapsible(True)
        spl.addWidget(self._build_left_panel())
        spl.addWidget(self._build_viewport_area())
        self._right_panel = self._build_right_panel()
        spl.addWidget(self._right_panel)
        spl.setSizes([_SPLIT_LEFT, _SPLIT_CENTER, _SPLIT_RIGHT])
        spl.setStretchFactor(0, 0)
        spl.setStretchFactor(1, 1)
        spl.setStretchFactor(2, 0)
        self._main_splitter = spl
        return spl

    # ── Activity bar ──────────────────────────────────────────────────────

    def _build_activity_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("activity_bar")
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(3, 6, 3, 6)
        lay.setSpacing(4)
        lay.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        # Toggle button — collapses / expands the left panel
        self._act_toggle_btn = QToolButton()
        self._act_toggle_btn.setText("◀")
        self._act_toggle_btn.setToolTip("Hide/show side panel")
        self._act_toggle_btn.setCheckable(False)
        self._act_toggle_btn.clicked.connect(self._toggle_left_panel)
        lay.addWidget(self._act_toggle_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Visual separator
        sep = QFrame()
        sep.setObjectName("act_sep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        # Files tab button        
        self._act_files_btn = QToolButton()
        _file_ico = os.path.join(_ICONS_DIR, "open_file_viewer_v2.ico")
        if os.path.exists(_file_ico):
            self._act_files_btn.setIcon(QIcon(_file_ico))
            self._act_files_btn.setIconSize(QSize(26, 26))
        else:
            self._act_files_btn.setText("📁")
        self._act_files_btn.setToolTip("Project Files")
        self._act_files_btn.setCheckable(True)
        self._act_files_btn.clicked.connect(lambda: self._act_show_tab(0))
        lay.addWidget(self._act_files_btn, alignment=Qt.AlignmentFlag.AlignHCenter)
        
        # Navigator tab button
        self._act_nav_btn = QToolButton()
        _nav_ico = os.path.join(_ICONS_DIR, "navigator_tree.ico")
        if os.path.exists(_nav_ico):
            self._act_nav_btn.setIcon(QIcon(_nav_ico))
            self._act_nav_btn.setIconSize(QSize(26, 26))
        else:
            self._act_nav_btn.setText("🌲")
        self._act_nav_btn.setToolTip("Model Navigator")
        self._act_nav_btn.setCheckable(True)
        self._act_nav_btn.clicked.connect(lambda: self._act_show_tab(1))
        lay.addWidget(self._act_nav_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Configuration tab button       
        self._act_cfg_btn = QToolButton()
        _cfg_ico = os.path.join(_ICONS_DIR, "settings_viewer_v2.ico")
        if os.path.exists(_cfg_ico):
            self._act_cfg_btn.setIcon(QIcon(_cfg_ico))
            self._act_cfg_btn.setIconSize(QSize(26, 26))
        else:
            self._act_cfg_btn.setText("🌲")
        self._act_cfg_btn.setToolTip("Model Navigator")
        self._act_cfg_btn.setCheckable(True)
        self._act_cfg_btn.clicked.connect(lambda: self._act_show_tab(2))
        lay.addWidget(self._act_cfg_btn, alignment=Qt.AlignmentFlag.AlignHCenter)
        

        lay.addStretch()
        return bar

    def _toggle_left_panel(self):
        """Collapse or expand the left side panel."""
        if self._left_panel_open:
            sizes = self._main_splitter.sizes()
            self._left_panel_size = max(sizes[0], 60)
            total = sizes[0] + sizes[1]
            self._main_splitter.setSizes([0, total, sizes[2]])
            self._act_toggle_btn.setText("▶")
            self._act_toggle_btn.setToolTip("Show side panel")
        else:
            sizes = self._main_splitter.sizes()
            w = self._left_panel_size or _SPLIT_LEFT
            center = max(200, sizes[0] + sizes[1] - w)
            self._main_splitter.setSizes([w, center, sizes[2]])
            self._act_toggle_btn.setText("◀")
            self._act_toggle_btn.setToolTip("Hide side panel")
        self._left_panel_open = not self._left_panel_open

    def _act_show_tab(self, index: int):
        """Show a specific tab in the left panel, opening it if collapsed."""
        # Update checked state of activity buttons
        self._act_files_btn.setChecked(index == 0)
        self._act_nav_btn.setChecked(index == 1)
        self._act_cfg_btn.setChecked(index == 2)
        # Switch tab
        self._left_tabs.setCurrentIndex(index)
        # If panel is collapsed, expand it
        if not self._left_panel_open:
            self._toggle_left_panel()

    def _on_left_tab_changed(self, index: int):
        """Sync activity bar button checked state when tab changes directly."""
        self._act_files_btn.setChecked(index == 0)
        self._act_nav_btn.setChecked(index == 1)
        self._act_cfg_btn.setChecked(index == 2)

    # ── Left panel ────────────────────────────────────────────────────────

    def _build_left_panel(self) -> QTabWidget:
        tabs = QTabWidget()
        tabs.setObjectName("side_tabs")
        tabs.setMinimumWidth(160)

        # Tab 1: Project Files
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(_panel_header("Project Files"))
        self._files_list = QListWidget()
        self._files_list.setToolTip("Double-click a .k file to open it")
        self._files_list.itemDoubleClicked.connect(self._on_file_double_clicked)
        lay.addWidget(self._files_list)
        self._populate_files_list()
        tabs.addTab(w, "Files")

        # Tab 2: Model Navigator
        w2 = QWidget()
        lay2 = QVBoxLayout(w2)
        lay2.setContentsMargins(0, 0, 0, 0)
        lay2.setSpacing(0)
        lay2.addWidget(_panel_header("Model Navigator"))
        self._nav_tree = QTreeWidget()
        self._nav_tree.setObjectName("side_tree")
        self._nav_tree.setHeaderHidden(True)
        self._nav_tree.setColumnCount(1)
        self._nav_tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        # Allow column to grow beyond panel width; horizontal scrollbar appears as needed
        self._nav_tree.header().setStretchLastSection(False)
        self._nav_tree.header().setSectionResizeMode(
            0, self._nav_tree.header().ResizeMode.ResizeToContents
        )
        self._nav_tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._nav_tree.itemChanged.connect(self._on_nav_item_checked)
        self._nav_tree.itemSelectionChanged.connect(self._on_nav_selection_changed)
        self._nav_tree_placeholder()
        lay2.addWidget(self._nav_tree)
        tabs.addTab(w2, "Navigator")

        # Tab 3: Configuration
        w3 = QWidget()
        lay3 = QVBoxLayout(w3)
        lay3.setContentsMargins(0, 0, 0, 0)
        lay3.setSpacing(0)
        lay3.addWidget(_panel_header("Configuration"))
        cfg_inner = QWidget()
        cfg_inner.setObjectName("cfg_panel")
        cfg_lay = QVBoxLayout(cfg_inner)
        cfg_lay.setContentsMargins(8, 8, 8, 8)
        cfg_lay.setSpacing(6)
        from PySide6.QtWidgets import QPushButton

        # ── Phantom mode ────────────────────────────────────────────────
        self._cfg_phantom_check = QCheckBox("Phantom mode")
        self._cfg_phantom_check.setToolTip(
            "Dim non-selected parts when a part is selected in the navigator"
        )
        self._cfg_phantom_check.setChecked(self._phantom_enabled)
        self._cfg_phantom_check.toggled.connect(self._on_phantom_toggled)
        cfg_lay.addWidget(self._cfg_phantom_check)

        opacity_row = QHBoxLayout()
        opacity_row.setContentsMargins(20, 0, 0, 0)  # indent under the checkbox
        opacity_row.setSpacing(6)
        opacity_row.addWidget(QLabel("Phantom opacity:"))
        self._cfg_phantom_opacity = QSpinBox()
        self._cfg_phantom_opacity.setRange(0, 100)
        self._cfg_phantom_opacity.setSingleStep(5)
        self._cfg_phantom_opacity.setSuffix(" %")
        self._cfg_phantom_opacity.setValue(int(round(self._phantom_opacity * 100)))
        self._cfg_phantom_opacity.setToolTip(
            "Opacity applied to non-selected parts while Phantom mode is on"
        )
        self._cfg_phantom_opacity.valueChanged.connect(self._on_phantom_opacity_changed)
        opacity_row.addWidget(self._cfg_phantom_opacity)
        opacity_row.addStretch(1)
        cfg_lay.addLayout(opacity_row)

        cfg_lay.addSpacing(8)

        # ── Icon and Text ───────────────────────────────────────────────
        self._cfg_icon_text_check = QCheckBox("Icon and Text")
        self._cfg_icon_text_check.setToolTip(
            "Show button labels under their icons in the ribbon toolbars"
        )
        self._cfg_icon_text_check.setChecked(self._icon_text_enabled)
        self._cfg_icon_text_check.toggled.connect(self._on_icon_text_toggled)
        cfg_lay.addWidget(self._cfg_icon_text_check)

        cfg_lay.addSpacing(8)

        # ── Model Display: custom colors ────────────────────────────────
        _md_header = QLabel("Model Display")
        _md_font = _md_header.font()
        _md_font.setBold(True)
        _md_header.setFont(_md_font)
        cfg_lay.addWidget(_md_header)

        # Background Color row
        bg_row = QHBoxLayout()
        bg_row.setContentsMargins(20, 0, 0, 0)
        bg_row.setSpacing(8)
        bg_row.addWidget(QLabel("Background Color:"))
        self._cfg_bg_swatch = self._make_color_swatch_btn(self._bg_swatch_initial_rgb())
        self._cfg_bg_swatch.setToolTip("Pick a custom background color")
        self._cfg_bg_swatch.clicked.connect(self._on_pick_bg_color)
        bg_row.addWidget(self._cfg_bg_swatch)
        bg_row.addStretch(1)
        cfg_lay.addLayout(bg_row)

        # Edge Color row
        edge_row = QHBoxLayout()
        edge_row.setContentsMargins(20, 0, 0, 0)
        edge_row.setSpacing(8)
        edge_row.addWidget(QLabel("Edge Color:"))
        self._cfg_edge_swatch = self._make_color_swatch_btn((0.0, 0.0, 0.0))
        self._cfg_edge_swatch.setToolTip("Pick a custom edge color for parts")
        self._cfg_edge_swatch.clicked.connect(self._on_pick_edge_color)
        edge_row.addWidget(self._cfg_edge_swatch)
        _edge_reset = QPushButton("Auto")
        _edge_reset.setToolTip("Reset edge color to follow the theme (white on dark / black on light)")
        _edge_reset.setFixedWidth(56)
        _edge_reset.clicked.connect(self._on_reset_edge_color)
        edge_row.addWidget(_edge_reset)
        edge_row.addStretch(1)
        cfg_lay.addLayout(edge_row)

        cfg_lay.addSpacing(8)

        _btn_cache = QPushButton("Clear Cache")
        _btn_cache.setToolTip("Delete cached VTK data for this file")
        _btn_cache.clicked.connect(self._action_clear_cache)
        cfg_lay.addWidget(_btn_cache)
        _btn_about = QPushButton("About")
        _btn_about.setToolTip("About 3D Viewer 2.0")
        _btn_about.clicked.connect(self._action_about)
        cfg_lay.addWidget(_btn_about)
        cfg_lay.addStretch(1)
        lay3.addWidget(cfg_inner)
        tabs.addTab(w3, "Config")

        self._left_tabs = tabs
        tabs.currentChanged.connect(self._on_left_tab_changed)
        # Open on the Navigator tab — it's the primary surface for picking
        # parts and seeing properties, so users land there directly. The
        # currentChanged signal syncs the activity-bar buttons.
        tabs.setCurrentIndex(1)
        return tabs

    # ── Viewport area ─────────────────────────────────────────────────────

    def _build_viewport_area(self) -> QWidget:
        container = QWidget()
        container.setMinimumWidth(300)
        lay = QVBoxLayout(container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._viewport_placeholder = QLabel(
            "3D Viewport\n\n"
            "Open a .k file via  File › Open\n"
            "or double-click a file in the  Files  panel"
        )
        self._viewport_placeholder.setObjectName("viewport_placeholder")
        self._viewport_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._viewport_placeholder.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay.addWidget(self._viewport_placeholder)

        self._loading_overlay = QWidget()
        self._loading_overlay.setObjectName("loading_overlay")
        lo_lay = QVBoxLayout(self._loading_overlay)
        lo_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_label = QLabel("Loading model…")
        self._loading_label.setObjectName("loading_label")
        self._loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lo_lay.addWidget(self._loading_label)
        self._loading_bar = QProgressBar()
        self._loading_bar.setRange(0, 0)
        self._loading_bar.setFixedWidth(320)
        lo_lay.addWidget(self._loading_bar, alignment=Qt.AlignmentFlag.AlignCenter)
        self._loading_overlay.hide()
        lay.addWidget(self._loading_overlay)

        self._viewport_lay = lay
        self._viewport_container = container
        return container

    # ── Right panel ───────────────────────────────────────────────────────

    def _build_right_panel(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("props_panel")
        frame.setMinimumWidth(150)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(_panel_header("Properties"))
        self._props_label = QLabel(
            "Select a part or element\nin the navigator or viewport\nto view properties."
        )
        self._props_label.setObjectName("props_content")
        self._props_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._props_label.setWordWrap(True)

        # Wrap the label in a QScrollArea so a vertical scrollbar appears
        # automatically when the properties content (many selected parts)
        # exceeds the panel height. Horizontal scroll stays off because
        # _resize_props_panel_to_fit() already grows the panel width to fit
        # the longest line (capped at 50% of the dialog).
        self._props_scroll = QScrollArea()
        self._props_scroll.setObjectName("props_scroll")
        self._props_scroll.setWidgetResizable(True)
        self._props_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._props_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._props_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._props_scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        self._props_scroll.setWidget(self._props_label)
        lay.addWidget(self._props_scroll, 1)
        return frame

    def _populate_files_list(self):
        """Populate the Files tab with .k files from the project directory."""
        self._files_list.clear()
        if self._file_path is None:
            item = QListWidgetItem("(no project file loaded)")
            item.setForeground(QColor(_CLR_TEXT_DIM))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._files_list.addItem(item)
            return

        folder = self._file_path.parent
        try:
            k_files = sorted(
                p for p in folder.iterdir()
                if p.suffix.lower() in (".k", ".key", ".dyn") and p.is_file()
            )
        except Exception:
            k_files = []

        if not k_files:
            item = QListWidgetItem("(no .k files found)")
            item.setForeground(QColor(_CLR_TEXT_DIM))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._files_list.addItem(item)
            return

        for p in k_files:
            item = QListWidgetItem(p.name)
            item.setData(Qt.ItemDataRole.UserRole, p)
            if p == self._file_path:
                item.setForeground(QColor(_CLR_ACCENT))
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self._files_list.addItem(item)

    def _on_file_double_clicked(self, item: QListWidgetItem):
        path: Path | None = item.data(Qt.ItemDataRole.UserRole)
        if path and path.is_file():
            self._file_path = path
            self.setWindowTitle(f"3D Viewer 2.0  —  {path.name}")
            self._populate_files_list()
            self._start_load(path)

    # =========================================================================
    #  Loading & VTK lifecycle
    # =========================================================================

    def _ensure_plotter(self):
        if self.plotter is not None:
            return
        try:
            from pyvistaqt import QtInteractor
        except ImportError as exc:
            QMessageBox.critical(self, "3D Viewer",
                                 f"pyvistaqt is required for the 3D viewport.\n{exc}")
            return
        self.plotter = QtInteractor(parent=self._viewport_container)
        self._viewport_lay.insertWidget(0, self.plotter, stretch=1)

    def _show_loading(self, filename: str):
        self._loading_label.setText(f"Loading  {filename}…")
        self._viewport_placeholder.hide()
        if self.plotter is not None:
            self.plotter.hide()
        self._loading_overlay.show()
        self._status.showMessage(f"Parsing {filename}…")

    def _hide_loading(self):
        self._loading_overlay.hide()
        if self.plotter is not None:
            self.plotter.show()

    def _start_load(self, path: Path):
        if self._load_worker is not None:
            try:
                self._load_worker.finished.disconnect()
            except RuntimeError:
                pass
            try:
                self._load_worker.geometry_ready.disconnect()
            except RuntimeError:
                pass
            self._load_worker.quit()
            self._load_worker.wait(2000)
        self._show_loading(path.name)
        from ui.viewer_v2_loader import LoadWorker
        self._load_worker = LoadWorker(path)
        self._load_worker.geometry_ready.connect(self._on_geometry_ready)
        self._load_worker.finished.connect(self._on_loaded)
        self._load_worker.start()

    def _on_geometry_ready(self, polydata, part_names):
        """Render the model as soon as polydata is ready (entities arrive later)."""
        self._hide_loading()
        self._polydata         = polydata
        # Normalize keys to plain Python int. The loader may produce np.int32
        # keys (from element-derived part_ids) and JSON cache may produce str
        # keys; both must match the int(pid) lookups used elsewhere.
        try:
            self._part_names = {int(k): v for k, v in (part_names or {}).items()}
        except Exception:
            self._part_names = dict(part_names or {})
        self._keyword_entities = {}
        if polydata is not None:
            has_parts = "part_ids" in polydata.cell_data
            self._n_parts   = len(set(polydata.cell_data["part_ids"])) if has_parts else 0
            self._has_parts = has_parts
        else:
            self._has_parts = False
            self._n_parts   = 0
        self._ensure_plotter()
        if self.plotter is None:
            return
        self.plotter.clear()
        self._actors.clear()
        self._part_actors.clear()
        self._part_polydata.clear()
        self._per_part_style.clear()
        self._per_part_fx.clear()
        self._per_part_mesh.clear()
        # Reset material highlighting state for the new model.
        self._materials = {}
        self._part_to_mid = {}
        self._material_selected_mids = set()
        self._material_checked_mids = set()
        self._part_user_visible = {}
        self._pre_dim_opacity = {}
        self._material_fe_actors = {}  # actors are dropped by plotter.clear() above
        self._style_fe_actors = {}     # ditto
        self._wire_overlay_actor = None  # ditto
        self._remove_all_entity_actors()
        self._remove_highlight_edges()
        self._silhouette_actor    = None
        if polydata is not None:
            self._add_model_mesh()
            self._part_user_visible = {pid: True for pid in self._part_actors}
            # Default style is Surface+Edges → ensure the feature-edge overlay
            # is present from the start. (Actor element-edges already on via
            # show_edges=True in _add_model_mesh.)
            if self._current_style != "Surface":
                self._rebuild_wire_overlay(
                    as_points=(self._current_style == "Points")
                )
            self._update_status()
        else:
            self.plotter.set_background(self._current_bg)
            self.plotter.add_axes()
            self._status.showMessage("No geometry in file")
        self._rebuild_nav_tree()

    def _on_loaded(self, polydata, part_names, kw_entities, error):
        """Final emission: SET entities are now ready; refresh the nav tree."""
        self._load_worker = None
        self._hide_loading()
        if error:
            self._status.showMessage(f"Error: {error}")
            QMessageBox.critical(self, "Load error", error)
            return
        # Geometry was already rendered via _on_geometry_ready; just merge entities.
        if self._polydata is None and polydata is not None:
            self._on_geometry_ready(polydata, part_names)
        else:
            # The geometry signal fires before walk_materials runs, so headings
            # and *INCLUDE_TRANSFORM titles captured from .k files only land in
            # the *finished* part_names dict. The loader resolves the final
            # name per PID (heading vs transform title) — adopt its decision
            # wholesale for any PID present, so transform-title overrides take
            # effect even when PyDyna already supplied a part heading.
            try:
                refreshed = {int(k): v for k, v in (part_names or {}).items()}
            except Exception:
                refreshed = dict(part_names or {})
            for pid, name in refreshed.items():
                if name:
                    self._part_names[pid] = name
        self._keyword_entities = kw_entities or {}
        # Materials come from the loader under "__materials__"; filter to those
        # actually referenced by at least one *PART and attach the PID set.
        self._consume_materials_payload(self._keyword_entities.get("__materials__"))
        self._rebuild_nav_tree()
        # Refresh properties panel so material info appears for any parts that
        # were already selected before the loader's final emission.
        try:
            self._update_properties_panel(self._get_selected_pids())
        except Exception:
            logger.debug("properties refresh after load failed", exc_info=True)

    # =========================================================================
    #  Mesh
    # =========================================================================

    def _get_part_colors(self) -> dict:
        try:
            from matplotlib import colormaps
            cmap = colormaps.get_cmap("tab20")
        except Exception:
            cmap = None
        part_ids = sorted(set(self._polydata.cell_data["part_ids"])) if self._has_parts else []
        colors: dict = {}
        for idx, pid in enumerate(part_ids):
            if cmap is not None:
                rgba = cmap(idx % cmap.N if hasattr(cmap, "N") else idx / max(len(part_ids), 1))
                colors[pid] = list(rgba[:3])
            else:
                colors[pid] = "steelblue"
        return colors

    def _add_model_mesh(self):
        import numpy as np
        pd = self._polydata
        self._part_actors.clear()
        self._part_polydata.clear()
        self._part_base_color.clear()
        try:
            self.plotter.enable_lightkit()
        except Exception:
            pass
        if self._has_parts:
            part_ids_arr = pd.cell_data["part_ids"]
            colors = self._get_part_colors()
            for pid in sorted(set(part_ids_arr)):
                mask = part_ids_arr == pid
                sub  = pd.extract_cells(np.where(mask)[0])
                if sub.n_cells == 0:
                    continue
                self._part_polydata[pid] = sub
                name  = self._part_names.get(pid, f"Part {pid}")
                color = colors.get(pid, "steelblue")
                actor = self.plotter.add_mesh(
                    sub, color=color,
                    show_edges=True, edge_color="#606060", line_width=0.5,
                    label=f"{pid} {name}",
                )
                self._actors.append(actor)
                self._part_actors[pid] = actor
                if isinstance(color, (list, tuple)) and len(color) >= 3:
                    self._part_base_color[pid] = (
                        float(color[0]), float(color[1]), float(color[2])
                    )
                else:
                    try:
                        c = actor.GetProperty().GetColor()
                        self._part_base_color[pid] = (float(c[0]), float(c[1]), float(c[2]))
                    except Exception:
                        pass
        else:
            actor = self.plotter.add_mesh(
                pd, show_edges=True, color="steelblue",
                edge_color="#606060", line_width=0.5,
            )
            self._actors.append(actor)
        self.plotter.add_axes()
        self.plotter.set_background(self._current_bg)
        self.plotter.reset_camera()
        self._install_viewport_picker()

    def _install_viewport_picker(self):
        """Install a Qt-level event filter on the interactor widget so left
        clicks on the 3D viewport select the underlying part.

        Click-vs-drag is decided by comparing the press/release positions;
        if the cursor moved more than 3 px we treat it as a camera rotation
        and skip the pick (so PyVista's default interactor still rotates).
        """
        if self.plotter is None or not self._has_parts:
            return
        widget = getattr(self.plotter, "interactor", None)
        if widget is None:
            return
        prev = getattr(self, "_viewport_click_filter", None)
        if prev is not None:
            try:
                widget.removeEventFilter(prev)
            except Exception:
                pass
        flt = _ViewerClickFilter(self)
        widget.installEventFilter(flt)
        # Keep a reference on `self` so Qt doesn't garbage-collect it.
        self._viewport_click_filter = flt

    def _pick_pid_at(self, qt_x: int, qt_y: int) -> int | None:
        """Return the PID of the part under the given viewport coords, or None.

        Shared core of ``_do_viewport_pick`` and ``_do_viewport_deselect``.
        Translates Qt (top-left origin) to VTK display coords (bottom-left),
        runs a cell pick, and matches the hit actor against ``_part_actors``.
        """
        if self.plotter is None or not self._has_parts:
            return None
        widget = getattr(self.plotter, "interactor", None)
        if widget is None:
            return None
        h = widget.height()
        vtk_x = int(qt_x)
        vtk_y = int(h - qt_y)
        try:
            import vtk
            picker = vtk.vtkCellPicker()
            picker.SetTolerance(0.005)
            picker.Pick(vtk_x, vtk_y, 0, self.plotter.renderer)
            picked = picker.GetActor() or picker.GetProp3D()
        except Exception as exc:
            logger.debug("viewport pick failed: %s", exc)
            return None
        if picked is None:
            return None
        pid_hit = next(
            (pid for pid, a in self._part_actors.items() if a is picked),
            None,
        )
        if pid_hit is None:
            picked_addr = picked.__this__ if hasattr(picked, "__this__") else None
            for pid, a in self._part_actors.items():
                if a is None:
                    continue
                a_addr = a.__this__ if hasattr(a, "__this__") else None
                if a_addr is not None and a_addr == picked_addr:
                    pid_hit = pid
                    break
        return pid_hit

    def _do_viewport_pick(self, qt_x: int, qt_y: int, *, additive: bool) -> None:
        """Pick the actor under viewport coords and (additively) select it."""
        pid_hit = self._pick_pid_at(qt_x, qt_y)
        if pid_hit is None:
            return
        self._select_pid_in_tree(pid_hit, additive=additive)

    def _do_viewport_deselect(self, qt_x: int, qt_y: int) -> None:
        """Pick the actor under viewport coords and remove it from selection.

        Bound to Ctrl+Right-Click in ``_ViewerClickFilter``. If the picked
        part isn't currently selected the call is a no-op — the navigator
        selection state is the single source of truth.
        """
        pid_hit = self._pick_pid_at(qt_x, qt_y)
        if pid_hit is None:
            return
        self._deselect_pid_in_tree(pid_hit)

    def _find_tree_leaf_for_pid(self, item, pid: int):
        d = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(d, numbers.Integral) and int(d) == pid:
            return item
        for i in range(item.childCount()):
            r = self._find_tree_leaf_for_pid(item.child(i), pid)
            if r is not None:
                return r
        return None

    def _select_pid_in_tree(self, pid: int, additive: bool = False):
        root = self._nav_tree.topLevelItem(0)
        if root is None:
            return
        target = self._find_tree_leaf_for_pid(root, pid)
        if target is None:
            return
        if not additive:
            self._nav_tree.clearSelection()
        target.setSelected(True)
        self._nav_tree.setCurrentItem(target)
        self._nav_tree.scrollToItem(target)

    def _deselect_pid_in_tree(self, pid: int):
        """Remove a single PID from the navigator selection (others kept)."""
        root = self._nav_tree.topLevelItem(0)
        if root is None:
            return
        target = self._find_tree_leaf_for_pid(root, pid)
        if target is None or not target.isSelected():
            return
        target.setSelected(False)

    def _clear_tree_selection(self):
        if self._nav_tree.selectedItems():
            self._nav_tree.clearSelection()
            self._status.showMessage("Selection cleared", 2000)

    def _apply_style_to_actor(self, actor, style: str, mesh_on: bool | None = None):
        """Set the actor's representation given the render *style* and Mesh state.

        Element-detail visibility (per-cell edges / per-cell points) is driven
        by ``mesh_on``. Main "feature" edges are NOT drawn here — they live in
        a separate global overlay managed by ``_set_render_style``.

        Matrix (per actor):
          * Surface          + mesh-on  → Surface,   EdgeVisibility ON
          * Surface          + mesh-off → Surface,   EdgeVisibility OFF
          * Surface+Edges    + mesh-on  → Surface,   EdgeVisibility ON
          * Surface+Edges    + mesh-off → Surface,   EdgeVisibility OFF
          * Wireframe        + mesh-on  → Wireframe (actor visible)
          * Wireframe        + mesh-off → actor hidden (overlay supplies edges)
          * Points           + mesh-on  → Points     (actor visible)
          * Points           + mesh-off → actor hidden (overlay supplies points)

        Caller (``_apply_material_highlight``) is the single source of truth
        for ``actor.SetVisibility(...)`` — we only touch representation/edges
        here. Visibility is handled there based on the same matrix.
        """
        if actor is None:
            return
        if mesh_on is None:
            mesh_on = bool(getattr(self, "_mesh_visible", True))
        prop = actor.GetProperty()
        if style == "Wireframe":
            prop.SetRepresentationToWireframe()
            prop.EdgeVisibilityOff()
        elif style == "Points":
            prop.SetRepresentationToPoints()
            prop.SetPointSize(5)
            prop.EdgeVisibilityOff()
        else:
            # Surface or Surface+Edges: element edges follow the Mesh toggle.
            prop.SetRepresentationToSurface()
            if mesh_on:
                prop.EdgeVisibilityOn()
            else:
                prop.EdgeVisibilityOff()

    def _get_pydyna_polydata(self):
        """Return a clean PyDyna polydata for Wireframe edge extraction.

        Lazy-builds and caches it on first request. Returns ``None`` if
        PyDyna parsing fails (caller will fall back to the polydata that
        viewer_v2_loader already produced).
        """
        if self._pydyna_polydata is not None:
            print("[wire-diag] PyDyna polydata: cache HIT", flush=True)
            return self._pydyna_polydata
        if self._pydyna_polydata_failed:
            print("[wire-diag] PyDyna polydata: previous failure cached, skipping", flush=True)
            return None
        if self._file_path is None:
            print("[wire-diag] PyDyna polydata: no file path", flush=True)
            return None
        import time
        t0 = time.perf_counter()
        print(
            f"[wire-diag] PyDyna polydata: starting parse for "
            f"{self._file_path.name} ...",
            flush=True,
        )
        # Apply the same geometry-only pre-filter the legacy viewer uses.
        # PyDyna otherwise materialises *CONSTRAINED_* / *MAT_SPOTWELD /
        # *RIGID_BODY etc. as synthetic line cells, which leak into the
        # Wireframe overlay and produce per-element artefacts.
        from ui.model_viewer import _write_geometry_filtered_temp
        tmp_path = None
        try:
            tmp_path = _write_geometry_filtered_temp(self._file_path)
        except Exception as exc:
            print(
                f"[wire-diag] geometry pre-filter raised "
                f"{type(exc).__name__}: {exc} — falling back to raw file",
                flush=True,
            )
        parse_path = tmp_path if tmp_path is not None else self._file_path
        print(
            f"[wire-diag] parsing {'filtered temp' if tmp_path else 'raw file'}: "
            f"{parse_path}",
            flush=True,
        )
        try:
            import warnings
            from ansys.dyna.core import Deck
            from ansys.dyna.core.lib.deck_plotter import get_polydata

            deck = Deck()
            t_imp0 = time.perf_counter()
            deck.import_file(str(parse_path))
            t_imp = time.perf_counter() - t_imp0
            cwd = str(self._file_path.parent) or "."
            t_pd0 = time.perf_counter()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                pd = get_polydata(deck, cwd=cwd)
            t_pd = time.perf_counter() - t_pd0
            self._pydyna_polydata = pd
            try:
                n_pts = int(pd.n_points)
                n_cells = int(pd.n_cells)
            except Exception:
                n_pts = n_cells = -1
            print(
                f"[wire-diag] PyDyna polydata: SUCCESS  "
                f"import={t_imp:.2f}s  get_polydata={t_pd:.2f}s  "
                f"total={time.perf_counter() - t0:.2f}s  "
                f"points={n_pts}  cells={n_cells}",
                flush=True,
            )
            return pd
        except Exception as exc:
            print(
                f"[wire-diag] PyDyna polydata: FAILED after "
                f"{time.perf_counter() - t0:.2f}s — {type(exc).__name__}: {exc}",
                flush=True,
            )
            logger.warning("PyDyna polydata regeneration failed: %s", exc, exc_info=True)
            self._pydyna_polydata_failed = True
            return None
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

    def _rebuild_wire_overlay(self, as_points: bool = False) -> None:
        """Add a single combined feature-edge overlay showing true part outlines.

        Uses ``self._polydata`` as loaded.

        When *as_points* is True, the overlay is rendered as points (one per
        feature-edge vertex) — used by the Points render style. Otherwise it
        is rendered as a thin wireframe — used by Wireframe and Surface+Edges.

        Key decisions:
        - 1D cells (BEAM/LINE) are filtered out: they are not polygon geometry
          and their per-segment edges would appear as dense artefacts.
        - ``feature_angle=89``: shows only near-90° sharp corners (box edges,
          plate bends) while filtering out spring/coil inter-element dihedral
          angles (typically < 60°).
        - ``boundary_edges=True``: shows the open perimeter of shell surfaces
          (plate rims, cylinder opening edges).
        - ``manifold_edges=False``: hides all interior shared polygon edges.
        """
        self._remove_wire_overlay()
        if self.plotter is None or self._polydata is None:
            return
        try:
            import numpy as np
            import pyvista as pv

            pd = self._polydata

            # Restrict the overlay to parts whose checkbox is on. Unchecked
            # parts must vanish from the global edge overlay too, otherwise
            # their silhouette persists as a phantom wireframe.
            if self._has_parts and "part_ids" in pd.cell_data:
                visible_pids = {
                    pid for pid, vis in self._part_user_visible.items() if vis
                }
                # Phantom mode: when at least one part is selected, suppress
                # the feature edges of every non-selected part so the dimmed
                # silhouettes stop competing visually with the selection.
                # No-selection / Phantom off → behaves as before (all visible
                # parts contribute to the outline).
                if self._phantom_enabled:
                    sel = {int(p) for p in self._get_selected_pids()}
                    if sel:
                        visible_pids = visible_pids & sel
                if not visible_pids:
                    return
                part_ids_arr = pd.cell_data["part_ids"]
                mask = np.isin(part_ids_arr, list(visible_pids))
                if not mask.any():
                    return
                if not mask.all():
                    cell_ids = np.where(mask)[0]
                    pd = pd.extract_cells(cell_ids).extract_surface()

            # Filter out 1D cells (BEAM/LINE elements from PyDyna fallback).
            # PyVista PolyData stores line cells in .lines and polygon cells
            # in .faces — rebuilding from .faces alone drops all 1D cells.
            if isinstance(pd, pv.PolyData):
                raw_lines = pd.lines
                if len(raw_lines) > 0:
                    faces = pd.faces
                    if len(faces) == 0:
                        return  # only 1D elements — nothing to outline
                    pd = pv.PolyData(pd.points, faces)

            edges = pd.extract_feature_edges(
                boundary_edges=True,
                feature_edges=True,
                manifold_edges=False,
                non_manifold_edges=False,
                feature_angle=89,
            )
            if edges is not None and edges.n_points > 0:
                fe_color = "white" if self._theme_is_dark else "black"
                kw: dict = dict(
                    color=fe_color,
                    lighting=False,
                    pickable=False,
                    render=False,
                    name="_wire_overlay_global",
                )
                if as_points:
                    kw.update(style="points", point_size=5,
                              render_points_as_spheres=True)
                else:
                    kw.update(style="wireframe", line_width=1.5)
                self._wire_overlay_actor = self.plotter.add_mesh(edges, **kw)
        except Exception as exc:
            logger.debug("wire overlay failed: %s", exc)

    def _remove_wire_overlay(self) -> None:
        if self._wire_overlay_actor is not None and self.plotter is not None:
            try:
                self.plotter.remove_actor(self._wire_overlay_actor, render=False)
            except Exception:
                pass
            self._wire_overlay_actor = None


    def _nav_tree_placeholder(self):
        self._nav_tree.clear()
        item = QTreeWidgetItem(self._nav_tree, ["(No model loaded)"])
        item.setForeground(0, QColor(_CLR_TEXT_DIM))
        item.setFlags(Qt.ItemFlag.NoItemFlags)

    def _rebuild_nav_tree(self):
        tree = self._nav_tree
        tree.blockSignals(True)
        tree.clear()
        has_parts    = bool(self._part_actors)
        has_sets     = any(k != "__names__" for k in self._keyword_entities)
        has_materials = bool(self._materials)
        has_entities = has_sets or has_materials
        if not has_parts and not has_entities:
            self._nav_tree_placeholder()
            tree.blockSignals(False)
            return
        root_item = QTreeWidgetItem(tree, ["Assembly"])
        root_item.setFlags(root_item.flags()
                           | Qt.ItemFlag.ItemIsUserCheckable
                           | Qt.ItemFlag.ItemIsAutoTristate)
        root_item.setCheckState(0, Qt.CheckState.Checked)
        if has_parts:
            fem = QTreeWidgetItem(root_item, ["FEM Parts"])
            fem.setFlags(fem.flags()
                         | Qt.ItemFlag.ItemIsUserCheckable
                         | Qt.ItemFlag.ItemIsAutoTristate)
            fem.setCheckState(0, Qt.CheckState.Checked)
            for pid in sorted(self._part_actors):
                heading = (self._part_names.get(pid, "") or "").strip()
                label = f"PID {pid}: {heading}" if heading else f"PID {pid}"
                it = QTreeWidgetItem(fem, [label])
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(0, Qt.CheckState.Checked)
                it.setData(0, Qt.ItemDataRole.UserRole, pid)
        if has_entities:
            ent = QTreeWidgetItem(root_item, ["Keyword Entities"])
            ent.setFlags(ent.flags()
                         | Qt.ItemFlag.ItemIsUserCheckable
                         | Qt.ItemFlag.ItemIsAutoTristate)
            ent.setCheckState(0, Qt.CheckState.Unchecked)
            if has_sets:
                _CAT_ORDER  = ["SET_NODE", "SET_SHELL", "SET_SOLID", "SET_PART", "SET_SEGMENT"]
                _CAT_LABELS = {"SET_NODE": "Set Node", "SET_SHELL": "Set Shell",
                               "SET_SOLID": "Set Solid", "SET_PART": "Set Part",
                               "SET_SEGMENT": "Set Segment"}
                _MBR_LABELS = {"SET_NODE": "nodes", "SET_SHELL": "shells",
                               "SET_SOLID": "solids", "SET_PART": "parts",
                               "SET_SEGMENT": "segments"}
                entity_names = self._keyword_entities.get("__names__", {})
                for cat in _CAT_ORDER:
                    sids = self._keyword_entities.get(cat)
                    if not sids:
                        continue
                    cat_it = QTreeWidgetItem(ent, [_CAT_LABELS.get(cat, cat)])
                    cat_it.setFlags(cat_it.flags()
                                    | Qt.ItemFlag.ItemIsUserCheckable
                                    | Qt.ItemFlag.ItemIsAutoTristate)
                    cat_it.setCheckState(0, Qt.CheckState.Unchecked)
                    mlbl = _MBR_LABELS.get(cat, "members")
                    for sid in sorted(sids):
                        name = entity_names.get((cat, sid), "")
                        n    = len(sids[sid])
                        txt  = (f"SID {sid}: {name}  ({n} {mlbl})" if name
                                else f"SID {sid}  ({n} {mlbl})")
                        it = QTreeWidgetItem(cat_it, [txt])
                        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        it.setCheckState(0, Qt.CheckState.Unchecked)
                        it.setData(0, Qt.ItemDataRole.UserRole, (cat, sid))
            if has_materials:
                mat_it = QTreeWidgetItem(ent, ["Material"])
                mat_it.setFlags(mat_it.flags()
                                | Qt.ItemFlag.ItemIsUserCheckable
                                | Qt.ItemFlag.ItemIsAutoTristate)
                mat_it.setCheckState(0, Qt.CheckState.Unchecked)
                for mid in sorted(self._materials):
                    title = (self._materials[mid].get("title") or "").strip()
                    txt   = f"MID {mid}: {title}" if title else f"MID {mid}"
                    it = QTreeWidgetItem(mat_it, [txt])
                    it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    it.setCheckState(0, Qt.CheckState.Unchecked)
                    it.setData(0, Qt.ItemDataRole.UserRole, ("MATERIAL", int(mid)))
        tree.expandAll()
        tree.resizeColumnToContents(0)
        tree.blockSignals(False)
        self._autosize_left_panel()

    def _autosize_left_panel(self) -> None:
        """Resize the left panel so the widest visible navigator row fits.

        Measured from the tree's natural column-0 width plus indentation,
        frame and vertical-scrollbar reserve. Clamped to a sensible range
        so a single very long part name can't blow the layout.
        """
        spl = getattr(self, "_main_splitter", None)
        tree = getattr(self, "_nav_tree", None)
        if spl is None or tree is None:
            return
        if not getattr(self, "_left_panel_open", True):
            return
        try:
            col_w = tree.sizeHintForColumn(0)
        except Exception:
            return
        sb_w = 0
        try:
            sb = tree.verticalScrollBar()
            if sb is not None and sb.isVisible():
                sb_w = sb.sizeHint().width()
        except Exception:
            pass
        extras = tree.indentation() + 2 * tree.frameWidth() + sb_w + 16
        needed = col_w + extras
        win_w = self.width() or 1200
        target = max(_SPLIT_LEFT, min(needed, int(win_w * 0.45)))
        sizes = spl.sizes()
        if len(sizes) < 3:
            return
        total = sum(sizes)
        new_left = target
        new_right = sizes[2]
        new_center = max(200, total - new_left - new_right)
        spl.setSizes([new_left, new_center, new_right])
        self._left_panel_size = new_left

    def _resize_props_panel_to_fit(self) -> None:
        """Grow the right Properties panel so its longest line fits without wrap.

        Uses ``QTextDocument.idealWidth`` to measure the rich-text content at
        its natural (unwrapped) width, then resizes the splitter section.
        Capped at 50% of the dialog width so the viewport stays usable; never
        shrinks below the default _SPLIT_RIGHT.
        """
        spl = getattr(self, "_main_splitter", None)
        panel = getattr(self, "_right_panel", None)
        label = getattr(self, "_props_label", None)
        if spl is None or panel is None or label is None:
            return
        if not panel.isVisible():
            return
        try:
            from PySide6.QtGui import QTextDocument
            doc = QTextDocument()
            doc.setDefaultFont(label.font())
            if Qt.mightBeRichText(label.text()):
                doc.setHtml(label.text())
            else:
                doc.setPlainText(label.text())
            doc.setTextWidth(-1)
            content_w = int(doc.idealWidth())
        except Exception:
            content_w = label.sizeHint().width()
        # Add label padding (QSS sets 8 px) + frame border + a small margin.
        extras = 2 * panel.frameWidth() + 16 + 12
        needed = content_w + extras
        win_w = self.width() or 1200
        target = max(_SPLIT_RIGHT, min(needed, int(win_w * 0.5)))
        sizes = spl.sizes()
        if len(sizes) < 3 or sizes[2] >= target:
            return  # already wide enough — don't shrink the user's manual resize
        total = sum(sizes)
        new_right = target
        new_left = sizes[0]
        new_center = max(200, total - new_left - new_right)
        spl.setSizes([new_left, new_center, new_right])

    def _on_nav_item_checked(self, item, _col):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data, tuple) and len(data) == 2:
            cat, val = data
            if cat == "MATERIAL":
                checked = item.checkState(0) == Qt.CheckState.Checked
                if checked:
                    self._material_checked_mids.add(int(val))
                else:
                    self._material_checked_mids.discard(int(val))
                self._apply_material_highlight()
            else:
                self._toggle_entity_visibility(cat, val, item.checkState(0) == Qt.CheckState.Checked)
        elif isinstance(data, numbers.Integral):
            pid = int(data)
            self._part_user_visible[pid] = (
                item.checkState(0) == Qt.CheckState.Checked
            )
            self._refresh_global_wire_overlay()
            self._apply_material_highlight()
        else:
            self._sync_parts_from_tree()
            self._sync_entities_from_tree()
            self._refresh_global_wire_overlay()
            self._apply_material_highlight()
        if self.plotter:
            self.plotter.render()

    def _refresh_global_wire_overlay(self) -> None:
        """Rebuild the global feature-edge overlay if it's currently active.

        The overlay is built once from the full polydata, so toggling part
        visibility leaves stale edges behind. Rebuild it from only the
        checked parts whenever an FEM-Parts checkbox changes.
        """
        style = getattr(self, "_current_style", "Surface")
        if style == "Surface":
            return
        try:
            self._rebuild_wire_overlay(as_points=(style == "Points"))
        except Exception:
            logger.debug("wire overlay refresh failed", exc_info=True)

    def _sync_parts_from_tree(self):
        root = self._nav_tree.topLevelItem(0)
        if root is None:
            return
        for ci in range(root.childCount()):
            child = root.child(ci)
            if child.text(0) == "FEM Parts":
                for i in range(child.childCount()):
                    it  = child.child(i)
                    pid = it.data(0, Qt.ItemDataRole.UserRole)
                    if isinstance(pid, numbers.Integral):
                        self._part_user_visible[int(pid)] = (
                            it.checkState(0) == Qt.CheckState.Checked
                        )

    def _sync_entities_from_tree(self):
        root = self._nav_tree.topLevelItem(0)
        if root is None:
            return
        for ci in range(root.childCount()):
            child = root.child(ci)
            if child.text(0) == "Keyword Entities":
                self._sync_entity_group(child)
                break

    def _sync_entity_group(self, group):
        for ci in range(group.childCount()):
            ch   = group.child(ci)
            data = ch.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(data, tuple) and len(data) == 2:
                cat, sid = data
                self._toggle_entity_visibility(cat, sid, ch.checkState(0) == Qt.CheckState.Checked)
            else:
                self._sync_entity_group(ch)

    def _on_nav_selection_changed(self):
        # Material preview: any selected MATERIAL items contribute their parts
        # to the live highlight set (transient — vanishes on deselection).
        sel_mids: set = set()
        for it in self._nav_tree.selectedItems():
            d = it.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(d, tuple) and len(d) == 2 and d[0] == "MATERIAL":
                sel_mids.add(int(d[1]))
        if sel_mids != self._material_selected_mids:
            self._material_selected_mids = sel_mids
            self._apply_material_highlight()
        selected_pids = self._get_selected_pids()
        self._apply_selection_dim(selected_pids)
        for pid in list(self._highlighted_pids - set(selected_pids)):
            actor = self._highlight_fe_actors.pop(pid, None)
            if actor is not None:
                try:
                    self.plotter.remove_actor(actor)
                except Exception:
                    pass
            self._highlighted_pids.discard(pid)
        for pid in set(selected_pids) - self._highlighted_pids:
            sub = self._part_polydata.get(pid)
            if sub is None:
                continue
            try:
                edges = sub.extract_feature_edges(
                    boundary_edges=True, feature_edges=True,
                    manifold_edges=False, non_manifold_edges=True, feature_angle=30,
                )
                if edges.n_points > 0:
                    actor = self.plotter.add_mesh(
                        edges, color="orange", style="wireframe",
                        line_width=4.0, label=f"_hl_{pid}",
                    )
                    self._highlight_fe_actors[pid] = actor
                    self._highlighted_pids.add(pid)
            except Exception as exc:
                logger.debug("Highlight pid %s failed: %s", pid, exc)
        self._update_properties_panel(selected_pids)
        if self.plotter:
            self.plotter.render()

    def _restore_pre_dim_opacity(self) -> None:
        """Restore every part to its snapshot opacity and clear the snapshot."""
        if not self._pre_dim_opacity:
            return
        for pid, opa in self._pre_dim_opacity.items():
            actor = self._part_actors.get(pid)
            if actor is None:
                continue
            try:
                actor.GetProperty().SetOpacity(float(opa))
            except Exception:
                pass
        self._pre_dim_opacity.clear()

    def _apply_selection_dim(self, selected_pids) -> None:
        """Dim non-selected parts to highlight the navigator selection.

        Driven by Config → "Phantom mode". When ON and at least one part is
        selected, every other part's opacity drops to ``_phantom_opacity``
        while the selected parts keep their pre-dim opacity. When the
        selection is cleared (or Phantom is turned off) every part returns
        to its snapshot value.

        Also refreshes the global feature-edge overlay so dimmed parts'
        silhouettes don't keep showing while their surface fades — see the
        Phantom branch in ``_rebuild_wire_overlay``.
        """
        if not self._part_actors:
            return
        if not self._phantom_enabled:
            self._restore_pre_dim_opacity()
            self._refresh_global_wire_overlay()
            return
        sel = {int(p) for p in (selected_pids or [])}
        if sel:
            # Lazy snapshot: capture each part's current opacity the first time
            # we enter the dimmed state. Subsequent selection changes reuse it.
            if not self._pre_dim_opacity:
                for pid, actor in self._part_actors.items():
                    if actor is None:
                        continue
                    try:
                        self._pre_dim_opacity[pid] = float(
                            actor.GetProperty().GetOpacity()
                        )
                    except Exception:
                        self._pre_dim_opacity[pid] = 1.0
            dim_opa = float(self._phantom_opacity)
            for pid, actor in self._part_actors.items():
                if actor is None:
                    continue
                try:
                    base = self._pre_dim_opacity.get(pid, 1.0)
                    target = base if pid in sel else dim_opa
                    actor.GetProperty().SetOpacity(target)
                except Exception:
                    pass
        else:
            self._restore_pre_dim_opacity()
        self._refresh_global_wire_overlay()

    def _on_phantom_toggled(self, checked: bool) -> None:
        self._phantom_enabled = bool(checked)
        if hasattr(self, "_cfg_phantom_opacity"):
            self._cfg_phantom_opacity.setEnabled(self._phantom_enabled)
        # Re-apply with the current selection so the change takes effect immediately.
        self._apply_selection_dim(self._get_selected_pids())
        if self.plotter:
            self.plotter.render()

    def _on_phantom_opacity_changed(self, value: int) -> None:
        self._phantom_opacity = max(0.0, min(1.0, value / 100.0))
        if self._phantom_enabled:
            self._apply_selection_dim(self._get_selected_pids())
            if self.plotter:
                self.plotter.render()

    def _on_icon_text_toggled(self, checked: bool) -> None:
        self._icon_text_enabled = bool(checked)
        style = (
            Qt.ToolButtonStyle.ToolButtonTextUnderIcon
            if self._icon_text_enabled
            else Qt.ToolButtonStyle.ToolButtonIconOnly
        )
        # QToolBar.setToolButtonStyle only affects QToolButtons it created
        # internally via addAction; custom QToolButtons added via addWidget
        # (the combo-style buttons) need the style set directly.
        for tb in self._ribbon_toolbars:
            try:
                tb.setToolButtonStyle(style)
            except Exception:
                pass
        for btn in self._ribbon_combo_buttons:
            try:
                btn.setToolButtonStyle(style)
            except Exception:
                pass

    # ── Custom color pickers (Config tab) ───────────────────────────────

    def _make_color_swatch_btn(self, rgb: tuple[float, float, float]) -> "QPushButton":
        """Build a small colored button used as a click-to-pick swatch."""
        from PySide6.QtWidgets import QPushButton
        btn = QPushButton()
        btn.setFixedSize(40, 22)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._set_swatch_color(btn, rgb)
        return btn

    @staticmethod
    def _set_swatch_color(btn: "QPushButton", rgb: tuple[float, float, float]) -> None:
        r = max(0, min(255, int(round(rgb[0] * 255))))
        g = max(0, min(255, int(round(rgb[1] * 255))))
        b = max(0, min(255, int(round(rgb[2] * 255))))
        hex_color = f"#{r:02x}{g:02x}{b:02x}"
        btn.setStyleSheet(
            f"QPushButton {{ background:{hex_color}; border:1px solid #808080; border-radius:3px; }}"
            f"QPushButton:hover {{ border:1px solid #1f6feb; }}"
        )
        btn.setProperty("rgb", (rgb[0], rgb[1], rgb[2]))

    def _bg_swatch_initial_rgb(self) -> tuple[float, float, float]:
        """Initial color shown on the BG swatch — best-effort guess from current state."""
        cur = getattr(self, "_current_bg", None)
        if isinstance(cur, str) and cur.startswith("#") and len(cur) == 7:
            try:
                return (int(cur[1:3], 16) / 255.0,
                        int(cur[3:5], 16) / 255.0,
                        int(cur[5:7], 16) / 255.0)
            except ValueError:
                pass
        named = {
            "white": (1.0, 1.0, 1.0),
            "black": (0.0, 0.0, 0.0),
            "lightgray": (0.83, 0.83, 0.83),
        }
        return named.get(cur, (0.95, 0.95, 0.95))

    def _on_pick_bg_color(self) -> None:
        from PySide6.QtWidgets import QColorDialog
        from PySide6.QtGui import QColor
        rgb = self._cfg_bg_swatch.property("rgb") or (1.0, 1.0, 1.0)
        initial = QColor.fromRgbF(*rgb)
        chosen = QColorDialog.getColor(initial, self, "Background Color")
        if not chosen.isValid():
            return
        new_rgb = (chosen.redF(), chosen.greenF(), chosen.blueF())
        self._set_swatch_color(self._cfg_bg_swatch, new_rgb)
        hex_color = chosen.name()  # "#rrggbb"
        self._custom_bg_color = hex_color
        self._set_background(hex_color)

    def _on_pick_edge_color(self) -> None:
        from PySide6.QtWidgets import QColorDialog
        from PySide6.QtGui import QColor
        cur = self._custom_edge_color or self._cfg_edge_swatch.property("rgb") or (0.0, 0.0, 0.0)
        initial = QColor.fromRgbF(*cur)
        chosen = QColorDialog.getColor(initial, self, "Edge Color")
        if not chosen.isValid():
            return
        new_rgb = (chosen.redF(), chosen.greenF(), chosen.blueF())
        self._set_swatch_color(self._cfg_edge_swatch, new_rgb)
        self._custom_edge_color = new_rgb
        self._apply_edge_color(new_rgb)

    def _on_reset_edge_color(self) -> None:
        """Restore the automatic edge color (white on dark / black on light)."""
        self._custom_edge_color = None
        bg = getattr(self, "_current_bg", "white")
        auto = (1.0, 1.0, 1.0) if bg == "black" else (0.0, 0.0, 0.0)
        self._set_swatch_color(self._cfg_edge_swatch, auto)
        self._apply_edge_color(auto)

    def _apply_edge_color(self, rgb: tuple[float, float, float]) -> None:
        """Apply *rgb* to the global wire overlay and per-part style edges."""
        actor = self._wire_overlay_actor
        if actor is not None:
            try:
                actor.GetProperty().SetColor(*rgb)
            except Exception:
                pass
        for fe in self._style_fe_actors.values():
            if fe is None:
                continue
            try:
                fe.GetProperty().SetColor(*rgb)
            except Exception:
                pass
        if self.plotter is not None:
            try:
                self.plotter.render()
            except Exception:
                pass

    def _update_properties_panel(self, selected_pids: list[int]):
        if not selected_pids:
            self._props_label.setText(
                "Select a part or element\nin the navigator or viewport\nto view properties."
            )
            return
        lines = []
        for pid in selected_pids:
            raw_name = (self._part_names.get(pid, "") or "").strip()
            heading_row = f"Heading: {raw_name}<br>" if raw_name else ""
            sub  = self._part_polydata.get(pid)
            mat_block = self._material_html_for_pid(pid)
            if sub is not None:
                lines.append(
                    f"<b>Part {pid}</b><br>"
                    f"{heading_row}"
                    f"Nodes: {sub.n_points:,}<br>"
                    f"Elements: {sub.n_cells:,}"
                    f"{mat_block}<hr>"
                )
            else:
                lines.append(
                    f"<b>Part {pid}</b><br>{heading_row}{mat_block}<hr>"
                )
        self._props_label.setTextFormat(Qt.TextFormat.RichText)
        self._props_label.setText("".join(lines))
        self._resize_props_panel_to_fit()

    def _material_html_for_pid(self, pid: int) -> str:
        mid = self._part_to_mid.get(int(pid))
        if not mid:
            return ""
        info = self._materials.get(mid, {}) or {}
        title = (info.get("title") or "").strip()
        keyword = (info.get("keyword") or "").strip()
        kw_html = f"*{keyword}" if keyword else "(undefined)"
        title_html = f": {title}" if title else ""
        return (
            f"<br>Material: {kw_html}"
            f"<br>MID {mid}{title_html}"
        )

    def _get_selected_pids(self) -> list[int]:
        """Return the leaf pids covered by the current selection.

        If the user selects a parent node (Assembly, FEM Parts, …) all
        descendant FEM-Part leaves are included. Entity items (tuple data)
        are ignored.
        """
        pids: list[int] = []
        seen: set[int] = set()

        def collect(it):
            d = it.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(d, numbers.Integral):
                pid = int(d)
                if pid not in seen:
                    seen.add(pid)
                    pids.append(pid)
                return
            for i in range(it.childCount()):
                collect(it.child(i))

        for item in self._nav_tree.selectedItems():
            collect(item)
        return pids

    def _remove_highlight_edges(self):
        for actor in list(self._highlight_fe_actors.values()):
            try:
                if self.plotter:
                    self.plotter.remove_actor(actor)
            except Exception:
                pass
        self._highlight_fe_actors.clear()
        self._highlighted_pids.clear()

    # =========================================================================
    #  Entity actors
    # =========================================================================

    def _entity_actor_key(self, cat: str, sid: int) -> str:
        return f"{cat}_{sid}"

    def _toggle_entity_visibility(self, cat: str, sid: int, visible: bool):
        key = self._entity_actor_key(cat, sid)
        if visible:
            if key not in self._entity_actors:
                self._add_entity_actor(cat, sid)
        else:
            actor = self._entity_actors.pop(key, None)
            if actor is not None:
                try:
                    self.plotter.remove_actor(actor)
                except Exception:
                    pass

    def _add_entity_actor(self, cat: str, sid: int):
        import numpy as np
        members = self._keyword_entities.get(cat, {}).get(sid)
        if not members or self._polydata is None or self.plotter is None:
            return
        pd  = self._polydata
        key = self._entity_actor_key(cat, sid)
        try:
            if cat == "SET_NODE":
                nids = pd.point_data.get("node_ids")
                if nids is None:
                    return
                mask = np.isin(nids, list(set(members)))
                pts  = pd.points[mask]
                if len(pts) == 0:
                    return
                import pyvista as pv
                actor = self.plotter.add_mesh(
                    pv.PolyData(pts), color="red", point_size=8,
                    render_points_as_spheres=True, label=f"_entity_{key}")
                self._entity_actors[key] = actor
            elif cat in ("SET_SHELL", "SET_SOLID"):
                elem_ids = pd.cell_data.get("element_ids")
                if elem_ids is None:
                    return
                idx = np.where(np.isin(elem_ids, list(set(members))))[0]
                if len(idx) == 0:
                    return
                actor = self.plotter.add_mesh(
                    pd.extract_cells(idx), color="red", opacity=0.6,
                    show_edges=True, edge_color="darkred", line_width=1.5,
                    label=f"_entity_{key}")
                self._entity_actors[key] = actor
            elif cat == "SET_PART":
                pids_arr = pd.cell_data.get("part_ids")
                if pids_arr is None:
                    return
                idx = np.where(np.isin(pids_arr, list(set(members))))[0]
                if len(idx) == 0:
                    return
                actor = self.plotter.add_mesh(
                    pd.extract_cells(idx), color="red", opacity=0.6,
                    show_edges=True, edge_color="darkred", line_width=1.5,
                    label=f"_entity_{key}")
                self._entity_actors[key] = actor
            elif cat == "SET_SEGMENT":
                nids = pd.point_data.get("node_ids")
                if nids is None:
                    return
                id_to_idx = {int(n): i for i, n in enumerate(nids)}
                import pyvista as pv
                faces: list = []
                for seg in members:
                    indices = [id_to_idx.get(n) for n in seg if id_to_idx.get(n) is not None]
                    if len(indices) >= 3:
                        faces.append(len(indices))
                        faces.extend(indices)
                if not faces:
                    return
                actor = self.plotter.add_mesh(
                    pv.PolyData(pd.points, faces=faces), color="red", opacity=0.6,
                    show_edges=True, edge_color="darkred", line_width=2.0,
                    label=f"_entity_{key}")
                self._entity_actors[key] = actor
        except Exception as exc:
            logger.debug("Entity actor %s failed: %s", key, exc)

    def _remove_all_entity_actors(self):
        for actor in list(self._entity_actors.values()):
            try:
                if self.plotter:
                    self.plotter.remove_actor(actor)
            except Exception:
                pass
        self._entity_actors.clear()

    # =========================================================================
    #  Materials — parsing + highlighting
    # =========================================================================

    def _consume_materials_payload(self, payload) -> None:
        """Consume the loader's ``__materials__`` payload.

        Shape: ``{"materials": {mid: {"title": str}}, "part_to_mid": {pid: mid}}``.
        Only materials referenced by at least one *PART are kept; each material
        gets its set of pids attached for highlighting.
        """
        self._materials = {}
        self._part_to_mid = {}
        if not isinstance(payload, dict):
            return
        raw_mats: dict = payload.get("materials") or {}
        part_to_mid: dict = {int(k): int(v) for k, v in (payload.get("part_to_mid") or {}).items()}
        used: dict = {}
        for pid, mid in part_to_mid.items():
            slot = used.setdefault(mid, {"title": "", "parts": set()})
            slot["parts"].add(pid)
        for mid, slot in used.items():
            info = raw_mats.get(mid) or raw_mats.get(str(mid)) or {}
            # Fallback for decks where materials are *INCLUDE'd at global scope
            # (idmoff=0) while parts reference them via *INCLUDE_TRANSFORM with
            # idmoff offsets (typically multiples of 1 000 000).  In that case
            # raw_mats has keys 1/2/3 but part_to_mid values are 1000001 etc.
            # Stripping the offset tier (mid % 1_000_000) recovers the base MID.
            if not info and mid >= 1_000_000:
                base_mid = mid % 1_000_000
                if base_mid > 0:
                    info = raw_mats.get(base_mid) or raw_mats.get(str(base_mid)) or {}
            if isinstance(info, dict):
                slot["title"]   = info.get("title", "") or ""
                slot["keyword"] = info.get("keyword", "") or ""
            else:
                slot["title"] = ""
                slot["keyword"] = ""
        self._materials = used
        self._part_to_mid = part_to_mid

    def _effective_highlighted_pids(self) -> set:
        """Union of pids covered by selected (preview) + checked (permanent) materials."""
        pids: set = set()
        for mid in self._material_checked_mids:
            pids.update(self._materials.get(mid, {}).get("parts", set()))
        for mid in self._material_selected_mids:
            pids.update(self._materials.get(mid, {}).get("parts", set()))
        return pids

    def _apply_material_highlight(self) -> None:
        """Apply material highlighting to part actors:

        - Parts whose material is selected/checked get an intensified version
          of their own base color, an orange feature-edge overlay, and are
          forced visible.
        - Other parts use their base color, no overlay, and respect the
          user's FEM Parts checkbox state (self._part_user_visible).
        """
        if not self._part_actors:
            return
        eff = self._effective_highlighted_pids()
        for pid, actor in self._part_actors.items():
            if actor is None:
                continue
            base = self._part_base_color.get(pid)
            try:
                prop = actor.GetProperty()
                if base is not None:
                    color = _intensify_rgb(base) if pid in eff else base
                    prop.SetColor(float(color[0]), float(color[1]), float(color[2]))
                # Check-off is absolute: nothing of this part renders.
                user_vis = bool(self._part_user_visible.get(pid, True))
                eff_style = self._per_part_style.get(pid, self._current_style)
                mesh_on = bool(self._per_part_mesh.get(pid, self._mesh_visible))
                # Wireframe / Points with mesh OFF: the actor itself carries
                # no useful detail (it'd just show the same outline the
                # feature-edge overlay already provides), so we hide it. The
                # overlay (global or per-part) then supplies the silhouette.
                hide_actor_for_overlay = (
                    eff_style in ("Wireframe", "Points") and not mesh_on
                )
                actor.SetVisibility(user_vis and not hide_actor_for_overlay)
                # Per-part style overlay (selection mode) tracks the part's
                # check-on state.
                fe_style = self._style_fe_actors.get(pid)
                if fe_style is not None:
                    fe_style.SetVisibility(user_vis)
                fe_mat = self._material_fe_actors.get(pid)
                if fe_mat is not None:
                    fe_mat.SetVisibility(user_vis)
            except Exception as exc:
                logger.debug("apply highlight pid %s failed: %s", pid, exc)

        # Manage orange feature-edge overlays: drop those no longer needed,
        # create new ones for newly-highlighted pids.
        if self.plotter is not None:
            for pid in list(self._material_fe_actors.keys()):
                if pid not in eff:
                    actor = self._material_fe_actors.pop(pid, None)
                    if actor is not None:
                        try:
                            self.plotter.remove_actor(actor, render=False)
                        except Exception:
                            pass
            for pid in eff:
                if pid in self._material_fe_actors:
                    continue
                sub = self._part_polydata.get(pid)
                if sub is None:
                    continue
                try:
                    edges = self._clean_feature_edges(sub, feature_angle=60)
                    if edges is None or edges.n_points == 0:
                        continue
                    fe_actor = self.plotter.add_mesh(
                        edges,
                        color=(1.0, 0.45, 0.0),  # orange
                        style="wireframe",
                        line_width=4.0,
                        lighting=False,
                        pickable=False,
                        render=False,
                        name=f"_matfe_{pid}",
                    )
                    self._material_fe_actors[pid] = fe_actor
                except Exception as exc:
                    logger.debug("feature-edge overlay pid %s failed: %s", pid, exc)

        if self.plotter:
            try:
                self.plotter.render()
            except Exception:
                pass

    # =========================================================================
    #  Toolbar action implementations
    # =========================================================================

    def _action_import(self):
        start = str(self._file_path.parent) if self._file_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open .k Model", start,
            "LS-DYNA Files (*.k *.key *.dyn);;All Files (*)",
        )
        if path:
            self._file_path = Path(path)
            self.setWindowTitle(f"3D Viewer 2.0  —  {self._file_path.name}")
            self._populate_files_list()
            self._start_load(self._file_path)

    def _action_screenshot(self):
        if self.plotter is None:
            self._status.showMessage("No viewport open yet")
            return
        default = (self._file_path.stem + "_3d.png") if self._file_path else "screenshot_3d.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Screenshot", default,
            "PNG Images (*.png);;JPEG Images (*.jpg);;All Files (*)",
        )
        if path:
            self.plotter.screenshot(path)
            self._status.showMessage(f"Screenshot saved: {path}", 5000)

    def _set_render_style(self, style: str):
        """Apply a render style to a scoped subset of parts.

        Scope rules (Style is independent from Mesh / visibility):
          * If one or more parts are selected in the navigator → apply only
            to those parts.
          * Otherwise → apply to every part that is currently *check-on*
            (``_part_user_visible[pid] is True``). Check-off parts stay
            hidden and untouched.
          * If no part is visible at all → no-op (status hint only).

        This action MUST NOT toggle the Mesh button or change
        ``_mesh_visible``/``_per_part_mesh``. It DOES manage the global
        feature-edge overlay because that overlay is conceptually part of
        the style (it represents "main edges" of the parts):
          * Surface        → overlay hidden
          * Surface+Edges  → overlay as wireframe
          * Wireframe      → overlay as wireframe
          * Points         → overlay as points

        Element-detail (per-cell edges/points) follows the Mesh toggle and
        is applied by ``_apply_style_to_actor``.
        """
        if self.plotter is None:
            return
        # Keep the Style dropdown's checked indicator in sync regardless of
        # selection — style buttons are mutually exclusive (QActionGroup).
        act = self._style_actions.get(style)
        if act is not None and not act.isChecked():
            act.setChecked(True)

        selected_pids = self._get_selected_pids()
        if selected_pids:
            target_pids = [pid for pid in selected_pids
                           if self._part_actors.get(pid) is not None]
            scope_msg = (
                f"Render style: {style}  (parts: "
                f"{', '.join(str(p) for p in target_pids)})"
                if target_pids else
                f"Render style: {style} — selection has no renderable parts"
            )
        else:
            self._current_style = style
            target_pids = [pid for pid, actor in self._part_actors.items()
                           if actor is not None
                           and self._part_user_visible.get(pid, True)]
            scope_msg = (
                f"Render style: {style}"
                if target_pids else
                f"Render style: {style} — no visible parts, nothing to apply"
            )

        is_global = not selected_pids

        # ── Global feature-edge overlay (the "main edges" of the parts) ──
        # Surface           → no overlay
        # Surface+Edges     → overlay (wireframe)
        # Wireframe         → overlay (wireframe)
        # Points            → overlay (points)
        if is_global:
            # Drop any per-part style overlays — global mode uses the single
            # combined overlay instead.
            for fe in list(self._style_fe_actors.values()):
                try:
                    self.plotter.remove_actor(fe, render=False)
                except Exception:
                    pass
            self._style_fe_actors.clear()
            if style == "Surface":
                self._remove_wire_overlay()
            else:
                self._rebuild_wire_overlay(as_points=(style == "Points"))

        mesh_on = bool(getattr(self, "_mesh_visible", True))
        for pid in target_pids:
            actor = self._part_actors.get(pid)
            self._per_part_style[pid] = style
            self._apply_style_to_actor(actor, style, mesh_on=mesh_on)
            if not is_global:
                self._update_style_fe_overlay(pid, style)

        # Visibility (actor vs feature-edge overlay) is reconciled centrally.
        self._apply_material_highlight()

        self._status.showMessage(scope_msg, 3000)
        self.plotter.render()

    def _clean_feature_edges(self, mesh, feature_angle: float = 60):
        """Extract clean silhouette edges from *mesh*.

        Two-stage approach:
          1. vtkCleanPolyData merges coincident points and removes
             degenerate cells that occasionally slip through and would
             break manifold detection downstream.
          2. vtkFeatureEdges extracts only **boundary edges** (open
             shell perimeters) and **sharp dihedral feature edges**
             above ``feature_angle``. Non-manifold and manifold edges
             are dropped — the former never appear in clean meshes,
             the latter would dump every element edge once topology
             is broken. With feature_angle=60 facet seams along
             cylindrical surfaces (typical dihedral ≤ 30°) are filtered
             out, leaving only true silhouettes (cube corners ≈ 90°,
             cylinder cap rims ≈ 90°).

        Note: we deliberately skip vtkTriangleFilter here. Triangulating
        non-coplanar quads (the lateral faces of a hex-meshed cylinder
        are slightly curved) introduces a diagonal edge whose dihedral
        with the quad's other half can exceed feature_angle, producing
        spurious "artefact" lines.
        """
        import vtk as _vtk
        import pyvista as pv
        try:
            if hasattr(mesh, "extract_surface") and not isinstance(mesh, pv.PolyData):
                mesh_pd = mesh.extract_surface()
            else:
                mesh_pd = mesh

            cleaner = _vtk.vtkCleanPolyData()
            cleaner.SetInputData(mesh_pd)
            cleaner.SetTolerance(0.0)
            cleaner.PointMergingOn()
            cleaner.ConvertLinesToPointsOff()
            cleaner.ConvertPolysToLinesOff()
            cleaner.ConvertStripsToPolysOff()
            cleaner.Update()

            fe = _vtk.vtkFeatureEdges()
            fe.SetInputConnection(cleaner.GetOutputPort())
            fe.BoundaryEdgesOn()
            fe.FeatureEdgesOn()
            fe.NonManifoldEdgesOff()
            fe.ManifoldEdgesOff()
            fe.SetFeatureAngle(feature_angle)
            fe.ColoringOff()
            fe.Update()
            return pv.wrap(fe.GetOutput())
        except Exception as exc:
            logger.debug("clean feature edges failed: %s", exc)
            return None

    def _update_style_fe_overlay(self, pid: int, style: str) -> None:
        """Manage the per-part feature-edge overlay (used in selection mode).

        Mirrors the global overlay matrix:
          * Surface          → remove overlay
          * Surface+Edges    → wireframe overlay
          * Wireframe        → wireframe overlay
          * Points           → points overlay

        For the global (no-selection) case, ``_rebuild_wire_overlay`` uses the
        combined polydata which correctly preserves inter-cell connectivity.
        """
        if self.plotter is None:
            return
        existing = self._style_fe_actors.get(pid)
        if style == "Surface":
            if existing is not None:
                try:
                    self.plotter.remove_actor(existing, render=False)
                except Exception:
                    pass
                self._style_fe_actors.pop(pid, None)
            return

        # Rebuild whenever style switches between wireframe and points; the
        # cheapest correct approach is to drop and re-create.
        if existing is not None:
            try:
                self.plotter.remove_actor(existing, render=False)
            except Exception:
                pass
            self._style_fe_actors.pop(pid, None)

        sub = self._part_polydata.get(pid)
        if sub is None:
            return
        try:
            edges = self._clean_feature_edges(sub, feature_angle=60)
            if edges is None or edges.n_points == 0:
                return
            fe_color = "white" if self._current_bg == "black" else "black"
            kw: dict = dict(
                color=fe_color,
                lighting=False,
                pickable=False,
                render=False,
                name=f"_stylefe_{pid}",
            )
            if style == "Points":
                kw.update(style="points", point_size=5,
                          render_points_as_spheres=True)
            else:
                kw.update(style="wireframe", line_width=1.5)
            fe = self.plotter.add_mesh(edges, **kw)
            self._style_fe_actors[pid] = fe
        except Exception as exc:
            logger.debug("style fe overlay pid %s failed: %s", pid, exc)

    def _action_fit_all(self):
        if self.plotter:
            self.plotter.reset_camera()
            self.plotter.render()

    # ── Zoom Window ──────────────────────────────────────────────────────

    def _action_toggle_zoom_window(self, checked: bool) -> None:
        """Enter / exit click-drag rubber-band zoom mode.

        Uses VTK's ``vtkInteractorStyleRubberBandZoom``: the user drags a
        rectangle and on mouse release VTK fits the camera to it. We swap
        the interactor style only while the mode is active, then restore the
        previous style automatically when the zoom completes.
        """
        if self.plotter is None or self.plotter.iren is None:
            self._a_zoom_window.setChecked(False)
            return
        if checked:
            self._zw_enter()
        else:
            self._zw_exit()

    def _zw_vtk_iren(self):
        """Return the underlying VTK render-window interactor.

        ``plotter.iren`` is the PyVista wrapper (``RenderWindowInteractor``)
        which exposes ``get_interactor_style`` / ``set_interactor_style`` but
        not the VTK-style PascalCase methods. The wrapped VTK object lives at
        ``plotter.iren.interactor`` and accepts the standard VTK API.
        """
        if self.plotter is None or self.plotter.iren is None:
            return None
        wrapper = self.plotter.iren
        return getattr(wrapper, "interactor", wrapper)

    def _zw_enter(self) -> None:
        vtk_iren = self._zw_vtk_iren()
        if vtk_iren is None:
            self._a_zoom_window.setChecked(False)
            return
        try:
            from vtkmodules.vtkInteractionStyle import (
                vtkInteractorStyleRubberBandZoom,
            )
        except Exception:
            try:
                import vtk
                vtkInteractorStyleRubberBandZoom = vtk.vtkInteractorStyleRubberBandZoom
            except Exception:
                logger.debug("vtkInteractorStyleRubberBandZoom unavailable", exc_info=True)
                self._a_zoom_window.setChecked(False)
                return
        self._zw_prev_style = vtk_iren.GetInteractorStyle()
        style = vtkInteractorStyleRubberBandZoom()
        # Left-drag draws the rectangle and zooms on release.
        try:
            style.SetLockAspectToViewport(True)
        except Exception:
            pass
        vtk_iren.SetInteractorStyle(style)
        # When the user releases the mouse VTK fires EndInteractionEvent on
        # the style — restore the previous style and uncheck the toolbar.
        self._zw_obs_id = style.AddObserver(
            "EndInteractionEvent", self._zw_on_end
        )
        try:
            self.plotter.interactor.setCursor(Qt.CursorShape.CrossCursor)
        except Exception:
            pass
        self._status.showMessage(
            "Zoom Window: drag a rectangle in the viewer (click the button again to cancel)",
            0,
        )

    def _zw_exit(self) -> None:
        vtk_iren = self._zw_vtk_iren()
        prev = getattr(self, "_zw_prev_style", None)
        obs_id = getattr(self, "_zw_obs_id", None)
        cur_style = vtk_iren.GetInteractorStyle() if vtk_iren is not None else None
        if cur_style is not None and obs_id is not None:
            try:
                cur_style.RemoveObserver(obs_id)
            except Exception:
                pass
        if vtk_iren is not None and prev is not None:
            try:
                vtk_iren.SetInteractorStyle(prev)
            except Exception:
                pass
        self._zw_obs_id = None
        self._zw_prev_style = None
        try:
            self.plotter.interactor.unsetCursor()
        except Exception:
            pass
        if self._a_zoom_window.isChecked():
            # Block recursion through toggled signal.
            self._a_zoom_window.blockSignals(True)
            self._a_zoom_window.setChecked(False)
            self._a_zoom_window.blockSignals(False)
        self._status.clearMessage()

    def _zw_on_end(self, _style, _event) -> None:
        """Fired by vtkInteractorStyleRubberBandZoom when the drag completes."""
        try:
            if self.plotter is not None:
                self.plotter.renderer.ResetCameraClippingRange()
                self.plotter.render()
        except Exception:
            pass
        self._zw_exit()

    def _action_toggle_orbit(self):
        """Start/stop continuous azimuth rotation around the model."""
        if self.plotter is None:
            self._a_orbit.setChecked(False)
            return
        if self._orbit_timer.isActive():
            self._orbit_timer.stop()
            self._a_orbit.setChecked(False)
            self._status.showMessage("Orbit stopped", 2000)
        else:
            self._orbit_timer.start()
            self._a_orbit.setChecked(True)
            self._status.showMessage("Orbit running — click Orbit again to stop", 0)

    def _orbit_step(self):
        """Rotate camera azimuth by 1 degree per timer tick."""
        if self.plotter is None:
            self._orbit_timer.stop()
            return
        try:
            self.plotter.camera.Azimuth(1.0)
            self.plotter.render()
        except Exception:
            self._orbit_timer.stop()
            self._a_orbit.setChecked(False)

    def _action_toggle_properties_panel(self):
        """Show / hide the right-hand Properties panel."""
        panel = getattr(self, "_right_panel", None)
        if panel is None:
            return
        new_visible = not panel.isVisible()
        panel.setVisible(new_visible)
        if hasattr(self, "_a_props"):
            self._a_props.setChecked(new_visible)
        if new_visible:
            self._resize_props_panel_to_fit()
        self._status.showMessage(
            f"Properties panel: {'shown' if new_visible else 'hidden'}", 2000
        )

    def _action_toggle_axes(self):
        if self.plotter is None:
            return
        self._axes_visible = not self._axes_visible
        if self._axes_visible:
            self.plotter.show_axes()
        else:
            self.plotter.hide_axes()
        self._a_axes.setChecked(self._axes_visible)
        self.plotter.render()

    def _action_toggle_grid(self):
        if self.plotter is None:
            return
        self._bounds_visible = not self._bounds_visible
        if self._bounds_visible:
            self.plotter.show_bounds(grid="back", location="outer")
        else:
            self.plotter.remove_bounds_axes()
        self._a_grid.setChecked(self._bounds_visible)
        self.plotter.render()

    def _action_toggle_projection(self):
        if self.plotter is None:
            return
        self._parallel = not self._parallel
        if self._parallel:
            self.plotter.enable_parallel_projection()
            self._a_proj.setIcon(getattr(self, '_v2_ortho_icon', QIcon()))
            self._a_proj.setText("Parallel")
        else:
            self.plotter.disable_parallel_projection()
            self._a_proj.setIcon(getattr(self, '_v2_persp_icon', QIcon()))
            self._a_proj.setText("Perspective")
        self._a_proj.setChecked(self._parallel)
        self.plotter.render()

    def _action_toggle_mesh(self):
        """Toggle visibility of per-cell mesh detail (element edges/points).

        This MUST NOT change the render style or the feature-edge overlay —
        those live in ``_set_render_style``. Mesh = element detail; Style =
        main edges and surface representation.
        """
        if self.plotter is None:
            return
        def _is_visible(actor) -> bool:
            try:
                return bool(actor) and bool(actor.GetVisibility())
            except Exception:
                return False
        selected_pids = self._get_selected_pids()
        if selected_pids:
            visible_pids = [
                pid for pid in selected_pids
                if _is_visible(self._part_actors.get(pid))
            ]
            if not visible_pids:
                self._status.showMessage(
                    "Mesh: no visible parts in selection", 3000
                )
                return
            all_on = all(
                self._per_part_mesh.get(pid, self._mesh_visible)
                for pid in visible_pids
            )
            checked = not all_on
            for pid in visible_pids:
                self._per_part_mesh[pid] = checked
                actor = self._part_actors.get(pid)
                if actor is None:
                    continue
                style = self._per_part_style.get(pid, self._current_style)
                self._apply_style_to_actor(actor, style, mesh_on=checked)
            scope = ", ".join(str(p) for p in visible_pids)
            state = "ON" if checked else "OFF"
            self._status.showMessage(
                f"Mesh: {state}  (parts: {scope})", 3000
            )
        else:
            checked = not self._mesh_visible
            self._mesh_visible = checked
            if hasattr(self, '_v2_a_mesh'):
                self._v2_a_mesh.setChecked(checked)
            self._per_part_mesh.clear()
            style = self._current_style
            for pid, actor in self._part_actors.items():
                if actor is None:
                    continue
                self._apply_style_to_actor(actor, style, mesh_on=checked)
            self._status.showMessage(
                f"Mesh: {'ON' if checked else 'OFF'}", 3000
            )

        # Visibility (actor vs feature-edge overlay) is reconciled centrally.
        self._apply_material_highlight()
        self.plotter.render()

    def _set_opacity(self, text: str):
        if self.plotter is None:
            return
        val = int(text.replace("%", "")) / 100.0
        selected_pids = self._get_selected_pids()
        if selected_pids:
            for pid in selected_pids:
                actor = self._part_actors.get(pid)
                if actor is not None:
                    actor.GetProperty().SetOpacity(val)
                # Keep the dim snapshot in sync so deselection restores the
                # value the user just chose, not the pre-dim baseline.
                if self._pre_dim_opacity:
                    self._pre_dim_opacity[int(pid)] = val
            scope = ", ".join(str(p) for p in selected_pids)
            self._status.showMessage(f"Opacity: {text}  (parts: {scope})", 3000)
        else:
            for actor in self._actors:
                if actor is not None:
                    actor.GetProperty().SetOpacity(val)
        self.plotter.render()

    def _make_fx_callback(self, effect_name: str):
        """Return a slot that toggles *effect_name* on/off."""
        def _callback(checked: bool):
            self._apply_fx(effect_name, checked)
        return _callback

    def _apply_fx_to_actor(self, actor, name: str, on: bool):
        """Apply a per-actor visual effect (PBR / Smooth Shading)."""
        if actor is None:
            return
        prop = actor.GetProperty()
        if name == "PBR (Metallic)":
            if on:
                prop.SetInterpolationToPBR()
                prop.SetMetallic(0.7)
                prop.SetRoughness(0.3)
            else:
                prop.SetInterpolationToPhong()
                prop.SetMetallic(0.0)
                prop.SetRoughness(1.0)
        elif name == "Smooth Shading":
            if on:
                prop.SetInterpolationToPhong()
            else:
                prop.SetInterpolationToFlat()

    def _apply_fx(self, name: str, on: bool):
        """Apply or remove a visual effect."""
        try:
            # Global effects (not per-actor)
            if name == "Silhouette":
                if on:
                    if not hasattr(self, '_silhouette_actor') or self._silhouette_actor is None:
                        self._silhouette_actor = self.plotter.add_silhouette(
                            self._polydata, color="black", line_width=2.5,
                        )
                else:
                    if hasattr(self, '_silhouette_actor') and self._silhouette_actor is not None:
                        self.plotter.remove_actor(self._silhouette_actor)
                        self._silhouette_actor = None

            elif name == "Anti-aliasing":
                if on:
                    self.plotter.enable_anti_aliasing('msaa')
                else:
                    self.plotter.disable_anti_aliasing()

            elif name == "Depth Peeling":
                if on:
                    self.plotter.enable_depth_peeling(number_of_peels=8, occlusion_ratio=0.0)
                else:
                    self.plotter.disable_depth_peeling()

            elif name == "SSAO":
                if on:
                    # Radius/bias auto-scale from the model's bounding box so
                    # the effect reads at any model size.
                    try:
                        b = self.plotter.bounds  # (xmin, xmax, ymin, ymax, zmin, zmax)
                        diag = max(
                            1e-6,
                            ((b[1]-b[0])**2 + (b[3]-b[2])**2 + (b[5]-b[4])**2) ** 0.5,
                        )
                    except Exception:
                        diag = 1.0
                    radius = diag * 0.02
                    bias   = diag * 0.001
                    self.plotter.enable_ssao(
                        radius=radius, bias=bias, kernel_size=64, blur=True,
                    )
                else:
                    try:
                        self.plotter.disable_ssao()
                    except AttributeError:
                        # Older PyVista: rebuild the render pass chain by
                        # toggling EnableSSAO off on the renderer directly.
                        try:
                            self.plotter.renderer.SetUseSSAO(False)
                        except Exception:
                            pass

            # Per-actor effects (PBR, Smooth Shading)
            elif name in ("PBR (Metallic)", "Smooth Shading"):
                selected_pids = self._get_selected_pids()
                if selected_pids:
                    for pid in selected_pids:
                        fx_state = self._per_part_fx.setdefault(pid, {})
                        fx_state[name] = on
                        actor = self._part_actors.get(pid)
                        self._apply_fx_to_actor(actor, name, on)
                    scope = ", ".join(str(p) for p in selected_pids)
                    state = "ON" if on else "OFF"
                    self._status.showMessage(f"{name}: {state}  (parts: {scope})", 3000)
                    self.plotter.render()
                    return
                else:
                    for pid, actor in self._part_actors.items():
                        fx_state = self._per_part_fx.setdefault(pid, {})
                        fx_state[name] = on
                        self._apply_fx_to_actor(actor, name, on)

            self.plotter.render()
            state = "ON" if on else "OFF"
            self._status.showMessage(f"{name}: {state}", 3000)
        except Exception as exc:
            logger.warning("Visual effect '%s' failed: %s", name, exc)
            self._status.showMessage(f"{name}: not available", 3000)

    def _set_background(self, bg: str):
        if self.plotter is None:
            return
        if bg == "__gradient_blue":
            self.plotter.set_background("royalblue", top="aliceblue")
        elif bg == "__gradient_gray":
            self.plotter.set_background("#3c3c3c", top="#888888")
        else:
            self.plotter.set_background(bg)
        self._current_bg = bg
        # Edge color follows the theme unless the user picked a custom one.
        if self._custom_edge_color is not None:
            edge_rgb = self._custom_edge_color
        else:
            edge_rgb = (1.0, 1.0, 1.0) if bg == "black" else (0.0, 0.0, 0.0)
        if self._wire_overlay_actor is not None:
            self._wire_overlay_actor.GetProperty().SetColor(*edge_rgb)
        # Keep the Config swatch in sync when bg changes via View-tab buttons.
        swatch = getattr(self, "_cfg_bg_swatch", None)
        if swatch is not None and isinstance(bg, str) and bg.startswith("#") and len(bg) == 7:
            try:
                rgb = (int(bg[1:3], 16) / 255.0,
                       int(bg[3:5], 16) / 255.0,
                       int(bg[5:7], 16) / 255.0)
                self._set_swatch_color(swatch, rgb)
            except ValueError:
                pass
        self.plotter.render()

    def _action_clear_cache(self):
        if self._file_path is None:
            self._status.showMessage("No file loaded", 2000)
            return
        try:
            from ui.viewer_v2_loader import viewer_cache_key, viewer_cache_dir
            key  = viewer_cache_key(self._file_path)
            d    = viewer_cache_dir()
            removed = 0
            for ext in (".vtp", ".json"):
                p = d / f"{key}{ext}"
                if p.exists():
                    p.unlink()
                    removed += 1
            msg = f"Cache cleared ({removed} files)" if removed else "No cache found"
            self._status.showMessage(msg, 4000)
        except Exception as exc:
            self._status.showMessage(f"Cache clear failed: {exc}", 4000)

    def _action_about(self):
        QMessageBox.about(
            self, "About 3D Viewer 2.0",
            "<h3>3D Viewer 2.0</h3>"
            "<p>Next-generation LS-DYNA model viewer for KeywordManager.</p>"
            "<p>Design: hybrid Open Step Viewer 27.3 + Autodesk Viewer.<br>"
            "Style: industrial/sober — steel-blue accent on dark panels.</p>"
            "<p>Reuses PyDyna parse, VTK cache, and render engine from<br>"
            "the classic 3D Model Viewer.</p>",
        )

    # =========================================================================
    #  Status / lifecycle
    # =========================================================================

    def _apply_theme(self, theme_name: str):
        """Apply a viewer GUI theme.
        'Dark (Viewer)' uses _STYLESHEET_DARK.
        All light themes use _STYLESHEET + per-theme overrides from _VIEWER_THEMES.
        """
        self._current_theme = theme_name
        if theme_name == "Dark (Viewer)":
            self.setStyleSheet(_STYLESHEET_DARK)
            if self.plotter is not None and not self._theme_is_dark:
                self._prev_bg = self._current_bg
                self._set_background("#2b2b2b")
            self._theme_is_dark = True
        else:
            override = _VIEWER_THEMES.get(theme_name, "")
            self.setStyleSheet(_STYLESHEET + override)
            if self.plotter is not None and self._theme_is_dark:
                self._set_background(self._prev_bg)
            self._theme_is_dark = False
        self._status.showMessage(f"Theme: {theme_name}", 3000)

    def _update_status(self):
        if self._polydata is None or self._file_path is None:
            return
        parts_txt = f"  |  Parts: {self._n_parts}" if self._has_parts else ""
        self._status.showMessage(
            f"{self._file_path.name}  |  "
            f"Nodes: {self._polydata.n_points:,}  |  "
            f"Cells: {self._polydata.n_cells:,}{parts_txt}"
        )

    def closeEvent(self, event):
        if getattr(self, "_orbit_timer", None) is not None:
            try:
                self._orbit_timer.stop()
            except Exception:
                pass
        if self._load_worker is not None:
            try:
                self._load_worker.finished.disconnect()
            except RuntimeError:
                pass
            try:
                self._load_worker.geometry_ready.disconnect()
            except RuntimeError:
                pass
            self._load_worker.quit()
            self._load_worker.wait(2000)
            self._load_worker = None
        if self.plotter is not None:
            try:
                self.plotter.clear()
            except Exception:
                pass
            try:
                rw = self.plotter.render_window
                if rw is not None:
                    rw.Finalize()
            except Exception:
                pass
            try:
                self.plotter.close()
            except Exception:
                pass
        # Drop the PyDyna polydata cache built lazily for Wireframe so the
        # underlying VTK objects can be released before the dialog dies.
        self._pydyna_polydata = None
        self._pydyna_polydata_failed = False
        super().closeEvent(event)
