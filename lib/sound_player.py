# Copyright (c) 2022-2025 Matthieu Imbert
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import re, pathlib, enum, threading, time, inspect, types, contextlib
from lib.logger import log, lightcyan, brightmagenta, lightgreen, brightgreen, lightblue, warmyellow, log_callstack
from gi.repository import GObject, Gst, GLib

SLEEP_HACK_TIME = 0 # float, in seconds. Ugly workaround for gst bug
                    # or something i don't do correctly (especially
                    # with pipewiresink)

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
    WAKE_UP = 4
    RESET = 5

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
    return {}
    values = []
    for v in prop.enum_class._value2member_map_:
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
    if not element:
        log.warn(f"unable to instanciate sink {factory_name}")
        return {}
    properties = {}
    for p in element.list_properties():
        if not(p.flags & GObject.ParamFlags.WRITABLE):
            continue
        properties[p.name] = p
    log.debug(f"available properties for gst audio sink {factory_name}:")
    property_specs = {}
    for p in properties:
        prop_type_name = properties[p].g_type_instance.g_class.g_type.name
        prop_pytype = properties[p].g_type_instance.g_class.g_type.pytype
        if prop_pytype not in _supported_property_types:
            log.debug(f"  (ignore unhandled property {p} of type {prop_type_name})")
        else:
            prop_specs = _supported_property_types[prop_pytype](properties[p])
            property_specs[p] = { 'property': properties[p], **prop_specs }
            log.debug(f"  {p}: {prop_type_name} {prop_specs}")
    return property_specs

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
        self.gst_bus_thread = None
        self._lock = threading.Lock()
        self._player_state_change_cv = threading.Condition()
        self._bus_watch_thread = None
        self.metadata_callback = None
        self.state_change_callback = None
        self.__change_player_state(PlayerStates.UNKNOWN)
        self.create_gst_player()
        self._semitone = 0
        self.loop = False
        self.create_seeks()

    def clean_close(self):
        self.stop()
        self.reset()
        self.stop_gst_bus_thread()

    def __on_pad_added(self, element, pad):
        sink_pad = self.audioconvert.get_static_pad("sink")
        if not sink_pad.is_linked():
            pad.link(sink_pad)

    def create_gst_player(self, sink_instance=None):
        self.stop_gst_bus_thread()
        self.decodebin = Gst.ElementFactory.make('uridecodebin', 'decoder')
        self.audioconvert = Gst.ElementFactory.make('audioconvert', 'converter')
        self.audioresample = Gst.ElementFactory.make('audioresample', 'resampler')
        if sink_instance:
            self.sink = sink_instance
        else:
            self.sink = Gst.ElementFactory.make('autoaudiosink', 'sink')
        self.gst_player = Gst.Pipeline.new('player')
        self.gst_player.add(self.decodebin)
        self.gst_player.add(self.audioconvert)
        self.gst_player.add(self.audioresample)
        self.gst_player.add(self.sink)
        self.audioresample.link(self.sink)
        self.audioconvert.link(self.audioresample)
        self.decodebin.connect('pad-added', self.__on_pad_added)
        self.bus = self.gst_player.get_bus()
        self.start_gst_bus_thread()

    def gst_bus_run(self):
        try:
            self.glib_context = GLib.MainContext()
            self.glib_context.push_thread_default()
            self.glib_loop = GLib.MainLoop(self.glib_context)
            self.bus_watch_source_id = self.bus.add_watch(GLib.PRIORITY_DEFAULT, self._gst_bus_message_handler, None)
            self.glib_loop.run()
        finally:
            self.glib_context.pop_thread_default()

    def start_gst_bus_thread(self):
        self.gst_bus_thread = threading.Thread(target=self.gst_bus_run)
        self.gst_bus_thread.start()

    def stop_gst_bus_thread(self):
        if self.gst_bus_thread:
            self.bus.remove_watch()
            self.glib_loop.quit()
            self.gst_bus_thread.join()

    def create_seeks(self):
        # seeks need to be preallocated in the main thread, because
        # creating them from the bus watch causes deadlocks
        self.reset_seek = Gst.Event.new_seek(
            self.playback_rate,
            Gst.Format.TIME,
            Gst.SeekFlags.ACCURATE | Gst.SeekFlags.FLUSH,
            Gst.SeekType.SET, 0,
            Gst.SeekType.NONE, -1)
        self.loop_seek = Gst.Event.new_seek(
            self.playback_rate,
            Gst.Format.TIME,
            Gst.SeekFlags.ACCURATE | Gst.SeekFlags.SEGMENT,
            Gst.SeekType.SET, 0,
            Gst.SeekType.END, 0)
        self.reset_loop_seek = Gst.Event.new_seek(
            self.playback_rate,
            Gst.Format.TIME,
            Gst.SeekFlags.ACCURATE | Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH,
            Gst.SeekType.SET, 0,
            Gst.SeekType.END, 0)

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
        self.stop()
        self.reset()
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
            for prop_name in list(gst_sink_properties.keys()):
                log.debug(f"check if property '{prop_name}' is available for gst sink '{gst_sink_factory_name}'")
                if prop_name not in available_properties:
                    log.info(f"unavailable property '{prop_name}' for gst sink '{gst_sink_factory_name}'")
                    del gst_sink_properties[prop_name]
            log.debug(f"instanciate gst sink '{gst_sink_factory_name}'")
            gst_sink_instance = Gst.ElementFactory.make(gst_sink_factory_name, 'sink')
            for prop_name, prop_value in list(gst_sink_properties.items()):
                try:
                    pytype_prop_value = _cast_str_to_prop_pytype(available_properties[prop_name]['property'], prop_value)
                    log.debug(f"gst sink '{gst_sink_factory_name}': set property '{prop_name}' to value '{pytype_prop_value}'")
                    gst_sink_instance.set_property(prop_name, pytype_prop_value)
                except:
                    log.error(f"gst sink '{gst_sink_factory_name}': unable to set property '{prop_name}' to value '{pytype_prop_value}'")
                    del gst_sink_properties[prop_name]
            try:
                self.create_gst_player(gst_sink_instance)
                log.debug(f"gst player: set audiosink to '{gst_sink_factory_name}' / {gst_sink_instance}")
                return True, gst_sink_factory_name, gst_sink_properties
            except:
                log.error(f"gst player: unable to set audiosink to '{gst_sink_factory_name}' / {gst_sink_instance}")
                return False, gst_sink_factory_name, gst_sink_properties
        else:
            try:
                self.create_gst_player()
                log.debug(f"gst player: set audiosink to None (default, autoaudiosink)")
                return True, gst_sink_factory_name, {}
            except:
                log.error(f"gst player: unable to set audiosink to None (default, autoaudiosink)")
                return False, gst_sink_factory_name, gst_sink_properties

    # ------------------------------------------------------------------------
    # playback rate

    @property
    def semitone(self):
        return self._semitone

    @semitone.setter
    def semitone(self, value):
        self._semitone = value
        self.update_rate()

    @property
    def playback_rate(self):
        return get_semitone_ratio(self.semitone)

    def update_rate(self):
        self.create_seeks()
        if self.player_state in [ PlayerStates.PLAYING, PlayerStates.PAUSED ]:
            got_duration, duration = self.gst_player.query_duration(Gst.Format.TIME)
            got_position, position = self.gst_player.query_position(Gst.Format.TIME)
            if got_duration and got_position:
                if self.loop:
                    update_seek = Gst.Event.new_seek(
                        self.playback_rate,
                        Gst.Format.TIME,
                        Gst.SeekFlags.ACCURATE | Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH,
                        Gst.SeekType.SET, position,
                        Gst.SeekType.SET, duration)
                else:
                    update_seek = Gst.Event.new_seek(
                        self.playback_rate,
                        Gst.Format.TIME,
                        Gst.SeekFlags.ACCURATE | Gst.SeekFlags.FLUSH,
                        Gst.SeekType.SET, position,
                        Gst.SeekType.SET, duration)
                log.debug(f"seek: {dump_gst_seek_event(update_seek)}")
                ok = self.gst_player.send_event(update_seek)
                if not ok:
                    log.warn(f"send seek={dump_gst_seek_event(update_seek)} returned not ok")
            else:
                log.warn(f"unable to seek, because got_duration={got_duration} got_position={got_position}")

    # ------------------------------------------------------------------------
    # query seeking

    def _query_seeking(self):
        seek_query = Gst.Query.new_seeking(Gst.Format.TIME)
        seek_query_retval = self.gst_player.query(seek_query)
        if seek_query_retval:
            seek_query_answer = seek_query.parse_seeking()
            log.debug(warmyellow(f"query seeking returned seek_query_answer={seek_query_answer}"))
            return seek_query_answer # format, seekable, segment_start, segment_end
        else:
            log.debug(warmyellow(f"query seeking failed seek_query_retval={seek_query_retval}"))
            return None

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
        # be sure to "see" the state change (in case it's too fast)
        log.debug(f"sending player message {dump_player_message(gst_message)}")
        self.bus.post(gst_message)

    # ------------------------------------------------------------------------
    # async utils

    def _wait(self, delay): # float seconds
        log.debug(warmyellow(f"create wait thread for {delay}s"))
        def wait_delay():
            log.debug(warmyellow(f"sleep {delay}s"))
            time.sleep(delay)
            log.debug(warmyellow(f"post wakeup"))
            self.post_player_message(PlayerMessages.WAKE_UP)
        threading.Thread(target=wait_delay).start()
        args = yield None
        while args.player_msg != PlayerMessages.WAKE_UP:
            args = yield None
        log.debug(warmyellow(f"waking up"))

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
        log.debug(lightgreen(f"set gst state to READY"))
        state_change_retval = self.gst_player.set_state(Gst.State.READY)
        yield from self._wait_gst_state_change(state_change_retval)
        log.debug(f"set property uri of gst player to '{uri}'")
        self.decodebin.set_property('uri', uri)
        yield next_state

    def _reset(self):
        log.debug(warmyellow(f"reset gst player"))
        log.debug(lightgreen(f"set gst state to READY"))
        state_change_retval = self.gst_player.set_state(Gst.State.READY)
        yield from self._wait_gst_state_change(state_change_retval)
        log.debug(lightgreen(f"set gst state to NULL"))
        state_change_retval = self.gst_player.set_state(Gst.State.NULL)
        yield from self._wait_gst_state_change(state_change_retval)
        yield PlayerStates.UNKNOWN

    # ------------------------------------------------------------------------
    # player state transition handlers

    def _unknown_state_transition_handler(self):
        args = yield None
        while args.player_msg != PlayerMessages.SET_URI:
            args = yield None
        yield from self._set_uri(args.gst_msg.get_structure().get_value('uri'), PlayerStates.STOPPED)

    def _stopped_state_transition_handler(self):
        args = yield None
        while args.player_msg not in [ PlayerMessages.ASK_PLAY,
                                       PlayerMessages.SET_URI,
                                       PlayerMessages.RESET ]:
            args = yield None
        if args.player_msg == PlayerMessages.RESET:
            yield from self._reset()
        elif args.player_msg == PlayerMessages.SET_URI:
            yield from self._set_uri(args.gst_msg.get_structure().get_value('uri'), PlayerStates.STOPPED)
        elif args.player_msg == PlayerMessages.ASK_PLAY:
            start_pos = args.gst_msg.get_structure().get_value('start_pos')

            log.debug(lightgreen(f"set gst state to PAUSED"))
            state_change_retval = self.gst_player.set_state(Gst.State.PAUSED)
            yield from self._wait_gst_state_change(state_change_retval)

            # need a reset seek to
            # - init playback rate
            # - in case of playing from a start position > 0, allow
            #   getting reliably the duration of the sound which is
            #   needed to do the position seek just after
            # - in case of looping, allow getting reliably the
            #   duration of the sound which is needed to do a segment
            #   seek just after
            # (getting duration without an appropriate reset seek
            # fails for some sounds)
            yield from self._send_seek(self.reset_seek)
            got_duration, duration = self.gst_player.query_duration(Gst.Format.TIME)
            if got_duration:
                log.debug(warmyellow(f"duration = {duration}"))
                if self.loop:
                    seek = Gst.Event.new_seek(
                        self.playback_rate,
                        Gst.Format.TIME,
                        Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH,
                        Gst.SeekType.SET, start_pos * duration,
                        Gst.SeekType.SET, duration)
                else:
                    seek = Gst.Event.new_seek(
                        self.playback_rate,
                        Gst.Format.TIME,
                        Gst.SeekFlags.FLUSH,
                        Gst.SeekType.SET, start_pos * duration,
                        Gst.SeekType.SET, duration)
                yield from self._send_seek(seek)
            else:
                log.warn(f"unable to perform initial seek, because unable to get sound duration")

            log.debug(lightgreen(f"set gst state to PLAYING"))
            state_change_retval = self.gst_player.set_state(Gst.State.PLAYING)
            yield from self._wait_gst_state_change(state_change_retval, preroll_is_error=True)
            yield PlayerStates.PLAYING

    def _paused_state_transition_handler(self):
        args = yield None
        while args.player_msg not in [ PlayerMessages.ASK_PLAY,
                                       PlayerMessages.SET_URI,
                                       PlayerMessages.RESET,
                                       PlayerMessages.ASK_STOP ]:
            args = yield None
        if args.player_msg == PlayerMessages.RESET:
            yield from self._reset()
        elif args.player_msg == PlayerMessages.SET_URI:
            yield from self._set_uri(args.gst_msg.get_structure().get_value('uri'), PlayerStates.STOPPED)
        elif args.player_msg == PlayerMessages.ASK_PLAY:
            log.debug(lightgreen(f"set gst state to PLAYING"))
            state_change_retval = self.gst_player.set_state(Gst.State.PLAYING)
            yield from self._wait_gst_state_change(state_change_retval, preroll_is_error=True)
            yield PlayerStates.PLAYING
        elif args.player_msg == PlayerMessages.ASK_STOP:
            log.debug(lightgreen(f"set gst state to READY"))
            state_change_retval = self.gst_player.set_state(Gst.State.READY)
            yield from self._wait_gst_state_change(state_change_retval)
            yield PlayerStates.STOPPED

    def _playing_state_transition_handler(self):
        args = yield None
        while not ( args.gst_msg.type in [ Gst.MessageType.SEGMENT_DONE,
                                           Gst.MessageType.EOS ]
                    or args.player_msg in [ PlayerMessages.ASK_PAUSE,
                                            PlayerMessages.ASK_STOP,
                                            PlayerMessages.RESET ] ):
            args = yield None
        if self.loop and args.gst_msg.type == Gst.MessageType.SEGMENT_DONE:
            yield from self._send_seek(self.loop_seek)
            yield PlayerStates.PLAYING
        elif self.loop and args.gst_msg.type == Gst.MessageType.EOS:
            yield from self._send_seek(self.reset_loop_seek)
            yield PlayerStates.PLAYING
        else:
            log.debug(lightgreen(f"set gst state to PAUSED"))
            state_change_retval = self.gst_player.set_state(Gst.State.PAUSED)
            yield from self._wait_gst_state_change(state_change_retval)
            if args.player_msg == PlayerMessages.RESET:
                yield from self._reset()
            elif args.gst_msg.type == Gst.MessageType.EOS or args.player_msg == PlayerMessages.ASK_STOP:
                log.debug(lightgreen(f"set gst state to READY"))
                state_change_retval = self.gst_player.set_state(Gst.State.READY)
                yield from self._wait_gst_state_change(state_change_retval)
                yield PlayerStates.STOPPED
            else:
                yield PlayerStates.PAUSED

    def _error_state_transition_handler(self):
        args = yield None
        while args.player_msg != PlayerMessages.RESET:
            args = yield None
        yield from self._reset()

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
                Gst.MessageType.APPLICATION: ( PlayerMessages.ASK_PLAY,
                                               PlayerMessages.SET_URI,
                                               PlayerMessages.WAKE_UP,
                                               PlayerMessages.RESET ),
                Gst.MessageType.ASYNC_DONE: None,
            },
            _stopped_state_transition_handler
        ),
        PlayerStates.PAUSED: (
            {
                Gst.MessageType.APPLICATION: ( PlayerMessages.ASK_PLAY,
                                               PlayerMessages.SET_URI,
                                               PlayerMessages.ASK_STOP,
                                               PlayerMessages.RESET ),
                Gst.MessageType.ASYNC_DONE: None,
            },
            _paused_state_transition_handler
        ),
        PlayerStates.PLAYING: (
            {
                Gst.MessageType.APPLICATION: ( PlayerMessages.ASK_PAUSE,
                                               PlayerMessages.ASK_STOP,
                                               PlayerMessages.RESET ),
                Gst.MessageType.ASYNC_DONE: None,
                Gst.MessageType.EOS: None,
                Gst.MessageType.SEGMENT_DONE: None,
            },
            _playing_state_transition_handler
        ),
        PlayerStates.ERROR: (
            {
                Gst.MessageType.APPLICATION: ( PlayerMessages.RESET, ),
            },
            _error_state_transition_handler
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
        log.debug(warmyellow(f"current state = {self.player_state.name}, wait for state to be in {[ s.name for s in player_states ]}"))
        if threading.current_thread() != self._bus_watch_thread and self._bus_watch_thread != None:
            while self.player_state not in player_states:
                with self._player_state_change_cv:
                    self._player_state_change_cv.wait()
        log.debug(warmyellow(f"wait finished, current state = {self.player_state.name}"))

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
        self._bus_watch_thread = threading.current_thread()
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
                if (Gst.MessageType.APPLICATION in SoundPlayer._msg_player_state_handlers[self.player_state][0]
                    and player_message in SoundPlayer._msg_player_state_handlers[self.player_state][0][Gst.MessageType.APPLICATION]):
                    current_state_handles_this_message = True
            if current_state_handles_this_message:
                log.debug(brightmagenta(f"send {dump_gst_message(message)} to player state {self.player_state.name}/{self._player_state_handler.gi_code.co_name}:{self._player_state_handler.gi_frame.f_lineno}"))
                new_player_state = self._player_state_handler.send(types.SimpleNamespace(gst_msg=message, player_msg=player_message))
                if new_player_state != None:
                    # note that if new_player_state is the same state
                    # as before, it will still be handled as a state
                    # change, which instanciate a new "state
                    # transition" generator and will notify the state
                    # change cond var
                    log.debug(brightgreen(f"player state change from {self.player_state} to {new_player_state}"))
                    self.__change_player_state(new_player_state)
                    log.debug(brightgreen(f"new player state {self.player_state}/{self._player_state_handler.gi_code.co_name}:{self._player_state_handler.gi_frame.f_lineno}"))
                else:
                    log.debug(brightgreen(f"player state stays {self.player_state}/{self._player_state_handler.gi_code.co_name}:{self._player_state_handler.gi_frame.f_lineno}"))
        return True

    # ------------------------------------------------------------------------
    # public interface

    def set_path(self, path):
        log.debug(warmyellow(f"set_path path={path}"))
        with self._lock:
            uri = pathlib.Path(path).as_uri()
            self.post_player_message(PlayerMessages.SET_URI, uri=uri)
            self.wait_player_state((PlayerStates.STOPPED, PlayerStates.ERROR))
            if SLEEP_HACK_TIME > 0:
                time.sleep(SLEEP_HACK_TIME)

    def play(self, start_pos=0):
        # 0 <= start_pos <= 1.0
        log.debug(warmyellow(f"play start_pos={start_pos}"))
        with self._lock:
            self.post_player_message(PlayerMessages.ASK_PLAY, start_pos=start_pos)
            self.wait_player_state((PlayerStates.PLAYING, PlayerStates.ERROR))
            if SLEEP_HACK_TIME > 0:
                time.sleep(SLEEP_HACK_TIME)

    def pause(self):
        log.debug(warmyellow(f"pause"))
        with self._lock:
            self.post_player_message(PlayerMessages.ASK_PAUSE)
            self.wait_player_state((PlayerStates.PAUSED, PlayerStates.ERROR))
            if SLEEP_HACK_TIME > 0:
                time.sleep(SLEEP_HACK_TIME)

    def stop(self):
        log.debug(warmyellow(f"stop"))
        with self._lock:
            self.post_player_message(PlayerMessages.ASK_STOP)
            self.wait_player_state((PlayerStates.UNKNOWN, PlayerStates.STOPPED, PlayerStates.ERROR))
            if SLEEP_HACK_TIME > 0:
                time.sleep(SLEEP_HACK_TIME)

    def reset(self):
        log.debug(warmyellow(f"reset"))
        with self._lock:
            self.post_player_message(PlayerMessages.RESET)
            self.wait_player_state((PlayerStates.UNKNOWN,))
            if SLEEP_HACK_TIME > 0:
                time.sleep(SLEEP_HACK_TIME)

    def seek(self, seek_pos):
        # 0 <= seek_pos <= 1.0
        log.debug(warmyellow(f"seek seek_pos={seek_pos}"))
        got_duration, duration = self.gst_player.query_duration(Gst.Format.TIME)
        if got_duration:
            if self.player_state == PlayerStates.PLAYING:
                seek_flags = Gst.SeekFlags.NONE
            elif self.player_state == PlayerStates.PAUSED:
                seek_flags = Gst.SeekFlags.FLUSH
            else:
                log.warn(f"trying to seek from state {self.player_state.name}")
            if self.loop:
                seek_flags = seek_flags | Gst.SeekFlags.SEGMENT
                seek = Gst.Event.new_seek(
                    self.playback_rate,
                    Gst.Format.TIME,
                    seek_flags,
                    Gst.SeekType.SET, seek_pos * duration,
                    Gst.SeekType.SET, duration)
            else:
                seek = Gst.Event.new_seek(
                    self.playback_rate,
                    Gst.Format.TIME,
                    seek_flags,
                    Gst.SeekType.SET, seek_pos * duration,
                    Gst.SeekType.SET, duration)
            log.debug(f"seek: {dump_gst_seek_event(seek)}")
            ok = self.gst_player.send_event(seek)
            if not ok:
                log.warn(f"send seek={dump_gst_seek_event(update_seek)} returned not ok")
        else:
            log.warn(f"unable to seek, because unable to get duration")

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
