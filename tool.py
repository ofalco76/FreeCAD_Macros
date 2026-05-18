"""
Unit Converter Plugin for KeywordManager.
Converts between different unit systems commonly used in LS-DYNA.
"""

from core.tools.base_tool import BaseTool

# Singleton instance for the dialog
_unit_converter_dialog = None


class UnitConverterTool(BaseTool):
    """Unit conversion tool for LS-DYNA simulations."""
    
    @property
    def name(self) -> str:
        return "unit_converter"

    @property
    def display_name(self) -> str:
        manifest = getattr(self, "_manifest", None)
        return manifest.get("display_name", "Unit Converter") if manifest else "Unit Converter"

    @property
    def description(self) -> str:
        manifest = getattr(self, "_manifest", None)
        return manifest.get("description", "Unit Converter") if manifest else "Unit Converter"
    
    @property
    def category(self) -> str:
        return "Conversion"
    
    @property
    def version(self) -> str:
        return "1.0.0"
    
    @property
    def author(self) -> str:
        return "KeywordManager Team"
    
    @property
    def shortcut(self) -> str:
        return "Ctrl+Shift+U"
    
    def execute(self, parent):
        """Show the unit converter dialog."""
        global _unit_converter_dialog
        
        # Check if dialog already exists and is visible
        if _unit_converter_dialog is not None:
            try:
                if _unit_converter_dialog.isVisible():
                    _unit_converter_dialog.raise_()
                    _unit_converter_dialog.activateWindow()
                    return
            except RuntimeError:
                # Dialog was deleted
                _unit_converter_dialog = None
        
        import importlib.util
        from pathlib import Path
        
        # Load dialog module dynamically (relative imports don't work for plugins)
        # Support both .py (source) and .pyc (compiled) files
        plugin_dir = Path(__file__).parent
        dialog_path = None
        
        # Try .pyc first (compiled/protected), then .py (source)
        for ext in ['.pyc', '.py']:
            candidate = plugin_dir / f"dialog{ext}"
            if candidate.exists():
                dialog_path = candidate
                break
        
        if dialog_path is None:
            raise FileNotFoundError(f"dialog.py or dialog.pyc not found in {plugin_dir}")
        
        spec = importlib.util.spec_from_file_location("dialog", dialog_path)
        dialog_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dialog_module)
        
        # Try to get project_vm from parent (MainWindow)
        # This allows the dialog to convert values in the database
        project_vm = None
        try:
            # The parent is MainWindow, which has access to the project_vm
            # through the project_manager or tree_view widgets
            if hasattr(parent, '_project_vm'):
                project_vm = parent._project_vm
            elif hasattr(parent, 'project_manager') and hasattr(parent.project_manager, 'project_vm'):
                project_vm = parent.project_manager.project_vm
        except Exception:
            pass  # If we can't get project_vm, dialog will work without DB conversion
        
        _unit_converter_dialog = dialog_module.UnitConverterDialog(parent, project_vm)
        _unit_converter_dialog.show()
    
    def on_load(self):
        print("Unit Converter plugin loaded")
