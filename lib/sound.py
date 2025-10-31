import os, os.path, re, pathlib, enum, threading, time, inspect, types
from lib.utils import LRU, format_duration
from lib.logger import log, cyan, brightgreen, brightmagenta, brightcyan, log_callstack
from lib.config import config
from gi.repository import GObject, Gst, GLib
from PySide2 import QtCore

CACHE_SIZE = 256
GST_BLOCKING_WAIT_TIMEOUT = 1000 * Gst.MSECOND
log_all_gst_messages = True
from enum import Enum

class PlayerStates(enum.Enum):
    UNKNOWN = 0
    PAUSED = 1
    PLAYING = 3

class PlayerMessages(enum.Enum):
    ASK_PAUSE = 0
    ASK_SEEK = 1
    ASK_PLAY = 2
    SEEK_COMPLETE = 3

class PlaybackDirection(enum.Enum):
    FORWARD = 1
    BACKWARD = -1

def get_semitone_ratio(semitones):
    return pow(2, semitones/12.0)

_blacklisted_gst_audio_sink_factory_regexes = [
    '^interaudiosink$',
    '^ladspasink.*',
]

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
    for p in properties:
        log.debug(f"  {p}")
    return properties

def dump_gst_state(s):
    return s.value_nick

def dump_gst_seek_event(e):
    return e.get_structure().to_string()

def dump_gst_element(e):
    return e.name

def set_gst_state_blocking(element, gst_state):
    log.debug(f"set_gst_state_blocking: element={dump_gst_element(element)} gst_state={dump_gst_state(gst_state)}")
    r = element.set_state(gst_state)
    if r == Gst.StateChangeReturn.ASYNC:
        log.debug(f"wait for async completion")
        retcode, gst_state, pending_gst_state = element.get_state(GST_BLOCKING_WAIT_TIMEOUT)
        log.debug(f"end of wait for async completion. retcode={retcode} gst_state={dump_gst_state(gst_state)} pending_state={dump_gst_state(pending_gst_state)}")
        if retcode == Gst.StateChangeReturn.FAILURE:
            log.warning(f"gst async gst_state change failure on element {dump_gst_element(element)} after timeout of {GST_BLOCKING_WAIT_TIMEOUT / Gst.SECOND}ms")
            log_callstack()
        elif retcode == Gst.StateChangeReturn.ASYNC:
            log.warning(f"gst async gst_state change on element {dump_gst_element(element)} still async after timeout of {GST_BLOCKING_WAIT_TIMEOUT / Gst.SECOND}ms")
            log_callstack()
        return retcode
    return r

def query_seek(element):
    query = Gst.Query.new_seeking(Gst.Format.TIME)
    query_retval = element.query(query)
    if query_retval:
        query_answer = query.parse_seeking()
    else:
        query_answer = None
    log.debug(f"query seeking: success={query_retval}, answer={query_answer})")
    return query_retval, query_answer

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

class SoundPlayer():

    def __init__(self):
        # from lib.debug import debug_toggle_thread
        # debug_toggle_thread()
        self._player_state = PlayerStates.UNKNOWN
        self._player_state_handler = self._unknown_state_transition_handler()
        next(self._player_state_handler)
        self._player_state_change_cv = threading.Condition()
        self.gst_player = Gst.ElementFactory.make('playbin')
        self.gst_player.set_property('flags', self.gst_player.get_property('flags') & ~(0x00000001 | 0x00000004 | 0x00000008)) # disable video, subtitles, visualisation
        self.configure_audio_output()
        self.bus = self.gst_player.get_bus()
        self.bus.add_watch(GLib.PRIORITY_DEFAULT, self._gst_bus_message_handler, None)
        self.playback_direction = PlaybackDirection.FORWARD
        self.semitone = 0
        #self.post_player_message(PlayerMessages.ASK_PAUSE)
        #time.sleep(1.0)

    def configure_audio_output(self):
        gst_sink_factory_name = config['gst_audio_sink']
        available_gst_audio_sink_factories = get_available_gst_audio_sink_factories()
        if gst_sink_factory_name:
            log.debug(f"check if gst sink '{gst_sink_factory_name}' available")
            if gst_sink_factory_name not in available_gst_audio_sink_factories:
                log.info(f"unavailable gst sink '{gst_sink_factory_name}', using default")
                gst_sink_factory_name = ''
            else:
                log.debug(f"gst sink '{gst_sink_factory_name}' is available")
        if gst_sink_factory_name:
            if gst_sink_factory_name not in config['gst_audio_sink_properties']:
                config['gst_audio_sink_properties'][gst_sink_factory_name] = {}
            available_properties = get_available_gst_factory_supported_properties(gst_sink_factory_name)
            for config_prop in list(config['gst_audio_sink_properties'][gst_sink_factory_name].keys()):
                log.debug(f"check if property '{config_prop}' (from config) is available for gst sink '{gst_sink_factory_name}'")
                if config_prop not in available_properties:
                    log.info(f"unavailable property '{config_prop}' for gst sink '{gst_sink_factory_name}', removing it from config")
                    del config['gst_audio_sink_properties'][gst_sink_factory_name][config_prop]
            log.debug(f"instanciate gst sink '{gst_sink_factory_name}'")
            gst_sink_instance = Gst.ElementFactory.make(gst_sink_factory_name)
            for k, v in list(config['gst_audio_sink_properties'][gst_sink_factory_name].items()):
                try:
                    log.debug(f"gst sink '{gst_sink_factory_name}': set property '{k}' to value '{cast_str_to_prop_pytype(available_properties[k], v)}'")
                    gst_sink_instance.set_property(k, cast_str_to_prop_pytype(available_properties[k], v))
                except:
                    log.error(f"gst sink '{gst_sink_factory_name}': unable to set property '{k}' to value '{cast_str_to_prop_pytype(available_properties[k], v)}'")
                    del config['gst_audio_sink_properties'][gst_sink_factory_name][k]
            try:
                self.gst_player.set_property("audio-sink", gst_sink_instance)
                log.debug(f"gst playbin: set audiosink to '{gst_sink_factory_name}' / {gst_sink_instance}")
            except:
                log.error(f"gst playbin: unable to set audiosink to '{gst_sink_factory_name}' / {gst_sink_instance}")
        if self.gst_player.get_property('audio-sink') and self.gst_player.get_property('audio-sink').get_factory():
            actual_gst_sink_factory_name = self.gst_player.get_property('audio-sink').get_factory().name
        else:
            actual_gst_sink_factory_name = ''
            log.debug(f"gst playbin has no explcit sink set, will use the default sink")
        config['gst_audio_sink'] = actual_gst_sink_factory_name

    def set_path(self, path):
        uri = pathlib.Path(path).as_uri()
        log.debug(f"set gst_player.uri to {uri}")
        self.gst_player.set_state(Gst.State.NULL)
        self.gst_player.set_property('uri', uri)
        self.pause()
        time.sleep(1)

    def get_duration_position(self):
        got_duration, duration = self.gst_player.query_duration(Gst.Format.TIME)
        got_position, position = self.gst_playerplayer.query_position(Gst.Format.TIME)
        return duration if got_duration else None, position if got_position else None

    @property
    def semitone(self):
        return self._semitone

    @semitone.setter
    def semitone(self, value):
        self._semitone = value
        self._playback_rate = get_semitone_ratio(value) * self.playback_direction.value
        got_seek_query_answer, seek_query_answer = query_seek(self.gst_player)
        got_position, position = self.gst_player.query_position(Gst.Format.TIME)
        if got_position:
            if self._playback_rate > 0.0:
                if got_seek_query_answer and seek_query_answer.seekable:
                    self.gst_player.seek(
                        self._playback_rate,
                        Gst.Format.TIME,
                        Gst.SeekFlags.FLUSH,
                        Gst.SeekType.SET, position,
                        Gst.SeekType.SET, seek_query_answer.segment_end)
                else:
                    self.gst_player.seek(
                        self._playback_rate,
                        Gst.Format.TIME,
                        Gst.SeekFlags.FLUSH,
                        Gst.SeekType.SET, position,
                        Gst.SeekType.NONE, -1)
            else:
                if got_seek_query_answer and seek_query_answer.seekable:
                    self.gst_player.seek(
                        self._playback_rate,
                        Gst.Format.TIME,
                        Gst.SeekFlags.FLUSH,
                        Gst.SeekType.SET, seek_query_answer.segment_start,
                        Gst.SeekType.SET, position)
                else:
                    self.gst_player.seek(
                        self._playback_rate,
                        Gst.Format.TIME,
                        Gst.SeekFlags.FLUSH,
                        Gst.SeekType.NONE, -1,
                        Gst.SeekType.NONE, position)

    def dump_gst_message(self, message):
        if message.type == Gst.MessageType.APPLICATION:
            msg_details_str = f"<{PlayerMessages[message.get_structure().get_name()].name}>"
        else:
            msg_details_str = f"<{message.get_structure().to_string() if message.get_structure() else 'None'}>"
        if message.src == None:
            src_str = ''
        else:
            src_str = f" src: {message.src.get_name()}({message.src.__class__.__name__})"
        return f"gst message {message.type.first_value_nick} [{message.get_seqnum()}]: {msg_details_str}{src_str}"

    def log_gst_message(self, message):
        if message.src == None:
            colorfunc = brightmagenta
        elif message.src == self.gst_player:
            colorfunc = brightgreen
        else:
            colorfunc = cyan
        log.debug(colorfunc(self.dump_gst_message(message)))

    def post_player_message(self, player_message):
        message_structure = Gst.Structure.new_empty(player_message.name)
        message = Gst.Message.new_custom(
            Gst.MessageType.APPLICATION,
            None,
            message_structure)
        # mettre le self.bus.post(message) dans un with cond var pour qu'ensuite le wiat de la cond var on soit sur de voir passer le changement d'état
        self.bus.post(message)

    # def seek_blocking(self, seek_event):
    #     ok = self.gst_player.send_event(seek_event)
    #     # if not ok:
    #     #     log.error(f"seek_blocking failed sending {seek_event}")
    #     #     return
    #     seek_seqnum = seek_event.get_seqnum()
    #     log.debug(f"seek_event sequence number {seek_seqnum}")
    #     seek_seqnum = seek_event.get_seqnum()
    #     log.debug(f"seek_event sequence number {seek_seqnum}")
    #     while True:
    #         msg = self.bus.timed_pop(
    #             GST_BLOCKING_WAIT_TIMEOUT)
    #         # msg = self.bus.timed_pop_filtered(
    #         #     GST_BLOCKING_WAIT_TIMEOUT,
    #         #     Gst.MessageType.ASYNC_DONE | Gst.MessageType.ERROR)
    #         if msg:
    #             if msg.type == Gst.MessageType.ASYNC_DONE and msg.get_seqnum() == seek_seqnum:
    #                 break
    #             if msg.type == Gst.MessageType.ERROR:
    #                 log.error(f"seek_blocking got error message {self.dump_gst_message(msg)}")
    #             log.debug(f"discard message {self.dump_gst_message(msg)}")
    #         else:
    #             log.error(f"seek_blocking timeout")

    # version initiale
    # def log_state_machine_error(msg, player_message):
    #     log.error(f"sound player state machine error: current_state={self._player_state} function={inspect.stack()[1][3]} msg={dump_gst_message(msg)} player_message={player_message}")

    def dump_state_machine_args(self, args):
        return f"gst_msg={self.dump_gst_message(args.gst_msg)} player_msg={args.player_msg.name if args.player_msg else args.player_msg}"

    # version coroutine
    def log_state_machine_error(self, args):
        log.error(f"sound player state machine error: current_state={self._player_state} function={inspect.stack()[1][3]} {dump_state_machine_args(args)}")

    # def _async_gst_send_event(self, seek_event):
    #     log.debug(f"_async_gst_send_event seek_event={seek_event}")
    #     ok = self.gst_player.send_event(seek_event)
    #     log.debug(f"_async_gst_send_event event sent")
    #     if ok:
    #         log.debug(f"_async_gst_send_event seek handled immediately")
    #         self.post_player_message(PlayerMessages.SEEK_COMPLETE)
    #     else:
    #         log.debug(f"_async_gst_send_event seek will trigger ASYNC_DONE")
    #     return False
    
    def _unknown_state_transition_handler(self):
        args = yield None
        while args.player_msg != PlayerMessages.ASK_PAUSE:
            args = yield None
        state_change_retval = self.gst_player.set_state(Gst.State.PAUSED)
        if state_change_retval == Gst.StateChangeReturn.FAILURE:
            self.log_state_machine_error(args)
        elif state_change_retval in [ Gst.StateChangeReturn.SUCCESS, Gst.StateChangeReturn.NO_PREROLL ]:
            args = yield PlayerStates.PAUSED
        elif state_change_retval == Gst.StateChangeReturn.ASYNC:
            args = yield None
            while args.gst_msg.type != Gst.MessageType.ASYNC_DONE:
                args = yield None
            args = yield PlayerStates.PAUSED

    def _paused_state_transition_handler(self):
        args = yield None
        while args.player_msg != PlayerMessages.ASK_PLAY:
            args = yield None
        log.debug(f"set gst state to PAUSED")
        state_change_retval = self.gst_player.set_state(Gst.State.PAUSED)
        if state_change_retval == Gst.StateChangeReturn.FAILURE:
            log.debug(f"state change returned {state_change_retval}")
            self.log_state_machine_error(args)
        elif state_change_retval == Gst.StateChangeReturn.ASYNC:
            log.debug(f"state change will trigger ASYNC_DONE, wait for it")
            args = yield None
            while args.gst_msg.type != Gst.MessageType.ASYNC_DONE:
                args = yield None
        got_seek_query_answer, seek_query_answer = query_seek(self.gst_player)
        if got_seek_query_answer and seek_query_answer.seekable:
            seek_event = Gst.Event.new_seek(
                self._playback_rate,
                Gst.Format.TIME,
                Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH,
                Gst.SeekType.SET, seek_query_answer.segment_start,
                Gst.SeekType.SET, seek_query_answer.segment_end)
        else:
            seek_event = Gst.Event.new_seek(
                self._playback_rate,
                Gst.Format.TIME,
                Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH,
                Gst.SeekType.SET, 0,
                Gst.SeekType.NONE, -1)
        log.debug(f"sending seek event: {dump_gst_seek_event(seek_event)}")
        ok = self.gst_player.send_event(seek_event)
        log.debug(f"seek event sent")
        if not ok:
            log.debug(f"seek will trigger ASYNC_DONE, wait for it")
            args = yield None
            while args.gst_msg.type != Gst.MessageType.ASYNC_DONE:
                args = yield None
        else:
            log.debug(f"seek handled immediately")
        # #threading.Thread(target=self._async_gst_send_event, args=(seek_event,)).start()
        # #GLib.idle_add(self._async_gst_send_event, seek_event)
        # self._async_gst_send_event(seek_event)
        # log.debug(f"after async seek")
        # args = yield None
        # while args.gst_msg.type != Gst.MessageType.ASYNC_DONE and args.player_msg != PlayerMessages.SEEK_COMPLETE:
        #     log.debug("waiting")
        #     args = yield None
        # log.debug("seek complete")
        # if not seek_retval:
        #     args = yield None
        #     while args.gst_msg.type != Gst.MessageType.ASYNC_DONE:
        #         args = yield None

        # # log.warning("before sleep")
        # # time.sleep(0.1)
        # # log.warning("after sleep")
        log.debug(f"set gst state to PLAYING")
        state_change_retval = self.gst_player.set_state(Gst.State.PLAYING)
        if state_change_retval in [ Gst.StateChangeReturn.FAILURE, Gst.StateChangeReturn.NO_PREROLL ]:
            log.debug(f"state change returned {state_change_retval}")
            self.log_state_machine_error(args)
        elif state_change_retval == Gst.StateChangeReturn.SUCCESS:
            log.debug(f"state change handled immediately")
            args = yield PlayerStates.PLAYING
        elif state_change_retval == Gst.StateChangeReturn.ASYNC:
            log.debug(f"state change will trigger ASYNC_DONE, wait for it")
            args = yield None
            while args.gst_msg.type != Gst.MessageType.ASYNC_DONE:
                args = yield None
            args = yield PlayerStates.PLAYING

    def _playing_state_transition_handler(self):
        args = yield None
        
        # chaque fois que je change d'état je dois instancier un
        # nouveau générateur qui va être la logique de cet état
        
        # en fait non la coroutine est la logique de changement
        # d'état. En fait soit j'embarque une logique "compliquée" de
        # changement d'état dans les coroutines, soit de crée des
        # états distincts explicites pour cette logique
        
        # exemple: si on est en état PAUSE, recevoir ASK_PLAY doit
        # d'abord faire une gst pause, attendre le ASYNC_DONE, puis
        # faire un gst seek, attendre le ASYNC_DONE, puis
        # éventuellement faire un autre gst seek, attendre le
        # ASYNC_DONE, puis faire un gst play, attendre le ASYNC_DONE
        # et ensuite seulement on est dans l'état play. Et cette
        # transition vers l'état play devrait alors générer une
        # condition ou gérer un sémaphore
        
    # def _unknown_transition(self, msg, player_message):
    #     if player_message == PlayerMessages.ASK_PAUSE:
    #         self.gst_player.set_state(Gst.State.PAUSED)
    #     elif msg.type == Gst.MessageType.ASYNC_DONE:
    #         return PlayerStates.PAUSED
    #     else:
    #         self.log_state_machine_error(msg, player_message)
    #     return PlayerStates.UNKNOWN

    # def _paused_transition(self, msg, player_message):
    #     if player_message == PlayerMessages.ASK_PLAY:
    #         new_player_state = PlayerStates.PAUSED_SEEKING
    #     elif player_message == PlayerMessages.ASK_PLAY:
    #         pass
    #     return new_player_state

    # # def _paused_seeking_transition(self, msg, player_message):
    # #     return None

    # def _playing_transition(self, msg, player_message):
    #     new_player_state = PlayerStates.PAUSED
    #     return new_player_state

    # def _playing_seeking_transition(self, msg, player_message):
    #     pass

    # version coroutines
    _msg_player_state_handlers = {
        PlayerStates.UNKNOWN: (
            (
                Gst.MessageType.APPLICATION,
                Gst.MessageType.ASYNC_DONE
            ),
            _unknown_state_transition_handler
        ),
        PlayerStates.PAUSED: (
            (
                Gst.MessageType.APPLICATION,
                Gst.MessageType.ASYNC_DONE
            ),
            _paused_state_transition_handler
        ),
        PlayerStates.PLAYING: (
            (),
            _playing_state_transition_handler
        )
    }

    # # version initiale
    # _msg_player_state_funcs = {
    #     Gst.MessageType.APPLICATION: {
    #         PlayerStates.UNKNOWN: _unknown_transition,
    #         PlayerStates.PAUSED: _paused_transition,
    #         # PlayerStates.PAUSED_SEEKING: _paused_seeking_transition,
    #         PlayerStates.PLAYING: _playing_transition,
    #         # PlayerStates.PLAYING_SEEKING: _playing_seeking_transition,
    #     },
    #     Gst.MessageType.ASYNC_DONE: {
    #         PlayerStates.UNKNOWN: _unknown_transition,
    #         # PlayerStates.PAUSED: _paused_transition,
    #         # PlayerStates.PAUSED_SEEKING: _paused_seeking_transition,
    #         PlayerStates.PLAYING: _playing_transition,
    #         # PlayerStates.PLAYING_SEEKING: _playing_seeking_transition,
    #     },
    #     Gst.MessageType.SEGMENT_DONE: {
    #     },
    #     Gst.MessageType.EOS: {
    #     },
    # }

    def wait_player_state(self, player_state):
        while self._player_state != player_state:
            with self._player_state_change_cv:
                self._player_state_change_cv.wait()
                # que se passe t-il si par exemple le state passe de
                # paused à paused_seeking, mais que le seeking va
                # tellement vite qu'il repasse immédiatement à paused
                # avant que le wait_player_state soit appelé?

                # ou alors ne pas attendre l'état du player et juste envoyer des events. Mais je risque d'envoyer le seek avant la completion d'autre chose

                # ou alors j'envoie juste un event play et c'est dans le message handler que j'implémente toute la mécanique
            
    def _gst_bus_message_handler(self, bus, message, *user_data):
        self.log_gst_message(message)
        # version coroutines
        if message.src == self.gst_player or message.src == None:
            if message.type in SoundPlayer._msg_player_state_handlers[self._player_state][0]:
                log.debug(brightmagenta(f"player state {self._player_state.name} received {self.dump_gst_message(message)}"))
                if message.type == Gst.MessageType.APPLICATION:
                    player_message = PlayerMessages[message.get_structure().get_name()]
                else:
                    player_message = None
                new_player_state = self._player_state_handler.send(types.SimpleNamespace(gst_msg=message, player_msg=player_message))
                if new_player_state != None and self._player_state != new_player_state:
                    with self._player_state_change_cv:
                        self._player_state = new_player_state
                        self._player_state_handler = SoundPlayer._msg_player_state_handlers[self._player_state][1](self)
                        next(self._player_state_handler)
                        log.debug(brightcyan(f"player state changed to {self._player_state}, state_handler is now {self._player_state_handler}"))
                        self._player_state_change_cv.notify()
                else:
                    log.debug(brightmagenta(f"player state stays {self._player_state}"))
        return True
        
        # # version initiale
        # if message.src == self.gst_player or message.src == None:
        #     if message.type in SoundPlayer._msg_player_state_funcs:
        #         if self._player_state in SoundPlayer._msg_player_state_funcs[message.type]:
        #             log.debug(brightmagenta(f"player state {self._player_state.name} received {self.dump_gst_message(message)}"))
        #             if message.type == Gst.MessageType.APPLICATION:
        #                 player_message = PlayerMessages[message.get_structure().get_name()]
        #             else:
        #                 player_message = None
        #             new_player_state = SoundPlayer._msg_player_state_funcs[message.type][self._player_state](self, message, player_message)
        #             if self._player_state != new_player_state:
        #                 with self._player_state_change_cv:
        #                     self._player_state = new_player_state
        #                     log.debug(brightcyan(f"player state changed to {self._player_state}"))
        #                     self._player_state_change_cv.notify()
        #             else:
        #                 log.debug(brightmagenta(f"player state stays {self._player_state}"))
                    
        # if message.type == Gst.MessageType.SEGMENT_DONE:
        #     self.log_gst_message(message)
        #     if config['play_looped']:
        #         # normal looping when no seeking has been done
        #         got_seek_query_answer, seek_query_answer = query_seek(self.gst_player)
        #         if got_seek_query_answer and seek_query_answer.seekable:
        #             self.gst_player.seek(
        #                 self._playback_rate,
        #                 Gst.Format.TIME,
        #                 Gst.SeekFlags.SEGMENT,
        #                 Gst.SeekType.SET, seek_query_answer.segment_start,
        #                 Gst.SeekType.SET, seek_query_answer.segment_end)
        #         else:
        #             self.gst_player.seek(
        #                 self._playback_rate,
        #                 Gst.Format.TIME,
        #                 Gst.SeekFlags.SEGMENT,
        #                 Gst.SeekType.SET, 0,
        #                 Gst.SeekType.NONE, -1)
        #     # else:
        #     #     self.notify_sound_stopped()
        # elif message.type == Gst.MessageType.EOS:
        #     self.log_gst_message(message)
        #     if config['play_looped']:
        #         # playing looped but a seek was done while playing
        #         # so must do a full restart of the stream
        #         self.gst_player.set_state(Gst.State.PAUSED)
        #         got_seek_query_answer, seek_query_answer = query_seek(self.gst_player)
        #         if got_seek_query_answer and seek_query_answer.seekable:
        #             self.gst_player.seek(
        #                 self._playback_rate,
        #                 Gst.Format.TIME,
        #                 Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH,
        #                 Gst.SeekType.SET, seek_query_answer.segment_start,
        #                 Gst.SeekType.SET, seek_query_answer.segment_end)
        #         else:
        #             self.gst_player.seek(
        #                 self._playback_rate,
        #                 Gst.Format.TIME,
        #                 Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH,
        #                 Gst.SeekType.SET, 0,
        #                 Gst.SeekType.NONE, -1)
        #         self.gst_player.set_state(Gst.State.PLAYING)
        #     else:
        #         pass
        #         #self.notify_sound_stopped()
        # elif message.type == Gst.MessageType.TAG:
        #     message_struct = message.get_structure()
        #     taglist = message.parse_tag()
        #     metadata = parse_tag_list(taglist)
        #     # self.current_sound_playing.update_metadata(metadata)
        #     # self.update_metadata_to_current_playing_message.emit()
        # elif message.type == Gst.MessageType.WARNING:
        #     log.warning(f"Gstreamer WARNING: {message.type}: {message.get_structure().to_string()}")
        # elif message.type == Gst.MessageType.ERROR:
        #     log.warning(f"Gstreamer ERROR: {message.type}: {message.get_structure().to_string()}")
        # elif message.type == Gst.MessageType.ASYNC_DONE:
        #     self.log_gst_message(message)
        # elif message.type == Gst.MessageType.STATE_CHANGED:
        #     if message.src == self.gst_player or log_all_gst_messages:
        #         self.log_gst_message(message)
        # elif log_all_gst_messages:
        #     self.log_gst_message(message)
        return True

    # def seek2(self, seek_event):
    #     self.post_player_message(PlayerMessages.ASK_SEEK)
    #     # prbolème: ce n'est pas modélisable par un état
    #     # si état unkown reçoit ça -> erreur
    #     # si état paused reçoit ça -> attendre un async done
    #     # mais si état paused reçoit un async done suite à un play -> comment distinguer?
    #     # même question pouir étét playing qui reçoit async_done
    #     # conclusion: les états doivent avoir un état, par exemple qu'est-ce que l'état est en train de faire
    #     # si combionaison incorrecte: erreur


    def play(self, from_position=None):
        self.post_player_message(PlayerMessages.ASK_PLAY)
        self.wait_player_state(PlayerStates.PLAYING)
        # time.sleep(5.0)
        # seek_event = Gst.Event.new_seek(
        #     self._playback_rate,
        #     Gst.Format.TIME,
        #     Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH,
        #     Gst.SeekType.SET, 0,
        #     Gst.SeekType.NONE, -1)
        # log.debug(f"before async seek seek_event={seek_event}")
        # #threading.Thread(target=self._async_gst_send_event, args=(seek_event,)).start()
        # GLib.timeout_add(2000, self._async_gst_send_event, seek_event)
        # log.debug(f"after async seek")

        # return
        # log.debug(f"gst play from_position={from_position}")
        # self.post_player_message(PlayerMessages.ASK_PAUSE)
        # self.wait_player_state(PlayerStates.PAUSED)
        # #set_gst_state_blocking(self.gst_player, Gst.State.PAUSED)
        # got_seek_query_answer, seek_query_answer = query_seek(self.gst_player)
        # if got_seek_query_answer and seek_query_answer.seekable:
        #     # seek_event = Gst.Event.new_seek(
        #     #     self._playback_rate,
        #     #     Gst.Format.TIME,
        #     #     Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH,
        #     #     Gst.SeekType.SET, seek_query_answer.segment_start,
        #     #     Gst.SeekType.SET, seek_query_answer.segment_end)
        #     # self.post_player_message(PlayerMessages.ASK_SEEK, seek_event)
        #     # self.seek_blocking(Gst.Event.new_seek(
        #     #     self._playback_rate,
        #     #     Gst.Format.TIME,
        #     #     Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH,
        #     #     Gst.SeekType.SET, seek_query_answer.segment_start,
        #     #     Gst.SeekType.SET, seek_query_answer.segment_end))
        #     self.gst_player.seek(
        #         self._playback_rate,
        #         Gst.Format.TIME,
        #         Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH,
        #         Gst.SeekType.SET, seek_query_answer.segment_start,
        #         Gst.SeekType.SET, seek_query_answer.segment_end)
        # else:
        #     # self.seek_blocking(Gst.Event.new_seek(
        #     #     self._playback_rate,
        #     #     Gst.Format.TIME,
        #     #     Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH,
        #     #     Gst.SeekType.SET, 0,
        #     #     Gst.SeekType.NONE, -1))
        #     self.gst_player.seek(
        #         self._playback_rate,
        #         Gst.Format.TIME,
        #         Gst.SeekFlags.SEGMENT | Gst.SeekFlags.FLUSH,
        #         Gst.SeekType.SET, 0,
        #         Gst.SeekType.NONE, -1)
        # if from_position != None:
        #     self.seek(from_position)
        # import time
        # log.debug("before sleep")
        # time.sleep(0.1)
        # log.debug("after sleep")
        # self.gst_player.set_state(Gst.State.PLAYING)

    def pause(self):
        self.post_player_message(PlayerMessages.ASK_PAUSE)
        self.wait_player_state(PlayerStates.PAUSED)

    # def pause(self):
    #     log.debug(f"gst pause")
    #     self.gst_player.set_state(Gst.State.PAUSED)

    def stop(self):
        log.debug(f"gst stop")
        self.gst_player.set_state(Gst.State.PAUSED)
        got_seek_query_answer, seek_query_answer = query_seek(self.gst_player)
        if got_seek_query_answer and seek_query_answer.seekable:
            self.player.seek(
                self._playback_rate,
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH,
                Gst.SeekType.SET, seek_query_answer.segment_start,
                Gst.SeekType.SET, seek_query_answer.segment_end)
        else:
            self.player.seek(
                self._playback_rate,
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH,
                Gst.SeekType.SET, 0,
                Gst.SeekType.NONE, -1)

    def seek(self, position):
        got_duration, duration = self.player.query_duration(Gst.Format.TIME)
        got_seek_query, seek_query_answer = query_seek(self.player)
        if got_duration:
            seek_pos = position * duration / 100.0
            log.debug(f"seek to {format_duration(seek_pos)}")
            if self._playback_rate > 0.0:
                self.player.seek(
                    self._playback_rate,
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH,
                    Gst.SeekType.SET, seek_pos,
                    Gst.SeekType.SET, seek_query_answer.segment_end)
            else:
                self.player.seek(
                    self._playback_rate,
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH,
                    Gst.SeekType.SET, seek_query_answer.segment_start,
                    Gst.SeekType.NONE, seek_pos)
        else:
            log.warning(f"unable to seek to {position}%, couldn't get duration")

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
