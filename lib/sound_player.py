import re, pathlib, enum, threading, time, inspect, types, contextlib
from lib.logger import log, lightcyan, brightmagenta, brightgreen, lightblue, log_callstack
from gi.repository import GObject, Gst, GLib

SLEEP_HACK_TIME = 0 # ugly workaround for gst bug or something i don't
                    # do correctly (especially with pipewiresink)

class PlayerStates(enum.Enum):
    UNKNOWN = 0
    ERROR = 1
    STOPPED = 2
    PLAYING = 3
    PAUSED = 4

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
        tag_name = taglist.nth_tag_name(i)
        value_tuple = None
        if tag_name in [ 'title', 'artist', 'album', 'genre', 'musical-key', 'album-artist', 'encoder', 'channel-mode', 'audio-codec', 'container-format', 'comment' ]:
            value_tuple = taglist.get_string(tag_name)
        elif tag_name in [ 'track-count', 'track-number', 'minimum-bitrate', 'maximum-bitrate', 'bitrate' ]:
            value_tuple = taglist.get_uint(tag_name)
        elif tag_name == 'duration':
            value_tuple = taglist.get_uint64(tag_name)
        elif tag_name in [  'beats-per-minute', 'replaygain-track-gain', 'replaygain-album-gain', 'replaygain-track-peak', 'replaygain-album-peak' ]:
            value_tuple = taglist.get_double(tag_name)
        elif tag_name == 'datetime':
            value_tuple = taglist.get_date_time(tag_name)
            if value_tuple[0]:
                value_tuple = (True, value_tuple[1].to_iso8601_string())
            else:
                value_tuple = taglist.get_date(tag_name)
                if value_tuple[0]:
                    value_tuple = (True, value_tuple[1].to_struct_tm()) # never tested, need to find an example stream
        elif tag_name == 'has-crc':
            value_tuple = taglist.get_boolean(tag_name)
        elif tag_name == 'image':
            sample_ok, sample = taglist.get_sample(tag_name)
            if sample_ok:
                gst_mem = sample.get_buffer().get_all_memory()
                map_ok, map_info = gst_mem.map(Gst.MapFlags.READ)
                if map_ok:
                    try:
                        map_data = bytes(map_info.data)
                        value_tuple = (True, map_data)
                    finally:
                        gst_mem.unmap(map_info)
                else:
                    log.warn(f"unsuccessful map gst_memory={gst_mem} tag={i}/{tag_name} of {taglist}")
            else:
                log.warn(f"unsuccessful get_sample_index tag={i}/{tag_name} of {taglist}")
        if value_tuple and value_tuple[0]:
            if tag_name == 'container-format':
                containers[value_tuple[1]] = tmp
                tmp = {}
            else:
                tmp[tag_name] = value_tuple[1]
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
        self._bus_wath_thread = None
        self.metadata_callback = None
        self.state_change_callback = None
        self.__change_player_state(PlayerStates.UNKNOWN)
        self.gst_player = Gst.ElementFactory.make('playbin')
        self.bus = self.gst_player.get_bus()
        self.bus.add_watch(GLib.PRIORITY_DEFAULT, self._gst_bus_message_handler, None)
        self._semitone = 0
        self._direction = PlaybackDirection.FORWARD
        self.reset_seek = Gst.Event.new_seek(
            self.playback_rate,
            Gst.Format.TIME,
            Gst.SeekFlags.ACCURATE | Gst.SeekFlags.FLUSH,
            Gst.SeekType.SET, 0,
            Gst.SeekType.NONE, 0)

    # ------------------------------------------------------------------------
    # callbacks

    def notify_metadata(self, metadata):
        if self.metadata_callback:
            self.metadata_callback(metadata)

    def set_metadata_callback(self, cb):
        self.metadata_callback = cb

    def notify_state_change(self, state):
        if self.state_change_callback:
            self.state_change_callback(state)

    def set_state_change_callback(self, cb):
        self.state_change_callback = cb

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

    @property
    def direction(self):
        return self._direction

    @direction.setter
    def direction(self, direction):
        self._direction = direction

    @property
    def playback_rate(self):
        return get_semitone_ratio(self.semitone) * self.direction.value

    # ------------------------------------------------------------------------
    # gst messages / player messages utils

    def log_gst_message(self, gst_message):
        if gst_message.src == None:
            colorfunc = lightblue
        elif gst_message.src == self.gst_player:
            colorfunc = lightblue
        else:
            colorfunc = lightcyan
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
        yield from self._set_uri(args.gst_msg.get_structure().get_value('uri'), PlayerStates.STOPPED)

    def _stopped_state_transition_handler(self):
        args = yield None
        while args.player_msg not in [ PlayerMessages.ASK_PLAY, PlayerMessages.SET_URI ]:
            args = yield None
        if args.player_msg == PlayerMessages.SET_URI:
            yield from self._set_uri(args.gst_msg.get_structure().get_value('uri'), PlayerStates.STOPPED)
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
            yield PlayerStates.STOPPED
        else:
            yield PlayerStates.PAUSED

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
        PlayerStates.STOPPED: (
            {
                Gst.MessageType.APPLICATION: ( PlayerMessages.ASK_PLAY, PlayerMessages.SET_URI, ),
                Gst.MessageType.ASYNC_DONE: None,
            },
            _stopped_state_transition_handler
        ),
        PlayerStates.PAUSED: (
            {
                Gst.MessageType.APPLICATION: ( PlayerMessages.ASK_PLAY, PlayerMessages.SET_URI, ),
                Gst.MessageType.ASYNC_DONE: None,
            },
            _stopped_state_transition_handler
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
            _unknown_state_transition_handler
        ),
    }

    # ------------------------------------------------------------------------
    # player state machine utils

    def dump_state_machine_args(self, args):
        return f"gst_msg={dump_gst_message(args.gst_msg)} player_msg={dump_player_message(args.player_msg) if args.player_msg else args.player_msg}"

    def log_state_machine_error(self, args):
        log.error(f"sound player state machine error: current_state={self.player_state} function={inspect.stack()[1][3]} {self.dump_state_machine_args(args)}")
        log_callstack()

    def wait_player_state(self, player_states):
        if threading.current_thread() != self._bus_wath_thread and self._bus_wath_thread != None:
            while self.player_state not in player_states:
                with self._player_state_change_cv:
                    self._player_state_change_cv.wait()
                    # est-ce que je met le whith CV autour du while ou
                    # à l'intérieur du while?  que se passe t-il si
                    # par exemple le state passe de paused à
                    # paused_seeking, mais que le seeking va tellement
                    # vite qu'il repasse immédiatement à paused avant
                    # que le wait_player_state soit appelé?  ou alors
                    # ne pas attendre l'état du player et juste
                    # envoyer des events. Mais je risque d'envoyer le
                    # seek avant la completion d'autre chose ou alors
                    # j'envoie juste un event play et c'est dans le
                    # message handler que j'implémente toute la
                    # mécanique

    def __change_player_state(self, new_state):
        with self._player_state_change_cv:
            self.player_state = new_state
            self._player_state_handler = SoundPlayer._msg_player_state_handlers[self.player_state][1](self)
            next(self._player_state_handler)
            self._player_state_change_cv.notify()
        self.notify_state_change(new_state)

    # ------------------------------------------------------------------------
    # gst bus handler

    def _gst_bus_message_handler(self, bus, message, *user_data):
        if not self._bus_wath_thread:
            self._bus_wath_thread = threading.current_thread()
        if log.log_all_gst_messages:
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
                if message.type in SoundPlayer._msg_player_state_handlers[self.player_state][0]:
                    current_state_handles_this_message = True
            else:
                player_message, player_message_args = extract_player_message(message)
                if player_message in SoundPlayer._msg_player_state_handlers[self.player_state][0][Gst.MessageType.APPLICATION]:
                    current_state_handles_this_message = True
            if current_state_handles_this_message:
                log.debug(brightmagenta(f"player state {self.player_state.name} received {dump_gst_message(message)}"))
                new_player_state = self._player_state_handler.send(types.SimpleNamespace(gst_msg=message, player_msg=player_message))
                if new_player_state != None:
                    # note that if new_player_state is the same state
                    # as before, it will still be handled as a state
                    # change, which instanciate a new "state
                    # transition" generator and will notify the state
                    # change cond var
                    log.debug(brightgreen(f"player state change from {self.player_state} to {new_player_state}"))
                    self.__change_player_state(new_player_state)
                else:
                    log.debug(brightgreen(f"player state stays {self.player_state}"))
        return True

    # ------------------------------------------------------------------------
    # public interface

    def _get_state_change_lock(self):
        if threading.current_thread() == self._bus_wath_thread or self._bus_wath_thread == None:
            return contextlib.nullcontext()
        else:
            return self._lock

    def set_path(self, path):
        with self._get_state_change_lock():
            uri = pathlib.Path(path).as_uri()
            self.post_player_message(PlayerMessages.SET_URI, uri=uri)
            self.wait_player_state((PlayerStates.STOPPED, PlayerStates.ERROR))
            if SLEEP_HACK_TIME > 0:
                time.sleep(SLEEP_HACK_TIME)

    def play(self, from_position=0):
        with self._get_state_change_lock():
            self.post_player_message(PlayerMessages.ASK_PLAY, position=from_position)
            self.wait_player_state((PlayerStates.PLAYING, PlayerStates.ERROR))
            if SLEEP_HACK_TIME > 0:
                time.sleep(SLEEP_HACK_TIME)

    def pause(self):
        with self._get_state_change_lock():
            self.post_player_message(PlayerMessages.ASK_PAUSE)
            self.wait_player_state((PlayerStates.PAUSED, PlayerStates.ERROR))
            if SLEEP_HACK_TIME > 0:
                time.sleep(SLEEP_HACK_TIME)

    def stop(self):
        with self._get_state_change_lock():
            self.post_player_message(PlayerMessages.ASK_STOP)
            self.wait_player_state((PlayerStates.STOPPED, PlayerStates.ERROR))
            if SLEEP_HACK_TIME > 0:
                time.sleep(SLEEP_HACK_TIME)

    def is_seekable(self):
        query = Gst.Query.new_seeking(Gst.Format.TIME)
        query_retval = self.gst_player.query(query)
        if query_retval:
            return query.parse_seeking().seekable
        else:
            return False

    def get_duration_position(self):
        got_duration, duration = self.gst_player.query_duration(Gst.Format.TIME)
        got_position, position = self.gst_player.query_position(Gst.Format.TIME)
        if got_duration:
            tv1 = duration
        else:
            tv1 = None
        if got_position:
            tv2 = position
        else:
            tv2 = None
        return (tv1, tv2)

def init_sound():
    Gst.init(None)
