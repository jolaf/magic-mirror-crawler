#!/usr/bin/python
from datetime import datetime
from getopt import getopt
from hashlib import md5 as dbHash
from os import fdopen, makedirs, remove
from os.path import getsize, isdir, isfile, islink, join
from subprocess import Popen, PIPE, STDOUT
from sys import argv, exit, stdout # pylint: disable=W0622
from tempfile import SpooledTemporaryFile
from urllib.parse import urlsplit, urlunsplit

from http.server import HTTPServer, BaseHTTPRequestHandler

stdout = fdopen(stdout.fileno(), 'wb', 0)

try: # Requests HTTP library
    import requests
    if requests.__version__.split('.') < ['2', '3', '0']:
        raise ImportError('Requests version %s < 2.3.0' % requests.__version__)
except ImportError as ex:
    print("%s: %s\nERROR: This software requires Requests.\nPlease install Requests v2.3.0 or later: https://pypi.python.org/pypi/requests" % (ex.__class__.__name__, ex))
    exit(-1)

try: # Filesystem symbolic links configuration
    from os import symlink # UNIX # pylint: disable=E0611
except ImportError:
    try:
        from ctypes import windll
        dll = windll.LoadLibrary('kernel32.dll')
        def symlink(source, linkName):
            if not dll.CreateSymbolicLinkW(linkName, source, int(isdir(source))):
                raise OSError("code %d" % dll.GetLastError())
    except Exception as ex:
        symlink = None
        print("%s: %s\nWARNING: Filesystem links will not be available.\nPlease run on UNIX or Windows Vista or later with NTFS.\n" % (ex.__class__.__name__, ex))

DATA_CHUNK = 10 * 1024 * 1024 # 10 megabytes

TIMESTAMP_FORMAT = '%Y-%m-%d'

HTTP = 'http'
HTTPS = 'https'
FTP = 'ftp'

STANDARD_PORTS = { HTTP: 80, HTTPS: 443, FTP: 21 }

WWW_PREFIX = 'www.'

def dataHash(data):
    """Returns a hexlified hash digest for the specified block of data or already existing hash object."""
    return (data if hasattr(data, 'digest') else dbHash(data.encode('utf-8'))).hexdigest()

def parseURL(url, retainHostNameCase = False):
    """Normalizes the specified URL and returns (scheme, userName, hostName, port, path, query, fragment) tuple.
    scheme is converted to lower case.
    If userName is not specified, None is returned.
    hostName is converted to lower case if lowerHostName is True, www. prefix is removed if it exists.
    If specified port is default for the specified scheme, it's set to None.
    """
    splitURL = urlsplit(url)
    (scheme, netloc, path, query, fragment) = splitURL
    scheme = scheme.lower()
    userName = splitURL.username
    hostName = splitURL.hostname or '' # already lower case
    if retainHostNameCase:
        index = netloc.lower().index(hostName)
        hostName = netloc[index : index + len(hostName)]
    if hostName.lower().startswith(WWW_PREFIX):
        hostName = hostName[len(WWW_PREFIX):]
    port = splitURL.port
    if port and port == STANDARD_PORTS.get(scheme):
        port = None
    return (scheme, userName, hostName, port, path, query, fragment)

def unparseURL(scheme, netloc, path, query, fragment):
    """Joins URL components together.
    In case of no query and no fragment, resulting trailing slash is removed.
    """
    url = urlunsplit((scheme, netloc, path, query, fragment))
    return url if query or fragment or not url.endswith('/') else url[:-1]

def processHostName(url):
    """Returns mirror host name for the specified url, without mirror suffix.
    The resulting host name is [scheme.]host.name[.port]
    Scheme is converted to lower case and omitted if it's http, port is omitted if it's default for the scheme.
    www. prefix is removed if it exists.
    Host names case is not altered.
    """
    (scheme, _userName, hostName, port, _path, _query, _fragment) = parseURL(url, True)
    return '.'.join(((scheme,) if scheme != HTTP else ()) + (hostName,) + ((str(port),) if port else ()))

def getUrlHash(scheme, netloc, path, query, fragment):
    """Returns URL hash for the specified URL parameters."""
    return dataHash(unparseURL(scheme, netloc, path, query, fragment))

def processOriginalURL(url): # ToDo: Rename to originalUrlHash
    """Returns URL hash for the specified original URL.
    The URL itself is normalized as follows to provide consistent hash values for equivalent URLs.
    Scheme is converted to lower case, and, if it's not http, added to the beginning of the host name.
    The scheme of normalized URL is always http.
    Username and password are dropped, if specified.
    Host name is converted to lower case, www. prefix is removed if it exists.
    Port is dropped, if it's default for the scheme.
    ? is dropped if no parameters are specified.
    # is dropped if no fragment is specified.
    Trailing / is dropped if there's no parameters or fragment.
    For example, URL HTTPS://username:password@www.Some.Host.com:443/some/path?#
    is normalized to http://username@https.some.host.com/some/path
    """
    (scheme, _userName, hostName, port, path, query, fragment) = parseURL(url)
    netloc = ''.join((('%s.' % scheme) if scheme != HTTP else '', hostName, (':%d' % port) if port else ''))
    return getUrlHash(HTTP, netloc, path, query, fragment)

def processMirrorURL(host, path, mirrorSuffix): # ToDo: Rename to mirrorUrlHash
    """Returns URL hash for the specified mirror URL and mirrorSuffix of the particular mirror site.
    The URL host name MUST end with .mirrorSuffix (case insensitive).
    The URL itself is normalized as follows to provide consistent hash values for equivalent URLs.
    The URL scheme and port are ignored.
    Password is dropped, if specified.
    Host name is converted to lower case, www. prefix is removed if it exists, mirrorSuffix is also removed.
    If the last token of the remaining host name is digital, it's conveted to port number.
    Port is dropped, if it's default for the scheme.
    ? is dropped if no parameters are specified.
    # is dropped if no fragment is specified.
    Trailing / is dropped if there's no parameters or fragment.
    For example, URL https://username:password@https.Some.Host.com.8443.my.archive.com:8080/some/path?#
    is normalized to http://https.some.host.com:8443/some/path
    """
    assert mirrorSuffix
    (_scheme, _userName, hostName, _port, path, query, fragment) = parseURL('http://' + host + path)
    assert hostName.endswith(mirrorSuffix.lower())
    hostName = hostName[:-len(mirrorSuffix) - 1]
    tokens = hostName.split('.')
    netloc = '%s:%s' % ('.'.join(tokens[:-1]), tokens[-1]) if tokens[-1].isdigit() else hostName
    return getUrlHash(HTTP, netloc, path, query, fragment)

class MagicMirrorDatabase(object): # abstract
    """Database access interface."""
    def __init__(self, location):
        self.location = location

    def setLocation(self, hostName, timeStamp):
        raise NotImplementedError

    def markLatest(self):
        raise NotImplementedError

    def saveURL(self, key, url, contentType, contentLength, contentHash):
        raise NotImplementedError

    def loadURL(self, key):
        # return (url, contentType, contentLength, contentHash)
        raise NotImplementedError

    def saveData(self, key, sourceStream):
        raise NotImplementedError

    def loadData(self, key):
        raise NotImplementedError

    def getDataSize(self, key):
        raise NotImplementedError

class MagicMirrorFileDatabase(MagicMirrorDatabase):

    URL_DATABASE = 'urls'
    CONTENT_DATABASE = 'data'
    LATEST_LINK = 'latest'

    def __init__(self, location):
        MagicMirrorDatabase.__init__(location)
        self.mirrorDir = None
        self.targetDir = None
        self.contentDatabaseDir = None
        self.urlDatabaseDir = None

    @staticmethod
    def getFileName(targetDirName, hashFileName, createDirs = False):
        dirName = join(targetDirName, hashFileName[:2], hashFileName[2:4])
        if createDirs and not isdir(dirName):
            makedirs(dirName)
        return join(dirName, hashFileName)

    def setLocation(self, hostName, timeStamp):
        self.mirrorDir = join(self.location, hostName)
        self.targetDir = join(self.mirrorDir, timeStamp)
        print(self.targetDir)
        self.contentDatabaseDir = join(self.targetDir, self.CONTENT_DATABASE)
        self.urlDatabaseDir = join(self.targetDir, self.URL_DATABASE)

    def markLatest(self):
        if symlink:
            try:
                linkName = join(self.mirrorDir, self.LATEST_LINK)
                if islink(linkName):
                    remove(linkName)
                    symlink(self.targetDir, linkName)
                    print("DONE, set as latest")
            except Exception as e:
                print("DONE, error linking: %s" % e)
        else:
            print("DONE, linking unsupported")

    def saveURL(self, key, *args):
        with open(self.getFileName(self.urlDatabaseDir, key), 'wb') as f:
            f.write('\n'.join(args + ('',)))

    def loadURL(self, key):
        fileName = self.getFileName(self.urlDatabaseDir, key)
        if isfile(fileName):
            with open(fileName, 'rb') as f:
                return tuple(line.strip() for line in f.readlines())

    def saveData(self, key, sourceStream):
        with open(self.getFileName(self.contentDatabaseDir, key), 'wb') as f:
            while True:
                data = sourceStream.read(DATA_CHUNK)
                if not data:
                    break
                f.write(data)
            return f.tell()

    def loadData(self, key):
        fileName = self.getFileName(self.contentDatabaseDir, key)
        if isfile(fileName):
            return (getsize(fileName), open(fileName, 'rb'))
        return None

    def getDataSize(self, key):
        fileName = self.getFileName(self.contentDatabaseDir, key)
        return getsize(fileName) if isfile(fileName) else None

class MagicMirror(object):
    def __init__(self, databaseLocation, databaseClass = MagicMirrorFileDatabase):
        self.database = databaseClass(databaseLocation)

    def downloadURL(self, url):
        print(url, end = ' ')
        try:
            request = requests.get(url, stream = True)
            request.headers['blah-test'] # ToDo: test, remove
            contentType = request.headers['content-type']
            contentLength = request.headers['content-length']
            print(contentType, contentLength, end=' ')
            tempHash = dbHash()
            with SpooledTemporaryFile(DATA_CHUNK) as tempFile:
                while True:
                    data = request.content.read(DATA_CHUNK)
                    if not data:
                        break
                    tempFile.write(data)
                    tempHash.update(data)
                if contentLength:
                    assert contentLength == tempFile.tell() # ToDo: May it fail actually?
                else:
                    contentLength = tempFile.tell()
                contentHash = dataHash(tempHash)
                dataSize = self.database.getDataSize(contentHash)
                if dataSize == contentLength:
                    print("exists, match", end=' ')
                else:
                    if dataSize:
                        print("DAMAGED, OVERWRITING", end=' ')
                    else:
                        print("saving", end=' ')
                    tempFile.seek(0)
                    written = self.database.saveData(contentHash, tempFile)
                    assert written == contentLength
            print("OK")
            urlHash = processOriginalURL(url)
            urlInfo = self.database.loadURL(urlHash)
            if urlInfo:
                (oldURL, oldContentType, oldContentLength, oldContentHash) = urlInfo # pylint: disable=W0633
                # ToDo: Do something better with this
                print("Overwriting URL %s %s %s, content %s" % (oldURL, oldContentType, oldContentLength, 'matches' if contentHash == oldContentHash else 'MISMATCHES'))
            self.database.saveURL(urlHash, url, contentType, contentLength, contentHash)
        except Exception as e:
            print("\nERROR: %s" % e)

    @staticmethod
    def test():
        # dataHash
        dataHash('abcd')
        assert dataHash('abcd') == 'e2fc714c4727ee9395f324cd2e7f331f'
        # processHostName
        assert processHostName('Http://Some.Host.com') == 'Some.Host.com'
        assert processHostName('hTtps://wWw.SOME.HOST.COM') == 'https.SOME.HOST.COM'
        assert processHostName('htTp://Some.Host.com:80') == 'Some.Host.com'
        assert processHostName('httP://Some.Host.com:8080') == 'Some.Host.com.8080'
        assert processHostName('httpS://Some.Host.com:443') == 'https.Some.Host.com'
        assert processHostName('HTTPS://wWw.Some.Host.com:8443') == 'https.Some.Host.com.8443'
        # processOriginalURL
        assert processOriginalURL('http://wWw.Some.Host.com') == dataHash('http://some.host.com')
        assert processOriginalURL('HTTPS://SOME.HOST.COM') == dataHash('http://https.some.host.com')
        assert processOriginalURL('Https://userName@Some.Host.com') == dataHash('http://https.some.host.com')
        assert processOriginalURL('Https://userName:password@wWw.somE.hosT.COM') == dataHash('http://https.some.host.com')
        assert processOriginalURL('HTTPS://SOME.HOST.COM:443') == dataHash('http://https.some.host.com')
        assert processOriginalURL('HTTPS://SOME.HOST.COM:8443') == dataHash('http://https.some.host.com:8443')
        assert processOriginalURL('http://wWw.Some.Host.com/?') == dataHash('http://some.host.com')
        assert processOriginalURL('http://wWw.Some.Host.com/#') == dataHash('http://some.host.com')
        assert processOriginalURL('http://wWw.Some.Host.com/?#') == dataHash('http://some.host.com')
        assert processOriginalURL('http://wWw.Some.Host.com/?#') == dataHash('http://some.host.com')
        assert processOriginalURL('http://wWw.Some.Host.com/?#') == dataHash('http://some.host.com')
        assert processOriginalURL('HTTPS://username:password@www.Some.Host.com:443/some/path?#') == dataHash('http://https.some.host.com/some/path')
        assert processOriginalURL('HTTPS://username:password@www.Some.Host.com:443/some/path?abc=def&klm=nop#') == dataHash('http://https.some.host.com/some/path?abc=def&klm=nop')
        assert processOriginalURL('HTTPS://username:password@www.Some.Host.com:443/some/path?abc=def&klm=nop#fig25') == dataHash('http://https.some.host.com/some/path?abc=def&klm=nop#fig25')
        # processMirrorURL
        assert processMirrorURL('wWw.Some.Host.com.my.archive.com', '/', 'My.Archive.com') == dataHash('http://some.host.com')
        assert processMirrorURL('SOME.HOST.COM.my.archive.com', '/', 'My.Archive.com') == dataHash('http://some.host.com')
        assert processMirrorURL('Some.Host.com.My.Archive.com', '/', 'my.archive.com') == dataHash('http://some.host.com')
        assert processMirrorURL('wWw.somE.hosT.COM.my.archive.com', '/', 'My.Archive.com') == dataHash('http://some.host.com')
        assert processMirrorURL('SOME.HOST.COM.My.Archive.com:443', '/', 'my.archive.com') == dataHash('http://some.host.com')
        assert processMirrorURL('SOME.HOST.COM.8443.my.archive.com', '/', 'My.Archive.com') == dataHash('http://some.host.com:8443')
        assert processMirrorURL('https.Some.Host.com.My.Archive.com', '/?', 'my.archive.com') == dataHash('http://https.some.host.com')
        assert processMirrorURL('FTP.Some.Host.com.my.archive.com', '/#', 'My.Archive.com') == dataHash('http://ftp.some.host.com')
        assert processMirrorURL('wWw.FTP.Some.Host.com.My.Archive.com', '/?#', 'my.archive.com') == dataHash('http://ftp.some.host.com')
        assert processMirrorURL('wWw.Some.Host.com.8080.my.archive.com', '/?#', 'My.Archive.com') == dataHash('http://some.host.com:8080')
        assert processMirrorURL('wWw.Some.Host.com.My.Archive.com', '/?#', 'my.archive.com') == dataHash('http://some.host.com')
        assert processMirrorURL('www.Some.Host.com.443.my.archive.com:8080', '/some/path?#', 'My.Archive.com') == dataHash('http://some.host.com:443/some/path')
        assert processMirrorURL('www.FTP.Some.Host.com.My.Archive.com:443', '/some/path?abc=def&klm=nop#', 'my.archive.com') == dataHash('http://ftp.some.host.com/some/path?abc=def&klm=nop')
        assert processMirrorURL('www.Some.Host.com.my.archive.com:443', '/some/path?abc=def&klm=nop#fig25', 'My.Archive.com') == dataHash('http://some.host.com/some/path?abc=def&klm=nop#fig25')
        print("OK")
        return 0

def wgetUrlSource(sourceURL): # generator
    WGET_ARGS = ('wget', '-r', '-l', 'inf', '-nd', '--spider', '--delete-after')
    WGET_URL_PREFIX = '--'
    wget = Popen(WGET_ARGS + (sourceURL,), stdout = PIPE, stderr = STDOUT)
    for url in (line.split()[-1] for line in (line.strip() for line in wget.stdout) if line.startswith(WGET_URL_PREFIX)):
        yield url
    if wget.poll() is None:
        print("Terminating...")
        wget.wait()
    if wget.returncode:
        raise Exception("wget error: %d" % wget.returncode)

class MagicMirrorCrawler(MagicMirror):
    def __init__(self, databaseLocation, urlSource = wgetUrlSource):
        MagicMirror.__init__(self, databaseLocation)
        self.urlSource = urlSource # generator

    def crawl(self, sourceURL):
        timeStamp = datetime.now().strftime(TIMESTAMP_FORMAT)
        print('\n%s -> ,' % sourceURL)
        self.database.setLocation(processHostName(sourceURL), timeStamp)
        try:
            for url in self.urlSource(sourceURL):
                self.downloadURL(url)
            self.database.markLatest()
        except Exception as e:
            print("ERROR:", e)

    def run(self, sourceURLs):
        for sourceURL in sourceURLs:
            self.crawl(sourceURL)

class MagicMirrorServer(MagicMirror):
    def __init__(self, databaseLocation, mirrorSuffix):
        MagicMirror.__init__(self, databaseLocation)
        self.mirrorSuffix = mirrorSuffix

    def serve(self, host, path):
        urlHash = processMirrorURL(host, path, self.mirrorSuffix)
        if not urlHash:
            return None
        urlInfo = self.database.loadURL(urlHash)
        if urlInfo:
            (url, contentType, contentLength, contentHash) = urlInfo
            (contentSize, contentStream) = self.database.loadData(contentHash)
            assert contentSize == contentLength
            return (url, contentType, contentLength, contentStream)

class MirrorHTTPRequestHandler(BaseHTTPRequestHandler):
    magicMirrorServer = None

    @classmethod
    def configure(cls, databaseLocation, mirrorSuffix):
        cls.magicMirrorServer = MagicMirrorServer(databaseLocation, mirrorSuffix)

    def send404(self):
        self.send_response(404)
        self.end_headers()
        self.wfile.write('404')

    def do_GET(self):
        urlInfo = self.magicMirrorServer.serve(self.headers['host'], self.path) # ToDo: Log the headers data
        if not urlInfo:
            self.send404()
        (_url, contentType, contentLength, contentStream) = urlInfo
        with contentStream:
            self.send_response(200)
            self.send_header('Content-Type', contentType)
            self.send_header('Content-Length', contentLength)
            self.end_headers()
            while True:
                data = contentStream.read(DATA_CHUNK)
                if not data:
                    break
                self.wfile.write(data)
            assert contentStream.tell() == contentLength

def usage():
    pass

def main(args):
    # ToDo: employ getopt for proper option handling
    if args:
        command = args[0].lower()
        parameters = args[1:]
        if command == 'test':
            exit(MagicMirror.test())
        elif command == 'crawl':
            exit(1 if MagicMirrorCrawler(parameters[0]).run(parameters[1:]) else 0)
        elif command == 'serve':
            HTTPServer(('', int(parameters[0]) if parameters else 80), MirrorHTTPRequestHandler).serve_forever()
            exit(1)
    usage()

if __name__ == '__main__':
    main(argv[1:])
