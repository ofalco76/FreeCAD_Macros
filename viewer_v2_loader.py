# ui/viewer_v2_loader.py
"""
Self-contained .k file loading pipeline for the v2 3D viewer.

Pipeline:
  1. VTK binary cache lookup (.vtp + .json) keyed by path/mtime/size/filter-version.
  2. On cache miss: write a geometry-only filtered copy of the .k file to %TEMP%,
     keeping only NODE / ELEMENT_* / PART / INCLUDE / SET_* / DEFINE_TRANSFORMATION
     keywords. PyDyna parses far fewer records.
  3. Pre-scan the filtered file to classify it (monolithic vs. assembly with
     transforms) so we can skip expensive stages when they are unnecessary.
  4. Hand the filtered file to PyDyna's Deck.import_file.
  5. Build a vtkPolyData via ansys.dyna.core.lib.deck_plotter.get_polydata.
  6. Conditionally expand the deck (only when INCLUDEs exist) and reuse parsed
     includes via a sub-deck cache when extracting SET_* entities.
  7. Persist polydata + metadata to the cache for fast future opens.

Public surface used by viewer_v2:
  - load_polydata(file_path) -> (polydata, part_names, kw_entities, error)
  - LoadWorker(QThread)         emits finished(polydata, part_names, entities, error)
  - viewer_cache_dir()          for cache cleanup hooks
  - viewer_cache_key(path)      for "Clear cache for this file" actions
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Background module warmup
#
#  The first .k load pays a multi-second cold-import cost for pyvista/vtk and
#  pandas. Kick those imports off on a daemon thread the moment this module
#  is loaded so they're warm by the time load_polydata actually needs them.
# ---------------------------------------------------------------------------

def _warmup_heavy_modules() -> None:
    try:
        import numpy   # noqa: F401
        import pandas  # noqa: F401
        import vtk     # noqa: F401
        import pyvista # noqa: F401
    except Exception:
        pass


threading.Thread(target=_warmup_heavy_modules, daemon=True, name="viewer-warmup").start()


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


class _Timings:
    """Lightweight per-phase timer for the load pipeline."""

    def __init__(self, label: str):
        self.label = label
        self._t0 = _now_ms()
        self._marks: list[tuple[str, float]] = []

    def mark(self, name: str) -> None:
        now = _now_ms()
        prev = self._marks[-1][1] if self._marks else self._t0
        self._marks.append((name, now))
        # print(f"[viewer-load] {self.label} | {name}: {now - prev:.1f} ms", flush=True)

    def total(self) -> None:
        total = _now_ms() - self._t0
        # print(f"[viewer-load] {self.label} | TOTAL: {total:.1f} ms", flush=True)


# ---------------------------------------------------------------------------
#  Geometry-only pre-filter
# ---------------------------------------------------------------------------

_VIEWER_KEEP_PREFIXES: tuple = (
    "KEYWORD", "END",
    "NODE",
    "ELEMENT_SHELL", "ELEMENT_SOLID", "ELEMENT_BEAM",
    "ELEMENT_TSHELL", "ELEMENT_MASS", "ELEMENT_DISCRETE",
    "ELEMENT_SEATBELT",
    "PART",
    "INCLUDE",
    "DEFINE_TRANSFORMATION",
    "SET_NODE", "SET_SHELL", "SET_SOLID", "SET_PART", "SET_SEGMENT",
)


def _kw_should_keep(line: str):
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
    """Write a geometry-only filtered copy of *file_path* to %TEMP%.

    Relative ``*INCLUDE`` paths inside the file are rewritten to absolute paths
    so PyDyna resolves them when parsing the temp file. Caller deletes the temp
    file afterwards.
    """
    import tempfile

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    base_dir = file_path.parent
    out_lines: list = []
    keep_block = True
    awaiting_include_fname = False

    for line in content.splitlines(keepends=True):
        stripped = line.strip()

        if awaiting_include_fname:
            if not stripped or stripped.startswith("$"):
                if keep_block:
                    out_lines.append(line)
                continue
            awaiting_include_fname = False
            if keep_block:
                fname = stripped
                if not (fname.startswith("/") or (len(fname) > 1 and fname[1] == ":")):
                    abs_fname = str(base_dir / fname)
                    ending = line[len(line.rstrip("\r\n")):]
                    line = abs_fname + ending
                out_lines.append(line)
            continue

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
        fd, tmp = tempfile.mkstemp(suffix=".k", prefix="mv_tmp_")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.writelines(out_lines)
        return Path(tmp)
    except Exception:
        return None


# ---------------------------------------------------------------------------
#  Pre-scan: classify a .k file without parsing it
# ---------------------------------------------------------------------------

class _DeckProfile:
    """Cheap structural summary of a .k file produced by a single line scan.

    Used to choose the fastest valid pipeline:
      * ``has_include`` False  -> skip ``deck.expand()`` (no-op).
      * ``has_include_xform`` False -> sub-deck SET extraction needs no offsets.
      * ``has_define_xform`` False -> no per-node transformations applied.
    """

    __slots__ = (
        "has_include",
        "has_include_xform",
        "has_define_xform",
        "include_paths",
    )

    def __init__(self) -> None:
        self.has_include = False
        self.has_include_xform = False
        self.has_define_xform = False
        self.include_paths: list[Path] = []


def _scan_kfile_profile(file_path: Path) -> _DeckProfile:
    """Walk the file lines once; record include + transform usage."""
    profile = _DeckProfile()
    base_dir = file_path.parent
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except Exception:
        return profile

    awaiting_include_fname = False
    for line in content.splitlines():
        if awaiting_include_fname:
            stripped = line.strip()
            if not stripped or stripped.startswith("$"):
                continue
            awaiting_include_fname = False
            fname = stripped
            try:
                if fname.startswith("/") or (len(fname) > 1 and fname[1] == ":"):
                    profile.include_paths.append(Path(fname))
                else:
                    profile.include_paths.append(base_dir / fname)
            except Exception:
                pass
            continue

        s = line.lstrip()
        if not s.startswith("*"):
            continue
        kw = s[1:].split("$")[0].strip().split()
        if not kw:
            continue
        kw_name = kw[0].upper()
        if kw_name == "INCLUDE_TRANSFORM" or kw_name.startswith("INCLUDE_TRANSFORM"):
            profile.has_include = True
            profile.has_include_xform = True
            awaiting_include_fname = True
        elif kw_name == "INCLUDE" or (
            kw_name.startswith("INCLUDE_") and kw_name != "INCLUDE_TRANSFORM"
        ):
            profile.has_include = True
            awaiting_include_fname = True
        elif kw_name == "DEFINE_TRANSFORMATION" or kw_name.startswith(
            "DEFINE_TRANSFORMATION"
        ):
            profile.has_define_xform = True

    return profile


# ---------------------------------------------------------------------------
#  VTK binary cache
# ---------------------------------------------------------------------------

_VIEWER_CACHE_SCHEMA = "v7"  # bump when the entities/materials payload shape changes


def viewer_cache_key(file_path: Path) -> str:
    """16-char hex key from path + mtime + size + filter version + schema.

    Also folds in *included* files' mtime/size so that re-exporting a child
    file (e.g. ``box_centre.k``) invalidates the cache for the parent
    (``main.k``) even when the parent itself wasn't rewritten.
    """
    import hashlib
    _filter_ver = hashlib.sha1(
        "|".join(sorted(_VIEWER_KEEP_PREFIXES)).encode()
    ).hexdigest()[:6]
    try:
        st = file_path.stat()
        raw = f"{file_path.resolve()}|{st.st_mtime}|{st.st_size}|{_filter_ver}|{_VIEWER_CACHE_SCHEMA}"
    except Exception:
        raw = f"{file_path.resolve()}|{_filter_ver}|{_VIEWER_CACHE_SCHEMA}"

    # Append top-level includes' fingerprints (sorted for stability).
    try:
        profile = _scan_kfile_profile(file_path)
        inc_parts: list[str] = []
        for inc in profile.include_paths:
            try:
                ist = inc.stat()
                inc_parts.append(f"{inc.resolve()}|{ist.st_mtime}|{ist.st_size}")
            except Exception:
                inc_parts.append(f"{inc}|missing")
        if inc_parts:
            raw = raw + "||INC||" + "||".join(sorted(inc_parts))
    except Exception:
        pass
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def viewer_cache_dir() -> Path:
    import tempfile
    return Path(tempfile.gettempdir()) / "pydeck_viewer_cache"


def _entities_to_json_safe(entities: dict) -> dict:
    out: dict = {}
    for cat, val in entities.items():
        if cat == "__names__":
            out["__names__"] = {
                f"{c}::{s}": title for (c, s), title in val.items()
            }
        elif cat == "__materials__":
            out["__materials__"] = {
                "materials": {
                    str(mid): dict(info) for mid, info in val.get("materials", {}).items()
                },
                "part_to_mid": {
                    str(pid): int(mid) for pid, mid in val.get("part_to_mid", {}).items()
                },
                "part_to_heading": {
                    str(pid): str(h) for pid, h in val.get("part_to_heading", {}).items()
                },
            }
        elif cat == "__topology__":
            out["__topology__"] = {
                "total_nodes": int(val.get("total_nodes", 0)),
                "per_pid": {
                    str(pid): {"elements": d["elements"], "nodes": d["nodes"]}
                    for pid, d in val.get("per_pid", {}).items()
                },
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
    out: dict = {}
    for cat, val in data.items():
        if cat == "__names__":
            names: dict = {}
            for key, title in val.items():
                parts = key.split("::", 1)
                if len(parts) == 2:
                    names[(parts[0], int(parts[1]))] = title
            out["__names__"] = names
        elif cat == "__materials__":
            out["__materials__"] = {
                "materials": {
                    int(mid): dict(info)
                    for mid, info in val.get("materials", {}).items()
                },
                "part_to_mid": {
                    int(pid): int(mid)
                    for pid, mid in val.get("part_to_mid", {}).items()
                },
                "part_to_heading": {
                    int(pid): str(h)
                    for pid, h in val.get("part_to_heading", {}).items()
                },
            }
        elif cat == "__topology__":
            out["__topology__"] = {
                "total_nodes": int(val.get("total_nodes", 0)),
                "per_pid": {
                    int(pid): {"elements": d["elements"], "nodes": d["nodes"]}
                    for pid, d in val.get("per_pid", {}).items()
                },
            }
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
    import json
    d = viewer_cache_dir()
    vtp_path = d / f"{cache_key}.vtp"
    meta_path = d / f"{cache_key}.json"
    if not vtp_path.exists() or not meta_path.exists():
        return None, None, None
    try:
        import pyvista as pv  # only pay the import cost on cache hit
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
    import json
    try:
        d = viewer_cache_dir()
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
#  SET_* entity extraction (with INCLUDE_TRANSFORM offset resolution)
# ---------------------------------------------------------------------------

_MEMBER_OFFSET_ATTR = {
    "SET_NODE":    "idnoff",
    "SET_SHELL":   "ideoff",
    "SET_SOLID":   "ideoff",
    "SET_PART":    "idpoff",
    "SET_SEGMENT": "idnoff",
}


def _extract_sets_from_deck(deck) -> list:
    """Return ``[(category, sid, members, title), ...]`` from SET keywords.

    Raw IDs as stored in the file (no offset).
    """
    from ansys.dyna.core.lib.series_card import SeriesCard

    _PREFIX_MAP = [
        ("SetNodeList",  "SET_NODE"),
        ("SetShellList", "SET_SHELL"),
        ("SetSolidList", "SET_SOLID"),
        ("SetPartList",  "SET_PART"),
        ("SetSegment",   "SET_SEGMENT"),
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


def _extract_keyword_entities(
    deck,
    cwd: str,
    *,
    profile: "_DeckProfile | None" = None,
    sub_deck_cache: "dict[str, object] | None" = None,
) -> dict:
    """Extract SET keywords resolving ``*INCLUDE_TRANSFORM`` offsets.

    PyDyna's ``deck.expand()`` offsets NODE/ELEMENT/PART IDs but **not** SET SIDs
    or their members. This function manually re-parses each include and applies
    ``idsoff`` / ``idnoff`` / ``ideoff`` / ``idpoff`` so entity IDs match the
    assembled polydata.

    Optimizations:
      * If ``profile.has_include`` is False, the include-walk loop is skipped.
      * ``sub_deck_cache`` is consulted before re-parsing an include file.
    """
    from ansys.dyna.core import Deck as _Deck

    entities: dict[str, dict[int, list]] = {}
    names: dict[tuple, str] = {}
    if sub_deck_cache is None:
        sub_deck_cache = {}

    def _merge(category, sid, members, title=""):
        entities.setdefault(category, {})[sid] = members
        if title:
            names[(category, sid)] = title

    try:
        for cat, sid, members, title in _extract_sets_from_deck(deck):
            _merge(cat, sid, members, title)

        if profile is not None and not profile.has_include:
            if names:
                entities["__names__"] = names
            return entities

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

            cache_key = str(sub_path.resolve())
            sub_deck = sub_deck_cache.get(cache_key)
            if sub_deck is None:
                try:
                    sub_deck = _Deck()
                    sub_deck.import_file(str(sub_path))
                    sub_deck_cache[cache_key] = sub_deck
                except Exception:
                    logger.debug("Failed to parse include %s", sub_path)
                    continue

            idsoff = int(getattr(kw_obj, "idsoff", 0) or 0) if is_transform else 0

            for cat, sid, members, title in _extract_sets_from_deck(sub_deck):
                offset_sid = sid + idsoff
                member_attr = _MEMBER_OFFSET_ATTR.get(cat, "")
                member_off = (
                    int(getattr(kw_obj, member_attr, 0) or 0)
                    if (is_transform and member_attr) else 0
                )
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
    """Extract ``{pid: heading}`` from a PyDyna Deck."""
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
#  Top-level loader
# ---------------------------------------------------------------------------

def _count_deck_topology(flat_deck) -> dict:
    """Count per-PID element totals and unique referenced node counts from a
    parsed (expanded) PyDyna Deck.

    Uses the same ``kw.elements`` DataFrame that the Keyword Manager UI reads,
    so the numbers match exactly what is shown in the *ELEMENT_* parameter table.

    Returns::

        {
            "total_nodes": int,               # all rows in all *NODE sections
            "per_pid": {
                pid: {"elements": int, "nodes": int},
                ...
            },
        }

    ``nodes`` per PID is the count of **unique node IDs** referenced by that
    part's element connectivity (n1..nN columns), which is the real LS-DYNA
    notion of "how many nodes belong to this part".
    """
    per_pid_elem: dict = {}   # pid -> element count
    per_pid_nids: dict = {}   # pid -> set of referenced nids
    total_nodes = 0

    # Total node count from *NODE sections
    try:
        for kw in flat_deck.nodes:
            nodes_card = getattr(kw, "nodes", None)
            df = getattr(nodes_card, "table", None) if nodes_card is not None else None
            if df is not None and "nid" in getattr(df, "columns", []):
                total_nodes += len(df)
    except Exception:
        pass

    # Element count and referenced node IDs per PID
    try:
        for kw in flat_deck.keywords:
            df = getattr(kw, "elements", None)
            if df is None or not hasattr(df, "columns"):
                continue
            cols = list(getattr(df, "columns", []))
            if "pid" not in cols:
                continue
            node_cols = [c for c in cols if len(c) > 1 and c[0] == "n" and c[1:].isdigit()]
            for pid_val, grp in df.groupby("pid"):
                pid = int(pid_val)
                per_pid_elem[pid] = per_pid_elem.get(pid, 0) + len(grp)
                if node_cols:
                    nset = per_pid_nids.setdefault(pid, set())
                    for col in node_cols:
                        vals = grp[col].dropna()
                        nset.update(int(v) for v in vals if v != 0)
    except Exception:
        logger.debug("_count_deck_topology failed", exc_info=True)

    return {
        "total_nodes": total_nodes,
        "per_pid": {
            pid: {
                "elements": per_pid_elem[pid],
                "nodes": len(per_pid_nids.get(pid, set())),
            }
            for pid in per_pid_elem
        },
    }


def _build_node_ids_array(flat_deck, polydata) -> None:
    """Attach a ``node_ids`` point_data array to *polydata* (NID per VTK point).

    Bypasses ``deck_plotter.merge_keywords`` (which builds a full pandas DataFrame
    of nodes + elements) by walking ``flat_deck.nodes`` directly with NumPy.
    Falls back to ``merge_keywords`` if the fast path fails.
    """
    orig_ids = polydata.point_data.get("vtkOriginalPointIds")
    if orig_ids is None:
        return

    try:
        import numpy as np

        nid_chunks: list = []
        for kw in flat_deck.nodes:
            nodes_card = getattr(kw, "nodes", None)
            df = getattr(nodes_card, "table", None) if nodes_card is not None else None
            if df is None or "nid" not in getattr(df, "columns", []):
                continue
            arr = df["nid"].to_numpy(copy=False)
            if arr.size:
                nid_chunks.append(arr)

        if nid_chunks:
            nid_array = np.concatenate(nid_chunks)
            polydata.point_data["node_ids"] = nid_array[orig_ids]
            return
    except Exception:
        logger.debug("Fast node_ids path failed; falling back", exc_info=True)

    try:
        from ansys.dyna.core.lib.deck_plotter import merge_keywords
        nodes_df, _ = merge_keywords(flat_deck)
        if "nid" in nodes_df.columns:
            nid_array = nodes_df["nid"].values
            polydata.point_data["node_ids"] = nid_array[orig_ids]
    except Exception:
        logger.debug("merge_keywords node_ids fallback failed", exc_info=True)


def load_polydata(file_path: Path, on_geometry_ready=None):
    """Parse *file_path* and return ``(polydata, part_names, kw_entities, error)``.

    On success ``error`` is ``None``. On failure the first three are empty/None.

    The pipeline self-tunes based on a one-pass pre-scan of the file:
      * Monolithic decks (no INCLUDEs) skip ``deck.expand()``.
      * Assemblies share a single sub-deck cache between expand and SET extraction.

    If *on_geometry_ready* is provided, it is invoked as
    ``on_geometry_ready(polydata, part_names)`` once geometry is ready, BEFORE
    the (potentially expensive) SET entity extraction runs. This lets a UI
    render the model immediately and populate the entity browser later.
    """
    from ansys.dyna.core import Deck
    from ansys.dyna.core.lib.deck_plotter import get_polydata
    import warnings

    timings = _Timings(file_path.name)

    cache_key = viewer_cache_key(file_path)
    cached_pd, cached_names, cached_entities = _viewer_cache_load(cache_key)
    timings.mark("cache_lookup")
    if cached_pd is not None:
        logger.debug("Viewer cache hit: %s", file_path.name)
        if on_geometry_ready is not None:
            try:
                on_geometry_ready(cached_pd, cached_names)
            except Exception:
                logger.debug("on_geometry_ready (cache) failed", exc_info=True)
        timings.total()
        return cached_pd, cached_names, cached_entities, None

    # NOTE: the numpy "fast_load" path was intentionally removed for geometry.
    # It emitted VTK_HEXAHEDRON cells for every *ELEMENT_SOLID and also kept
    # *ELEMENT_SHELL cells used as helical springs, which produced wireframe
    # artefacts that the legacy viewer never showed. The legacy viewer relies
    # exclusively on PyDyna's ``get_polydata``, which excludes those spring
    # shells. To stay 1:1 with legacy we always go through PyDyna here.

    # Pre-scan the original file FIRST so we can decide whether the geometry
    # filter is safe. For decks with *INCLUDE the master is small (no real
    # benefit from filtering) and rewriting relative include filenames to
    # absolute paths overflows PyDyna's 80-char *INCLUDE filename field —
    # PyDyna then truncates the path and fails to resolve the include,
    # leaving deck.nodes empty and crashing get_polydata with
    # KeyError(['x','y','z']).
    profile = _scan_kfile_profile(file_path)
    timings.mark("pre_scan")

    if profile.has_include:
        tmp_path = None
        parse_path = file_path
    else:
        tmp_path = _write_geometry_filtered_temp(file_path)
        parse_path = tmp_path if tmp_path is not None else file_path
    timings.mark("filter_temp")
    # print(
    #     f"[viewer-load] {file_path.name} | profile: "
    #     f"include={profile.has_include} xform_inc={profile.has_include_xform} "
    #     f"xform_def={profile.has_define_xform} includes={len(profile.include_paths)}",
    #     flush=True,
    # )

    try:
        deck = Deck()
        deck.import_file(str(parse_path))
    except Exception as exc:
        logger.exception("Deck.import_file failed for %s", file_path)
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        return None, {}, {}, f"Failed to parse the model file:\n\n{file_path.name}\n\n{exc}"
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
    timings.mark("deck_import")

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
        return None, {}, {}, f"Could not extract geometry from the model:\n\n{exc}"
    timings.mark("get_polydata")

    if profile.has_include:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                flat_deck = deck.expand(cwd=cwd, recurse=True)
        except Exception:
            flat_deck = deck
    else:
        flat_deck = deck
    timings.mark("expand")

    _build_node_ids_array(flat_deck, polydata)
    timings.mark("node_ids")

    part_names = _extract_part_names(flat_deck)
    timings.mark("part_names")

    if on_geometry_ready is not None:
        try:
            on_geometry_ready(polydata, part_names)
        except Exception:
            logger.debug("on_geometry_ready callback failed", exc_info=True)
        timings.mark("geometry_signal")

    sub_deck_cache: dict[str, object] = {}
    keyword_entities = _extract_keyword_entities(
        deck, cwd, profile=profile, sub_deck_cache=sub_deck_cache
    )
    timings.mark("entities")

    try:
        from ui.viewer_v2_materials import walk_materials
        mat_payload = walk_materials(file_path)
        if mat_payload.get("materials") or mat_payload.get("part_to_mid"):
            keyword_entities["__materials__"] = mat_payload
        # Authoritative headings from the .k walker — PyDyna's ``str(p)`` can
        # drop the heading line for parts pulled in through INCLUDE_TRANSFORM
        # after offset expansion. Prefer the walker value whenever the
        # PyDyna-derived name is missing or synthetic (e.g. "Part 12").
        walker_headings = mat_payload.get("part_to_heading", {}) or {}
        for pid, heading in walker_headings.items():
            if not heading:
                continue
            existing = part_names.get(pid, "")
            synthetic = existing in ("", f"Part {pid}")
            if synthetic:
                part_names[pid] = heading

        # Transform titles take precedence over individual part headings for
        # parts pulled in via *INCLUDE_TRANSFORM. The user's mental model of
        # an assembly is "this part belongs to the <transform-title> group",
        # so showing the transform's title in the navigator is more useful
        # than the raw *PART heading (which may be a cryptic sub-assembly
        # identifier). Master-file parts and plain *INCLUDE parts have no
        # tranid and are unaffected.
        walker_xform_titles = mat_payload.get("part_to_transform_title", {}) or {}
        for pid, title in walker_xform_titles.items():
            if title:
                part_names[pid] = title
    except Exception:
        logger.debug("walk_materials failed", exc_info=True)
    timings.mark("materials")

    try:
        keyword_entities["__topology__"] = _count_deck_topology(flat_deck)
    except Exception:
        logger.debug("_count_deck_topology failed", exc_info=True)
    timings.mark("topology")

    _viewer_cache_save(cache_key, polydata, part_names, keyword_entities)
    timings.mark("cache_save")
    timings.total()

    return polydata, part_names, keyword_entities, None


class LoadWorker(QThread):
    """QThread that runs ``load_polydata`` in the background.

    Emits two signals in sequence so the UI can render the model before the
    SET entity browser is populated:

      * ``geometry_ready(polydata, part_names)``  — emitted as soon as polydata
        and part names are available (typically the first 60-80% of total
        load time).
      * ``finished(polydata, part_names, kw_entities, error)`` — emitted when
        the full pipeline (including SET extraction) completes.

    Existing consumers can keep using ``finished`` only; ``geometry_ready`` is
    optional.
    """

    # NOTE: typing the dict params as `dict` triggers a QVariantMap conversion
    # across thread boundaries that silently drops non-string keys (our
    # part_names / part_to_mid use int keys). Using `object` bypasses the
    # conversion and the Python dict arrives intact.
    geometry_ready = Signal(object, object)  # polydata, part_names
    finished = Signal(object, object, object, str)  # polydata, part_names, kw_entities, error

    def __init__(self, path: Path):
        super().__init__()
        self._path = path

    def _emit_geometry(self, polydata, part_names: dict) -> None:
        try:
            self.geometry_ready.emit(polydata, part_names)
        except Exception:
            logger.debug("geometry_ready emit failed", exc_info=True)

    def run(self):
        polydata, part_names, kw_entities, error = load_polydata(
            self._path, on_geometry_ready=self._emit_geometry
        )
        self.finished.emit(polydata, part_names, kw_entities, error or "")
