Set of scripts to mirror web sites and serve the mirrored copies.

### Requirements ###

  * [Python](http://python.org) v3.3 or later
  * [python3-requests](http://pypi.python.org/pypi/requests/) v2.2.1 or later
  * [wget](http://www.gnu.org/software/wget/) v1.15 or later

### Installing on Ubuntu ###

  * `sudo apt-get install wget python3-pip`
  * `sudo pip3 install requests`
  * [Checkout](https://code.google.com/p/magic-mirror-crawler/source/checkout) or [download](https://magic-mirror-crawler.googlecode.com/git/MagicMirror.py) the latest version of `MagicMirror.py`

### Installing on Windows ###

  * Install the latest [Python 3.x](http://python.org/download/).
  * (recommended) Add `C:\Python3x\Scripts` (check the actual path on your system) to your PATH.
  * `pip3 install requests`
  * Install [wget](https://eternallybored.org/misc/wget/) v1.15 or later, add it to PATH.
  * [Checkout](https://code.google.com/p/magic-mirror-crawler/source/checkout) or [download](https://magic-mirror-crawler.googlecode.com/git/MagicMirror.py) the latest version of `MagicMirror.py`

### Crawling a web site ###

`$ python3 MagicMirror.py crawl databaseDir startURL [additionalURL additionalURL ...]`

Specifying additional URLs (they may even be on a different domain) may be necessary when `wget` used as a web crawler fails to detect links to those files &ndash; in most cases it produces code 404 page or missing images while browsing the mirrored copy of the site.

For example:

`$ python3 MagicMirror.py crawl /home/User/mmDB http://some.site.com http://some.site.com/print.shtml?smth`

`$ python3 MagicMirror.py crawl /home/User/mmDB https://other.site.com:444`

### Serving mirrored web sites ###

`$ python3 MagicMirror.py serve databaseDir archive.Domain.Suffix [port] &`

For example:

`$ python3 MagicMirror.py serve /home/User/mmDB my.archive.com 8080 &`

Make sure DNS or `/etc/hosts` or whatever domain naming system points `archive.Domain.Suffix` and `*.archive.Domain.Suffix` to the server IP address.

### Accessing mirrored web sites ###

`$ wget http://my.archive.com:8080`

`$ wget http://some.site.com.my.archive.com:8080`

`$ wget http://https.other.site.com.444.my.archive.com:8080`

### Running built-in test suite ###

`$ python3 MagicMirror.py test`

-- Moved from https://code.google.com/p/magic-mirror-crawler
