#!/usr/bin/env python3

import os, os.path, collections, yaml, schema, signal, sys, pathlib, threading, logging, argparse, traceback

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
SEEK_MIN_INTERVAL_MS = 100
CONF_FILE = os.path.expanduser("~/.soundbrowser.conf.yaml")

def log_callstack():
    LOG.debug("callstack:\n" + "".join(traceback.format_list(traceback.extract_stack())[:-1]))

class CustomFormatter(logging.Formatter):
    grey = '\033[2m\033[37m'
    brightyellow = '\033[93m'
    brightred = '\033[91m'
    reversebrightboldred = '\033[7m\033[1m\033[91m'
    reset = '\033[m'
    format = "%(asctime)s %(levelname)s %(message)s (%(filename)s:%(funcName)s:%(lineno)d)"
    FORMATTERS = {
        logging.DEBUG: logging.Formatter(grey + format + reset),
        logging.INFO: logging.Formatter(format),
        logging.WARNING: logging.Formatter(brightyellow + format + reset),
        logging.ERROR: logging.Formatter(brightred + format + reset),
        logging.CRITICAL: logging.Formatter(reversebrightboldred + format + reset),
    }
    def format(self, record):
        return self.FORMATTERS.get(record.levelno).format(record)

LOG = logging.getLogger()
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(CustomFormatter())
_handler.setLevel(logging.DEBUG)
LOG.addHandler(_handler)

STARTUP_DIR_MODE_SPECIFIED_DIR = 1
STARTUP_DIR_MODE_LAST_DIR = 2
STARTUP_DIR_MODE_CURRENT_DIR = 3
STARTUP_DIR_MODE_HOME_DIR = 4

conf_schema = schema.Schema({
    schema.Optional('startup_dir_mode', default=STARTUP_DIR_MODE_HOME_DIR): int,
    schema.Optional('specified_dir', default=os.path.expanduser('~')): str,
    schema.Optional('last_dir', default=os.path.expanduser('~')): str,
    schema.Optional('show_hidden_files', default=False): bool,
    schema.Optional('show_metadata_pane', default=True): bool,
    schema.Optional('show_parent_folder_in_file_pane', default=True): bool,
    schema.Optional('main_window_geometry', default=None): bytes,
    schema.Optional('main_window_state', default=None): bytes,
    schema.Optional('splitter_state', default=None): bytes,
    schema.Optional('play_looped', default=False): bool,
    schema.Optional('file_extensions_filter', default=['wav', 'mp3', 'aiff', 'flac', 'ogg', 'm4a', 'aac']): [str],
    schema.Optional('filter_files', default=True): bool,
})

def load_conf(path):
    LOG.debug(f"loading conf from {path}")
    try:
        with open(path) as fh:
            conf = yaml.safe_load(fh)
    except OSError:
        LOG.debug(f"error reading conf from {path}, using an empty conf")
        conf = {}
    return conf_schema.validate(conf)

def save_conf(path, conf):
    conf = conf_schema.validate(conf)
    LOG.debug(f"saving conf to {path}")
    try:
        with open(path, 'w') as fh:
            yaml.dump(conf, fh)
    except OSError:
        LOG.debug(f"unable to save conf to {path}")

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
            LOG.debug(f"LRU max size, removing {oldest}")
            del self[oldest]

def gst_bus_message_handler(bus, message, *user_data):
    sound = user_data[0]
    sound.gst_message.emit(message)
    return True

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
    # LOG.debug(f"tag_list: {containers}")
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

class Sound(QtCore.QObject):

    gst_message = QtCore.Signal(Gst.Message)

    def __init__(self, path = None, stat_result = None, browser = None):
        super().__init__()
        LOG.debug(f"new sound path={path} stat={stat_result}")
        self.metadata = { None: {}, 'all': {} }
        self.path = path
        self.stat_result = stat_result
        self.browser = browser
        self.player = Gst.ElementFactory.make('playbin')
        fakevideosink = Gst.ElementFactory.make('fakesink')
        self.player.set_property("video-sink", fakevideosink)
        self.player.get_bus().add_watch(GLib.PRIORITY_DEFAULT, gst_bus_message_handler, self)
        uri = pathlib.Path(path).as_uri()
        self.player.set_property('uri', uri)
        self.seek_pos_update_timer = QtCore.QTimer()
        self.seek_min_interval_timer = None
        self.seek_next_value = None
        self.gst_async_done_callbacks = []
        self.gst_message.connect(self.receive_gst_message)
        self.paused = False

    def __str__(self):
        return f"Sound<path={self.path}>"

    def player_set_state_with_callback(self, state, callback_tuple):
        self.gst_async_done_callbacks.append(callback_tuple)
        return self.player.set_state(state)

    def player_set_state_blocking(self, state):
        r = self.player.set_state(state)
        if r == Gst.StateChangeReturn.ASYNC:
            retcode, state, pending_state = self.player.get_state(10000 * Gst.MSECOND)
            if retcode == Gst.StateChangeReturn.FAILURE:
                LOG.warning(f"gst async state change failure after timeout. retcode: {retcode}, state: {state}, pending_state: {pending_state} ")
            elif retcode == Gst.StateChangeReturn.ASYNC:
                LOG.warning(f"gst async state change still async after timeout. retcode: {retcode}, state: {state}, pending_state: {pending_state}")
            return retcode
        return r

    def player_seek_with_callback(self, rate, formt, flags, start_type, start, stop_type, stop, callback_tuple):
        self.gst_async_done_callbacks.append(callback_tuple)
        self.player.seek(rate, formt, flags, start_type, start, stop_type, stop)

    def update_metadata(self, metadata):
        for k in metadata:
            if not k in self.metadata:
                self.metadata[k] = {}
            self.metadata[k].update(metadata[k])
            self.metadata['all'].update(metadata[k])

    def update_metadata_field(self, field, value, force = None):
        f = getattr(self.browser, field)
        l = getattr(self.browser, field + '_label')
        if value or force == True:
            f.setText(str(value))
            f.setEnabled(True)
            l.setEnabled(True)
        if not value or force == False:
            f.setText(str(value))
            f.setEnabled(False)
            l.setEnabled(False)

    def update_metadata_pane(self):
        m = self.metadata['all']
        b = self.browser
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
            self.browser.image.setPixmap(m.get('image'))
        else:
            self.browser.image.setPixmap(None)

    @QtCore.Slot(Gst.Message)
    def receive_gst_message(self, message):
        # LOG.debug(f"gst_bus_message_handler message: {message.type}: {message.get_structure().to_string() if message.get_structure() else 'None'}")
        if message.type == Gst.MessageType.ASYNC_DONE:
            for callback in self.gst_async_done_callbacks:
                func = callback[0]
                args = callback[1]
                kwargs = callback[2]
                LOG.debug(f"ASYNC_DONE, calling {func.__name__} with args {args} kwargs {kwargs}")
                func(*args, **kwargs)
            self.gst_async_done_callbacks.clear()
        elif message.type == Gst.MessageType.SEGMENT_DONE:
            if self.browser.config['play_looped']:
                # normal looping when no seeking has been done
                self.player.seek(1.0,
                                 Gst.Format.TIME,
                                 Gst.SeekFlags.SEGMENT,
                                 Gst.SeekType.SET, 0,
                                 Gst.SeekType.NONE, 0)
        elif message.type == Gst.MessageType.EOS:
            if self.browser.config['play_looped']:
                # get this when playing looped but a seek was done while playing
                # must then do a full restart of the stream
                self.player_set_state_blocking(Gst.State.PAUSED)
                self.player.seek(1.0,
                                 Gst.Format.TIME,
                                 Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                                 Gst.SeekType.SET, 0,
                                 Gst.SeekType.NONE, 0)
                self.player.set_state(Gst.State.PLAYING)
        elif message.type == Gst.MessageType.TAG:
            # LOG.debug(f"{message.type}: {message.get_structure().to_string()}")
            message_struct = message.get_structure()
            taglist = message.parse_tag()
            metadata = parse_tag_list(taglist)
            self.update_metadata(metadata)
            self.update_metadata_pane()
        elif message.type == Gst.MessageType.WARNING:
            LOG.warning(f"Gstreamer WARNING: {message.type}: {message.get_structure().to_string()}")
        elif message.type == Gst.MessageType.ERROR:
            LOG.warning(f"Gstreamer ERROR: {message.type}: {message.get_structure().to_string()}")

    @QtCore.Slot()
    def seek_position_updater(self):
        got_duration, duration = self.player.query_duration(Gst.Format.TIME)
        got_position, position = self.player.query_position(Gst.Format.TIME)
        # LOG.debug(f"seek pos update got_position={got_position} position={position} got_duration={got_duration} duration={duration}")
        if got_duration:
            if 'duration' not in self.metadata[None] or 'duration' not in self.metadata['all']:
                self.metadata[None]['duration'] = self.metadata['all']['duration'] = duration
                self.update_metadata_pane()
            if got_position:
                signals_blocked = self.browser.seek.blockSignals(True)
                self.browser.seek.setValue(position * 100.0 / duration)
                self.browser.seek.blockSignals(signals_blocked)
                if duration == position and not self.browser.config['play_looped']:
                    LOG.debug(f"reached end {self}")
                    self.disable_seek_pos_updates()
                    self.browser.notify_sound_stop()
                    # following 2 lines added because otherwise when
                    # playing the sound again there is a timeout in
                    # the waiting of the async state change
                    self.player.set_state(Gst.State.PAUSED)
                    self.player.seek(1.0,
                                     Gst.Format.TIME,
                                     Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                                     Gst.SeekType.SET, 0,
                                     Gst.SeekType.NONE, 0)

    def enable_seek_pos_updates(self):
        LOG.debug(f"enable seek pos updates {self}")
        self.seek_pos_update_timer.timeout.connect(self.seek_position_updater)
        self.seek_pos_update_timer.start(SEEK_POS_UPDATER_INTERVAL_MS)

    def disable_seek_pos_updates(self):
        LOG.debug(f"disable seek pos updates {self}")
        self.seek_pos_update_timer.stop()
        # following block added because sometimes, when the sound
        # reaches its end, it looks like even though
        # disable_seek_pos_updates is called before the seek to the
        # beginning, there may still be a seek_position_updater call
        # occuring after, which causes the slider to reset to zero
        # anyway
        try:
            self.seek_pos_update_timer.timeout.disconnect(self.seek_position_updater)
        except:
            pass

    def play(self):
        LOG.debug(f"play {self}")
        if not self.paused:
            self.player_set_state_blocking(Gst.State.PAUSED)
            self.player.seek(1.0,
                             Gst.Format.TIME,
                             Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                             Gst.SeekType.SET, 0,
                             Gst.SeekType.NONE, 0)
        self.paused = False
        self.enable_seek_pos_updates()
        self.player.set_state(Gst.State.PLAYING)

    def pause(self):
        LOG.debug(f"pause {self}")
        self.paused = True
        self.player_set_state_blocking(Gst.State.PAUSED)
        self.disable_seek_pos_updates()

    def stop(self):
        LOG.debug(f"stop {self}")
        self.paused = False
        self.player.set_state(Gst.State.PAUSED)
        self.player_seek_with_callback(1.0,
                                       Gst.Format.TIME,
                                       Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                                       Gst.SeekType.SET, 0,
                                       Gst.SeekType.NONE, 0,
                                       (self.disable_seek_pos_updates,[],{}))
        signals_blocked = self.browser.seek.blockSignals(True)
        self.browser.seek.setValue(0.0)
        self.browser.seek.blockSignals(signals_blocked)

    def seek(self, position):
        if self.seek_min_interval_timer != None:
            LOG.debug(f"seek to {position} {self} delayed to limit gst seek events frequency")
            self.seek_next_value = position
        else:
            self._actual_seek(position)
            self.seek_next_value = None
            self.seek_min_interval_timer = QtCore.QTimer()
            self.seek_min_interval_timer.setSingleShot(True)
            self.seek_min_interval_timer.timeout.connect(self.seek_min_interval_timer_fired)
            self.seek_min_interval_timer.start(SEEK_MIN_INTERVAL_MS)

    @QtCore.Slot()
    def seek_min_interval_timer_fired(self):
        if self.seek_next_value:
            self._actual_seek(self.seek_next_value)
        self.seek_next_value = None
        self.seek_min_interval_timer = None

    def _actual_seek(self, position):
        LOG.debug(f"seek to {position} {self}")
        got_duration, duration = self.player.query_duration(Gst.Format.TIME)
        if got_duration:
            seek_pos = position * duration / 100.0
            self.player.seek(1.0,
                             Gst.Format.TIME,
                             Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, # could be Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE ?
                             Gst.SeekType.SET, seek_pos,
                             Gst.SeekType.NONE, 0)

class SoundManager():

    def __init__(self, browser):
        self._cache = LRU(maxsize = CACHE_SIZE) # keys: file pathes. Values: Sound
        self._browser = browser
        Gst.init(None)

    def get(self, path, force_reload=False ):
        # LOG.debug(f"sound manager get {path}")
        if path in self._cache and not force_reload:
            if not os.path.isfile(path):
                del self._cache[path]
                return None
            sound = self._cache[path]
            stat_result = os.stat(path)
            if stat_result.st_mtime_ns > sound.stat_result.st_mtime_ns:
                LOG.debug(f"sound in cache but changed on disk, reloading (and stop it if it was playing): {sound}")
                sound.stop()
                return self._load(path)
            # LOG.debug(f"sound in cache, using it: {sound}")
            return sound
        else:
            LOG.debug(f"sound not in cache, or reload forced, load it: {path}")
            return self._load(path)

    def _load(self, path):
        if not os.path.isfile(path):
            return None
        sound = Sound(path=path, stat_result=os.stat(path), browser=self._browser)
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
        if not sep: return True
        if not self.parent().config['filter_files']: return True
        if ext in self.parent().config['file_extensions_filter']: return True
        return False

class PrefsDialog(prefs_dial.Ui_PrefsDialog, QtWidgets.QDialog):

    def __init__(self, *args, **kwds):
        super(PrefsDialog, self).__init__(*args, **kwds)
        self.setupUi(self)
        self.populate()

    def populate(self):
        self.specified_dir_button.clicked.connect(self.specified_dir_button_clicked)

    def specified_dir_button_clicked(self, checked = False):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "startup directory", self.specified_dir.text())
        if path:
            self.specified_dir.setText(path)

class SoundBrowser(main_win.Ui_MainWindow, QtWidgets.QMainWindow):

    def __init__(self, clipboard):
        super().__init__()
        self.clipboard = clipboard
        self.config = load_conf(CONF_FILE)
        self.manager = SoundManager(self)
        self.currently_playing = None
        self._ignore_click_event = False
        self.setupUi(self)
        self.populate()

    def clean_close(self):
        self.config['main_window_geometry'] = self.saveGeometry().data()
        self.config['main_window_state'] = self.saveState().data()
        self.config['splitter_state'] = self.splitter.saveState().data()
        if self.config['startup_dir_mode'] == STARTUP_DIR_MODE_LAST_DIR:
            self.config['last_dir'] = self.fs_model.filePath(self.treeView.currentIndex())
        save_conf(CONF_FILE, self.config)

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
        if self.config['show_parent_folder_in_file_pane']:
            dir_model_filter |= QtCore.QDir.NoDot
        else:
            dir_model_filter |= QtCore.QDir.NoDotAndDotDot
        self.fs_model.setFilter(fs_model_filter)
        self.dir_model.setFilter(dir_model_filter)
        if self.config['show_metadata_pane']:
            self.bottom_pane.show()
        else:
            self.bottom_pane.hide()

    def populate(self):
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
        if self.config['startup_dir_mode'] == STARTUP_DIR_MODE_SPECIFIED_DIR:
            startup_dir = self.config['specified_dir']
        elif self.config['startup_dir_mode'] == STARTUP_DIR_MODE_LAST_DIR:
            startup_dir = self.config['last_dir']
        elif self.config['startup_dir_mode'] == STARTUP_DIR_MODE_CURRENT_DIR:
            startup_dir = os.getcwd()
        elif self.config['startup_dir_mode'] == STARTUP_DIR_MODE_HOME_DIR:
            startup_dir = os.path.expanduser('~')
        self.treeView.setCurrentIndex(self.fs_model.index(startup_dir))
        self.treeView.expand(self.fs_model.index(startup_dir))
        self.treeView.header().setSortIndicator(0,QtCore.Qt.AscendingOrder)
        self.treeView.setSortingEnabled(True)
        self.treeView.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.dir_model.directoryLoaded.connect(self.dir_model_directory_loaded)
        self.prefsButton.clicked.connect(self.prefs_button_clicked)
        self.seek.valueChanged.connect(self.slider_value_changed)
        self.seek.sliderPressed.connect(self.slider_pressed)
        self.seek.sliderReleased.connect(self.slider_released)
        self.loop.setChecked(self.config['play_looped'])
        self.show_hidden_files.setChecked(self.config['show_hidden_files'])
        self.show_metadata_pane.setChecked(self.config['show_metadata_pane'])
        self.filter_files.setChecked(self.config['filter_files'])
        self.loop.clicked.connect(self.loop_clicked)
        self.show_hidden_files.clicked.connect(self.show_hidden_files_clicked)
        self.show_metadata_pane.clicked.connect(self.show_metadata_pane_clicked)
        self.filter_files.clicked.connect(self.filter_files_clicked)
        self.copy_path.clicked.connect(self.copy_path_clicked)
        self.play.clicked.connect(self.play_clicked)
        self.pause.clicked.connect(self.pause_clicked)
        self.stop.clicked.connect(self.stop_clicked)
        self.refresh_config()
        if self.config['main_window_geometry']:
            self.restoreGeometry(QtCore.QByteArray(self.config['main_window_geometry']))
        if self.config['main_window_state']:
            self.restoreState(QtCore.QByteArray(self.config['main_window_state']))
        if self.config['splitter_state']:
            self.splitter.restoreState(QtCore.QByteArray(self.config['splitter_state']))
        self.image.setScaledContents(True)
        self.tableView_contextMenu = QtWidgets.QMenu(self.tableView)
        reload_sound_action = QtWidgets.QAction("Reload", self.tableView)
        self.tableView_contextMenu.addAction(reload_sound_action)
        reload_sound_action.triggered.connect(self.reload_sound)

    def showEvent(self, event):
        self.image.setFixedWidth(self.metadata.height())
        self.image.setFixedHeight(self.metadata.height())

    def dir_model_directory_loaded(self, path):
        self.tableView.resizeColumnToContents(0)

    def treeview_selection_changed(self, selected, deselected):
        path = self.fs_model.filePath(self.treeView.currentIndex())
        self.locationBar.setText(path)
        self.tableView.setRootIndex(self.dir_proxy_model.mapFromSource(self.dir_model.index(path)))
        self.treeView.setCurrentIndex(self.fs_model.index(path))
        self.treeView.expand(self.fs_model.index(path))

    def play_clicked(self, checked):
        self.start_sound(self.tableview_get_path(self.tableView.currentIndex()))

    def pause_clicked(self, checked):
        self.pause_sound(self.currently_playing)

    def stop_clicked(self, checked):
        self.stop_sound(self.currently_playing)

    def tableview_get_path(self, index):
        return self.dir_model.filePath(self.dir_proxy_model.mapToSource(index))

    def tableview_selection_changed(self, selected, deselected):
        self._ignore_click_event = True
        for r in deselected:
            for pmi in r.indexes():
                self.stop_sound(self.tableview_get_path(pmi))
                break # only first column
        if len(selected) == 1:
            self.start_sound(self.tableview_get_path(self.tableView.currentIndex()))
        else:
            self.play.setEnabled(False)
            self.pause.setEnabled(False)
            self.stop.setEnabled(False)
            if self.currently_playing:
                self.stop_sound(self.currently_playing)

    def tableview_clicked(self, index):
        if not self._ignore_click_event:
            fi = self.dir_model.fileInfo(self.dir_proxy_model.mapToSource(index))
            if fi.isDir():
                path = self.tableview_get_path(index)
                self.locationBar.setText(path)
                self.tableView.setRootIndex(self.dir_proxy_model.mapFromSource(self.dir_model.index(path)))
                self.treeView.setCurrentIndex(self.fs_model.index(path))
                self.treeView.expand(self.fs_model.index(path))
            else:
                for r in self.tableView.selectionModel().selection():
                    for pmi in r.indexes():
                        self.stop_sound(self.tableview_get_path(pmi))
                        break # only first column
                self.start_sound(self.tableview_get_path(index))
        self._ignore_click_event = False

    def tableView_contextMenuEvent(self, event):
        index = self.tableView.indexAt(event.pos())
        if index:
            path = self.tableview_get_path(index)
            if path:
                self.tableView_contextMenu.path_to_reload = path
                self.tableView_contextMenu.popup(QtGui.QCursor.pos())

    def reload_sound(self):
        path = self.tableView_contextMenu.path_to_reload
        self.stop_sound(path)
        sound = self.manager.get(path, force_reload=True)
        self.start_sound(path)

    def loop_clicked(self, checked = False):
        self.config['play_looped'] = checked

    def show_hidden_files_clicked(self, checked = False):
        self.config['show_hidden_files'] = checked
        self.refresh_config()

    def show_metadata_pane_clicked(self, checked = False):
        self.config['show_metadata_pane'] = checked
        self.refresh_config()

    def filter_files_clicked(self, checked = False):
        self.config['filter_files'] = checked
        self.refresh_config()
        self.dir_proxy_model.invalidateFilter()

    def prefs_button_clicked(self, checked = False):
        prefs = PrefsDialog(self)
        prefs.file_extensions_filter.setText(' '.join(self.config['file_extensions_filter']))
        prefs.specified_dir.setText(self.config['specified_dir'])
        if self.config['startup_dir_mode'] == STARTUP_DIR_MODE_SPECIFIED_DIR:
            prefs.startup_dir_mode_specified_dir.setChecked(True)
        elif self.config['startup_dir_mode'] == STARTUP_DIR_MODE_LAST_DIR:
            prefs.startup_dir_mode_last_dir.setChecked(True)
        elif self.config['startup_dir_mode'] == STARTUP_DIR_MODE_CURRENT_DIR:
            prefs.startup_dir_mode_current_dir.setChecked(True)
        elif self.config['startup_dir_mode'] == STARTUP_DIR_MODE_HOME_DIR:
            prefs.startup_dir_mode_home_dir.setChecked(True)
        if prefs.exec_():
            if prefs.startup_dir_mode_specified_dir.isChecked():
                self.config['startup_dir_mode'] = STARTUP_DIR_MODE_SPECIFIED_DIR
            elif prefs.startup_dir_mode_last_dir.isChecked():
                self.config['startup_dir_mode'] = STARTUP_DIR_MODE_LAST_DIR
            elif prefs.startup_dir_mode_current_dir.isChecked():
                self.config['startup_dir_mode'] = STARTUP_DIR_MODE_CURRENT_DIR
            elif prefs.startup_dir_mode_home_dir.isChecked():
                self.config['startup_dir_mode'] = STARTUP_DIR_MODE_HOME_DIR
            self.config['specified_dir'] = prefs.specified_dir.text()
            self.config['file_extensions_filter'] = prefs.file_extensions_filter.text().split(' ')
            self.refresh_config()

    def copy_path_clicked(self, checked = False):
        self.locationBar.setSelection(0, len(self.locationBar.text()))
        self.clipboard.setText(self.locationBar.text())

    def slider_value_changed(self, position):
        if self.currently_playing:
            sound = self.manager.get(self.currently_playing)
            if sound:
                sound.seek(position)
                return
        signals_blocked = self.seek.blockSignals(True)
        self.seek.setValue(0)
        self.seek.blockSignals(signals_blocked)

    def slider_pressed(self):
        LOG.debug(f"slider pressed")
        if self.currently_playing:
            sound = self.manager.get(self.currently_playing)
            if sound:
                sound.disable_seek_pos_updates()

    def slider_released(self):
        LOG.debug(f"slider released")
        if self.currently_playing:
            sound = self.manager.get(self.currently_playing)
            if sound:
                sound.enable_seek_pos_updates()

    def notify_sound_stop(self):
        self.currently_playing = None
        self.play.setEnabled(True)
        self.pause.setEnabled(False)
        self.stop.setEnabled(False)

    def start_sound(self, path):
        sound = self.manager.get(path)
        if sound:
            if self.currently_playing:
                if self.currently_playing != path:
                    self.stop_sound(self.currently_playing)
            sound.play()
            self.locationBar.setText(path)
            sound.update_metadata_pane()
            self.currently_playing = path
            self.play.setEnabled(False)
            self.pause.setEnabled(True)
            self.stop.setEnabled(True)

    def pause_sound(self, path):
        sound = self.manager.get(path)
        if sound:
            sound.pause()
        if self.currently_playing == path:
            self.play.setEnabled(True)
            self.pause.setEnabled(False)
            self.stop.setEnabled(True)

    def stop_sound(self, path):
        sound = self.manager.get(path)
        if sound:
            sound.stop()
        if path == self.currently_playing:
            self.currently_playing = None
            self.play.setEnabled(True)
            self.pause.setEnabled(False)
            self.stop.setEnabled(False)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Sound Browser')
    parser.add_argument('-d', '--debug', action='store_true', help='enable debug output')
    args = parser.parse_args()
    if args.debug:
        LOG.setLevel(logging.DEBUG)
    else:
        LOG.setLevel(logging.INFO)
    app = QtWidgets.QApplication([])
    sb = SoundBrowser(app.clipboard())
    def signal_handler(sig, frame):
        sb.clean_close()
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)
    sb.show()
    sys.exit(app.exec_())
