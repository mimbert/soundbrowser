import os, os.path, re, pathlib, enum, threading, time, inspect, types
from lib.utils import LRU, format_duration
from lib.logger import log, cyan, brightgreen, brightmagenta, brightcyan, log_callstack
from gi.repository import GObject, Gst, GLib
from PySide2 import QtCore

CACHE_SIZE = 256
LOG_ALL_GST_MESSAGES = True
SLEEP_HACK_TIME = 0 # ugly workaround for gst bug or something i don't
                    # do correctly (especially with pipewiresink)

class PlayerStates(enum.Enum):
    UNKNOWN = 0
    PAUSED = 1
    PLAYING = 3
    ERROR = 4

class PlayerMessages(enum.Enum):
    SET_URI = 0
    ASK_STOP = 1
    ASK_PAUSE = 2
    ASK_PLAY = 3

class PlaybackDirection(enum.Enum):
    FORWARD = 1
    BACKWARD = -1

# ------------------------------------------------------------------------
# gst config

_blacklisted_gst_audio_sink_factory_regexes = [
    '^interaudiosink$',
    '^ladspasink.*',
]

def _get_empty_prop_specs(prop):
    return {}

def _get_boolean_prop_specs(prop):
    return {
        'values': [ (True, 'True'), (False, 'False') ],
    }

def _get_enum_prop_specs(prop):
    values = []
    for v in range(prop.enum_class.minimum, prop.enum_class.maximum + 1):
        values.append((v, GObject.enum_to_string(prop.value_type, v)))
    return {
        'values': values,
    }

def _get_numeric_prop_specs(prop):
    return {
        'min': prop.minimum,
        'max': prop.maximum,
    }

_supported_property_types = {
    GObject.ParamSpecBoolean: _get_boolean_prop_specs,
    GObject.ParamSpecEnum: _get_enum_prop_specs,
    GObject.ParamSpecString: _get_empty_prop_specs,
    GObject.ParamSpecChar: _get_numeric_prop_specs,
    GObject.ParamSpecUChar: _get_numeric_prop_specs,
    GObject.ParamSpecInt: _get_numeric_prop_specs,
    GObject.ParamSpecUInt: _get_numeric_prop_specs,
    GObject.ParamSpecInt64: _get_numeric_prop_specs,
    GObject.ParamSpecUInt64: _get_numeric_prop_specs,
    GObject.ParamSpecFloat: _get_numeric_prop_specs,
    GObject.ParamSpecLong: _get_numeric_prop_specs,
    GObject.ParamSpecULong: _get_numeric_prop_specs,
    GObject.ParamSpecDouble: _get_numeric_prop_specs,
}

def get_available_gst_audio_sink_factories():
    factories = Gst.Registry.get().get_feature_list(Gst.ElementFactory)
    audio_sinks_factories = [ f for f in factories if ('Audio' in f.get_metadata('klass') and ('sink' in f.name or 'Sink' in f.get_metadata('klass'))) ]
    for regex in _blacklisted_gst_audio_sink_factory_regexes:
        audio_sinks_factories = [ f for f in audio_sinks_factories if not re.search(regex, f.name) ]
    available_gst_audio_sink_factories = { f.name: f for f in audio_sinks_factories }
    log.debug(f"available gstreamer audio sinks:")
    for sink_factoy in available_gst_audio_sink_factories:
        log.debug(f"  {sink_factoy}")
    return available_gst_audio_sink_factories

def get_available_gst_factory_supported_properties(factory_name):
    element = Gst.ElementFactory.make(factory_name, None)
    properties = {}
    for p in element.list_properties():
        if not(p.flags & GObject.ParamFlags.WRITABLE):
            continue
        properties[p.name] = p
    log.debug(f"available properties for gst audio sink {factory_name}:")
    for p in list(properties):
        prop_type_name = properties[p].g_type_instance.g_class.g_type.name
        prop_pytype = properties[p].g_type_instance.g_class.g_type.pytype
        if prop_pytype not in _supported_property_types:
            log.debug(f"  (remove unhandled property {p} of type {prop_type_name})")
            del properties[p]
        else:
            properties[p] = ( properties[p], _supported_property_types[prop_pytype](properties[p]) )
            log.debug(f"  {p}: {prop_type_name} {properties[p][1]}")
    return properties

def _cast_str_to_prop_pytype(prop, s):
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

# ------------------------------------------------------------------------
# dumping functions for nice logs

def dump_gst_state(s):
    return s.value_nick

def dump_gst_seek_event(e):
    return e.get_structure().to_string()

def dump_gst_element(e):
    return e.name

def dump_player_message(gst_message):
    player_message, player_message_payload = extract_player_message(gst_message)
    return f"{player_message.name}{player_message_payload}"

def dump_gst_message(gst_message):
    if gst_message.type == Gst.MessageType.APPLICATION:
        msg_details_str = f"<{dump_player_message(gst_message)}>"
    else:
        msg_details_str = f"<{gst_message.get_structure().to_string() if gst_message.get_structure() else 'None'}>"
    if gst_message.src == None:
        src_str = ''
    else:
        src_str = f" src: {gst_message.src.get_name()}({gst_message.src.__class__.__name__})"
    return f"gst message {gst_message.type.first_value_nick} [{gst_message.get_seqnum()}]: {msg_details_str}{src_str}"

# ------------------------------------------------------------------------
# deal with low level format of gst messages

def extract_player_message_cb(field_name, value, payload_dict):
    payload_dict[field_name.as_str()] = value

def extract_player_message(gst_message):
    if gst_message.type == Gst.MessageType.APPLICATION:
        s = gst_message.get_structure()
        player_message = PlayerMessages[s.get_name()]
        player_message_payload = {}
        s.foreach_id_str(extract_player_message_cb, player_message_payload)
        return player_message, player_message_payload
    else:
        return None, None

# ------------------------------------------------------------------------
# dealing with tags

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

# ------------------------------------------------------------------------
# convert semitones to playback rate

def get_semitone_ratio(semitones):
    return pow(2, semitones/12.0)

# ------------------------------------------------------------------------
# the core sound player

class SoundPlayer():

    def __init__(self):
        self._lock = threading.Lock()
        self._player_state_change_cv = threading.Condition()
        self.__change_player_state(PlayerStates.UNKNOWN)
        self.metadata_callback = None
        self.gst_player = Gst.ElementFactory.make('playbin3')
        self.bus = self.gst_player.get_bus()
        self.bus.add_watch(GLib.PRIORITY_DEFAULT, self._gst_bus_message_handler, None)
        self.playback_direction = PlaybackDirection.FORWARD
        self.semitone = 0
        self.reset_seek = Gst.Event.new_seek(
            self._playback_rate,
            Gst.Format.TIME,
            Gst.SeekFlags.ACCURATE | Gst.SeekFlags.FLUSH,
            Gst.SeekType.SET, 0,
            Gst.SeekType.NONE, 0)

    # ------------------------------------------------------------------------
    # metadata

    def notify_metadata(self, metadata):
        if self.metadata_callback:
            self.metadata_callback(metadata)

    def set_metadata_callback(self, cb):
        self.metadata_callback = cb

    # ------------------------------------------------------------------------
    # output sink config

    def configure_audio_output(self, gst_sink_factory_name, gst_sink_properties):
        if not gst_sink_properties:
            gst_sink_properties = {}
        available_gst_audio_sink_factories = get_available_gst_audio_sink_factories()
        if gst_sink_factory_name:
            log.debug(f"check if gst sink '{gst_sink_factory_name}' available")
            if gst_sink_factory_name not in available_gst_audio_sink_factories:
                log.info(f"unavailable gst sink '{gst_sink_factory_name}', using default")
                gst_sink_factory_name = ''
            else:
                log.debug(f"gst sink '{gst_sink_factory_name}' is available")
        if gst_sink_factory_name:
            available_properties = get_available_gst_factory_supported_properties(gst_sink_factory_name)
            for prop in list(gst_sink_properties.keys()):
                log.debug(f"check if property '{prop}' is available for gst sink '{gst_sink_factory_name}'")
                if prop not in available_properties:
                    log.info(f"unavailable property '{prop}' for gst sink '{gst_sink_factory_name}'")
                    del gst_sink_properties[prop]
            log.debug(f"instanciate gst sink '{gst_sink_factory_name}'")
            gst_sink_instance = Gst.ElementFactory.make(gst_sink_factory_name)
            for k, v in list(gst_sink_properties.items()):
                try:
                    prop_val = _cast_str_to_prop_pytype(available_properties[k][0], v)
                    log.debug(f"gst sink '{gst_sink_factory_name}': set property '{k}' to value '{prop_val}'")
                    gst_sink_instance.set_property(k, prop_val)
                except:
                    log.error(f"gst sink '{gst_sink_factory_name}': unable to set property '{k}' to value '{prop_val}'")
                    del gst_sink_properties[k]
            try:
                self.gst_player.set_property("audio-sink", gst_sink_instance)
                log.debug(f"gst playbin: set audiosink to '{gst_sink_factory_name}' / {gst_sink_instance}")
            except:
                log.error(f"gst playbin: unable to set audiosink to '{gst_sink_factory_name}' / {gst_sink_instance}")
                return False, gst_sink_factory_name, gst_sink_properties
        if self.gst_player.get_property('audio-sink') and self.gst_player.get_property('audio-sink').get_factory():
            actual_gst_sink_factory_name = self.gst_player.get_property('audio-sink').get_factory().name
            return True, actual_gst_sink_factory_name, gst_sink_properties
        else:
            actual_gst_sink_factory_name = ''
            log.debug(f"gst playbin has no explicit sink set, will use the default sink")
            return True, actual_gst_sink_factory_name, {}

    # ------------------------------------------------------------------------
    # playback rate

    @property
    def semitone(self):
        return self._semitone

    @semitone.setter
    def semitone(self, value):
        self._semitone = value
        self._playback_rate = get_semitone_ratio(value) * self.playback_direction.value

    # ------------------------------------------------------------------------
    # gst messages / player messages utils

    def log_gst_message(self, gst_message):
        if gst_message.src == None:
            colorfunc = brightmagenta
        elif gst_message.src == self.gst_player:
            colorfunc = brightgreen
        else:
            colorfunc = cyan
        log.debug(colorfunc(dump_gst_message(gst_message)))

    def post_player_message(self, player_message, **kwargs):
        gst_message_structure = Gst.Structure.new_empty(player_message.name)
        for kwarg in kwargs:
            gst_message_structure.set_value(kwarg, kwargs[kwarg])
        gst_message = Gst.Message.new_custom(
            Gst.MessageType.APPLICATION,
            None,
            gst_message_structure)
        # MAYBE TODO: puy self.bus.post(message) in a wait cond var to
        # be sure to "see" the state change (in case it's too false)
        log.debug(f"sending player message {dump_player_message(gst_message)}")
        self.bus.post(gst_message)

    # ------------------------------------------------------------------------
    # gst async utils

    def _wait_gst_state_change(self, state_change_retval, preroll_is_error=False):
        if (state_change_retval == Gst.StateChangeReturn.FAILURE
            or (preroll_is_error
                and state_change_retval == Gst.StateChangeReturn.NO_PREROLL)):
            log.warn(f"state change returned FAILURE")
            yield PlayerStates.ERROR
        elif state_change_retval in [ Gst.StateChangeReturn.SUCCESS, Gst.StateChangeReturn.NO_PREROLL ]:
            log.debug(f"state change handled immediately")
        elif state_change_retval == Gst.StateChangeReturn.ASYNC:
            log.debug(f"state change will trigger ASYNC_DONE, wait for it")
            args = yield None
            while args.gst_msg.type != Gst.MessageType.ASYNC_DONE:
                args = yield None

    def _send_seek(self, seek):
        log.debug(f"sending seek: {dump_gst_seek_event(seek)}")
        ok = self.gst_player.send_event(seek)
        if not ok:
            log.warn(f"seek event send returned not ok")
            yield PlayerStates.ERROR
        if seek.get_structure().get_value('flags') & Gst.SeekFlags.FLUSH:
            log.debug(f"flushing seek will trigger ASYNC_DONE, wait for it")
            args = yield None
            while args.gst_msg.type != Gst.MessageType.ASYNC_DONE:
                args = yield None
        else:
            log.debug(f"seek is not flushing, do not wait for ASYNC_DONE")

    def _set_uri(self, uri, next_state):
        log.debug(f"set gst state to READY")
        state_change_retval = self.gst_player.set_state(Gst.State.READY)
        yield from self._wait_gst_state_change(state_change_retval)
        log.debug(f"set property uri of gst player to '{uri}'")
        self.gst_player.set_property('uri', uri)
        flags = self.gst_player.get_property('flags') & ~(0x00000001 | 0x00000004 | 0x00000008)
        # disable video, subtitles, visualisation
        log.debug(f"set flags of gst player to 0x{flags:08x}")
        self.gst_player.set_property('flags',
                                     flags)
        # following 4 lines not needed anymore: the gst state
        # corresponding to player state paused is gst_ready
        # log.debug(f"set gst state to PAUSED")
        # state_change_retval = self.gst_player.set_state(Gst.State.PAUSED)
        # yield from self._wait_gst_state_change(state_change_retval)
        # yield from self._send_seek(self.reset_seek)
        yield next_state

    # ------------------------------------------------------------------------
    # player state transition handlers

    def _unknown_state_transition_handler(self):
        args = yield None
        while args.player_msg != PlayerMessages.SET_URI:
            args = yield None
        yield from self._set_uri(args.gst_msg.get_structure().get_value('uri'), PlayerStates.PAUSED)

    def _paused_state_transition_handler(self):
        args = yield None
        while args.player_msg not in [ PlayerMessages.ASK_PLAY, PlayerMessages.SET_URI ]:
            args = yield None
        if args.player_msg == PlayerMessages.SET_URI:
            yield from self._set_uri(args.gst_msg.get_structure().get_value('uri'), PlayerStates.PAUSED)
        elif args.player_msg == PlayerMessages.ASK_PLAY:
            log.debug(f"set gst state to PAUSED")
            state_change_retval = self.gst_player.set_state(Gst.State.PAUSED)
            yield from self._wait_gst_state_change(state_change_retval)
            #yield from self._send_seek(self.reset_seek)
            log.debug(f"set gst state to PLAYING")
            state_change_retval = self.gst_player.set_state(Gst.State.PLAYING)
            yield from self._wait_gst_state_change(state_change_retval, preroll_is_error=True)
            yield PlayerStates.PLAYING

    def _playing_state_transition_handler(self):
        args = yield None
        while not ( args.gst_msg.type in [ Gst.MessageType.SEGMENT_DONE, Gst.MessageType.EOS ]
                    or args.player_msg in [ PlayerMessages.ASK_PAUSE, PlayerMessages.ASK_STOP ] ):
            args = yield None
        log.debug(f"set gst state to PAUSED")
        state_change_retval = self.gst_player.set_state(Gst.State.PAUSED)
        yield from self._wait_gst_state_change(state_change_retval)
        if ( args.gst_msg.type in [ Gst.MessageType.SEGMENT_DONE, Gst.MessageType.EOS ]
             or args.player_msg == PlayerMessages.ASK_STOP ):
            log.debug(f"set gst state to READY")
            state_change_retval = self.gst_player.set_state(Gst.State.READY)
            yield from self._wait_gst_state_change(state_change_retval)
        yield PlayerStates.PAUSED

    def _error_state_transition_handler(self):
        while True:
            args = yield None

    # ------------------------------------------------------------------------
    # player states transitions matrix

    _msg_player_state_handlers = {
        PlayerStates.UNKNOWN: (
            {
                Gst.MessageType.APPLICATION: ( PlayerMessages.SET_URI, ),
                Gst.MessageType.ASYNC_DONE: None,
            },
            _unknown_state_transition_handler
        ),
        PlayerStates.PAUSED: (
            {
                Gst.MessageType.APPLICATION: ( PlayerMessages.ASK_PLAY, PlayerMessages.SET_URI, ),
                Gst.MessageType.ASYNC_DONE: None,
            },
            _paused_state_transition_handler
        ),
        PlayerStates.PLAYING: (
            {
                Gst.MessageType.APPLICATION: ( PlayerMessages.ASK_PAUSE, PlayerMessages.ASK_STOP, ),
                Gst.MessageType.ASYNC_DONE: None,
                Gst.MessageType.EOS: None,
                Gst.MessageType.SEGMENT_DONE: None,
            },
            _playing_state_transition_handler
        ),
        PlayerStates.ERROR: (
            {},
            _error_state_transition_handler
        ),
    }

    # ------------------------------------------------------------------------
    # player state machine utils

    def dump_state_machine_args(self, args):
        return f"gst_msg={dump_gst_message(args.gst_msg)} player_msg={dump_player_message(args.player_msg) if args.player_msg else args.player_msg}"

    def log_state_machine_error(self, args):
        log.error(f"sound player state machine error: current_state={self._player_state} function={inspect.stack()[1][3]} {self.dump_state_machine_args(args)}")
        log_callstack()

    def wait_player_state(self, player_states):
        while self._player_state not in player_states:
            with self._player_state_change_cv:
                self._player_state_change_cv.wait()
                # est-ce que je met le whith CV autour du while ou à l'intérieur du while?
                # que se passe t-il si par exemple le state passe de
                # paused à paused_seeking, mais que le seeking va
                # tellement vite qu'il repasse immédiatement à paused
                # avant que le wait_player_state soit appelé?
                # ou alors ne pas attendre l'état du player et juste
                # envoyer des events. Mais je risque d'envoyer le seek
                # avant la completion d'autre chose
                # ou alors j'envoie juste un event play et c'est dans
                # le message handler que j'implémente toute la
                # mécanique

    def __change_player_state(self, new_state):
        with self._player_state_change_cv:
            self._player_state = new_state
            self._player_state_handler = SoundPlayer._msg_player_state_handlers[self._player_state][1](self)
            next(self._player_state_handler)
            log.debug(brightcyan(f"player state changed to {self._player_state}, state_handler is now {self._player_state_handler}"))
            self._player_state_change_cv.notify()

    # ------------------------------------------------------------------------
    # gst bus handler

    def _gst_bus_message_handler(self, bus, message, *user_data):
        if LOG_ALL_GST_MESSAGES:
            self.log_gst_message(message)
        if message.type == Gst.MessageType.WARNING:
            log.warning(f"Gstreamer WARNING: {message.type}: {message.get_structure().to_string()}")
        elif message.type == Gst.MessageType.ERROR:
            log.warning(f"Gstreamer ERROR: {message.type}: {message.get_structure().to_string()}")
        elif message.type == Gst.MessageType.TAG:
            message_struct = message.get_structure()
            taglist = message.parse_tag()
            metadata = parse_tag_list(taglist)
            self.notify_metadata(metadata)
        elif message.src == self.gst_player or message.src == None:
            current_state_handles_this_message = False
            player_message = None
            if message.type != Gst.MessageType.APPLICATION:
                if message.type in SoundPlayer._msg_player_state_handlers[self._player_state][0]:
                    current_state_handles_this_message = True
            else:
                player_message, player_message_args = extract_player_message(message)
                if player_message in SoundPlayer._msg_player_state_handlers[self._player_state][0][Gst.MessageType.APPLICATION]:
                    current_state_handles_this_message = True
            if current_state_handles_this_message:
                log.debug(brightmagenta(f"player state {self._player_state.name} received {dump_gst_message(message)}"))
                new_player_state = self._player_state_handler.send(types.SimpleNamespace(gst_msg=message, player_msg=player_message))
                if new_player_state != None:
                    # note that if new_player_state is the same state
                    # as before, it will still be handled as a state
                    # change, which instanciate a new "state
                    # transition" generator and will notify the state
                    # change cond var
                    log.debug(brightmagenta(f"player state change from {self._player_state} to {new_player_state}"))
                    self.__change_player_state(new_player_state)
                else:
                    log.debug(brightmagenta(f"player state stays {self._player_state}"))
        return True

    # ------------------------------------------------------------------------
    # public interface

    def set_path(self, path):
        with self._lock:
            uri = pathlib.Path(path).as_uri()
            self.post_player_message(PlayerMessages.SET_URI, uri=uri)
            self.wait_player_state((PlayerStates.PAUSED, PlayerStates.ERROR))
            if SLEEP_HACK_TIME > 0:
                time.sleep(SLEEP_HACK_TIME)

    def play(self, from_position=None):
        with self._lock:
            self.post_player_message(PlayerMessages.ASK_PLAY)
            self.wait_player_state((PlayerStates.PLAYING, PlayerStates.ERROR))
            if SLEEP_HACK_TIME > 0:
                time.sleep(SLEEP_HACK_TIME)

    def pause(self):
        with self._lock:
            self.post_player_message(PlayerMessages.ASK_PAUSE)
            self.wait_player_state((PlayerStates.PAUSED, PlayerStates.ERROR))
            if SLEEP_HACK_TIME > 0:
                time.sleep(SLEEP_HACK_TIME)

    def stop(self):
        with self._lock:
            self.post_player_message(PlayerMessages.ASK_STOP)
            self.wait_player_state((PlayerStates.PAUSED, PlayerStates.ERROR))
            if SLEEP_HACK_TIME > 0:
                time.sleep(SLEEP_HACK_TIME)

class Sound():

    def __init__(self, path, stat_result):
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

    def file_changed(self):
        try:
            stat_result = os.stat(self.path)
        except:
            log.debug(f"file_changed?: unable to stat {self.path}")
            return True
        return stat_result.st_mtime_ns > self.stat_result.st_mtime_ns

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
            if sound.file_changed():
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

def init_sound():
    Gst.init(None)
