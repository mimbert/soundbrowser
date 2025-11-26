import schema, yaml, os.path
from lib.logger import log

STARTUP_PATH_MODE_SPECIFIED_PATH = 1
STARTUP_PATH_MODE_LAST_PATH = 2
STARTUP_PATH_MODE_CURRENT_DIR = 3
STARTUP_PATH_MODE_HOME_DIR = 4

config = {}
_config_path = None

conf_schema = schema.Schema({
    schema.Optional('startup_path_mode', default=STARTUP_PATH_MODE_HOME_DIR): int,
    schema.Optional('specified_path', default=os.path.expanduser('~')): str,
    schema.Optional('last_path', default=os.path.expanduser('~')): str,
    schema.Optional('show_hidden_files', default=False): bool,
    schema.Optional('show_metadata_pane', default=True): bool,
    schema.Optional('autoplay_mouse', default=True): bool,
    schema.Optional('autoplay_keyboard', default=False): bool,
    schema.Optional('main_window_geometry', default=None): bytes,
    schema.Optional('main_window_state', default=None): bytes,
    schema.Optional('treeview_state', default=None): [str],
    schema.Optional('splitter_state', default=None): bytes,
    schema.Optional('play_looped', default=False): bool,
    schema.Optional('hide_tune', default=True): bool,
    schema.Optional('reset_tune_between_sounds', default=True): bool,
    schema.Optional('file_extensions_filter', default=['wav', 'mp3', 'aiff', 'flac', 'ogg', 'm4a', 'aac', 'wma', 'aiff', 'ape', 'wv', 'mpc', 'au', 's3m', 'xm', 'mod', 'it', 'dbm', 'mid' ]): [str],
    schema.Optional('filter_files', default=True): bool,
    schema.Optional('gst_audio_sink', default=''): str,
    schema.Optional('gst_audio_sink_properties', default={}): {schema.Optional(str): {schema.Optional(str): str}},
    schema.Optional('dark_theme', default=False): bool,
})

def load_conf(path):
    global config
    global _config_path
    _config_path = path
    log.debug(f"loading conf from {_config_path}")
    try:
        with open(_config_path) as fh:
            tmp_conf = yaml.safe_load(fh)
    except OSError:
        log.debug(f"error reading conf from {_config_path}, using an empty conf")
        tmp_conf = {}
    config.clear()
    config.update(conf_schema.validate(tmp_conf))

def save_conf():
    global config
    config = conf_schema.validate(config)
    log.debug(f"saving conf to {_config_path}")
    try:
        with open(_config_path, 'w') as fh:
            yaml.dump(config, fh)
    except OSError:
        log.debug(f"unable to save conf to {_config_path}")
