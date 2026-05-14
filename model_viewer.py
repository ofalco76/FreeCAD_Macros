# ui/model_viewer.py
"""
3D Viewer for LS-DYNA .k model files.

Uses PyDyna (ansys-dyna-core) to parse the model and PyVista/VTK to render.

Two modes:
  - **Qt-embedded** (preferred): pyvistaqt ``QtInteractor`` inside a QDialog
    with custom toolbars for render style, camera views, display toggles, etc.
  - **Native fallback**: plain ``pv.Plotter`` window (keyboard-only controls)
    used automatically when *pyvistaqt* is not installed.

See docs/VISUALIZATION_ANALYSIS.md for full architecture analysis.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSize, QThread, Signal, QTimer
from PySide6.QtGui import QAction, QIcon, QPixmap, QPainter, QColor, QPen, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QSizePolicy,
    QStatusBar,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Dependency helpers
# ---------------------------------------------------------------------------

_HAS_PYVISTAQT: bool | None = None  # cached at first call


def _check_dependencies() -> Optional[str]:
    """Return an error message if any dependency is missing, else ``None``."""
    try:
        import pyvista  # noqa: F401
    except ImportError as exc:
        # Capture the real underlying error (e.g. missing stdlib module in
        # frozen builds) so it's visible in the UI instead of a generic
        # "PyVista is not installed" message.
        import traceback
        tb = traceback.format_exc()
        logger.error("pyvista import failed:\n%s", tb)
        return (
            f"3D viewer not available\n"
            f"({type(exc).__name__}: {exc})"
        )
    try:
        from ansys.dyna.core import Deck  # noqa: F401
        from ansys.dyna.core.lib.deck_plotter import get_polydata  # noqa: F401
    except ImportError as exc:
        return (
            f"ansys-dyna-core not available\n"
            f"({type(exc).__name__}: {exc})"
        )
    return None


def _has_pyvistaqt() -> bool:
    global _HAS_PYVISTAQT
    if _HAS_PYVISTAQT is None:
        try:
            from pyvistaqt import QtInteractor  # noqa: F401
            _HAS_PYVISTAQT = True
        except Exception:
            _HAS_PYVISTAQT = False
    return _HAS_PYVISTAQT


# ---------------------------------------------------------------------------
#  Geometry-only pre-filter for the 3D viewer
# ---------------------------------------------------------------------------
# Keyword prefixes to *keep* when filtering a .k file before PyDyna parsing.
# Both the exact name and underscore-extended variants are matched, e.g.
# "ELEMENT_SHELL" also covers "ELEMENT_SHELL_BETA", "ELEMENT_SHELL_THICKNESS".
#
# Everything else (*MAT_*, *CONTACT_*, *PARAMETER, *DEFINE_*, *BOUNDARY_*, …)
# is discarded so PyDyna has far fewer keywords to parse.
_VIEWER_KEEP_PREFIXES: tuple = (
    "KEYWORD", "END",
    "NODE",
    "ELEMENT_SHELL", "ELEMENT_SOLID", "ELEMENT_BEAM",
    "ELEMENT_TSHELL", "ELEMENT_MASS", "ELEMENT_DISCRETE",
    "ELEMENT_SEATBELT",
    "PART",
    "INCLUDE",               # catches INCLUDE and INCLUDE_TRANSFORM
    "DEFINE_TRANSFORMATION", # required so INCLUDE_TRANSFORM can apply TRANSL/ROTATE
    "SET_NODE", "SET_SHELL", "SET_SOLID", "SET_PART", "SET_SEGMENT",
)


def _kw_should_keep(line: str):
    """Return True/False if *line* opens a keyword block to keep/discard.

    Returns ``None`` for non-keyword lines (data lines, ``$`` comments).
    """
    stripped = line.lstrip()
    if not stripped.startswith("*"):
        return None
    rest = stripped[1:].split("$")[0].strip()
    kw = rest.split()[0].upper() if rest else ""
    for prefix in _VIEWER_KEEP_PREFIXES:
        if kw == prefix or kw.startswith(prefix + "_"):
            return True
    return False


def _write_geometry_filtered_temp(file_path: Path):
    """Write a geometry-only filtered copy of *file_path* to a temp file.

    The temp file is written **next to the original** (same directory, ``.tmp``
    extension) so that relative ``*INCLUDE`` / ``*INCLUDE_TRANSFORM`` paths
    resolve naturally without any path rewriting.  A ``.tmp`` extension is used
    instead of ``.k`` to avoid triggering QFileSystemWatcher patterns that
    watch for ``.k`` files in the project directory.

    Absolute INCLUDE paths are kept as-is.  Relative paths are left unchanged
    because the temp file lives in the same directory as the source.

    Returns the temp ``Path`` on success, or ``None`` on any failure.
    **The caller is responsible for deleting the file afterwards.**
    """
    import tempfile

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    base_dir = file_path.parent
    out_lines: list = []
    keep_block = True       # keep any leading header/comment lines before first *keyword
    awaiting_include_fname = False   # True immediately after an *INCLUDE* keyword line

    for line in content.splitlines(keepends=True):
        stripped = line.strip()

        # ── filename line right after *INCLUDE* / *INCLUDE_TRANSFORM* ──────
        if awaiting_include_fname:
            if not stripped or stripped.startswith("$"):
                # comment or blank — still waiting for the filename
                if keep_block:
                    out_lines.append(line)
                continue
            # First non-comment data line is the include filename — keep as-is.
            # Relative paths resolve correctly because the temp file is in the
            # same directory as the original.  No rewriting needed.
            awaiting_include_fname = False
            if keep_block:
                out_lines.append(line)
            continue

        # ── keyword or data line ────────────────────────────────────────────
        decision = _kw_should_keep(line)
        if decision is not None:
            keep_block = decision
            if keep_block and stripped.startswith("*"):
                kw = stripped[1:].split("$")[0].strip().split()[0].upper()
                if kw.startswith("INCLUDE"):
                    awaiting_include_fname = True

        if keep_block:
            out_lines.append(line)

    try:
        # Write next to the original so relative INCLUDE paths still resolve.
        # Use .tmp (not .k) to avoid triggering file-watcher patterns.
        fd, tmp = tempfile.mkstemp(suffix=".tmp", prefix="._mv_", dir=str(base_dir))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.writelines(out_lines)
        return Path(tmp)
    except Exception:
        # Fall back to %TEMP% if the source directory is not writable.
        try:
            fd, tmp = tempfile.mkstemp(suffix=".tmp", prefix="mv_tmp_")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.writelines(out_lines)
            return Path(tmp)
        except Exception:
            return None


# ---------------------------------------------------------------------------
#  Viewer VTK binary cache
# ---------------------------------------------------------------------------

def _viewer_cache_key(file_path: Path) -> str:
    """Return a 16-char hex key derived from the resolved path + mtime + size + filter version.

    The filter version is a short hash of _VIEWER_KEEP_PREFIXES so that any
    change to the geometry pre-filter automatically invalidates all cached
    entries (they were built with a different set of keywords).
    """
    import hashlib
    _filter_ver = hashlib.sha1("|".join(sorted(_VIEWER_KEEP_PREFIXES)).encode()).hexdigest()[:6]
    try:
        st = file_path.stat()
        raw = f"{file_path.resolve()}|{st.st_mtime}|{st.st_size}|{_filter_ver}"
    except Exception:
        raw = f"{file_path.resolve()}|{_filter_ver}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _viewer_cache_dir() -> Path:
    import tempfile
    return Path(tempfile.gettempdir()) / "pydeck_viewer_cache"


def _entities_to_json_safe(entities: dict) -> dict:
    """Convert a keyword_entities dict to a JSON-serialisable form.

    ``__names__`` tuple-keys ``(category, sid)`` are encoded as ``"cat::sid"``.
    SET_SEGMENT members (tuples of ints) are converted to lists.
    """
    out: dict = {}
    for cat, val in entities.items():
        if cat == "__names__":
            out["__names__"] = {
                f"{c}::{s}": title for (c, s), title in val.items()
            }
        else:
            out[cat] = {
                str(sid): [
                    list(m) if isinstance(m, tuple) else m
                    for m in members
                ]
                for sid, members in val.items()
            }
    return out


def _entities_from_json_safe(data: dict) -> dict:
    """Restore a keyword_entities dict from its JSON-serialised form."""
    out: dict = {}
    for cat, val in data.items():
        if cat == "__names__":
            names: dict = {}
            for key, title in val.items():
                parts = key.split("::", 1)
                if len(parts) == 2:
                    names[(parts[0], int(parts[1]))] = title
            out["__names__"] = names
        else:
            sid_map: dict = {}
            for sid_str, members in val.items():
                if cat == "SET_SEGMENT":
                    sid_map[int(sid_str)] = [tuple(m) for m in members]
                else:
                    sid_map[int(sid_str)] = [int(m) for m in members]
            out[cat] = sid_map
    return out


def _viewer_cache_load(cache_key: str):
    """Return ``(polydata, part_names, entities)`` from cache, or ``(None, None, None)``."""
    import json
    import pyvista as pv
    d = _viewer_cache_dir()
    vtp_path = d / f"{cache_key}.vtp"
    meta_path = d / f"{cache_key}.json"
    if not vtp_path.exists() or not meta_path.exists():
        return None, None, None
    try:
        polydata = pv.read(str(vtp_path))
        with meta_path.open("r", encoding="utf-8") as f:
            m = json.load(f)
        part_names = {int(k): v for k, v in m.get("part_names", {}).items()}
        entities = _entities_from_json_safe(m.get("entities", {}))
        return polydata, part_names, entities
    except Exception as exc:
        logger.debug("Viewer cache load failed (%s): %s", cache_key, exc)
        return None, None, None


def _viewer_cache_save(cache_key: str, polydata, part_names: dict, entities: dict) -> None:
    """Persist viewer polydata and metadata to cache.  Silent on any failure."""
    import json
    try:
        d = _viewer_cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        vtp_path = d / f"{cache_key}.vtp"
        polydata.save(str(vtp_path))
        meta = {
            "part_names": {str(k): v for k, v in part_names.items()},
            "entities": _entities_to_json_safe(entities),
        }
        meta_path = d / f"{cache_key}.json"
        with meta_path.open("w", encoding="utf-8") as f:
            json.dump(meta, f)
        logger.debug("Viewer cache saved: %s", vtp_path.name)
    except Exception as exc:
        logger.debug("Viewer cache save failed (%s): %s", cache_key, exc)


# ---------------------------------------------------------------------------
#  Shared: parse model → polydata
# ---------------------------------------------------------------------------

def _load_polydata(file_path: Path):
    """Parse *file_path* with PyDyna and return *(polydata, part_names, keyword_entities, error)*.

    Returns ``(polydata, part_names_dict, keyword_entities_dict, None)`` on
    success or ``(None, {}, {}, error_string)`` on failure.

    Performance optimisations applied here:
    - VTK binary cache: on a cache hit the full PyDyna parse is skipped
      entirely; the cached ``.vtp`` + ``.json`` are loaded instead.
    - Geometry-only pre-filter: on a cache miss, the file is pre-filtered
      to keep only NODE, ELEMENT_*, PART, INCLUDE and SET_* keywords before
      being handed to PyDyna, discarding MAT, CONTACT, PARAMETER, etc.
    """
    from ansys.dyna.core import Deck
    from ansys.dyna.core.lib.deck_plotter import get_polydata
    import warnings

    # ── 1. VTK cache lookup ──────────────────────────────────────────────
    _cache_key = _viewer_cache_key(file_path)
    _cached_pd, _cached_names, _cached_entities = _viewer_cache_load(_cache_key)
    if _cached_pd is not None:
        logger.debug("Viewer cache hit: %s", file_path.name)
        return _cached_pd, _cached_names, _cached_entities, None

    # ── 2. Geometry-only pre-filter ──────────────────────────────────────
    # Write a filtered copy to a temp file in the same directory so that
    # relative *INCLUDE paths inside the file still resolve correctly.
    tmp_path = _write_geometry_filtered_temp(file_path)
    parse_path = tmp_path if tmp_path is not None else file_path

    try:
        deck = Deck()
        deck.import_file(str(parse_path))
    except Exception as exc:
        logger.exception("Deck.import_file failed for %s", file_path)
        return None, {}, {}, f"Failed to parse the model file:\n\n{file_path.name}\n\n{exc}"
    finally:
        # Always remove the temp filtered file, even if import_file raised.
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    cwd = str(file_path.parent) or "."

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r".*ROTATE option with parameters \(0\.0, 0\.0, 0\.0, 0\.0, 0\.0, 0\.0, 0\.0\).*",
            )
            warnings.simplefilter("ignore", DeprecationWarning)
            polydata = get_polydata(deck, cwd=cwd)
    except Exception as exc:
        logger.exception("get_polydata failed for %s", file_path)
        # Detect "no geometry" cases: PyDyna raises a plain Exception with this
        # message when the deck has no NODE/ELEMENT keywords, and a KeyError on
        # ['x','y','z'] when the nodes DataFrame is empty (same root cause).
        exc_str = str(exc)
        no_geometry = (
            "missing node or element keyword" in exc_str.lower()
            or ("x" in exc_str and "y" in exc_str and "z" in exc_str and "columns" in exc_str)
        )
        if no_geometry:
            return None, {}, {}, (
                "This file does not contain geometry data (NODE / ELEMENT keywords).\n\n"
                "The 3D viewer only displays files with mesh data.\n"
                "Material-only or parameter-only decks cannot be visualised."
            )
        return None, {}, {}, f"Could not extract geometry from the model:\n\n{exc}"

    # Expand for part names (flat deck has all parts with offsets applied)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            flat_deck = deck.expand(cwd=cwd, recurse=True)
    except Exception:
        flat_deck = deck

    # Map VTK surface point indices → LS-DYNA node IDs so entity
    # visualization can match SET_NODE / SET_SEGMENT members correctly.
    # ``vtkOriginalPointIds`` holds UnstructuredGrid indices (before
    # surface extraction), and ``merge_keywords`` gives us the node
    # DataFrame whose row order matches those UG indices.
    try:
        from ansys.dyna.core.lib.deck_plotter import merge_keywords
        nodes_df, _ = merge_keywords(flat_deck)
        orig_ids = polydata.point_data.get("vtkOriginalPointIds")
        if orig_ids is not None and "nid" in nodes_df.columns:
            nid_array = nodes_df["nid"].values
            polydata.point_data["node_ids"] = nid_array[orig_ids]
    except Exception:
        logger.debug("Could not build node_ids mapping", exc_info=True)

    part_names = _extract_part_names(flat_deck)
    keyword_entities = _extract_keyword_entities(deck, cwd)

    # ── 3. Persist to VTK cache for future opens ─────────────────────────
    _viewer_cache_save(_cache_key, polydata, part_names, keyword_entities)

    return polydata, part_names, keyword_entities, None


def _extract_sets_from_deck(deck) -> list:
    """Return a list of ``(category, sid, members, title)`` from SET keywords in *deck*.

    Does NOT apply any ID offsets – returns raw IDs as stored in the file.
    """
    from ansys.dyna.core.lib.series_card import SeriesCard

    # Longer prefixes first so "SetNodeList" matches before "SetNode".
    _PREFIX_MAP = [
        ("SetNodeList",  "SET_NODE"),
        ("SetShellList", "SET_SHELL"),
        ("SetSolidList", "SET_SOLID"),
        ("SetPartList",  "SET_PART"),
        ("SetSegment",   "SET_SEGMENT"),
        # Non-*_LIST variants (PyDyna uses SetSolid, SetNode, SetShell …)
        ("SetSolid",     "SET_SOLID"),
        ("SetShell",     "SET_SHELL"),
        ("SetNode",      "SET_NODE"),
        ("SetPart",      "SET_PART"),
    ]

    results: list[tuple[str, int, list, str]] = []
    for kw_obj in deck.keywords:
        cname = type(kw_obj).__name__
        category = None
        for prefix, cat in _PREFIX_MAP:
            if cname.startswith(prefix):
                category = cat
                break
        if category is None:
            continue

        sid = getattr(kw_obj, "sid", None)
        if sid is None:
            try:
                sid = int(kw_obj.cards[0].sid)
            except Exception:
                continue
        if sid is None:
            continue
        sid = int(sid)

        title = getattr(kw_obj, "title", None) or ""
        if isinstance(title, str):
            title = title.strip()
        else:
            title = ""

        members: list = []
        try:
            if category == "SET_SEGMENT":
                for card in kw_obj.cards:
                    if type(card).__name__ == "TableCard":
                        df = card.table
                        if hasattr(df, "columns") and len(df) > 0:
                            cols = [c for c in ["n1", "n2", "n3", "n4"]
                                    if c in df.columns]
                            for _, row in df[cols].iterrows():
                                seg = tuple(
                                    int(row[c]) for c in cols if row[c] != 0
                                )
                                if seg:
                                    members.append(seg)
                        break
            else:
                for card in kw_obj.cards:
                    if isinstance(card, SeriesCard):
                        members = [int(v) for v in card.data if v != 0]
                        break
        except Exception:
            continue

        if members:
            results.append((category, sid, members, title))
    return results


# Offset key per SET category: which IncludeTransform attribute offsets the
# member IDs inside each SET type.
_MEMBER_OFFSET_ATTR = {
    "SET_NODE":    "idnoff",
    "SET_SHELL":   "ideoff",
    "SET_SOLID":   "ideoff",
    "SET_PART":    "idpoff",
    "SET_SEGMENT": "idnoff",
}


def _extract_keyword_entities(deck, cwd: str) -> dict:
    """Extract SET keyword entities, resolving ``*INCLUDE_TRANSFORM`` offsets.

    ``deck.expand()`` applies node/element/part ID offsets to geometric
    keywords (NODE, ELEMENT) but does **not** offset SET keyword SIDs or
    their member IDs.  This function manually resolves each include file,
    parses it, and applies the ``idsoff`` / ``idnoff`` / ``ideoff`` /
    ``idpoff`` offsets to produce entities that match the assembled polydata.

    Returns::

        {
            "SET_NODE":    {2000001: [2000001, 2000002, ...], ...},
            "SET_PART":    {1000001: [1000001]},
            ...
        }
    """
    from ansys.dyna.core import Deck as _Deck

    entities: dict[str, dict[int, list]] = {}
    names: dict[tuple, str] = {}  # (cat, sid) → title

    def _merge(category, sid, members, title=""):
        entities.setdefault(category, {})[sid] = members
        if title:
            names[(category, sid)] = title

    try:
        # 1) SET keywords in the main file itself (no offset)
        for cat, sid, members, title in _extract_sets_from_deck(deck):
            _merge(cat, sid, members, title)

        # 2) Resolve *INCLUDE_TRANSFORM sub-files with offsets
        from ansys.dyna.core.keywords.keyword_classes.auto.include.include_transform import (
            IncludeTransform as _IncludeTransform,
        )
        from ansys.dyna.core.keywords.keyword_classes.auto.include.include import (
            Include as _Include,
        )

        for kw_obj in deck.keywords:
            is_transform = isinstance(kw_obj, _IncludeTransform)
            is_include = isinstance(kw_obj, _Include) and not is_transform

            if not (is_transform or is_include):
                continue

            fname = getattr(kw_obj, "filename", None)
            if not fname:
                continue

            sub_path = Path(cwd) / fname
            if not sub_path.is_file():
                continue

            # Parse the sub-file
            try:
                sub_deck = _Deck()
                sub_deck.import_file(str(sub_path))
            except Exception:
                logger.debug("Failed to parse include %s", sub_path)
                continue

            # Determine offsets
            idsoff = int(getattr(kw_obj, "idsoff", 0) or 0) if is_transform else 0

            for cat, sid, members, title in _extract_sets_from_deck(sub_deck):
                # Apply idsoff to SID
                offset_sid = sid + idsoff

                # Apply the category-specific member ID offset
                member_attr = _MEMBER_OFFSET_ATTR.get(cat, "")
                member_off = int(getattr(kw_obj, member_attr, 0) or 0) if (is_transform and member_attr) else 0

                if cat == "SET_SEGMENT":
                    offset_members = [
                        tuple(nid + member_off for nid in seg)
                        for seg in members
                    ]
                else:
                    offset_members = [mid + member_off for mid in members]

                _merge(cat, offset_sid, offset_members, title)

    except Exception:
        logger.debug("Keyword entity extraction failed", exc_info=True)

    if names:
        entities["__names__"] = names
    return entities


def _extract_part_names(deck) -> dict:
    """Extract ``{pid: part_heading}`` from a PyDyna ``Deck``.

    Falls back to ``Part <pid>`` when the heading is blank.
    """
    part_names: dict[int, str] = {}
    try:
        parts = deck.parts
        for i in range(len(parts)):
            p = parts[i]
            s = str(p)
            lines = s.strip().splitlines()
            data_lines = [
                l for l in lines
                if not l.startswith('*') and not l.startswith('$')
            ]
            heading = ""
            pid_val = None
            if len(data_lines) >= 2:
                heading = data_lines[0].strip()
                fields = data_lines[1].split()
                if fields:
                    try:
                        pid_val = int(fields[0])
                    except ValueError:
                        pass
            if pid_val is not None:
                part_names[pid_val] = heading if heading else f"Part {pid_val}"
    except Exception:
        pass
    return part_names


# ---------------------------------------------------------------------------
#  Small icon helpers (programmatic, no resource files needed)
# ---------------------------------------------------------------------------

def _icon_from_text(text: str, size: int = 20, fg: str = "#333",
                    bg: str = "transparent") -> QIcon:
    """Create a tiny QIcon with *text* rendered in the centre."""
    px = QPixmap(QSize(size, size))
    px.fill(QColor(bg) if bg != "transparent" else QColor(0, 0, 0, 0))
    p = QPainter(px)
    if not p.isActive():
        return QIcon(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor(fg))
    p.setFont(QFont("Segoe UI", int(size * 0.48), QFont.Weight.Bold))
    p.drawText(px.rect(), Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return QIcon(px)


# ---------------------------------------------------------------------------
#  Menu style override – force 32 px icons in dropdown menus
# ---------------------------------------------------------------------------

from PySide6.QtWidgets import QProxyStyle, QStyle

class _LargeIconMenuStyle(QProxyStyle):
    """QProxyStyle that enlarges the icon size inside QMenu to 32×32."""

    def pixelMetric(self, metric, option=None, widget=None):
        if metric == QStyle.PixelMetric.PM_SmallIconSize:
            return 32
        return super().pixelMetric(metric, option, widget)

# Keep a single shared instance to avoid GC issues
_large_icon_style = _LargeIconMenuStyle()


# ---------------------------------------------------------------------------
#  Draggable toolbar widget (floats freely over any parent)
# ---------------------------------------------------------------------------

class _DraggableToolBar(QToolBar):
    """A QToolBar that can be dragged freely over its parent widget."""

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(title, parent)
        self._drag_pos = None
        self.setMovable(False)      # we handle movement ourselves
        self.setFloatable(False)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.pos()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            new_pos = event.globalPosition().toPoint() - self._drag_pos
            # Clamp inside parent, avoiding menu bar and status bar
            p = self.parentWidget()
            if p:
                max_x = p.width() - self.width()
                # Determine vertical bounds: below menu bar, above status bar
                min_y = 0
                max_y = p.height() - self.height()
                menu_bar = getattr(p, '_menu_bar', None)
                status_bar = getattr(p, '_status', None)
                if menu_bar and menu_bar.isVisible():
                    min_y = menu_bar.y() + menu_bar.height()
                if status_bar and status_bar.isVisible():
                    max_y = status_bar.y() - self.height()
                new_pos.setX(max(0, min(new_pos.x(), max_x)))
                new_pos.setY(max(min_y, min(new_pos.y(), max_y)))
            self.move(new_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)


# ═══════════════════════════════════════════════════════════════════════════
#  Background worker for parsing .k files
# ═══════════════════════════════════════════════════════════════════════════

class _LoadWorker(QThread):
    """Background thread that parses a .k file into VTK polydata."""
    finished = Signal(object, dict, dict, str)  # (polydata, part_names, kw_entities, error)

    def __init__(self, path: Path):
        super().__init__()
        self._path = path

    def run(self):
        polydata, part_names, kw_entities, error = _load_polydata(self._path)
        self.finished.emit(polydata, part_names, kw_entities, error or "")


# ═══════════════════════════════════════════════════════════════════════════
#  Qt-embedded 3D viewer (pyvistaqt)  —  full toolbar
# ═══════════════════════════════════════════════════════════════════════════

class KModelViewerDialog(QDialog):
    """Modal-less dialog with an embedded VTK viewport and custom toolbars.

    Requires *pyvistaqt*.
    """

    def __init__(self, polydata=None, file_path: Path | None = None,
                 part_names: dict | None = None,
                 keyword_entities: dict | None = None,
                 parent: QWidget | None = None,
                 _deferred_load: bool = False):
        super().__init__(parent)
        title = f" 3D Model Viewer \u2014 {file_path.name} " if file_path else "3D Model Viewer"
        self.setWindowTitle(title)
        self.resize(1100, 650)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint
        )

        self._polydata = polydata
        self._file_path = file_path
        self._actors: list = []
        self._part_actors: dict[int, object] = {}    # pid → VTK actor
        self._part_polydata: dict[int, object] = {}  # pid → sub-polydata
        self._part_names: dict[int, str] = part_names or {}
        self._keyword_entities: dict = keyword_entities or {}
        self._entity_actors: dict[str, object] = {}   # "SET_NODE_1" → actor
        self._edges_visible = True
        self._mesh_visible = True          # all cell edges (mesh wireframe)
        self._feature_edges_actor = None   # actor for outline-only edges
        self._current_bg = "white"             # track current background for edge color
        self._current_style = "Surface + Edges"  # currently selected render style
        self._silhouette_actor = None    # silhouette overlay actor
        self._axes_visible = True
        self._scalar_bar_visible = False
        self._bounds_visible = False
        self._parallel = False
        self._load_worker: _LoadWorker | None = None

        if polydata is not None:
            has_parts = "part_ids" in polydata.cell_data
            self._n_parts = len(set(polydata.cell_data["part_ids"])) if has_parts else 0
            self._has_parts = has_parts
        else:
            self._has_parts = False
            self._n_parts = 0

        # --- Layout -----------------------------------------------------------
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Menu bar
        self._menu_bar = self._build_menu_bar()
        root.setMenuBar(self._menu_bar)

        # Loading overlay (shown while background parsing is in progress)
        self._loading_widget = QWidget(self)
        _lw_lay = QVBoxLayout(self._loading_widget)
        _lw_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_label = QLabel("Loading model\u2026")
        self._loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_label.setStyleSheet("font-size: 18px; color: #666;")
        _lw_lay.addWidget(self._loading_label)
        self._loading_bar = QProgressBar()
        self._loading_bar.setRange(0, 0)  # indeterminate
        self._loading_bar.setFixedWidth(300)
        _lw_lay.addWidget(self._loading_bar, alignment=Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._loading_widget, stretch=1)

        # Plotter placeholder \u2013 created lazily by _ensure_plotter()
        self.plotter = None
        self._plotter_container = root  # remember so we can insert plotter later

        # Status bar
        self._status = QStatusBar()
        self._status.setSizeGripEnabled(False)
        root.addWidget(self._status)

        if _deferred_load:
            # Dialog is shown immediately; model will be delivered later
            # via _on_background_loaded().
            self._status.showMessage("Parsing model\u2026")
            return

        # --- Non-deferred path (polydata already available) -------------------
        self._init_viewer()

    # ------------------------------------------------------------------
    #  Deferred initialisation helpers
    # ------------------------------------------------------------------

    def _ensure_plotter(self):
        """Create the QtInteractor if it hasn't been created yet."""
        if self.plotter is not None:
            return
        from pyvistaqt import QtInteractor
        self.plotter = QtInteractor(parent=self)
        # Insert plotter *before* the status bar (last widget)
        idx = self._plotter_container.count() - 1
        self._plotter_container.insertWidget(idx, self.plotter, stretch=1)

    def _hide_loading(self):
        """Remove the loading overlay."""
        if self._loading_widget is not None:
            self._loading_widget.setVisible(False)

    def _init_viewer(self):
        """Initialise plotter, toolbar, tree, and mesh (called once data is
        available, either immediately or after background loading)."""
        self._hide_loading()
        self._ensure_plotter()

        # --- Build floating toolbar ----------------------------------------
        self._build_floating_toolbar()

        # --- Build part tree panel (hidden until a multi-part model loads) --
        self._build_part_tree()

        # --- Add mesh (only if model provided) -----------------------------
        if self._polydata is not None:
            self._add_model_mesh()
            self._update_status_info()
        else:
            self.plotter.set_background("white")
            self.plotter.add_axes()
            self._status.showMessage("No model loaded  \u2014  use File \u2192 Import .k Model")

        # Reposition toolbar & tree (showEvent may have already fired
        # before the plotter / toolbar existed in deferred-load mode).
        if hasattr(self, '_floating_tb') and self.plotter is not None:
            # Need a short delay so the plotter widget gets its geometry
            # settled before we try to position the toolbar over it.
            QTimer.singleShot(100, self._position_toolbar_and_tree)

    def _position_toolbar_and_tree(self):
        """Place the floating toolbar and part tree after geometry is settled."""
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()  # ensure layout geometry is up-to-date
        if hasattr(self, '_floating_tb') and self.plotter is not None:
            plotter_pos = self.plotter.mapTo(self, self.plotter.rect().topLeft())
            self._floating_tb.move(plotter_pos.x() + 8, plotter_pos.y() + 8)
            self._floating_tb.raise_()
            self._floating_tb.setVisible(True)
        # Reset cached tree position so it recalculates from the real toolbar
        if hasattr(self, '_tree_fixed_top_y'):
            del self._tree_fixed_top_y
        self._reposition_part_tree()

    def load_file_async(self, path: Path):
        """Start background parsing of *path*.  Dialog should already be visible."""
        self._file_path = path
        self.setWindowTitle(f" 3D Model Viewer \u2014 {path.name} ")
        self._loading_label.setText(f"Loading {path.name}\u2026")
        self._load_worker = _LoadWorker(path)
        self._load_worker.finished.connect(self._on_background_loaded)
        self._load_worker.start()

    def _on_background_loaded(self, polydata, part_names, kw_entities, error):
        """Slot called when *_LoadWorker* finishes."""
        self._load_worker = None
        if error:
            self._hide_loading()
            self._status.showMessage(f"Error: {error}")
            QMessageBox.critical(self, "Model error", error)
            return

        self._polydata = polydata
        self._part_names = part_names
        self._keyword_entities = kw_entities

        if polydata is not None:
            has_parts = "part_ids" in polydata.cell_data
            self._n_parts = len(set(polydata.cell_data["part_ids"])) if has_parts else 0
            self._has_parts = has_parts
        else:
            self._has_parts = False
            self._n_parts = 0

        self._init_viewer()

    # ------------------------------------------------------------------
    #  Toolbar builders
    # ------------------------------------------------------------------

    def _build_menu_bar(self):
        """Create a menu bar with File menu."""
        from PySide6.QtWidgets import QMenuBar
        mb = QMenuBar(self)

        file_menu = mb.addMenu("&File")

        act_import = QAction("Import .k Model...", self)
        act_import.setShortcut("Ctrl+O")
        act_import.setToolTip("Open a .k / .key / .dyn model file")
        act_import.triggered.connect(self._import_model)
        file_menu.addAction(act_import)

        file_menu.addSeparator()

        act_screenshot = QAction("Save Screenshot...", self)
        act_screenshot.setShortcut("Ctrl+S")
        act_screenshot.triggered.connect(self._screenshot)
        file_menu.addAction(act_screenshot)

        file_menu.addSeparator()

        act_close = QAction("Close", self)
        act_close.setShortcut("Ctrl+W")
        act_close.triggered.connect(self.close)
        file_menu.addAction(act_close)

        # ── About menu ──────────────────────────────────────────────
        about_menu = mb.addMenu("&About")

        act_about = QAction("About 3D Model Viewer", self)
        act_about.triggered.connect(self._show_about)
        about_menu.addAction(act_about)

        return mb

    def _show_about(self):
        """Show an About dialog with version information."""
        icons_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "resources", "icons",
        )
        msg = QMessageBox(self)
        msg.setWindowTitle("About 3D Model Viewer")
        ico_path = os.path.join(icons_dir, "viewer_3D_.ico")
        if os.path.exists(ico_path):
            px = QPixmap(ico_path).scaled(
                64, 64, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            msg.setIconPixmap(px)
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText(
            "<h3>3D Model Viewer</h3>"
            "<p>Version <b>0.9.1-rc1</b></p>"
            "<p>Interactive LS-DYNA .k model viewer built with "
            "PyVista and PySide6.</p>"
            "<p>© 2026 KeywordManager</p>"
        )
        msg.exec()

    def _import_model(self):
        """Open a file dialog to import a new .k model into the viewer."""
        start_dir = str(self._file_path.parent) if self._file_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Import .k Model",
            start_dir,
            "LS-DYNA Files (*.k *.key *.dyn);;All Files (*)",
        )
        if not path:
            return

        new_path = Path(path)

        # Clear existing model and show loading state
        if self.plotter is not None:
            self.plotter.clear()
            self.plotter.setVisible(False)
        self._actors.clear()
        self._part_actors.clear()
        self._part_polydata.clear()
        self._feature_edges_actor = None
        self._loading_label.setText(f"Loading {new_path.name}\u2026")
        self._loading_widget.setVisible(True)
        self._status.showMessage("Parsing model\u2026")

        # Background parse
        if self._load_worker is not None:
            self._load_worker.finished.disconnect()
            self._load_worker.quit()
            self._load_worker.wait(2000)
        self._file_path = new_path
        self._load_worker = _LoadWorker(new_path)
        self._load_worker.finished.connect(self._on_import_loaded)
        self._load_worker.start()

    def _on_import_loaded(self, polydata, part_names, kw_entities, error):
        """Slot for background import completion."""
        self._load_worker = None
        if error:
            self._hide_loading()
            if self.plotter is not None:
                self.plotter.setVisible(True)
            self._status.showMessage(f"Error: {error}")
            QMessageBox.critical(self, "Model error", error)
            return

        # Replace current model
        self._polydata = polydata
        new_path = self._file_path  # set by load_file_async or below
        self._part_names = part_names
        self._keyword_entities = kw_entities

        has_parts = "part_ids" in polydata.cell_data
        self._n_parts = len(set(polydata.cell_data["part_ids"])) if has_parts else 0
        self._has_parts = has_parts

        self._hide_loading()
        self._ensure_plotter()
        self.plotter.setVisible(True)
        self.plotter.clear()
        self._remove_all_entity_actors()
        self._remove_highlight_edges()
        self.plotter.enable_lightkit()
        self._add_model_mesh()
        # Re-apply mesh toggle state after reload
        if not self._mesh_visible:
            style = self._current_style
            if style in ("Wireframe", "Points"):
                for actor in self._actors:
                    if actor is not None:
                        actor.SetVisibility(False)
                fe_style = "points" if style == "Points" else "wireframe"
            else:
                for actor in self._actors:
                    if actor is not None:
                        actor.GetProperty().EdgeVisibilityOff()
                fe_style = "wireframe"
            self._add_feature_edges(render_style=fe_style)
        self._rebuild_part_tree()
        self._update_status_info()
        self.setWindowTitle(f" 3D Model Viewer \u2014 {self._file_path.name}")
        self._status.showMessage(f"Loaded: {self._file_path.name}", 5000)

    def _build_floating_toolbar(self):
        """Draggable floating toolbar: Axes, Grid, Style dropdown, View dropdown."""
        icons_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "resources", "icons",
        )

        # Use a _DraggableToolBar so it can be repositioned freely
        tb = _DraggableToolBar("Viewer Tools", parent=self)
        tb.setIconSize(QSize(32, 32))
        tb.setStyleSheet("""
            QToolBar {
                background-color: #f9f9f9;
                border: 1px solid #d0d0d0;
                border-radius: 6px;
                spacing: 4px;
                padding: 2px 6px;
            }
            QToolButton {
                background: transparent;
                border: none;
                border-radius: 4px;
                padding: 0px;
                min-width: 36px;
                min-height: 36px;
            }
            QToolButton:hover { background-color: #e8e8e8; }
            QToolButton:pressed { background-color: #d8d8d8; border-style: inset; }
            QToolButton:checked {
                background-color: #d8d8d8;
            }
            QToolButton::menu-indicator {
                subcontrol-position: right center;
                subcontrol-origin: padding;
                width: 12px;
                left: -4px;
            }
            QToolButton[popupMode="1"] {
                padding-right: 16px;
            }
        """)

        # ── Part tree toggle (first button) ────────────────────────
        tree_ico = os.path.join(icons_dir, "tree_model_viewer3.ico")
        if os.path.exists(tree_ico):
            _px = QPixmap(tree_ico).scaled(
                32, 32, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            tree_icon = QIcon(_px)
        else:
            tree_icon = _icon_from_text("Tr")
        self._ft_tree = QAction(tree_icon, "Part Tree", self)
        self._ft_tree.setCheckable(True)
        self._ft_tree.setChecked(True)
        self._ft_tree.setToolTip("Show / hide the assembly part tree")
        self._ft_tree.toggled.connect(self._toggle_part_tree)
        tb.addAction(self._ft_tree)

        tb.addSeparator()

        # ── Axes toggle ──────────────────────────────────────────────
        axes_ico = os.path.join(icons_dir, "axes_MV.ico")
        axes_icon = QIcon(axes_ico) if os.path.exists(axes_ico) else _icon_from_text("Ax")
        self._ft_axes = QAction(axes_icon, "Axes", self)
        self._ft_axes.setCheckable(False)
        self._ft_axes.setToolTip("Toggle orientation axes widget")
        self._ft_axes.triggered.connect(self._ft_toggle_axes)
        tb.addAction(self._ft_axes)

        # ── Grid toggle ──────────────────────────────────────────────
        grid_ico = os.path.join(icons_dir, "ruler_MV.ico")
        grid_icon = QIcon(grid_ico) if os.path.exists(grid_ico) else _icon_from_text("Gr")
        self._ft_grid = QAction(grid_icon, "Grid", self)
        self._ft_grid.setCheckable(True)
        self._ft_grid.setChecked(False)
        self._ft_grid.setToolTip("Toggle bounding grid with coordinates")
        self._ft_grid.toggled.connect(self._toggle_bounds)
        tb.addAction(self._ft_grid)

        tb.addSeparator()

        # ── Style dropdown ───────────────────────────────────────────
        _menu_icon_css = "QMenu { icon-size: 32px; }"
        style_menu = QMenu(self)
        style_menu.setStyle(_large_icon_style)
        styles = [
            ("Surface + Edges", "surface_wire_MV.ico"),
            ("Surface",         "surface_MV.ico"),
            ("Wireframe",       "wire_MV.ico"),
            ("Points",          "points_MV.ico"),
        ]

        self._style_btn = QToolButton(self)
        self._style_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._style_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._style_btn.setMinimumWidth(56)

        default_style_icon = None
        for label, ico_name in styles:
            ico_path = os.path.join(icons_dir, ico_name)
            icon = QIcon(ico_path) if os.path.exists(ico_path) else _icon_from_text(label[:2])
            if default_style_icon is None:
                default_style_icon = icon
            act = style_menu.addAction(icon, label)
            act.setToolTip(f"Render style: {label}")
            act.triggered.connect(self._make_style_callback(label, icon))

        self._style_btn.setMenu(style_menu)
        if default_style_icon:
            self._style_btn.setIcon(default_style_icon)
        self._style_btn.setToolTip("Select render style")
        # Click the button itself → apply current style (Surface + Edges)
        self._style_btn.clicked.connect(lambda: self._set_render_style("Surface + Edges"))
        tb.addWidget(self._style_btn)

        tb.addSeparator()

        # ── View dropdown ────────────────────────────────────────────
        view_menu = QMenu(self)
        view_menu.setStyle(_large_icon_style)
        views = [
            ("Front",  "front_view_MV.ico",  "Front view (–Y)",   lambda: self.plotter.view_vector((0, 1, 0), (0, 0, 1))),
            ("Back",   "back_view_MV.ico",   "Back view (+Y)",    lambda: self.plotter.view_vector((0, -1, 0), (0, 0, 1))),
            ("Left",   "left_view_MV.ico",   "Left view (–X)",    lambda: self.plotter.view_vector((1, 0, 0), (0, 0, 1))),
            ("Right",  "right_view_MV.ico",  "Right view (+X)",   lambda: self.plotter.view_vector((-1, 0, 0), (0, 0, 1))),
            ("Top",    "top_view_MV.ico",    "Top view (–Z)",     lambda: self.plotter.view_vector((0, 0, 1), (0, 1, 0))),
            ("Bottom", "bottom_view_MV.ico", "Bottom view (+Z)",  lambda: self.plotter.view_vector((0, 0, -1), (0, 1, 0))),
            ("Iso",    "iso_view_MV.ico",    "Isometric view",    lambda: self.plotter.view_isometric()),
        ]

        self._ft_view_btn = QToolButton(self)
        self._ft_view_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._ft_view_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._ft_view_btn.setMinimumWidth(56)

        default_view_icon = None
        default_view_func = None
        for label, ico_name, tooltip, func in views:
            ico_path = os.path.join(icons_dir, ico_name)
            icon = QIcon(ico_path) if os.path.exists(ico_path) else _icon_from_text(label[:2])
            if label == "Iso":
                default_view_icon = icon
                default_view_func = func
            act = view_menu.addAction(icon, label)
            act.setToolTip(tooltip)
            act.triggered.connect(self._make_ft_view_callback(func, icon, label))

        self._ft_view_btn.setMenu(view_menu)
        if default_view_icon:
            self._ft_view_btn.setIcon(default_view_icon)
        self._ft_view_btn.setToolTip("Select camera view direction")
        self._ft_view_btn.clicked.connect(lambda: self.plotter.view_isometric())
        tb.addWidget(self._ft_view_btn)

        tb.addSeparator()

        # ── Zoom dropdown ────────────────────────────────────────────
        zoom_menu = QMenu(self)
        zoom_menu.setStyle(_large_icon_style)
        zoom_items = [
            ("Zoom In",  "zoom_in_MV.ico",  "Zoom in",  lambda: (self.plotter.camera.Zoom(1.3), self.plotter.render())),
            ("Zoom Out", "zoom_out_MV.ico", "Zoom out", lambda: (self.plotter.camera.Zoom(0.7), self.plotter.render())),
        ]

        self._ft_zoom_btn = QToolButton(self)
        self._ft_zoom_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._ft_zoom_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._ft_zoom_btn.setMinimumWidth(56)

        default_zoom_icon = None
        for label, ico_name, tooltip, func in zoom_items:
            ico_path = os.path.join(icons_dir, ico_name)
            icon = QIcon(ico_path) if os.path.exists(ico_path) else _icon_from_text(label[:2])
            if default_zoom_icon is None:
                default_zoom_icon = icon
            act = zoom_menu.addAction(icon, label)
            act.setToolTip(tooltip)
            act.triggered.connect(self._make_ft_zoom_callback(func, icon, label))

        self._ft_zoom_btn.setMenu(zoom_menu)
        if default_zoom_icon:
            self._ft_zoom_btn.setIcon(default_zoom_icon)
        self._ft_zoom_btn.setToolTip("Zoom in / out")
        self._ft_zoom_btn.clicked.connect(lambda: (self.plotter.camera.Zoom(1.3), self.plotter.render()))
        tb.addWidget(self._ft_zoom_btn)

        # ── Fit / Reset Camera ───────────────────────────────────────
        fit_ico = os.path.join(icons_dir, "fit_MV.ico")
        fit_icon = QIcon(fit_ico) if os.path.exists(fit_ico) else _icon_from_text("Fit")
        self._ft_fit = QAction(fit_icon, "Reset Camera", self)
        self._ft_fit.setToolTip("Reset camera to fit entire model")
        self._ft_fit.triggered.connect(lambda: self.plotter.reset_camera())
        tb.addAction(self._ft_fit)

        # ── Parallel / Perspective toggle ────────────────────────────
        persp_ico = os.path.join(icons_dir, "perspective_MV.ico")
        ortho_ico = os.path.join(icons_dir, "orthogonal_MV.ico")
        self._persp_icon = QIcon(persp_ico) if os.path.exists(persp_ico) else _icon_from_text("P")
        self._ortho_icon = QIcon(ortho_ico) if os.path.exists(ortho_ico) else _icon_from_text("O")
        self._ft_proj = QAction(self._persp_icon, "Parallel / Perspective", self)
        self._ft_proj.setCheckable(True)
        self._ft_proj.setChecked(False)
        self._ft_proj.setToolTip("Toggle parallel / perspective projection")
        self._ft_proj.toggled.connect(self._ft_toggle_projection)
        tb.addAction(self._ft_proj)

        tb.addSeparator()

        # ── Background dropdown ──────────────────────────────────────
        bg_menu = QMenu(self)
        bg_menu.setStyle(_large_icon_style)
        backgrounds = [
            ("White",            "backgroun_white_MV.ico",    "white"),
            ("Light Gray",       "backgroun_grey_MV.ico",   "lightgray"),
            ("Dark Gray",        "backgroun_dark_grey_MV.ico" ,     "#3c3c3c"),
            ("Black",            "backgroun_black_MV.ico",    "black"),
            ("Gradient (blue)",  "backgroun_gradient_MV.ico", "__gradient_blue"),
            ("Gradient (gray)",  "backgroun_gradient_grey_MV.ico", "__gradient_gray"),
        ]

        self._ft_bg_btn = QToolButton(self)
        self._ft_bg_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._ft_bg_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._ft_bg_btn.setMinimumWidth(56)

        default_bg_icon = None
        for label, ico_name, bg_key in backgrounds:
            if ico_name:
                ico_path = os.path.join(icons_dir, ico_name)
                icon = QIcon(ico_path) if os.path.exists(ico_path) else _icon_from_text(label[:2])
            else:
                icon = _icon_from_text(label[:2])
            if default_bg_icon is None:
                default_bg_icon = icon
            act = bg_menu.addAction(icon, label)
            act.setToolTip(f"Background: {label}")
            act.triggered.connect(self._make_ft_bg_callback(label, bg_key, icon))

        self._ft_bg_btn.setMenu(bg_menu)
        if default_bg_icon:
            self._ft_bg_btn.setIcon(default_bg_icon)
        self._ft_bg_btn.setToolTip("Background color")
        self._ft_bg_btn.clicked.connect(lambda: self._set_background("White"))
        tb.addWidget(self._ft_bg_btn)

        tb.addSeparator()

        # ── Opacity combo ────────────────────────────────────────────
        lbl_op = QLabel(" Opacity: ")
        lbl_op.setStyleSheet("font-weight:bold; font-size:11px;")
        tb.addWidget(lbl_op)
        self._ft_opacity_combo = QComboBox()
        self._ft_opacity_combo.addItems(["100%", "90%", "75%", "50%", "25%"])
        self._ft_opacity_combo.setCurrentIndex(0)
        self._ft_opacity_combo.setToolTip("Mesh opacity")
        self._ft_opacity_combo.setFixedWidth(70)
        self._ft_opacity_combo.currentTextChanged.connect(self._set_opacity)
        tb.addWidget(self._ft_opacity_combo)

        tb.addSeparator()

        # ── Mesh toggle (show/hide internal wireframe) ─────────────
        mesh_ico = os.path.join(icons_dir, "mesh_MV.ico")
        mesh_icon = QIcon(mesh_ico) if os.path.exists(mesh_ico) else _icon_from_text("Me")
        self._ft_mesh = QAction(mesh_icon, "Mesh", self)
        self._ft_mesh.setCheckable(True)
        self._ft_mesh.setChecked(True)
        self._ft_mesh.setToolTip("Toggle mesh wireframe (uncheck to show feature edges only)")
        self._ft_mesh.toggled.connect(self._toggle_mesh)
        tb.addAction(self._ft_mesh)

        tb.addSeparator()

        # ── Visual effects dropdown ────────────────────────────────────
        fx_menu = QMenu(self)
        fx_menu.setStyle(_large_icon_style)

        self._fx_actions: dict[str, QAction] = {}
        effects = [
            ("Silhouette",       "Si", "Add silhouette contour around model"),
            ("PBR (Metallic)",   "PB", "Physically-based metallic rendering"),
            ("Smooth Shading",   "Sm", "Enable smooth (Phong) shading"),
            ("Anti-aliasing",    "AA", "Multi-sample anti-aliasing (smooth edges)"),
            ("Depth Peeling",    "Dp", "Accurate transparency ordering (depth peeling)"),
        ]
        for label, abbr, tooltip in effects:
            ico_path = os.path.join(icons_dir, f"{label.lower().replace(' ', '_').replace('(', '').replace(')', '')}_MV.ico")
            icon = QIcon(ico_path) if os.path.exists(ico_path) else _icon_from_text(abbr)
            act = fx_menu.addAction(icon, label)
            act.setCheckable(True)
            act.setChecked(False)
            act.setToolTip(tooltip)
            act.toggled.connect(self._make_fx_callback(label))
            self._fx_actions[label] = act

        self._ft_fx_btn = QToolButton(self)
        self._ft_fx_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._ft_fx_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._ft_fx_btn.setMinimumWidth(56)
        fx_ico = os.path.join(icons_dir, "fx.ico")
        fx_icon = QIcon(fx_ico) if os.path.exists(fx_ico) else _icon_from_text("Fx")
        self._ft_fx_btn.setIcon(fx_icon)
        self._ft_fx_btn.setMenu(fx_menu)
        self._ft_fx_btn.setToolTip("Visual effects")
        self._ft_fx_btn.clicked.connect(lambda: self._ft_fx_btn.showMenu())
        tb.addWidget(self._ft_fx_btn)

        tb.addSeparator()

        # ── Screenshot button ────────────────────────────────────────
        ss_ico = os.path.join(icons_dir, "imagen.ico")
        screenshot_icon = QIcon(ss_ico) if os.path.exists(ss_ico) else _icon_from_text("Sc")
        self._ft_screenshot = QAction(screenshot_icon, "Screenshot", self)
        self._ft_screenshot.setToolTip("Save current view as PNG image")
        self._ft_screenshot.triggered.connect(self._screenshot)
        tb.addAction(self._ft_screenshot)

        # Don't add to layout — float over the plotter viewport
        self._floating_tb = tb
        tb.adjustSize()
        tb.raise_()

    def _make_ft_view_callback(self, view_func, icon, label):
        """Return a slot that applies a camera view and updates the view dropdown."""
        def _callback():
            view_func()
            self._ft_view_btn.setIcon(icon)
            try:
                self._ft_view_btn.clicked.disconnect()
            except RuntimeError:
                pass
            self._ft_view_btn.clicked.connect(lambda: view_func())
            self._status.showMessage(f"View: {label}", 3000)
        return _callback

    def _make_ft_zoom_callback(self, zoom_func, icon, label):
        """Return a slot that executes *zoom_func* and updates the zoom dropdown."""
        def _callback():
            zoom_func()
            self._ft_zoom_btn.setIcon(icon)
            try:
                self._ft_zoom_btn.clicked.disconnect()
            except RuntimeError:
                pass
            self._ft_zoom_btn.clicked.connect(lambda: zoom_func())
            self._status.showMessage(f"{label}", 2000)
        return _callback

    def _ft_toggle_axes(self):
        """Toggle axes visibility via a non-checkable button click."""
        self._axes_visible = not self._axes_visible
        self._toggle_axes(self._axes_visible)

    def _ft_toggle_projection(self, checked: bool):
        """Toggle parallel/perspective and swap icon accordingly."""
        self._toggle_projection(checked)
        if checked:
            self._ft_proj.setIcon(self._ortho_icon)
            self._status.showMessage("Projection: Parallel (orthogonal)", 3000)
        else:
            self._ft_proj.setIcon(self._persp_icon)
            self._status.showMessage("Projection: Perspective", 3000)

    def _make_ft_bg_callback(self, label: str, bg_key: str, icon: QIcon):
        """Return a slot that sets the background and updates the dropdown icon."""
        def _callback():
            if bg_key == "__gradient_blue":
                self.plotter.set_background("royalblue", top="aliceblue")
            elif bg_key == "__gradient_gray":
                self.plotter.set_background("#3c3c3c", top="#888888")
            else:
                self.plotter.set_background(bg_key)
            self._current_bg = bg_key
            # Update feature-edges color for black background
            if self._feature_edges_actor is not None:
                fe_color = "white" if bg_key == "black" else "black"
                self._feature_edges_actor.GetProperty().SetColor(
                    *((1.0, 1.0, 1.0) if fe_color == "white" else (0.0, 0.0, 0.0))
                )
            self.plotter.render()
            self._ft_bg_btn.setIcon(icon)
            try:
                self._ft_bg_btn.clicked.disconnect()
            except RuntimeError:
                pass
            self._ft_bg_btn.clicked.connect(lambda: _callback())
            self._status.showMessage(f"Background: {label}", 3000)
        return _callback

    def _make_style_callback(self, style_name: str, icon: QIcon):
        """Return a slot that applies *style_name* and updates the dropdown icon."""
        def _callback():
            self._set_render_style(style_name)
            self._style_btn.setIcon(icon)
            try:
                self._style_btn.clicked.disconnect()
            except RuntimeError:
                pass
            self._style_btn.clicked.connect(lambda: self._set_render_style(style_name))
        return _callback

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
                    for actor in self._actors:
                        self._apply_fx_to_actor(actor, name, on)

            self.plotter.render()
            state = "ON" if on else "OFF"
            self._status.showMessage(f"{name}: {state}", 3000)
        except Exception as exc:
            logger.warning("Visual effect '%s' failed: %s", name, exc)
            self._status.showMessage(f"{name}: not available", 3000)

    # ------------------------------------------------------------------
    #  Part tree (assembly tree)
    # ------------------------------------------------------------------

    def _build_part_tree(self):
        """Create the QTreeWidget panel overlaying the left side of the plotter."""
        tree = QTreeWidget(self)
        tree.setHeaderHidden(True)
        tree.setColumnCount(1)
        tree.setStyleSheet(
            "QTreeWidget { font-size: 11px; }"
        )
        tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        tree.itemChanged.connect(self._on_part_checked)
        tree.itemSelectionChanged.connect(self._on_part_selection_changed)
        tree.setVisible(False)          # hidden until a multi-part model loads
        self._part_tree = tree
        self._highlighted_pids: set = set()         # pids currently highlighted
        self._highlight_fe_actors: dict = {}        # pid → feature-edge overlay actor
        self._per_part_style: dict = {}             # pid → render style override
        self._per_part_fx: dict = {}                # pid → {fx_name: bool, ...}
        self._per_part_mesh: dict = {}              # pid → bool (mesh on/off override)

    def _rebuild_part_tree(self):
        """Populate the tree widget from current ``_part_names`` / ``_part_actors``."""
        tree = self._part_tree
        tree.blockSignals(True)
        tree.clear()

        has_parts = bool(self._part_actors)
        has_entities = any(k != "__names__" for k in self._keyword_entities)

        if not has_parts and not has_entities:
            tree.setVisible(False)
            tree.blockSignals(False)
            return

        # Root ► FEM Parts
        root_item = QTreeWidgetItem(tree, ["Assembly"])
        root_item.setFlags(root_item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
        root_item.setCheckState(0, Qt.CheckState.Checked)

        if has_parts:
            fem_item = QTreeWidgetItem(root_item, ["FEM Parts"])
            fem_item.setFlags(fem_item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
            fem_item.setCheckState(0, Qt.CheckState.Checked)

            for pid in sorted(self._part_actors.keys()):
                name = self._part_names.get(pid, f"Part {pid}")
                label = f"{pid}: {name}"
                item = QTreeWidgetItem(fem_item, [label])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked)
                item.setData(0, Qt.ItemDataRole.UserRole, pid)   # store pid

        # ── Keyword Entities section ──────────────────────────────────
        if has_entities:
            # Display order for categories
            _CAT_ORDER = ["SET_NODE", "SET_SHELL", "SET_SOLID", "SET_PART", "SET_SEGMENT"]
            _CAT_LABELS = {
                "SET_NODE": "Set Node",
                "SET_SHELL": "Set Shell",
                "SET_SOLID": "Set Solid",
                "SET_PART": "Set Part",
                "SET_SEGMENT": "Set Segment",
            }
            _MEMBER_LABELS = {
                "SET_NODE": "nodes",
                "SET_SHELL": "shells",
                "SET_SOLID": "solids",
                "SET_PART": "parts",
                "SET_SEGMENT": "segments",
            }

            entities_item = QTreeWidgetItem(root_item, ["Keyword Entities"])
            entities_item.setFlags(
                entities_item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsAutoTristate
            )
            entities_item.setCheckState(0, Qt.CheckState.Unchecked)

            for cat in _CAT_ORDER:
                sids = self._keyword_entities.get(cat)
                if not sids:
                    continue
                cat_label = _CAT_LABELS.get(cat, cat)
                cat_item = QTreeWidgetItem(entities_item, [cat_label])
                cat_item.setFlags(
                    cat_item.flags()
                    | Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsAutoTristate
                )
                cat_item.setCheckState(0, Qt.CheckState.Unchecked)

                member_label = _MEMBER_LABELS.get(cat, "members")
                entity_names = self._keyword_entities.get("__names__", {})
                for sid in sorted(sids.keys()):
                    members = sids[sid]
                    name = entity_names.get((cat, sid), "")
                    if name:
                        label = f"SID {sid}: {name}  ({len(members)} {member_label})"
                    else:
                        label = f"SID {sid}  ({len(members)} {member_label})"
                    item = QTreeWidgetItem(cat_item, [label])
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(0, Qt.CheckState.Unchecked)
                    # Store entity identity: ("SET_NODE", 1)
                    item.setData(0, Qt.ItemDataRole.UserRole, (cat, sid))

        tree.expandAll()
        # Auto-size column to fit the longest label
        tree.resizeColumnToContents(0)
        content_w = tree.columnWidth(0) + 40   # +indentation + checkbox + margin
        tree.setFixedWidth(max(content_w, 160))
        # Respect the toolbar toggle state
        show = self._ft_tree.isChecked() if hasattr(self, '_ft_tree') else True
        tree.setVisible(show)
        tree.blockSignals(False)
        if show:
            self._reposition_part_tree()

    def _reposition_part_tree(self):
        """Position the tree widget at a fixed place below the toolbar's initial position."""
        if not hasattr(self, '_part_tree') or not self._part_tree.isVisible():
            return
        tree = self._part_tree
        margin = 6

        # Compute fixed top_y once and reuse it forever
        if not hasattr(self, '_tree_fixed_top_y'):
            tb = self._floating_tb if hasattr(self, '_floating_tb') else None
            if tb is not None and tb.isVisible() and tb.height() > 0:
                self._tree_fixed_top_y = tb.y() + tb.height() + 12
            else:
                # fallback: plotter top + estimated toolbar height
                if self.plotter is not None:
                    plotter_top = self.plotter.mapTo(self, self.plotter.rect().topLeft()).y()
                else:
                    plotter_top = self._menu_bar.height() if self._menu_bar else 0
                self._tree_fixed_top_y = plotter_top + 60  # ~toolbar height + gap

        top_y = self._tree_fixed_top_y
        status_h = self._status.height() if self._status else 0
        avail_h = self.height() - top_y - status_h - margin
        tree_h = min(avail_h, tree.sizeHint().height() + 40)
        tree_h = max(tree_h, 120)
        tree.setGeometry(margin, top_y, tree.width(), tree_h)
        tree.raise_()

    def resizeEvent(self, event):
        """Keep the part tree positioned correctly on resize."""
        super().resizeEvent(event)
        self._reposition_part_tree()

    def _sync_part_visibility(self):
        """Walk tree items and set actor visibility to match check states."""
        if not hasattr(self, '_part_tree'):
            return
        tree = self._part_tree
        if not tree.isVisible():
            return
        root = tree.topLevelItem(0)      # Assembly
        if root is None:
            return
        # Find FEM Parts child by text (not by index, since Keyword Entities
        # may also be present)
        fem = None
        for ci in range(root.childCount()):
            if root.child(ci).text(0) == "FEM Parts":
                fem = root.child(ci)
                break
        if fem is None:
            return
        for i in range(fem.childCount()):
            item = fem.child(i)
            pid = item.data(0, Qt.ItemDataRole.UserRole)
            if pid is None:
                continue
            actor = self._part_actors.get(pid)
            if actor is not None:
                visible = item.checkState(0) == Qt.CheckState.Checked
                actor.SetVisibility(visible)

    def _on_part_checked(self, item, column):
        """Callback when a tree checkbox is toggled."""
        data = item.data(0, Qt.ItemDataRole.UserRole)

        if isinstance(data, tuple) and len(data) == 2:
            # Keyword entity item: (category, sid)
            cat, sid = data
            checked = item.checkState(0) == Qt.CheckState.Checked
            self._toggle_entity_visibility(cat, sid, checked)
        elif isinstance(data, int):
            # FEM Part leaf item — toggle single actor
            pid = data
            actor = self._part_actors.get(pid)
            if actor is not None:
                visible = item.checkState(0) == Qt.CheckState.Checked
                actor.SetVisibility(visible)
        else:
            # Parent item (Assembly, FEM Parts, Keyword Entities, etc.)
            self._sync_part_visibility()
            self._sync_entity_visibility()
        self.plotter.render()

    def _toggle_part_tree(self, checked: bool):
        """Show or hide the assembly part tree panel."""
        if not hasattr(self, '_part_tree'):
            return
        if checked:
            self._part_tree.setVisible(True)
            self._reposition_part_tree()
        else:
            self._part_tree.setVisible(False)

    def _get_selected_pids(self) -> list[int]:
        """Return the list of pids currently selected in the part tree."""
        pids = []
        for item in self._part_tree.selectedItems():
            pid = item.data(0, Qt.ItemDataRole.UserRole)
            if pid is not None and not isinstance(pid, (tuple, list)):
                pids.append(pid)
        return pids

    def _get_selected_actors(self) -> list:
        """Return actors for the currently selected pids (or all if none selected)."""
        pids = self._get_selected_pids()
        if pids:
            return [self._part_actors[p] for p in pids if p in self._part_actors]
        return list(self._actors)  # fallback: all

    def _on_part_selection_changed(self):
        """Highlight all selected parts with orange feature edges."""
        selected_pids = set(self._get_selected_pids())

        # Remove highlights for deselected parts
        for pid in list(self._highlighted_pids - selected_pids):
            actor = self._highlight_fe_actors.pop(pid, None)
            if actor is not None:
                try:
                    self.plotter.remove_actor(actor)
                except Exception:
                    pass
            self._highlighted_pids.discard(pid)

        # Add highlights for newly selected parts
        for pid in selected_pids - self._highlighted_pids:
            if pid in self._part_polydata:
                try:
                    sub = self._part_polydata[pid]
                    edges = sub.extract_feature_edges(
                        boundary_edges=True, feature_edges=True,
                        manifold_edges=False, non_manifold_edges=True,
                        feature_angle=30,
                    )
                    if edges.n_points > 0:
                        actor = self.plotter.add_mesh(
                            edges,
                            color="orange",
                            style="wireframe",
                            line_width=4.0,
                            label=f"_highlight_{pid}",
                        )
                        self._highlight_fe_actors[pid] = actor
                        self._highlighted_pids.add(pid)
                except Exception as exc:
                    logger.debug("Highlight edges for pid %s failed: %s", pid, exc)

        self.plotter.render()

    def _remove_highlight_edges(self):
        """Remove all highlight feature-edge overlays."""
        for actor in list(self._highlight_fe_actors.values()):
            try:
                self.plotter.remove_actor(actor)
            except Exception:
                pass
        self._highlight_fe_actors.clear()
        self._highlighted_pids.clear()

    # ------------------------------------------------------------------
    #  Keyword Entity visualisation
    # ------------------------------------------------------------------

    def _entity_actor_key(self, cat: str, sid: int) -> str:
        return f"{cat}_{sid}"

    def _toggle_entity_visibility(self, cat: str, sid: int, visible: bool):
        """Show or hide a keyword entity overlay on the 3D model."""
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
        """Create a highlight actor for the given entity and add to plotter."""
        import numpy as np

        members = self._keyword_entities.get(cat, {}).get(sid)
        if not members or self._polydata is None:
            return

        pd = self._polydata
        key = self._entity_actor_key(cat, sid)

        try:
            if cat == "SET_NODE":
                # Highlight nodes as red spheres
                nids = pd.point_data.get("node_ids")
                if nids is None:
                    return
                member_set = set(members)
                mask = np.isin(nids, list(member_set))
                pts = pd.points[mask]
                if len(pts) == 0:
                    return
                import pyvista as pv
                cloud = pv.PolyData(pts)
                actor = self.plotter.add_mesh(
                    cloud,
                    color="red",
                    point_size=8,
                    render_points_as_spheres=True,
                    label=f"_entity_{key}",
                )
                self._entity_actors[key] = actor

            elif cat in ("SET_SHELL", "SET_SOLID"):
                # Highlight elements by extracting matching cells
                elem_ids = pd.cell_data.get("element_ids")
                if elem_ids is None:
                    return
                member_set = set(members)
                mask = np.isin(elem_ids, list(member_set))
                cell_indices = np.where(mask)[0]
                if len(cell_indices) == 0:
                    return
                sub = pd.extract_cells(cell_indices)
                actor = self.plotter.add_mesh(
                    sub,
                    color="red",
                    opacity=0.6,
                    show_edges=True,
                    edge_color="darkred",
                    line_width=1.5,
                    label=f"_entity_{key}",
                )
                self._entity_actors[key] = actor

            elif cat == "SET_PART":
                # Highlight entire parts by part_ids
                part_ids_arr = pd.cell_data.get("part_ids")
                if part_ids_arr is None:
                    return
                member_set = set(members)
                mask = np.isin(part_ids_arr, list(member_set))
                cell_indices = np.where(mask)[0]
                if len(cell_indices) == 0:
                    return
                sub = pd.extract_cells(cell_indices)
                actor = self.plotter.add_mesh(
                    sub,
                    color="red",
                    opacity=0.6,
                    show_edges=True,
                    edge_color="darkred",
                    line_width=1.5,
                    label=f"_entity_{key}",
                )
                self._entity_actors[key] = actor

            elif cat == "SET_SEGMENT":
                # Highlight segments as faces built from node quads/tris
                nids = pd.point_data.get("node_ids")
                if nids is None:
                    return
                id_to_idx = {int(nid): idx for idx, nid in enumerate(nids)}
                import pyvista as pv
                faces = []
                for seg in members:
                    indices = [id_to_idx.get(nid) for nid in seg]
                    indices = [i for i in indices if i is not None]
                    if len(indices) >= 3:
                        faces.append(len(indices))
                        faces.extend(indices)
                if not faces:
                    return
                seg_pd = pv.PolyData(pd.points, faces=faces)
                actor = self.plotter.add_mesh(
                    seg_pd,
                    color="red",
                    opacity=0.6,
                    show_edges=True,
                    edge_color="darkred",
                    line_width=2.0,
                    label=f"_entity_{key}",
                )
                self._entity_actors[key] = actor

        except Exception as exc:
            logger.debug("Failed to add entity actor %s: %s", key, exc)

    def _sync_entity_visibility(self):
        """Walk entity tree items and sync their actor visibility."""
        tree = self._part_tree
        root = tree.topLevelItem(0)
        if root is None:
            return
        # Find "Keyword Entities" child
        for ci in range(root.childCount()):
            group = root.child(ci)
            if group.text(0) == "Keyword Entities":
                self._sync_entity_group(group)
                break

    def _sync_entity_group(self, group_item):
        """Recursively sync entity actors for all children."""
        for ci in range(group_item.childCount()):
            child = group_item.child(ci)
            data = child.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(data, tuple) and len(data) == 2:
                cat, sid = data
                checked = child.checkState(0) == Qt.CheckState.Checked
                self._toggle_entity_visibility(cat, sid, checked)
            else:
                # Category group — recurse
                self._sync_entity_group(child)

    def _remove_all_entity_actors(self):
        """Remove all keyword entity overlay actors."""
        for actor in list(self._entity_actors.values()):
            try:
                self.plotter.remove_actor(actor)
            except Exception:
                pass
        self._entity_actors.clear()

    # ------------------------------------------------------------------
    #  Mesh management
    # ------------------------------------------------------------------

    def _get_part_colors(self) -> dict:
        """Return a ``{pid: [r,g,b]}`` mapping using the active colormap."""
        try:
            from matplotlib import colormaps
            cmap_name = self._cmap_combo.currentText() if hasattr(self, '_cmap_combo') else "tab20"
            cmap = colormaps.get_cmap(cmap_name)
        except Exception:
            cmap = None

        part_ids = sorted(set(self._polydata.cell_data["part_ids"])) if self._has_parts else []
        colors: dict[int, object] = {}
        for idx, pid in enumerate(part_ids):
            if cmap is not None:
                rgba = cmap(idx % cmap.N if hasattr(cmap, 'N') else idx / max(len(part_ids), 1))
                colors[pid] = [rgba[0], rgba[1], rgba[2]]
            else:
                colors[pid] = "steelblue"
        return colors

    def _add_model_mesh(self):
        """Add the polydata to the plotter with per-part actors when possible."""
        import numpy as np
        pd = self._polydata
        self._part_actors.clear()
        self._part_polydata.clear()

        if self._has_parts:
            part_ids_arr = pd.cell_data["part_ids"]
            unique_pids = sorted(set(part_ids_arr))
            colors = self._get_part_colors()

            for pid in unique_pids:
                mask = part_ids_arr == pid
                sub = pd.extract_cells(np.where(mask)[0])
                if sub.n_cells == 0:
                    continue
                self._part_polydata[pid] = sub
                name = self._part_names.get(pid, f"Part {pid}")
                actor = self.plotter.add_mesh(
                    sub,
                    color=colors.get(pid, "steelblue"),
                    show_edges=True,
                    edge_color="gray",
                    line_width=0.5,
                    label=f"{pid} {name}",
                )
                self._actors.append(actor)
                self._part_actors[pid] = actor
        else:
            actor = self.plotter.add_mesh(
                pd,
                show_edges=True,
                color="steelblue",
                opacity=1.0,
                edge_color="gray",
                line_width=0.5,
            )
            self._actors.append(actor)

        self.plotter.add_axes()
        self.plotter.set_background("white")
        self.plotter.reset_camera()
        self._rebuild_part_tree()

    def _rebuild_mesh(self, **kw):
        """Clear and re-add the mesh with current settings.

        Used when changing colormaps or other properties that require
        full re-add.
        """
        import numpy as np
        self.plotter.clear()
        self._actors.clear()
        self._part_actors.clear()
        self._part_polydata.clear()
        self._per_part_style.clear()
        self._per_part_fx.clear()
        self._per_part_mesh.clear()
        self._remove_highlight_edges()
        self._remove_all_entity_actors()
        self.plotter.enable_lightkit()   # restore default lighting after clear

        pd = self._polydata
        defaults = dict(
            show_edges=self._edges_visible,
            edge_color="gray",
            line_width=0.5,
        )
        defaults.update(kw)

        if self._has_parts:
            part_ids_arr = pd.cell_data["part_ids"]
            unique_pids = sorted(set(part_ids_arr))
            colors = self._get_part_colors()

            for pid in unique_pids:
                mask = part_ids_arr == pid
                sub = pd.extract_cells(np.where(mask)[0])
                if sub.n_cells == 0:
                    continue
                self._part_polydata[pid] = sub
                name = self._part_names.get(pid, f"Part {pid}")
                actor = self.plotter.add_mesh(
                    sub,
                    color=colors.get(pid, "steelblue"),
                    label=f"{pid} {name}",
                    **defaults,
                )
                self._actors.append(actor)
                self._part_actors[pid] = actor
        else:
            actor = self.plotter.add_mesh(
                pd,
                color="steelblue",
                opacity=1.0,
                **defaults,
            )
            self._actors.append(actor)

        self.plotter.add_axes()
        if self._axes_visible:
            self.plotter.show_axes()
        else:
            self.plotter.hide_axes()
        if self._bounds_visible:
            self.plotter.show_bounds(grid="back", location="outer")
        # Re-apply mesh toggle state after rebuild
        if not self._mesh_visible:
            style = self._current_style
            if style in ("Wireframe", "Points"):
                for a in self._actors:
                    if a is not None:
                        a.SetVisibility(False)
                fe_style = "points" if style == "Points" else "wireframe"
            else:
                for a in self._actors:
                    if a is not None:
                        a.GetProperty().EdgeVisibilityOff()
                fe_style = "wireframe"
            self._add_feature_edges(render_style=fe_style)
        # Sync tree checkboxes → actor visibility
        self._sync_part_visibility()
        self.plotter.render()

    # ------------------------------------------------------------------
    #  Toolbar callbacks
    # ------------------------------------------------------------------

    def _apply_style_to_actor(self, actor, style: str, mesh_visible: bool | None = None):
        """Apply a render style to a single actor.

        Parameters
        ----------
        mesh_visible : bool | None
            Override for mesh (edge) visibility.  When *None* the global
            ``_mesh_visible`` flag is used.
        """
        if actor is None:
            return
        if mesh_visible is None:
            mesh_visible = self._mesh_visible
        mesh_off = not mesh_visible
        prop = actor.GetProperty()

        if style == "Surface":
            actor.SetVisibility(True)
            prop.SetRepresentationToSurface()
            prop.EdgeVisibilityOff()
        elif style == "Wireframe":
            if mesh_off:
                actor.SetVisibility(False)
            else:
                actor.SetVisibility(True)
                prop.SetRepresentationToWireframe()
        elif style == "Surface + Edges":
            actor.SetVisibility(True)
            prop.SetRepresentationToSurface()
            if mesh_off:
                prop.EdgeVisibilityOff()
            else:
                prop.EdgeVisibilityOn()
        elif style == "Points":
            if mesh_off:
                actor.SetVisibility(False)
            else:
                actor.SetVisibility(True)
                prop.SetRepresentationToPoints()
                prop.SetPointSize(5)

    def _set_render_style(self, style: str):
        selected_pids = self._get_selected_pids()

        if selected_pids:
            # Apply only to selected parts
            for pid in selected_pids:
                self._per_part_style[pid] = style
                actor = self._part_actors.get(pid)
                mesh_vis = self._per_part_mesh.get(pid, self._mesh_visible)
                self._apply_style_to_actor(actor, style, mesh_visible=mesh_vis)
            scope = ", ".join(str(p) for p in selected_pids)
            self._status.showMessage(f"Render style: {style}  (parts: {scope})", 3000)
        else:
            # No selection → apply to all
            self._current_style = style
            self._per_part_style.clear()
            mesh_off = not self._mesh_visible
            for actor in self._actors:
                self._apply_style_to_actor(actor, style)
            self._edges_visible = style == "Surface + Edges"

            if mesh_off:
                fe_style = "points" if style == "Points" else "wireframe"
                self._add_feature_edges(render_style=fe_style)
            else:
                self._remove_feature_edges()
            self._status.showMessage(f"Render style: {style}", 3000)

        self.plotter.render()

    def _toggle_mesh(self, checked: bool):
        """Toggle mesh wireframe visibility.

        When *checked* is True the full element wireframe is drawn.
        When False only the feature / boundary edges of each part are
        shown, giving a cleaner "outline-only" look.

        If parts are selected in the tree, only those parts are affected.
        Otherwise the change is applied to the whole model.
        """
        selected_pids = self._get_selected_pids()

        if selected_pids:
            # ── Per-part toggle ──────────────────────────────────────
            for pid in selected_pids:
                self._per_part_mesh[pid] = checked
                actor = self._part_actors.get(pid)
                if actor is None:
                    continue
                style = self._per_part_style.get(pid, self._current_style)
                self._apply_style_to_actor(actor, style, mesh_visible=checked)
            scope = ", ".join(str(p) for p in selected_pids)
            state = "ON" if checked else "OFF"
            self._status.showMessage(
                f"Mesh wireframe: {state}  (parts: {scope})", 3000
            )
        else:
            # ── Global toggle ────────────────────────────────────────
            self._mesh_visible = checked
            self._per_part_mesh.clear()
            style = self._current_style

            if checked:
                for actor in self._actors:
                    if actor is None:
                        continue
                    self._apply_style_to_actor(actor, style)
                self._remove_feature_edges()
                self._status.showMessage("Mesh wireframe: ON", 3000)
            else:
                if style in ("Wireframe", "Points"):
                    for actor in self._actors:
                        if actor is not None:
                            actor.SetVisibility(False)
                    fe_style = "points" if style == "Points" else "wireframe"
                    self._add_feature_edges(render_style=fe_style)
                else:
                    for actor in self._actors:
                        if actor is not None:
                            actor.GetProperty().EdgeVisibilityOff()
                    self._add_feature_edges(render_style="wireframe")
                self._status.showMessage(
                    "Mesh wireframe: OFF  (feature edges only)", 3000
                )

        self.plotter.render()

    def _add_feature_edges(self, render_style: str = "wireframe"):
        """Extract and display feature edges (boundaries + sharp edges).

        Parameters
        ----------
        render_style : str
            PyVista style for the overlay actor: ``"wireframe"`` (lines)
            or ``"points"`` (dots at boundary vertices).
        """
        if self._polydata is None:
            return
        # Remove previous feature edges actor if any
        if self._feature_edges_actor is not None:
            self.plotter.remove_actor(self._feature_edges_actor)
            self._feature_edges_actor = None
        try:
            edges = self._polydata.extract_feature_edges(
                boundary_edges=True,
                feature_edges=True,
                manifold_edges=False,
                non_manifold_edges=True,
                feature_angle=30,
            )
            if edges.n_points > 0:
                fe_color = "white" if getattr(self, '_current_bg', '') == "black" else "black"
                kw = dict(
                    color=fe_color,
                    label="_feature_edges",  # underscore = hidden from legend
                )
                if render_style == "points":
                    kw.update(style="points", point_size=5, render_points_as_spheres=True)
                else:
                    kw.update(style="wireframe", line_width=1.5)
                self._feature_edges_actor = self.plotter.add_mesh(edges, **kw)
        except Exception as exc:
            logger.warning("Could not extract feature edges: %s", exc)

    def _remove_feature_edges(self):
        """Remove the feature-edges overlay actor if present."""
        if self._feature_edges_actor is not None:
            self.plotter.remove_actor(self._feature_edges_actor)
            self._feature_edges_actor = None

    def _set_opacity(self, text: str):
        val = int(text.replace("%", "")) / 100.0
        selected_pids = self._get_selected_pids()
        if selected_pids:
            for pid in selected_pids:
                actor = self._part_actors.get(pid)
                if actor is not None:
                    actor.GetProperty().SetOpacity(val)
            scope = ", ".join(str(p) for p in selected_pids)
            self._status.showMessage(f"Opacity: {text}  (parts: {scope})", 3000)
        else:
            for actor in self._actors:
                if actor is not None:
                    actor.GetProperty().SetOpacity(val)
        self.plotter.render()

    def _change_colormap(self, cmap_name: str):
        self._rebuild_mesh()
        self._status.showMessage(f"Colormap: {cmap_name}", 3000)

    def _toggle_axes(self, checked: bool):
        self._axes_visible = checked
        if checked:
            self.plotter.show_axes()
        else:
            self.plotter.hide_axes()
        self.plotter.render()

    def _toggle_bounds(self, checked: bool):
        self._bounds_visible = checked
        if checked:
            self.plotter.show_bounds(grid="back", location="outer")
        else:
            self.plotter.remove_bounds_axes()
        self.plotter.render()

    def _toggle_scalar_bar(self, checked: bool):
        self._scalar_bar_visible = checked
        self._rebuild_mesh()

    def _toggle_projection(self, checked: bool):
        self._parallel = checked
        if checked:
            self.plotter.enable_parallel_projection()
        else:
            self.plotter.disable_parallel_projection()
        self.plotter.render()

    def _set_background(self, choice: str):
        bg_map = {
            "White": "white",
            "Light Gray": "lightgray",
            "Dark Gray": "#3c3c3c",
            "Black": "black",
        }
        if choice in bg_map:
            self.plotter.set_background(bg_map[choice])
        elif choice == "Gradient (blue)":
            self.plotter.set_background("royalblue", top="aliceblue")
        elif choice == "Gradient (gray)":
            self.plotter.set_background("#3c3c3c", top="#888888")
        self.plotter.render()

    def _screenshot(self):
        if self.plotter is None:
            return
        default_name = (str(self._file_path.stem) + "_3d.png") if self._file_path else "screenshot_3d.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Screenshot", default_name,
            "PNG Images (*.png);;JPEG Images (*.jpg);;All Files (*)",
        )
        if path:
            self.plotter.screenshot(path)
            self._status.showMessage(f"Screenshot saved: {path}", 5000)

    @staticmethod
    def _create_camera_icon(size: int = 28) -> QIcon:
        """Create a camera/screenshot icon programmatically."""
        px = QPixmap(QSize(size, size))
        px.fill(QColor(0, 0, 0, 0))
        p = QPainter(px)
        if not p.isActive():
            return QIcon(px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#333333"))
        pen.setWidth(2)
        p.setPen(pen)
        # Camera body
        m = int(size * 0.15)
        body_top = int(size * 0.35)
        p.drawRoundedRect(m, body_top, size - 2 * m, int(size * 0.50), 3, 3)
        # Lens bump on top
        bump_w = int(size * 0.28)
        bump_x = (size - bump_w) // 2
        p.drawRect(bump_x, body_top - int(size * 0.12), bump_w, int(size * 0.14))
        # Lens circle
        lens_r = int(size * 0.16)
        cx, cy = size // 2, body_top + int(size * 0.25)
        p.drawEllipse(cx - lens_r, cy - lens_r, 2 * lens_r, 2 * lens_r)
        p.end()
        return QIcon(px)

    # ------------------------------------------------------------------
    #  Status helpers
    # ------------------------------------------------------------------

    def _update_status_info(self):
        pd = self._polydata
        parts_txt = f"  |  Parts: {self._n_parts}" if self._has_parts else ""
        self._status.showMessage(
            f"{self._file_path.name}  |  "
            f"Nodes: {pd.n_points:,}  |  Cells: {pd.n_cells:,}{parts_txt}"
        )

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        # Position the floating toolbar at the top-left of the plotter
        if hasattr(self, '_floating_tb') and self.plotter is not None:
            plotter_pos = self.plotter.mapTo(self, self.plotter.rect().topLeft())
            self._floating_tb.move(plotter_pos.x() + 8, plotter_pos.y() + 8)
            self._floating_tb.raise_()
        # Defer tree reposition so toolbar geometry is settled
        from PySide6.QtCore import QTimer
        QTimer.singleShot(50, self._reposition_part_tree)

    def closeEvent(self, event):
        if self._load_worker is not None:
            self._load_worker.finished.disconnect()
            self._load_worker.quit()
            self._load_worker.wait(2000)
            self._load_worker = None
        try:
            if self.plotter is not None:
                # Remove all actors and release the render window before
                # Qt tears down the widget.  This prevents VTK from
                # calling wglMakeCurrent on a destroyed OpenGL context.
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
                self.plotter.close()
        except Exception:
            pass
        super().closeEvent(event)


# ═══════════════════════════════════════════════════════════════════════════
#  Native PyVista fallback  (keyboard-only controls)
# ═══════════════════════════════════════════════════════════════════════════

def _open_native_viewer(polydata, file_path: Path):
    """Open a standalone ``pv.Plotter`` window — no pyvistaqt needed."""
    import pyvista as pv

    has_parts = "part_ids" in polydata.cell_data
    n_parts = len(set(polydata.cell_data["part_ids"])) if has_parts else 0

    plotter = pv.Plotter(
        title=f"3D Model Viewer  (native) — {file_path.name}",
        window_size=(1024, 768),
    )

    if has_parts and n_parts > 1:
        plotter.add_mesh(
            polydata,
            scalars="part_ids",
            show_edges=True,
            edge_color="gray",
            line_width=0.5,
            cmap="tab20",
            show_scalar_bar=True,
            scalar_bar_args={"title": "Part ID", "n_labels": min(n_parts, 10)},
        )
    else:
        plotter.add_mesh(
            polydata,
            show_edges=True,
            color="steelblue",
            opacity=1.0,
            edge_color="gray",
            line_width=0.5,
        )

    plotter.add_axes()
    plotter.set_background("white")

    info = [
        f"File: {file_path.name}",
        f"Nodes: {polydata.n_points:,}",
        f"Cells: {polydata.n_cells:,}",
    ]
    if has_parts:
        info.append(f"Parts: {n_parts}")
    info.append("")
    info.append("Keys: w=wireframe  s=surface  v=isometric  r=reset  q=quit")
    plotter.add_text(
        "\n".join(info),
        position="upper_left",
        font_size=9,
        color="black",
        shadow=True,
    )

    plotter.show(interactive_update=True)


# ═══════════════════════════════════════════════════════════════════════════
#  Public entry point
# ═══════════════════════════════════════════════════════════════════════════

def open_k_model_viewer(
    file_path: str | Path | None = None,
    parent: QWidget | None = None,
    force_native: bool = False,
) -> None:
    """Open the 3D viewer for a .k model file.

    Automatically uses the Qt-embedded viewer (with toolbars) when
    *pyvistaqt* is available, otherwise falls back to the native PyVista
    window.

    When *file_path* is ``None``, the Qt-embedded viewer opens empty so
    the user can import a model via File \u2192 Import.

    Parameters
    ----------
    file_path : str, Path or None
        Path to the .k / .key / .dyn file.  Pass ``None`` to open an
        empty viewer.
    parent : QWidget, optional
        Parent widget for error dialogs and dialog ownership.
    force_native : bool
        If ``True``, skip pyvistaqt and open the native VTK window.
    """
    # Dependency check
    err = _check_dependencies()
    if err:
        QMessageBox.critical(parent, "Missing dependency", err)
        return

    # --- Empty viewer (no file) -------------------------------------------
    if file_path is None:
        if not _has_pyvistaqt():
            QMessageBox.warning(
                parent, "Not available",
                "Cannot open an empty viewer in native mode.\n"
                "Please select a .k file or install pyvistaqt.",
            )
            return
        dlg = KModelViewerDialog(parent=parent)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()
        return

    # --- Open with a file -------------------------------------------------
    file_path = Path(file_path)
    if not file_path.exists():
        QMessageBox.warning(parent, "File not found", f"File does not exist:\n{file_path}")
        return
    if file_path.suffix.lower() not in (".k", ".key", ".dyn"):
        QMessageBox.warning(
            parent,
            "Unsupported file",
            f"Only .k / .key / .dyn files can be visualized.\n\nSelected: {file_path.name}",
        )
        return

    # Parse model
    if not force_native and _has_pyvistaqt():
        # Show dialog immediately with loading indicator, parse in background
        dlg = KModelViewerDialog(parent=parent, _deferred_load=True)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()
        dlg.load_file_async(file_path)
    else:
        polydata, part_names, keyword_entities, error = _load_polydata(file_path)
        if error:
            QMessageBox.critical(parent, "Model error", error)
            return
        _open_native_viewer(polydata, file_path)
