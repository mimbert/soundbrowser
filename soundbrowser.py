#!/usr/bin/env python3

import os, os.path, collections, yaml, schema, signal, sys, pathlib, threading, logging, argparse, traceback, enum

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
    LOG.debug(brightmagenta("callstack:\n" + "".join(traceback.format_list(traceback.extract_stack())[:-1])))

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
    format = "%(asctime)s %(levelname)s %(message)s" # (%(filename)s:%(funcName)s:%(lineno)d)"
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
    schema.Optional('main_window_geometry', default=None): bytes,
    schema.Optional('main_window_state', default=None): bytes,
    schema.Optional('splitter_state', default=None): bytes,
    schema.Optional('play_looped', default=False): bool,
    schema.Optional('file_extensions_filter', default=['wav', 'mp3', 'aiff', 'flac', 'ogg', 'm4a', 'aac']): [str],
    schema.Optional('filter_files', default=True): bool,
    schema.Optional('gst_audio_sink', default=''): str,
    schema.Optional('gst_audio_sink_properties', default={}): {schema.Optional(str): str},
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
    LOG.debug(cyan(f"gst message: {message.type.first_value_name}: {message.get_structure().to_string() if message.get_structure() else 'None'}"))

class Sound(QtCore.QObject):

    def __init__(self, path = None, stat_result = None):
        super().__init__()
        LOG.debug(f"new sound path={path} stat={stat_result}")
        self.metadata = { None: {}, 'all': {} }
        self.path = path
        self.stat_result = stat_result

    def __str__(self):
        return f"Sound<path={self.path}>"

    def update_metadata(self, metadata):
        for k in metadata:
            if not k in self.metadata:
                self.metadata[k] = {}
            self.metadata[k].update(metadata[k])
            self.metadata['all'].update(metadata[k])

class SoundManager():

    def __init__(self):
        self._cache = LRU(maxsize = CACHE_SIZE) # keys: file pathes. Values: Sound
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
                #TODO il va y avoir un souci ici
                #sound.stop()
                return self._load(path)
            # LOG.debug(f"sound in cache, using it: {sound}")
            return sound
        else:
            LOG.debug(f"sound not in cache, or reload forced, load it: {path}")
            return self._load(path)

    def is_loaded(self, path):
        return path in self._cache

    def _load(self, path):
        if not os.path.isfile(path):
            return None
        sound = Sound(path=path, stat_result=os.stat(path))
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
        self.specified_dir_button.clicked.connect(self.specified_dir_button_clicked)

    def specified_dir_button_clicked(self, checked = False):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "startup directory", self.specified_dir.text())
        if path:
            self.specified_dir.setText(path)

# not to be confused with gst state which is only PLAYING or PAUSED
SoundState = enum.Enum('SoundState', ['STOPPED', 'PLAYING', 'PAUSED'])

class SoundBrowser(main_win.Ui_MainWindow, QtWidgets.QMainWindow):

    update_metadata_to_current_playing_message = QtCore.Signal()

    def __init__(self, startup_path, clipboard, conf_file):
        super().__init__()
        self._state = SoundState.STOPPED
        self.clipboard = clipboard
        self.conf_file = conf_file
        self.config = load_conf(self.conf_file)
        self.manager = SoundManager()
        self.current_sound_selected = None
        self.current_sound_playing = None
        self.setupUi(self)
        self.populate(startup_path)
        self.player = Gst.ElementFactory.make('playbin')
        self.player.set_property('flags', self.player.get_property('flags') & ~(0x00000001 | 0x00000004 | 0x00000008)) # disable video, subtitles, visualisation
        if self.config['gst_audio_sink']:
            audiosink = Gst.ElementFactory.make(self.config['gst_audio_sink'])
            for k, v in self.config['gst_audio_sink_properties'].items():
                audiosink.set_property(k, v)
            self.player.set_property("audio-sink", audiosink)
        self.player.get_bus().add_watch(GLib.PRIORITY_DEFAULT, self.gst_bus_message_handler, None)
        self.seek_pos_update_timer = QtCore.QTimer()
        self.seek_min_interval_timer = None
        self.seek_next_value = None
        self.gst_async_done_callbacks = []
        self.update_metadata_to_current_playing_message.connect(self.update_metadata_pane_to_current_playing)

    def __str__(self):
        return f"SoundBrowser <state={self.state.name}, current_sound_selected={self.current_sound_selected} current_sound_playing={self.current_sound_playing}>"

    def _update_ui_to_selection(self):
        if self.current_sound_selected:
            self.play.setEnabled(True)
            self.stop.setEnabled(True)
            self.seek.setEnabled(True)
            self.seek.setValue(0)
        else:
            self.play.setEnabled(False)
            self.stop.setEnabled(False)
            self.seek.setEnabled(False)
            self.seek.setValue(0)

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        self._state = value
        if value == SoundState.STOPPED:
            self.play.setIcon(self.play_icon)
            self._update_ui_to_selection()
        elif value == SoundState.PLAYING:
            self.play.setIcon(self.pause_icon)
            self.play.setEnabled(True)
            self.stop.setEnabled(True)
        elif value == SoundState.PAUSED:
            self.play.setIcon(self.play_icon)
            self.play.setEnabled(True)
            self.stop.setEnabled(True)

    def select_path(self):
        # est-ce que je teste si il y a une sélection de taille 1 ou est-ce le code appelant? plutôt le code appelant à priori?
        # est-ce que je vais chercher self.tableView.currentIndex() ou j'utilise un param index?
        # lorsque c'est un dir qui est sélectionné est ce que cette fonction est appelée?
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
            self._update_ui_to_selection()

    def _notify_sound_stopped(self):
        self.state = SoundState.STOPPED
        self.disable_seek_pos_updates()
        LOG.debug(f"sound reached end")

    # def player_set_state_with_callback(self, state, callback_tuple):
    #     self.gst_async_done_callbacks.append(callback_tuple)
    #     return self.player.set_state(state)

    # def player_set_state_blocking(self, state):
    #     r = self.player.set_state(state)
    #     if r == Gst.StateChangeReturn.ASYNC:
    #         retcode, state, pending_state = self.player.get_state(BLOCKING_GET_STATE_TIMEOUT)
    #         if retcode == Gst.StateChangeReturn.FAILURE:
    #             LOG.warning(f"gst async state change failure after timeout of {BLOCKING_GET_STATE_TIMEOUT / Gst.MSECOND}ms. retcode: {retcode}, state: {state}, pending_state: {pending_state}")
    #             log_callstack()
    #         elif retcode == Gst.StateChangeReturn.ASYNC:
    #             LOG.warning(f"gst async state change still async after timeout of {BLOCKING_GET_STATE_TIMEOUT / Gst.MSECOND}ms. retcode: {retcode}, state: {state}, pending_state: {pending_state}")
    #             log_callstack()
    #         return retcode
    #     return r

    # def wait_state_stable(self):
    #     while True:
    #         LOG.debug(f"wait state stable")
    #         retcode, state, pending_state = self.player.get_state(Gst.CLOCK_TIME_NONE)
    #         print(f"retcode = {retcode}, state = {state}, pending_state = {pending_state}")
    #         if retcode == Gst.StateChangeReturn.SUCCESS:
    #             print(f"ok no more pending state changes")
    #             return retcode, state, pending_state

    # def safe_seek(self, rate, format, flags, start_type, start, stop_type, stop):
    #     while True:
    #         retcode, state, pending_state = wait_state_stable(self.player)
    #         if (state == Gst.State.PAUSED
    #             or (state == Gst.State.PLAYING
    #                 and flags & Gst.SeekFlags.FLUSH)):
    #             break
    #     return self.player.seek(rate, format, flags, start_type, start, stop_type, stop)

    def player_seek_with_callback(self, rate, formt, flags, start_type, start, stop_type, stop, callback_tuple):
        self.gst_async_done_callbacks.append(callback_tuple)
        self.player.seek(rate, formt, flags, start_type, start, stop_type, stop)

    # def player_seek_blocking(self, rate, formt, flags, start_type, start, stop_type, stop):
    #     finished = threading.Event()
    #     self.player_seek_with_callback(rate, formt, flags, start_type, start, stop_type, stop,
    #                                    (lambda: finished.set(), [], {}))
    #     timeout = BLOCKING_GET_STATE_TIMEOUT / (Gst.MSECOND * 1000.0)
    #     if not finished.wait(timeout):
    #         LOG.warning(f"wait for seek completion failure after timeout of {timeout}s.")
    #         log_callstack()
    #         return False
    #     return True

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

    @QtCore.Slot()
    def update_metadata_pane_to_current_playing(self):
        self.update_metadata_pane(self.current_sound_playing.metadata)

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

    def gst_bus_message_handler(self, bus, message, *user_data):
        if message.type == Gst.MessageType.ASYNC_DONE:
            for callback in self.gst_async_done_callbacks:
                func = callback[0]
                args = callback[1]
                kwargs = callback[2]
                LOG.debug(f"ASYNC_DONE, calling {func.__name__} with args {args} kwargs {kwargs}")
                func(*args, **kwargs)
            self.gst_async_done_callbacks.clear()
        elif message.type == Gst.MessageType.SEGMENT_DONE:
            log_gst_message(message)
            if self.config['play_looped']:
                # normal looping when no seeking has been done
                self.player.seek(1.0,
                                 Gst.Format.TIME,
                                 Gst.SeekFlags.SEGMENT,
                                 Gst.SeekType.SET, 0,
                                 Gst.SeekType.NONE, 0)
            else:
                self._notify_sound_stopped()
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
                self._notify_sound_stopped()
        elif message.type == Gst.MessageType.TAG:
            message_struct = message.get_structure()
            taglist = message.parse_tag()
            metadata = parse_tag_list(taglist)
            self.current_sound_playing.update_metadata(metadata)
            self.update_metadata_to_current_playing_message.emit()
        elif message.type == Gst.MessageType.WARNING:
            LOG.warning(f"Gstreamer WARNING: {message.type}: {message.get_structure().to_string()}")
        elif message.type == Gst.MessageType.ERROR:
            LOG.warning(f"Gstreamer ERROR: {message.type}: {message.get_structure().to_string()}")
        return True

    @QtCore.Slot()
    def seek_position_updater(self):
        got_duration, duration = self.player.query_duration(Gst.Format.TIME)
        got_position, position = self.player.query_position(Gst.Format.TIME)
        # LOG.debug(cyan(f"seek pos update got_position={got_position} position={position} got_duration={got_duration} duration={duration}"))
        if got_duration:
            if 'duration' not in self.current_sound_playing.metadata[None] or 'duration' not in self.current_sound_playing.metadata['all']:
                self.current_sound_playing.metadata[None]['duration'] = self.current_sound_playing.metadata['all']['duration'] = duration
                self.update_metadata_pane(self.current_sound_playing.metadata)
            if got_position:
                signals_blocked = self.seek.blockSignals(True)
                self.seek.setValue(position * 100.0 / duration)
                self.seek.blockSignals(signals_blocked)
                if position >= duration and not self.config['play_looped']:
                    self._notify_sound_stopped()

    def enable_seek_pos_updates(self):
        LOG.debug(f"enable seek pos updates")
        self.seek_pos_update_timer.timeout.connect(self.seek_position_updater)
        self.seek_pos_update_timer.start(SEEK_POS_UPDATER_INTERVAL_MS)

    def disable_seek_pos_updates(self):
        LOG.debug(f"disable seek pos updates")
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

    def clean_close(self):
        self.config['main_window_geometry'] = self.saveGeometry().data()
        self.config['main_window_state'] = self.saveState().data()
        self.config['splitter_state'] = self.splitter.saveState().data()
        if self.config['startup_dir_mode'] == STARTUP_DIR_MODE_LAST_DIR:
            self.config['last_dir'] = self.fs_model.filePath(self.treeView.currentIndex())
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
        startup_dir, startup_filename = None, None
        if startup_path:
            startup_dir, startup_filename = split_path_filename(startup_path)
        if startup_dir:
            self.config['last_dir'] = startup_dir
        else:
            if self.config['startup_dir_mode'] == STARTUP_DIR_MODE_SPECIFIED_DIR:
                startup_dir, startup_filename = split_path_filename(self.config['specified_dir'])
                if not startup_dir:
                    startup_dir = os.getcwd()
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
        self.locationBar.returnPressed.connect(self.locationBar_return_pressed)
        self.prefsButton.clicked.connect(self.prefs_button_clicked)
        self.seek.mousePressEvent = self.slider_mousePressEvent
        self.seek.mouseMoveEvent = self.slider_mouseMoveEvent
        self.seek.mouseReleaseEvent = self.slider_mouseReleaseEvent
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
        self.stop.clicked.connect(self.stop_clicked)
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
        # keyboard shortcuts
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
        self.paste_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtGui.QKeySequence.Paste), self)
        self.paste_shortcut.activated.connect(self.mainwin_paste)
        self.clear_metadata_pane()
        self.tableView.setFocus()

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
        if self.state == SoundState.STOPPED:
            self.play_sound()
        else:
            self.pause_sound()

    def stop_clicked(self, checked):
        self.stop_sound()

    def tableview_get_path(self, index):
        return self.dir_model.filePath(self.dir_proxy_model.mapToSource(index))

    def locationBar_return_pressed(self):
        directory, filename = split_path_filename(self.locationBar.text())
        if directory:
            self.treeView.setCurrentIndex(self.fs_model.index(directory))
            self.treeView.expand(self.fs_model.index(directory))

    def loop_shortcut_activated(self):
        self.loop.click()

    def metadata_shortcut_activated(self):
        self.show_metadata_pane.click()

    def hidden_shortcut_activated(self):
        self.show_hidden_files.click()

    def filter_shortcut_activated(self):
        self.filter_files.click()

    def play_shortcut_activated(self):
        self.play.click()

    def stop_shortcut_activated(self):
        self.stop.click()

    def tableview_selection_changed(self, selected, deselected):
        LOG.debug(f"tableview_selection_changed  len(selected)={len(selected)}")
        if len(selected) == 1:
            self.select_path()

    def tableView_return_pressed(self):
        self.tableView.selectionModel().selectedRows()
        LOG.debug(f"tableview_return_pressed  len(self.tableView.selectionModel().selectedRows())={len(self.tableView.selectionModel().selectedRows())}")
        if len(self.tableView.selectionModel().selectedRows()) == 1:
            self.select_path()
            fileinfo = self.dir_model.fileInfo(self.dir_proxy_model.mapToSource(self.tableView.currentIndex()))
            if fileinfo.isDir():
                path = self.tableview_get_path(self.tableView.currentIndex())
                self.tableView.setRootIndex(self.dir_proxy_model.mapFromSource(self.dir_model.index(path)))
                self.treeView.setCurrentIndex(self.fs_model.index(path))
                self.treeView.expand(self.fs_model.index(path))
            elif fileinfo.isFile():
                self.play_sound()

    def tableview_clicked(self, index):
        self.tableView_return_pressed()

    def tableView_contextMenuEvent(self, event):
        # todo:
        #
        # - vérifier que si c'est appelé la sélection a déjà
        #   changé. Dans ce cas, pas besoin de récupe le path, tout
        #   ça, il suffit d'instancier le popup et celui ci se servira
        #   de current_sound_selected
        #
        # - éventuellement ajouter du code qui vérifie si un sound est
        #   déjà dans le soundmanager et ne propose pas le reload si
        #   il y est pas
        #
        # - Se poser la question si c'est nécessaire d'avoir un reload
        #   si on change l'uri des sons à chaque fois à un seul
        #   playbin. les arguments pour:
        #
        #   - si on change pas de son, permet de forcer un reload
        #
        #   - reset des metadata (mais ils seront updatés
        #     automatiquement à la prochaine lecture de toutes façon,
        #     non?)
        index = self.tableView.indexAt(event.pos())
        if index:
            path = self.tableview_get_path(index)
            if path:
                self.tableView_contextMenu.path_to_reload = path
                self.tableView_contextMenu.popup(QtGui.QCursor.pos())

    def mainwin_paste(self):
        directory, filename = split_path_filename(self.clipboard.text())
        if directory:
            self.treeView.setCurrentIndex(self.fs_model.index(directory))
            self.treeView.expand(self.fs_model.index(directory))

    def reload_sound(self):
        # todo: voir tableView_contextMenuEvent
        path = self.tableView_contextMenu.path_to_reload
        self.stop_sound()
        self.manager.get(path, force_reload=True)
        self.play_sound()

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

    def slider_seek_to_pos(self):
        position = QtWidgets.QStyle.sliderValueFromPosition(self.seek.minimum(), self.seek.maximum(), mouse_event.pos().x(), self.seek.geometry().width())
        if self.state in [ SoundState.PLAYING, SoundState.PAUSED ]:
            self.seek_sound(position)
        if self.state == SoundState.STOPPED:
            if self.current_sound_selected:
                self.play(position)

    def slider_mousePressEvent(self, mouse_event):
        self.slider_seek_to_pos()

    def slider_mouseMoveEvent(self, mouse_event):
        self.slider_seek_to_pos()

    def slider_mouseReleaseEvent(self, mouse_event):
        self.slider_seek_to_pos()

    # def start_sound(self, path, position=None):
    #     sound = self.manager.get(path)
    #     if sound:
    #         if self.currently_playing:
    #             if self.currently_playing != path:
    #                 self.stop_sound()
    #                 uri = pathlib.Path(path).as_uri()
    #                 self.player.set_property('uri', uri)
    #         else:
    #             uri = pathlib.Path(path).as_uri()
    #             self.player.set_property('uri', uri)
    #         self._play(position)
    #         self.locationBar.setText(path)
    #         self.update_metadata_pane(sound.metadata)
    #         self.state = SoundState.PLAYING, path

    # def pause_sound(self):
    #     if self.currently_playing:
    #         sound = self.manager.get(self.currently_playing)
    #         if sound:
    #             if self.state == SoundState.PLAYING:
    #                 self._pause()
    #                 self.state = SoundState.PAUSED
    #             elif self.state == SoundState.PAUSED:
    #                 self._play()
    #                 self.state = SoundState.PLAYING

    # def stop_sound(self, path=None):
    #     if path == None: path = self.currently_playing
    #     if path:
    #         sound = self.manager.get(path)
    #         if sound:
    #             self._stop()
    #         if path == self.currently_playing:
    #             self.state = SoundState.STOPPED

    def _update_player_path(self, sound):
        LOG.debug(f"update_player_path to {sound.path}")
        self.player.set_state(Gst.State.NULL)
        uri = pathlib.Path(sound.path).as_uri()
        self.player.set_property('uri', uri)
        self.current_sound_playing = sound

    def play_sound(self, start_pos=None):
        LOG.debug(f"play {self}")
        if self.state == SoundState.PAUSED:
            LOG.error(f"play_sound called with state = {self.state.name}")
            return
        if (not self.current_sound_selected) and (not self.current_sound_playing):
            LOG.error(f"play_sound called with no sound selected nor playing")
            return
        if self.state == SoundState.PLAYING:
            self.state = SoundState.STOPPED
            self.player.set_state(Gst.State.PAUSED)
        if self.state == SoundState.STOPPED:
            if self.current_sound_selected and self.current_sound_playing != self.current_sound_selected:
                self._update_player_path(self.current_sound_selected)
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
            self._actual_seek(start_pos)
        self.player.set_state(Gst.State.PLAYING)
        self.state = SoundState.PLAYING
        self.enable_seek_pos_updates()

    def pause_sound(self):
        LOG.debug(f"pause {self}")
        if not self.state == SoundState.PLAYING:
            LOG.error(f"pause_sound called with state = {self.state.name}")
            return
        if not self.current_sound_playing:
            LOG.error(f"pause_sound called with current_sound_playing = {self.current_sound_playing}")
            return
        self.player.set_state(Gst.State.PAUSED)
        self.state = SoundState.PAUSED
        self.disable_seek_pos_updates()

    def stop_sound(self):
        LOG.debug(f"stop {self}")
        self.player.set_state(Gst.State.PAUSED)
        self.player_seek_with_callback(1.0,
                                       Gst.Format.TIME,
                                       Gst.SeekFlags.FLUSH,
                                       Gst.SeekType.SET, 0,
                                       Gst.SeekType.NONE, 0,
                                       (self.disable_seek_pos_updates, [], {}))
        self.state = SoundState.STOPPED
        self._current_sound_playing = None
        signals_blocked = self.seek.blockSignals(True)
        self.seek.setValue(0.0)
        self.seek.blockSignals(signals_blocked)

    def seek_sound(self, position):
        LOG.debug(f"seek to {position} {self}")
        if self.seek_min_interval_timer != None:
            LOG.debug(f"seek to {position} delayed to limit gst seek events frequency")
            self.seek_next_value = position
        else:
            self._actual_seek(position)
            self.seek_next_value = None
            self.seek_min_interval_timer = QtCore.QTimer()
            self.seek_min_interval_timer.setSingleShot(True)
            self.seek_min_interval_timer.timeout.connect(self._seek_min_interval_timer_fired)
            self.seek_min_interval_timer.start(SEEK_MIN_INTERVAL_MS)

    @QtCore.Slot()
    def _seek_min_interval_timer_fired(self):
        if self.seek_next_value:
            self._actual_seek(self.seek_next_value)
        self.seek_next_value = None
        self.seek_min_interval_timer = None

    def _actual_seek(self, position):
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
        LOG.setLevel(logging.DEBUG)
    else:
        LOG.setLevel(logging.INFO)
    app = QtWidgets.QApplication([])
    sb = SoundBrowser(args.startup_path, app.clipboard(), args.conf_file)
    def signal_handler(sig, frame):
        sb.clean_close()
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)
    sb.show()
    sys.exit(app.exec_())
