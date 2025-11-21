import os, os.path, enum
from lib.config import config, save_conf, STARTUP_PATH_MODE_SPECIFIED_PATH, STARTUP_PATH_MODE_LAST_PATH
from lib.utils import split_path_filename, format_duration
from lib.sound_player import SoundPlayer, PlayerStates, PlaybackDirection
from lib.sound_manager import SoundManager
from lib.logger import log, brightcyan, warmyellow
from PySide2 import QtCore, QtGui, QtWidgets
from lib.ui_lib import main_win
from lib.ui_utils import set_pixmap, set_dark_theme, SbQFileSystemModel, SbQSortFilterProxyModel
from lib.prefsdialog_ui import PrefsDialog

SEEK_POS_UPDATER_INTERVAL_MS = 50
SEEK_MIN_INTERVAL_MS = 200

class SoundBrowserUI(main_win.Ui_MainWindow, QtWidgets.QMainWindow):

    update_metadata_to_current_playing_message = QtCore.Signal()

    def __init__(self, startup_path, app):
        super().__init__()
        self.app = app
        self.current_sound_selected = None
        self.current_sound_playing = None
        self.in_keyboard_press_event = False
        self.manager = SoundManager()
        self.player = SoundPlayer()
        self.configure_audio_output()
        self.player.set_metadata_callback(self.update_metadata)
        self.player.set_state_change_callback(self.sound_player_state_changed)
        self.setupUi(self)
        self.populate(startup_path)
        self.seek_pos_update_timer = QtCore.QTimer()
        self.seek_min_interval_timer = None
        self.next_seek_pos = None
        self.update_metadata_to_current_playing_message.connect(self.update_metadata_pane_to_current_playing)

    def configure_audio_output(self):
        audio_config_success, gst_audio_sink, gst_audio_sink_properties = self.player.configure_audio_output(
            config['gst_audio_sink'],
            config['gst_audio_sink_properties'].get(config['gst_audio_sink'], {}))
        if audio_config_success:
            log.debug(f"successfuly configured gst_audio_sink={gst_audio_sink} properties={gst_audio_sink_properties}")
            config['gst_audio_sink'] = gst_audio_sink
            config['gst_audio_sink_properties'][config['gst_audio_sink']] = gst_audio_sink_properties
        else:
            log.warn(f"failed configuring gst_audio_sink={config['gst_audio_sink']} properties={config['gst_audio_sink_properties'][config['gst_audio_sink']]}")

    def update_metadata(self, metadata):
        for container_format in metadata:
            if 'image' in metadata[container_format]:
                img = QtGui.QImage()
                img.loadFromData(metadata[container_format]['image'])
                img = QtGui.QPixmap(img)
                metadata[container_format]['image'] = img
        self.current_sound_playing.update_metadata(metadata)
        self.update_metadata_to_current_playing_message.emit()

    def sound_player_state_changed(self, state):
        if state in [ PlayerStates.UNKNOWN, PlayerStates.ERROR, PlayerStates.STOPPED ]:
            self.disable_seek_pos_updates()
            self.play_button.setIcon(self.play_icon)
            self._current_sound_playing = None
            self.update_ui_to_selection()
        elif state == PlayerStates.PLAYING:
            self.play_button.setIcon(self.pause_icon)
            self.play_button.setEnabled(True)
            self.stop_button.setEnabled(True)
            self.enable_seek_pos_updates()
        elif state == PlayerStates.PAUSED:
            self.disable_seek_pos_updates()
            self.play_button.setIcon(self.play_icon)
            self.play_button.setEnabled(True)
            self.stop_button.setEnabled(True)

    def __str__(self):
        return f"SoundBrowserUI <state={self.player.player_state.name}, current_sound_selected={self.current_sound_selected} current_sound_playing={self.current_sound_playing}>"

    def clean_close(self):
        config['main_window_geometry'] = self.saveGeometry().data()
        config['main_window_state'] = self.saveState().data()
        config['splitter_state'] = self.splitter.saveState().data()
        if config['startup_path_mode'] == STARTUP_PATH_MODE_LAST_PATH:
            config['last_path'] = self.tableview_get_path(self.tableView.currentIndex())
        save_conf()

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
        self.fs_model = SbQFileSystemModel(config['show_hidden_files'], self)
        self.fs_model.setRootPath((QtCore.QDir.rootPath()))
        self.dir_model = QtWidgets.QFileSystemModel(self)
        self.dir_model.setRootPath((QtCore.QDir.rootPath()))
        self.treeView.setModel(self.fs_model)
        self.dir_proxy_model = SbQSortFilterProxyModel(self)
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
        else:
            self.play_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.seek_slider.setEnabled(False)

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
        if self.player.player_state == PlayerStates.STOPPED:
            self.update_ui_to_selection()
            self.seek_slider.setValue(0)

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
    def prefs_button_clicked(self, checked = False):
        self.preference_dialog.exec_prefs()

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
        if self.player.player_state in [ PlayerStates.STOPPED, PlayerStates.PAUSED ] :
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
        log.debug(warmyellow(f"slider_mouseReleaseEvent pos={self.get_slider_pos(mouse_event)}"))
        self.seek_slider.setValue(self.get_slider_pos(mouse_event))
        if self.player.player_state in [ PlayerStates.PLAYING, PlayerStates.PAUSED ]:
            self.seek(self.get_slider_pos(mouse_event) / 100.0)
            self.enable_seek_pos_updates()
        else:
            if self.current_sound_selected:
                self.play(self.get_slider_pos(mouse_event) / 100.0)
        return True

    @QtCore.Slot()
    def tune_dial_valueChanged(self, value):
        self.tune_value.setText(str(value))
        self.player.semitone = value

    @QtCore.Slot()
    def reverse_clicked(self, checked = False):
        if checked:
            self.player.direction = PlaybackDirection.BACKWARD
        else:
            self.player.direction = PlaybackDirection.FORWARD
        config['play_reverse'] = checked

    @QtCore.Slot()
    def reverse_shortcut_activated(self):
        self.reverse_button.click()

    # ------------------------------------------------------------------------
    # sound position update

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

    def enable_seek_pos_updates(self):
        log.debug(warmyellow(f"enable seek pos updates"))
        self.seek_pos_update_timer.timeout.connect(self.seek_position_updater)
        self.seek_pos_update_timer.start(SEEK_POS_UPDATER_INTERVAL_MS)

    def disable_seek_pos_updates(self):
        log.debug(warmyellow(f"disable seek pos updates"))
        self.seek_pos_update_timer.stop()

    @QtCore.Slot()
    def seek_min_interval_timer_fired(self):
        if self.next_seek_pos:
            self.player.seek(self.next_seek_pos)
        self.next_seek_pos = None
        self.seek_min_interval_timer = None

    def seek(self, seek_pos):
        # 0 <= seek_pos <= 1.0
        if self.seek_min_interval_timer != None:
            log.debug(f"seek to {seek_pos} delayed to limit gst seek events frequency")
            self.next_seek_pos = position
        else:
            self.player.seek(seek_pos)
            self.next_seek_pos = None
            self.seek_min_interval_timer = QtCore.QTimer()
            self.seek_min_interval_timer.setSingleShot(True)
            self.seek_min_interval_timer.timeout.connect(self.seek_min_interval_timer_fired)
            self.seek_min_interval_timer.start(SEEK_MIN_INTERVAL_MS)

    # ------------------------------------------------------------------------
    # play / pause / stop

    def play(self, start_pos=0):
        # 0 <= start_pos <= 1.0
        log.debug(brightcyan(f"play {self} start_pos={start_pos}"))
        if (not self.current_sound_selected) and (not self.current_sound_playing):
            log.error(f"play called with no sound selected nor playing")
            return
        if self.player.player_state in [ PlayerStates.PLAYING, PlayerStates.PAUSED ]:
            self.player.stop()
        if self.current_sound_selected and self.current_sound_playing != self.current_sound_selected:
            self.player.set_path(self.current_sound_selected.path)
            self.current_sound_playing = self.current_sound_selected
        elif self.current_sound_playing.file_changed():
            self.player.set_path(self.current_sound_playing.path)
        self.player.play(start_pos)

    def pause(self):
        log.debug(brightcyan(f"pause {self}"))
        if not self.player.player_state == PlayerStates.PLAYING:
            log.error(f"pause called with state = {self.state.name}")
            return
        if not self.current_sound_playing:
            log.error(f"pause called with current_sound_playing = {self.current_sound_playing}")
            return
        self.player.pause()

    def stop(self):
        log.debug(brightcyan(f"stop {self}"))
        self.player.stop()
        self.seek_slider.setValue(0.0)

