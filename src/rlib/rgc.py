try:
    from rpython.rlib.rgc import collect  # pylint: disable=unused-import
except ImportError:
    "NOT_RPYTHON"

    def collect():
        pass
