#!/usr/bin/env python3

# Copyright (c) 2022 Matthieu Imbert
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

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gst', '1.0')
import argparse, os.path
from lib.logger import init_logger, log
from lib.config import load_conf
from lib.sound import init_sound
from lib.ui import start_ui

DEFAULT_CONF_FILE = os.path.expanduser("~/.soundbrowser.conf.yaml")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Sound Browser')
    parser.add_argument('-d', '--debug', action='count', default=0, help='enable debug output')
    parser.add_argument('-c', '--conf_file', type=str, default=DEFAULT_CONF_FILE, help=f'use alternate conf file (default={DEFAULT_CONF_FILE})')
    parser.add_argument('startup_path', nargs='?', help='open this path')
    args = parser.parse_args()
    init_logger(args.debug)
    load_conf(args.conf_file)
    init_sound()
    start_ui(args.startup_path)

    # from lib.sound import SoundPlayer
    # from gi.repository import GObject, Gst, GLib
    # import threading, time
    # mainloop = GLib.MainLoop()
    # threading.Thread(target=mainloop.run).start()
    # player = SoundPlayer()
    # audio_output_config = player.configure_audio_output('', None)
    # log.debug(f"sound output config: {audio_output_config}")
    # #player.set_path("/home/mimbert/devel/soundbrowser.git/bass_drum.wav")
    # #player.set_path("/home/mimbert/devel/soundbrowser.git/bass_drum_reverb.wav")
    # player.set_path("/home/mimbert/music/staging/albums/R. Kelly - Double Up/08 I'm a Flirt (remix).mp3")
    # import time
    # #time.sleep(0.1)
    # while True:
    #     player.play()
    #     time.sleep(2)
    #     player.pause()
    #     time.sleep(2)

    #     #player.set_path("/home/mimbert/devel/soundbrowser.git/bass_drum.wav")
    #     #player.set_path("/home/mimbert/devel/soundbrowser.git/bass_drum_reverb.wav")
    #     #player.set_path("/home/mimbert/music/staging/tracks-mixing/01 01A1 Untitled.mp3")

