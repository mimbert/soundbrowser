import logging, sys, traceback

def log_callstack():
    log.debug(brightmagenta("callstack:\n" + "".join(traceback.format_list(traceback.extract_stack())[:-1])))

def cyan(s):
    return '\033[36m' + s + '\033[m'

def brightgreen(s):
    return '\033[92m' + s + '\033[m'

def brightmagenta(s):
    return '\033[95m' + s + '\033[m'

def brightcyan(s):
    return '\033[96m' + s + '\033[m'


class CustomFormatter(logging.Formatter):
    grey = '\033[2m\033[37m'
    brightyellow = '\033[93m'
    brightred = '\033[91m'
    reversebrightboldred = '\033[7m\033[1m\033[91m'
    reset = '\033[m'
    detailed_format = "%(asctime)s %(threadName)s %(levelname)s %(filename)s:%(lineno)d %(message)s"
    normal_format = "%(asctime)s %(levelname)s %(message)s"
    log_format = detailed_format
    FORMATTERS = {
        logging.DEBUG: logging.Formatter(grey + log_format + reset),
        logging.INFO: logging.Formatter(log_format),
        logging.WARNING: logging.Formatter(brightyellow + log_format + reset),
        logging.ERROR: logging.Formatter(brightred + log_format + reset),
        logging.CRITICAL: logging.Formatter(reversebrightboldred + log_format + reset),
    }
    def format(self, record):
        return self.FORMATTERS.get(record.levelno).format(record)

log = logging.getLogger()

def init_logger(debug):
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(CustomFormatter())
    if debug:
        log.setLevel(logging.DEBUG)
    else:
        log.setLevel(logging.INFO)
    log.addHandler(_handler)
