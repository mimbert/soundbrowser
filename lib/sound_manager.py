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

import os, os.path
from lib.utils import LRU
from lib.logger import log

CACHE_SIZE = 256

class Sound():

    def __init__(self, path, stat_result):
        super().__init__()
        log.debug(f"new sound path={path} stat={stat_result}")
        self.metadata = { None: {}, 'all': {} }
        self.path = path
        self.stat_result = stat_result

    def __str__(self):
        return f"\"{self.path}\""

    def update_metadata(self, metadata):
        for container_format in metadata:
            if not container_format in self.metadata:
                self.metadata[container_format] = {}
            self.metadata[container_format].update(metadata[container_format])
            self.metadata['all'].update(metadata[container_format])

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
