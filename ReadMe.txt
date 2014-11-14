Install on Ubuntu:

sudo apt-get install python-pip
sudo pip install python-requests

To install on Windows:
- Install the latest Python 2.x: http://python.org/download/ (use Windows x86 MSI Installer even on 64-bit systems)
- Install the latest PIP: http://www.lfd.uci.edu/~gohlke/pythonlibs/#pip (use win32 version for your version of Python)
- Install the latest pycurl: http://www.lfd.uci.edu/~gohlke/pythonlibs/#pycurl (use win32 version for your version of Python)
- (recommended) Add C:\Python2x\Scripts (check the actual path on your system) to your PATH.
- Run pip install requests

To crawl and mirrow web sites, run:

python MirrorCrawler.py crawl [-d databaseDirectory] mirror.suffix source.url [souce.url ...]

To serve the mirroe with a minimal HTTP web server, run:

python MirrorCrawler.py serve [-d databaseDirectory] [port]

To run built-in test suite:

python MirrorCrawler.py test
