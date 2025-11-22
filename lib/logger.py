import logging, sys, traceback

def log_callstack():
    log.debug(brightmagenta("callstack:\n" + "".join(traceback.format_list(traceback.extract_stack())[:-1])))

def lightwhite(s):
    return '\033[0;2;37m' + s + '\033[0m'

def lightcyan(s):
    return '\033[0;2;36m' + s + '\033[0m'

def lightblue(s):
    return '\033[0;0;34m' + s + '\033[0m'

def lightgreen(s):
    return '\033[38;5;70m' + s + '\033[0m'

def warmyellow(s):
    return '\033[38;5;220m' + s + '\033[0m'

def warmred(s):
    return '\033[38;5;204m' + s + '\033[0m'

def brightmagenta(s):
    return '\033[0;1;95m' + s + '\033[0m'

def brightyellow(s):
    return '\033[0;1;93m' + s + '\033[0m'

def brightgreen(s):
    return '\033[0;1;92m' + s + '\033[0m'

def brightcyan(s):
    return '\033[0;1;96m' + s + '\033[0m'

def brightred(s):
    return '\033[0;1;91m' + s + '\033[0m'

def reversebrightred(s):
    return '\033[0;7;1;91m' + s + '\033[0m'

class CustomFormatter(logging.Formatter):
    detailed_format = "%(asctime)s %(threadName)s %(levelname)s %(filename)s:%(lineno)d %(message)s"
    normal_format = "%(asctime)s %(levelname)s %(message)s"
    log_format = detailed_format
    FORMATTERS = {
        logging.DEBUG: logging.Formatter(lightwhite(log_format)),
        logging.INFO: logging.Formatter(log_format),
        logging.WARNING: logging.Formatter(brightyellow(log_format)),
        logging.ERROR: logging.Formatter(brightred(log_format)),
        logging.CRITICAL: logging.Formatter(reversebrightred(log_format)),
    }
    def format(self, record):
        return self.FORMATTERS.get(record.levelno).format(record)

log = logging.getLogger()
log.log_all_gst_messages = False

def init_logger(debug):
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(CustomFormatter())
    if debug>=1:
        log.setLevel(logging.DEBUG)
    else:
        log.setLevel(logging.INFO)
    log.addHandler(_handler)
    if debug>=2:
        log.log_all_gst_messages = True
    else:
        log.log_all_gst_messages = False
