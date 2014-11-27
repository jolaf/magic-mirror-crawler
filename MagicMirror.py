#!/usr/bin/env python3
from datetime import datetime
from gzip import GzipFile
from hashlib import md5 as dbHash
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import BytesIO
from os import listdir, makedirs, readlink, remove, symlink
from os.path import basename, getsize, isdir, isfile, islink, join
from subprocess import Popen, PIPE, STDOUT
from sys import argv, exit # pylint: disable=W0622
from tempfile import SpooledTemporaryFile
from traceback import format_exc
from urllib.parse import urlsplit, urlunsplit

try: # Requests HTTP library
    import requests
    if requests.__version__.split('.') < ['2', '2', '1']:
        raise ImportError('Requests version %s < 2.2.1' % requests.__version__)
except ImportError as ex:
    print("%s: %s\nERROR: This software requires Requests.\nPlease install Requests v2.3.0 or later: https://pypi.python.org/pypi/requests" % (ex.__class__.__name__, ex))
    exit(-1)

# ToDo: Localize links
# ToDo: Think about remote pictures and pages (redirects)
# ToDo: Find out why wget skips certain addresses
# ToDo: Use Tornado as web engine
# ToDo: Use Tornado as asynchronous retriever
# ToDo: Employ getopt for proper option handling
# ToDo: Employ proper logging
# ToDo: Use config file for logging, DB and suffix settings

TITLE = '\nMagicMirror v0.03 (c) 2014 Vasily Zakharov vmzakhar@gmail.com\n'

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

    def saveURL(self, key, *args):
        raise NotImplementedError

    def loadURL(self, key):
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
        self.targetDir = join(self.mirrorDir, timeStamp if timeStamp else self.LATEST_LINK)
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
            if isdir(latest):
                ret.append((url, basename(latest)))
        return tuple(sorted(ret))

class MagicMirror(object):
    ZERO_HASH = '0'

    GZIP_SUFFIX = '.gz'
    MIN_SIZE_FOR_GZIP = 513
    MIN_GZIP_EFFECTIVENESS = 90

    def __init__(self, databaseLocation, mirrorSuffix = None, databaseClass = MagicMirrorFileDatabase):
        self.database = databaseClass(databaseLocation)
        self.mirrorSuffix = mirrorSuffix

    @staticmethod
    def dataHash(data):
        """Returns a hexlified hash digest for the specified block of data or already existing hash object."""
        ret = (data if hasattr(data, 'digest') else dbHash(data.encode('utf-8'))).hexdigest()
        assert len(ret) == 2 * dbHash().digest_size # pylint: disable=E1101
        return ret

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
        if not scheme:
            (scheme, hostName, port, _path, _query, _fragment) = cls.parseURL(HTTP + '://' + url)
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
            print(url, end = ' ', flush = True)
            request = requests.get(url, stream = True)
            contentType = request.headers['content-type']
            contentLength = request.headers.get('content-length', '')
            print(':: %s :: %s ::' % (contentType, ('%s bytes' % contentLength) if contentLength else 'no content-length'), end = ' ', flush = True)
            tempHash = dbHash()
            with SpooledTemporaryFile(DATA_CHUNK) as tempFile:
                for chunk in request.iter_content(DATA_CHUNK):
                    tempFile.write(chunk)
                    tempHash.update(chunk)
                size = tempFile.tell()
                if contentLength:
                    if size != int(contentLength):
                        print("ACTUALLY %d bytes ::" % size, end = ' ', flush = True)
                else:
                    print("%d bytes ::" % size, end = ' ', flush = True)
                contentLength = size
                if contentLength:
                    contentHash = self.dataHash(tempHash)
                    (dataSize, _dataStream) = self.database.loadData(contentHash)
                    if contentLength == dataSize:
                        print("exists, match", end = ' ', flush = True)
                    else:
                        print("DAMAGED, OVERWRITING" if dataSize else "new, saving", end = ' ', flush = True)
                        gzipped = False
                        if contentLength >= self.MIN_SIZE_FOR_GZIP:
                            tempFile.seek(0)
                            with SpooledTemporaryFile(DATA_CHUNK) as gzipFile:
                                with GzipFile(contentHash, 'wb', fileobj = gzipFile) as gzip:
                                    while True:
                                        data = tempFile.read(DATA_CHUNK)
                                        if not data:
                                            break
                                        gzip.write(data)
                                zipLength = gzipFile.tell()
                                if zipLength * 100 < contentLength * self.MIN_GZIP_EFFECTIVENESS:
                                    contentHash += self.GZIP_SUFFIX
                                    gzipFile.seek(0)
                                    written = self.database.saveData(contentHash, gzipFile)
                                    assert written == zipLength
                                    gzipped = True
                        if not gzipped:
                            tempFile.seek(0)
                            written = self.database.saveData(contentHash, tempFile)
                            assert written == contentLength
                else:
                    contentHash = self.ZERO_HASH
            print("OK")
            urlHash = self.processOriginalURL(url)
            (oldURL, oldContentType, oldContentLength, oldContentHash) = self.database.loadURL(urlHash)
            if oldURL:
                print("Previous URL %s :: %s :: %s bytes :: content %s" % (oldURL, oldContentType, oldContentLength, 'matches' if contentHash == oldContentHash else 'DIFFERENT'))
                if oldContentHash != self.ZERO_HASH or contentHash == self.ZERO_HASH:
                    return
                print("Previous URL contained empty page, overwriting")
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
            yield urlBytes.decode('utf-8')
        except Exception as e:
            print("ERROR decoding URL %r: %s" % (urlBytes, e))
    if wget.poll() is None:
        print("Terminating...")
        wget.wait()
    if wget.returncode: # ToDo: What if really bad problem occurs?
        print("WARNING: wget error %d" % wget.returncode)

class MagicMirrorCrawler(MagicMirror):
    ROBOTS_TXT = 'robots.txt'

    def __init__(self, databaseLocation, urlSource = wgetUrlSource):
        MagicMirror.__init__(self, databaseLocation)
        self.urlSource = urlSource # generator

    def crawl(self, sourceURL):
        timeStamp = datetime.now().strftime(TIMESTAMP_FORMAT)
        print('%s ->' % sourceURL, end = ' ', flush = True)
        self.database.setLocation(self.processHostName(sourceURL), timeStamp)
        urlCache = set()
        try:
            for url in self.urlSource(sourceURL):
                if url not in urlCache and not url.endswith(self.ROBOTS_TXT):
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
    TYPES_TO_PROCESS = set()#'text/html', 'text/css', 'text/javascript')
    def __init__(self, databaseLocation, mirrorSuffix):
        MagicMirror.__init__(self, databaseLocation, mirrorSuffix)

    def processContent(self, hostName, content):
        return content # ToDo

    def serve(self, host, path):
        (hostName, urlHash) = self.processMirrorURL(host, path)
        if hostName:
            self.database.setLocation(hostName)
            (url, contentType, contentLength, contentHash) = self.database.loadURL(urlHash)
            if url:
                contentLength = int(contentLength)
                if contentLength == 0:
                    assert contentHash == self.ZERO_HASH
                    return (url, contentType, 0, None)
                else:
                    gzipped = contentHash.endswith(self.GZIP_SUFFIX)
                    assert len(contentHash) == 2 * dbHash().digest_size + len(self.GZIP_SUFFIX) * gzipped # pylint: disable=E1101
                    (contentSize, contentStream) = self.database.loadData(contentHash)
                    if gzipped:
                        assert contentSize < contentLength
                        contentStream = GzipFile(fileobj = contentStream)
                    else:
                        assert contentSize == contentLength
                    if contentLength < DATA_CHUNK and contentType.lower() in self.TYPES_TO_PROCESS:
                        contentStream = BytesIO(self.processContent(hostName, contentStream.read()))
                    return (url, contentType, contentLength, contentStream)
        return (None, None, None, None)

class MirrorHTTPRequestHandler(BaseHTTPRequestHandler):
    ENCODING = 'utf-8'

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
<hr>
</body>
</html>
'''
    NO_URLS = '<em>(sorry, no sites have been mirrored yet)</em>'

    EMPTY_PAGE = '''
<html>
<head>
<title>Magic Mirror</title>
</head>
<body>
<h1>Magic Mirror</h1>
<p><strong>Sorry, there was an empty page at this address at the original web-site.</strong></p>
<p>
Original URL: <a href="{0}"><code>{0}</code></a>
<br>
Content-Type: <code>{1}</code>
</p>
<p>
Original web site: <a href="{2}"><code>{2}</code></a>
<br>Mirrored copy root: <a href="http://{3}"><code>http://{3}</code></a>
</p>
<p>Magic Mirror root: <a href="http://{4}{5}"><code>http://{4}{5}</code></a></p>
<p>Magic Mirror project: <a href="https://code.google.com/p/magic-mirror-crawler"><code>https://code.google.com/p/magic-mirror-crawler</code></p>
<hr>
</body>
</html>
'''

    NOT_FOUND_PAGE = '''
<html>
<head>
<title>404: Not Found :: Magic Mirror</title>
</head>
<body>
<h1>Magic Mirror</h1>
<p><strong>404: Not found. Sorry, this page is not present in the mirror.</strong></p>
<p>Magic Mirror root: <a href="http://{0}{1}"><code>http://{0}{1}</code></a></p>
<p>Magic Mirror project: <a href="https://code.google.com/p/magic-mirror-crawler"><code>https://code.google.com/p/magic-mirror-crawler</code></p>
<hr>
</body>
</html>
'''
    magicMirrorServer = None

    @classmethod
    def configure(cls, databaseLocation, mirrorSuffix, port = None):
        cls.magicMirrorServer = MagicMirrorServer(databaseLocation, mirrorSuffix)
        cls.port = int(port) if port else None

    def do_GET(self):
        host = self.headers['host']
        print(host)
        if host.split(':')[0] == self.magicMirrorServer.mirrorSuffix or not host.split(':')[0].endswith(self.magicMirrorServer.mirrorSuffix): # root page
            mirroredURLs = self.magicMirrorServer.database.listURLs()
            if mirroredURLs:
                htmlURLs = '<br>'.join(('<a href="http://%s.%s%s">%s</a> (%s)' % (url, self.magicMirrorServer.mirrorSuffix, ':%s' % self.port if self.port else '', url, date)) for (url, date) in mirroredURLs)
            else:
                htmlURLs = self.NO_URLS
            content = self.INDEX_PAGE % htmlURLs
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content.encode(self.ENCODING))
            return
        (url, contentType, contentLength, contentStream) = self.magicMirrorServer.serve(host, self.path) # ToDo: Log the headers data
        if url: # page found
            self.send_response(200)
            if contentLength:
                self.send_header('Content-Type', contentType)
                self.send_header('Content-Length', contentLength)
                self.end_headers()
                while True:
                    data = contentStream.read(DATA_CHUNK)
                    if not data:
                        break
                    self.wfile.write(data)
                assert contentStream.tell() == contentLength
                return
            else: # empty page
                content = self.EMPTY_PAGE.format(url, contentType, '://'.join(urlsplit(url)[:2]), host, self.magicMirrorServer.mirrorSuffix, ':%d' % self.port if self.port else '')
        else: # page not found
            self.send_response(404)
            content = self.NOT_FOUND_PAGE.format(self.magicMirrorServer.mirrorSuffix, ':%d' % self.port if self.port else '')
        self.send_header('Content-Type', 'text/html; charset=%s' % self.ENCODING)
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content.encode(self.ENCODING))

def usage():
    print(USAGE)

def main(args):
    print(TITLE)
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
