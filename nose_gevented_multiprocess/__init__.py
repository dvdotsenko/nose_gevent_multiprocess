__author__ = 'Daniel Dotsenko'
__versioninfo__ = (0, 1, 2)
__version__ = '.'.join(map(str, __versioninfo__))

from .nose_gevented_multiprocess import individual_client_starter, GeventedMultiProcess,\
    BaseTaskRunner


__all__ = [
    'individual_client_starter', 'GeventedMultiProcess'
]
