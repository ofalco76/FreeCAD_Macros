from pathlib import Path
import re
from io import StringIO
import json
import ast
import logging
import pandas as pd

from core.adapters import pydyna

from .massive_data_handler import MassiveDataHandler, create_massive_data_handler

logger = logging.getLogger(__name__)


class KFileBuilder:
    """Builds .k files using PyDYNA Deck/keywords.

    Strategy:
    - One .k file per root node.
    - Try to map the node name to a ``kwd`` class (e.g., "CONTACT_SURFACE_TO_SURFACE" -> ``kwd.ContactSurfaceToSurface``).
    - Hydrate keyword attributes with stored parameters (best-effort numeric/bool conversion).
    - If no class is found, append a raw block with comments and key=value pairs.
    - Always write via ``Deck.write`` to leverage PyDYNA serialization.
    
    Supports hybrid storage architecture (SQLite + HDF5) for efficient massive data handling.
    """

    def __init__(self, project_vm, hybrid_manager=None):
        """
        Initialize KFileBuilder.
        
        Args:
            project_vm: ProjectViewModel for accessing parameters
            hybrid_manager: Optional HybridDataManager for efficient HDF5 data access
        """
        self.project_vm = project_vm
        self.hybrid_manager = hybrid_manager
        self._data_handler: MassiveDataHandler = None
        self._current_project_id: int = None
    
    def _init_data_handler(self, project_id: int):
        """Initialize the massive data handler for an export session."""
        self._current_project_id = project_id
        self._data_handler = create_massive_data_handler(
            project_vm=self.project_vm,
            hybrid_manager=self.hybrid_manager,
            project_id=project_id
        )
        logger.debug(f"Initialized data handler for project {project_id}")
    
    def _cleanup_data_handler(self):
        """Clean up data handler after export."""
        if self._data_handler:
            stats = self._data_handler.get_cache_stats()
            logger.debug(f"Export cache stats: {stats}")
            self._data_handler.clear_cache()
            self._data_handler = None
            self._current_project_id = None

    def slug(self, name: str, fallback: str) -> str:
        """Public helper to create safe slugs for file/folder names."""
        return self._slug(name, fallback)

    def _slug(self, name: str, fallback: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", name or "").strip("_")
        return cleaned or fallback

    def _export_dir_for(self, project_id: int, output_root: Path, project_name: str | None = None) -> Path:
        """Return the export folder using the project name (not the ID).

        - Use a slug of the project name to avoid invalid characters.
        - If the name is missing, fall back to ``project_<id>`` to avoid failures.
        - Accept an optional ``project_name`` hint to avoid DB lookups after deletion.
        """
        name = project_name
        if name is None and hasattr(self.project_vm, "get_project_name"):
            name = self.project_vm.get_project_name(project_id)
        slug = self._slug(name, f"project_{project_id}")
        return output_root / "exports" / slug

    def export_dir_for(self, project_id: int, output_root: Path, project_name: str | None = None) -> Path:
        """Public helper to get the export directory for a project."""
        return self._export_dir_for(project_id, output_root, project_name)

    def _to_number(self, val: str):
        # Use _is_null_value for comprehensive null checking
        if self._is_null_value(val):
            return None
        if isinstance(val, (int, float)):
            # Check for numpy nan (which also is a float)
            import numpy as np
            try:
                if np.isnan(val):
                    return None
            except (TypeError, ValueError):
                pass
            if isinstance(val, float) and val.is_integer():
                return int(val)
            return val
        s = str(val).strip()
        # Double-check for None-like strings after str conversion
        if s.lower() in {"none", "null", "nan", "<na>"}:
            return None
        if s == "":
            return None
        # LS-DYNA parameters start with "&" - keep as string for raw output
        if s.startswith("&"):
            return s
        if s.lower() in {"true", "false"}:
            return s.lower() == "true"
        try:
            if "." in s or "e" in s.lower():
                f = float(s)
                if f.is_integer():
                    return int(f)
                return f
            return int(s)
        except ValueError:
            # Final check: if it looks like a None string, return None
            if s.lower() in {"none", "null", "nan", "<na>"}:
                return None
            return val

    def _is_null_value(self, val) -> bool:
        """
        Check if a value represents a null/None/NaN value.
        
        Handles:
        - Python None
        - numpy.nan
        - pandas.NA, pandas.NaT
        - String representations: 'None', 'null', 'nan', '', etc.
        
        Note: For arrays/lists/DataFrames, returns False (they are not scalar null values).
        
        Returns:
            True if the value should be treated as null/missing
        """
        import pandas as pd
        import numpy as np
        
        # Python None
        if val is None:
            return True
        
        # Skip arrays, lists, DataFrames, Series - they are not scalar null values
        # This avoids "truth value of an array is ambiguous" errors
        if isinstance(val, (list, tuple, np.ndarray, pd.DataFrame, pd.Series)):
            return False
        
        # pandas null types (NA, NaT) - only for scalar values
        try:
            if pd.isna(val):
                return True
        except (TypeError, ValueError):
            # pd.isna can fail on some types
            pass
        
        # numpy nan (float('nan'))
        try:
            if isinstance(val, float) and np.isnan(val):
                return True
        except (TypeError, ValueError):
            pass
        
        # String representations of null
        if isinstance(val, str):
            s = val.strip().lower()
            if s in {'none', 'null', 'nan', '<na>', ''}:
                return True
        
        return False

    # Common PyDyna keyword options that appear as suffixes in keyword names
    KNOWN_OPTIONS = {"TITLE", "ID"}
    
    # Mapping of MAT numbers to PyDyna class name suffixes (e.g., "015" -> "JohnsonCook" -> MatJohnsonCook)
    MAT_NUMBER_TO_NAME = {
        "001": "Elastic", "002": "OrthotropicElastic", "003": "PlasticKinematic",
        "004": "ElasticPlasticThermal", "005": "SoilAndFoam", "006": "Viscoelastic",
        "007": "BlatzKoRubber", "008": "HighExplosiveBurn", "009": "Null",
        "010": "ElasticPlasticHydro", "011": "Steinberg", "012": "IsotropicElasticPlastic",
        "013": "IsotropicElasticFailure", "014": "SoilAndFoamFailure", "015": "JohnsonCook",
        "016": "PseudoTensor", "017": "OrientedCrack", "018": "PowerLawPlasticity",
        "019": "StrainRateDependentPlasticity", "020": "Rigid", "021": "OrthotropicThermal",
        "022": "CompositeDamage", "023": "TemperatureDependentOrthotropic",
        "024": "PiecewiseLinearPlasticity", "025": "GeologicCapModel",
        "026": "Honeycomb", "027": "MooneyRivlinRubber", "028": "ResultantPlasticity",
        "029": "ForceLimited", "030": "ShapeMemory", "033": "BarlatAnisotropicPlasticity",
        "034": "Fabric", "036": "3ParameterBarlat", "037": "TransverselyAnisotropicElasticPlastic",
        "038": "BlatzKoFoam", "057": "LowDensityFoam", "058": "LaminatedCompositeFabric",
        "059": "CompositeFailure", "062": "ViscoplasticMixedHardening", "063": "CrushableFoam",
        "064": "RateSensitivePowerlawPlasticity", "065": "ModifiedZerilliArmstrong",
        "066": "LinearElasticDiscreteBeam", "067": "NonlinearElasticDiscreteBeam",
        "068": "NonlinearPlasticDiscreteBeam", "071": "CableDiscreteBeam",
        "072": "ConcreteDamage", "073": "LowDensityViscousFoam",
        "074": "ElasticSpringDiscreteBeam", "076": "GeneralViscoelastic",
        "077": "OgdenRubber", "078": "SoilConcite", "079": "HystereticSoil",
        "080": "RambergOsgood", "081": "PlasticityWithDamage",
        "083": "FuChangFoam", "084": "WinfrithConcrete",
        "087": "CellularRubber", "089": "PlasticityPolymer",
        "093": "SimplifiedJohnsonCook", "098": "SimplifiedJohnsonCookOrthotropicDamage",
        "099": "SimplifiedRubberFoam", "100": "Spotweld",
        "107": "ModifiedJohnsonCook", "110": "JohnsonHolmquistCeramics",
        "111": "JohnsonHolmquistConcrete", "120": "Gurson",
        "123": "ModifiedPiecewiseLinearPlasticity", "124": "PlasticityCompressionTension",
        "126": "ModifiedHoneycomb", "127": "ArrudaBoyceRubber",
        "138": "CohesiveMixedMode", "143": "Wood", "145": "SchwerMurrayCap",
        "154": "DeshpandeFleckFoam", "155": "Cscm", "159": "CscmConcrete",
        "163": "ModifiedCrushableFoam", "173": "MohrCoulomb", "181": "SimplifiedRubberFoamWithFailure",
        "183": "SimplifiedRubber", "187": "Samp1", "193": "DruckerPrager",
    }
    
    def _class_from_name(self, name: str):
        """Find the PyDyna class for a keyword name.
        
        Returns tuple (kw_class, options_to_activate) where options_to_activate
        is a list of option names that should be activated based on the keyword name.
        E.g., "DEFINE_CURVE_TITLE" -> (DefineCurve, ["TITLE"])
        
        For MAT keywords:
        - "015_JOHNSON_COOK" -> MatJohnsonCook
        - "JOHNSON_COOK" -> MatJohnsonCook
        """
        if not name:
            return None, []
        
        # Special handling for MAT keywords with format "015_JOHNSON_COOK" or just "JOHNSON_COOK"
        # First check if it starts with a 3-digit number (MAT number format)
        mat_match = re.match(r'^(\d{3})(?:[_\s])?(.+)?$', name)
        if mat_match:
            mat_num = mat_match.group(1)
            suffix = mat_match.group(2)  # e.g., "JOHNSON_COOK"
            
            # Try to build class name from suffix first (more reliable)
            if suffix:
                # Convert JOHNSON_COOK -> JohnsonCook
                tokens = re.split(r"[^A-Za-z0-9]+", suffix)
                suffix_camel = "".join(t.title() for t in tokens if t)
                mat_class_name = f"Mat{suffix_camel}"
                cls = pydyna.get_keyword_class(mat_class_name)
                if cls:
                    return cls, []
            
            # Fallback: use number-to-name mapping
            mat_suffix = self.MAT_NUMBER_TO_NAME.get(mat_num)
            if mat_suffix:
                mat_class_name = f"Mat{mat_suffix}"
                cls = pydyna.get_keyword_class(mat_class_name)
                if cls:
                    return cls, []
        
        # Attempts: remove spaces, TitleCase, drop dashes/underscores
        tokens = re.split(r"[^A-Za-z0-9]+", name)
        camel = "".join(t.title() for t in tokens if t)
        candidates = [name, camel, camel.replace("_", ""), camel.upper(), camel.lower()]
        
        # Also try with "Mat" prefix for material keywords
        if not camel.lower().startswith("mat"):
            candidates.append(f"Mat{camel}")
        
        # First, try exact match
        for cand in candidates:
            cls = pydyna.get_keyword_class(cand)
            if cls:
                return cls, []
        
        # If not found, try removing known options from the end and find base class
        detected_options = []
        upper_tokens = [t.upper() for t in tokens if t]
        
        # Check for option suffixes at the end
        while upper_tokens and upper_tokens[-1] in self.KNOWN_OPTIONS:
            detected_options.insert(0, upper_tokens.pop())
        
        if detected_options and upper_tokens:
            # Try to find base class without options
            base_camel = "".join(t.title() for t in upper_tokens)
            base_candidates = [base_camel, base_camel.replace("_", ""), f"Mat{base_camel}"]
            for cand in base_candidates:
                cls = pydyna.get_keyword_class(cand)
                if cls:
                    return cls, detected_options
        
        return None, []

    def _class_from_chain(self, chain_names: list[str]):
        """Robust deduction: use the tail of the path (without IDs) to map to kwd.
        
        Returns tuple (kw_class, options_to_activate).
        
        For MAT keywords, chain might be:
        - ["05_materialcard", "MAT", "015_JOHNSON_COOK"] -> MatJohnsonCook
        - ["05_materialcard", "MAT", "JOHNSON_COOK"] -> MatJohnsonCook
        """
        if not chain_names:
            return None, []

        def camel(parts):
            tokens = re.split(r"[^A-Za-z0-9]+", " ".join(parts))
            return "".join(t.title() for t in tokens if t)

        # Special handling for MAT family keywords
        # Check if "MAT" is in the chain (case-insensitive)
        chain_upper = [n.upper() for n in chain_names]
        if "MAT" in chain_upper:
            mat_idx = chain_upper.index("MAT")
            # The keyword name should be after "MAT"
            if mat_idx + 1 < len(chain_names):
                keyword_name = chain_names[mat_idx + 1]
                # Try to find class from keyword name (handles "015_JOHNSON_COOK" format)
                cls, options = self._class_from_name(keyword_name)
                if cls:
                    return cls, options
                
                # Also try building "Mat" + descriptive name
                # e.g., "JOHNSON_COOK" -> "MatJohnsonCook"
                tokens = re.split(r"[^A-Za-z0-9]+", keyword_name)
                # Filter out pure numbers (like "015")
                name_tokens = [t for t in tokens if t and not t.isdigit()]
                if name_tokens:
                    mat_class_name = "Mat" + "".join(t.title() for t in name_tokens)
                    cls = pydyna.get_keyword_class(mat_class_name)
                    if cls:
                        return cls, options

        # Try combinations from the end: 3, 2, 1 elements
        variants = []
        for span in (3, 2, 1):
            if len(chain_names) >= span:
                sub = chain_names[-span:]
                variants.append(camel(sub))
        # Also include last element alone as safety
        variants.append(camel([chain_names[-1]]))

        seen = set()
        for cand in variants:
            if not cand or cand in seen:
                continue
            seen.add(cand)
            cls, options = self._class_from_name(cand)
            if cls:
                return cls, options
        return None, []

    def _fix_field_types(self, kw_obj) -> None:
        """
        Fix field types on a PyDyna keyword object to ensure compatibility.
        
        Some fields in PyDyna expect specific types (str, float, int) and will
        fail if given the wrong type. This method corrects common issues:
        - String fields that received int/float values
        - Float fields that received int values
        - Enum-like fields that require specific string values (e.g., 'its' in SetNodeList)
        
        NOTE: SetSegment.its expects INTEGER, not string, so we skip this conversion
        for SetSegment keywords.
        """
        # Fields that should always be strings
        string_fields = {'solver', 'title', 'heading', 'name', 'label'}
        
        # Fields that should always be floats
        float_fields = {'da1', 'da2', 'da3', 'da4', 'x', 'y', 'z', 'tc', 'rc',
                        'a1', 'a2', 'a3', 'a4', 'a5', 'a6', 'a7', 'a8'}
        
        # Fields that require specific string values (enum-like fields)
        # These are fields where PyDyna expects a string representation of a number
        # EXCEPTION: SetSegment.its expects int, not string
        enum_string_fields = {'its'}
        
        # Keywords where 'its' should remain as integer (not converted to string)
        its_as_int_keywords = {'SetSegment', 'SetSegmentGeneral', 'SetSegmentTitle', 
                               'SetSegmentGeneralTitle', 'SetSegmentCollect', 
                               'SetSegmentAdd', 'SetSegmentIntersect'}
        
        # Check if this keyword type needs 'its' as int
        kw_class_name = pydyna.get_keyword_type_name(kw_obj)
        skip_its_string_conversion = kw_class_name in its_as_int_keywords
        
        for field in string_fields:
            if pydyna.has_keyword_attr(kw_obj, field):
                val = pydyna.get_keyword_attr(kw_obj, field)
                if val is not None and not isinstance(val, str):
                    try:
                        pydyna.set_keyword_attr(kw_obj, field, str(val))
                    except Exception:
                        pass
        
        for field in float_fields:
            if pydyna.has_keyword_attr(kw_obj, field):
                val = pydyna.get_keyword_attr(kw_obj, field)
                if val is not None and isinstance(val, int):
                    try:
                        pydyna.set_keyword_attr(kw_obj, field, float(val))
                    except Exception:
                        pass
        
        # Handle enum-like string fields
        # These fields may have been stored as int but must be passed as string
        # EXCEPTION: SetSegment keywords need 'its' as int
        for field in enum_string_fields:
            # Skip 'its' conversion for SetSegment keywords
            if field == 'its' and skip_its_string_conversion:
                logger.debug(f"Skipping 'its' string conversion for {kw_class_name}")
                continue
            if pydyna.has_keyword_attr(kw_obj, field):
                val = pydyna.get_keyword_attr(kw_obj, field)
                logger.debug(f"_fix_field_types: {kw_class_name}.{field} = {val!r} (type: {type(val).__name__})")
                if val is not None and isinstance(val, (int, float)):
                    try:
                        # Try to set as string - if it fails, the value might not be valid
                        pydyna.set_keyword_attr(kw_obj, field, str(int(val)))
                        logger.debug(f"  -> Converted to string: {str(int(val))!r}")
                    except Exception:
                        # If setting as string fails, try None as fallback
                        try:
                            pydyna.set_keyword_attr(kw_obj, field, None)
                        except Exception:
                            pass
        
        # For SetSegment keywords, ensure 'its' is an integer (not string)
        # PyDyna SetSegment.its expects int for write, but may have received string from DB
        if skip_its_string_conversion and pydyna.has_keyword_attr(kw_obj, 'its'):
            val = pydyna.get_keyword_attr(kw_obj, 'its')
            if val is not None and isinstance(val, str):
                try:
                    pydyna.set_keyword_attr(kw_obj, 'its', int(val))
                    logger.debug(f"Converted SetSegment.its from string '{val}' to int {int(val)}")
                except (ValueError, TypeError):
                    # If conversion fails, try setting to 0 as default
                    try:
                        pydyna.set_keyword_attr(kw_obj, 'its', 0)
                    except Exception:
                        pass

    def _clean_dataframe(self, df: 'pd.DataFrame') -> 'pd.DataFrame':
        """
        Clean a DataFrame for PyDyna compatibility.
        
        Handles:
        - NA values: Replace with appropriate defaults (0 for numeric, '' for string)
        - Empty strings in numeric columns: Convert to 0
        - Rows with all NA in key columns: Remove them
        - Type conversion: Ensure all columns have proper numeric types
        - String columns: Keep string columns as strings (heading, title, name, etc.)
        
        Args:
            df: DataFrame to clean
            
        Returns:
            Cleaned DataFrame (may be empty if all rows were invalid)
        """
        if df is None or len(df) == 0:
            return df
        
        try:
            import pandas as pd
            import numpy as np
            
            df = df.copy()  # Always work on a copy
            
            # Columns that should remain as strings (not converted to numeric)
            # Note: 'option' is required for DefineTransformation (e.g., "TRANSL", "MIRROR", etc.)
            string_columns = {'heading', 'title', 'name', 'label', 'filename', 'solver', 'option'}
            
            # Pre-process: Replace string representations of None/null/nan with actual NA
            # This handles cases where 'None' is stored as a literal string in the database
            none_strings = ['None', 'none', 'NONE', 'null', 'NULL', 'nan', 'NaN', 'NAN', '<NA>']
            for col in df.columns:
                if df[col].dtype == object:
                    # Replace common None-like strings with pd.NA
                    df[col] = df[col].replace(none_strings, pd.NA)
            
            # First pass: Convert columns - but preserve string columns
            for col in df.columns:
                col_lower = str(col).lower()
                
                # Skip string columns - keep them as strings
                if col_lower in string_columns:
                    # Ensure it's a string type, fill NA with empty string
                    df[col] = df[col].fillna('').astype(str)
                    continue
                
                # Try to convert to numeric first (this handles empty strings too)
                try:
                    numeric_col = pd.to_numeric(df[col], errors='coerce')
                    # If at least some values converted successfully, use numeric
                    if not numeric_col.isna().all():
                        df[col] = numeric_col
                except (TypeError, ValueError):
                    pass
                
                # For object columns, also replace empty/whitespace strings with NA
                if df[col].dtype == object:
                    df[col] = df[col].replace('', pd.NA)
                    df[col] = df[col].replace(r'^\s*$', pd.NA, regex=True)
            
            # For node/element ID columns, NA means potentially invalid row
            # Common ID columns in LS-DYNA keywords
            id_cols = [c for c in df.columns if str(c).lower() in ('nid', 'eid', 'pid', 'n1', 'n2', 'n3', 'n4', 'n5', 'n6', 'n7', 'n8')]
            
            if id_cols:
                # Keep only rows where at least one ID column is not NA
                mask = df[id_cols].notna().any(axis=1)
                if not mask.any():
                    # All rows have all NA in ID columns - return empty
                    return df.iloc[0:0]  # Empty DataFrame with same columns
                df = df[mask].copy()
            
            # Final pass: Fill remaining NA values with appropriate defaults
            for col in df.columns:
                col_lower = str(col).lower()
                
                # Skip already-handled string columns
                if col_lower in string_columns:
                    continue
                    
                if df[col].isna().any():
                    # Check if this column appears to be numeric
                    is_numeric = pd.api.types.is_numeric_dtype(df[col])
                    
                    if is_numeric:
                        # Use 0 for numeric columns
                        df[col] = df[col].fillna(0)
                    else:
                        # For non-numeric, try to fill with 0 (most LS-DYNA fields are numeric)
                        try:
                            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
                        except (TypeError, ValueError):
                            df[col] = df[col].fillna('')
            
            # Ensure no object dtype columns contain empty strings (except string columns)
            # Final conversion pass for any remaining object columns
            for col in df.columns:
                col_lower = str(col).lower()
                if col_lower in string_columns:
                    continue
                    
                if df[col].dtype == object:
                    # Try one more time to convert to numeric
                    try:
                        converted = pd.to_numeric(df[col], errors='coerce')
                        if not converted.isna().any():
                            df[col] = converted
                    except (TypeError, ValueError):
                        pass
            
            # Convert float columns with whole numbers to int
            # PyDyna's field_writer requires actual int types for integer fields
            # This handles both named columns (eid, pid, n1...) AND numeric column indices (0, 1, 2...)
            for col in df.columns:
                col_lower = str(col).lower()
                if col_lower in string_columns:
                    continue
                
                # Check if this is a float column with all whole numbers
                if pd.api.types.is_float_dtype(df[col]):
                    try:
                        # Check if we can safely convert to int (no NaN, no decimals)
                        if not df[col].isna().any():
                            # Check if values are whole numbers (difference from rounded is zero)
                            if np.allclose(df[col], df[col].round()):
                                df[col] = df[col].astype('int64')
                                logger.debug(f"Converted column '{col}' from float to int64")
                    except (TypeError, ValueError, OverflowError) as e:
                        logger.warning(f"Could not convert column '{col}' to int: {e}")
            
            # Log final dtypes for debugging
            logger.debug(f"DataFrame after cleaning - dtypes: {df.dtypes.to_dict()}")
            
            return df
            
        except Exception as e:
            logger.warning(f"Error cleaning DataFrame: {e}")
            return df

    def _sync_scalars_to_first_row(self, df: 'pd.DataFrame', scalars: dict) -> 'pd.DataFrame':
        # Defensive sync for legacy data: if a scalar diverged from row 0 of
        # the matching DataFrame (e.g. project saved before the inline-edit
        # lock was introduced), realign row 0 to the scalar value at export
        # time. Symmetric to the importer's row-0 → scalar promotion.
        if df is None or len(df) == 0 or not scalars:
            return df
        try:
            df = df.copy()
            first_idx = df.index[0]
            for col in df.columns:
                if col in scalars and scalars[col] is not None:
                    df.at[first_idx, col] = scalars[col]
            return df
        except Exception as e:
            logger.warning(f"Error syncing scalars to first row: {e}")
            return df

    def _parse_selected_options(self, raw):
        if raw is None:
            return None
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, list):
                    return loaded
            except ValueError:
                return [p.strip() for p in raw.split(",") if p.strip()]
        return None

    def _parse_list(self, val):
        """Parse a string representation of a list into an actual list."""
        if val is None:
            return None
        if isinstance(val, list):
            return val
        if isinstance(val, str):
            val = val.strip()
            if val.startswith("["):
                try:
                    return ast.literal_eval(val)
                except (ValueError, SyntaxError):
                    return None
        return None

    def _normalize_params(self, params: dict, node_id: int = None, class_name: str = None):
        """
        Normalize parameters for keyword hydration.
        
        Uses MassiveDataHandler for efficient handling of large data from HDF5.
        
        Args:
            params: Raw parameters dictionary
            node_id: Optional node ID for massive data resolution
            class_name: Optional keyword class name for context-aware type handling
            
        Returns:
            Tuple of (normalized_params, selected_options)
        """
        normalized = {}
        selected_opts = self._parse_selected_options(params.get("__selected_option_specs__"))
        
        # Fields that must remain as strings, not converted to numbers
        # NOTE: fcttem in IncludeTransform is a string field (default '1.0'), not a float
        string_fields = {
            "filename", "title", "heading", "prefix", "suffix", "option",
            "name", "alias", "label", "comment", "path", "fcttem"
        }
        
        # Fields that PyDyna expects as string representation of integers
        # These fields validate on set, so we must convert int->str before setting
        # Example: SetNodeList.its must be "1" or "2", not 1 or 2
        # NOTE: SetSegment.its expects INTEGER, not string - so we only apply this
        # to specific keywords that need string representation
        int_to_string_fields = {"its"}
        
        # Keywords where 'its' should remain as integer (not converted to string)
        # SetSegment.its expects int (0, 1, etc.), not string
        its_as_int_keywords = {"setsegment", "setsegmentgeneral"}
        
        # Determine if we should skip int->string conversion for 'its' field
        class_name_lower = (class_name or "").lower()
        skip_its_conversion = any(kw in class_name_lower for kw in its_as_int_keywords)
        
        # Check if we have a1/o1 curve data to convert to DataFrame
        a1_data = None
        o1_data = None
        
        # Check for nodes/elements/parts/segments DataFrames stored as JSON or HDF5
        nodes_df = None
        elements_df = None
        parts_df = None
        segments_df = None
        transforms_df_direct = None  # For directly stored transforms DataFrame
        
        # Check for set node/part lists stored as JSON or HDF5
        set_nodes_list_data = None
        set_parts_list_data = None
        
        # Use data handler if available for efficient massive data loading
        if self._data_handler and node_id:
            nodes_df = self._data_handler.get_nodes_dataframe(node_id, params)
            elements_df = self._data_handler.get_elements_dataframe(node_id, params)
            parts_df = self._data_handler.get_parts_dataframe(node_id, params)
            segments_df = self._data_handler.get_segments_dataframe(node_id, params)
            
            # Get set node/part lists for SetNodeList, SetPartList keywords
            set_nodes_list_data = self._data_handler.get_set_nodes_list(node_id, params)
            set_parts_list_data = self._data_handler.get_set_parts_list(node_id, params)
            
            # Get curve data efficiently
            curve_a1, curve_o1 = self._data_handler.get_curve_data(node_id, params)
            if curve_a1 is not None:
                a1_data = curve_a1
            if curve_o1 is not None:
                o1_data = curve_o1
        
        # First pass: detect if this is a DefineTransformation (has a2-a7 fields)
        has_transform_a_fields = False
        for k, v in params.items():
            if k.startswith("_"):
                # If we didn't use data handler, extract DataFrames manually
                if not self._data_handler and nodes_df is None:
                    if k == "__nodes_dataframe__" and v:
                        try:
                            if isinstance(v, dict):
                                nodes_df = pd.DataFrame(v['data'], columns=v['columns'])
                            elif isinstance(v, pd.DataFrame):
                                nodes_df = v
                            elif isinstance(v, str) and not v.startswith('hdf5:'):
                                nodes_df = pd.read_json(StringIO(v), orient='split')
                        except Exception as e:
                            logger.debug(f"Error parsing nodes DataFrame: {e}")
                
                if not self._data_handler and elements_df is None:
                    if k == "__elements_dataframe__" and v:
                        try:
                            if isinstance(v, dict):
                                elements_df = pd.DataFrame(v['data'], columns=v['columns'])
                            elif isinstance(v, pd.DataFrame):
                                elements_df = v
                            elif isinstance(v, str) and not v.startswith('hdf5:'):
                                elements_df = pd.read_json(StringIO(v), orient='split')
                        except Exception as e:
                            logger.debug(f"Error parsing elements DataFrame: {e}")
                
                if not self._data_handler and parts_df is None:
                    if k == "__parts_dataframe__" and v:
                        try:
                            if isinstance(v, dict):
                                parts_df = pd.DataFrame(v['data'], columns=v['columns'])
                            elif isinstance(v, pd.DataFrame):
                                parts_df = v
                            elif isinstance(v, str) and not v.startswith('hdf5:'):
                                parts_df = pd.read_json(StringIO(v), orient='split')
                        except Exception as e:
                            logger.debug(f"Error parsing parts DataFrame: {e}")
                
                if not self._data_handler and segments_df is None:
                    if k == "__segments_dataframe__" and v:
                        try:
                            if isinstance(v, dict):
                                segments_df = pd.DataFrame(v['data'], columns=v['columns'])
                            elif isinstance(v, pd.DataFrame):
                                segments_df = v
                            elif isinstance(v, str) and not v.startswith('hdf5:'):
                                segments_df = pd.read_json(StringIO(v), orient='split')
                        except Exception as e:
                            logger.debug(f"Error parsing segments DataFrame: {e}")
                
                # Extract transforms DataFrame directly if present
                if k == "__transforms_dataframe__" and v:
                    try:
                        if isinstance(v, dict):
                            transforms_df_direct = pd.DataFrame(v['data'], columns=v['columns'])
                        elif isinstance(v, pd.DataFrame):
                            transforms_df_direct = v
                        elif isinstance(v, str) and not v.startswith('hdf5:'):
                            transforms_df_direct = pd.read_json(StringIO(v), orient='split')
                        else:
                            transforms_df_direct = None
                        # Mark that we have direct transforms data
                        if transforms_df_direct is not None:
                            has_transform_a_fields = True  # Enable transform processing
                    except Exception as e:
                        logger.debug(f"Error parsing transforms DataFrame: {e}")
                        transforms_df_direct = None
                continue
            base = k.split(".")[-1]
            if base in ("a2", "a3", "a4", "a5", "a6", "a7"):
                has_transform_a_fields = True
                # Don't break - continue to extract nodes/elements DataFrames
        
        # Check for DefineTransformation transforms data
        transform_option = None
        transform_a1 = None
        transform_a2 = None
        transform_a3 = None
        transform_a4 = None
        transform_a5 = None
        transform_a6 = None
        transform_a7 = None
        
        for k, v in params.items():
            # Skip internal metadata fields (start with _ or __)
            if k.startswith("_"):
                # Generic TableCard data: __tablecard_<prop>_data__ → parse to DataFrame
                if k.startswith("__tablecard_") and k.endswith("_data__"):
                    prop_name = k[len("__tablecard_"):-len("_data__")]
                    try:
                        import json
                        tc_df = None
                        if isinstance(v, str):
                            if v.strip():  # Skip empty strings
                                parsed = json.loads(v)
                                if isinstance(parsed, dict) and 'columns' in parsed and 'data' in parsed:
                                    tc_df = pd.DataFrame(parsed['data'], columns=parsed['columns'])
                        elif isinstance(v, dict) and 'columns' in v and 'data' in v:
                            tc_df = pd.DataFrame(v['data'], columns=v['columns'])
                        elif isinstance(v, pd.DataFrame):
                            tc_df = v
                        if tc_df is not None and isinstance(tc_df, pd.DataFrame):
                            normalized[f"__tablecard_{prop_name}__"] = tc_df
                    except Exception as e:
                        logger.debug(f"Error parsing TableCard data for {prop_name}: {e}")
                # Skip __tablecard_<prop>__ marker strings (e.g., "[N rows - Click to edit]")
                # These are UI markers, not actual data
                continue
            base = k.split(".")[-1]
            
            # Skip UI marker strings like "[0 rows - Click to edit]" that may
            # have leaked into the DB as bare keys (e.g. bare "parameters" key
            # stored alongside the proper __tablecard_parameters_data__ key).
            # Also catches "[0 curves - Click to edit]", "[0 points - Click to edit]", etc.
            if isinstance(v, str) and re.match(r'^\[\d+ \w+ - Click to edit\]$', v):
                logger.debug(f"Skipping UI marker value for bare key '{base}': {v}")
                continue
            
            # Handle curve data (a1/o1 lists for DefineCurve)
            if base == "a1":
                parsed = self._parse_list(v)
                if parsed is not None:
                    a1_data = parsed
                    continue  # Don't add a1 directly, will be part of curves DataFrame
                elif has_transform_a_fields:
                    # It's a scalar a1 for DefineTransformation
                    transform_a1 = self._to_number(v)
                    continue
                else:
                    # Regular a1 field (not curve, not transform)
                    normalized[base] = self._to_number(v)
                    continue
            elif base == "o1":
                o1_data = self._parse_list(v)
                continue  # Don't add o1 directly, will be part of curves DataFrame
            
            # Handle DefineTransformation fields only if we detected transform a2-a7 fields
            elif base == "option" and has_transform_a_fields:
                # Keep option as string for DefineTransformation
                transform_option = str(v) if v is not None else None
                continue
            elif base in ("a2", "a3", "a4", "a5", "a6", "a7"):
                if base == "a2":
                    transform_a2 = self._to_number(v)
                elif base == "a3":
                    transform_a3 = self._to_number(v)
                elif base == "a4":
                    transform_a4 = self._to_number(v)
                elif base == "a5":
                    transform_a5 = self._to_number(v)
                elif base == "a6":
                    transform_a6 = self._to_number(v)
                elif base == "a7":
                    transform_a7 = self._to_number(v)
                continue
            
            # Handle fields that PyDyna expects as string representation of integers
            # These must be converted from int to string BEFORE setting on keyword
            # EXCEPTION: SetSegment.its expects int, not string, so skip conversion
            if base in int_to_string_fields and not skip_its_conversion:
                if v is None or v == "None" or v == "":
                    normalized[base] = None
                else:
                    # Convert to string (e.g., 1 -> "1")
                    normalized[base] = str(int(v)) if isinstance(v, (int, float)) else str(v)
                continue
            
            # Keep string fields as strings, convert others to numbers
            if base in string_fields:
                # Handle "None" string as actual None, and empty strings as None
                if v is None or v == "None" or v == "":
                    normalized[base] = None
                else:
                    normalized[base] = str(v)
            else:
                normalized[base] = self._to_number(v)
        
        # If we have a1 and o1 data (lists), create curves DataFrame for DefineCurve
        if a1_data is not None or o1_data is not None:
            # Ensure a1_list and o1_list are always lists
            if a1_data is None or self._is_null_value(a1_data):
                a1_list = [0.0]
            elif isinstance(a1_data, (list, tuple)):
                a1_list = list(a1_data)
            else:
                # Check if it's a null-like string before converting
                try:
                    a1_list = [float(a1_data)]  # Single value, wrap in list
                except (ValueError, TypeError):
                    a1_list = [0.0]
            
            if o1_data is None or self._is_null_value(o1_data):
                o1_list = [0.0]
            elif isinstance(o1_data, (list, tuple)):
                o1_list = list(o1_data)
            else:
                # Check if it's a null-like string before converting
                try:
                    o1_list = [float(o1_data)]  # Single value, wrap in list
                except (ValueError, TypeError):
                    o1_list = [0.0]
            
            # Pad to same length if needed
            max_len = max(len(a1_list), len(o1_list))
            while len(a1_list) < max_len:
                a1_list.append(0.0)
            while len(o1_list) < max_len:
                o1_list.append(0.0)
            normalized["curves"] = pd.DataFrame({"a1": a1_list, "o1": o1_list})
        
        # If we have transform data, create transforms DataFrame for DefineTransformation
        if has_transform_a_fields:
            transforms_df = None
            
            # Priority 1: Use directly stored transforms DataFrame (multiple rows)
            if transforms_df_direct is not None:
                transforms_df = transforms_df_direct
            # Priority 2: Check if option contains JSON-encoded multiple transforms (legacy)
            elif transform_option is not None and isinstance(transform_option, str) and transform_option.strip().startswith('['):
                try:
                    import json
                    transforms_list = json.loads(transform_option)
                    if isinstance(transforms_list, list) and len(transforms_list) > 0:
                        transforms_df = pd.DataFrame(transforms_list)
                        # Ensure all required columns exist
                        for col in ['option', 'a1', 'a2', 'a3', 'a4', 'a5', 'a6', 'a7']:
                            if col not in transforms_df.columns:
                                transforms_df[col] = 0.0 if col != 'option' else 'TRANSL'
                except (json.JSONDecodeError, ValueError):
                    pass
            
            # Priority 3: Single-row transform from individual fields
            if transforms_df is None and transform_option is not None:
                transforms_data = {
                    "option": [str(transform_option)],  # Ensure it's a string
                    "a1": [transform_a1 if transform_a1 is not None else 0.0],
                    "a2": [transform_a2 if transform_a2 is not None else 0.0],
                    "a3": [transform_a3 if transform_a3 is not None else 0.0],
                    "a4": [transform_a4 if transform_a4 is not None else 0.0],
                    "a5": [transform_a5 if transform_a5 is not None else 0.0],
                    "a6": [transform_a6 if transform_a6 is not None else 0.0],
                    "a7": [transform_a7 if transform_a7 is not None else 0.0],
                }
                transforms_df = pd.DataFrame(transforms_data)
            
            if transforms_df is not None:
                normalized["transforms"] = transforms_df
        
        # Include nodes DataFrame if extracted from JSON
        if nodes_df is not None:
            normalized["nodes"] = nodes_df
        
        # Include elements DataFrame if extracted from JSON
        if elements_df is not None:
            normalized["elements"] = elements_df
        
        # Include parts DataFrame if extracted from JSON
        if parts_df is not None:
            normalized["parts"] = parts_df
        
        # Include segments DataFrame if extracted from JSON
        if segments_df is not None:
            normalized["segments"] = segments_df
        
        # Convert legacy __set_nodes_list__ / __set_parts_list__ to generic __tablecard_ format
        # This ensures backward compatibility with existing DB records
        for legacy_key, prop_name in [("__set_nodes_list__", "nodes"), ("__set_parts_list__", "parts")]:
            list_data = None
            if legacy_key == "__set_nodes_list__":
                list_data = set_nodes_list_data
            else:
                list_data = set_parts_list_data
            
            # Skip if already handled by generic __tablecard_ flow
            if f"__tablecard_{prop_name}__" in normalized:
                continue
                
            if list_data is not None:
                # Convert list to single-column DataFrame for generic tablecard flow
                col_name = legacy_key.replace("__set_", "").replace("_list__", "")  # "nodes" or "parts"
                tc_df = pd.DataFrame({col_name: list_data})
                normalized[f"__tablecard_{prop_name}__"] = tc_df
            elif legacy_key in params:
                value = params[legacy_key]
                if isinstance(value, str) and value.startswith('hdf5:'):
                    pass  # Skip - couldn't be resolved from HDF5
                elif isinstance(value, str):
                    try:
                        import json
                        value = json.loads(value)
                    except Exception:
                        value = self._parse_list(value)
                    if value and isinstance(value, list):
                        col_name = legacy_key.replace("__set_", "").replace("_list__", "")
                        tc_df = pd.DataFrame({col_name: value})
                        normalized[f"__tablecard_{prop_name}__"] = tc_df
                elif isinstance(value, list):
                    col_name = legacy_key.replace("__set_", "").replace("_list__", "")
                    tc_df = pd.DataFrame({col_name: value})
                    normalized[f"__tablecard_{prop_name}__"] = tc_df
        
        # Convert legacy __parameters_series__ to generic __tablecard_parameters__ format
        # This handles old DB records and imported .k files
        if "__tablecard_parameters__" not in normalized:
            if "__parameters_series__" in params:
                value = params["__parameters_series__"]
                try:
                    if isinstance(value, str):
                        import json
                        value = json.loads(value)
                    if isinstance(value, list) and value:
                        # Convert list of dicts [{name, value/val}, ...] to DataFrame
                        rows = []
                        for item in value:
                            if isinstance(item, dict):
                                rows.append({
                                    'name': str(item.get('name', '')),
                                    'val': str(item.get('value', item.get('val', '')))
                                })
                        if rows:
                            normalized["__tablecard_parameters__"] = pd.DataFrame(rows)
                except Exception:
                    pass
            
            elif "__parameters_table__" in params:
                value = params["__parameters_table__"]
                try:
                    tc_df = None
                    if isinstance(value, str):
                        import json
                        parsed = json.loads(value)
                        if isinstance(parsed, dict) and 'columns' in parsed:
                            tc_df = pd.DataFrame(parsed['data'], columns=parsed['columns'])
                    elif isinstance(value, dict) and 'columns' in value:
                        tc_df = pd.DataFrame(value['data'], columns=value['columns'])
                    elif hasattr(value, 'columns'):
                        tc_df = value
                    if tc_df is not None and len(tc_df) > 0:
                        normalized["__tablecard_parameters__"] = tc_df
                except Exception:
                    pass
        
        # Convert Material Library __param_N_type/name/value__ format to TableCard DataFrame
        # This handles PARAMETER records created via Material Library that only have
        # __param_0_type__, __param_0_name__, __param_0_value__ keys (no __tablecard_*_data__)
        if "__tablecard_parameters__" not in normalized:
            param_count = None
            # Detect __param_N_xxx__ keys
            for k in params:
                if isinstance(k, str) and k.startswith("__param_") and k.endswith("__"):
                    import re as _re
                    m = _re.match(r'^__param_(\d+)_(type|name|value|expression)__$', k)
                    if m:
                        idx = int(m.group(1))
                        if param_count is None or idx + 1 > param_count:
                            param_count = idx + 1
            
            if param_count is None:
                # Also check explicit __param_count__
                pc = params.get('__param_count__')
                if pc is not None:
                    try:
                        param_count = int(pc)
                    except (ValueError, TypeError):
                        pass
            
            if param_count and param_count > 0:
                entries = []
                for idx in range(param_count):
                    ptype = str(params.get(f'__param_{idx}_type__', 'R')).strip() or 'R'
                    pname = str(params.get(f'__param_{idx}_name__', '')).strip()
                    pvalue = str(params.get(f'__param_{idx}_value__', '')).strip()
                    pexpr = str(params.get(f'__param_{idx}_expression__', '')).strip()
                    # LS-DYNA rule: parameter name max 8 chars (PRMR field = 10 chars:
                    # 1 type char + up to 8 name chars + trailing space).
                    # Names longer than 8 chars exceed the fixed-width card format and
                    # will trigger "Detected out of bound card characters" in PyDyna.
                    if len(pname) > 8:
                        logger.warning(
                            f"Parameter name '{pname}' is {len(pname)} characters "
                            f"(LS-DYNA max is 8). It will be truncated by LS-DYNA "
                            f"and may cause import warnings."
                        )
                    entries.append((ptype, pname, pvalue, pexpr))
                
                # Determine column layout from keyword class name
                cn_lower = (class_name or "").lower()
                if 'expression' in cn_lower:
                    # prmr, expression — one entry per row
                    rows = [{'prmr': f"{t}{n}" if n else '', 'expression': e if e else v}
                            for t, n, v, e in entries]
                    tc_df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=['prmr', 'expression'])
                elif 'type' in cn_lower and 'duplication' not in cn_lower:
                    # prmr, val, prtyp — one entry per row
                    rows = [{'prmr': f"{t}{n}" if n else '', 'val': v, 'prtyp': '   '}
                            for t, n, v, _e in entries]
                    tc_df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=['prmr', 'val', 'prtyp'])
                else:
                    # PARAMETER, LOCAL, NOECHO — 4 pairs per row
                    rows = []
                    for cs in range(0, len(entries), 4):
                        row = {}
                        for j in range(4):
                            ci = j + 1
                            if cs + j < len(entries):
                                t, n, v, _e = entries[cs + j]
                                row[f'prmr{ci}'] = f"{t}{n}" if n else ''
                                row[f'val{ci}'] = v
                            else:
                                row[f'prmr{ci}'] = ''
                                row[f'val{ci}'] = ''
                        rows.append(row)
                    cols = ['prmr1', 'val1', 'prmr2', 'val2', 'prmr3', 'val3', 'prmr4', 'val4']
                    tc_df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
                
                if len(tc_df) > 0:
                    normalized["__tablecard_parameters__"] = tc_df
                    logger.info(f"Converted {param_count} __param_N_xxx__ entries to TableCard DataFrame for {class_name}")
        
        return normalized, selected_opts

    def _activate_options(self, keyword_obj, selected_opts: list[str] | None):
        if not selected_opts:
            return
        # Activate options via adapter (wraps keyword OptionSpec API)
        for opt in selected_opts:
            pydyna.activate_option(keyword_obj, opt)

    def _reformat_define_table(self, raw_content: str) -> str:
        """Re-format DEFINE_TABLE raw content to use proper LS-DYNA column widths.
        
        LS-DYNA DEFINE_TABLE format (after the header card):
          - VALUE field: 20 characters wide (columns 1-20), right-justified
          - LCID  field: 20 characters wide (columns 21-40), right-justified
        Values use decimal notation when in normal range, scientific for extreme.
        """
        lines = raw_content.split('\n')
        result = []
        in_data_section = False
        
        for line in lines:
            stripped = line.strip()
            # Detect the value/lcid comment header
            if stripped.startswith('$') and 'value' in stripped.lower() and 'lcid' in stripped.lower():
                in_data_section = True
                result.append(line)
                continue
            
            # Pass through keyword line, comments, and header cards
            if not in_data_section or not stripped or stripped.startswith('$') or stripped.startswith('*'):
                result.append(line)
                continue
            
            # Parse data line: VALUE LCID (possibly with & parameter references)
            parts = stripped.split()
            if len(parts) >= 2:
                raw_val, raw_lcid = parts[0], parts[1]
                # Format VALUE (20 chars)
                if raw_val.startswith('&'):
                    value_str = f"{raw_val:>20}"
                else:
                    try:
                        fval = float(raw_val)
                        if fval == 0 or (1e-4 <= abs(fval) < 1e10):
                            value_str = f"{fval:>20.3f}"
                        else:
                            value_str = f"{fval:>20.6E}"
                    except ValueError:
                        value_str = f"{raw_val:>20}"
                # Format LCID (20 chars)
                if raw_lcid.startswith('&'):
                    lcid_str = f"{raw_lcid:>20}"
                else:
                    try:
                        lcid_str = f"{int(float(raw_lcid)):>20d}"
                    except (ValueError, TypeError):
                        lcid_str = f"{raw_lcid:>20}"
                result.append(f"{value_str}{lcid_str}")
            else:
                result.append(line)
        
        return '\n'.join(result)

    def _build_keyword(self, class_name: str, params: dict, chain_hint: list[str] | None = None, node_id: int = None):
        # Handle raw content keywords (imported as string_keywords from PyDyna)
        # These are keywords that PyDyna couldn't parse, like *DEFINE_TABLE_TITLE
        if '__raw_content__' in params:
            raw_content = params['__raw_content__']
            if raw_content and isinstance(raw_content, str):
                # Re-format DEFINE_TABLE to ensure proper 20+20 column widths
                if '*DEFINE_TABLE' in raw_content.upper():
                    raw_content = self._reformat_define_table(raw_content)
                return ('__raw__', raw_content)
        
        kw_cls, name_options = None, []
        # Prefer chain resolution when available — it encodes the full
        # keyword path (e.g. CONTROL → HOURGLASS → ControlHourglass) and
        # avoids collisions where a bare subkeyword matches a different
        # PyDyna class (e.g. "HOURGLASS" → kwd.Hourglass instead of
        # kwd.ControlHourglass).
        if chain_hint:
            kw_cls, name_options = self._class_from_chain(chain_hint)
        if not kw_cls:
            kw_cls, name_options = self._class_from_name(class_name)
        if not kw_cls:
            return None
        # Extract user comment before normalization (it's a dunder field, would be skipped)
        user_comment = params.get('__user_comment__', '') or ''
        hydrated, selected_opts = self._normalize_params(params, node_id=node_id, class_name=class_name)

        # Extract curves DataFrame if present (must be set after construction)
        curves_df = hydrated.pop("curves", None)
        
        # Extract transforms DataFrame if present (for DefineTransformation)
        transforms_df = hydrated.pop("transforms", None)
        
        # Extract nodes DataFrame if present (for Node keywords)
        nodes_df = hydrated.pop("nodes", None)
        
        # Extract elements DataFrame if present (for Element keywords)
        elements_df = hydrated.pop("elements", None)
        
        # Extract parts DataFrame if present (for Part keywords)
        parts_df = hydrated.pop("parts", None)
        
        # Extract segments DataFrame if present (for SetSegment keywords)
        segments_df = hydrated.pop("segments", None)
        
        # Extract generic TableCard DataFrames (for SET_SOLID set_entries, PARAMETER, etc.)
        tablecard_items = {}
        for k in list(hydrated.keys()):
            if k.startswith("__tablecard_") and k.endswith("__"):
                prop_name = k[len("__tablecard_"):-len("__")]
                val = hydrated.pop(k)
                # Only keep DataFrame values — skip marker strings like "[N rows - Click to edit]"
                if isinstance(val, pd.DataFrame):
                    tablecard_items[prop_name] = val
                elif val is not None:
                    logger.debug(f"Skipped non-DataFrame tablecard value for '{prop_name}': {type(val).__name__}")
        
        # Validate required fields for Include/IncludeTransform
        kw_name_lower = (class_name or "").lower()
        if "include" in kw_name_lower:
            filename = hydrated.get("filename")
            if filename is None or (isinstance(filename, str) and not filename.strip()):
                # Skip Include keywords without a valid filename
                return None

        # Check if any value contains LS-DYNA parameters (starts with "&")
        # If so, we need special handling since PyDyna may not support string values
        # in fields that expect integers
        has_lsdyna_params = any(
            isinstance(v, str) and v.startswith("&") 
            for v in hydrated.values()
        )
        
        # Collect LS-DYNA parameter fields for later raw text substitution
        lsdyna_param_fields = {}
        if has_lsdyna_params:
            lsdyna_param_fields = {
                k: v for k, v in hydrated.items()
                if isinstance(v, str) and v.startswith("&")
            }
            # Remove parametrized fields from hydrated and use placeholders
            # Placeholder: unique number that we'll replace in raw output
            for field_name in lsdyna_param_fields:
                hydrated.pop(field_name, None)
        
        try:
            kw_obj = kw_cls(**hydrated)
        except (TypeError, ValueError, AssertionError) as e:
            # AssertionError comes from PyDyna's TableCard._check_type when a non-DataFrame
            # value is passed to a property backed by a TableCard (e.g., "parameters")
            logger.warning(f"Construction failed for {class_name}: {e}. hydrated keys={list(hydrated.keys())}")
            # Retry with only simple scalar values (strip dicts, lists, DataFrames)
            safe_hydrated = {k: v for k, v in hydrated.items()
                            if isinstance(v, (str, int, float, type(None)))}
            try:
                kw_obj = kw_cls(**safe_hydrated)
                logger.info(f"Retry construction succeeded for {class_name} with {len(safe_hydrated)} safe params")
            except Exception:
                return None

        # Set curves DataFrame if present (for DEFINE_CURVE, etc.)
        if curves_df is not None and pydyna.has_keyword_attr(kw_obj, 'curves'):
            curves_df = self._clean_dataframe(curves_df)
            if curves_df is not None and len(curves_df) > 0:
                pydyna.set_keyword_curves(kw_obj, curves_df)
        
        # Set transforms DataFrame if present (for DEFINE_TRANSFORMATION)
        # Or set individual scalar fields for DefineTransform (which doesn't have 'transforms' attribute)
        if transforms_df is not None:
            transforms_df = self._clean_dataframe(transforms_df)
            if transforms_df is not None and len(transforms_df) > 0:
                if pydyna.has_keyword_attr(kw_obj, 'transforms'):
                    # DefineTransformation - uses DataFrame
                    pydyna.set_keyword_transforms(kw_obj, transforms_df)
                else:
                    # DefineTransform - uses individual scalar fields (option, a1-a7)
                    # Take the first row only (DefineTransform only supports single transform)
                    row = transforms_df.iloc[0]
                    for field in ['option', 'a1', 'a2', 'a3', 'a4', 'a5', 'a6', 'a7']:
                        if field in transforms_df.columns and pydyna.has_keyword_attr(kw_obj, field):
                            val = row.get(field)
                            # Check for None, np.nan, pd.NA, or string representations of None
                            if self._is_null_value(val):
                                # For option, set None; for numeric fields, don't set (PyDyna will use default)
                                if field == 'option':
                                    pass  # Don't set None option, PyDyna has default "MIRROR"
                                # For a1-a7, don't set - PyDyna writes empty field for None
                                continue
                            if field == 'option':
                                pydyna.set_keyword_attr(kw_obj, field, str(val))
                            else:
                                try:
                                    float_val = float(val)
                                    pydyna.set_keyword_attr(kw_obj, field, float_val)
                                except (ValueError, TypeError):
                                    # If conversion fails, don't set - let PyDyna use default
                                    pass
        
        # Set nodes DataFrame if present (for Node keywords)
        if nodes_df is not None and pydyna.has_keyword_attr(kw_obj, 'nodes'):
            nodes_df = self._clean_dataframe(nodes_df)
            if nodes_df is not None and len(nodes_df) > 0:
                nodes_df = self._sync_scalars_to_first_row(nodes_df, hydrated)
                pydyna.set_keyword_nodes(kw_obj, nodes_df)

        # Set elements DataFrame if present (for Element keywords)
        if elements_df is not None and pydyna.has_keyword_attr(kw_obj, 'elements'):
            elements_df = self._clean_dataframe(elements_df)
            if elements_df is not None and len(elements_df) > 0:
                elements_df = self._sync_scalars_to_first_row(elements_df, hydrated)
                pydyna.set_keyword_elements(kw_obj, elements_df)

        # Set parts DataFrame if present (for Part keywords)
        if parts_df is not None and pydyna.has_keyword_attr(kw_obj, 'parts'):
            parts_df = self._clean_dataframe(parts_df)
            if parts_df is not None and len(parts_df) > 0:
                parts_df = self._sync_scalars_to_first_row(parts_df, hydrated)
                pydyna.set_keyword_parts(kw_obj, parts_df)

        # Set segments DataFrame if present (for SetSegment keywords)
        if segments_df is not None and pydyna.has_keyword_attr(kw_obj, 'segments'):
            segments_df = self._clean_dataframe(segments_df)
            if segments_df is not None:
                if len(segments_df) > 0:
                    segments_df = self._sync_scalars_to_first_row(segments_df, hydrated)
                # Always assign the cleaned DataFrame, even if empty
                # Empty is valid - PyDyna can write SetSegment without segment rows
                pydyna.set_keyword_segments(kw_obj, segments_df)
        elif pydyna.has_keyword_attr(kw_obj, 'segments') and pydyna.get_keyword_segments(kw_obj) is not None:
            # Even if we didn't provide segments data, clean the default DataFrame
            # PyDyna may initialize segments with NA values that can't be written
            cleaned = self._clean_dataframe(pydyna.get_keyword_segments(kw_obj))
            if cleaned is not None:
                pydyna.set_keyword_segments(kw_obj, cleaned)

        # Set generic TableCard/SeriesCard/TableCardGroup properties
        # Handles SET_SOLID set_entries, PARAMETER, SetNodeList nodes, SetPartList parts, etc.
        # NOTE: Do NOT use _clean_dataframe here — it converts string columns
        # (like prmr1, prmr2...) to numeric, destroying parameter names.
        for prop_name, tc_df in tablecard_items.items():
            if pydyna.has_keyword_attr(kw_obj, prop_name):
                try:
                    if isinstance(tc_df, pd.DataFrame) and len(tc_df) > 0:
                        # Only remove rows that are completely empty/NA
                        tc_df = tc_df.copy()
                        check_df = tc_df.replace(['', 'None', 'none', 'null', 'NULL'], pd.NA)
                        mask = check_df.notna().any(axis=1)
                        tc_df = tc_df[mask].reset_index(drop=True)
                        
                        # Replace None/NaN with empty strings to prevent PyDyna
                        # from writing literal "None" in the output (e.g., PARAMETER
                        # cards with prmr1,val1 filled but prmr2-4,val2-4 empty)
                        import numpy as np
                        tc_df = tc_df.fillna('')
                        tc_df = tc_df.replace({None: '', 'None': '', 'none': '', 'nan': '', 'NaN': ''})
                        # Also replace numpy NaN that survived fillna
                        for col in tc_df.columns:
                            tc_df[col] = tc_df[col].apply(
                                lambda x: '' if (isinstance(x, float) and np.isnan(x)) else x
                            )
                        
                        if len(tc_df) > 0:
                            # Detect if target property is a SeriesCard (needs list, not DataFrame)
                            prop_val = pydyna.get_keyword_attr(kw_obj, prop_name)
                            is_series_card = pydyna.is_series_card_instance(prop_val)
                            
                            if is_series_card:
                                # SeriesCard: convert DataFrame to list
                                cols = list(tc_df.columns)
                                if len(cols) == 1:
                                    # Scalar SeriesCard (e.g., SetNodeList.nodes = [int])
                                    values = tc_df[cols[0]].tolist()
                                    # Try to convert to int for node/part IDs
                                    try:
                                        values = [int(float(v)) for v in values
                                                  if v is not None and str(v).strip() != '']
                                    except (ValueError, TypeError):
                                        pass
                                    pydyna.set_keyword_attr(kw_obj, prop_name, values)
                                else:
                                    # Struct SeriesCard (e.g., SectionShell.integration_points)
                                    # Convert each row to the inner dataclass
                                    inner_cls = getattr(type(kw_obj), type(kw_obj).__name__, None)
                                    if inner_cls is None:
                                        # Try common inner class naming: ClassName.ClassName
                                        for attr_name in dir(type(kw_obj)):
                                            attr = getattr(type(kw_obj), attr_name, None)
                                            if isinstance(attr, type) and hasattr(attr, '__dataclass_fields__'):
                                                # Check if fields match DataFrame columns
                                                dc_fields = {f.name for f in __import__('dataclasses').fields(attr)}
                                                if dc_fields == set(cols) or dc_fields.issuperset(set(cols)):
                                                    inner_cls = attr
                                                    break
                                    
                                    if inner_cls:
                                        items = []
                                        for _, row in tc_df.iterrows():
                                            items.append(inner_cls(**{c: row[c] for c in cols if c in row}))
                                        pydyna.set_keyword_attr(kw_obj, prop_name, items)
                                    else:
                                        # Fallback: try setting as list of dicts
                                        pydyna.set_keyword_attr(kw_obj, prop_name, tc_df.to_dict('records'))
                                logger.debug(f"Set SeriesCard property '{prop_name}' with {len(tc_df)} items")
                            else:
                                # TableCard or TableCardGroup: set DataFrame directly.
                                # fillna('') above converted NaN → ''.  PyDyna's TableCard
                                # setter expects numeric values (int/float) in data columns,
                                # so convert '' back to 0 as a safe numeric default.
                                tc_df_set = tc_df.replace('', 0)
                                pydyna.set_keyword_attr(kw_obj, prop_name, tc_df_set)
                                logger.debug(f"Set TableCard property '{prop_name}' with {len(tc_df_set)} rows")
                except Exception as e:
                    logger.warning(f"Error setting TableCard/SeriesCard property '{prop_name}': {e}")

        # Fix field types to ensure PyDyna compatibility
        self._fix_field_types(kw_obj)

        # Activate options detected from keyword name (e.g., TITLE from DEFINE_CURVE_TITLE)
        self._activate_options(kw_obj, name_options)
        
        # After constructing, activate selected options so OptionCardSet is written
        self._activate_options(kw_obj, selected_opts)
        
        # Set user comment (written as $ lines before keyword data in .k file)
        if user_comment and isinstance(user_comment, str) and user_comment.strip():
            try:
                kw_obj.user_comment = user_comment.strip()
            except Exception as e:
                logger.debug(f"Could not set user_comment on {class_name}: {e}")
        
        # If we have LS-DYNA parameter fields, generate raw text and substitute values
        if lsdyna_param_fields:
            return self._generate_raw_with_params(kw_obj, lsdyna_param_fields)
        
        return kw_obj

    def _as_raw_string(self, node_name: str, node_id: int, params: dict) -> str:
        lines = ["*KEYWORD", f"$ Node: {node_name} (id={node_id})"]
        # Filter out internal metadata fields (start with _ or __)
        filtered_params = {k: v for k, v in params.items() if not k.startswith("_")}
        if not filtered_params:
            lines.append("$ No params found")
        else:
            for k, v in filtered_params.items():
                lines.append(f"$ {k} = {v}")
        lines.append("*END")
        return "\n".join(lines)

    def _generate_raw_with_params(self, kw_obj, lsdyna_param_fields: dict):
        """
        Generate raw keyword text with LS-DYNA parameter substitutions.
        
        When a keyword has fields like vx="&vx" or mid="&xxx", PyDyna
        can't write string values in numeric fields.  This method:
        1. Uses PyDyna to generate the keyword text with default values.
        2. Parses the ``$#`` comment headers to discover which field names
           appear on each data line and at which byte offsets.
        3. Substitutes the parameter references at the correct positions.
        
        This header-based approach is robust against OptionCardSet title lines
        and other non-standard card ordering.
        
        Args:
            kw_obj: PyDyna keyword object
            lsdyna_param_fields: Dict of {field_name: parameter_value}
                                 e.g. ``{'vx': '&vx', 'vy': '&vy'}``
            
        Returns:
            Tuple ``('__raw__', raw_text)`` for direct deck append
        """
        # ── 1. Generate baseline text via PyDyna ──
        temp_deck = pydyna.create_deck()
        pydyna.append_to_deck(temp_deck, kw_obj)
        raw_text = pydyna.write_deck(temp_deck)
        
        # Remove *KEYWORD / *END wrappers (the main deck adds them)
        lines = raw_text.split('\n')
        filtered_lines = [
            ln for ln in lines
            if ln.strip().upper() not in ('*KEYWORD', '*END')
        ]
        
        # ── 2. Walk lines, using $# headers to identify field positions ──
        # Each ``$#`` comment line lists field names in 10-char columns.
        # The very next non-empty, non-comment line is the matching data line.
        remaining_params = dict(lsdyna_param_fields)  # copy to track consumed
        result_lines = []
        pending_subs = None   # list of (byte_offset, width, param_value) for next data line
        
        for line in filtered_lines:
            stripped = line.strip()
            
            # ── $# comment header → parse field names for upcoming data line ──
            if stripped.startswith('$#'):
                result_lines.append(line)
                # Parse field names from the header.
                # Format: "$#field0   field1    field2 ..."
                # Each field occupies a 10-char column; first column overlaps "$#".
                pending_subs = []
                n_cols = max(len(line), 80) // 10
                for col_idx in range(n_cols):
                    start = col_idx * 10
                    end = start + 10
                    chunk = line[start:end] if start < len(line) else ''
                    # First column starts with "$#"
                    if col_idx == 0:
                        chunk = chunk[2:]   # strip "$#"
                    name = chunk.strip().lower()
                    if name and name in remaining_params:
                        pending_subs.append(
                            (start, 10, remaining_params[name]))
                continue
            
            # ── Data line right after a $# header ──
            if pending_subs is not None:
                if stripped and not stripped.startswith('$') and not stripped.startswith('*'):
                    if pending_subs:  # we have substitutions to apply
                        modified = line
                        if len(modified) < 80:
                            modified = modified.ljust(80)
                        for byte_off, width, param_value in pending_subs:
                            new_field = f"{param_value:>{width}}"
                            modified = (modified[:byte_off] + new_field
                                        + modified[byte_off + width:])
                            logger.debug(
                                f"Substituted [{byte_off}:{byte_off+width}] "
                                f"-> '{new_field}'")
                        result_lines.append(modified.rstrip())
                    else:
                        result_lines.append(line)
                    pending_subs = None
                    continue
                # If it's another comment or keyword line, discard pending
                pending_subs = None
            
            result_lines.append(line)
        
        raw_text = '\n'.join(result_lines).strip()
        return ('__raw__', raw_text)

    def build_all(self, project_id: int, output_root: Path, progress_callback=None) -> list[Path]:
        """
        Build .k files for all roots in a project.
        
        Uses hybrid storage for efficient handling of massive data.
        
        Args:
            project_id: Project ID
            output_root: Root directory for output
            progress_callback: Optional callback (current, total, message)
            
        Returns:
            List of paths to generated .k files
        """
        # Initialize data handler for this export session
        self._init_data_handler(project_id)
        
        try:
            return self._build_all_internal(project_id, output_root, progress_callback)
        finally:
            # Clean up
            self._cleanup_data_handler()

    def get_root_id_for_filename(self, project_id: int, k_filename: str) -> int | None:
        """Find the root node ID that corresponds to a .k filename.
        
        Args:
            project_id: Project ID
            k_filename: The .k filename (e.g., "MySource.k")
            
        Returns:
            The root node ID, or None if not found.
        """
        # Remove .k extension if present
        base_name = k_filename
        if base_name.lower().endswith(".k"):
            base_name = base_name[:-2]
        
        nodes = self.project_vm.get_nodes(project_id)
        id_to_name = {nid: name for nid, _, name in nodes}
        roots = [nid for nid, pid, _ in nodes if pid is None]
        
        for root_id in roots:
            root_name = id_to_name.get(root_id, 'root')
            expected_slug = self._slug(root_name, str(root_id))
            if expected_slug == base_name:
                return root_id
        return None

    def build_single(self, project_id: int, output_root: Path, k_filename: str, 
                     progress_callback=None) -> Path | None:
        """Build a single .k file for a specific root in a project.
        
        Args:
            project_id: Project ID
            output_root: Root directory for output
            k_filename: The .k filename to regenerate (e.g., "MySource.k")
            progress_callback: Optional callback (current, total, message)
            
        Returns:
            Path to the generated .k file, or None if the root was not found.
        """
        # Find the root_id for this filename
        root_id = self.get_root_id_for_filename(project_id, k_filename)
        if root_id is None:
            return None
        
        # Initialize data handler for this export session
        self._init_data_handler(project_id)
        
        try:
            return self._build_single_internal(project_id, output_root, root_id, progress_callback)
        finally:
            # Clean up
            self._cleanup_data_handler()

    def _build_single_internal(self, project_id: int, output_root: Path, root_id: int,
                               progress_callback=None) -> Path:
        """Internal method to build a single .k file for a specific root."""
        nodes = self.project_vm.get_nodes(project_id)
        id_to_parent = {nid: pid for nid, pid, _ in nodes}
        id_to_name = {nid: name for nid, _, name in nodes}
        # Build children dict – children are already in sort_order because
        # get_nodes() returns ORDER BY sort_order
        children = {}
        for nid, pid, _ in nodes:
            children.setdefault(pid, []).append(nid)

        export_dir = self._export_dir_for(project_id, output_root)

        def ancestors(nid):
            chain = []
            cur = nid
            while cur is not None:
                chain.append(cur)
                cur = id_to_parent.get(cur)
            return list(reversed(chain))

        def keyword_name_for_leaf(leaf_id):
            chain = ancestors(leaf_id)
            names = [id_to_name[i] for i in chain]
            filtered = [n for n in names if ":" not in n]
            if not filtered:
                return id_to_name.get(leaf_id)
            if len(filtered) > 1:
                filtered = filtered[1:]
            if filtered:
                last_name = filtered[-1]
                if last_name.upper() == "GENERAL" and len(filtered) > 1:
                    last_name = filtered[-2]
                # Special case: PARAMETER family needs GROUP_KEYWORD format
                # e.g., ['PARAMETER', 'EXPRESSION'] -> 'PARAMETER_EXPRESSION'
                elif len(filtered) >= 2 and filtered[0].upper() == "PARAMETER" and last_name.upper() != "PARAMETER":
                    last_name = "_".join(filtered)
                if last_name.endswith("General"):
                    last_name = last_name[:-7]
                return last_name
            return id_to_name.get(leaf_id)

        export_dir.mkdir(parents=True, exist_ok=True)

        # Special fields that DO count as "real parameters" even though they start with __
        allowed_dunder = {"__selected_option_specs__", "__nodes_dataframe__", "__elements_dataframe__", 
                         "__parts_dataframe__", "__segments_dataframe__", "__transforms_dataframe__",
                         "__raw_content__", "__original_name__", "__table_points__",
                         "__set_nodes_list__", "__set_parts_list__",
                         "__set_nodes_count__", "__set_parts_count__"}
        
        def is_real_param(k):
            """Check if a parameter key should be considered as real (not internal)."""
            if not k.startswith("_"):
                return True
            if k in allowed_dunder:
                return True
            # Allow __tablecard_<prop>__ markers and __tablecard_<prop>_data__ storage
            if k.startswith('__tablecard_') and k.endswith('__'):
                return True
            return False
        
        # Report progress
        if progress_callback:
            root_name = id_to_name.get(root_id, 'root')
            progress_callback(0, 1, f"Building {root_name}.k")

        deck = pydyna.create_deck()

        # DFS traversal from root_id to collect leaf records in correct
        # tree-hierarchy order (respects sibling sort_order at every level)
        def _dfs_leaves(start_id):
            """Yield (leaf_id, params) for ID: record nodes under start_id in DFS tree order."""
            stack = [start_id]
            while stack:
                nid = stack.pop()
                name = id_to_name.get(nid, '')
                if name.startswith("ID:"):
                    params = self.project_vm.get_params(nid) or {}
                    real_params = {k: v for k, v in params.items() if is_real_param(k)}
                    if real_params:
                        yield (nid, params)
                # Push children in reverse so first child (lowest sort_order) is popped first
                for kid in reversed(children.get(nid, [])):
                    stack.append(kid)

        for leaf_id, params in _dfs_leaves(root_id):
            kw_name = keyword_name_for_leaf(leaf_id)
            chain = ancestors(leaf_id)
            chain_names = [id_to_name[i] for i in chain if ":" not in id_to_name[i]]
            keyword_obj = self._build_keyword(kw_name, params, chain_hint=chain_names, node_id=leaf_id)
            if keyword_obj is not None:
                # Handle raw content keywords (returned as tuple ('__raw__', content))
                if isinstance(keyword_obj, tuple) and keyword_obj[0] == '__raw__':
                    pydyna.append_to_deck(deck, keyword_obj[1])  # append string directly
                else:
                    pydyna.append_to_deck(deck, keyword_obj)

        fname = f"{self._slug(id_to_name.get(root_id, 'root'), str(root_id))}.k"
        out_path = export_dir / fname
        try:
            content = pydyna.write_deck(deck) if pydyna.deck_has_content(deck) else "*KEYWORD\n*END\n"
        except TypeError as e:
            error_msg = f"Error writing deck: {e}\n"
            for kw in pydyna.get_parsed_keywords(deck):
                error_msg += f"  Keyword: {pydyna.get_keyword_type_name(kw)}\n"
            raise TypeError(error_msg) from e
        out_path.write_text(content, encoding="utf-8")

        # Final progress update
        if progress_callback:
            progress_callback(1, 1, "Complete")

        return out_path
    
    def _build_all_internal(self, project_id: int, output_root: Path, progress_callback=None) -> list[Path]:
        """Internal method to build all .k files."""
        nodes = self.project_vm.get_nodes(project_id)
        id_to_parent = {nid: pid for nid, pid, _ in nodes}
        id_to_name = {nid: name for nid, _, name in nodes}
        children = {}
        for nid, pid, _ in nodes:
            children.setdefault(pid, []).append(nid)

        roots = [nid for nid, pid, _ in nodes if pid is None]

        export_dir = self._export_dir_for(project_id, output_root)
        paths: list[Path] = []

        def ancestors(nid):
            chain = []
            cur = nid
            while cur is not None:
                chain.append(cur)
                cur = id_to_parent.get(cur)
            return list(reversed(chain))

        def keyword_name_for_leaf(leaf_id):
            chain = ancestors(leaf_id)
            names = [id_to_name[i] for i in chain]
            # Use intermediate names without IDs like "ID:123"
            filtered = [n for n in names if ":" not in n]
            if not filtered:
                return id_to_name.get(leaf_id)
            # Drop the root if there is more than one element
            if len(filtered) > 1:
                filtered = filtered[1:]
            
            # Para keywords como Part y Node, la cadena es:
            # ['PART', 'GENERAL'] o ['NODE', 'GENERAL']
            # En ese caso queremos 'PART' o 'NODE', no 'GENERAL'
            # 
            # Para Elements la cadena es:
            # ['ELEMENT', 'SHELL'] o ['ELEMENT', 'SOLID']
            # En ese caso queremos 'SHELL' o 'SOLID'
            #
            # Para PARAMETER_EXPRESSION la cadena es:
            # ['PARAMETER', 'EXPRESSION']
            # En ese caso queremos 'PARAMETER_EXPRESSION' (combinar ambos)
            if filtered:
                last_name = filtered[-1]
                
                # Si el último es "GENERAL", usar el penúltimo que es el tipo real
                if last_name.upper() == "GENERAL" and len(filtered) > 1:
                    last_name = filtered[-2]
                # Special case: PARAMETER family needs GROUP_KEYWORD format
                # e.g., ['PARAMETER', 'EXPRESSION'] -> 'PARAMETER_EXPRESSION'
                elif len(filtered) >= 2 and filtered[0].upper() == "PARAMETER" and last_name.upper() != "PARAMETER":
                    # Combine PARAMETER + EXPRESSION -> PARAMETER_EXPRESSION
                    last_name = "_".join(filtered)
                # Special case: DEFINE family needs GROUP_KEYWORD format
                # e.g., ['DEFINE', 'TABLE'] -> 'DEFINE_TABLE'
                # e.g., ['DEFINE', 'CURVE'] -> 'DEFINE_CURVE'
                # e.g., ['DEFINE', 'TRANSFORMATION'] -> 'DEFINE_TRANSFORMATION'
                elif len(filtered) >= 2 and filtered[0].upper() == "DEFINE" and last_name.upper() != "DEFINE":
                    last_name = "_".join(filtered)
                
                # Quitar sufijo "General" si existe (PartGeneral -> Part, NodeGeneral -> Node)
                if last_name.endswith("General"):
                    last_name = last_name[:-7]  # Remove "General"
                return last_name
            
            return id_to_name.get(leaf_id)

        export_dir.mkdir(parents=True, exist_ok=True)

        # Campos especiales que SÍ cuentan como "parámetros reales" aunque empiecen con __
        allowed_dunder = {"__selected_option_specs__", "__nodes_dataframe__", "__elements_dataframe__", "__parts_dataframe__", "__segments_dataframe__", "__transforms_dataframe__", "__raw_content__", "__original_name__", "__table_points__", "__set_nodes_list__", "__set_parts_list__", "__set_nodes_count__", "__set_parts_count__"}
        
        def is_real_param(k):
            """Check if a parameter key should be considered as real (not internal)."""
            if not k.startswith("_"):
                return True
            if k in allowed_dunder:
                return True
            # Allow __tablecard_<prop>__ markers and __tablecard_<prop>_data__ storage
            if k.startswith('__tablecard_') and k.endswith('__'):
                return True
            return False
        
        # DFS traversal to collect leaf records in correct tree-hierarchy order
        # (respects sibling sort_order at every level)
        def _dfs_leaves(start_id):
            """Yield (leaf_id, params) for ID: record nodes under start_id in DFS tree order."""
            stack = [start_id]
            while stack:
                nid = stack.pop()
                name = id_to_name.get(nid, '')
                if name.startswith("ID:"):
                    params = self.project_vm.get_params(nid) or {}
                    real_params = {k: v for k, v in params.items() if is_real_param(k)}
                    if real_params:
                        yield (nid, params)
                # Push children in reverse so first child (lowest sort_order) is popped first
                for kid in reversed(children.get(nid, [])):
                    stack.append(kid)

        total_roots = len(roots)
        for idx, root_id in enumerate(roots):
            # Report progress for each root file
            if progress_callback:
                root_name = id_to_name.get(root_id, 'root')
                progress_callback(idx, total_roots, f"Building {root_name}.k")
            
            deck = pydyna.create_deck()

            # Collect leaves under this root in tree-hierarchy DFS order
            for leaf_id, params in _dfs_leaves(root_id):
                kw_name = keyword_name_for_leaf(leaf_id)
                chain = ancestors(leaf_id)
                chain_names = [id_to_name[i] for i in chain if ":" not in id_to_name[i]]
                keyword_obj = self._build_keyword(kw_name, params, chain_hint=chain_names, node_id=leaf_id)
                if keyword_obj is not None:
                    # Handle raw content keywords (returned as tuple ('__raw__', content))
                    if isinstance(keyword_obj, tuple) and keyword_obj[0] == '__raw__':
                        pydyna.append_to_deck(deck, keyword_obj[1])  # append string directly
                    else:
                        pydyna.append_to_deck(deck, keyword_obj)
                # Si no se pudo construir el keyword, NO escribir raw string
                # Los nodos intermedios (NodeGeneral, PartGeneral) se ignoran

            fname = f"{self._slug(id_to_name.get(root_id, 'root'), str(root_id))}.k"
            out_path = export_dir / fname
            try:
                content = pydyna.write_deck(deck) if pydyna.deck_has_content(deck) else "*KEYWORD\n*END\n"
            except (TypeError, AssertionError) as e:
                # Debug: identify which keyword is causing the issue
                error_msg = f"Error writing deck for '{fname}': {type(e).__name__}: {e}\n"
                for kw in pydyna.get_parsed_keywords(deck):
                    error_msg += f"  Keyword: {pydyna.get_keyword_type_name(kw)}\n"
                    for attr in dir(kw):
                        if not attr.startswith('_'):
                            try:
                                val = pydyna.get_keyword_attr(kw, attr)
                                if not callable(val):
                                    val_repr = repr(val)[:100]
                                    error_msg += f"    {attr} = {val_repr} (type: {type(val).__name__})\n"
                            except:
                                pass
                logger.error(error_msg)
                raise type(e)(error_msg) from e
            out_path.write_text(content, encoding="utf-8")
            paths.append(out_path)
        
        # Final progress update
        if progress_callback:
            progress_callback(total_roots, total_roots, "Complete")

        return paths
