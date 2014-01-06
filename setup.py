import sys
from setuptools import setup

version = '0.1.2'

long_description = "Gevent-supporting multiprocess plugin for Nose testing framework.\n" + \
"Normal multiprocess plugin relies on specific parts of multiprocess module that break under gevent. "+\
"This specifically applies to multiprocessing.Queue objects that are used to communicate the tests and results "+\
"between master thread and worker threads.\n"+\
"This plugin switches the communication to JSON-RPC requests over HTTP - a medium and the protocol that should "+\
"work under gevent-patched sockets."

project_home = 'http://github.com/dvdotsenko/nose_gevent_multiprocess'

if __name__ == "__main__":
    setup(
        name='nose-gevented-multiprocess',
        description='Gevent-supporting multiprocess plugin for Nose testing framework',
        long_description=long_description,
        version=version,
        author='Daniel Dotsenko',
        author_email='ddotsenko@shift.com',
        url=project_home,
        download_url=project_home+'/tarball/stable',
        classifiers=[
            "Development Status :: 4 - Beta",
            'Intended Audience :: Developers',
            'License :: OSI Approved :: GNU Library or Lesser General Public License (LGPL)',
            'Natural Language :: English',
            'Operating System :: OS Independent',
            'Programming Language :: Python',
            "Programming Language :: Python :: 2.6",
            "Programming Language :: Python :: 2.7",
            'Topic :: Software Development :: Testing'
        ],
        keywords = ['nose', 'multiprocess', 'gevent'],
        license = 'GNU LGPL',
        # py_modules=['nose_gevented_multiprocess'],
        packages=['nose_gevented_multiprocess'],
        install_requires=['requests', 'gevent', 'jsonrpcparts'],
        entry_points={
            'nose.plugins.0.10': [
                'gevented_multiprocess = nose_gevented_multiprocess:GeventedMultiProcess'
            ],
            'console_scripts': [
                'nose_gevented_multiprocess_runner = nose_gevented_multiprocess:individual_client_starter'
            ]
        }
    )

    # Next:
    # python setup.py register
    # python setup.py sdist upload
