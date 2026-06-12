import sys


class Tee:
    """Duplicate sys.stdout writes to a log file.

    Usage:
        with Tee(path):
            print(...)   # goes to both terminal and log file
    """

    def __init__(self, path):
        self._file   = open(path, 'w')
        self._stdout = sys.stdout
        sys.stdout   = self

    def write(self, data):
        self._stdout.write(data)
        self._file.write(data)

    def flush(self):
        self._stdout.flush()
        self._file.flush()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        sys.stdout = self._stdout
        self._file.close()
