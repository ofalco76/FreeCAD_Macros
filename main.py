# main.py
# Entry point for My Ansys App

import sys
import threading
import warnings
import atexit
from pathlib import Path

# Warm heavy imports (pyvista/vtk/pandas) on a daemon thread so the first
# .k load in the v2 viewer doesn't pay the multi-second cold-import cost.
def _viewer_warmup():
    try:
        import numpy   # noqa: F401
        import pandas  # noqa: F401
        import vtk     # noqa: F401
        import pyvista # noqa: F401
    except Exception:
        pass

threading.Thread(target=_viewer_warmup, daemon=True, name="viewer-warmup").start()

# Suppress FutureWarning from ansys.dyna.core table_card when setting summary
# strings into float64 DataFrame columns (used for massive data placeholders)
warnings.filterwarnings(
    "ignore",
    message="Setting an item of incompatible dtype is deprecated",
    category=FutureWarning,
    module=r"ansys\.dyna\.core\.lib\.table_card"
)
# Suppress known PyDyna ParameterHandler warning: occurs when a PARAMETER
# keyword's internal DataFrame lacks the expected .data attribute in v0.11.0.
# The keyword is still captured correctly via the raw-string fallback path.
warnings.filterwarnings(
    "ignore",
    message="Error processing parameter",
    category=UserWarning,
    module=r"ansys\.dyna\.core\.lib\.parameters"
)
from PySide6.QtWidgets import QApplication, QMessageBox, QFileDialog, QLineEdit, QInputDialog
from PySide6.QtGui import QIcon
from PySide6.QtCore import QtMsgType, qInstallMessageHandler


def _qt_message_handler(msg_type, context, message):
    """Filter noisy Qt-internal warnings that are harmless in production.

    Suppressed categories:
    - QPainter::begin/end/setPen/... "Painter not active" — emitted by
      QWindowsVistaStyle when it tries to render onto a ForeignWindow device
      (type 3) during early startup before the native backing store is ready.
      Qt retries automatically; no visual artifact occurs.
    - "Detected out of bound card characters" is a Python UserWarning handled
      separately via warnings.filterwarnings above.
    """
    if msg_type in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg):
        if message.startswith("QPainter::") and "Painter not active" in message:
            return
        if message.startswith("QPainter::begin: Paint device returned engine == 0"):
            return
        if message.startswith("QPainter::end: Painter not active"):
            return
    # Forward everything else to the default handler (stderr)
    import sys
    prefix = {
        QtMsgType.QtDebugMsg: "Qt Debug",
        QtMsgType.QtInfoMsg: "Qt Info",
        QtMsgType.QtWarningMsg: "Qt Warning",
        QtMsgType.QtCriticalMsg: "Qt Critical",
        QtMsgType.QtFatalMsg: "Qt Fatal",
    }.get(msg_type, "Qt")
    print(f"{prefix}: {message}", file=sys.stderr)


qInstallMessageHandler(_qt_message_handler)

# --- Import Splash Screen (early import for fast display) ---
from ui.splash_screen import SplashScreen, LoadingManager

# --- Import UI ---
from ui.main_window import MainWindow
from ui.tree_view import TreeView
from ui.tabs_view import TabsView
from ui.file_viewer import FileViewer
from ui.project_manager_view import ProjectManagerView
from view_models.tree_view_model import TreeViewModel
from ui.placeholder import PlaceholderWidget
from ui.import_deck_view import ImportDeckDialog
from ui.dialogs import ActivationDialog, LoginDialog

# --- Import Core ---
from core.db.db_manager import SQLiteStrategy, DBManager
from core.validators.validator_manager import ValidatorManager
from core.validators.strategies import NumericValidator
from core.builder.kfile_builder import KFileBuilder
from core.licensing import (
    LicenseValidator, LicenseInfo, LicenseStatus, 
    DatabaseEncryption, EncryptedDatabaseManager,
    session, PersistentSessionManager
)

# --- Import ViewModels ---
from view_models.project_view_model import ProjectViewModel
from view_models.file_view_model import FileViewModel
from view_models.import_view_model import ImportViewModel

# --- Import Project Export/Import ---
from core.project_io import ProjectExporter, ProjectImporter

# --- Import Database Backup ---
from core.db_backup import DatabaseExporter, DatabaseImporter
from ui.db_backup_dialogs import ExportDialog, ImportDialog
from ui.styles import show_status_message

# --- Import Material Library ---
from ui.material_library import LoginDialog as MaterialLibraryLoginDialog, MaterialLibraryDialog
from core.material_library import MaterialLibraryAuth


# --- Global state for license ---
_license_info: LicenseInfo = None
_readonly_mode: bool = False
_persistent_session: PersistentSessionManager = None
_db_encryption_manager: EncryptedDatabaseManager = None  # Global reference for atexit cleanup
_db_manager: DBManager = None  # Global reference for closing connections on exit
_cleanup_done: bool = False  # Flag to prevent double cleanup


def _cleanup_databases():
    """
    Cleanup function called on application exit.
    Ensures databases are re-encrypted and .db files are deleted.
    
    This function is called from:
    - MainWindow.closeEvent()
    - atexit handler
    - QApplication.aboutToQuit signal
    
    We use a flag to ensure cleanup only runs once.
    """
    global _db_encryption_manager, _db_manager, _cleanup_done
    
    # Prevent double cleanup
    if _cleanup_done:
        print("Cleanup already done, skipping.")
        return
    _cleanup_done = True
    
    import gc
    
    print("Starting database cleanup...")
    
    # First, close all database connections from DBManager
    if _db_manager is not None:
        try:
            _db_manager.stop()
            print("DBManager connections closed.")
        except Exception as e:
            print(f"Error closing DBManager connections: {e}")
        finally:
            _db_manager = None
    
    # Force garbage collection to release any remaining SQLite connections
    # This is critical on Windows where handles stay open
    gc.collect()
    
    # Try to close any orphaned SQLite connections by forcing a vacuum
    # This helps ensure all handles are released
    try:
        # Give a small delay for connections to be released
        import time
        time.sleep(0.2)
        gc.collect()
    except Exception as e:
        print(f"GC collection warning: {e}")
    
    # Now encrypt the databases
    if _db_encryption_manager is not None:
        try:
            if _db_encryption_manager.lock_databases():
                print("Databases encrypted successfully.")
            else:
                print("Warning: Could not fully encrypt all databases on exit.")
        except Exception as e:
            print(f"Error during database encryption: {e}")
        finally:
            _db_encryption_manager = None  # Clear reference
    
    print("Database cleanup completed.")


def _show_database_recovery_dialog(app_dir: Path, error_message: str) -> tuple[bool, str]:
    """
    Show recovery options when database decryption fails.
    
    Args:
        app_dir: Application directory path
        error_message: The error message to display
        
    Returns:
        Tuple of (should_continue, action_taken)
        - should_continue: True if app should continue, False if should exit
        - action_taken: "restored", "reset", or "cancelled"
    """
    msg_box = QMessageBox()
    msg_box.setIcon(QMessageBox.Critical)
    msg_box.setWindowTitle("Database Error")
    msg_box.setText(error_message)
    msg_box.setInformativeText(
        "What would you like to do?\n\n"
        "• Restore from Backup: Load a previous backup file (.kbackup or .kmdb)\n"
        "• Start Fresh: Delete encrypted databases and start with empty databases\n"
        "• Exit: Close the application"
    )
    
    btn_restore = msg_box.addButton("Restore from Backup...", QMessageBox.ActionRole)
    btn_reset = msg_box.addButton("Start Fresh", QMessageBox.DestructiveRole)
    btn_exit = msg_box.addButton("Exit", QMessageBox.RejectRole)
    
    msg_box.setDefaultButton(btn_exit)
    msg_box.exec()
    
    clicked = msg_box.clickedButton()
    
    if clicked == btn_restore:
        # Let user select a backup file
        backup_dir = app_dir / "backups"
        if not backup_dir.exists():
            backup_dir = app_dir
            
        file_path, _ = QFileDialog.getOpenFileName(
            None,
            "Select Backup File",
            str(backup_dir),
            "Backup Files (*.kbackup *.kmdb *.kpdb);;All Files (*.*)"
        )
        
        if not file_path:
            # User cancelled file selection - show dialog again
            return _show_database_recovery_dialog(app_dir, error_message)
        
        # Ask for backup password
        password, ok = QInputDialog.getText(
            None,
            "Backup Password",
            "Enter the password for this backup file:",
            QLineEdit.Password
        )
        
        if not ok:
            return _show_database_recovery_dialog(app_dir, error_message)
        
        try:
            # Determine backup type and restore
            from core.db_backup import DatabaseImporter, ImportMode
            
            projects_db = app_dir / "database.sqlite"
            materials_db = app_dir / "data" / "material_library.db"
            backup_dir_path = app_dir / "backups"
            
            importer = DatabaseImporter(projects_db, materials_db, backup_dir_path)
            
            # First, we need to remove the encrypted files so we can restore
            enc_dir = app_dir / "data" / "encrypted"
            if enc_dir.exists():
                import shutil
                shutil.rmtree(enc_dir)
            
            # Remove existing plain databases if they exist
            if projects_db.exists():
                projects_db.unlink()
            if materials_db.exists():
                materials_db.unlink()
            
            # Ensure data directory exists
            (app_dir / "data").mkdir(exist_ok=True)
            
            # Import the backup with REPLACE mode
            success, message = importer.import_backup(
                Path(file_path), 
                password, 
                ImportMode.REPLACE,
                create_backup=False
            )
            
            if success:
                QMessageBox.information(
                    None, 
                    "Restore Successful", 
                    f"Database restored successfully.\n{message}\n\n"
                    "The application will now restart to apply changes."
                )
                return True, "restored"
            else:
                QMessageBox.warning(None, "Restore Failed", f"Could not restore backup:\n{message}")
                return _show_database_recovery_dialog(app_dir, error_message)
                
        except Exception as e:
            QMessageBox.warning(None, "Restore Failed", f"Error restoring backup:\n{str(e)}")
            return _show_database_recovery_dialog(app_dir, error_message)
    
    elif clicked == btn_reset:
        # Confirm destructive action
        confirm = QMessageBox.warning(
            None,
            "Confirm Reset",
            "This will DELETE all your projects and materials!\n\n"
            "Are you sure you want to start with empty databases?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if confirm == QMessageBox.Yes:
            try:
                # Remove encrypted database files
                enc_dir = app_dir / "data" / "encrypted"
                if enc_dir.exists():
                    import shutil
                    shutil.rmtree(enc_dir)
                
                # Remove plain databases if they exist
                projects_db = app_dir / "database.sqlite"
                materials_db = app_dir / "data" / "material_library.db"
                
                if projects_db.exists():
                    projects_db.unlink()
                if materials_db.exists():
                    materials_db.unlink()
                
                # Ensure data directory exists
                (app_dir / "data").mkdir(exist_ok=True)
                
                QMessageBox.information(
                    None,
                    "Reset Complete",
                    "Databases have been reset.\n\n"
                    "The application will now start with empty databases."
                )
                return True, "reset"
                
            except Exception as e:
                QMessageBox.critical(None, "Reset Failed", f"Could not reset databases:\n{str(e)}")
                return False, "cancelled"
        else:
            # User said No - show dialog again
            return _show_database_recovery_dialog(app_dir, error_message)
    
    else:  # btn_exit or closed
        return False, "cancelled"


def check_license_and_login(app: QApplication, config_dir: Path) -> tuple[bool, str, LicenseInfo]:
    """
    Check license status and show appropriate dialog.
    Uses persistent session if available and valid.
    
    Returns:
        Tuple of (success, master_password, license_info)
        If success is False, app should exit.
    """
    global _license_info, _readonly_mode, _persistent_session
    
    validator = LicenseValidator(config_dir)
    _persistent_session = PersistentSessionManager(config_dir)
    
    # Check if license is configured
    if not validator.is_license_configured():
        # First time - show activation dialog
        dialog = ActivationDialog(config_dir)
        if dialog.exec() != ActivationDialog.Accepted:
            return False, "", None
        
        password = dialog.get_password()
        _license_info = dialog.get_license_info()
        
        # Save persistent session for future logins
        # After activation, the license path should be saved in config
        license_file = validator.get_saved_license_path()
        if license_file:
            _persistent_session.save_session(password, license_file)
        
        return True, password, _license_info
    
    else:
        # Returning user - check for valid persistent session first
        
        # Get the actual license file path (not a hardcoded one)
        license_file = validator.get_saved_license_path()
        if not license_file:
            license_file = config_dir / "license.key"  # Fallback
        
        # Try to get license info to check expiration
        temp_info = validator.get_current_license()
        license_expiry = None
        if temp_info and temp_info.expires_at:
            license_expiry = temp_info.expires_at
        
        # Check if we have a valid persistent session
        stored_password = _persistent_session.get_stored_password(license_file, license_expiry)
        
        if stored_password:
            # Valid session exists - auto-login
            _license_info = temp_info
            
            # Check if license is expired (read-only mode)
            if _license_info and _license_info.status == LicenseStatus.EXPIRED:
                _readonly_mode = True
            
            return True, stored_password, _license_info
        
        # No valid session - show login dialog
        dialog = LoginDialog(config_dir)
        if dialog.exec() != LoginDialog.Accepted:
            return False, "", None
        
        password = dialog.get_password()
        _license_info = dialog.get_license_info()
        _readonly_mode = dialog.is_readonly_mode()
        
        # Save persistent session for future logins
        _persistent_session.save_session(password, license_file)
        
        return True, password, _license_info


def main():
    # create App
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon("resources/Logo/App_Logo.ico"))
    
    # --- Show Splash Screen immediately ---
    splash = SplashScreen()
    splash.show()
    app.processEvents()  # Ensure splash is displayed
    
    # Create loading manager (10 steps total)
    loader = LoadingManager(splash, total_steps=10)
    
    # --- Setup paths ---
    loader.step("Setting up paths...")
    app_dir = Path(__file__).resolve().parent
    config_dir = app_dir / "config"
    config_dir.mkdir(exist_ok=True)
    
    # --- License Check and Login ---
    loader.step("Checking license...")
    splash.hide()  # Hide splash during login dialog
    success, master_password, license_info = check_license_and_login(app, config_dir)
    if not success:
        sys.exit(0)
    splash.show()  # Show splash again after login
    app.processEvents()
    
    # Set read-only mode based on license
    global _readonly_mode
    if license_info and license_info.status == LicenseStatus.EXPIRED:
        _readonly_mode = True
    
    # Initialize session with credentials and license info
    loader.step("Initializing session...")
    session.initialize(master_password, license_info, _readonly_mode)
    
    # --- Setup Database Encryption Manager ---
    loader.step("Setting up encryption...")
    db_path = app_dir / "database.sqlite"
    materials_db_path = app_dir / "data" / "material_library.db"
    
    db_encryption_manager = EncryptedDatabaseManager(app_dir, master_password)
    
    # Store global reference for atexit cleanup and register handler
    global _db_encryption_manager
    _db_encryption_manager = db_encryption_manager
    atexit.register(_cleanup_databases)
    
    # Also connect to Qt's aboutToQuit signal for cleanup
    def on_about_to_quit():
        global _db_encryption_manager
        if _db_encryption_manager is not None:
            _cleanup_databases()
    
    app.aboutToQuit.connect(on_about_to_quit)
    
    # Check if this is first time (no encrypted DBs exist)
    if db_encryption_manager.is_initialized():
        # Encrypted databases exist - verify password and decrypt
        loader.step("Decrypting databases...")
        
        # Allow multiple password attempts
        max_attempts = 3
        attempt = 0
        password_verified = db_encryption_manager.verify_password()
        
        while not password_verified and attempt < max_attempts:
            attempt += 1
            remaining = max_attempts - attempt
            splash.hide()
            
            if remaining > 0:
                msg = f"Incorrect password. {remaining} attempt(s) remaining.\nPlease enter the correct master password:"
            else:
                msg = "Last attempt. Please enter the correct master password:"
            
            new_password, ok = QInputDialog.getText(
                None, 
                "Password Required", 
                msg,
                QLineEdit.Password
            )
            
            if not ok or not new_password:
                # User cancelled
                splash.close()
                sys.exit(0)
            
            # Update encryption manager with new password and retry
            master_password = new_password
            db_encryption_manager = EncryptedDatabaseManager(app_dir, master_password)
            _db_encryption_manager = db_encryption_manager  # Update global reference
            password_verified = db_encryption_manager.verify_password()
            
            if password_verified:
                # Update session with correct password
                session.initialize(master_password, license_info, _readonly_mode)
                # Update persistent session with correct password
                if _persistent_session and license_info:
                    license_file = config_dir / "license.key"
                    _persistent_session.save_session(master_password, license_file)
            
            splash.show()
            app.processEvents()
        
        if not password_verified:
            splash.close()
            # Show recovery options
            should_continue, action = _show_database_recovery_dialog(
                app_dir, 
                "Maximum password attempts exceeded.\nCannot decrypt databases."
            )
            if not should_continue:
                sys.exit(0)
            # If restored or reset, re-initialize encryption manager
            db_encryption_manager = EncryptedDatabaseManager(app_dir, master_password)
            _db_encryption_manager = db_encryption_manager  # Update global reference
            splash.show()
            app.processEvents()
        
        if db_encryption_manager.is_initialized() and not db_encryption_manager.unlock_databases():
            splash.close()
            # Show recovery options
            should_continue, action = _show_database_recovery_dialog(
                app_dir, 
                "Failed to decrypt databases.\nThe database files may be corrupted."
            )
            if not should_continue:
                sys.exit(0)
            # If restored or reset, re-initialize encryption manager and unlock
            db_encryption_manager = EncryptedDatabaseManager(app_dir, master_password)
            _db_encryption_manager = db_encryption_manager  # Update global reference
            if db_encryption_manager.is_initialized():
                db_encryption_manager.unlock_databases()
            splash.show()
            app.processEvents()
    else:
        # First time - if plain databases exist, encrypt them
        loader.step("Initializing databases...")
        if db_encryption_manager.has_plain_databases():
            if not db_encryption_manager.initialize_encryption():
                QMessageBox.warning(None, "Warning", "Could not initialize database encryption.")

    # --- Setup DB Manager ---
    loader.step("Connecting to database...")
    db_strategy = SQLiteStrategy(db_path)
    db_manager = DBManager(db_strategy)
    db_manager.start()   # crea conexión y tablas
    
    # Store global reference for cleanup on exit
    global _db_manager
    _db_manager = db_manager

    # --- Setup Validator Manager ---
    loader.step("Loading validators...")
    validators = ValidatorManager()
    validators.register("thickness", NumericValidator(min_val=0.0))
    validators.register("density", NumericValidator(min_val=0.0))
    validators.register("young_modulus", NumericValidator(min_val=0.0))

    # --- Setup ViewModels ---
    loader.step("Initializing view models...")
    project_vm = ProjectViewModel(db_manager)   # 👈 recibe db_manager directamente
    tree_vm = TreeViewModel(project_vm)
    builder = KFileBuilder(project_vm)
    file_vm = FileViewModel(builder)

    # --- Setup UI ---
    loader.step("Building user interface...")
    window = MainWindow()
    
    # Set cleanup callback for proper database encryption on close
    window.set_cleanup_callback(_cleanup_databases)
    
    project_manager = ProjectManagerView(project_vm)

    # Store project_vm reference in window for plugins to access
    window._project_vm = project_vm

    # Crear primero TabsView con tree_view=None
    tabs_widget = TabsView(None, project_vm)

    # Crear TreeView con referencia al TabsView
    tree_widget = TreeView(tree_vm, tabs_widget)

    # Actualizar referencia en TabsView
    tabs_widget.tree_view = tree_widget

    file_viewer = FileViewer()
    placeholder_widget = PlaceholderWidget()

    # Layout principal
    loader.step("Loading plugins...")
    window.setCentralWidgets(
        project_manager,
        tree_widget,
        tabs_widget,
        file_viewer,
        placeholder_widget
    )

    # --- actions ---
    def on_new_project():
        # Usar el mismo método del ProjectManager
        project_manager.create_project()

    def on_open_project():
        project_manager.open_project()

    def on_project_opened(pid: int):
        window.start_task("Loading project...", total_steps=5)
        try:
            window.update_task(1, "Opening project...")
            project_vm.open_project(pid)  # Set active project

            window.update_task(2, "Loading tree structure...")
            tree_widget.load_project(pid)

            window.update_task(3, "Reading project info...")
            new_name = project_vm.get_project_name(pid)
            window.setWindowTitle(f"LSPP Deck File Generator - {new_name}")

            window.update_task(4, "Refreshing files...")
            file_viewer.update_files(file_vm.refresh_exports(pid, app_dir, new_name))

            window.update_task(5, "Finalizing...")
            window.finish_task(success=True, message=f"Project '{new_name}' loaded")
        except Exception as e:
            window.finish_task(success=False, message=f"Failed to load project: {e}")

    def on_root_selected(root_id: int, root_name: str):
        pid = project_vm.active_project_id
        if pid is None:
            return
        project_name = project_vm.get_project_name(pid)
        path = file_vm.root_kfile_path(pid, root_id, root_name, app_dir, project_name)
        file_viewer.select_file(path)

    def on_file_selected(path: Path):
        pid = project_vm.active_project_id
        if pid is None or not path:
            return
        
        # Only process files from the active project's export directory
        # This prevents signal cycles when user selects files from external folders
        project_name = project_vm.get_project_name(pid)
        export_dir = file_vm.export_dir_for(pid, app_dir, project_name)
        try:
            # Check if the file is inside the project's export directory
            path.relative_to(export_dir)
        except ValueError:
            # File is not in the project's export directory, ignore
            return
        
        nodes = project_vm.get_nodes(pid)
        roots = [(nid, name) for nid, parent_id, name in nodes if parent_id is None]
        stem = Path(path).stem
        for nid, name in roots:
            slug = builder.slug(name, str(nid))
            if slug == stem:
                tree_widget.select_node_by_id(nid)
                break

    def on_root_node_renamed(root_id: int, old_name: str, new_name: str):
        """Handle root node rename - delete old .k file and refresh FileViewer."""
        pid = project_vm.active_project_id
        if pid is None:
            return
        project_name = project_vm.get_project_name(pid)
        
        # Get old file path and delete it
        old_path = file_vm.root_kfile_path(pid, root_id, old_name, app_dir, project_name)
        try:
            if old_path.exists():
                old_path.unlink()
        except OSError as exc:
            print(f"Could not delete old .k file {old_path}: {exc}")
        
        # Refresh the file viewer
        file_viewer.update_files(file_vm.refresh_exports(pid, app_dir, project_name))

    def on_project_renamed(pid, new_name, old_name):
        try:
            file_vm.rename_exports_folder(pid, app_dir, old_name, new_name)
        except OSError as exc:
            print(f"Could not move exports folder for project {pid}: {exc}")
        if project_vm.active_project_id == pid:
            tree_widget.load_project(pid)
            window.setWindowTitle(f"LSPP Deck File Generator - {new_name}")
            file_viewer.update_files(file_vm.refresh_exports(pid, app_dir, new_name))

    def on_save_project():
        """Save the active project as a backup .kproj file in the backups folder."""
        from datetime import datetime
        
        # Check if there's an active project
        active_project_id = project_vm.active_project_id
        if not active_project_id:
            QMessageBox.warning(window, "Save Project", "No active project. Please open a project first.")
            return
        
        # Get project name
        project_name = project_vm.get_project_name(active_project_id) or "project"
        
        # Create backups folder if it doesn't exist
        backup_dir = app_dir / "backups"
        backup_dir.mkdir(exist_ok=True)
        
        # Generate filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"{project_name}_{timestamp}.kproj"
        backup_path = backup_dir / backup_filename
        
        # Use a simple default password for quick saves (user can use Export for custom password)
        default_password = "quicksave"
        
        try:
            exporter = ProjectExporter(db_strategy)
            summary = exporter.get_export_summary(active_project_id)
            total_nodes = summary['nodes']
            
            # Create progress callback
            def progress_callback(current: int, total: int, message: str):
                window.update_task(current, message)
            
            # Start progress tracking
            window.start_task(f"Saving {project_name}...", total_steps=total_nodes + 2)
            
            if exporter.export_project_to_file(active_project_id, str(backup_path), default_password,
                                               progress_callback=progress_callback):
                window.finish_task(success=True, message=f"Saved: {backup_filename}")
                show_status_message(
                    window.statusBar,
                    f"✅ Project saved: {backup_filename}",
                    4000
                )
            else:
                window.finish_task(success=False, message="Save failed")
                QMessageBox.warning(window, "Save Failed", "Failed to save project backup.")
        except Exception as e:
            window.finish_task(success=False, message=f"Error: {str(e)[:30]}")
            QMessageBox.critical(window, "Save Error", f"Error saving project:\n{e}")

    def _format_export_error(e: Exception, action: str = "generating .k files") -> str:
        """Build a user-friendly export error message.

        Detects common issues (e.g. empty columns from older LS-PrePost
        versions) and appends actionable advice.
        """
        msg = f"Error {action}:\n{e}"
        err_str = str(e)
        if "invalid literal for int()" in err_str and "''" in err_str:
            msg += (
                "\n\nThis error is usually caused by .k files with blank/empty "
                "fields in element or node data columns.\n"
                "Please re-save the original .k file from a newer version of "
                "LS-PrePost (4.10+) so that empty fields are written as explicit "
                "zeros, then re-import it."
            )
        return msg

    def on_export_project():
        if project_vm.active_project_id is None:
            QMessageBox.warning(window, "Export", "No active project.")
            return
        pid = project_vm.active_project_id
        project_name = project_vm.get_project_name(pid)
        
        # Check for duplicate source names before exporting
        if tree_widget.has_duplicate_sources():
            duplicate_names = tree_widget.get_duplicate_sources()
            msg = (
                "Cannot export: Duplicate source names detected!\n\n"
                f"Duplicate names: {', '.join(duplicate_names)}\n\n"
                "Each source must have a unique name to generate separate .k files.\n"
                "Please rename the duplicate sources before exporting."
            )
            QMessageBox.warning(window, "Export Error", msg)
            return
        
        # Check if there are already generated .k files and a file is selected
        selected_k_file = file_viewer.get_selected_k_file()
        has_existing_k_files = file_viewer.has_k_files()
        
        if has_existing_k_files and selected_k_file is not None:
            # Regenerate only the selected .k file
            k_filename = selected_k_file.name
            
            def progress_callback(current: int, total: int, message: str):
                window.update_task(current, message)
            
            window.start_task(f"Regenerating {k_filename}...", total_steps=1)
            
            try:
                exported_path = file_vm.export_single(pid, app_dir, k_filename, 
                                                      progress_callback=progress_callback)
                
                if exported_path is None:
                    window.finish_task(success=False, message="Could not find source node")
                    QMessageBox.warning(window, "Export", 
                        f"Could not find the source node for '{k_filename}'.\n"
                        "The source may have been renamed or deleted.")
                    return
                
                # Refresh file list and re-select the regenerated file
                all_paths = file_vm.refresh_exports(pid, app_dir, project_name)
                file_viewer.update_files(all_paths)
                file_viewer.select_file(exported_path)
                
                window.finish_task(success=True, message=f"Regenerated {k_filename}")
                QMessageBox.information(window, "Export", f"Regenerated:\n{exported_path}")
                
            except Exception as e:
                window.finish_task(success=False, message=f"Export failed: {str(e)[:50]}")
                QMessageBox.critical(window, "Export Error",
                                    _format_export_error(e, "regenerating .k file"))
            return
        
        # No existing files or no file selected - export all
        # Get total root nodes count for progress
        nodes = project_vm.get_nodes(pid)
        roots = [nid for nid, parent_id, _ in nodes if parent_id is None]
        total_roots = len(roots)
        
        # Create progress callback
        def progress_callback(current: int, total: int, message: str):
            window.update_task(current, message)
        
        # Start progress tracking with total steps
        window.start_task(f"Generating {total_roots} .k files...", total_steps=total_roots)
        
        try:
            exported_paths = file_vm.export_all(pid, app_dir, progress_callback=progress_callback)
            
            if not exported_paths:
                window.finish_task(success=True, message="No root nodes to export", auto_hide_delay=2000)
                QMessageBox.information(window, "Export", "No root nodes to export.")
                return
            
            # Refresh to show all .k files in the folder (including manually copied ones)
            all_paths = file_vm.refresh_exports(pid, app_dir, project_name)
            file_viewer.update_files(all_paths)
            
            # Finish progress with success
            window.finish_task(success=True, message=f"Generated {len(exported_paths)} .k files")
            
            output_msg = "\n".join(str(p) for p in exported_paths)
            QMessageBox.information(window, "Export", f"Generated .k files:\n{output_msg}")
            
        except Exception as e:
            window.finish_task(success=False, message=f"Export failed: {str(e)[:50]}")
            QMessageBox.critical(window, "Export Error",
                                _format_export_error(e, "generating .k files"))

    def on_export_single_project():
        """Generate .k file only for the currently selected tab/source."""
        if project_vm.active_project_id is None:
            QMessageBox.warning(window, "Export", "No active project.")
            return
        
        # Get the currently selected tab name (source name)
        current_tab_index = tabs_widget.currentIndex()
        if current_tab_index < 0:
            QMessageBox.warning(window, "Export", "No tab selected.\nPlease select a source tab first.")
            return
        
        source_name = tabs_widget.tabText(current_tab_index).strip()
        if not source_name:
            QMessageBox.warning(window, "Export", "Could not determine source name from selected tab.")
            return
        
        pid = project_vm.active_project_id
        project_name = project_vm.get_project_name(pid)
        
        # The .k filename is in the format: source_name.k
        k_filename = f"{source_name}.k"
        
        def progress_callback(current: int, total: int, message: str):
            window.update_task(current, message)
        
        window.start_task(f"Generating {k_filename}...", total_steps=1)
        
        try:
            exported_path = file_vm.export_single(pid, app_dir, k_filename, 
                                                  progress_callback=progress_callback)
            
            if exported_path is None:
                window.finish_task(success=False, message="Could not find source node")
                QMessageBox.warning(window, "Export", 
                    f"Could not find the source node for '{source_name}'.\n"
                    "The source may have been renamed or deleted.")
                return
            
            # Refresh file list and select the generated file
            all_paths = file_vm.refresh_exports(pid, app_dir, project_name)
            file_viewer.update_files(all_paths)
            file_viewer.select_file(exported_path)
            
            window.finish_task(success=True, message=f"Generated {k_filename}")
            QMessageBox.information(window, "Export", f"Generated:\n{exported_path}")
            
        except Exception as e:
            window.finish_task(success=False, message=f"Export failed: {str(e)[:50]}")
            QMessageBox.critical(window, "Export Error",
                                _format_export_error(e, "generating .k file"))

    def on_import_kfile():
        """Abre el diálogo de importación de archivos .k"""
        if project_vm.active_project_id is None:
            QMessageBox.warning(window, "Import", "Please open or create a project first.")
            return
        
        # Primero mostrar el diálogo de selección de archivo
        filepath, _ = QFileDialog.getOpenFileName(
            window,
            "Select LS-DYNA Deck File",
            "",
            "LS-DYNA Files (*.k *.key *.dyn);;All Files (*.*)"
        )
        
        # Si el usuario canceló, no hacer nada
        if not filepath:
            return
        
        # Solo si se seleccionó un archivo, abrir el diálogo de importación
        import_vm = ImportViewModel(project_vm=project_vm)
        dialog = ImportDeckDialog(import_vm, project_vm, window, filepath=filepath)
        
        # Conectar señal para refrescar después de importar
        def on_data_added(pid, source_node_id):
            # Load project and only expand the imported source node
            tree_widget.load_project(pid, expand_node_id=source_node_id)
            window.show_status(f"Data imported to project successfully!", 5000)
        
        dialog.dataAddedToProject.connect(on_data_added)
        dialog.exec()

    def on_project_deleted(pid, project_name=None):
        # Reset UI
        tree_widget.clear_tree()
        tabs_widget.clear_tabs()
        file_viewer.update_files([])
        window.setWindowTitle("LSPP Deck File Generator")

        # Delete .k files associated with the deleted project
        try:
            file_vm.cleanup_exports(pid, app_dir, project_name)
        except OSError as exc:
            # No interrumpir flujo; solo avisar en consola
            print(f"Could not delete exports for project {pid}: {exc}")

    def on_exit_app():
        window.close()

    def on_logout():
        """Logout - clear persistent session and close application."""
        global _persistent_session
        
        reply = QMessageBox.question(
            window,
            "Logout",
            "Are you sure you want to logout?\n\n"
            "This will clear your saved session.\n"
            "Next time you start the application, you will need to enter your password again.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            # Clear persistent session
            if _persistent_session:
                _persistent_session.clear_session()
            
            # Close the application
            window.close()

    # --- Project Export/Import functions ---
    def on_export_library():
        """Export project (nodes + parameters) to encrypted file."""
        from PySide6.QtWidgets import QInputDialog
        
        # Check role-based permissions - admin and authorized can export projects
        user_role = session.user_role or 'public'
        if user_role not in ('admin', 'authorized'):
            QMessageBox.warning(
                window,
                "Permission Denied",
                f"Your license ({user_role}) does not allow exporting projects.\n"
                "Only authorized and admin users can export projects."
            )
            return
        
        # Check if there's an active project
        active_project_id = project_vm.active_project_id
        if not active_project_id:
            QMessageBox.warning(window, "Export Project", "No active project. Please open a project first.")
            return
        
        # Get project name for default filename
        project_name = project_vm.get_project_name(active_project_id) or "project"
        
        # Ask for password
        password, ok = QInputDialog.getText(
            window, "Export Password",
            "Enter a password to encrypt the file:\n(You will need this password to import the file)",
            QLineEdit.EchoMode.Password
        )
        
        if not ok:
            return
        
        if len(password) < 4:
            QMessageBox.warning(window, "Export", "Password must be at least 4 characters.")
            return
        
        # Confirm password
        password2, ok = QInputDialog.getText(
            window, "Confirm Password",
            "Confirm the password:",
            QLineEdit.EchoMode.Password
        )
        
        if not ok or password != password2:
            QMessageBox.warning(window, "Export", "Passwords do not match.")
            return
        
        # Select file to export
        filepath, _ = QFileDialog.getSaveFileName(
            window,
            "Export Project",
            str(app_dir / f"{project_name}.kproj"),
            "Project Files (*.kproj);;All Files (*.*)"
        )
        
        if not filepath:
            return
        
        try:
            exporter = ProjectExporter(db_strategy)
            summary = exporter.get_export_summary(active_project_id)
            total_nodes = summary['nodes']
            
            # Create progress callback
            def progress_callback(current: int, total: int, message: str):
                window.update_task(current, message)
            
            # Start progress tracking with total steps (nodes + serialize + encrypt)
            window.start_task(f"Exporting {total_nodes} nodes...", total_steps=total_nodes + 2)
            
            if exporter.export_project_to_file(active_project_id, filepath, password, 
                                               progress_callback=progress_callback):
                window.finish_task(success=True, message=f"Exported {total_nodes} nodes")
                QMessageBox.information(
                    window, "Export Successful",
                    f"Project exported successfully!\n\n"
                    f"Nodes: {total_nodes}\n"
                    f"File: {filepath}\n\n"
                    f"🔒 File is encrypted"
                )
            else:
                window.finish_task(success=False, message="Export failed")
                QMessageBox.warning(window, "Export Failed", "Failed to export project.")
        except Exception as e:
            window.finish_task(success=False, message=f"Error: {str(e)[:30]}")
            QMessageBox.critical(window, "Export Error", f"Error exporting project:\n{e}")
    
    def on_import_library():
        """Import project (nodes + parameters) from encrypted file."""
        from PySide6.QtWidgets import QInputDialog
        
        # Check role-based permissions - admin and authorized can import projects
        user_role = session.user_role or 'public'
        if user_role not in ('admin', 'authorized'):
            QMessageBox.warning(
                window,
                "Permission Denied",
                f"Your license ({user_role}) does not allow importing projects.\n"
                "Only authorized and admin users can import projects."
            )
            return
        
        # Select file to import
        filepath, _ = QFileDialog.getOpenFileName(
            window,
            "Import Project",
            str(app_dir),
            "Project Files (*.kproj);;All Files (*.*)"
        )
        
        if not filepath:
            return
        
        # Ask for password
        password, ok = QInputDialog.getText(
            window, "Import Password",
            "Enter the password to decrypt the file:",
            QLineEdit.EchoMode.Password
        )
        
        if not ok:
            return
        
        try:
            importer = ProjectImporter(db_strategy)
            
            # Start progress - preview phase
            window.start_task("Reading project file...", indeterminate=True)
            
            # Preview import
            preview = importer.preview_import(filepath, password)
            
            if not preview["valid"]:
                window.finish_task(success=False, message="Invalid file")
                warnings = "\n".join(preview["warnings"])
                QMessageBox.warning(window, "Invalid File", f"Cannot import file:\n{warnings}")
                return
            
            # Hide progress while showing confirmation dialog
            window._reset_progress_widget()
            
            # Show preview and confirm
            msg = (
                f"Import from: {preview['project_name']}\n"
                f"Exported by: {preview['exported_by']}\n"
                f"Date: {preview['exported_at'][:10] if preview['exported_at'] else 'Unknown'}\n\n"
                f"Nodes: {preview['nodes_count']}\n"
            )
            
            if preview["warnings"]:
                msg += "\n⚠️ Warnings:\n" + "\n".join(f"  • {w}" for w in preview["warnings"])
            
            reply = QMessageBox.question(
                window, "Confirm Import",
                msg + "\n\nProceed with import?",
                QMessageBox.Yes | QMessageBox.No
            )
            
            if reply != QMessageBox.Yes:
                return
            
            # Create progress callback
            def progress_callback(current: int, total: int, message: str):
                window.update_task(current, message)
            
            # Start progress - import phase (0-100 scale used internally)
            total_nodes = preview['nodes_count']
            window.start_task(f"Importing {total_nodes} nodes...", total_steps=100)
            
            # Perform import with progress callback
            result = importer.import_from_file(filepath, password, progress_callback=progress_callback)
            
            if result.project_id:
                window.finish_task(success=True, message=f"Imported {result.nodes_imported} nodes")
                msg = (
                    f"Import completed!\n\n"
                    f"Project: {result.project_name}\n"
                    f"Nodes imported: {result.nodes_imported}"
                )
                if result.errors:
                    msg += "\n\nNotes:\n" + "\n".join(result.errors[:5])
                    if len(result.errors) > 5:
                        msg += f"\n...and {len(result.errors) - 5} more"
                QMessageBox.information(window, "Import Successful", msg)
                
                # Refresh project list and open imported project
                project_manager.refresh()
                if result.project_id:
                    project_manager.project_opened.emit(result.project_id)
            else:
                window.finish_task(success=False, message="Import failed")
                errors = "\n".join(result.errors[:5])
                QMessageBox.warning(window, "Import Failed", f"Import failed:\n{errors}")
        except Exception as e:
            window.finish_task(success=False, message=f"Error: {str(e)[:30]}")
            QMessageBox.critical(window, "Import Error", f"Error importing project:\n{e}")
        except Exception as e:
            window.finish_task(success=False, message=f"Error: {str(e)[:30]}")
            QMessageBox.critical(window, "Import Error", f"Error importing project:\n{e}")

    # --- Conectar acciones del menú/toolbar ---
    window.connect_actions(
        on_new_project,
        on_open_project,
        on_save_project,
        on_import_kfile,
        on_export_project,
        on_exit_app,
        on_export_library,
        on_import_library,
        on_logout,
        on_export_single_project
    )

    # Conectar señales de ProjectManagerView
    project_manager.project_opened.connect(on_project_opened)
    project_manager.project_opened.connect(tree_widget.load_project)
    project_manager.project_deleted.connect(lambda pid, name=None: tree_widget.clear_tree())
    project_manager.project_deleted.connect(on_project_deleted)
    project_manager.project_renamed.connect(on_project_renamed)
    tree_widget.rootSelected.connect(on_root_selected)
    tree_widget.rootNodeRenamed.connect(on_root_node_renamed)
    file_viewer.fileSelected.connect(on_file_selected)
    
    # Connect file loading progress signals to status bar
    def on_file_loading_started(filename: str, file_size: int):
        # Only show progress for files > 100KB
        if file_size > 100 * 1024:
            size_mb = file_size / (1024 * 1024)
            window.start_task(f"Loading {filename} ({size_mb:.1f} MB)...", total_steps=100)
    
    def on_file_loading_progress(percent: int, message: str):
        window.update_task(percent, message)
    
    def on_file_loading_finished(success: bool, message: str):
        window.finish_task(success=success, message=message, auto_hide_delay=1500)
    
    file_viewer.loadingStarted.connect(on_file_loading_started)
    file_viewer.loadingProgress.connect(on_file_loading_progress)
    file_viewer.loadingFinished.connect(on_file_loading_finished)
    
    # Connect project folder request signal
    def on_project_folder_requested():
        """Load .k files from the active project's folder."""
        pid = project_vm.active_project_id
        if pid is None:
            QMessageBox.warning(window, "No Project", "No active project. Please open a project first.")
            return
        project_name = project_vm.get_project_name(pid)
        paths = file_vm.refresh_exports(pid, app_dir, project_name)
        file_viewer.update_files(paths, is_project_folder=True)
        if paths:
            show_status_message(window.statusBar, f"Loaded {len(paths)} files from project folder", 3000)
        else:
            show_status_message(window.statusBar, "No .k files found in project folder", 3000)
    
    file_viewer.projectFolderRequested.connect(on_project_folder_requested)

    # Connect theme change signal to views
    window.themeChanged.connect(project_manager.on_theme_changed)
    window.themeChanged.connect(tabs_widget.on_theme_changed)
    window.themeChanged.connect(file_viewer.on_theme_changed)
    window.themeChanged.connect(tree_widget.on_theme_changed)
    window.themeChanged.connect(window._model_viewer_panel.on_theme_changed)

    # --- Database Backup ---
    # Setup paths for database backup
    projects_db_path = db_path  # main projects database
    materials_db_path = app_dir / "data" / "material_library.db"
    backup_dir = app_dir / "backups"
    
    # Create exporter and importer
    db_exporter = DatabaseExporter(projects_db_path, materials_db_path)
    db_importer = DatabaseImporter(projects_db_path, materials_db_path, backup_dir)
    
    def _check_admin_permission(action_name: str) -> bool:
        """Check if user has admin permission for database operations."""
        user_role = session.user_role or 'public'
        if user_role != 'admin':
            QMessageBox.warning(
                window,
                "Permission Denied",
                f"Your license ({user_role}) does not allow {action_name}.\n"
                "Only admin users can perform database operations."
            )
            return False
        return True
    
    def on_db_export_full():
        if not _check_admin_permission("exporting databases"):
            return
        dialog = ExportDialog(db_exporter, window, backup_dir=backup_dir)
        dialog.radio_full.setChecked(True)
        dialog.exec()
    
    def on_db_export_projects():
        if not _check_admin_permission("exporting databases"):
            return
        dialog = ExportDialog(db_exporter, window, backup_dir=backup_dir)
        dialog.radio_projects.setChecked(True)
        dialog.exec()
    
    def on_db_export_materials():
        if not _check_admin_permission("exporting databases"):
            return
        dialog = ExportDialog(db_exporter, window, backup_dir=backup_dir)
        dialog.radio_materials.setChecked(True)
        dialog.exec()
    
    def on_db_import():
        if not _check_admin_permission("importing databases"):
            return
        dialog = ImportDialog(db_importer, window)
        # Connect import completed signal to refresh UI
        def on_import_completed():
            # Reload projects list
            project_manager._load_projects()
            # Clear tree if project was open
            tree_widget.clear_tree()
            window.setWindowTitle("LSPP Deck File Generator")
            window.show_status("Database imported successfully. Please reopen a project.", 5000)
            
            # Refresh Material Library dialog if it's open (using singleton)
            mat_lib_instance = MaterialLibraryDialog._instance
            if mat_lib_instance is not None:
                try:
                    if mat_lib_instance.isVisible():
                        mat_lib_instance.refresh_materials()
                        window.show_status("Database imported. Material Library refreshed.", 5000)
                except RuntimeError:
                    # Dialog was deleted
                    MaterialLibraryDialog._instance = None
        
        dialog.importCompleted.connect(on_import_completed)
        dialog.exec()
    
    # --- Material Library from menu ---
    mat_lib_auth = MaterialLibraryAuth()
    
    def on_open_material_library():
        """Open Material Library dialog from Database menu."""
        # Check if user is already logged in
        if not mat_lib_auth.is_logged_in():
            # Show login dialog
            from PySide6.QtWidgets import QDialog
            login_dialog = MaterialLibraryLoginDialog(mat_lib_auth, window)
            if login_dialog.exec() != QDialog.Accepted:
                return  # User cancelled login
        
        # Use singleton pattern - get or create the instance
        dialog = MaterialLibraryDialog.get_or_create_instance(
            auth=mat_lib_auth,
            current_keyword_type="",
            current_params={},
            current_family=None,
            allow_use_selected=False,
            parent=window
        )
        dialog.show()
    
    # Connect database backup actions
    window.connect_db_actions(
        on_db_export_full,
        on_db_export_projects,
        on_db_export_materials,
        on_db_import,
        on_open_material_library
    )

    # Apply default theme to all views at startup
    window.themeChanged.emit("Modern Header")

    # --- Complete loading and show main window ---
    loader.complete(window)
    window.showMaximized()
    exit_code = app.exec()

    # --- Cleanup: Stop DB and re-encrypt databases ---
    db_manager.stop()
    
    # Re-encrypt databases before exiting
    # (Also handled by atexit/_aboutToQuit, but try explicitly first)
    if _db_encryption_manager is not None:
        if _db_encryption_manager.lock_databases():
            print("Databases encrypted successfully.")
        else:
            print("Warning: Could not encrypt databases on exit.")
        _db_encryption_manager = None  # Prevent double cleanup
    
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

# End of file main.py
