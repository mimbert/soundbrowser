import os, os.path, signal, sys, enum, copy
from lib.config import config, save_conf, STARTUP_PATH_MODE_SPECIFIED_PATH, STARTUP_PATH_MODE_LAST_PATH
from lib.utils import split_path_filename, format_duration
from lib.sound import SoundManager, SoundPlayer
from lib.logger import log, cyan
from gi.repository import Gst
from PySide2 import QtCore, QtGui, QtWidgets
from lib.ui_lib import main_win, prefs_dial
from lib.ui_utils import set_pixmap, set_dark_theme

SEEK_POS_UPDATER_INTERVAL_MS = 50
SEEK_MIN_INTERVAL_MS = 200
DEFAULT_SINK_DISPLAY_NAME = '(default)'

# Sound playing state from the UI's point of view
# (not to be confused with gstteamer state which is only PLAYING or PAUSED)
class SoundState(enum.Enum):
    STOPPED = 0
    PLAYING = 1
    PAUSED = 2

class MyQFileSystemModel(QtWidgets.QFileSystemModel):

    def __init__(self, show_hidden_files, *args, **kwds):
        super().__init__(*args, **kwds)
        self.show_hidden_files = show_hidden_files

    def hasChildren(self, parent):
        if self.flags(parent) & QtCore.Qt.ItemNeverHasChildren:
            return False
        try:
            with os.scandir(self.filePath(parent)) as it:
                for entry in it:
                    if (not entry.name.startswith('.') or self.show_hidden_files) and entry.is_dir():
                        return True
        except PermissionError:
            return False
        return False

class MyQSortFilterProxyModel(QtCore.QSortFilterProxyModel):

    def lessThan(self, left, right):
        if left.column() not in [ 0, 2 ] or right.column() not in [ 0, 2 ]:
            return super().lessThan(left, right)
        info_left = self.sourceModel().fileInfo(left)
        info_right =  self.sourceModel().fileInfo(right)
        if info_left.isDir() and info_right.isFile():
            return True
        elif info_left.isFile() and info_right.isDir():
            return False
        return super().lessThan(left, right)

    def filterAcceptsRow(self, source_row, source_parent):
        first_col_index = self.sourceModel().index(source_row, 0, source_parent);
        file_info = self.sourceModel().fileInfo(first_col_index)
        if file_info.isDir():
            return super().filterAcceptsRow(source_row, source_parent)
        filename = self.sourceModel().fileName(first_col_index)
        remaining, sep, ext = filename.rpartition('.')
        if not config['filter_files']: return True
        if not sep: return False
        if ext.lower() in [ e.lower() for e in config['file_extensions_filter'] ]: return True
        return False

class PrefsDialog(prefs_dial.Ui_PrefsDialog, QtWidgets.QDialog):

    def __init__(self, *args, **kwds):
        super(PrefsDialog, self).__init__(*args, **kwds)
        self.setupUi(self)
        self.populate()

    def populate(self):
        self.specified_path_button.clicked.connect(self.specified_path_button_clicked)

    @QtCore.Slot()
    def specified_path_button_clicked(self, checked = False):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "startup path", self.specified_path.text())
        if path:
            self.specified_path.setText(path)

class SoundBrowserUI(main_win.Ui_MainWindow, QtWidgets.QMainWindow):

    update_metadata_to_current_playing_message = QtCore.Signal()
    update_prefs_audio_sink_properties = QtCore.Signal()

    def __init__(self, startup_path, app, conf_file):
        super().__init__()
        self._state = SoundState.STOPPED
        self.app = app
        self.conf_file = conf_file
        self.manager = SoundManager()
        self.current_sound_selected = None
        self.current_sound_playing = None
        self.setupUi(self)
        self.in_keyboard_press_event = False
        self.populate(startup_path)
        self.player = SoundPlayer()
        self._playback_rate = 1.0
        self.seek_pos_update_timer = QtCore.QTimer()
        self.seek_min_interval_timer = None
        self.seek_next_value = None
        self.update_metadata_to_current_playing_message.connect(self.update_metadata_pane_to_current_playing)

    def __str__(self):
        return f"SoundBrowserUI <state={self.state.name}, current_sound_selected={self.current_sound_selected} current_sound_playing={self.current_sound_playing}>"

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        self._state = value
        if value == SoundState.STOPPED:
            self.play_button.setIcon(self.play_icon)
            self.update_ui_to_selection()
        elif value == SoundState.PLAYING:
            self.play_button.setIcon(self.pause_icon)
            self.play_button.setEnabled(True)
            self.stop_button.setEnabled(True)
        elif value == SoundState.PAUSED:
            self.play_button.setIcon(self.play_icon)
            self.play_button.setEnabled(True)
            self.stop_button.setEnabled(True)

    @property
    def playback_rate(self):
        if config['play_reverse']:
            return - self._playback_rate
        else:
            return self._playback_rate

    def clean_close(self):
        config['main_window_geometry'] = self.saveGeometry().data()
        config['main_window_state'] = self.saveState().data()
        config['splitter_state'] = self.splitter.saveState().data()
        if config['startup_path_mode'] == STARTUP_PATH_MODE_LAST_PATH:
            config['last_path'] = self.tableview_get_path(self.tableView.currentIndex())
        save_conf(self.conf_file)

    def closeEvent(self, event):
        self.clean_close()
        event.accept()

    def refresh_config(self):
        set_dark_theme(self.app, config['dark_theme'])
        self.fs_model.show_hidden_files = config['show_hidden_files']
        fs_model_filter = QtCore.QDir.NoDotAndDotDot | QtCore.QDir.AllDirs
        dir_model_filter = QtCore.QDir.Files | QtCore.QDir.AllDirs
        if config['show_hidden_files']:
            fs_model_filter |= QtCore.QDir.Hidden
            dir_model_filter |= QtCore.QDir.Hidden
        dir_model_filter |= QtCore.QDir.NoDot
        self.fs_model.setFilter(fs_model_filter)
        self.dir_model.setFilter(dir_model_filter)
        if config['show_metadata_pane']:
            self.bottom_pane.show()
        else:
            self.bottom_pane.hide()
        if config['hide_reverse']:
            self.reverse_button.hide()
            self.line_reverse_button.hide()
        else:
            self.reverse_button.show()
            self.line_reverse_button.show()
        if config['hide_tune']:
            self.tune_dial.hide()
            self.tune_value.hide()
        else:
            self.tune_dial.show()
            self.tune_value.show()

    def populate(self, startup_path):
        set_dark_theme(self.app, config['dark_theme'])
        self.fs_model = MyQFileSystemModel(config['show_hidden_files'], self)
        self.fs_model.setRootPath((QtCore.QDir.rootPath()))
        self.dir_model = QtWidgets.QFileSystemModel(self)
        self.dir_model.setRootPath((QtCore.QDir.rootPath()))
        self.treeView.setModel(self.fs_model)
        self.dir_proxy_model = MyQSortFilterProxyModel(self)
        self.dir_proxy_model.setSourceModel(self.dir_model)
        self.tableView.contextMenuEvent = self.tableView_contextMenuEvent
        self.tableView.setModel(self.dir_proxy_model)
        self.tableView.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.tableView.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.tableView.selectionModel().selectionChanged.connect(self.tableview_selection_changed)
        self.tableView.clicked.connect(self.tableview_clicked)
        self.orig_tableView_keyPressEvent = self.tableView.keyPressEvent
        self.tableView.keyPressEvent = self.tableview_keyPressEvent
        self.tableView.doubleClicked.connect(self.tableview_doubleClicked)
        self.tableView.setRootIndex(self.dir_proxy_model.mapFromSource(self.dir_model.index('/')))
        self.tableView.verticalHeader().hide()
        self.tableView.horizontalHeader().setSortIndicator(0, QtCore.Qt.AscendingOrder)
        self.tableView.setSortingEnabled(True)
        self.tableView.horizontalHeader().setStretchLastSection(True)
        self.tableView.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.treeView.setColumnHidden(1, True)
        self.treeView.setColumnHidden(2, True)
        self.treeView.setColumnHidden(3, True)
        self.treeView.selectionModel().selectionChanged.connect(self.treeview_selection_changed)
        self.treeView.setRootIndex(self.fs_model.index('/'))
        if not startup_path:
            if config['startup_path_mode'] == STARTUP_PATH_MODE_SPECIFIED_PATH:
                startup_path = config['specified_path']
                if not startup_path:
                    startup_path = os.getcwd()
            elif config['startup_path_mode'] == STARTUP_PATH_MODE_LAST_PATH:
                startup_path = config['last_path']
            elif config['startup_path_mode'] == STARTUP_PATH_MODE_CURRENT_DIR:
                startup_path = os.getcwd()
            elif config['startup_path_mode'] == STARTUP_PATH_MODE_HOME_DIR:
                startup_path = os.path.expanduser('~')
        self.goto_path(startup_path)
        self.treeView.header().setSortIndicator(0,QtCore.Qt.AscendingOrder)
        self.treeView.setSortingEnabled(True)
        self.treeView.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.dir_model.directoryLoaded.connect(self.dir_model_directory_loaded)
        self.locationBar.returnPressed.connect(self.locationBar_return_pressed)
        self.prefs_button.clicked.connect(self.prefs_button_clicked)
        self.seek_slider.orig_mousePressEvent = self.seek_slider.mousePressEvent
        self.seek_slider.orig_mouseMoveEvent = self.seek_slider.mouseMoveEvent
        self.seek_slider.orig_mouseReleaseEvent = self.seek_slider.mouseReleaseEvent
        self.seek_slider.mousePressEvent = self.slider_mousePressEvent
        self.seek_slider.mouseMoveEvent = self.slider_mouseMoveEvent
        self.seek_slider.mouseReleaseEvent = self.slider_mouseReleaseEvent
        self.loop_button.setChecked(config['play_looped'])
        self.show_hidden_files_button.setChecked(config['show_hidden_files'])
        self.show_metadata_pane_button.setChecked(config['show_metadata_pane'])
        self.filter_files_button.setChecked(config['filter_files'])
        self.loop_button.clicked.connect(self.loop_clicked)
        self.show_hidden_files_button.clicked.connect(self.show_hidden_files_clicked)
        self.show_metadata_pane_button.clicked.connect(self.show_metadata_pane_clicked)
        self.filter_files_button.clicked.connect(self.filter_files_clicked)
        self.copy_path_button.clicked.connect(self.copy_path_clicked)
        self.paste_path_button.clicked.connect(self.paste_path_clicked)
        self.play_button.clicked.connect(self.play_clicked)
        self.stop_button.clicked.connect(self.stop_clicked)
        self.pause_icon = QtGui.QIcon(":/icons/pause.png")
        self.play_icon = QtGui.QIcon(":/icons/play.png")
        self.play_icon.addFile(":/icons/play_disabled.png", mode=QtGui.QIcon.Disabled)
        self.refresh_config()
        if config['main_window_geometry']:
            self.restoreGeometry(QtCore.QByteArray(config['main_window_geometry']))
        if config['main_window_state']:
            self.restoreState(QtCore.QByteArray(config['main_window_state']))
        if config['splitter_state']:
            self.splitter.restoreState(QtCore.QByteArray(config['splitter_state']))
        self.tableView_contextMenu = QtWidgets.QMenu(self.tableView)
        reload_sound_action = QtWidgets.QAction("Reload", self.tableView)
        self.tableView_contextMenu.addAction(reload_sound_action)
        reload_sound_action.triggered.connect(self.reload_sound)
        self.state = SoundState.STOPPED
        tableView_return_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Return), self.tableView)
        tableView_enter_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Enter), self.tableView)
        tableView_return_shortcut.setContext(QtCore.Qt.WidgetShortcut)
        tableView_enter_shortcut.setContext(QtCore.Qt.WidgetShortcut)
        tableView_return_shortcut.activated.connect(self.tableView_return_pressed)
        tableView_enter_shortcut.activated.connect(self.tableView_return_pressed)
        loop_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_L), self)
        metadata_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_M), self)
        hidden_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_H), self)
        filter_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_F), self)
        play_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Space), self)
        stop_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Escape), self)
        loop_shortcut.activated.connect(self.loop_shortcut_activated)
        metadata_shortcut.activated.connect(self.metadata_shortcut_activated)
        hidden_shortcut.activated.connect(self.hidden_shortcut_activated)
        filter_shortcut.activated.connect(self.filter_shortcut_activated)
        play_shortcut.activated.connect(self.play_shortcut_activated)
        stop_shortcut.activated.connect(self.stop_shortcut_activated)
        self.copy_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtGui.QKeySequence.Copy), self)
        self.copy_shortcut.activated.connect(self.mainwin_copy)
        self.paste_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtGui.QKeySequence.Paste), self)
        self.paste_shortcut.activated.connect(self.mainwin_paste)
        self.tune_value.setFixedWidth(self.tune_value.height())
        self.tune_value.setFixedHeight(self.tune_value.height())
        self.tune_value.setText('0')
        self.tune_dial.valueChanged.connect(self.tune_dial_valueChanged)
        self.reverse_button.setChecked(config['play_reverse'])
        self.reverse_button.clicked.connect(self.reverse_clicked)
        reverse_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_R), self)
        reverse_shortcut.activated.connect(self.reverse_shortcut_activated)
        self.preference_dialog = PrefsDialog(self)
        self.preference_dialog.setMinimumSize(self.preference_dialog.size())
        self.preference_dialog.setMaximumSize(self.preference_dialog.size())
        self.update_prefs_audio_sink_properties.connect(self.prefs_fill_audio_sink_properties, QtCore.Qt.QueuedConnection)
        prefs_audio_sink_properties_del_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Delete), self.preference_dialog.audio_output_properties)
        prefs_audio_sink_properties_del_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        prefs_audio_sink_properties_del_shortcut.activated.connect(self.prefs_audio_sink_prop_del)
        prefs_audio_sink_properties_backspace_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Backspace), self.preference_dialog.audio_output_properties)
        prefs_audio_sink_properties_backspace_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        prefs_audio_sink_properties_backspace_shortcut.activated.connect(self.prefs_audio_sink_prop_del)
        self.clear_metadata_pane()
        self.tableView.setFocus()

    def showEvent(self, event):
        self.image.setFixedWidth(self.metadata.height())
        self.image.setFixedHeight(self.metadata.height())

    def update_ui_to_selection(self):
        if self.current_sound_selected:
            self.play_button.setEnabled(True)
            self.stop_button.setEnabled(True)
            self.seek_slider.setEnabled(True)
            self.seek_slider.setValue(0)
        else:
            self.play_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.seek_slider.setEnabled(False)
            self.seek_slider.setValue(0)

    def goto_path(self, path):
        directory, filename = split_path_filename(path)
        if directory:
            self.treeView.setCurrentIndex(self.fs_model.index(directory))
            self.treeView.expand(self.fs_model.index(directory))
            config['last_path'] = directory
            if filename:
                self.tableView.setRootIndex(self.dir_proxy_model.mapFromSource(self.dir_model.index(directory)))
                self.tableView.selectRow(self.dir_proxy_model.mapFromSource(self.dir_model.index(path)).row())
                config['last_path'] = path

    def select_path(self):
        fileinfo = self.dir_model.fileInfo(self.dir_proxy_model.mapToSource(self.tableView.currentIndex()))
        filepath = self.tableview_get_path(self.tableView.currentIndex())
        self.locationBar.setText(filepath)
        if fileinfo.isFile():
            previous_current_sound_selected = self.current_sound_selected
            self.current_sound_selected = self.manager.get(filepath)
            if self.current_sound_selected != previous_current_sound_selected:
                if config['reset_tune_between_sounds']:
                    self.tune_dial.setValue(0)
            if self.current_sound_selected:
                self.update_metadata_pane(self.current_sound_selected.metadata)
            else:
                self.clear_metadata_pane()
        else:
            self.current_sound_selected = None
        if self.state == SoundState.STOPPED:
            self.update_ui_to_selection()

    def update_metadata_field(self, field, value, force = None):
        f = getattr(self, field)
        l = getattr(self, field + '_label')
        if value or force == True:
            f.setText(str(value))
            f.setEnabled(True)
            l.setEnabled(True)
        if not value or force == False:
            f.setText(str(value))
            f.setEnabled(False)
            l.setEnabled(False)

    def clear_metadata_pane(self):
        for field, default_val in [
                ('title', ''),
                ('artist', ''),
                ('album', ''),
                ('album_artist', ''),
                ('track', '?/?'),
                ('duration', ''),
                ('genre', ''),
                ('date', ''),
                ('bpm', ''),
                ('key', ''),
                ('channel_mode', ''),
                ('audio_codec', ''),
                ('encoder', ''),
                ('bitrate', '? (min=?/max=?)'),
                ('comment', ''),
                ]:
            getattr(self, field).setText(default_val)
            getattr(self, field).setEnabled(False)
            getattr(self, field + '_label').setEnabled(False)
        self.image.setPixmap(None)

    def update_metadata_pane(self, metadata):
        m = metadata['all']
        self.update_metadata_field('title', m.get('title', ''))
        self.update_metadata_field('artist', m.get('artist', ''))
        self.update_metadata_field('album', m.get('album', ''))
        self.update_metadata_field('album_artist', m.get('album-artist', ''))
        self.update_metadata_field('track', str(m.get('track-number', '?')) + '/' + str(m.get('track-count', '?')),
                                   True if ('track-number' in m or 'track-count' in m) else False)
        self.update_metadata_field('duration', format_duration(m.get('duration')))
        self.update_metadata_field('genre', m.get('genre', ''))
        self.update_metadata_field('date', m.get('datetime', ''))
        self.update_metadata_field('bpm', f"{m['beats-per-minute']:.2f}" if 'beats-per-minute' in m else '')
        self.update_metadata_field('key', m.get('musical-key', ''))
        self.update_metadata_field('channel_mode', m.get('channel-mode', ''))
        self.update_metadata_field('audio_codec', m.get('audio-codec', ''))
        self.update_metadata_field('encoder', m.get('encoder', ''))
        self.update_metadata_field('bitrate', str(m.get('bitrate', '?')) + ' (min=' + str(m.get('minimum-bitrate', '?')) + '/max=' + str(m.get('maximum-bitrate', '?')) + ')',
                                   True if 'bitrate' in m else False)
        self.update_metadata_field('comment', m.get('comment', ''))
        if m.get('image'):
            set_pixmap(self.image, m.get('image'))
        else:
            self.image.setPixmap(None)

    @QtCore.Slot()
    def update_metadata_pane_to_current_playing(self):
        self.update_metadata_pane(self.current_sound_playing.metadata)

    @QtCore.Slot()
    def dir_model_directory_loaded(self, path):
        self.tableView.resizeColumnToContents(0)

    @QtCore.Slot()
    def treeview_selection_changed(self, selected, deselected):
        path = self.fs_model.filePath(self.treeView.currentIndex())
        self.locationBar.setText(path)
        self.tableView.setRootIndex(self.dir_proxy_model.mapFromSource(self.dir_model.index(path)))
        self.treeView.setCurrentIndex(self.fs_model.index(path))
        self.treeView.expand(self.fs_model.index(path))

    def tableview_get_path(self, index):
        return os.path.abspath(self.dir_model.filePath(self.dir_proxy_model.mapToSource(index)))

    @QtCore.Slot()
    def tableview_selection_changed(self, selected, deselected):
        if len(selected) == 1:
            self.select_path()
        if self.in_keyboard_press_event and config['autoplay_keyboard']:
            self.tableView_return_pressed(change_dir=False)

    def tableview_keyPressEvent(self, event):
        self.in_keyboard_press_event = True
        self.orig_tableView_keyPressEvent(event)
        self.in_keyboard_press_event = False

    @QtCore.Slot()
    def tableView_return_pressed(self, change_dir=True):
        if len(self.tableView.selectionModel().selectedRows()) == 1:
            self.select_path()
            fileinfo = self.dir_model.fileInfo(self.dir_proxy_model.mapToSource(self.tableView.currentIndex()))
            if fileinfo.isDir() and change_dir:
                path = self.tableview_get_path(self.tableView.currentIndex())
                self.tableView.setRootIndex(self.dir_proxy_model.mapFromSource(self.dir_model.index(path)))
                self.treeView.setCurrentIndex(self.fs_model.index(path))
                self.treeView.expand(self.fs_model.index(path))
            elif fileinfo.isFile():
                self.stop()
                self.play()

    @QtCore.Slot()
    def tableview_clicked(self, index):
        if config['autoplay_mouse']:
            self.tableView_return_pressed()

    @QtCore.Slot()
    def tableview_doubleClicked(self, index):
        if not config['autoplay_mouse']:
            self.tableView_return_pressed()

    @QtCore.Slot()
    def locationBar_return_pressed(self):
        self.goto_path(self.locationBar.text())

    @QtCore.Slot()
    def mainwin_copy(self):
        self.locationBar.setSelection(0, len(self.locationBar.text()))
        self.app.clipboard().setText(self.locationBar.text())

    @QtCore.Slot()
    def mainwin_paste(self):
        self.goto_path(self.app.clipboard().text())

    @QtCore.Slot()
    def copy_path_clicked(self, checked = False):
        self.locationBar.setSelection(0, len(self.locationBar.text()))
        self.app.clipboard().setText(self.locationBar.text())

    @QtCore.Slot()
    def paste_path_clicked(self, checked = False):
        self.goto_path(self.app.clipboard().text())

    @QtCore.Slot()
    def prefs_audio_sink_prop_del(self):
        item = self.preference_dialog.audio_output_properties.currentItem()
        if item:
            row = item.row()
            if row >= 0 and row < self.preference_dialog.audio_output_properties.rowCount() - 1:
                propkey = self.preference_dialog.audio_output_properties.item(row, 0).text()
                del self.tmpconfig['gst_audio_sink_properties'][self.preference_dialog.audio_output.currentText()][propkey]
                self.preference_dialog.audio_output_properties.removeRow(row)
                self.update_prefs_audio_sink_properties.emit()

    @QtCore.Slot()
    def prefs_audio_sink_prop_value_changed(self, item):
        if self.preference_dialog.audio_output.currentText() not in self.tmpconfig['gst_audio_sink_properties']:
            self.tmpconfig['gst_audio_sink_properties'] \
                [self.preference_dialog.audio_output.currentText()] = {}
        if item.row() == self.preference_dialog.audio_output_properties.rowCount() - 1:
            propkey = self.preference_dialog.audio_output_properties.cellWidget(item.row(), 0).currentText()
        else:
            propkey = self.preference_dialog.audio_output_properties.item(item.row(), 0).text()
        self.tmpconfig['gst_audio_sink_properties'] \
            [self.preference_dialog.audio_output.currentText()][propkey] \
            = self.preference_dialog.audio_output_properties.item(item.row(), 1).text()
        self.update_prefs_audio_sink_properties.emit()

    @QtCore.Slot()
    def prefs_fill_audio_sink_properties(self):
        audiosink = self.preference_dialog.audio_output.currentText()
        available_properties = get_available_gst_factory_supported_properties(audiosink)
        self.preference_dialog.audio_output_properties.blockSignals(True)
        self.preference_dialog.audio_output_properties.clear()
        self.preference_dialog.audio_output_properties.setHorizontalHeaderLabels([ 'property', 'value' ])
        self.preference_dialog.audio_output_properties.setRowCount(0)
        if audiosink in self.tmpconfig['gst_audio_sink_properties']:
            self.preference_dialog.audio_output_properties.setRowCount(len(self.tmpconfig['gst_audio_sink_properties'][audiosink]))
            for i, config_prop in enumerate(self.tmpconfig['gst_audio_sink_properties'][audiosink]):
                del available_properties[config_prop]
                kitem = QtWidgets.QTableWidgetItem(config_prop)
                kitem.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable)
                vitem = QtWidgets.QTableWidgetItem(self.tmpconfig['gst_audio_sink_properties'][audiosink][config_prop])
                vitem.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEditable)
                self.preference_dialog.audio_output_properties.setItem(i, 0, kitem)
                self.preference_dialog.audio_output_properties.setItem(i, 1, vitem)
        prop_selection_combo = QtWidgets.QComboBox(self.preference_dialog.audio_output_properties)
        prop_selection_combo.addItems(sorted(available_properties.keys()))
        self.preference_dialog.audio_output_properties.setRowCount(self.preference_dialog.audio_output_properties.rowCount() + 1)
        self.preference_dialog.audio_output_properties.setCellWidget(self.preference_dialog.audio_output_properties.rowCount() - 1, 0, prop_selection_combo)
        prop_value_item = QtWidgets.QTableWidgetItem('')
        prop_value_item.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsEditable)
        self.preference_dialog.audio_output_properties.setItem(self.preference_dialog.audio_output_properties.rowCount() - 1, 1, prop_value_item)
        self.preference_dialog.audio_output_properties.blockSignals(False)
        self.preference_dialog.audio_output_properties.itemChanged.connect(self.prefs_audio_sink_prop_value_changed)

    @QtCore.Slot()
    def prefs_button_clicked(self, checked = False):
        self.tmpconfig = copy.deepcopy(config)
        self.preference_dialog.check_autoplay_mouse.setChecked(self.tmpconfig['autoplay_mouse'])
        self.preference_dialog.check_autoplay_keyboard.setChecked(self.tmpconfig['autoplay_keyboard'])
        self.preference_dialog.check_dark_theme.setChecked(self.tmpconfig['dark_theme'])
        self.preference_dialog.check_dark_theme.stateChanged.connect(self.check_dark_theme_state_changed)
        self.preference_dialog.check_hide_reverse.setChecked(self.tmpconfig['hide_reverse'])
        self.preference_dialog.check_hide_tune.setChecked(self.tmpconfig['hide_tune'])
        self.preference_dialog.check_reset_tune_between_sounds.setChecked(self.tmpconfig['reset_tune_between_sounds'])
        self.preference_dialog.file_extensions_filter.setText(' '.join(self.tmpconfig['file_extensions_filter']))
        self.preference_dialog.specified_path.setText(self.tmpconfig['specified_path'])
        if self.tmpconfig['startup_path_mode'] == STARTUP_PATH_MODE_SPECIFIED_PATH:
            self.preference_dialog.startup_path_mode_specified_path.setChecked(True)
        elif self.tmpconfig['startup_path_mode'] == STARTUP_PATH_MODE_LAST_PATH:
            self.preference_dialog.startup_path_mode_last_path.setChecked(True)
        elif self.tmpconfig['startup_path_mode'] == STARTUP_PATH_MODE_CURRENT_DIR:
            self.preference_dialog.startup_path_mode_current_dir.setChecked(True)
        elif self.tmpconfig['startup_path_mode'] == STARTUP_PATH_MODE_HOME_DIR:
            self.preference_dialog.startup_path_mode_home_dir.setChecked(True)
        self.preference_dialog.audio_output.blockSignals(True)
        self.preference_dialog.audio_output.clear()
        self.preference_dialog.audio_output.addItems( [DEFAULT_SINK_DISPLAY_NAME] + [ fname for fname in self.available_gst_audio_sink_factories ])
        self.preference_dialog.audio_output.blockSignals(False)
        self.preference_dialog.audio_output.currentIndexChanged.connect(self.audio_output_prefs_index_changed)
        self.preference_dialog.audio_output.setCurrentIndex(self.preference_dialog.audio_output.findText(self.tmpconfig['gst_audio_sink']))
        self.prefs_fill_audio_sink_properties()
        if self.preference_dialog.exec_():
            self.tmpconfig['autoplay_mouse'] = self.preference_dialog.check_autoplay_mouse.isChecked()
            self.tmpconfig['autoplay_keyboard'] = self.preference_dialog.check_autoplay_keyboard.isChecked()
            self.tmpconfig['hide_reverse'] = self.preference_dialog.check_hide_reverse.isChecked()
            self.tmpconfig['hide_tune'] = self.preference_dialog.check_hide_tune.isChecked()
            self.tmpconfig['reset_tune_between_sounds'] = self.preference_dialog.check_reset_tune_between_sounds.isChecked()
            if self.preference_dialog.startup_path_mode_specified_path.isChecked():
                self.tmpconfig['startup_path_mode'] = STARTUP_PATH_MODE_SPECIFIED_PATH
            elif self.preference_dialog.startup_path_mode_last_path.isChecked():
                self.tmpconfig['startup_path_mode'] = STARTUP_PATH_MODE_LAST_PATH
            elif self.preference_dialog.startup_path_mode_current_dir.isChecked():
                self.tmpconfig['startup_path_mode'] = STARTUP_PATH_MODE_CURRENT_DIR
            elif self.preference_dialog.startup_path_mode_home_dir.isChecked():
                self.tmpconfig['startup_path_mode'] = STARTUP_PATH_MODE_HOME_DIR
            self.tmpconfig['specified_path'] = self.preference_dialog.specified_path.text()
            self.tmpconfig['file_extensions_filter'] = self.preference_dialog.file_extensions_filter.text().split(' ')
            self.tmpconfig['gst_audio_sink'] = self.preference_dialog.audio_output.currentText()
            config.update(self.tmpconfig)
            self.refresh_config()
            self.configure_audio_output()
        else:
            self.refresh_config()

    @QtCore.Slot()
    def check_dark_theme_state_changed(self):
        self.tmpconfig['dark_theme'] = self.preference_dialog.check_dark_theme.isChecked()
        set_dark_theme(self.app, self.preference_dialog.check_dark_theme.isChecked())

    @QtCore.Slot()
    def audio_output_prefs_index_changed(self):
        audiosink = self.preference_dialog.audio_output.currentText()
        for o in [ self.preference_dialog.label_gst_aa_details, self.preference_dialog.label_aa_long_name,
                   self.preference_dialog.audio_output_long_name, self.preference_dialog.label_aa_description,
                   self.preference_dialog.audio_output_description, self.preference_dialog.audio_output_description,
                   self.preference_dialog.label_aa_plugin, self.preference_dialog.audio_output_plugin,
                   self.preference_dialog.label_aa_plugin_description, self.preference_dialog.audio_output_plugin_description,
                   self.preference_dialog.label_aa_plugin_package, self.preference_dialog.audio_output_plugin_package,
                   self.preference_dialog.label_aa_properties, self.preference_dialog.audio_output_properties ]:
            o.setEnabled(audiosink not in [ DEFAULT_SINK_DISPLAY_NAME, '' ])
        if audiosink == '':
            audiosink == DEFAULT_SINK_DISPLAY_NAME
            #self.preference_dialog.audio_output.itemText(DEFAULT_SINK_DISPLAY_NAME)
        if audiosink == DEFAULT_SINK_DISPLAY_NAME:
            factory = None
            for o in [ self.preference_dialog.audio_output_long_name, self.preference_dialog.audio_output_description,
                       self.preference_dialog.audio_output_plugin, self.preference_dialog.audio_output_plugin_description,
                       self.preference_dialog.audio_output_plugin_package ]:
                o.setText('')
            self.preference_dialog.audio_output_properties.clear()
        else:
            factory = self.available_gst_audio_sink_factories[audiosink]
            self.preference_dialog.audio_output_long_name.setText(factory.get_metadata('long-name'))
            self.preference_dialog.audio_output_description.setText(factory.get_metadata('description'))
            self.preference_dialog.audio_output_plugin.setText(factory.get_plugin().get_name())
            self.preference_dialog.audio_output_plugin_description.setText(factory.get_plugin().get_description())
            self.preference_dialog.audio_output_plugin_package.setText(factory.get_plugin().get_package())
            self.prefs_fill_audio_sink_properties()

    def tableView_contextMenuEvent(self, event):
        index = self.tableView.indexAt(event.pos())
        if index:
            path = self.tableview_get_path(index)
            if path:
                self.tableView_contextMenu.path_to_reload = path
                self.tableView_contextMenu.popup(QtGui.QCursor.pos())

    @QtCore.Slot()
    def reload_sound(self):
        path = self.tableView_contextMenu.path_to_reload
        self.current_sound_selected = self.manager.get(path, force_reload=True)

    @QtCore.Slot()
    def loop_shortcut_activated(self):
        self.loop_button.click()

    @QtCore.Slot()
    def metadata_shortcut_activated(self):
        self.show_metadata_pane_button.click()

    @QtCore.Slot()
    def hidden_shortcut_activated(self):
        self.show_hidden_files_button.click()

    @QtCore.Slot()
    def filter_shortcut_activated(self):
        self.filter_files_button.click()

    @QtCore.Slot()
    def play_shortcut_activated(self):
        self.play_button.click()

    @QtCore.Slot()
    def stop_shortcut_activated(self):
        self.stop_button.click()

    @QtCore.Slot()
    def loop_clicked(self, checked = False):
        config['play_looped'] = checked

    @QtCore.Slot()
    def show_metadata_pane_clicked(self, checked = False):
        config['show_metadata_pane'] = checked
        self.refresh_config()

    @QtCore.Slot()
    def show_hidden_files_clicked(self, checked = False):
        config['show_hidden_files'] = checked
        self.refresh_config()

    @QtCore.Slot()
    def filter_files_clicked(self, checked = False):
        config['filter_files'] = checked
        self.refresh_config()
        self.dir_proxy_model.invalidateFilter()

    @QtCore.Slot()
    def play_clicked(self, checked):
        if self.state in [ SoundState.STOPPED, SoundState.PAUSED ] :
            self.play()
        else:
            self.pause()

    @QtCore.Slot()
    def stop_clicked(self, checked):
        self.stop()

    def get_slider_pos(self, mouse_event):
        return QtWidgets.QStyle.sliderValueFromPosition(self.seek_slider.minimum(), self.seek_slider.maximum(), mouse_event.pos().x(), self.seek_slider.geometry().width())

    def slider_mousePressEvent(self, mouse_event):
        self.disable_seek_pos_updates()
        return self.seek_slider.orig_mousePressEvent(mouse_event)

    def slider_mouseMoveEvent(self, mouse_event):
        return self.seek_slider.orig_mouseMoveEvent(mouse_event)

    def slider_mouseReleaseEvent(self, mouse_event):
        if self.state in [ SoundState.PLAYING, SoundState.PAUSED ]:
            self.seek(self.get_slider_pos(mouse_event))
        else:
            if self.current_sound_selected:
                self.play(self.get_slider_pos(mouse_event))
        self.enable_seek_pos_updates()
        return True

    @QtCore.Slot()
    def tune_dial_valueChanged(self, value):
        self.tune_value.setText(str(value))
        self.update_playback_rate(value)

    def notify_sound_stopped(self):
        self.state = SoundState.STOPPED
        self.disable_seek_pos_updates()
        self.seek_slider.setValue(100.0)
        log.debug(f"sound reached end")

    @QtCore.Slot()
    def reverse_clicked(self, checked = False):
        config['play_reverse'] = checked
        self.update_playback_rate()

    @QtCore.Slot()
    def reverse_shortcut_activated(self):
        self.reverse_button.click()

    @QtCore.Slot()
    def seek_position_updater(self):
        duration, position = self.player.get_duration_position()
        if duration != None:
            if 'duration' not in self.current_sound_playing.metadata[None] or 'duration' not in self.current_sound_playing.metadata['all']:
                self.current_sound_playing.metadata[None]['duration'] = self.current_sound_playing.metadata['all']['duration'] = duration
                self.update_metadata_pane(self.current_sound_playing.metadata)
            if position:
                signals_blocked = self.seek_slider.blockSignals(True)
                self.seek_slider.setValue(position * 100.0 / duration)
                self.seek_slider.blockSignals(signals_blocked)
                if position >= duration and not config['play_looped']:
                    self.notify_sound_stopped()

    def enable_seek_pos_updates(self):
        log.debug(f"enable seek pos updates")
        self.seek_pos_update_timer.timeout.connect(self.seek_position_updater)
        self.seek_pos_update_timer.start(SEEK_POS_UPDATER_INTERVAL_MS)

    def disable_seek_pos_updates(self):
        log.debug(f"disable seek pos updates")
        self.seek_pos_update_timer.stop()

    def play(self, start_pos=None):
        log.debug(f"play {self}")
        if (not self.current_sound_selected) and (not self.current_sound_playing):
            log.error(f"play called with no sound selected nor playing")
            return
        if self.state == SoundState.PLAYING:
            self.state = SoundState.STOPPED
            self.player.set_state(Gst.State.PAUSED)
        if self.state == SoundState.STOPPED:
            if self.current_sound_selected and self.current_sound_playing != self.current_sound_selected:
                self.player.set_path(self.current_sound_selected.path)
                self.current_sound_playing = sound
            elif self.current_sound_playing.file_changed():
                self.player.set_path(self.current_sound_playing.path)
                self.current_sound_playing = sound
            set_state_blocking(self.player, Gst.State.PAUSED)
            got_seek_query_answer, seek_query_answer = query_seek(self.player)
            if got_seek_query_answer and seek_query_answer.seekable:
                self.player.seek(self.playback_rate,
                                 Gst.Format.TIME,
                                 Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH,
                                 Gst.SeekType.SET, seek_query_answer.segment_start,
                                 Gst.SeekType.SET, seek_query_answer.segment_end)
        else:
            self.player.seek(self.playback_rate,
                             Gst.Format.TIME,
                             Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH,
                             Gst.SeekType.SET, 0,
                             Gst.SeekType.NONE, -1)
        if start_pos != None:
            self.actual_seek(start_pos)
        self.player.set_state(Gst.State.PLAYING)
        self.state = SoundState.PLAYING
        self.enable_seek_pos_updates()

    def pause(self):
        log.debug(f"pause {self}")
        if not self.state == SoundState.PLAYING:
            log.error(f"pause called with state = {self.state.name}")
            return
        if not self.current_sound_playing:
            log.error(f"pause called with current_sound_playing = {self.current_sound_playing}")
            return
        self.player.set_state(Gst.State.PAUSED)
        self.state = SoundState.PAUSED
        self.disable_seek_pos_updates()

    def stop(self):
        log.debug(f"stop {self}")
        self.player.set_state(Gst.State.PAUSED)
        got_seek_query_answer, seek_query_answer = query_seek(self.player)
        if got_seek_query_answer and seek_query_answer.seekable:
            self.player.seek(self.playback_rate,
                             Gst.Format.TIME,
                             Gst.SeekFlags.FLUSH,
                             Gst.SeekType.SET, seek_query_answer.segment_start,
                             Gst.SeekType.SET, seek_query_answer.segment_end)
        else:
            self.player.seek(self.playback_rate,
                             Gst.Format.TIME,
                             Gst.SeekFlags.FLUSH,
                             Gst.SeekType.SET, 0,
                             Gst.SeekType.NONE, -1)
        self.state = SoundState.STOPPED
        self.disable_seek_pos_updates()
        self._current_sound_playing = None
        self.seek_slider.setValue(0.0)

    def seek(self, position):
        if self.seek_min_interval_timer != None:
            log.debug(f"seek to {position} delayed to limit gst seek events frequency")
            self.seek_next_value = position
        else:
            self.actual_seek(position)
            self.seek_next_value = None
            self.seek_min_interval_timer = QtCore.QTimer()
            self.seek_min_interval_timer.setSingleShot(True)
            self.seek_min_interval_timer.timeout.connect(self.seek_min_interval_timer_fired)
            self.seek_min_interval_timer.start(SEEK_MIN_INTERVAL_MS)

    @QtCore.Slot()
    def seek_min_interval_timer_fired(self):
        if self.seek_next_value:
            self.actual_seek(self.seek_next_value)
        self.seek_next_value = None
        self.seek_min_interval_timer = None

    def actual_seek(self, position):
        got_duration, duration = self.player.query_duration(Gst.Format.TIME)
        got_seek_query, seek_query_answer = query_seek(self.player)
        if got_duration:
            seek_pos = position * duration / 100.0
            log.debug(f"seek to {format_duration(seek_pos)} {self}")
            if self.playback_rate > 0.0:
                self.player.seek(self.playback_rate,
                                        Gst.Format.TIME,
                                        Gst.SeekFlags.FLUSH,
                                        Gst.SeekType.SET, seek_pos,
                                        Gst.SeekType.SET, seek_query_answer.segment_end)
            else:
                self.player.seek(self.playback_rate,
                                        Gst.Format.TIME,
                                        Gst.SeekFlags.FLUSH,
                                        Gst.SeekType.SET, seek_query_answer.segment_start,
                                        Gst.SeekType.NONE, seek_pos)
        else:
            log.warning(f"unable to seek to {position}%, couldn't get duration")

    def update_playback_rate(self, value=None):
        if value != None:
            self._playback_rate = get_semitone_ratio(value)
            log.debug(f"update playback rate to {self.playback_rate} ({value} semitones)")
        else:
            log.debug(f"update playback rate to {self.playback_rate}")
        if self.state in [ SoundState.PLAYING, SoundState.PAUSED ]:
            got_seek_query_answer, seek_query_answer = query_seek(self.player)
            got_position, position = self.player.query_position(Gst.Format.TIME)
            if got_position:
                if self.playback_rate > 0.0:
                    if got_seek_query_answer and seek_query_answer.seekable:
                        self.player.seek(self.playback_rate,
                                         Gst.Format.TIME,
                                         Gst.SeekFlags.FLUSH,
                                         Gst.SeekType.SET, position,
                                         Gst.SeekType.SET, seek_query_answer.segment_end)
                    else:
                        self.player.seek(self.playback_rate,
                                         Gst.Format.TIME,
                                         Gst.SeekFlags.FLUSH,
                                         Gst.SeekType.SET, position,
                                         Gst.SeekType.NONE, -1)
                else:
                    if got_seek_query_answer and seek_query_answer.seekable:
                        self.player.seek(self.playback_rate,
                                         Gst.Format.TIME,
                                         Gst.SeekFlags.FLUSH,
                                         Gst.SeekType.SET, seek_query_answer.segment_start,
                                         Gst.SeekType.SET, position)
                    else:
                        self.player.seek(self.playback_rate,
                                         Gst.Format.TIME,
                                         Gst.SeekFlags.FLUSH,
                                         Gst.SeekType.NONE, -1,
                                         Gst.SeekType.NONE, position)
            else:
                log.warning(f"update_playback_rate: got_position, position = {got_position}, {position}")

def start_ui(startup_path, config_file):
    app = QtWidgets.QApplication([])
    app.setStyle("Fusion")
    app.default_palette = app.palette()
    sb = SoundBrowserUI(startup_path, app, config_file)
    def signal_handler(sig, frame):
        sb.clean_close()
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)
    sb.show()
    signal_handler_timer = QtCore.QTimer()
    signal_handler_timer.start(250)
    signal_handler_timer.timeout.connect(lambda: None)
    sys.exit(app.exec_())
