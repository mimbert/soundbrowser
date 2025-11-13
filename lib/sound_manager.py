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
