# ui/dialogs/tablecard_editor.py
"""
Generic dialog for editing TableCard data (N rows with dynamic columns).
Used for any keyword that has a TableCard structure (SET_*, NODE, etc.)
except DEFINE_CURVE (which has its own CurveEditorDialog).
"""
import pandas as pd
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QDialogButtonBox, QToolBar, QMessageBox, QFileDialog,
    QPushButton, QSizePolicy, QComboBox
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont
from ui.styles import TOOLBAR_BUTTON_STYLE, TOOLBAR_STYLE, apply_dialog_theme


class TableCardEditorDialog(QDialog):
    """Generic dialog for editing TableCard data as a table with N rows.
    
    Supports dynamic columns extracted from the TableCard's _fields.
    Each column has a name, type (int/float/str), and default value.
    """
    
    def __init__(self, columns, data=None, title="Edit Table Data", parent=None):
        """Initialize the dialog.
        
        Args:
            columns: List of dicts with column info:
                [{"name": "k1", "type": int, "default": 0}, ...]
            data: Initial data as pd.DataFrame, list of dicts, or None.
            title: Dialog window title.
            parent: Parent widget.
        """
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(600, 450)
        self._columns = columns
        
        layout = QVBoxLayout(self)
        
        # Info label showing column count and types
        type_map = {int: "int", float: "float", str: "str"}
        col_summary = ", ".join(
            f"{c['name']}({type_map.get(c.get('type', float), '?')})" 
            for c in columns[:6]
        )
        if len(columns) > 6:
            col_summary += f" ... (+{len(columns) - 6} more)"
        info_label = QLabel(f"Columns: {col_summary}")
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #666; font-size: 11px; margin-bottom: 4px;")
        layout.addWidget(info_label)
        
        # Toolbar
        toolbar = QToolBar()
        toolbar.setStyleSheet(TOOLBAR_STYLE)
        
        self.action_add = QAction("Add Row", self)
        self.action_add.triggered.connect(self._add_row)
        toolbar.addAction(self.action_add)
        
        self.action_insert = QAction("Insert Row", self)
        self.action_insert.setToolTip("Insert a new row at current cursor position")
        self.action_insert.triggered.connect(self._insert_row)
        toolbar.addAction(self.action_insert)
        
        self.action_remove = QAction("Remove Row", self)
        self.action_remove.triggered.connect(self._remove_row)
        toolbar.addAction(self.action_remove)
        
        toolbar.addSeparator()
        
        self.action_clear = QAction("Clear All", self)
        self.action_clear.triggered.connect(self._clear_all)
        toolbar.addAction(self.action_clear)
        
        toolbar.addSeparator()
        
        # Paste from clipboard
        self.action_paste = QAction("📋 Paste", self)
        self.action_paste.setToolTip("Paste tabular data from clipboard (tab/space separated)")
        self.action_paste.triggered.connect(self._paste_from_clipboard)
        toolbar.addAction(self.action_paste)
        
        layout.addWidget(toolbar)
        
        # Table widget
        self.table = QTableWidget()
        self.table.setColumnCount(len(columns))
        
        # Set headers from column names
        headers = [c["name"] for c in columns]
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setStretchLastSection(True)
        
        # If few columns, stretch all; otherwise resize to contents
        if len(columns) <= 8:
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        else:
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
            self.table.horizontalHeader().setStretchLastSection(True)
        
        layout.addWidget(self.table)
        
        # Row count label
        self._row_count_label = QLabel("0 rows")
        self._row_count_label.setStyleSheet("color: #888; font-size: 10px;")
        layout.addWidget(self._row_count_label)
        
        # Dialog buttons
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.setStyleSheet(TOOLBAR_BUTTON_STYLE)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
        
        # Connect row count update
        self.table.model().rowsInserted.connect(self._update_row_count)
        self.table.model().rowsRemoved.connect(self._update_row_count)
        
        # Load initial data
        self._load_data(data)
        self._update_row_count()
        apply_dialog_theme(self)
    
    def _update_row_count(self):
        """Update the row count label."""
        count = self.table.rowCount()
        self._row_count_label.setText(f"{count} row{'s' if count != 1 else ''}")
    
    def _get_default(self, col_idx):
        """Get default value string for a column."""
        col = self._columns[col_idx]
        default = col.get("default", 0)
        col_type = col.get("type", float)
        if col_type == int:
            return str(int(default)) if default is not None else "0"
        elif col_type == float:
            return str(float(default)) if default is not None else "0.0"
        else:
            return str(default) if default is not None else ""

    def _has_predefined(self, col_idx):
        """Check if a column has predefined values."""
        return bool(self._columns[col_idx].get("predefined_values"))

    def _create_combo(self, col_idx, current_value=None):
        """Create a QComboBox with predefined values for a column."""
        vals = self._columns[col_idx].get("predefined_values", [])
        combo = QComboBox()
        for v in vals:
            combo.addItem(v, v)
        if current_value is not None:
            idx = combo.findData(str(current_value))
            if idx >= 0:
                combo.setCurrentIndex(idx)
        return combo

    def _set_cell(self, row, col, text):
        """Set a cell value, using a QComboBox if the column has predefined values."""
        if self._has_predefined(col):
            combo = self._create_combo(col, str(text))
            self.table.setCellWidget(row, col, combo)
        else:
            self.table.setItem(row, col, QTableWidgetItem(str(text)))

    def _get_cell_text(self, row, col):
        """Get the text value from a cell (handles both QTableWidgetItem and QComboBox)."""
        widget = self.table.cellWidget(row, col)
        if isinstance(widget, QComboBox):
            return widget.currentText()
        item = self.table.item(row, col)
        return item.text().strip() if item else ""
    
    def _load_data(self, data):
        """Load data from various formats."""
        if data is None:
            self._add_row()
            return
        
        # Handle pd.DataFrame
        if isinstance(data, pd.DataFrame) and len(data) > 0:
            self.table.setRowCount(len(data))
            col_names = [c["name"] for c in self._columns]
            for i in range(len(data)):
                for j, col_name in enumerate(col_names):
                    if col_name in data.columns:
                        val = data.iloc[i][col_name]
                        text = self._format_value(val, j)
                    else:
                        text = self._get_default(j)
                    self._set_cell(i, j, text)
            return
        
        # Handle dict with 'columns' and 'data' (JSON split format)
        if isinstance(data, dict) and 'data' in data:
            col_names = data.get('columns', [c["name"] for c in self._columns])
            rows = data.get('data', [])
            if rows:
                self.table.setRowCount(len(rows))
                for i, row in enumerate(rows):
                    for j in range(len(self._columns)):
                        if j < len(row):
                            text = self._format_value(row[j], j)
                        else:
                            text = self._get_default(j)
                        self._set_cell(i, j, text)
                return
        
        # Handle list of dicts
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            self.table.setRowCount(len(data))
            col_names = [c["name"] for c in self._columns]
            for i, row_dict in enumerate(data):
                for j, col_name in enumerate(col_names):
                    val = row_dict.get(col_name, self._columns[j].get("default", 0))
                    text = self._format_value(val, j)
                    self._set_cell(i, j, text)
            return
        
        # Handle JSON string
        if isinstance(data, str):
            try:
                import json
                parsed = json.loads(data)
                if isinstance(parsed, dict) and 'data' in parsed:
                    self._load_data(parsed)
                    return
            except (json.JSONDecodeError, ValueError):
                pass
        
        # Default: add one empty row
        if self.table.rowCount() == 0:
            self._add_row()
    
    def _format_value(self, val, col_idx):
        """Format a value for display based on column type."""
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return self._get_default(col_idx)
        col_type = self._columns[col_idx].get("type", float)
        if col_type == int:
            try:
                return str(int(float(val)))
            except (ValueError, TypeError):
                return "0"
        elif col_type == float:
            try:
                return str(float(val))
            except (ValueError, TypeError):
                return "0.0"
        return str(val)
    
    def _add_row(self):
        """Add a new row at the end with default values."""
        row = self.table.rowCount()
        self.table.insertRow(row)
        for j in range(len(self._columns)):
            self._set_cell(row, j, self._get_default(j))
        self.table.setCurrentCell(row, 0)
    
    def _insert_row(self):
        """Insert a new row at the current cursor position."""
        current_row = self.table.currentRow()
        if current_row < 0:
            current_row = 0
        self.table.insertRow(current_row)
        for j in range(len(self._columns)):
            self._set_cell(current_row, j, self._get_default(j))
        self.table.setCurrentCell(current_row, 0)
    
    def _remove_row(self):
        """Remove all selected rows."""
        selected_rows = set()
        for item in self.table.selectedItems():
            selected_rows.add(item.row())
        if not selected_rows:
            current_row = self.table.currentRow()
            if current_row >= 0:
                selected_rows.add(current_row)
        for row in sorted(selected_rows, reverse=True):
            self.table.removeRow(row)
        if self.table.rowCount() == 0:
            self._add_row()
    
    def _clear_all(self):
        """Clear all rows and add one empty."""
        self.table.setRowCount(0)
        self._add_row()
    
    def _paste_from_clipboard(self):
        """Paste tabular data from clipboard (tab or space separated)."""
        from PySide6.QtWidgets import QApplication
        clipboard = QApplication.clipboard()
        text = clipboard.text()
        if not text or not text.strip():
            QMessageBox.information(self, "Paste", "Clipboard is empty.")
            return
        
        lines = text.strip().split('\n')
        new_rows = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('$'):
                continue
            # Split by tab first, then by whitespace
            if '\t' in line:
                parts = line.split('\t')
            else:
                parts = line.split()
            new_rows.append(parts)
        
        if not new_rows:
            QMessageBox.information(self, "Paste", "No valid data found in clipboard.")
            return
        
        # Ask whether to replace or append
        reply = QMessageBox.question(
            self, "Paste Data",
            f"Found {len(new_rows)} rows. Replace existing data or append?",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel
        )
        if reply == QMessageBox.Cancel:
            return
        
        start_row = 0
        if reply == QMessageBox.No:
            # Append
            start_row = self.table.rowCount()
        else:
            # Replace
            self.table.setRowCount(0)
            start_row = 0
        
        num_cols = len(self._columns)
        for row_data in new_rows:
            row_idx = self.table.rowCount()
            self.table.insertRow(row_idx)
            for j in range(num_cols):
                if j < len(row_data):
                    text = self._format_value(row_data[j], j)
                else:
                    text = self._get_default(j)
                self._set_cell(row_idx, j, text)
    
    def get_dataframe(self):
        """Get data as a pandas DataFrame with proper column types."""
        col_names = [c["name"] for c in self._columns]
        data = {name: [] for name in col_names}
        
        for row in range(self.table.rowCount()):
            for j, col_name in enumerate(col_names):
                raw = self._get_cell_text(row, j)
                col_type = self._columns[j].get("type", float)
                
                if col_type == int:
                    try:
                        data[col_name].append(int(float(raw)) if raw else 0)
                    except (ValueError, TypeError):
                        data[col_name].append(0)
                elif col_type == float:
                    try:
                        data[col_name].append(float(raw) if raw else 0.0)
                    except (ValueError, TypeError):
                        data[col_name].append(0.0)
                else:
                    data[col_name].append(raw)
        
        return pd.DataFrame(data)
    
    def get_json(self):
        """Get data as JSON string (orient='split') for DB storage."""
        df = self.get_dataframe()
        return df.to_json(orient='split')
    
    def get_row_count(self):
        """Get the number of data rows."""
        return self.table.rowCount()

    # ------------------------------------------------------------------
    # LS-DYNA parameter name validation
    # ------------------------------------------------------------------

    def _is_prmr_column(self, col_name: str) -> bool:
        """Return True if the column holds a PRMR (parameter name) field."""
        name = col_name.lower()
        return name == "prmr" or (name.startswith("prmr") and name[4:].isdigit())

    def _validate_prmr_names(self) -> list[str]:
        """Validate all prmr* cells and return a list of error messages.

        LS-DYNA rule: the PRMR field is 10 chars wide.
        Format is <type_char><name> where type_char is 1 char (R/I/C).
        → parameter name must be ≤ 8 characters (9 total with type char).
        Names longer than 8 chars will be silently truncated by LS-DYNA
        and will cause PyDyna to emit "Detected out of bound card characters".
        """
        MAX_NAME_LEN = 8
        errors = []
        prmr_col_indices = [
            j for j, c in enumerate(self._columns) if self._is_prmr_column(c["name"])
        ]
        if not prmr_col_indices:
            return errors

        for row in range(self.table.rowCount()):
            for j in prmr_col_indices:
                raw = self._get_cell_text(row, j).strip()
                if not raw:
                    continue
                # Strip leading type char (R/I/C) if present
                if len(raw) >= 1 and raw[0].upper() in ('R', 'I', 'C', ' '):
                    name = raw[1:]
                else:
                    name = raw
                if len(name) > MAX_NAME_LEN:
                    col_label = self._columns[j]["name"]
                    errors.append(
                        f"Row {row + 1}, column '{col_label}': "
                        f"parameter name '{name}' is {len(name)} characters "
                        f"(max {MAX_NAME_LEN}). LS-DYNA will truncate it."
                    )
        return errors

    def accept(self):
        """Validate prmr names before closing. Warn user but allow override."""
        errors = self._validate_prmr_names()
        if errors:
            msg = "\n".join(f"• {e}" for e in errors)
            reply = QMessageBox.warning(
                self,
                "Parameter Name Too Long",
                f"The following parameter names exceed the LS-DYNA limit of 8 characters:\n\n"
                f"{msg}\n\n"
                f"Names longer than 8 chars will be truncated by LS-DYNA and will cause "
                f"import warnings in PyDyna.\n\n"
                f"Do you want to save anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return  # stay open so user can fix
        super().accept()


class TableCardButton(QPushButton):
    """Button that opens the TableCardEditorDialog when clicked.
    
    Displays "[N rows - Click to edit]" text and stores the underlying data.
    """
    
    dataChanged = Signal(object)  # Emits the new DataFrame
    
    def __init__(self, columns, data=None, property_name="", title="Edit Table Data", parent=None):
        """Initialize the button.
        
        Args:
            columns: Column definitions for the dialog.
            data: Initial data (DataFrame, JSON string, or None).
            property_name: The property name on the keyword object (e.g., 'set_entries').
            title: Dialog title.
            parent: Parent widget.
        """
        super().__init__(parent)
        self._columns = columns
        self._data = data
        self._property_name = property_name
        self._title = title
        self._update_text()
        self.clicked.connect(self._open_editor)
    
    def _update_text(self):
        """Update button text to show row count."""
        count = 0
        if isinstance(self._data, pd.DataFrame):
            count = len(self._data)
        elif isinstance(self._data, str):
            try:
                import json
                parsed = json.loads(self._data)
                if isinstance(parsed, dict) and 'data' in parsed:
                    count = len(parsed['data'])
            except (json.JSONDecodeError, ValueError):
                pass
        elif isinstance(self._data, dict) and 'data' in self._data:
            count = len(self._data.get('data', []))
        self.setText(f"[{count} rows - Click to edit]")
    
    def _open_editor(self):
        """Open the TableCardEditorDialog."""
        dialog = TableCardEditorDialog(
            self._columns, self._data, self._title, self
        )
        if dialog.exec() == QDialog.Accepted:
            self._data = dialog.get_dataframe()
            self._update_text()
            self.dataChanged.emit(self._data)
    
    @property
    def data(self):
        return self._data
    
    @data.setter
    def data(self, value):
        self._data = value
        self._update_text()
