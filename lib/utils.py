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

def _get_milliseconds_suffix(secs):
    ms_suffix = ""
    msecs = int (round(secs - int(secs), 3) * 1000)
    if msecs != 0:
        ms_suffix = ".%03i" % msecs
    return ms_suffix

def format_duration(nsecs, showms=True):
    if nsecs == None:
        return '?'
    secs = nsecs / 1e9
    s = secs
    h = (s - (s % 3600)) // 3600
    s -= h * 3600
    m = (s - (s % 60)) // 60
    s -= m * 60
    formatted_duration = f"{int(h) + ':' if h > 0 else ''}{int(m):02}:{int(s):02}{_get_milliseconds_suffix(s) if showms else ''}"
    return formatted_duration

def split_path_filename(s):
    if os.path.isdir(s):
        return s, None
    elif os.path.isfile(s):
        return os.path.dirname(s), os.path.basename(s)
    else:
        return None, None
