#!/usr/bin/env python3

import os, os.path, collections, yaml, schema, signal, sys, pathlib, threading, logging, argparse, traceback, enum, re, copy

from PySide2 import QtCore
from PySide2 import QtGui
from PySide2 import QtWidgets
from ui import main_win, prefs_dial

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gst', '1.0')
from gi.repository import GObject, Gst, Gtk, GLib

CACHE_SIZE = 256
SEEK_POS_UPDATER_INTERVAL_MS = 50
SEEK_MIN_INTERVAL_MS = 200
BLOCKING_GET_STATE_TIMEOUT = 1000 * Gst.MSECOND
CONF_FILE = os.path.expanduser("~/.soundbrowser.conf.yaml")

def log_callstack():
    log.debug(brightmagenta("callstack:\n" + "".join(traceback.format_list(traceback.extract_stack())[:-1])))

def cyan(s):
    return '\033[36m' + s + '\033[m'

def brightmagenta(s):
    return '\033[95m' + s + '\033[m'

class CustomFormatter(logging.Formatter):
    grey = '\033[2m\033[37m'
    brightyellow = '\033[93m'
    brightred = '\033[91m'
    reversebrightboldred = '\033[7m\033[1m\033[91m'
    reset = '\033[m'
    format = "%(asctime)s %(levelname)s %(message)s"
    FORMATTERS = {
        logging.DEBUG: logging.Formatter(grey + format + reset),
        logging.INFO: logging.Formatter(format),
        logging.WARNING: logging.Formatter(brightyellow + format + reset),
        logging.ERROR: logging.Formatter(brightred + format + reset),
        logging.CRITICAL: logging.Formatter(reversebrightboldred + format + reset),
    }
    def format(self, record):
        return self.FORMATTERS.get(record.levelno).format(record)

log = logging.getLogger()
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(CustomFormatter())
_handler.setLevel(logging.DEBUG)
log.addHandler(_handler)

STARTUP_PATH_MODE_SPECIFIED_PATH = 1
STARTUP_PATH_MODE_LAST_PATH = 2
STARTUP_PATH_MODE_CURRENT_DIR = 3
STARTUP_PATH_MODE_HOME_DIR = 4

conf_schema = schema.Schema({
    schema.Optional('startup_path_mode', default=STARTUP_PATH_MODE_HOME_DIR): int,
    schema.Optional('specified_path', default=os.path.expanduser('~')): str,
    schema.Optional('last_path', default=os.path.expanduser('~')): str,
    schema.Optional('show_hidden_files', default=False): bool,
    schema.Optional('show_metadata_pane', default=True): bool,
    schema.Optional('main_window_geometry', default=None): bytes,
    schema.Optional('main_window_state', default=None): bytes,
    schema.Optional('splitter_state', default=None): bytes,
    schema.Optional('play_looped', default=False): bool,
    schema.Optional('file_extensions_filter', default=['wav', 'mp3', 'aiff', 'flac', 'ogg', 'm4a', 'aac']): [str],
    schema.Optional('filter_files', default=True): bool,
    schema.Optional('gst_audio_sink', default=''): str,
    schema.Optional('gst_audio_sink_properties', default={}): {schema.Optional(str): {schema.Optional(str): str}},
})

def load_conf(path):
    log.debug(f"loading conf from {path}")
    try:
        with open(path) as fh:
            conf = yaml.safe_load(fh)
    except OSError:
        log.debug(f"error reading conf from {path}, using an empty conf")
        conf = {}
    return conf_schema.validate(conf)

def save_conf(path, conf):
    conf = conf_schema.validate(conf)
    log.debug(f"saving conf to {path}")
    try:
        with open(path, 'w') as fh:
            yaml.dump(conf, fh)
    except OSError:
        log.debug(f"unable to save conf to {path}")

_blacklisted_gst_audio_sink_factory_regexes = [
    '^interaudiosink$',
    '^ladspasink.*',
]
def get_available_gst_audio_sink_factories():
    factories = Gst.Registry.get().get_feature_list(Gst.ElementFactory)
    audio_sinks_factories = [ f for f in factories if ('Audio' in f.get_klass() and ('sink' in f.name or 'Sink' in f.get_klass())) ]
    for regex in _blacklisted_gst_audio_sink_factory_regexes:
        audio_sinks_factories = [ f for f in audio_sinks_factories if not re.search(regex, f.name) ]
    return { f.name: f for f in audio_sinks_factories }

def get_available_gst_factory_supported_properties(factory_name):
    element = Gst.ElementFactory.make(factory_name, None)
    properties = {}
    for p in element.list_properties():
        if not(p.flags & GObject.ParamFlags.WRITABLE):
            continue
        # if not(p.value_type.name in [ 'gchararray', 'gboolean', 'gint64', 'guint', 'gint64', 'gint', 'gdouble']):
        #     continue
        properties[p.name] = p
    return properties

def cast_str_to_prop_pytype(prop, s):
    if prop.value_type.name == 'gchararray':
        return s
    elif prop.value_type.name == 'gboolean':
        return s.lower() in [ 'true', '1' ]
    elif prop.value_type.name in [ 'gint64', 'guint', 'gint64', 'gint' ]:
        return int(s)
    elif prop.value_type.name == 'gdouble':
        return float(s)
    else:
        return s

class LRU(collections.OrderedDict):
    'Limit size, evicting the least recently looked-up key when full'

    def __init__(self, maxsize=128, *args, **kwds):
        self.maxsize = maxsize
        super().__init__(*args, **kwds)

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.maxsize:
            oldest = next(iter(self))
            log.debug(f"LRU max size, removing {oldest}")
            del self[oldest]

def parse_tag_list(taglist):
    tmp = {}
    containers = {}
    for i in range(taglist.n_tags()):
        tag = taglist.nth_tag_name(i)
        value = None
        if tag in [ 'title', 'artist', 'album', 'genre', 'musical-key', 'album-artist', 'encoder', 'channel-mode', 'audio-codec', 'container-format', 'comment' ]:
            value = taglist.get_string(tag)
        elif tag in [ 'track-count', 'track-number', 'minimum-bitrate', 'maximum-bitrate', 'bitrate' ]:
            value = taglist.get_uint(tag)
        elif tag == 'duration':
            value = taglist.get_uint64(tag)
        elif tag in [  'beats-per-minute', 'replaygain-track-gain', 'replaygain-album-gain', 'replaygain-track-peak', 'replaygain-album-peak' ]:
            value = taglist.get_double(tag)
        elif tag == 'datetime':
            value = taglist.get_date_time(tag)
            if value[0]:
                value = (True, value[1].to_iso8601_string())
            else:
                value = taglist.get_date(tag)
                if value[0]:
                    value = (True, value[1].to_struct_tm()) # never tested, need to find an example stream
        elif tag == 'has-crc':
            value = taglist.get_boolean(tag)
        elif tag == 'image':
            value = taglist.get_sample(tag)
            memmap = value[1].get_buffer().get_all_memory().map(Gst.MapFlags.READ)
            bytearr = QtCore.QByteArray(memmap.data.tobytes())
            img = QtGui.QImage()
            img.loadFromData(bytearr)
            img = QtGui.QPixmap(img)
            value = (True, img)
        if value and value[0]:
            if tag == 'container-format':
                containers[value[1]] = tmp
                tmp = {}
            else:
                tmp[tag] = value[1]
    if len(tmp) > 0:
        containers[None] = tmp
        tmp = {}
    return containers

def get_milliseconds_suffix(secs):
    ms_suffix = ""
    msecs = int (round(secs - int(secs), 3) * 1000)
    if msecs != 0:
        ms_suffix = ".%03i" % msecs
    return ms_suffix

def format_duration(nsecs):
    if nsecs == None:
        return ''
    secs = nsecs / 1e9
    formatted_duration = ""
    if secs < 0:
        secs = -secs
        formatted_duration += "-"
    s = secs
    d = (s - (s % 86400)) // 86400
    s -= d * 86400
    h = (s - (s % 3600)) // 3600
    s -= h * 3600
    m = (s - (s % 60)) // 60
    s -= m * 60
    if secs >= 86400: formatted_duration += "%id" % d
    if secs >= 3600: formatted_duration += "%ih" % h
    if secs >= 60: formatted_duration += "%im" % m
    formatted_duration += "%i%ss" % (s, get_milliseconds_suffix(s))
    return formatted_duration

def split_path_filename(s):
    if os.path.isdir(s):
        return s, None
    elif os.path.isfile(s):
        return os.path.dirname(s), os.path.basename(s)
    else:
        return None, None

def set_pixmap(qlabel, qpixmap):
    w = qlabel.width()
    h = qlabel.height()
    qlabel.setAlignment(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter)
    qlabel.setPixmap(qpixmap.scaled(w, h, QtCore.Qt.KeepAspectRatio))

def log_gst_message(message):
    log.debug(cyan(f"gst message: {message.type.first_value_name}: {message.get_structure().to_string() if message.get_structure() else 'None'}"))

class Sound(QtCore.QObject):

    def __init__(self, path = None, stat_result = None):
        super().__init__()
        log.debug(f"new sound path={path} stat={stat_result}")
        self.metadata = { None: {}, 'all': {} }
        self.path = path
        self.stat_result = stat_result

    def __str__(self):
        return f"Sound@0x{id(self):x}<path={self.path}>"

    def update_metadata(self, metadata):
        for k in metadata:
            if not k in self.metadata:
                self.metadata[k] = {}
            self.metadata[k].update(metadata[k])
            self.metadata['all'].update(metadata[k])

def file_changed(sound):
    try:
        stat_result = os.stat(sound.path)
    except:
        log.debug(f"file_changed?: unable to stat {sound.path}")
        return True
    return stat_result.st_mtime_ns > sound.stat_result.st_mtime_ns

class SoundManager():

    def __init__(self):
        self._cache = LRU(maxsize = CACHE_SIZE) # keys: file pathes. Values: Sound

    def get(self, path, force_reload=False ):
        if path in self._cache and not force_reload:
            if not os.path.isfile(path):
                log.debug(f"SoundManager: sound in cache, but there is no file anymore, discard it ({self._cache[path]})")
                del self._cache[path]
                return None
            sound = self._cache[path]
            if file_changed(sound):
                log.debug(f"SoundManager: sound in cache but changed on disk, reload it ({self._cache[path]})")
                return self._load(path)
            return sound
        else:
            log.debug(f"SoundManager: sound not in cache, or reload forced, load it ({path})")
            return self._load(path)

    def is_loaded(self, path):
        return path in self._cache

    def _load(self, path):
        if not os.path.isfile(path):
            log.debug(f"SoundManager: not an existing file, unable to load {path}")
            return None
        try:
            stat_result=os.stat(path)
        except:
            log.debug(f"SoundManager: unable to stat, unable to load {path}")
            return None
        sound = Sound(path=path, stat_result=stat_result)
        self._cache[path] = sound
        return sound

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
        if not self.parent().config['filter_files']: return True
        if not sep: return False
        if ext.lower() in [ e.lower() for e in self.parent().config['file_extensions_filter'] ]: return True
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

# not to be confused with gst state which is only PLAYING or PAUSED
SoundState = enum.Enum('SoundState', ['STOPPED', 'PLAYING', 'PAUSED'])

class SoundBrowser(main_win.Ui_MainWindow, QtWidgets.QMainWindow):

    update_metadata_to_current_playing_message = QtCore.Signal()
    update_prefs_audio_sink_properties = QtCore.Signal()

    def __init__(self, startup_path, clipboard, conf_file):
        super().__init__()
        self._state = SoundState.STOPPED
        self.clipboard = clipboard
        self.conf_file = conf_file
        self.config = load_conf(self.conf_file)
        self.available_gst_audio_sink_factories = get_available_gst_audio_sink_factories()
        self.manager = SoundManager()
        self.current_sound_selected = None
        self.current_sound_playing = None
        self.setupUi(self)
        self.populate(startup_path)
        self.player = Gst.ElementFactory.make('playbin')
        self.player.set_property('flags', self.player.get_property('flags') & ~(0x00000001 | 0x00000004 | 0x00000008)) # disable video, subtitles, visualisation
        self.configure_audio_output()
        self.player.get_bus().add_watch(GLib.PRIORITY_DEFAULT, self.gst_bus_message_handler, None)
        self.seek_pos_update_timer = QtCore.QTimer()
        self.seek_min_interval_timer = None
        self.seek_next_value = None
        self.update_metadata_to_current_playing_message.connect(self.update_metadata_pane_to_current_playing)

    def __str__(self):
        return f"SoundBrowser <state={self.state.name}, current_sound_selected={self.current_sound_selected} current_sound_playing={self.current_sound_playing}>"

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

    def clean_close(self):
        self.config['main_window_geometry'] = self.saveGeometry().data()
        self.config['main_window_state'] = self.saveState().data()
        self.config['splitter_state'] = self.splitter.saveState().data()
        if self.config['startup_path_mode'] == STARTUP_PATH_MODE_LAST_PATH:
            self.config['last_path'] = self.tableview_get_path(self.tableView.currentIndex())
        save_conf(self.conf_file, self.config)

    def closeEvent(self, event):
        self.clean_close()
        event.accept()

    def refresh_config(self):
        self.fs_model.show_hidden_files = self.config['show_hidden_files']
        fs_model_filter = QtCore.QDir.NoDotAndDotDot | QtCore.QDir.AllDirs
        dir_model_filter = QtCore.QDir.Files | QtCore.QDir.AllDirs
        if self.config['show_hidden_files']:
            fs_model_filter |= QtCore.QDir.Hidden
            dir_model_filter |= QtCore.QDir.Hidden
        dir_model_filter |= QtCore.QDir.NoDot
        self.fs_model.setFilter(fs_model_filter)
        self.dir_model.setFilter(dir_model_filter)
        if self.config['show_metadata_pane']:
            self.bottom_pane.show()
        else:
            self.bottom_pane.hide()

    def configure_audio_output(self):
        log.debug(f"check gst sink {self.config['gst_audio_sink']} available")
        if self.config['gst_audio_sink'] not in self.available_gst_audio_sink_factories:
            log.info(f"unavailable gstreamer audio sink '{self.config['gst_audio_sink']}', using default")
            self.config['gst_audio_sink'] = ''
        if self.config['gst_audio_sink']:
            if self.config['gst_audio_sink'] not in self.config['gst_audio_sink_properties']:
                self.config['gst_audio_sink_properties'][self.config['gst_audio_sink']] = {}
            available_properties = get_available_gst_factory_supported_properties(self.config['gst_audio_sink'])
            for config_prop in list(self.config['gst_audio_sink_properties'][self.config['gst_audio_sink']].keys()):
                log.debug(f"check gst sink property {config_prop} available for {self.config['gst_audio_sink']}")
                if config_prop not in available_properties:
                    log.info(f"unavailable gstreamer audio sink '{self.config['gst_audio_sink']}' property '{config_prop}', removing it from config")
                    del self.config['gst_audio_sink_properties'][self.config['gst_audio_sink']][config_prop]
            audiosink = Gst.ElementFactory.make(self.config['gst_audio_sink'])
            for k, v in self.config['gst_audio_sink_properties'][self.config['gst_audio_sink']].items():
                try:
                    audiosink.set_property(k, cast_str_to_prop_pytype(available_properties[k], v))
                except:
                    log.error(f"gst sink {self.config['gst_audio_sink']}: unable to set property {k} to value {cast_str_to_prop_pytype(available_properties[k], v)}")
            try:
                self.player.set_property("audio-sink", audiosink)
            except:
                log.error(f"gst playbin: unable to set audiosink to {self.config['gst_audio_sink']}")

    def populate(self, startup_path):
        self.fs_model = MyQFileSystemModel(self.config['show_hidden_files'], self)
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
            if self.config['startup_path_mode'] == STARTUP_PATH_MODE_SPECIFIED_PATH:
                startup_path = self.config['specified_path']
                if not startup_path:
                    startup_path = os.getcwd()
            elif self.config['startup_path_mode'] == STARTUP_PATH_MODE_LAST_PATH:
                startup_path = self.config['last_path']
            elif self.config['startup_path_mode'] == STARTUP_PATH_MODE_CURRENT_DIR:
                startup_path = os.getcwd()
            elif self.config['startup_path_mode'] == STARTUP_PATH_MODE_HOME_DIR:
                startup_path = os.path.expanduser('~')
        self.goto_path(startup_path)
        self.treeView.header().setSortIndicator(0,QtCore.Qt.AscendingOrder)
        self.treeView.setSortingEnabled(True)
        self.treeView.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.dir_model.directoryLoaded.connect(self.dir_model_directory_loaded)
        self.locationBar.returnPressed.connect(self.locationBar_return_pressed)
        self.prefs_button.clicked.connect(self.prefs_button_clicked)
        self.seek_slider.mousePressEvent = self.slider_mousePressEvent
        self.seek_slider.mouseMoveEvent = self.slider_mouseMoveEvent
        self.seek_slider.mouseReleaseEvent = self.slider_mouseReleaseEvent
        self.loop_button.setChecked(self.config['play_looped'])
        self.show_hidden_files_button.setChecked(self.config['show_hidden_files'])
        self.show_metadata_pane_button.setChecked(self.config['show_metadata_pane'])
        self.filter_files_button.setChecked(self.config['filter_files'])
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
        if self.config['main_window_geometry']:
            self.restoreGeometry(QtCore.QByteArray(self.config['main_window_geometry']))
        if self.config['main_window_state']:
            self.restoreState(QtCore.QByteArray(self.config['main_window_state']))
        if self.config['splitter_state']:
            self.splitter.restoreState(QtCore.QByteArray(self.config['splitter_state']))
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
        self.preference_dialog = PrefsDialog(self)
        self.preference_dialog.setMinimumSize(self.preference_dialog.size())
        self.preference_dialog.setMaximumSize(self.preference_dialog.size())
        self.update_prefs_audio_sink_properties.connect(self.prefs_fill_audio_sink_properties, QtCore.Qt.QueuedConnection)
        prefs_audio_sink_properties_del_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Delete), self.preference_dialog.audio_output_properties)
        prefs_audio_sink_properties_del_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        prefs_audio_sink_properties_del_shortcut.activated.connect(self.prefs_audio_sink_prop_del)
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
            self.config['last_path'] = directory
            if filename:
                self.tableView.setRootIndex(self.dir_proxy_model.mapFromSource(self.dir_model.index(directory)))
                self.tableView.selectRow(self.dir_proxy_model.mapFromSource(self.dir_model.index(path)).row())
                self.config['last_path'] = path

    def select_path(self):
        fileinfo = self.dir_model.fileInfo(self.dir_proxy_model.mapToSource(self.tableView.currentIndex()))
        filepath = self.tableview_get_path(self.tableView.currentIndex())
        self.locationBar.setText(filepath)
        if fileinfo.isFile():
            self.current_sound_selected = self.manager.get(filepath)
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
        log.debug(f"tableview_selection_changed  len(selected)={len(selected)}")
        if len(selected) == 1:
            self.select_path()

    @QtCore.Slot()
    def tableView_return_pressed(self):
        self.tableView.selectionModel().selectedRows()
        log.debug(f"tableview_return_pressed  len(self.tableView.selectionModel().selectedRows())={len(self.tableView.selectionModel().selectedRows())}")
        if len(self.tableView.selectionModel().selectedRows()) == 1:
            self.select_path()
            fileinfo = self.dir_model.fileInfo(self.dir_proxy_model.mapToSource(self.tableView.currentIndex()))
            if fileinfo.isDir():
                path = self.tableview_get_path(self.tableView.currentIndex())
                self.tableView.setRootIndex(self.dir_proxy_model.mapFromSource(self.dir_model.index(path)))
                self.treeView.setCurrentIndex(self.fs_model.index(path))
                self.treeView.expand(self.fs_model.index(path))
            elif fileinfo.isFile():
                self.stop()
                self.play()

    @QtCore.Slot()
    def tableview_clicked(self, index):
        self.tableView_return_pressed()

    @QtCore.Slot()
    def locationBar_return_pressed(self):
        self.goto_path(self.locationBar.text())

    @QtCore.Slot()
    def mainwin_copy(self):
        self.locationBar.setSelection(0, len(self.locationBar.text()))
        self.clipboard.setText(self.locationBar.text())

    @QtCore.Slot()
    def mainwin_paste(self):
        self.goto_path(self.clipboard.text())

    @QtCore.Slot()
    def copy_path_clicked(self, checked = False):
        self.locationBar.setSelection(0, len(self.locationBar.text()))
        self.clipboard.setText(self.locationBar.text())

    @QtCore.Slot()
    def paste_path_clicked(self, checked = False):
        self.goto_path(self.clipboard.text())

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
        self.tmpconfig = copy.deepcopy(self.config)
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
        self.preference_dialog.audio_output.addItems( ['(default)'] + [ fname for fname in self.available_gst_audio_sink_factories ])
        self.preference_dialog.audio_output.blockSignals(False)
        self.preference_dialog.audio_output.currentIndexChanged.connect(self.audio_output_prefs_index_changed)
        self.preference_dialog.audio_output.setCurrentIndex(self.preference_dialog.audio_output.findText(self.tmpconfig['gst_audio_sink']))
        self.prefs_fill_audio_sink_properties()
        if self.preference_dialog.exec_():
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
            self.config = self.tmpconfig
            self.refresh_config()
            self.configure_audio_output()

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
            o.setEnabled(audiosink != '(default)')
        if audiosink == '(default)':
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
        self.config['play_looped'] = checked

    @QtCore.Slot()
    def show_metadata_pane_clicked(self, checked = False):
        self.config['show_metadata_pane'] = checked
        self.refresh_config()

    @QtCore.Slot()
    def show_hidden_files_clicked(self, checked = False):
        self.config['show_hidden_files'] = checked
        self.refresh_config()

    @QtCore.Slot()
    def filter_files_clicked(self, checked = False):
        self.config['filter_files'] = checked
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

    def slider_seek_to_pos(self, mouse_event):
        position = QtWidgets.QStyle.sliderValueFromPosition(self.seek_slider.minimum(), self.seek_slider.maximum(), mouse_event.pos().x(), self.seek_slider.geometry().width())
        if self.state in [ SoundState.PLAYING, SoundState.PAUSED ]:
            self.seek(position)
        if self.state == SoundState.STOPPED:
            if self.current_sound_selected:
                self.play(position)

    def slider_mousePressEvent(self, mouse_event):
        self.slider_seek_to_pos(mouse_event)

    def slider_mouseMoveEvent(self, mouse_event):
        self.slider_seek_to_pos(mouse_event)

    def slider_mouseReleaseEvent(self, mouse_event):
        self.slider_seek_to_pos(mouse_event)

    def notify_sound_stopped(self):
        self.state = SoundState.STOPPED
        self.disable_seek_pos_updates()
        self.seek_slider.setValue(100.0)
        log.debug(f"sound reached end")

    def gst_bus_message_handler(self, bus, message, *user_data):
        if message.type == Gst.MessageType.SEGMENT_DONE:
            log_gst_message(message)
            if self.config['play_looped']:
                # normal looping when no seeking has been done
                self.player.seek(1.0,
                                 Gst.Format.TIME,
                                 Gst.SeekFlags.SEGMENT,
                                 Gst.SeekType.SET, 0,
                                 Gst.SeekType.NONE, 0)
            else:
                self.notify_sound_stopped()
        elif message.type == Gst.MessageType.EOS:
            log_gst_message(message)
            if self.config['play_looped']:
                # playing looped but a seek was done while playing
                # so must do a full restart of the stream
                self.player.set_state(Gst.State.PAUSED)
                self.player.seek(1.0,
                                 Gst.Format.TIME,
                                 Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH,
                                 Gst.SeekType.SET, 0,
                                 Gst.SeekType.NONE, 0)
                self.player.set_state(Gst.State.PLAYING)
            else:
                self.notify_sound_stopped()
        elif message.type == Gst.MessageType.TAG:
            message_struct = message.get_structure()
            taglist = message.parse_tag()
            metadata = parse_tag_list(taglist)
            self.current_sound_playing.update_metadata(metadata)
            self.update_metadata_to_current_playing_message.emit()
        elif message.type == Gst.MessageType.WARNING:
            log.warning(f"Gstreamer WARNING: {message.type}: {message.get_structure().to_string()}")
        elif message.type == Gst.MessageType.ERROR:
            log.warning(f"Gstreamer ERROR: {message.type}: {message.get_structure().to_string()}")
        return True

    @QtCore.Slot()
    def seek_position_updater(self):
        got_duration, duration = self.player.query_duration(Gst.Format.TIME)
        got_position, position = self.player.query_position(Gst.Format.TIME)
        # log.debug(cyan(f"seek pos update got_position={got_position} position={position} got_duration={got_duration} duration={duration}"))
        if got_duration:
            if 'duration' not in self.current_sound_playing.metadata[None] or 'duration' not in self.current_sound_playing.metadata['all']:
                self.current_sound_playing.metadata[None]['duration'] = self.current_sound_playing.metadata['all']['duration'] = duration
                self.update_metadata_pane(self.current_sound_playing.metadata)
            if got_position:
                signals_blocked = self.seek_slider.blockSignals(True)
                self.seek_slider.setValue(position * 100.0 / duration)
                self.seek_slider.blockSignals(signals_blocked)
                if position >= duration and not self.config['play_looped']:
                    self.notify_sound_stopped()

    def enable_seek_pos_updates(self):
        log.debug(f"enable seek pos updates")
        self.seek_pos_update_timer.timeout.connect(self.seek_position_updater)
        self.seek_pos_update_timer.start(SEEK_POS_UPDATER_INTERVAL_MS)

    def disable_seek_pos_updates(self):
        log.debug(f"disable seek pos updates")
        self.seek_pos_update_timer.stop()

    def update_player_path(self, sound):
        log.debug(f"update_player_path to {sound.path}")
        self.player.set_state(Gst.State.NULL)
        uri = pathlib.Path(sound.path).as_uri()
        self.player.set_property('uri', uri)
        self.current_sound_playing = sound

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
                self.update_player_path(self.current_sound_selected)
            elif file_changed(self.current_sound_playing):
                self.update_player_path(self.current_sound_playing)
            self.player.set_state(Gst.State.PAUSED)
            if self.config['play_looped']:
                self.player.seek(1.0,
                                 Gst.Format.TIME,
                                 Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH,
                                 Gst.SeekType.SET, 0,
                                 Gst.SeekType.NONE, 0)
            else:
                self.player.seek(1.0,
                                 Gst.Format.TIME,
                                 Gst.SeekFlags.FLUSH,
                                 Gst.SeekType.SET, 0,
                                 Gst.SeekType.NONE, 0)
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
        self.player.seek(1.0,
                         Gst.Format.TIME,
                         Gst.SeekFlags.FLUSH,
                         Gst.SeekType.SET, 0,
                         Gst.SeekType.NONE, 0)
        self.state = SoundState.STOPPED
        self.disable_seek_pos_updates()
        self._current_sound_playing = None
        self.seek_slider.setValue(0.0)

    def seek(self, position):
        log.debug(f"seek to {position} {self}")
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
        if got_duration:
            seek_pos = position * duration / 100.0
            self.player.seek(1.0,
                             Gst.Format.TIME,
                             Gst.SeekFlags.FLUSH,
                             Gst.SeekType.SET, seek_pos,
                             Gst.SeekType.NONE, 0)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Sound Browser')
    parser.add_argument('-d', '--debug', action='store_true', help='enable debug output')
    parser.add_argument('-c', '--conf_file', type=str, default=CONF_FILE, help=f'use alternate conf file (default={CONF_FILE})')
    parser.add_argument('startup_path', nargs='?', help='open this path')
    args = parser.parse_args()
    if args.debug:
        log.setLevel(logging.DEBUG)
    else:
        log.setLevel(logging.INFO)
    Gst.init(None)
    app = QtWidgets.QApplication([])
    sb = SoundBrowser(args.startup_path, app.clipboard(), args.conf_file)
    def signal_handler(sig, frame):
        sb.clean_close()
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)
    sb.show()
    sys.exit(app.exec_())
