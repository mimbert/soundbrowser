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

import os.path, collections
from lib.logger import log

def mybreakpoint():
    import ipdb
    ipdb.set_trace()

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

def _get_centiseconds_suffix(secs):
    csecs = int (round(secs - int(secs), 2) * 100)
    cs_suffix = ".%02i" % csecs
    return cs_suffix

def format_duration(nsecs, showcs=True):
    if nsecs == None:
        return '?'
    secs = nsecs / 1e9
    s = secs
    h = (s - (s % 3600)) // 3600
    s -= h * 3600
    m = (s - (s % 60)) // 60
    s -= m * 60
    if h == 0 and m == 0:
        formatted_duration = f"{int(s):02}{_get_centiseconds_suffix(s)}"
    elif h == 0:
        formatted_duration = f"{m:02}:{int(s):02}{_get_centiseconds_suffix(s) if showcs else ''}"
    else:
        formatted_duration = f"{h}:{m:02}:{int(s):02}{_get_centiseconds_suffix(s) if showcs else ''}"
    return formatted_duration

def split_path_filename(s):
    if os.path.isdir(s):
        return s, None
    elif os.path.isfile(s):
        return os.path.dirname(s), os.path.basename(s)
    else:
        return None, None
