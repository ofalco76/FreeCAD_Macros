# ui/main_window.py
import os
import platform
import sys
from PySide6.QtWidgets import (QMainWindow, QToolBar, QStatusBar, QWidget, QSplitter, 
                             QVBoxLayout, QMessageBox, QPushButton, QProgressBar,
                             QLabel, QHBoxLayout, QApplication)
from PySide6.QtGui import QAction, QIcon, QDesktopServices, QPixmap, QPainter, QColor, QPen, QFont
from PySide6.QtCore import Qt, QUrl, Signal, QTimer, QEvent, QObject, QSize

from ui.styles import TOOLBAR_STYLE, get_resource_path
from ui.translations import get_translator, tr



# Define the path where fallback icons are stored
icon_path = get_resource_path("icons")
resources_icon_path = get_resource_path("resources/icons")

def get_icon(theme_name: str, fallback_file: str) -> QIcon:
    """Return a themed icon if available, otherwise a fallback file icon."""
    icon = QIcon.fromTheme(theme_name)
    if icon.isNull():
        # Try icons folder first
        icon_file = os.path.join(icon_path, fallback_file)
        if os.path.exists(icon_file):
            return QIcon(icon_file)
        # Try resources/icons folder
        icon_file = os.path.join(resources_icon_path, fallback_file)
        if os.path.exists(icon_file):
            return QIcon(icon_file)
        return QIcon()
    return icon

class _SplitterToggleFilter(QObject):
    """Event filter: double-click on a panel header toggles splitter expansion."""

    def __init__(self, splitter, panel_index, main_window, *,
                 title_zone_px=0, secondary_splitter=None, secondary_index=None, parent=None):
        super().__init__(parent)
        self.splitter = splitter
        self.panel_index = panel_index
        self.main_window = main_window
        # If >0, only trigger when click.y < title_zone_px (for QGroupBox title area)
        self.title_zone_px = title_zone_px
        # Optional second splitter to toggle simultaneously
        self.secondary_splitter = secondary_splitter
        self.secondary_index = secondary_index

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonDblClick:
            if self.title_zone_px > 0:
                try:
                    y = event.position().y()
                except AttributeError:
                    y = event.pos().y()
                if y > self.title_zone_px:
                    return False  # click inside content, ignore
            self.main_window._toggle_splitter_panel(self.splitter, self.panel_index)
            if self.secondary_splitter is not None:
                self.main_window._toggle_splitter_panel(
                    self.secondary_splitter, self.secondary_index)
            return True
        return super().eventFilter(obj, event)


class MainWindow(QMainWindow):
    # Signal emitted when theme changes: (theme_name)
    themeChanged = Signal(str)
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LSPP Deck File Generator")
        self.resize(1200, 800)
        
        # Set application icon
        app_icon_path = get_resource_path("resources/Logo/App_Logo.ico")
        if os.path.exists(app_icon_path):
            self.setWindowIcon(QIcon(app_icon_path))

        # --- Menu bar and toolbar ---
        self._create_menu()
        self._create_toolbar()
        
        # --- Status bar ---
        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)
        self.statusBar.showMessage("Ready", 5000)

        # Flag: 3D libraries not yet loaded (lazy import on first use)
        self._3d_libs_loaded = False
        
        # --- Task Progress Widget (right side of status bar) ---
        # Initialize progress-related attributes before setup
        self._progress_container = None
        self._progress_bar = None
        self._task_label = None
        self._current_task = None
        self._task_cancelled = False
        self._progress_timer = None
        self._simulated_progress = 0
        self._setup_progress_widget()

        # Splitter references
        self.main_splitter = None
        self.left_splitter = None
        self.right_splitter = None

        # Base styles (without QGroupBox - controlled by theme)
        self._base_stylesheet = """
            /* --- ToolBar --- */
            QToolBar {
                background-color: #e8e8e8;
                spacing: 4px;
                border: none;
            }
            QToolButton {
                background-color: #f0f0f0;
                border-top: 1px solid #ffffff;
                border-left: 1px solid #ffffff;
                border-bottom: 1px solid #909090;
                border-right: 1px solid #909090;
                border-radius: 2px;
                padding: 2px;
                min-width: 36px;
                min-height: 36px;
            }
            QToolButton:hover {
                background-color: #e0e8f8;
                border-top: 1px solid #ffffff;
                border-left: 1px solid #ffffff;
                border-bottom: 1px solid #8888aa;
                border-right: 1px solid #8888aa;
            }
            QToolButton:pressed {
                background-color: #c8c8c8;
                border-top: 1px solid #909090;
                border-left: 1px solid #909090;
                border-bottom: 1px solid #ffffff;
                border-right: 1px solid #ffffff;
            }

            /* --- Tabs --- */
            QTabBar::tab {
                background: #f0f0f0;
                border: 1px solid #cccccc;
                border-bottom-color: #999999;
                padding: 6px 12px;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                border: 1px solid #999999;
                border-bottom: 2px solid #4a90e2;
            }
            QTabWidget::pane {
                border: 1px solid #cccccc;
                background: #ffffff;
            }

            /* --- Tables --- */
            QTableWidget {
                gridline-color: #cccccc;
                background-color: #ffffff;
                alternate-background-color: #f9f9f9;
            }
            QHeaderView::section {
                background-color: #f0f0f0;
                border: 1px solid #cccccc;
                padding: 4px;
            }
            QTableWidget::item:selected {
                background-color: #cce0ff;
                color: #000000;
            }
        """
        
        # Apply base stylesheet with Modern Header theme by default
        from ui.styles import get_app_theme
        self.setStyleSheet(self._base_stylesheet + get_app_theme("Modern Header"))

    def show_status(self, message: str, timeout: int = 7000):
        """Show a message in the status bar for 'timeout' ms."""
        self.statusBar.showMessage(message, timeout)
    
    def show_status_success(self, message: str, timeout: int = 10000):
        """Show a success message in status bar with green styling."""
        self.statusBar.setStyleSheet("QStatusBar { color: #28a745; font-weight: bold; }")
        self.statusBar.showMessage(f"✓ {message}", timeout)
        # Reset style after timeout
        QTimer.singleShot(timeout, lambda: self.statusBar.setStyleSheet(""))
    
    def show_status_warning(self, message: str, timeout: int = 10000):
        """Show a warning message in status bar with orange styling."""
        self.statusBar.setStyleSheet("QStatusBar { color: #fd7e14; font-weight: bold; }")
        self.statusBar.showMessage(f"⚠ {message}", timeout)
        QTimer.singleShot(timeout, lambda: self.statusBar.setStyleSheet(""))
    
    def show_status_error(self, message: str, timeout: int = 15000):
        """Show an error message in status bar with red styling."""
        self.statusBar.setStyleSheet("QStatusBar { color: #dc3545; font-weight: bold; }")
        self.statusBar.showMessage(f"✗ {message}", timeout)
        QTimer.singleShot(timeout, lambda: self.statusBar.setStyleSheet(""))

    # =========================================================================
    # TASK PROGRESS SYSTEM
    # =========================================================================
    
    def _setup_progress_widget(self):
        """Setup the progress widget in the right side of status bar."""
        # Container widget for progress elements
        self._progress_container = QWidget()
        self._progress_layout = QHBoxLayout(self._progress_container)
        self._progress_layout.setContentsMargins(0, 0, 0, 0)
        self._progress_layout.setSpacing(8)
        
        # Task label (shows what's happening)
        self._task_label = QLabel("")
        self._task_label.setStyleSheet("color: #666; font-size: 11px;")
        self._progress_layout.addWidget(self._task_label)
        
        # Progress bar
        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedWidth(200)
        self._progress_bar.setFixedHeight(18)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setStyleSheet("""
            QProgressBar {
                border: 2px solid #4a90e2;
                border-radius: 4px;
                background-color: #e8f0fe;
                text-align: center;
                font-size: 10px;
                font-weight: bold;
                color: #333;
            }
            QProgressBar::chunk {
                background-color: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #4a90e2, stop: 0.5 #63a4ff, stop: 1 #4a90e2
                );
                border-radius: 2px;
            }
        """)
        self._progress_layout.addWidget(self._progress_bar)
        
        # Timer for simulated progress animation
        self._progress_timer = None
        self._simulated_progress = 0
        
        # Add to status bar (permanent widget on right side)
        self.statusBar.addPermanentWidget(self._progress_container)
        
        # Initially hidden
        self._progress_container.hide()
    
    def start_task(self, task_name: str, total_steps: int = 0, indeterminate: bool = False):
        """
        Start a new task with progress tracking.
        
        Args:
            task_name: Description of the task (e.g., "Exporting .k file")
            total_steps: Total number of steps (0 for indeterminate)
            indeterminate: If True, shows animated simulated progress bar
        """
        # Stop any previous timer
        if self._progress_timer is not None:
            self._progress_timer.stop()
            self._progress_timer = None
        
        self._current_task = task_name
        self._task_cancelled = False
        self._task_label.setText(task_name)
        
        # Reset progress bar style to default
        self._progress_bar.setStyleSheet("""
            QProgressBar {
                border: 2px solid #4a90e2;
                border-radius: 4px;
                background-color: #e8f0fe;
                text-align: center;
                font-size: 10px;
                font-weight: bold;
                color: #333;
            }
            QProgressBar::chunk {
                background-color: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #4a90e2, stop: 0.5 #63a4ff, stop: 1 #4a90e2
                );
                border-radius: 2px;
            }
        """)
        
        if indeterminate or total_steps == 0:
            # Simulated progress mode - shows gradual progress up to 90%
            self._progress_bar.setRange(0, 100)
            self._progress_bar.setValue(0)
            self._simulated_progress = 0
            self._progress_bar.setFormat("%p%")
            
            # Start timer for gradual progress simulation
            self._progress_timer = QTimer(self)
            self._progress_timer.timeout.connect(self._simulate_progress)
            self._progress_timer.start(80)  # Update every 80ms
        else:
            # Determinate mode - shows percentage
            self._progress_bar.setRange(0, total_steps)
            self._progress_bar.setValue(0)
            self._progress_bar.setFormat("%p%")
        
        self._progress_container.show()
        # Process events to show immediately
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
    
    def _simulate_progress(self):
        """Simulate gradual progress for indeterminate operations."""
        if self._simulated_progress < 90:
            # Slow down as we approach 90%
            if self._simulated_progress < 30:
                increment = 3  # Fast at start
            elif self._simulated_progress < 60:
                increment = 2  # Medium speed
            elif self._simulated_progress < 80:
                increment = 1  # Slower
            else:
                increment = 0.5  # Very slow near end
            
            self._simulated_progress = min(90, self._simulated_progress + increment)
            self._progress_bar.setValue(int(self._simulated_progress))
            
            from PySide6.QtWidgets import QApplication
            QApplication.processEvents()
    
    def update_task(self, current_step: int = None, message: str = None):
        """
        Update the current task progress.
        
        Args:
            current_step: Current step number (for determinate mode)
            message: Optional message to update task label
        """
        if self._task_cancelled:
            return False
            
        if current_step is not None:
            self._progress_bar.setValue(current_step)
            
        if message:
            self._task_label.setText(message)
        
        # Process events to update UI
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
        
        return not self._task_cancelled
    
    def finish_task(self, success: bool = True, message: str = None, auto_hide_delay: int = 2000):
        """
        Finish the current task.
        
        Args:
            success: Whether task completed successfully
            message: Optional completion message
            auto_hide_delay: Delay in ms before hiding progress (0 to hide immediately)
        """
        # Stop simulated progress timer if running
        if self._progress_timer is not None:
            self._progress_timer.stop()
            self._progress_timer = None
        
        if success:
            # Animate to 100% quickly
            self._progress_bar.setRange(0, 100)
            current = self._progress_bar.value()
            for val in range(current, 101, 5):
                self._progress_bar.setValue(val)
                from PySide6.QtWidgets import QApplication
                QApplication.processEvents()
            self._progress_bar.setValue(100)
            
            # Show success state
            self._progress_bar.setStyleSheet("""
                QProgressBar {
                    border: 2px solid #28a745;
                    border-radius: 4px;
                    background-color: #d4edda;
                    text-align: center;
                    font-size: 10px;
                    font-weight: bold;
                    color: #155724;
                }
                QProgressBar::chunk {
                    background-color: #28a745;
                    border-radius: 2px;
                }
            """)
            self._progress_bar.setFormat("✓ Done")
            if message:
                self._task_label.setText(message)
                self.show_status_success(message)
            else:
                self._task_label.setText(f"{self._current_task} - Complete")
        else:
            # Show error state
            self._progress_bar.setStyleSheet("""
                QProgressBar {
                    border: 2px solid #dc3545;
                    border-radius: 4px;
                    background-color: #f8d7da;
                    text-align: center;
                    font-size: 10px;
                    font-weight: bold;
                    color: #721c24;
                }
                QProgressBar::chunk {
                    background-color: #dc3545;
                    border-radius: 2px;
                }
            """)
            self._progress_bar.setFormat("✗ Failed")
            if message:
                self._task_label.setText(message)
                self.show_status_error(message)
            else:
                self._task_label.setText(f"{self._current_task} - Failed")
        
        # Process events to show completion state
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
        
        # Auto-hide after delay
        if auto_hide_delay > 0:
            QTimer.singleShot(auto_hide_delay, self._reset_progress_widget)
        
        self._current_task = None
    
    def cancel_task(self):
        """Cancel the current task."""
        self._task_cancelled = True
        self._task_label.setText("Cancelling...")
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
    
    def is_task_cancelled(self) -> bool:
        """Check if current task was cancelled."""
        return self._task_cancelled
    
    def _reset_progress_widget(self):
        """Reset and hide the progress widget."""
        self._progress_container.hide()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #ccc;
                border-radius: 3px;
                background-color: #f0f0f0;
                text-align: center;
                font-size: 10px;
            }
            QProgressBar::chunk {
                background-color: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #4a90e2, stop: 1 #63a4ff
                );
                border-radius: 2px;
            }
        """)
        self._task_label.setText("")
        self._current_task = None
        self._task_cancelled = False

    def _create_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")

        # Define actions with icons
        new_project_ico = os.path.join(resources_icon_path, "new_project.ico")
        new_icon = QIcon(new_project_ico) if os.path.exists(new_project_ico) else get_icon("document-new", "new.png")
        self.action_new = QAction(new_icon, "New Project", self)
        open_project_ico = os.path.join(resources_icon_path, "open_project.ico")
        open_icon = QIcon(open_project_ico) if os.path.exists(open_project_ico) else get_icon("folder", "open.png")
        self.action_open = QAction(open_icon, "Open Project", self)
        self.action_save = QAction(get_icon("document-save", "save.png"), "Save Project", self)
        import_k_ico = os.path.join(resources_icon_path, "import_k_file.ico")
        import_k_icon = QIcon(import_k_ico) if os.path.exists(import_k_ico) else get_icon("document-open", "import.png")
        self.action_import = QAction(import_k_icon, "Import .k File", self)
        gen_k_ico = os.path.join(resources_icon_path, "generate_k_files.ico")
        if os.path.exists(gen_k_ico):
            gen_k_pm = QPixmap(gen_k_ico).scaled(
                32, 32, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            gen_k_icon = QIcon(gen_k_pm)
        else:
            gen_k_icon = get_icon("document-send", "export.png")
        self.action_export = QAction(gen_k_icon, "Generate .k File", self)
        
        # Single .k file export action
        single_k_ico = os.path.join(resources_icon_path, "generate_single_k_file.ico")
        if os.path.exists(single_k_ico):
            single_k_pm = QPixmap(single_k_ico).scaled(
                32, 32, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.action_export_single = QAction(QIcon(single_k_pm), "Generate Single .k File", self)
        else:
            self.action_export_single = QAction(get_icon("document-export", "export.png"), "Generate Single .k File", self)
        
        # Exit action - use Exit_App.ico if available
        exit_icon_path = os.path.join(resources_icon_path, "exit.ico")
        if os.path.exists(exit_icon_path):
            self.action_exit = QAction(QIcon(exit_icon_path), "Exit", self)
        else:
            self.action_exit = QAction(self._create_exit_icon(24), "Exit", self)
        
        # Project export/import actions - use specific icons from resources
        export_kproj_icon = os.path.join(resources_icon_path, "export_project.ico")
        import_kproj_icon = os.path.join(resources_icon_path, "import_project.ico")
        
        if os.path.exists(export_kproj_icon):
            self.action_export_library = QAction(QIcon(export_kproj_icon), "Export Project...", self)
        else:
            self.action_export_library = QAction(get_icon("document-save-as", "export.png"), "Export Project...", self)
        
        if os.path.exists(import_kproj_icon):
            self.action_import_library = QAction(QIcon(import_kproj_icon), "Import Project...", self)
        else:
            self.action_import_library = QAction(get_icon("document-open", "import.png"), "Import Project...", self)
        
        self.action_export_library.setToolTip(
            "Export Project (Encrypted)\n"
            "Export the active project to an encrypted .kproj file\n"
            "(includes nodes and parameters, protected with password)"
        )
        self.action_import_library.setToolTip(
            "Import Project (Encrypted)\n"
            "Import a project from an encrypted .kproj file\n"
            "(requires password to decrypt)"
        )

        # Add them to the File menu (with text)
        file_menu.addAction(self.action_new)
        file_menu.addAction(self.action_open)
        file_menu.addAction(self.action_save)
        file_menu.addSeparator()
        file_menu.addAction(self.action_export_library)
        file_menu.addAction(self.action_import_library)
        file_menu.addSeparator()
        
        # Preferences submenu
        self.preferences_menu = file_menu.addMenu("Preferences")
        
        # Theme submenu inside Preferences
        self.theme_menu = self.preferences_menu.addMenu("Theme")
        
        # Theme actions
        self.action_theme_classic = QAction("Classic", self)
        self.action_theme_classic.setCheckable(True)
        self.action_theme_classic.triggered.connect(lambda: self._apply_theme("Classic"))
        self.theme_menu.addAction(self.action_theme_classic)
        
        self.action_theme_modern = QAction("Modern", self)
        self.action_theme_modern.setCheckable(True)
        self.action_theme_modern.triggered.connect(lambda: self._apply_theme("Modern"))
        self.theme_menu.addAction(self.action_theme_modern)
        
        self.action_theme_modern_header = QAction("Modern Header", self)
        self.action_theme_modern_header.setCheckable(True)
        self.action_theme_modern_header.setChecked(True)  # Default theme
        self.action_theme_modern_header.triggered.connect(lambda: self._apply_theme("Modern Header"))
        self.theme_menu.addAction(self.action_theme_modern_header)
        
        # Store theme actions for easy management
        self.theme_actions = [self.action_theme_classic, self.action_theme_modern, self.action_theme_modern_header]
        
        # Language submenu inside Preferences
        self.language_menu = self.preferences_menu.addMenu(tr("menu_language"))
        
        # Language actions
        self.action_lang_english = QAction("English", self)
        self.action_lang_english.setCheckable(True)
        self.action_lang_english.triggered.connect(lambda: self._change_language("en"))
        self.language_menu.addAction(self.action_lang_english)
        
        self.action_lang_spanish = QAction("Español", self)
        self.action_lang_spanish.setCheckable(True)
        self.action_lang_spanish.triggered.connect(lambda: self._change_language("es"))
        self.language_menu.addAction(self.action_lang_spanish)
        
        # Store language actions and set current language check
        self.language_actions = [self.action_lang_english, self.action_lang_spanish]
        self._update_language_checkmarks()
        
        # Register for language change notifications
        get_translator().register_observer(self._on_language_changed)
        
        file_menu.addSeparator()
        
        # Logout action - clears persistent session
        self.action_logout = QAction(get_icon("system-log-out", "logout.png"), "Logout", self)
        self.action_logout.setToolTip(
            "Logout\n"
            "Clear saved session and close application.\n"
            "Next startup will require password."
        )
        file_menu.addAction(self.action_logout)
        
        file_menu.addSeparator()
        file_menu.addAction(self.action_exit)

        # Deck menu (for .k files import/export)
        deck_menu = menubar.addMenu("&Deck")
        
        deck_menu.addAction(self.action_export)
        deck_menu.addAction(self.action_export_single)
        deck_menu.addSeparator()
        deck_menu.addAction(self.action_import)

        # Database menu (for backup/restore operations)
        database_menu = menubar.addMenu("D&atabase")
        
        # Material Library action (at top)
        self.action_material_library = QAction(
            get_icon("database", "database_icon-icons.com_70204.ico"), 
            "Material Library...", 
            self
        )
        self.action_material_library.setToolTip("Open Material Library browser")
        database_menu.addAction(self.action_material_library)
        
        database_menu.addSeparator()
        
        # Export actions
        _exp_full_ico = os.path.join(resources_icon_path, "export_backup_db.ico")
        _exp_full_icon = QIcon(_exp_full_ico) if os.path.exists(_exp_full_ico) else QIcon()
        self.action_db_export_full = QAction(_exp_full_icon, "Export Full Backup...", self)
        self.action_db_export_full.setToolTip("Export all projects and materials to encrypted backup")
        database_menu.addAction(self.action_db_export_full)
        
        _exp_proj_ico = os.path.join(resources_icon_path, "export_project_db.ico")
        _exp_proj_icon = QIcon(_exp_proj_ico) if os.path.exists(_exp_proj_ico) else QIcon()
        self.action_db_export_projects = QAction(_exp_proj_icon, "Export Projects DB...", self)
        self.action_db_export_projects.setToolTip("Export only projects database")
        database_menu.addAction(self.action_db_export_projects)
        
        _exp_mat_ico = os.path.join(resources_icon_path, "export_material_db.ico")
        _exp_mat_icon = QIcon(_exp_mat_ico) if os.path.exists(_exp_mat_ico) else QIcon()
        self.action_db_export_materials = QAction(_exp_mat_icon, "Export Materials DB...", self)
        self.action_db_export_materials.setToolTip("Export only materials database")
        database_menu.addAction(self.action_db_export_materials)
        
        database_menu.addSeparator()
        
        # Import actions
        _imp_backup_ico = os.path.join(resources_icon_path, "import_backup_db.ico")
        _imp_backup_icon = QIcon(_imp_backup_ico) if os.path.exists(_imp_backup_ico) else get_icon("document-open", "import.png")
        self.action_db_import = QAction(_imp_backup_icon, "Import Backup...", self)
        self.action_db_import.setToolTip("Import database from encrypted backup file")
        database_menu.addAction(self.action_db_import)

        # Tools menu
        tools_menu = menubar.addMenu("&Tools")
        self.action_plots = QAction("Plots", self)
        self.action_plots.setToolTip("Open Curve Plot dialog")
        self.action_plots.triggered.connect(self._show_plot_dialog)
        tools_menu.addAction(self.action_plots)
        
        # 3D Model Viewer action (Qt-embedded with toolbars)
        self.action_3d_viewer = QAction("3D Model Viewer", self)
        self.action_3d_viewer.setToolTip("Open 3D viewer for the .k file selected in File Viewer")
        self.action_3d_viewer.triggered.connect(self._show_3d_viewer)
        tools_menu.addAction(self.action_3d_viewer)

        # 3D Viewer 2.0 — new-generation viewer (ribbon + three-panel layout)
        self.action_3d_viewer_v2 = QAction("3D Viewer 2.0", self)
        self.action_3d_viewer_v2.setToolTip(
            "Open 3D Viewer 2.0 — new industrial-style viewer with ribbon and panels"
        )
        self.action_3d_viewer_v2.triggered.connect(self._show_3d_viewer_v2)
        tools_menu.addAction(self.action_3d_viewer_v2)

        # 3D Viewer — Native (keyboard-only fallback)
        self.action_3d_native = QAction("3D Viewer (native)", self)
        self.action_3d_native.setToolTip("Open native VTK window (keyboard shortcuts only)")
        self.action_3d_native.triggered.connect(lambda: self._show_3d_viewer(force_native=True))
        tools_menu.addAction(self.action_3d_native)
        
        # Reset Splitters action
        reset_splitters_icon_path = os.path.join(resources_icon_path, "Viewport_split.ico")
        if os.path.exists(reset_splitters_icon_path):
            self.action_reset_splitters = QAction(QIcon(reset_splitters_icon_path), "Reset Layout", self)
        else:
            self.action_reset_splitters = QAction("Reset Layout", self)
        self.action_reset_splitters.setToolTip("Reset splitters to default positions")
        self.action_reset_splitters.triggered.connect(self._reset_splitters)
        tools_menu.addAction(self.action_reset_splitters)
        
        # Store reference for plugins
        self._tools_menu = tools_menu
        
        # Load plugins (after built-in tools)
        self._load_plugins()

        # Help menu
        help_menu = menubar.addMenu("&Help")
        
        # User Manual - opens integrated HTML viewer
        self.action_user_manual = QAction(get_icon("help-contents", "book_86162.ico"), "User Manual...", self)
        self.action_user_manual.setShortcut("F1")
        self.action_user_manual.setToolTip("Open the User Manual with tutorials and guides")
        self.action_user_manual.triggered.connect(self._show_user_manual)
        help_menu.addAction(self.action_user_manual)
        
        # Keyword Reference - opens LS-DYNA manual folder
        self.action_keyword_ref = QAction(get_icon("accessories-dictionary", "book_86162.ico"), "Keyword Reference...", self)
        self.action_keyword_ref.setToolTip("Open LS-DYNA keyword documentation (PDF manuals)")
        self.action_keyword_ref.triggered.connect(self._open_keyword_reference)
        help_menu.addAction(self.action_keyword_ref)
        
        help_menu.addSeparator()
        
        # Online Documentation
        self.action_online_docs = QAction(get_icon("applications-internet", ""), "Online Documentation", self)
        self.action_online_docs.setToolTip("Open online documentation in web browser")
        self.action_online_docs.triggered.connect(self._open_online_docs)
        help_menu.addAction(self.action_online_docs)
        
        help_menu.addSeparator()
        
        # What's New / Release Notes
        self.action_whats_new = QAction(get_icon("dialog-information", ""), "What's New...", self)
        self.action_whats_new.setToolTip("See new features and recent changes")
        self.action_whats_new.triggered.connect(self._show_whats_new)
        help_menu.addAction(self.action_whats_new)

        help_menu.addSeparator()
        
        # Contact (existing)
        self.action_contact = QAction(get_icon("mail-message", "contact.png"), "Contact", self)
        self.action_contact.triggered.connect(self._show_contact_dialog)
        help_menu.addAction(self.action_contact)
        
        # About (existing)
        self.action_about = QAction(get_icon("help-about", "about.png"), "About", self)
        self.action_about.triggered.connect(self._show_about_dialog)
        help_menu.addAction(self.action_about)

    def _create_toolbar(self):
        toolbar = QToolBar("Main Toolbar", self)
        toolbar.setMovable(True)
        toolbar.setIconSize(QSize(32, 32))
        toolbar.setStyleSheet(TOOLBAR_STYLE)

        # Create plot icon programmatically (must be done after Qt is initialized)
        plot_icon = self._create_plot_icon(24)
        self.action_plots.setIcon(plot_icon)
        
        # 3D viewer icon
        viewer_3d_icon_path = os.path.join(resources_icon_path, "viewer_3D_.ico")
        if os.path.exists(viewer_3d_icon_path):
            self.action_3d_viewer.setIcon(QIcon(viewer_3d_icon_path))

        # DB export/import icons for toolbar
        _export_db_ico = os.path.join(resources_icon_path, "export_db.ico")
        if os.path.exists(_export_db_ico):
            self.action_db_export_full.setIcon(QIcon(_export_db_ico))
        _import_db_ico = os.path.join(resources_icon_path, "import_db.ico")
        if os.path.exists(_import_db_ico):
            self.action_db_import.setIcon(QIcon(_import_db_ico))

        # Add the same actions (only icons will be shown)
        toolbar.addAction(self.action_new)
        toolbar.addAction(self.action_open)
        toolbar.addAction(self.action_save)
        toolbar.addSeparator()
        toolbar.addAction(self.action_import)
        toolbar.addAction(self.action_export)
        toolbar.addAction(self.action_export_single)
        toolbar.addSeparator()
        toolbar.addAction(self.action_export_library)
        toolbar.addAction(self.action_import_library)
        toolbar.addSeparator()
        toolbar.addAction(self.action_material_library)
        toolbar.addSeparator()
        toolbar.addAction(self.action_db_export_full)
        toolbar.addAction(self.action_db_import)
        toolbar.addSeparator()
        toolbar.addAction(self.action_plots)
        toolbar.addAction(self.action_3d_viewer)
        toolbar.addAction(self.action_reset_splitters)
        toolbar.addSeparator()
        toolbar.addAction(self.action_exit)

        # Show only icons
        toolbar.setToolButtonStyle(Qt.ToolButtonIconOnly)

        self.addToolBar(Qt.TopToolBarArea, toolbar)
        
        # Store toolbar reference
        self._main_toolbar = toolbar

        # --- Plugin toolbar (separate, same style) ---
        self._plugin_toolbar = QToolBar("Plugins", self)
        self._plugin_toolbar.setMovable(True)
        self._plugin_toolbar.setIconSize(QSize(32, 32))
        self._plugin_toolbar.setStyleSheet(TOOLBAR_STYLE)
        self._plugin_toolbar.setToolButtonStyle(Qt.ToolButtonIconOnly)

        if hasattr(self, '_loaded_plugins') and self._loaded_plugins:
            self._add_plugin_toolbar_buttons(self._plugin_toolbar, self._loaded_plugins)

        self.addToolBar(Qt.TopToolBarArea, self._plugin_toolbar)

    def setCentralWidgets(self, project_manager, tree_widget, tabs_widget, file_viewer, placeholder_widget):
        """Layout: one horizontal splitter containing two vertical splitters.
        Left: ProjectManager above + TreeView below.
        Right: TabsView above + (FileViewer | ModelViewerPanel) below.
        """
        from ui.model_viewer_panel import ModelViewerPanel

        # Store file_viewer reference for 3D viewer access
        self._file_viewer = file_viewer

        # Left vertical splitter: ProjectManager + TreeView
        self.left_splitter = QSplitter(Qt.Vertical)
        self.left_splitter.addWidget(project_manager)   # top
        self.left_splitter.addWidget(tree_widget)       # bottom

        # Bottom-right: FileViewer + ModelViewerPanel side by side
        self._model_viewer_panel = ModelViewerPanel()
        self.bottom_right_splitter = QSplitter(Qt.Horizontal)
        self.bottom_right_splitter.addWidget(file_viewer)          # left
        self.bottom_right_splitter.addWidget(self._model_viewer_panel)  # right
        self.bottom_right_splitter.setStretchFactor(0, 4)
        self.bottom_right_splitter.setStretchFactor(1, 1)

        # Connect FileViewer signals → ModelViewerPanel
        file_viewer.modelFileRequested.connect(self._model_viewer_panel.load_model)
        file_viewer.modelClearRequested.connect(self._model_viewer_panel.clear_model)

        # Right vertical splitter: TabsView + (FileViewer | ModelViewer)
        self.right_splitter = QSplitter(Qt.Vertical)
        self.right_splitter.addWidget(tabs_widget)                 # top
        self.right_splitter.addWidget(self.bottom_right_splitter)  # bottom

        # Horizontal splitter containing both
        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.addWidget(self.left_splitter)
        self.main_splitter.addWidget(self.right_splitter)

        # Central container
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(self.main_splitter)
        layout.setContentsMargins(6, 6, 6, 6)

        self.setCentralWidget(container)

        # Adjust initial sizes
        self._splitter_saved_sizes = {}   # id(splitter) -> saved sizes before maximize
        self._update_splitter_sizes()

        # Install double-click-to-expand on panel headers
        self._setup_header_double_click(project_manager, tree_widget, tabs_widget, file_viewer)

    # ── Splitter header double-click expand/restore ────────────────────

    def _setup_header_double_click(self, project_manager, tree_widget, tabs_widget, file_viewer):
        """Install event filters so double-clicking a panel header toggles splitter expansion."""
        self._header_filters = []  # prevent GC

        def _install(splitter, index, widget, *, title_zone_px=0):
            f = _SplitterToggleFilter(splitter, index, self, title_zone_px=title_zone_px, parent=self)
            widget.installEventFilter(f)
            self._header_filters.append(f)

        # PM – Project Manager  (left_splitter index 0)
        _install(self.left_splitter, 0, project_manager.header_label)
        _install(self.left_splitter, 0, project_manager.group_box, title_zone_px=30)

        # MM – Model Manager / TreeView  (left_splitter index 1)
        # QHeaderView is a QAbstractScrollArea — mouse events land on its viewport()
        _install(self.left_splitter, 1, tree_widget.tree.header().viewport())

        # KM – Keyword Manager / TabsView  (right_splitter index 0)
        # QTabBar processes double-clicks internally; use QTabWidget signal instead
        tabs_widget.tabBarDoubleClicked.connect(
            lambda _idx: self._toggle_splitter_panel(self.right_splitter, 0)
        )

        # FV – File Viewer  (right_splitter index 1 + bottom_right_splitter index 0)
        # Double-click toggles BOTH: hides TabsView AND ModelViewerPanel
        def _install_fv(widget, *, title_zone_px=0):
            f = _SplitterToggleFilter(
                self.right_splitter, 1, self,
                title_zone_px=title_zone_px,
                secondary_splitter=self.bottom_right_splitter,
                secondary_index=0,
                parent=self,
            )
            widget.installEventFilter(f)
            self._header_filters.append(f)

        _install_fv(file_viewer.header_label)
        _install_fv(file_viewer.group, title_zone_px=30)

    def _toggle_splitter_panel(self, splitter, panel_index):
        """Toggle a splitter panel between maximized and restored."""
        key = id(splitter)
        current_sizes = splitter.sizes()
        total = sum(current_sizes)
        if total <= 0:
            return

        sibling_index = 1 - panel_index
        is_maximized = current_sizes[sibling_index] <= 1

        if is_maximized:
            # Restore previously saved sizes
            saved = self._splitter_saved_sizes.pop(key, None)
            if saved:
                splitter.setSizes(saved)
            else:
                half = total // 2
                splitter.setSizes([half, total - half])
        else:
            # Save current sizes, then maximize
            self._splitter_saved_sizes[key] = list(current_sizes)
            new_sizes = [0, 0]
            new_sizes[panel_index] = total
            splitter.setSizes(new_sizes)

    # ── Resize / reset ─────────────────────────────────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_splitter_sizes()

    def _update_splitter_sizes(self):
        """Ajusta proporciones de los splitters según tamaño actual."""
        if not self.main_splitter or not self.left_splitter or not self.right_splitter:
            return

        total_width = self.width()
        total_height = self.height()

        # Horizontal: left (ProjectManager + Tree) ~1/4 of right width
        self.main_splitter.setSizes([total_width // 4, (total_width * 3) // 4])

        saved = getattr(self, '_splitter_saved_sizes', {})

        # Vertical izquierdo: 1/3 - 2/3  (skip if a panel is maximized)
        if id(self.left_splitter) not in saved:
            self.left_splitter.setSizes([total_height // 3, (total_height * 2) // 3])

        # Vertical derecho: 60% TabsView - 40% FileViewer+ModelViewer
        if id(self.right_splitter) not in saved:
            self.right_splitter.setSizes([(total_height * 5.5) // 10, (total_height * 4.5) // 10])

        # Bottom-right: FileViewer ~80% - ModelViewer ~20%
        if hasattr(self, 'bottom_right_splitter') and self.bottom_right_splitter:
            br_width = self.bottom_right_splitter.width() or (total_width * 3) // 4
            if id(self.bottom_right_splitter) not in saved:
                self.bottom_right_splitter.setSizes([(br_width * 4) // 5, br_width // 5])

    def _reset_splitters(self):
        """Reset all splitters to their default positions."""
        self._splitter_saved_sizes.clear()
        self._update_splitter_sizes()
        # Force bottom_right_splitter reset even if _update_splitter_sizes skipped it
        if hasattr(self, 'bottom_right_splitter') and self.bottom_right_splitter:
            br_width = self.bottom_right_splitter.width() or self.width()
            self.bottom_right_splitter.setSizes([(br_width * 4) // 5, br_width // 5])
        self.show_status("Layout reset to default positions", 3000)

    def connect_actions(self, on_new, on_open, on_save, on_import, on_export, on_exit,
                        on_export_library=None, on_import_library=None, on_logout=None,
                        on_export_single=None):
        """Conecta las acciones del menú y toolbar con funciones externas"""
        self.action_new.triggered.connect(on_new)
        self.action_open.triggered.connect(on_open)
        self.action_save.triggered.connect(on_save)
        self.action_import.triggered.connect(on_import)
        self.action_export.triggered.connect(on_export)
        self.action_exit.triggered.connect(on_exit)
        
        # Library actions
        if on_export_library:
            self.action_export_library.triggered.connect(on_export_library)
        if on_import_library:
            self.action_import_library.triggered.connect(on_import_library)
        
        # Logout action
        if on_logout:
            self.action_logout.triggered.connect(on_logout)
        
        # Single .k file export
        if on_export_single:
            self.action_export_single.triggered.connect(on_export_single)

    def connect_db_actions(self, on_export_full, on_export_projects, on_export_materials, on_import,
                           on_material_library=None):
        """Connect database backup actions to handlers."""
        self.action_db_export_full.triggered.connect(on_export_full)
        self.action_db_export_projects.triggered.connect(on_export_projects)
        self.action_db_export_materials.triggered.connect(on_export_materials)
        self.action_db_import.triggered.connect(on_import)
        
        # Material Library action
        if on_material_library:
            self.action_material_library.triggered.connect(on_material_library)

    def _change_language(self, lang: str):
        """Change the application language."""
        translator = get_translator()
        translator.set_language(lang)
        self._update_language_checkmarks()
        
        # Show notification in status bar only (less intrusive)
        msg = tr("status_language_changed")
        self.show_status_success(msg, 3000)
    
    def _update_language_checkmarks(self):
        """Update language menu checkmarks based on current language."""
        current_lang = get_translator().current_language
        self.action_lang_english.setChecked(current_lang == "en")
        self.action_lang_spanish.setChecked(current_lang == "es")
    
    def _on_language_changed(self):
        """Callback when language changes - update all UI texts."""
        self._update_ui_texts()
    
    def _update_ui_texts(self):
        """Update all UI element texts with current language translations."""
        # Window title
        self.setWindowTitle(tr("window_title"))
        
        # Update menu titles
        menubar = self.menuBar()
        actions = menubar.actions()
        # Actual menus: File, Deck, Database, Tools, Help
        menu_keys = ["menu_file", "menu_deck", "menu_database", "menu_tools", "menu_help"]
        for i, action in enumerate(actions):
            if i < len(menu_keys):
                action.setText(tr(menu_keys[i]))
        
        # File menu actions
        self.action_new.setText(tr("menu_new_project"))
        self.action_open.setText(tr("menu_open_project"))
        self.action_save.setText(tr("menu_save_project"))
        self.action_export_library.setText(tr("menu_export_project"))
        self.action_import_library.setText(tr("menu_import_project"))
        self.action_exit.setText(tr("menu_exit"))
        self.action_logout.setText(tr("menu_logout"))
        
        # Update tooltips
        self.action_new.setToolTip(tr("tooltip_new_project"))
        self.action_open.setToolTip(tr("tooltip_open_project"))
        self.action_save.setToolTip(tr("tooltip_save_project"))
        self.action_export_library.setToolTip(tr("tooltip_export_project"))
        self.action_import_library.setToolTip(tr("tooltip_import_project"))
        self.action_exit.setToolTip(tr("tooltip_exit"))
        self.action_logout.setToolTip(tr("tooltip_logout"))
        
        # Deck menu actions
        self.action_export.setText(tr("menu_export_deck"))
        self.action_export_single.setText(tr("menu_export_single_deck"))
        self.action_import.setText(tr("menu_import_deck"))
        self.action_export.setToolTip(tr("tooltip_export_deck"))
        self.action_export_single.setToolTip(tr("tooltip_export_single_deck"))
        self.action_import.setToolTip(tr("tooltip_import_deck"))
        
        # Database menu actions
        self.action_material_library.setText(tr("menu_material_library"))
        self.action_db_export_full.setText(tr("menu_db_export_full"))
        self.action_db_export_projects.setText(tr("menu_db_export_projects"))
        self.action_db_export_materials.setText(tr("menu_db_export_materials"))
        self.action_db_import.setText(tr("menu_db_import"))
        
        # Tools menu actions
        self.action_plots.setText(tr("menu_plots"))
        
        # Help menu actions
        self.action_user_manual.setText(tr("menu_user_manual"))
        self.action_keyword_ref.setText(tr("menu_keyword_reference"))
        self.action_online_docs.setText(tr("menu_online_docs"))
        self.action_contact.setText(tr("menu_contact"))
        self.action_about.setText(tr("menu_about"))
        
        # Preferences submenus
        self.preferences_menu.setTitle(tr("menu_preferences"))
        self.theme_menu.setTitle(tr("menu_theme"))
        self.language_menu.setTitle(tr("menu_language"))
        
        # Theme actions
        self.action_theme_classic.setText(tr("theme_classic"))
        self.action_theme_modern.setText(tr("theme_modern"))
        self.action_theme_modern_header.setText(tr("theme_modern_header"))

    def _apply_theme(self, theme_name: str):
        """Apply the selected theme to the application."""
        from ui.styles import get_app_theme
        
        # Update checkmarks - only the selected theme should be checked
        for action in self.theme_actions:
            action.setChecked(action.text() == theme_name)
        
        # Get and apply the theme stylesheet combined with base styles
        theme_style = get_app_theme(theme_name)
        self.setStyleSheet(self._base_stylesheet + theme_style)
        
        # Emit signal to notify widgets about theme change
        self.themeChanged.emit(theme_name)
        
        self.statusBar.showMessage(f"Theme changed to: {theme_name}", 5000)

    def _show_whats_new(self):
        """Show the What's New / Release Notes dialog."""
        from ui.whats_new_dialog import WhatsNewDialog
        dlg = WhatsNewDialog(parent=self)
        dlg.exec()

    def _show_about_dialog(self):
        """Show the About dialog with application and system information."""
        from PySide6 import __version__ as pyside_version
        from pathlib import Path
        import h5py
        
        app_name = "LSPP Deck File Generator"
        app_version = "0.9.1-rc1"
        pydyna_version = "0.11.0"
        h5py_version = h5py.__version__
        
        # System information
        os_info = f"{platform.system()} {platform.release()}"
        os_version = platform.version()
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        architecture = platform.machine()
        
        # Logo paths
        app_logo_path = get_resource_path("resources/Logo/App_pydeckgen_logo.jpg")
        itp_logo_path = get_resource_path("resources/Logo/ITP_Logo.png")
        
        app_logo_html = ""
        if os.path.exists(app_logo_path):
            app_logo_html = f'<p align="center"><img src="{app_logo_path}" width="300"></p>'
        
        itp_logo_html = ""
        if os.path.exists(itp_logo_path):
            itp_logo_html = f'<img src="{itp_logo_path}" width="80" style="vertical-align: middle;">'
        
        about_text = f"""
{app_logo_html}
<h2 align="center">{app_name}</h2>
<p align="center"><b>Version:</b> {app_version}</p>

<hr>

<h3>Description</h3>
<p>Keyword Manager is a tool for managing LS-DYNA keyword deck files.
It provides functionality for creating, editing, and organizing keyword cards
with a material library system for reusable material definitions.</p>

<hr>

<h3>System Information</h3>
<table width="100%" cellspacing="10" cellpadding="5">
<tr>
<td width="50%" style="vertical-align: top;">
<p style="margin: 0; padding: 0;"><b>OS:</b> {os_info}</p>
<p style="margin: 0; padding: 0;"><b>OS Version:</b> {os_version}</p>
<p style="margin: 0; padding: 0;"><b>Architecture:</b> {architecture}</p>
<p style="margin: 0; padding: 0;"><b>Python:</b> {python_version}</p>
</td>
<td width="50%" style="vertical-align: top;">
<p style="margin: 0; padding: 0;"><b>PySide6:</b> {pyside_version}</p>
<p style="margin: 0; padding: 0;"><b>ansys-dyna-core:</b> {pydyna_version}</p>
<p style="margin: 0; padding: 0;"><b>PyVista:</b> 0.46.5</p>
<p style="margin: 0; padding: 0;"><b>h5py:</b> {h5py_version}</p>
</td>
</tr>
</table>

<hr>

<h3>Intellectual Property {itp_logo_html}</h3>
<p>© 2026 ITP Aero. All rights reserved.</p>
<p>This software and its documentation are proprietary and confidential.
Unauthorized copying, distribution, or use of this software is strictly prohibited.</p>

<p><i>Developed by ITP Aero Engineering and Technology</i><br>
<i>Component Life-Impact team</i></p>
"""
        
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("About")
        msg_box.setWindowIcon(QIcon())  # No icon in title bar
        msg_box.setTextFormat(Qt.RichText)
        msg_box.setText(about_text)
        msg_box.setIcon(QMessageBox.NoIcon)
        
        msg_box.setMinimumWidth(500)
        msg_box.exec()

    def _show_contact_dialog(self):
        """Show the Contact dialog with contact information."""
        contact_email = "olben.falco@itpaero.com"
        
        contact_text = f"""
<h2>Contact Information</h2>

<p>For support, questions, or feedback about the LSPP Deck File Generator,
please contact us at:</p>

<p style="font-size: 14pt;">
<b>📧 Email:</b> {contact_email}
</p>

<hr>

<p><i>We aim to respond to all inquiries within 48 business hours.</i></p>
"""
        
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Contact")
        msg_box.setTextFormat(Qt.RichText)
        msg_box.setText(contact_text)
        msg_box.setIcon(QMessageBox.Information)
        
        msg_box.setMinimumWidth(500)
        
        # Add button to open email client
        email_btn = msg_box.addButton("Open Email Client", QMessageBox.ActionRole)
        msg_box.addButton(QMessageBox.Close)
        
        msg_box.exec()
        
        if msg_box.clickedButton() == email_btn:
            QDesktopServices.openUrl(QUrl(f"mailto:{contact_email}"))

    def _show_plot_dialog(self):
        """Open the Curve Plot dialog for data visualization."""
        from ui.plot_utils import PlotDialog
        
        dialog = PlotDialog(self, title="Curve Plot")
        dialog.show()  # Non-modal - allows interaction with main window

    def _show_user_manual(self):
        """Open the User Manual in an integrated HTML viewer or external browser."""
        from pathlib import Path
        
        # Check for README files
        app_dir = Path(__file__).parent.parent
        readme_en = app_dir / "README_EN.md"
        readme_es = app_dir / "README.md"
        
        # Try to open with the HelpViewer dialog (non-modal)
        try:
            from ui.help_viewer import HelpViewerDialog
            # If dialog already exists and is visible, just bring it to front
            if hasattr(self, '_help_viewer_dialog') and self._help_viewer_dialog is not None:
                try:
                    if self._help_viewer_dialog.isVisible():
                        self._help_viewer_dialog.raise_()
                        self._help_viewer_dialog.activateWindow()
                        return
                except RuntimeError:
                    # Dialog was deleted, create new one
                    pass
            
            # Create new non-modal dialog
            self._help_viewer_dialog = HelpViewerDialog(self)
            self._help_viewer_dialog.show()
        except ImportError:
            # Fallback: open README in default markdown viewer or browser
            if readme_en.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(readme_en)))
            elif readme_es.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(readme_es)))
            else:
                self.show_status_warning("User manual not found. Please check README files.", 8000)
    
    def _open_keyword_reference(self):
        """Open the LS-DYNA keyword reference documentation folder."""
        from pathlib import Path
        
        manual_dir = Path(__file__).parent.parent / "resources" / "LSDYNA_manual"
        
        if manual_dir.exists():
            # Open the folder in file explorer
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(manual_dir)))
        else:
            QMessageBox.warning(
                self,
                "Keyword Reference",
                "LS-DYNA manual folder not found.\n\n"
                f"Expected location: {manual_dir}\n\n"
                "Please ensure the LSDYNA_manual folder is in the resources directory."
            )
    
    def _open_online_docs(self):
        """Open documentation as HTML in the default web browser."""
        from pathlib import Path
        import re
        
        app_dir = Path(__file__).parent.parent
        readme_en = app_dir / "README_EN.md"
        readme_es = app_dir / "README.md"
        
        # Select the file to use
        if readme_en.exists():
            readme_path = readme_en
        elif readme_es.exists():
            readme_path = readme_es
        else:
            QMessageBox.warning(
                self,
                "Documentation",
                "Documentation files not found.\n\n"
                "Please check the README.md or README_EN.md files in the application directory."
            )
            return
        
        # Read markdown content
        content = readme_path.read_text(encoding="utf-8")
        
        # Convert to HTML
        html_content = self._convert_markdown_to_html(content, readme_path.name)
        
        # Save as HTML file in docs directory
        html_path = app_dir / "docs" / "manual.html"
        html_path.parent.mkdir(exist_ok=True)
        html_path.write_text(html_content, encoding="utf-8")
        
        # Open in browser
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(html_path)))
    
    def _convert_markdown_to_html(self, markdown_text: str, title: str = "Documentation") -> str:
        """Convert markdown to HTML with styling."""
        import re
        
        html = markdown_text
        
        # Code blocks
        html = re.sub(
            r'```(\w+)?\n(.*?)```', 
            r'<pre><code>\2</code></pre>', 
            html, 
            flags=re.DOTALL
        )
        
        # Headers
        html = re.sub(r'^#{6}\s+(.+)$', r'<h6>\1</h6>', html, flags=re.MULTILINE)
        html = re.sub(r'^#{5}\s+(.+)$', r'<h5>\1</h5>', html, flags=re.MULTILINE)
        html = re.sub(r'^#{4}\s+(.+)$', r'<h4>\1</h4>', html, flags=re.MULTILINE)
        html = re.sub(r'^#{3}\s+(.+)$', r'<h3>\1</h3>', html, flags=re.MULTILINE)
        html = re.sub(r'^#{2}\s+(.+)$', r'<h2>\1</h2>', html, flags=re.MULTILINE)
        html = re.sub(r'^#\s+(.+)$', r'<h1>\1</h1>', html, flags=re.MULTILINE)
        
        # Bold and italic
        html = re.sub(r'\*\*\*(.+?)\*\*\*', r'<strong><em>\1</em></strong>', html)
        html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html)
        html = re.sub(r'\*(.+?)\*', r'<em>\1</em>', html)
        
        # Inline code
        html = re.sub(r'`([^`]+)`', r'<code>\1</code>', html)
        
        # Links
        html = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', html)
        
        # Horizontal rules
        html = re.sub(r'^---+$', r'<hr>', html, flags=re.MULTILINE)
        
        # Tables
        lines = html.split('\n')
        in_table = False
        table_html = []
        result_lines = []
        is_header = True
        
        for line in lines:
            if '|' in line and '<pre>' not in line and '</pre>' not in line:
                if not in_table:
                    in_table = True
                    is_header = True
                    table_html = ['<table>']
                
                if re.match(r'^\|[\s\-:|]+\|$', line.strip()):
                    is_header = False
                    continue
                
                cells = [c.strip() for c in line.split('|')[1:-1]]
                if cells:
                    row_tag = 'th' if is_header else 'td'
                    row = '<tr>' + ''.join(f'<{row_tag}>{c}</{row_tag}>' for c in cells) + '</tr>'
                    table_html.append(row)
                    if is_header:
                        is_header = False
            else:
                if in_table:
                    table_html.append('</table>')
                    result_lines.append('\n'.join(table_html))
                    table_html = []
                    in_table = False
                result_lines.append(line)
        
        if in_table:
            table_html.append('</table>')
            result_lines.append('\n'.join(table_html))
        
        html = '\n'.join(result_lines)
        html = re.sub(r'\n\n+', '\n</p>\n<p>\n', html)
        
        return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} - LS-DYNA Keyword Manager</title>
    <style>
        body {{
            font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif;
            line-height: 1.7;
            max-width: 1000px;
            margin: 0 auto;
            padding: 40px 20px;
            color: #333;
            background: #fff;
        }}
        h1, h2, h3, h4, h5, h6 {{ color: #0066cc; margin-top: 2em; }}
        h1 {{ font-size: 2.5em; border-bottom: 3px solid #0066cc; padding-bottom: 15px; margin-top: 0; }}
        h2 {{ font-size: 1.8em; border-bottom: 1px solid #e0e0e0; padding-bottom: 10px; }}
        a {{ color: #0066cc; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        code {{ background: #f5f5f5; padding: 2px 6px; border-radius: 4px; font-family: Consolas, monospace; }}
        pre {{ background: #282c34; color: #abb2bf; padding: 20px; border-radius: 8px; overflow-x: auto; }}
        pre code {{ background: transparent; padding: 0; color: inherit; }}
        table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
        th, td {{ border: 1px solid #e0e0e0; padding: 12px 15px; text-align: left; }}
        th {{ background: #0066cc; color: white; }}
        tr:nth-child(even) {{ background: #f8f9fa; }}
        tr:hover {{ background: #e8f4fc; }}
        hr {{ border: none; border-top: 2px solid #e0e0e0; margin: 30px 0; }}
        .back-to-top {{ position: fixed; bottom: 30px; right: 30px; background: #0066cc; color: white; 
                        padding: 12px 16px; border-radius: 50%; text-decoration: none; font-size: 1.2em; }}
        .back-to-top:hover {{ background: #004999; text-decoration: none; }}
    </style>
</head>
<body>
<p>{html}</p>
<a href="#" class="back-to-top" title="Back to top">↑</a>
</body>
</html>'''

    def _create_plot_icon(self, size: int = 24) -> QIcon:
        """Create a plot/chart icon programmatically."""
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        
        painter = QPainter(pixmap)
        if not painter.isActive():
            return QIcon(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Draw axes
        pen = QPen(QColor("#333333"))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawLine(4, size - 4, 4, 4)  # Y axis
        painter.drawLine(4, size - 4, size - 4, size - 4)  # X axis
        
        # Draw chart line (upward trend)
        pen.setColor(QColor("#2196F3"))  # Blue
        pen.setWidth(2)
        painter.setPen(pen)
        points = [
            (6, size - 8),
            (10, size - 12),
            (14, size - 10),
            (18, size - 16),
            (size - 6, size - 18)
        ]
        for i in range(len(points) - 1):
            painter.drawLine(points[i][0], points[i][1], points[i+1][0], points[i+1][1])
        
        painter.end()
        return QIcon(pixmap)
    
    def _show_3d_viewer_v2(self):
        """Open the new 3D Viewer 2.0 dialog (ribbon + three-panel layout)."""
        fv = getattr(self, '_file_viewer', None)
        file_path = fv._current_file_path if fv else None
        if file_path is not None:
            from pathlib import Path
            p = Path(file_path)
            if not p.exists() or p.suffix.lower() not in (".k", ".key", ".dyn"):
                file_path = None
        from ui.model_viewer_v2 import KModelViewerV2Dialog
        dlg = KModelViewerV2Dialog(
            file_path=Path(file_path) if file_path else None,
            parent=self,
        )
        dlg.show()

    def _show_3d_viewer(self, force_native: bool = False):
        """Open a 3D viewer for the .k file selected in File Viewer.

        When *force_native* is ``True`` (or pyvistaqt is unavailable),
        the plain VTK window with keyboard shortcuts is used.
        Otherwise a Qt-embedded dialog with toolbars is opened.

        If no file is currently selected the viewer opens empty so the
        user can import a model via File > Import.
        """
        fv = getattr(self, '_file_viewer', None)
        file_path = fv._current_file_path if fv else None

        # Only pass the file if it's a supported .k/.key/.dyn model;
        # otherwise open the viewer empty (user can import via File menu).
        if file_path is not None:
            from pathlib import Path
            p = Path(file_path)
            if not p.exists() or p.suffix.lower() not in (".k", ".key", ".dyn"):
                file_path = None

        # Lazy-load heavy 3D libraries on first use with a progress dialog
        if not self._3d_libs_loaded:
            from PySide6.QtWidgets import QProgressDialog
            progress = QProgressDialog(
                "Loading 3D visualization libraries...\nThis only happens once per session.",
                None, 0, 0, self
            )
            progress.setWindowTitle("Loading 3D Libraries")
            progress.setMinimumDuration(0)
            progress.setModal(True)
            progress.show()
            QApplication.processEvents()
            try:
                import pyvista          # noqa: F401
                from pyvistaqt import QtInteractor  # noqa: F401
                self._3d_libs_loaded = True
            except Exception:
                pass  # model_viewer will handle missing deps
            finally:
                progress.close()

        from ui.model_viewer import open_k_model_viewer
        open_k_model_viewer(file_path, parent=self, force_native=force_native)

    def _create_exit_icon(self, size: int = 24) -> QIcon:
        """Create an exit icon (arrow pointing right out of a box)."""
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        
        painter = QPainter(pixmap)
        if not painter.isActive():
            return QIcon(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        
        pen = QPen(QColor("#000000"))  # Black
        pen.setWidth(2)
        painter.setPen(pen)
        
        # Draw the box (open on the right side)
        # Top line
        painter.drawLine(4, 4, size - 6, 4)
        # Bottom line
        painter.drawLine(4, size - 4, size - 6, size - 4)
        # Left line
        painter.drawLine(4, 4, 4, size - 4)
        # Top-right corner down
        painter.drawLine(size - 6, 4, size - 6, 8)
        # Bottom-right corner up
        painter.drawLine(size - 6, size - 4, size - 6, size - 8)
        
        # Draw arrow pointing right (exit arrow)
        arrow_y = size // 2
        # Arrow shaft
        painter.drawLine(8, arrow_y, size - 4, arrow_y)
        # Arrowhead
        painter.drawLine(size - 8, arrow_y - 4, size - 4, arrow_y)
        painter.drawLine(size - 8, arrow_y + 4, size - 4, arrow_y)
        
        painter.end()
        return QIcon(pixmap)
    
    def _create_unit_converter_icon(self, size: int = 24) -> QIcon:
        """Create a unit converter icon (two black arrows in opposite directions)."""
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        
        painter = QPainter(pixmap)
        if not painter.isActive():
            return QIcon(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        
        pen = QPen(QColor("#000000"))  # Black
        pen.setWidth(2)
        painter.setPen(pen)
        
        # Upper arrow: pointing right (→)
        y_top = 8
        painter.drawLine(4, y_top, size - 6, y_top)  # Line
        painter.drawLine(size - 10, y_top - 3, size - 6, y_top)  # Upper arrowhead
        painter.drawLine(size - 10, y_top + 3, size - 6, y_top)  # Lower arrowhead
        
        # Lower arrow: pointing left (←)
        y_bottom = size - 8
        painter.drawLine(size - 4, y_bottom, 6, y_bottom)  # Line
        painter.drawLine(10, y_bottom - 3, 6, y_bottom)  # Upper arrowhead
        painter.drawLine(10, y_bottom + 3, 6, y_bottom)  # Lower arrowhead
        
        painter.end()
        return QIcon(pixmap)
    
    def _create_velocity_calculator_icon(self, size: int = 24) -> QIcon:
        """Create a velocity calculator icon (v with arrow on top - vector notation)."""
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        
        painter = QPainter(pixmap)
        if not painter.isActive():
            return QIcon(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        
        pen = QPen(QColor("#000000"))  # Black
        pen.setWidth(2)
        painter.setPen(pen)
        
        # Draw "v" letter using Times New Roman italic font
        font = QFont("Times New Roman", 14)
        font.setItalic(True)
        font.setBold(True)
        painter.setFont(font)
        
        # Center the "v" horizontally
        v_center_x = size // 2
        painter.drawText(v_center_x - 5, size - 4, "v")
        
        # Draw arrow on top (vector notation)
        arrow_y = 6
        arrow_left = v_center_x - 6
        arrow_right = v_center_x + 6
        
        # Arrow line
        painter.drawLine(arrow_left, arrow_y, arrow_right, arrow_y)
        # Arrowhead pointing right
        painter.drawLine(arrow_right - 3, arrow_y - 3, arrow_right, arrow_y)
        painter.drawLine(arrow_right - 3, arrow_y + 3, arrow_right, arrow_y)
        
        painter.end()
        return QIcon(pixmap)

    # =========================================================================
    # Plugin System
    # =========================================================================
    
    def _load_plugins(self):
        """
        Load external plugins from the plugins directory.
        Plugins are added to the Tools menu after built-in tools.
        This does not affect existing functionality.
        """
        try:
            from core.tools.plugin_loader import PluginLoader
            
            self._plugin_loader = PluginLoader()
            loaded_count = self._plugin_loader.load_all_plugins()
            
            if loaded_count == 0:
                # No plugins found, that's OK
                self._loaded_plugins = []
                return
            
            # Add separator between built-in tools and plugins
            self._tools_menu.addSeparator()
            
            # Get all loaded plugins
            plugins = self._plugin_loader.get_plugins()
            
            # Store plugins for toolbar buttons (added later in _create_toolbar)
            self._loaded_plugins = plugins
            
            # Group by category
            categories = {}
            for plugin in plugins:
                cat = plugin.category
                if cat not in categories:
                    categories[cat] = []
                categories[cat].append(plugin)
            
            # Add plugins to menu
            if len(categories) == 1:
                # Single category: add directly to Tools menu
                for plugin in plugins:
                    self._add_plugin_action(self._tools_menu, plugin)
            else:
                # Multiple categories: create submenus
                for category in sorted(categories.keys()):
                    submenu = self._tools_menu.addMenu(category)
                    for plugin in categories[category]:
                        self._add_plugin_action(submenu, plugin)
            
            # Log success
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"Loaded {loaded_count} plugins into Tools menu")
            
        except Exception as e:
            # Plugin loading should never crash the app
            self._loaded_plugins = []
            import traceback
            print(f"Warning: Failed to load plugins: {e}")
            traceback.print_exc()
    
    def _add_plugin_toolbar_buttons(self, toolbar, plugins):
        """Add toolbar buttons for each plugin."""
        if not toolbar or not plugins:
            return
        
        # Icon mapping based on plugin name
        plugin_icons = {
            'unit_converter': self._create_unit_converter_icon(24),
            'velocity_calculator': self._create_velocity_calculator_icon(24),
        }
        
        for plugin in plugins:
            action = QAction(self)
            action.setToolTip(f"{plugin.display_name}\n{plugin.description}")
            
            # Get icon for this plugin - use plugin.name property directly
            plugin_id = plugin.name if hasattr(plugin, 'name') else ''
            if plugin_id in plugin_icons:
                action.setIcon(plugin_icons[plugin_id])
            elif plugin.icon:
                icon = get_icon("", plugin.icon)
                if not icon.isNull():
                    action.setIcon(icon)
            else:
                # Create generic tool icon
                action.setIcon(self._create_generic_tool_icon(24))
            
            # Connect to execute method
            action.triggered.connect(lambda checked, p=plugin: self._execute_plugin(p))
            
            toolbar.addAction(action)
            
            # Store reference
            if not hasattr(self, '_plugin_toolbar_actions'):
                self._plugin_toolbar_actions = []
            self._plugin_toolbar_actions.append(action)
    
    def _create_generic_tool_icon(self, size: int = 24) -> QIcon:
        """Create a generic tool icon."""
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        
        painter = QPainter(pixmap)
        if not painter.isActive():
            return QIcon(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Draw wrench/tool shape
        pen = QPen(QColor("#607D8B"))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(QColor("#90A4AE"))
        
        # Simple gear shape
        painter.drawEllipse(6, 6, size - 12, size - 12)
        painter.setBrush(Qt.transparent)
        painter.drawEllipse(9, 9, size - 18, size - 18)
        
        painter.end()
        return QIcon(pixmap)
    
    def _add_plugin_action(self, menu, plugin):
        """Add a plugin as an action to a menu."""
        action = QAction(plugin.display_name, self)
        action.setToolTip(plugin.description)
        
        # Set icon if available
        if plugin.icon:
            icon = get_icon("", plugin.icon)
            if not icon.isNull():
                action.setIcon(icon)
        
        # Set shortcut if available
        if plugin.shortcut:
            action.setShortcut(plugin.shortcut)
        
        # Connect to execute method
        action.triggered.connect(lambda checked, p=plugin: self._execute_plugin(p))
        
        menu.addAction(action)
        
        # Store reference to prevent garbage collection
        if not hasattr(self, '_plugin_actions'):
            self._plugin_actions = []
        self._plugin_actions.append(action)
    
    def _execute_plugin(self, plugin):
        """Execute a plugin safely."""
        try:
            plugin.execute(self)
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(
                self,
                f"Plugin Error: {plugin.display_name}",
                f"An error occurred while running the plugin:\n\n{str(e)}"
            )
            import traceback
            traceback.print_exc()

    def set_cleanup_callback(self, callback):
        """
        Set the cleanup callback to be called when the window closes.
        
        Args:
            callback: Function to call for cleanup (e.g., _cleanup_databases from main)
        """
        self._cleanup_callback = callback

    def closeEvent(self, event):
        """
        Handle window close event - ensures proper database cleanup.
        
        This is critical for compiled (.exe) versions where atexit handlers
        may not run reliably. We explicitly trigger database encryption and
        cleanup here.
        """
        import gc
        import sys
        
        print("MainWindow.closeEvent: Starting cleanup...")
        
        # Force garbage collection to release any SQLite connections
        gc.collect()
        
        # Use the callback if set
        if hasattr(self, '_cleanup_callback') and self._cleanup_callback is not None:
            try:
                print("MainWindow.closeEvent: Calling cleanup callback...")
                self._cleanup_callback()
                print("MainWindow.closeEvent: Cleanup callback completed.")
            except Exception as e:
                print(f"MainWindow.closeEvent: Error in cleanup callback: {e}")
                import traceback
                traceback.print_exc()
        else:
            # Fallback: try to import main module
            try:
                import main
                if hasattr(main, '_cleanup_databases'):
                    print("MainWindow.closeEvent: Triggering database cleanup via main module...")
                    main._cleanup_databases()
                    print("MainWindow.closeEvent: Database cleanup completed.")
            except Exception as e:
                print(f"MainWindow.closeEvent: Error during cleanup (fallback): {e}")
                import traceback
                traceback.print_exc()
        
        # Clear viewer VTK cache on app exit (persists across viewer opens
        # within a session for fast reopens, but freed when the app closes).
        try:
            from ui.viewer_v2_loader import viewer_cache_dir
            _d = viewer_cache_dir()
            if _d.exists():
                for _f in _d.iterdir():
                    if _f.suffix in (".vtp", ".json") and _f.is_file():
                        try:
                            _f.unlink()
                        except Exception:
                            pass
        except Exception:
            pass

        # Accept the close event
        event.accept()
        print("MainWindow.closeEvent: Close event accepted.")
