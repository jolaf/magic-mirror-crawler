#!/usr/bin/env python3
from datetime import datetime
from hashlib import md5 as dbHash
from os import fdopen, listdir, makedirs, readlink, remove, symlink
from os.path import basename, getmtime, getsize, isdir, isfile, islink, join
from subprocess import Popen, PIPE, STDOUT
from sys import argv, exit, stdout # pylint: disable=W0622
from tempfile import SpooledTemporaryFile
from traceback import format_exc
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

TITLE = '\nMagicMirror v0.01 (c) 2014 Vasily Zakharov vmzakhar@gmail.com\n'

USAGE = '''Usage:
python3 MagicMirror.py crawl databaseDir startURL startURL ...
python3 MagicMirror.py serve databaseDir archiveDomainSuffix [port]
python3 MagicMirror.py test

In crawl mode the script crawls the specified start URLs recursively
and saves the downloaded fiels into the database at specified location.

In serve mode the scripts starts a primitive web server on the specified
port number (80 if not specified), and serves the downloaded data on
domain names with the specified suffix.

Note: all the mirrored domains must be directed to that webserver using
DNS, /etc/hosts or any other means.

Examples:
$ python3 MagicMirror.py crawl /home/User/mmDB http://some.site.com
$ python3 MagicMirror.py crawl /home/User/mmDB https://other.site.com:444
$ python3 MagicMirror.py serve /home/User/mmDB my.archive.com 8080 &
$ wget http://some.site.com.my.archive.com:8080
$ wget http://https.other.site.com.444.my.archive.com:8080
'''

DATA_CHUNK = 10 * 1024 * 1024 # 10 megabytes

TIMESTAMP_FORMAT = '%Y-%m-%d'

HTTP = 'http'
HTTPS = 'https'
FTP = 'ftp'

STANDARD_PORTS = { HTTP: 80, HTTPS: 443, FTP: 21 }

WWW_PREFIX = 'www.'

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

    def listURLs(self):
        raise NotImplementedError

class MagicMirrorFileDatabase(MagicMirrorDatabase):

    URL_DATABASE = 'urls'
    CONTENT_DATABASE = 'data'
    LATEST_LINK = 'latest'
    NUM_FIELDS = 4

    def __init__(self, location):
        MagicMirrorDatabase.__init__(self, location)
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

    def setLocation(self, hostName, timeStamp = None):
        self.mirrorDir = join(self.location, hostName)
        if timeStamp:
            self.targetDir = join(self.mirrorDir, timeStamp)
        else:
            latest = join(self.mirrorDir, self.LATEST_LINK)
            if islink(latest) or isdir(latest):
                self.targetDir = latest
            else:
                self.targetDir = max((join(self.mirrorDir, d) for d in listdir(self.mirrorDir)), key = getmtime, default = self.mirrorDir)
        if timeStamp:
            print(self.targetDir)
        self.contentDatabaseDir = join(self.targetDir, self.CONTENT_DATABASE)
        self.urlDatabaseDir = join(self.targetDir, self.URL_DATABASE)

    def markLatest(self):
        try:
            linkName = join(self.mirrorDir, self.LATEST_LINK)
            if islink(linkName):
                remove(linkName)
            symlink(self.targetDir, linkName)
            print("DONE, set as latest")
        except NotImplementedError:
            print("DONE, linking unsupported")
        except Exception as e:
            print("DONE, error linking: %s" % e)

    def saveURL(self, key, *args):
        assert len(args) == self.NUM_FIELDS
        with open(self.getFileName(self.urlDatabaseDir, key, True), 'w') as f:
            f.write('\n'.join(args + ('',)))

    def loadURL(self, key):
        fileName = self.getFileName(self.urlDatabaseDir, key)
        if isfile(fileName):
            with open(fileName, 'r') as f:
                return tuple(line.strip() for line in f.readlines())[:self.NUM_FIELDS]
        return (None,) * self.NUM_FIELDS

    def saveData(self, key, sourceStream):
        with open(self.getFileName(self.contentDatabaseDir, key, True), 'wb') as f:
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
        return (None, None)

    def listURLs(self):
        ret = []
        for url in listdir(self.location):
            urlDir = join(self.location, url)
            if not isdir(urlDir):
                continue
            latest = join(urlDir, self.LATEST_LINK)
            if islink(latest):
                latest = readlink(latest)
            if isdir(latest) and isdir(join(latest, self.CONTENT_DATABASE)) and isdir(join(latest, self.URL_DATABASE)):
                ret.append((url, basename(latest)))
            else:
                latest = max((join(urlDir, d) for d in listdir(urlDir)), key = getmtime, default = urlDir)
                if isdir(latest) and isdir(join(latest, self.CONTENT_DATABASE)) and isdir(join(latest, self.URL_DATABASE)):
                    ret.append((url, basename(latest)))
        return tuple(sorted(ret))

class MagicMirror(object):
    def __init__(self, databaseLocation, mirrorSuffix = None, databaseClass = MagicMirrorFileDatabase):
        self.database = databaseClass(databaseLocation)
        self.mirrorSuffix = mirrorSuffix

    @staticmethod
    def dataHash(data):
        """Returns a hexlified hash digest for the specified block of data or already existing hash object."""
        return (data if hasattr(data, 'digest') else dbHash(data.encode('utf-8'))).hexdigest()

    @staticmethod
    def parseURL(url):
        """Normalizes the specified URL and returns (scheme, hostName, port, path, query, fragment) tuple.
        scheme is converted to lower case.
        Username and password information is dropped.
        hostName is converted to lower case, www. prefix is removed if it exists.
        If specified port is default for the specified scheme, it's set to None.
        """
        splitURL = urlsplit(url)
        (scheme, _netloc, path, query, fragment) = splitURL
        scheme = scheme.lower()
        hostName = splitURL.hostname or '' # always lower case
        if hostName.startswith(WWW_PREFIX):
            hostName = hostName[len(WWW_PREFIX):]
        port = splitURL.port
        if port and port == STANDARD_PORTS.get(scheme):
            port = None
        return (scheme, hostName, port, path, query, fragment)

    @staticmethod
    def unparseURL(scheme, netloc, path, query, fragment):
        """Joins URL components together.
        In case of no query and no fragment, resulting trailing slash is removed.
        """
        url = urlunsplit((scheme, netloc, path, query, fragment))
        return url if query or fragment or not url.endswith('/') else url[:-1]

    @classmethod
    def getUrlHash(cls, scheme, netloc, path, query, fragment):
        """Returns URL hash for the specified URL parameters."""
        return cls.dataHash(cls.unparseURL(scheme, netloc, path, query, fragment))

    @classmethod
    def processHostName(cls, url):
        """Returns mirror host name for the specified url, without mirror suffix.
        The resulting host name is [scheme.]host.name[.port], all lower case.
        Scheme is omitted if it's http, port is omitted if it's default for the scheme.
        www. prefix is removed if it exists.
        """
        (scheme, hostName, port, _path, _query, _fragment) = cls.parseURL(url)
        return '.'.join(((scheme,) if scheme and scheme != HTTP else ()) + (hostName,) + ((str(port),) if port else ()))

    @classmethod
    def processOriginalURL(cls, url):
        """Returns URL hash for the specified original URL.
        The URL itself is normalized as follows to provide consistent hash values for equivalent URLs.
        Scheme is converted to lower case, and, if it's not http, added to the beginning of the host name.
        The scheme of normalized URL is always http.
        Username and password, if specified, are dropped.
        Host name is converted to lower case, www. prefix is removed if it exists.
        Port is dropped, if it's default for the scheme.
        ? is dropped if no parameters are specified.
        # is dropped if no fragment is specified.
        Trailing / is dropped if there's no parameters or fragment.
        For example, URL HTTPS://username:password@www.Some.Host.com:443/some/path?#
        is normalized to http://https.some.host.com/some/path
        """
        (scheme, hostName, port, path, query, fragment) = cls.parseURL(url)
        netloc = ''.join((('%s.' % scheme) if scheme != HTTP else '', hostName, (':%d' % port) if port else ''))
        return cls.getUrlHash(HTTP, netloc, path, query, fragment)

    def processMirrorURL(self, host, path):
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
        assert self.mirrorSuffix
        (_scheme, hostName, _port, path, query, fragment) = self.parseURL(HTTP + '://' + host + path)
        if not hostName.endswith(self.mirrorSuffix.lower()):
            return (None, None)
        hostName = hostName[:-len(self.mirrorSuffix) - 1]
        tokens = hostName.split('.')
        if tokens[-1].isdigit():
            netloc = '%s:%s' % ('.'.join(tokens[:-1]), tokens[-1])
        else:
            netloc = hostName
        return (hostName, self.getUrlHash(HTTP, netloc, path, query, fragment))

    def downloadURL(self, url):
        try:
            print(url, end = ' ')
            request = requests.get(url, stream = True)
            contentType = request.headers['content-type']
            contentLength = request.headers.get('content-length', '')
            print(':: %s :: %s ::' % (contentType, ('%s bytes' % contentLength) if contentLength else 'no content-length'), end = ' ')
            tempHash = dbHash()
            with SpooledTemporaryFile(DATA_CHUNK) as tempFile:
                for chunk in request.iter_content(DATA_CHUNK):
                    tempFile.write(chunk)
                    tempHash.update(chunk)
                if contentLength:
                    contentLength = int(contentLength)
                    assert contentLength == tempFile.tell()
                else:
                    contentLength = tempFile.tell()
                    assert contentLength
                    print("%d bytes ::" % contentLength, end = ' ')
                contentHash = self.dataHash(tempHash)
                (dataSize, _dataStream) = self.database.loadData(contentHash)
                if dataSize == contentLength:
                    print("exists, match", end = ' ')
                else:
                    if dataSize:
                        print("DAMAGED, OVERWRITING", end = ' ')
                        print()
                        print(contentHash)
                        raise BaseException('BOOM')
                    else:
                        print("new, saving", end = ' ')
                    tempFile.seek(0)
                    written = self.database.saveData(contentHash, tempFile)
                    assert written == contentLength
            print("OK")
            urlHash = self.processOriginalURL(url)
            (oldURL, oldContentType, oldContentLength, oldContentHash) = self.database.loadURL(urlHash)
            if oldURL:
                print("Repeated URL %s :: %s :: %s bytes :: content %s" % (oldURL, oldContentType, oldContentLength, 'matches' if contentHash == oldContentHash else 'DIFFERENT'))
            else:
                self.database.saveURL(urlHash, url, contentType, str(contentLength), contentHash)
        except Exception as e:
            print("\nERROR: %s" % e)
            print(format_exc())
            raise

    @staticmethod
    def test():
        magicMirror = MagicMirror('', mirrorSuffix = 'my.archive.com')
        # dataHash
        assert magicMirror.dataHash('abcd') == 'e2fc714c4727ee9395f324cd2e7f331f'
        # processHostName
        assert magicMirror.processHostName('Http://Some.Host.com') == 'some.host.com'
        assert magicMirror.processHostName('hTtps://wWw.SOME.HOST.COM') == 'https.some.host.com'
        assert magicMirror.processHostName('htTp://Some.Host.com:80') == 'some.host.com'
        assert magicMirror.processHostName('httP://Some.Host.com:8080') == 'some.host.com.8080'
        assert magicMirror.processHostName('httpS://Some.Host.com:443') == 'https.some.host.com'
        assert magicMirror.processHostName('HTTPS://wWw.Some.Host.com:8443') == 'https.some.host.com.8443'
        # processOriginalURL
        assert magicMirror.processOriginalURL('http://wWw.Some.Host.com') == magicMirror.dataHash('http://some.host.com')
        assert magicMirror.processOriginalURL('HTTPS://SOME.HOST.COM') == magicMirror.dataHash('http://https.some.host.com')
        assert magicMirror.processOriginalURL('Https://userName@Some.Host.com') == magicMirror.dataHash('http://https.some.host.com')
        assert magicMirror.processOriginalURL('Https://userName:password@wWw.somE.hosT.COM') == magicMirror.dataHash('http://https.some.host.com')
        assert magicMirror.processOriginalURL('HTTPS://SOME.HOST.COM:443') == magicMirror.dataHash('http://https.some.host.com')
        assert magicMirror.processOriginalURL('HTTPS://SOME.HOST.COM:8443') == magicMirror.dataHash('http://https.some.host.com:8443')
        assert magicMirror.processOriginalURL('http://wWw.Some.Host.com/?') == magicMirror.dataHash('http://some.host.com')
        assert magicMirror.processOriginalURL('http://wWw.Some.Host.com/#') == magicMirror.dataHash('http://some.host.com')
        assert magicMirror.processOriginalURL('http://wWw.Some.Host.com/?#') == magicMirror.dataHash('http://some.host.com')
        assert magicMirror.processOriginalURL('http://wWw.Some.Host.com/?#') == magicMirror.dataHash('http://some.host.com')
        assert magicMirror.processOriginalURL('http://wWw.Some.Host.com/?#') == magicMirror.dataHash('http://some.host.com')
        assert magicMirror.processOriginalURL('HTTPS://username:password@www.Some.Host.com:443/some/path?#') == magicMirror.dataHash('http://https.some.host.com/some/path')
        assert magicMirror.processOriginalURL('HTTPS://username:password@www.Some.Host.com:443/some/path?abc=def&klm=nop#') == magicMirror.dataHash('http://https.some.host.com/some/path?abc=def&klm=nop')
        assert magicMirror.processOriginalURL('HTTPS://username:password@www.Some.Host.com:443/some/path?abc=def&klm=nop#fig25') == magicMirror.dataHash('http://https.some.host.com/some/path?abc=def&klm=nop#fig25')
        # processMirrorURL
        assert magicMirror.processMirrorURL('wWw.Some.Host.com.my.archive.com', '/') == ('some.host.com', magicMirror.dataHash('http://some.host.com'))
        assert magicMirror.processMirrorURL('SOME.HOST.COM.my.archive.com', '/') == ('some.host.com', magicMirror.dataHash('http://some.host.com'))
        assert magicMirror.processMirrorURL('Some.Host.com.My.Archive.com', '/') == ('some.host.com', magicMirror.dataHash('http://some.host.com'))
        assert magicMirror.processMirrorURL('wWw.somE.hosT.COM.my.archive.com', '/') == ('some.host.com', magicMirror.dataHash('http://some.host.com'))
        assert magicMirror.processMirrorURL('SOME.HOST.COM.My.Archive.com:443', '/') == ('some.host.com', magicMirror.dataHash('http://some.host.com'))
        assert magicMirror.processMirrorURL('SOME.HOST.COM.8443.my.archive.com', '/') == ('some.host.com.8443', magicMirror.dataHash('http://some.host.com:8443'))
        assert magicMirror.processMirrorURL('https.Some.Host.com.My.Archive.com', '/?') == ('https.some.host.com', magicMirror.dataHash('http://https.some.host.com'))
        assert magicMirror.processMirrorURL('FTP.Some.Host.com.my.archive.com', '/#') == ('ftp.some.host.com', magicMirror.dataHash('http://ftp.some.host.com'))
        assert magicMirror.processMirrorURL('wWw.FTP.Some.Host.com.My.Archive.com', '/?#') == ('ftp.some.host.com', magicMirror.dataHash('http://ftp.some.host.com'))
        assert magicMirror.processMirrorURL('wWw.Some.Host.com.8080.my.archive.com', '/?#') == ('some.host.com.8080', magicMirror.dataHash('http://some.host.com:8080'))
        assert magicMirror.processMirrorURL('wWw.Some.Host.com.My.Archive.com', '/?#') == ('some.host.com', magicMirror.dataHash('http://some.host.com'))
        assert magicMirror.processMirrorURL('www.Some.Host.com.443.my.archive.com:8080', '/some/path?#') == ('some.host.com.443', magicMirror.dataHash('http://some.host.com:443/some/path'))
        assert magicMirror.processMirrorURL('www.FTP.Some.Host.com.My.Archive.com:443', '/some/path?abc=def&klm=nop#') == ('ftp.some.host.com', magicMirror.dataHash('http://ftp.some.host.com/some/path?abc=def&klm=nop'))
        assert magicMirror.processMirrorURL('www.Some.Host.com.my.archive.com:443', '/some/path?abc=def&klm=nop#fig25') == ('some.host.com', magicMirror.dataHash('http://some.host.com/some/path?abc=def&klm=nop#fig25'))
        print("OK")
        return 0

WGET_ARGS = ('wget', '-r', '-l', 'inf', '-nd', '--spider', '--delete-after')
WGET_URL_PREFIX = b'--'
def wgetUrlSource(sourceURL): # generator
    wget = Popen(WGET_ARGS + (sourceURL,), stdout = PIPE, stderr = STDOUT)
    for urlBytes in (line.split()[-1] for line in (line.strip() for line in wget.stdout) if line.startswith(WGET_URL_PREFIX)):
        try:
            yield urlBytes.decode('utf-8')#, error = 'replace') # ToDo
        except Exception as e:
            print("ERROR decoding URL %r: %s" % (urlBytes, e))
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
        print('\n%s ->' % sourceURL, end = ' ')
        self.database.setLocation(self.processHostName(sourceURL), timeStamp)
        urlCache = set()
        try:
            for url in self.urlSource(sourceURL):
                if url not in urlCache:
                    self.downloadURL(url)
                    urlCache.add(url)
            self.database.markLatest()
        except Exception as e:
            print("ERROR:", e)
            print(format_exc())

    def run(self, sourceURLs):
        for sourceURL in sourceURLs:
            self.crawl(sourceURL)

class MagicMirrorServer(MagicMirror):
    def __init__(self, databaseLocation, mirrorSuffix):
        MagicMirror.__init__(self, databaseLocation, mirrorSuffix)

    def serve(self, host, path):
        (hostName, urlHash) = self.processMirrorURL(host, path)
        if hostName:
            self.database.setLocation(hostName)
            (url, contentType, contentLength, contentHash) = self.database.loadURL(urlHash)
            if url:
                contentLength = int(contentLength)
                (contentSize, contentStream) = self.database.loadData(contentHash)
                assert contentSize == contentLength
                return (url, contentType, contentLength, contentStream)
        return (None, None, None, None)

class MirrorHTTPRequestHandler(BaseHTTPRequestHandler):
    INDEX_PAGE = '''
<html>
<head>
<title>Magic Mirror</title>
</head>
<body>
<h1>Magic Mirror</h1>
<p>
Available mirrored sites are:
</p>
<p>
%s
</p>
</body>
</html>
'''
    magicMirrorServer = None

    @classmethod
    def configure(cls, databaseLocation, mirrorSuffix, port = None):
        cls.magicMirrorServer = MagicMirrorServer(databaseLocation, mirrorSuffix)
        cls.port = port

    def send404(self):
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b'404')

    def do_GET(self):
        host = self.headers['host']
        print(host)
        if host.split(':')[0] == self.magicMirrorServer.mirrorSuffix or not host.split(':')[0].endswith(self.magicMirrorServer.mirrorSuffix):
            content = self.INDEX_PAGE % '<br>'.join(('<a href="http://%s.%s%s">%s</a> (%s)' % (url, self.magicMirrorServer.mirrorSuffix, ':%s' % self.port if self.port else '', url, date)) for (url, date) in self.magicMirrorServer.database.listURLs())
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content.encode('utf-8'))
            return
        (url, contentType, contentLength, contentStream) = self.magicMirrorServer.serve(host, self.path) # ToDo: Log the headers data
        if url:
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
        else:
            self.send404()

def usage():
    print(USAGE)

def main(args):
    print(TITLE)
    # ToDo: employ getopt for proper option handling
    if args:
        command = args[0].lower()
        parameters = args[1:]
        if command == 'test':
            exit(MagicMirror.test())
        elif command == 'crawl':
            exit(1 if MagicMirrorCrawler(parameters[0]).run(parameters[1:]) else 0)
        elif command == 'serve':
            MirrorHTTPRequestHandler.configure(*parameters[:3])
            parameters = parameters[2:]
            HTTPServer(('', int(parameters[0]) if parameters else 80), MirrorHTTPRequestHandler).serve_forever()
            exit(1)
    usage()

if __name__ == '__main__':
    main(argv[1:])
