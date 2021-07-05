# -*- coding: utf-8 -*-
from __future__ import (absolute_import, division,
                        print_function, unicode_literals)
from builtins import *
"""Hanterar domslut (detaljer och referat) från Domstolsverket. Data
hämtas fran DV:s (ickepublika) FTP-server, eller fran lagen.nu."""

# system libraries (incl six-based renames)
from bz2 import BZ2File
from collections import defaultdict
from datetime import datetime, timedelta, date
from ftplib import FTP
from io import BytesIO
from time import mktime
from urllib.parse import urljoin, urlparse
import codecs
import itertools
import logging
import os
import re
import tempfile
import zipfile

# 3rdparty libs
from ferenda.requesthandler import UnderscoreConverter
from cached_property import cached_property
from rdflib import Namespace, URIRef, Graph, RDF, RDFS, BNode
from rdflib.namespace import DCTERMS, SKOS, FOAF
import requests
import lxml.html
from lxml import etree
from bs4 import BeautifulSoup, NavigableString


# my libs
from ferenda import (Document, DocumentStore, Describer, WordReader, FSMParser, Facet)
from ferenda.decorators import newstate, action
from ferenda import util, errors, fulltextindex
from ferenda.sources.legal.se.legalref import LegalRef
from ferenda.elements import (Body, Paragraph, CompoundElement, OrdinalElement,
                              Heading, Link)

from ferenda.elements.html import Strong, Em, Div, P
from . import SwedishLegalSource, SwedishCitationParser, RPUBL
from .elements import *
from .swedishlegalsource import SwedishLegalHandler


PROV = Namespace(util.ns['prov'])

class DVConverterBase(UnderscoreConverter):
    regex = "[^/].*?"
    repo = None  # we create a subclass of this at runtime, when we have access to the repo object
    # this converter translates "nja/2015s180" -> "HDO/Ö6229-14"
    # because this might be an appropriate place to do so in the
    # werkzeug routing system
    def to_python(self, value):
        return self.repo.basefile_from_uri("%s%s/%s" % (self.repo.config.url, self.repo.urispace_segment, value))
        # return value.replace("_", " ")

    # and maybe vice versa (not super important)
    def to_url(self, value):
        return value

    

class DVHandler(SwedishLegalHandler):

    
    @property
    def rule_context(self):
        return {"converter": "dv"}

    @property
    def rule_converters(self):
        class DVConverter(DVConverterBase):
            repo = self.repo
        return (("dv", DVConverter),)


class DVStore(DocumentStore):

    """Customized DocumentStore.
    """
    downloaded_suffixes = [".docx", ".doc"]
    
    def basefile_to_pathfrag(self, basefile):
        return basefile

    def pathfrag_to_basefile(self, pathfrag):
        return pathfrag


class KeywordContainsDescription(errors.FerendaException):
    def __init__(self, keywords, descriptions):
        self.keywords = keywords
        self.descriptions = descriptions

class DuplicateReferatDoc(errors.DocumentRemovedError):
    pass

        
class DV(SwedishLegalSource):

    """Handles legal cases, in report form, from primarily final instance courts.

    Cases are fetched from Domstolsverkets FTP server for "Vägledande
    avgöranden", and are converted from doc/docx format.

    """
    requesthandler_class = DVHandler
    alias = "dv"
    downloaded_suffix = ".zip"
    rdf_type = (RPUBL.Rattsfallsreferat, RPUBL.Rattsfallsnotis)
    documentstore_class = DVStore
    # This is very similar to SwedishLegalSource.required_predicates,
    # only DCTERMS.title has been changed to RPUBL.referatrubrik (and if
    # our validating function grokked that rpubl:referatrubrik
    # rdfs:isSubpropertyOf dcterms:title, we wouldn't need this). Also, we
    # removed dcterms:issued because there is no actual way of getting
    # this data (apart from like the file time stamps).  On further
    # thinking, we remove RPUBL.referatrubrik as it's not present (or
    # required) for rpubl:Rattsfallsnotis
    required_predicates = [RDF.type, DCTERMS.identifier, PROV.wasGeneratedBy]

    DCTERMS = Namespace(util.ns['dcterms'])
    sparql_annotations = "sparql/dv-annotations.rq"
    sparql_expect_results = False
    xslt_template = "xsl/dv.xsl"

    @classmethod
    def relate_all_setup(cls, config, *args, **kwargs):
        # FIXME: If this was an instancemethod, we could use
        # self.store methods instead
        parsed_dir = os.path.sep.join([config.datadir, 'dv', 'parsed'])
        mapfile = os.path.sep.join(
            [config.datadir, 'dv', 'generated', 'uri.map'])
        log = logging.getLogger(cls.alias)
        if (not util.outfile_is_newer(util.list_dirs(parsed_dir, ".xhtml"), mapfile)) or config.force:
            re_xmlbase = re.compile('<head about="([^"]+)"')
            log.info("Creating uri.map file")
            cnt = 0
            # also remove any uri-<client>-<pid>.map files that might be laying around
            for m in util.list_dirs(os.path.dirname(mapfile), ".map"):
                if m == mapfile:
                    continue
                util.robust_remove(m)
            util.robust_remove(mapfile + ".new")
            util.ensure_dir(mapfile)
            # FIXME: Not sure utf-8 is the correct codec for us -- it
            # might be iso-8859-1 (it's to be used by mod_rewrite).
            with codecs.open(mapfile + ".new", "w", encoding="utf-8") as fp:
                paths = set()
                for f in util.list_dirs(parsed_dir, ".xhtml"):
                    if not os.path.getsize(f):
                        # skip empty files
                        continue
                    # get basefile from f in the simplest way
                    basefile = f[len(parsed_dir) + 1:-6]
                    head = codecs.open(f, encoding='utf-8').read(1024)
                    m = re_xmlbase.search(head)
                    if m:
                        path = urlparse(m.group(1)).path
                        if path in paths:
                            log.warning("Path %s is already in map" % path)
                            continue
                        assert path
                        assert basefile
                        if config.mapfiletype == "nginx":
                            fp.write("%s\t/dv/generated/%s.html;\n" % (path, basefile))
                        else:
                            # remove prefix "/dom/" from path
                            path = path.replace("/%s/" % cls.urispace_segment, "", 1)
                            fp.write("%s\t%s\n" % (path, basefile))
                        cnt += 1
                        paths.add(path)
                    else:
                        log.warning(
                            "%s: Could not find valid head[@about] in %s" %
                            (basefile, f))
            util.robust_rename(mapfile + ".new", mapfile)
            log.info("uri.map created, %s entries" % cnt)
        else:
            log.debug("Not regenerating uri.map")
            pass
        return super(DV, cls).relate_all_setup(config, *args, **kwargs)

    # def relate(self, basefile, otherrepos): pass
    @classmethod
    def get_default_options(cls):
        opts = super(DV, cls).get_default_options()
        opts['ftpuser'] = ''  # None  # Doesn't work great since Defaults is a typesource...
        opts['ftppassword'] = ''  # None
        opts['mapfiletype'] = 'apache' # or nginx
        return opts

    def canonical_uri(self, basefile, version=None):
        # The canonical URI for HDO/B3811-03 should be
        # https://lagen.nu/dom/nja/2004s510. We can't know
        # this URI before we parse the document. Once we have, we can
        # find the first rdf:type = rpubl:Rattsfallsreferat (or
        # rpubl:Rattsfallsnotis) and get its url.
        #
        # FIXME: It would be simpler and faster to read
        # DocumentEntry(self.store.entry_path(basefile))['id'], but
        # parse does not yet update the DocumentEntry once it has the
        # canonical uri/id for the document.
        p = self.store.distilled_path(basefile)
        if not os.path.exists(p):
            raise ValueError("No distilled file for basefile %s at %s" % (basefile, p))

        with self.store.open_distilled(basefile) as fp:
            g = Graph().parse(data=fp.read())
        for uri, rdftype in g.subject_objects(predicate=RDF.type):
            if rdftype in (RPUBL.Rattsfallsreferat,
                           RPUBL.Rattsfallsnotis):
                return str(uri)
        raise ValueError("Can't find canonical URI for basefile %s in %s" % (basefile, p))

    # we override make_document to avoid having it calling
    # canonical_uri prematurely
    def make_document(self, basefile=None, version=None):
        doc = Document()
        doc.basefile = basefile
        doc.meta = self.make_graph()
        doc.lang = self.lang
        doc.body = Body()
        doc.uri = None  # can't know this yet
        return doc

    urispace_segment = "rf"


    expected_cases = {"ra":  1993,
                      "nja": 1981,
                      "rh":  1993,
                      "ad":  1993,
                      "mod": 1999,
                      "md":  2004}

    # override to account for the fact that there is no 1:1
    # correspondance between basefiles and uris
    def basefile_from_uri(self, uri):
        def build_basefilemap(path, filename):
            if self.config.mapfiletype == "nginx":
                # chop of leading "/dom/"
                path = path[len(self.urispace_segment)+2:]
                # /dv/generated/HDO/T-254.html; => HDO/T-254
                filename = filename[14:-6]
            self._basefilemap[path] = filename
        
        basefile = super(DV, self).basefile_from_uri(uri)
        # the "basefile" at this point is just the remainder of the
        # URI (eg "nja/1995s362"). Create a lookup table to find the
        # real basefile (eg "HDO/Ö463-95_1")
        if basefile:
            if not hasattr(self, "_basefilemap"):
                self._basefilemap = {}
                self.readmapfile(build_basefilemap)
            if basefile in self._basefilemap:
                return self._basefilemap[basefile]
            else:
                # this will happen for older cases for which we don't
                # have any files. We invent URI-derived basefiles for
                # these to gain a sort of skeleton entry for those,
                # which we can use to track eg. frequently referenced
                # older cases.

                # however, we check if we OUGHT to have a basefile
                # (because it's recent enough) and warn.
                court, year = basefile.split("/", 1)
                year=int(year[:4])
                if court not in self.expected_cases or self.expected_cases[court] <= year:
                    self.log.warning("%s: Could not find corresponding basefile" % uri)
                return basefile.replace(":", "/")

    def readmapfile(self, callback):
        mapfile = self.store.path("uri", "generated", ".map")
        util.ensure_dir(mapfile)
        if self.config.clientname:
            mapfiles = list(util.list_dirs(os.path.dirname(mapfile), ".map"))
        else:
            mapfiles = [mapfile]

        if self.config.mapfiletype == "nginx":
            regex = "/%s/(.*)\t/dv/generated/(.*).html;" % self.urispace_segment
        else:
            idx = len(self.urispace_base) + len(self.urispace_segment) + 2
            regex = "(.*)\t(.*)"

        append_path = True
        for mapfile in mapfiles:
            if os.path.exists(mapfile):
                with codecs.open(mapfile, encoding="utf-8") as fp:
                    for line in fp:
                        path, filename = line.strip().split("\t", 1)
                        ret = callback(path, filename)
                        if ret is not None:
                            return ret
                        
    def download(self, basefile=None):
        if basefile is not None:
            raise ValueError("DV.download cannot process a basefile parameter")
        # recurse =~ download everything, which we do if refresh is
        # specified OR if we've never downloaded before
        recurse = False
        # if self.config.lastdownload has not been set, it has only
        # the type value, so self.config.lastdownload will raise
        # AttributeError. Should it return None instead?
        if self.config.refresh or 'lastdownload' not in self.config:
            recurse = True
        self.downloadcount = 0  # number of files extracted from zip files
        # (not number of zip files)
        try:
            if self.config.ftpuser:
                self.download_ftp("", recurse,
                                  self.config.ftpuser,
                                  self.config.ftppassword)
            else:
                self.log.warning(
                    "Config variable ftpuser not set, downloading from secondary source (https://lagen.nu/dv/downloaded/) instead")
                self.download_www("", recurse)
        except errors.MaxDownloadsReached:  # ok we're done!
            pass

    def download_ftp(self, dirname, recurse, user=None, password=None, connection=None):
        self.log.debug('Listing contents of %s' % dirname)
        lines = []
        if not connection:
            connection = FTP('ftp.dom.se')
            connection.login(user, password)

        connection.cwd(dirname)
        connection.retrlines('LIST', lines.append)

        for line in lines:
            parts = line.split()
            filename = parts[-1].strip()
            if line.startswith('d') and recurse:
                self.download_ftp(filename, recurse, connection=connection)
            elif line.startswith('-'):
                basefile = os.path.splitext(filename)[0]
                if dirname:
                    basefile = dirname + "/" + basefile
                # localpath = self.store.downloaded_path(basefile)
                localpath = self.store.path(basefile, 'downloaded/zips', '.zip')
                if os.path.exists(localpath) and not self.config.refresh:
                    pass  # we already got this
                else:
                    util.ensure_dir(localpath)
                    self.log.debug('Fetching %s to %s' % (filename,
                                                          localpath))
                    connection.retrbinary('RETR %s' % filename,
                                          # FIXME: retrbinary calls .close()?
                                          open(localpath, 'wb').write)
                    self.process_zipfile(localpath)
        connection.cwd('/')

    def download_www(self, dirname, recurse):
        url = 'https://lagen.nu/dv/downloaded/%s' % dirname
        self.log.debug('Listing contents of %s' % url)
        resp = requests.get(url)
        iterlinks = lxml.html.document_fromstring(resp.text).iterlinks()
        for element, attribute, link, pos in iterlinks:
            if link.startswith("/"):
                continue
            elif link.endswith("/") and recurse:
                self.download_www(link, recurse)
            elif link.endswith(".zip"):
                basefile = os.path.splitext(link)[0]
                if dirname:
                    basefile = dirname + basefile

                # localpath = self.store.downloaded_path(basefile)
                localpath = self.store.path(basefile, 'downloaded/zips', '.zip')
                if os.path.exists(localpath) and not self.config.refresh:
                    pass  # we already got this
                else:
                    absolute_url = urljoin(url, link)
                    self.log.debug('Fetching %s to %s' % (link, localpath))
                    resp = requests.get(absolute_url)
                    with self.store.open(basefile, "downloaded/zips", ".zip", "wb") as fp:
                        fp.write(resp.content)
                    self.process_zipfile(localpath)

    # eg. HDO_T3467-96.doc or HDO_T3467-96_1.doc
    re_malnr = re.compile(r'([^_]*)_([^_\.]*)()_?(\d*)(\.docx?)')
    # eg. HDO_T3467-96_BYTUT_2010-03-17.doc or
    #     HDO_T3467-96_BYTUT_2010-03-17_1.doc or
    #     HDO_T254-89_1_BYTUT_2009-04-28.doc (which is sort of the
    #     same as the above but the "_1" goes in a different place)
    re_bytut_malnr = re.compile(
        r'([^_]*)_([^_\.]*)_?(\d*)_BYTUT_\d+-\d+-\d+_?(\d*)(\.docx?)')
    re_tabort_malnr = re.compile(
        r'([^_]*)_([^_\.]*)_?(\d*)_TABORT_\d+-\d+-\d+_?(\d*)(\.docx?)')

    # temporary helper
    @action
    def process_all_zipfiles(self):
        self.downloadcount = 0
        zippath = self.store.path('', 'downloaded/zips', '')
        # Peocess zips in subdirs first (HDO, ADO
        # etc), then in numerics-only order.
        mykey = lambda v: (-len(v.split(os.sep)), "".join(c for c in v if c.isnumeric()))
        zipfiles = sorted(util.list_dirs(zippath, suffix=".zip"), key=mykey)
        for zipfilename in zipfiles:
            self.log.info("%s: Processing..." % zipfilename)
            self.process_zipfile(zipfilename)

    @action
    def process_zipfile(self, zipfilename):
        """Extract a named zipfile into appropriate documents"""
        removed = replaced = created = untouched = 0
        if not hasattr(self, 'downloadcount'):
            self.downloadcount = 0
        try:
            zipf = zipfile.ZipFile(zipfilename, "r")
        except zipfile.BadZipfile as e:
            self.log.error("%s is not a valid zip file: %s" % (zipfilename, e))
            return
        for bname in zipf.namelist():
            if not isinstance(bname, str):  # py2
                # Files in the zip file are encoded using codepage 437
                name = bname.decode('cp437')
            else:
                name = bname
            if "_notis_" in name:
                base, suffix = os.path.splitext(name)
                segments = base.split("_")
                coll, year = segments[0], segments[1]
                # Extract this doc as a temp file -- we won't be
                # creating an actual permanent file, but let
                # extract_notis extract individual parts of this file
                # to individual basefiles
                fp = tempfile.NamedTemporaryFile("wb", suffix=suffix, delete=False)
                filebytes = zipf.read(bname)
                fp.write(filebytes)
                fp.close()
                tempname = fp.name
                r = self.extract_notis(tempname, year, coll)
                assert r[0] + r[1], "No notices extracted from %s in %s" % (bname, zipfilename)
                created += r[0]
                untouched += r[1]
                os.unlink(tempname)
            else:
                name = os.path.split(name)[1]
                if 'BYTUT' in name:
                    m = self.re_bytut_malnr.match(name)
                elif 'TABORT' in name:
                    m = self.re_tabort_malnr.match(name)
                else:
                    m = self.re_malnr.match(name)
                if m:
                    (court, malnr, opt_referatnr, referatnr, suffix) = (
                        m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
                    assert ((suffix == ".doc") or (suffix == ".docx")
                            ), "Unknown suffix %s in %r" % (suffix, name)
                    if referatnr:
                        basefile = "%s/%s_%s" % (court, malnr, referatnr)
                    elif opt_referatnr:
                        basefile = "%s/%s_%s" % (court, malnr, opt_referatnr)
                    else:
                        basefile = "%s/%s" % (court, malnr)

                    basefile = basefile.strip()  # to avoid spurious trailing spaces in the filename before the file suffix

                    outfile = self.store.path(basefile, 'downloaded', suffix)

                    if "TABORT" in name:
                        self.log.info("%s: Removing" % basefile)
                        if not os.path.exists(outfile):
                            self.log.warning("%s: %s doesn't exist" % (basefile,
                                                                       outfile))
                        else:
                            os.unlink(outfile)
                        removed += 1
                    elif "BYTUT" in name:
                        self.log.info("%s: download OK (replacing with new)" % basefile)
                        if not os.path.exists(outfile):
                            self.log.warning("%s: %s doesn't exist" %
                                             (basefile, outfile))
                        replaced += 1
                    else:
                        self.log.info("%s: download OK (unpacking)" % basefile)
                        if os.path.exists(outfile):
                            untouched += 1
                            continue
                        else:
                            created += 1
                    if not "TABORT" in name:
                        data = zipf.read(bname)
                        with self.store.open(basefile, "downloaded", suffix, "wb") as fp:
                            fp.write(data)

                        # Make the unzipped files have correct timestamp
                        zi = zipf.getinfo(bname)
                        dt = datetime(*zi.date_time)
                        ts = mktime(dt.timetuple())
                        os.utime(outfile, (ts, ts))

                        self.downloadcount += 1
                    # fix HERE
                    if ('downloadmax' in self.config and
                            self.config.downloadmax and
                            self.downloadcount >= self.config.downloadmax):
                        raise errors.MaxDownloadsReached()
                else:
                    self.log.warning('Could not interpret filename %r i %s' %
                                     (name, os.path.relpath(zipfilename)))
        self.log.debug('Processed %s, created %s, replaced %s, removed %s, untouched %s files' %
                       (os.path.relpath(zipfilename), created, replaced, removed, untouched))

    def extract_notis(self, docfile, year, coll="HDO"):
        def find_month_in_previous(basefile):
            # The big word file with all notises might not
            # start with a month name -- try to find out
            # current month by examining the previous notis
            # (belonging to a previous word file).
            #
            # FIXME: It's possible that the two word files might be
            # different types (eg docx and doc). In that case the
            # resulting file will contain both OOXML and DocBook tags.
            self.log.warning(
                "No month specified in %s, attempting to look in previous file" %
                basefile)
            # HDO/2009_not_26 -> HDO/2009_not_25
            tmpfunc = lambda x: str(int(x.group(0)) - 1)
            prev_basefile = re.sub('\d+$', tmpfunc, basefile)
            prev_path = self.store.intermediate_path(prev_basefile)
            avd_p = None
            if os.path.exists(prev_path):
                with self.store.open_intermediate(prev_basefile, "rb") as fp:
                    soup = BeautifulSoup(fp.read(), "lxml")
                tmp = soup.find(["w:p", "para"])
                if re_avdstart.match(tmp.get_text().strip()):
                    avd_p = tmp
            if not avd_p:
                raise errors.ParseError(
                    "Cannot find value for month in %s (looked in %s)" %
                    (basefile, prev_path))
            return avd_p

        # Given a word document containing a set of "notisfall" from
        # either HD or HFD (earlier RegR), spit out a constructed
        # intermediate XML file for each notis and create a empty
        # placeholder downloaded file. The empty file should never be
        # opened since parse_open will prefer the constructed
        # intermediate file.
        if coll == "HDO":
            re_notisstart = re.compile(
                "(?P<day>Den\s+\d+\s*:[ae].\s+|)(?P<ordinal>\d+)\s*\.\s*\((?P<malnr>\w\s\d+-\d+)\)",
                flags=re.UNICODE)
            re_avdstart = re.compile(
                "(Januari|Februari|Mars|April|Maj|Juni|Juli|Augusti|September|Oktober|November|December)$")
        else:  # REG / HFD
            if int(year) < 2016:
                re_notisstart = re.compile(
                    "[\w\: ]*Lnr:(?P<court>\w+) ?(?P<year>\d+) ?not ?(?P<ordinal>\d+)",
                    flags=re.UNICODE)
            else:
                re_notisstart = re.compile("Not (?P<ordinal>\d+)$")
            re_avdstart = None
        created = untouched = 0
        intermediatefile = os.path.splitext(docfile)[0] + ".xml"
        with open(intermediatefile, "wb") as fp:
            filetype = WordReader().read(docfile, fp)
        
        soup = BeautifulSoup(util.readfile(intermediatefile), "lxml")
        os.unlink(intermediatefile) # won't be needed past this point
        if filetype == "docx":
            p_tag = "w:p"
            xmlns = ' xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        else:
            p_tag = "para"
            xmlns = ''
        iterator = soup.find_all(p_tag, limit=2147483647)
        basefile = None
        fp = None
        avd_p = None
        day = None
        intermediate_path = None
        for p in iterator:
            t = p.get_text().strip()
            if re_avdstart:
                # keep track of current month, store that in avd_p
                m = re_avdstart.match(t)
                if m:
                    avd_p = p
                    continue

            m = re_notisstart.match(t)
            if m:
                ordinal = m.group("ordinal")
                try:
                    if m.group("day"):
                        day = m.group("day")
                    else:
                        # inject current day in the first text node of
                        # p (which should inside of a <emphasis
                        # role="bold" or equivalent).
                        subnode = None
                        # FIXME: is this a deprecated method?
                        for c in p.recursiveChildGenerator():
                            if isinstance(c, NavigableString):
                                c.string.replace_with(day + str(c.string))
                                break
                except IndexError:
                    pass

                if intermediate_path:
                    previous_intermediate_path = intermediate_path
                basefile = "%(coll)s/%(year)s_not_%(ordinal)s" % locals()
                self.log.info("%s: Extracting from %s file" % (basefile, filetype))
                created += 1
                downloaded_path = self.store.path(basefile, 'downloaded', '.' + filetype)
                util.ensure_dir(downloaded_path)
                with open(downloaded_path, "w"):
                    pass  # just create an empty placeholder file --
                          # parse_open will load the intermediate file
                          # anyway.
                if fp:
                    fp.write(b"</body>\n")
                    fp.close()
                util.ensure_dir(self.store.intermediate_path(basefile))
                fp = self.store.open_intermediate(basefile, mode="wb")
                bodytag = '<body%s>' % xmlns 
                fp.write(bodytag.encode("utf-8"))
                if filetype != "docx":
                    fp.write(b"\n")
                if coll == "HDO" and not avd_p:
                    avd_p = find_month_in_previous(basefile)
                if avd_p:
                    fp.write(str(avd_p).encode("utf-8"))
            if fp:
                fp.write(str(p).encode("utf-8"))
                if filetype != "docx":
                    fp.write(b"\n")
        if fp:  # should always be the case
            fp.write(b"</body>\n")
            fp.close()
        else:
            self.log.error("%s/%s: No notis were extracted (%s)" %
                           (coll, year, docfile))
        return created, untouched

    re_delimSplit = re.compile("[;,] ?").split
    labels = {'Rubrik': DCTERMS.description,
              'Domstol': DCTERMS.creator,
              'Målnummer': RPUBL.malnummer,
              'Domsnummer': RPUBL.domsnummer,
              'Diarienummer': RPUBL.diarienummer,
              'Avdelning': RPUBL.domstolsavdelning,
              'Referat': DCTERMS.identifier,
              'Avgörandedatum': RPUBL.avgorandedatum,
              }

    def remote_url(self, basefile):
        # There is no publicly available URL where the source document
        # could be fetched.
        return None

    def adjust_basefile(self, doc, orig_uri):
        pass # See comments in swedishlegalsource.py 

    def parse_open(self, basefile, attachment=None, version=None):
        intermediate_path = self.store.intermediate_path(basefile)
        if not os.path.exists(intermediate_path):
            fp = self.downloaded_to_intermediate(basefile)
        else:
            fp = self.store.open_intermediate(basefile, "rb")
            # Determine if the previously-created intermediate files
            # came from .doc or OOXML (.docx) sources by sniffing the
            # first bytes.
            start = fp.read(6)
            assert isinstance(start, bytes), "fp seems to have been opened in a text-like mode"
            if start in (b"<w:doc", b"<body "):
                filetype = "docx"
            elif start in (b"<book ", b"<book>", b"<body>"):
                filetype = "doc"
            else:
                raise ValueError("Can't guess filetype from %r" % start)
            fp.seek(0)
            self.filetype = filetype
        return self.patch_if_needed(fp, basefile)

    def downloaded_to_intermediate(self, basefile, attachment=None):
        assert "_not_" not in basefile, "downloaded_to_intermediate can't handle Notisfall %s" % basefile
        docfile = self.store.downloaded_path(basefile)
        intermediatefile = self.store.intermediate_path(basefile)
        if os.path.getsize(docfile) == 0:
            raise errors.ParseError("%s: Downloaded file %s is empty, %s should have "
                                    "been created by download() but is missing!" %
                                    (basefile, docfile, intermediatefile))
        wr = WordReader()
        fp = self.store.open_intermediate(basefile, mode="wb")
        self.filetype = wr.read(docfile, fp, simplify=True)
        # FIXME: Do something with filetype if it's not what we expect
        fp.close()
        if hasattr(fp, 'utime'):
            os.utime(self.store.intermediate_path(basefile), (fp.utime, fp.utime))
        # re-open in read mode -- since we can't open a compressed
        # file in read-write mode
        return self.store.open_intermediate(basefile, mode="rb")
        

    def extract_head(self, fp, basefile):
        filetype = self.filetype
        patched = fp.read()
        # rawhead is a simple dict that we'll later transform into a
        # rdflib Graph. rawbody is a list of plaintext strings, each
        # representing a paragraph.
        if "not" in basefile:
            rawhead, rawbody = self.parse_not(patched, basefile, filetype)
        elif filetype == "docx":
            rawhead, rawbody = self.parse_ooxml(patched, basefile)
        else:
            rawhead, rawbody = self.parse_antiword_docbook(patched, basefile)
        # stash the body away for later reference
        self._rawbody = rawbody
        return rawhead

    def extract_metadata(self, rawhead, basefile):
        # we have already done all the extracting in extract_head
        return rawhead

    def parse_entry_title(self, doc):
        # FIXME: The primary use for entry.title is to generate
        # feeds. Should we construct a feed-friendly title here
        # (rpubl:referatrubrik is often too wordy, dcterm:identifier +
        # dcterms:subject might be a better choice -- also notisfall
        # does not have any rpubl:referatrubrik)
        title = doc.meta.value(URIRef(doc.uri), RPUBL.referatrubrik)
        if title:
            return str(title)

    def extract_body(self, fp, basefile):
        return self._rawbody

    def sanitize_body(self, rawbody):
        result = []
        seen_delmal = {}

        # ADO 1994 nr 102 to nr 113 have double \n between *EVERY
        # LINE*, not between every paragraph. Lines are short, less
        # than 60 chars. This leads to is_heading matching almost
        # every chunk. The weirdest thing is that the specific line
        # starting with "Ledamöter: " do NOT exhibit this trait... Try
        # to detect and undo.
        if (isinstance(rawbody[0], str) and  # Notisfall rawbody is a list of lists...
            max(len(line) for line in rawbody if not line.startswith("Ledamöter: ")) < 60):
            self.log.warning("Source has double newlines between every line, attempting "
                             "to reconstruct sections")
            newbody = []
            currentline = ""
            for idx, line in enumerate(rawbody):
                if (line.isupper() or # this is a obvious header
                    (idx + 1 < len(rawbody) and rawbody[idx+1].isupper()) or # next line is a obvious header
                    (idx + 1 < len(rawbody) and # line is short and a probable sentence enter + next line starts with a new sentence 
                     len(line) < 45 and
                     line[-1] in (".", "?", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9") and
                     rawbody[idx+1][0].isupper()) or
                    (idx + 1 < len(rawbody) and re.match("\d\.\s+[A-ZÅÄÖ]", rawbody[idx+1])) # next line seem to be a ordered paragraph
                    ):
                    newbody.append(currentline + "\n" + line)
                    currentline = ""
                else:
                    currentline += "\n" + line
            rawbody = newbody
        for idx, x in enumerate(rawbody):
            if isinstance(x, str):
                # detect and fix smushed numbered sections which MD
                # has, eg "18Marknadsandelar är..." ->
                # "18. Marknadsandelar är..."
                x = re.sub("^(\d{1,3})([A-ZÅÄÖ])", r"\1. \2", x)

                m = re.match("(KÄRANDE|SVARANDE|SAKEN)([A-ZÅÄÖ].*)", x) 
                if m:
                    # divide smushed-together headings like MD has,
                    # eg. "SAKENMarknadsföring av bilverkstäder..."
                    x = [m.group(1), m.group(2)]
                else:
                    # match smushed-together delmål markers like in "(jfr
                    # 1990 s 772 och s 796)I" and "Domslut HD fastställer
                    # HovR:ns domslut.II"
                    #
                    # But since we apparently need to handle spaces before
                    # "I", we might get false positives with sentences like
                    # "...och Dalarna. I\ndistributionsrörelsen
                    # sysselsattes...". Try to avoid this by checking for
                    # probable sentence start in next line
                    m = re.match("(.*[\.\) ])(I+)$", x, re.DOTALL)
                    if (m and rawbody[idx+1][0].isupper() and
                        not re.search("mellandomstema I+$", x, flags=re.IGNORECASE)):
                        x = [m.group(1), m.group(2)]
                    else:
                        x = [x]
                for p in x:
                    m = re.match("(I{1,3}|IV)\.? ?(|\(\w+\-\d+\))$", p)
                    if m:
                        seen_delmal[m.group(1)] = True
                    result.append(Paragraph([p]))
            else:
                result.append(Paragraph(x))
        # Many referats that are split into delmål lack the first
        # initial "I" that signifies the start of the first delmål
        # (but do have "II", "III" and possibly more)
        if seen_delmal and "I" not in seen_delmal:
            self.log.warning("Inserting missing 'I' for first delmal")
            result.insert(0, Paragraph(["I"]))

        return result

    def glue_shortlines(self, iterator, shortlen=62):
        buffer = None
        for line in iterator:
            # if the last line is short, and the current starts with a
            # lower case, the current line is probably a continuation
            # of the last
            if buffer and len(buffer.contents[-1].string) < shortlen and line.get_text()[0].islower():
                buffer.append(line)
            else:
                if buffer:
                    yield buffer
                    buffer = None
                if len(line.get_text()) < shortlen:
                    if buffer:
                        buffer.append(line)
                    else:
                        buffer = line #
        if buffer:
            yield buffer
            buffer = None
            
    
    def parse_not(self, text, basefile, filetype):
        basefile_regex = re.compile("(?P<type>\w+)/(?P<year>\d+)_not_(?P<ordinal>\d+)")
        referat_templ = {'REG': 'RÅ %(year)s not %(ordinal)s',
                         'HDO': 'NJA %(year)s not %(ordinal)s',
                         'HFD': 'HFD %(year)s not %(ordinal)s'}

        head = {}
        body = []

        m = basefile_regex.match(basefile).groupdict()
        coll = m['type']
        year = int(m['year'])
        head["Referat"] = referat_templ[coll] % m
        soup = BeautifulSoup(text, "lxml")
        if filetype == "docx":
            ptag = "w:p", "para" # support intermediate files with a
                                 # mix of OOXML/DocBook
        else:
            ptag = "para", "w:p"

        iterator = soup.find_all(ptag, limit=2147483647)
        if coll == "HDO":
            # keep this in sync w extract_notis
            re_notisstart = re.compile(
                "(?:Den (?P<avgdatum>\d+)\s*:[ae].\s+|)(?P<ordinal>\d+)\s*\.\s*\((?P<malnr>\w[ \xa0]\d+-\d+)\)",
                flags=re.UNICODE)
            re_avgdatum = re_malnr = re_notisstart
            re_lagrum = re_sokord = None
            # headers consist of the first two chunks. (month, then
            # date+ordinal+malnr)
            header = iterator.pop(0), iterator[0]  # need to re-read the second line later
            curryear = m['year']
            currmonth = self.swedish_months[header[0].get_text().strip().lower()]
            secondline = util.normalize_space(header[-1].get_text())
            m = re_notisstart.match(secondline)
            if m:
                head["Rubrik"] = secondline[m.end():].strip()
                if curryear == "2003":  # notisfall in this year lack
                                        # newline between heading and
                                        # actual text, so we use a
                                        # heuristic to just match
                                        # first sentence
                    m2 = re.search("\. [A-ZÅÄÖ]", head["Rubrik"])
                    if m2:
                        # now we know where the first sentence ends. Only keep that. 
                        head["Rubrik"] = head["Rubrik"][:m2.start()+1]
                                      
        else:  # "REG", "HFD"
            # keep in sync like above
            if year < 2016:
                re_notisstart = re.compile(
                    "[\w\: ]*Lnr:(?P<court>\w+) ?(?P<year>\d+) ?not ?(?P<ordinal>\d+)")
                re_malnr = re.compile(r"[AD][:-] ?(?P<malnr>\d+\-\d+)")
                # the avgdatum regex attempts to include valid dates, eg
                # not "2770-71-12".It's also somewhat tolerant of
                # formatting mistakes, eg accepts " :03-06-16" instead of
               # "A:03-06-16"
                re_avgdatum = re.compile(r"[AD ]: ?(?P<avgdatum>\d{2,4}\-[01]\d\-\d{2})")
                re_sokord = re.compile("Uppslagsord: ?(?P<sokord>.*)", flags=re.DOTALL)
                re_lagrum = re.compile("Lagrum: ?(?P<lagrum>.*)", flags=re.DOTALL)
            else:
                re_notisstart = re.compile("Not (?P<ordinal>\d+)")
                re_malnr = re.compile("Högsta förvaltningsdomstolen meddelade( den|) (?P<avgdatum>\d+ \w+ \d{4}) (följande |)(?P<avgtyp>dom|beslut) \((mål nr |)(?P<malnr>[\d\-–]+(| och [\d\-–]+))\)")
                re_avgdatum = re_malnr
                re_sokord = None
                re_lagrum = None
            # headers consists of the first five or six
            # chunks. Doesn't end until "^Not \d+."
            header = []
            done = False
            print("Maybe testing the new code")
            iterator = self.glue_shortlines(iterator)
            while not done and iterator:
                line = next(iterator).get_text().strip()
                # can possibly be "Not 1a." (RÅ 1994 not 1) or
                # "Not. 109." (RÅ 1998 not 109). There might be a
                # space separating the notis from the next sentence,
                # but there might also not be!
                # Also, avoid matchin the very first line of 2016+ style headers
                if re.match("Not(is|)\.? \d+[abc]?\.? ?", line) and not re.match("Not \d+$", line):
                    # this means a 2015 or earlier header
                    done = True
                    if ". -" in line[:2000]:
                        # Split out "Not 56" and the first
                        # sentence up to ". -", signalling that is the
                        # equiv of referatrubrik
                        rubr = line.split(". -", 1)[0]
                        rubr = re.sub("Not(is|)\.? \d+[abc]?\.? ", "", rubr)
                        head['Rubrik'] = rubr
                    else:
                        if line.endswith("Notisen har utgått."):
                            raise errors.DocumentRemovedError(basefile, dummyfile=self.store.parsed_path(basefile))
                else:
                    tmp = next(iterator)
                    if re_malnr.match(line):
                        # For HFD notises from 2016 there is no data
                        # that could serve as a rubrik. We could
                        # transform "Högsta förvaltningsdomstolen
                        # meddelade den 17 januari 2020 följande dom
                        # (mål nr 4430-19)." to "Dom den 17 januari
                        # 2020 i mål nr 4430" which I guess is better
                        # than nothing
                        m = re_malnr.match(line)
                        head['Rubrik'] = ("%(avgtyp)s den %(avgdatum)s i mål %(malnr)s" % m).capitalize()
                        done = True
                    if tmp.get_text().strip():
                        # REG specialcase
                        if header and header[-1].get_text().strip() == "Lagrum:":
                            # get the first bs4.element.Tag child
                            children = [x for x in tmp.children if hasattr(x, 'get_text')]
                            if children:
                                header[-1].append(children[0])
                        else:
                            header.append(tmp)
            if not done:
                raise errors.ParseError("Cannot find notis number in %s" % basefile)

        if coll == "HDO":
            head['Domstol'] = "Högsta Domstolen"
        elif coll == "HFD":
            head['Domstol'] = "Högsta förvaltningsdomstolen"
        elif coll == "REG":
            head['Domstol'] = "Regeringsrätten"
        else:
            raise errors.ParseError("Unsupported: %s" % coll)
        for node in header:
            t = util.normalize_space(node.get_text())
            # if not malnr, avgdatum found, look for those
            for fld, key, rex in (('Målnummer', 'malnr', re_malnr),
                                  ('Avgörandedatum', 'avgdatum', re_avgdatum),
                                  ('Lagrum', 'lagrum', re_lagrum),
                                  ('Sökord', 'sokord', re_sokord)):
                if not rex:
                    continue
                m = rex.search(t)
                if m and m.group(key):
                    if fld in ('Lagrum'):  # Sökord is split by sanitize_metadata
                        head[fld] = self.re_delimSplit(m.group(key))
                    else:
                        head[fld] = m.group(key)

        if coll == "HDO" and 'Avgörandedatum' in head:
            head[
                'Avgörandedatum'] = "%s-%02d-%02d" % (curryear, currmonth, int(head['Avgörandedatum']))

        # Do a basic conversion of the rest (bodytext) to Element objects
        #
        # This is generic enough that it could be part of WordReader
        for node in iterator:
            line = []
            if filetype == "doc":
                subiterator = node
            elif filetype == "docx":
                subiterator = node.find_all("w:r", limit=2147483647)
            for part in subiterator:
                if part.name:
                    t = part.get_text()
                else:
                    t = str(part)  # convert NavigableString to pure string
                # if not t.strip():
                #     continue
                if filetype == "doc" and part.name == "emphasis":  # docbook
                    if part.get("role") == "bold":
                        if line and isinstance(line[-1], Strong):
                            line[-1][-1] += t
                        else:
                            line.append(Strong([t]))
                    else:
                        if line and isinstance(line[-1], Em):
                            line[-1][-1] += t
                        else:
                            line.append(Em([t]))
                # ooxml
                elif filetype == "docx" and part.find("w:rpr") and part.find("w:rpr").find(["w:b", "w:i"]):
                    rpr = part.find("w:rpr")
                    if rpr.find("w:b"):
                        if line and isinstance(line[-1], Strong):
                            line[-1][-1] += t
                        else:
                            line.append(Strong([t]))
                    elif rpr.find("w:i"):
                        if line and isinstance(line[-1], Em):
                            line[-1][-1] += t
                        else:
                            line.append(Em([t]))
                else:
                    if line and isinstance(line[-1], str):
                        line[-1] += t
                    else:
                        line.append(t)
            if line:
                body.append(util.normalize_space(x) for x in line)
        return head, body


    def parse_ooxml(self, text, basefile):
        soup = BeautifulSoup(text, "lxml")
        head = {}
        # Högst uppe på varje domslut står domstolsnamnet ("Högsta
        # domstolen") följt av referatnumret ("NJA 1987
        # s. 113").
        firstfield = soup.find("w:t")
        # Ibland är domstolsnamnet uppsplittat på två
        # w:r-element. Bäst att gå på all text i
        # föräldra-w:tc-cellen
        firstfield = firstfield.find_parent("w:tc")
        head['Domstol'] = firstfield.get_text(strip=True)

        nextfield = firstfield.find_next("w:tc")
        head['Referat'] = nextfield.get_text(strip=True)
        # Hitta övriga enkla metadatafält i sidhuvudet
        for key in self.labels:
            if key in head:
                continue
            node = soup.find(text=re.compile(key + ':'))
            if not node:
                # FIXME: should warn for missing Målnummer iff
                # Domsnummer is not present, and vice versa. But at
                # this point we don't have all fields
                if key not in ('Diarienummer', 'Domsnummer', 'Avdelning', 'Målnummer'):
                    self.log.warning("%s: Couldn't find field %r" % (basefile, key))
                continue

            txt = "".join([n.get_text() for n in node.find_next("w:t").find_parent("w:p").find_all("w:t", limit=2147483647)])
            if txt.strip():  # skippa fält med tomma strängen-värden (eller bara whitespace)
                head[key] = txt

        # Hitta sammansatta metadata i sidhuvudet
        for key in ["Lagrum", "Rättsfall"]:
            node = soup.find(text=re.compile(key + ':'))
            if node:
                textnodes = node.find_parent('w:tc').find_next_sibling('w:tc')
                if not textnodes:
                    continue
                items = []
                for textnode in textnodes.find_all('w:t', limit=2147483647):
                    t = textnode.get_text(strip=True)
                    if t:
                        items.append(t)
                if items:
                    head[key] = items

        # The main text body of the verdict
        body = []
        for p in soup.find(text=re.compile('EFERAT')).find_parent(
                'w:tr').find_next_sibling('w:tr').find_all('w:p', limit=2147483647):
            ptext = ''
            for e in p.find_all("w:t", limit=2147483647):
                ptext += e.string
            body.append(ptext)

        # Finally, some more metadata in the footer
        if soup.find(text=re.compile(r'Sökord:')):
            head['Sökord'] = soup.find(
                text=re.compile(r'Sökord:')).find_next('w:t').get_text(strip=True)

        if soup.find(text=re.compile('^\s*Litteratur:\s*$')):
            n = soup.find(text=re.compile('^\s*Litteratur:\s*$'))
            head['Litteratur'] = n.findNext('w:t').get_text(strip=True)
        return head, body

    def parse_antiword_docbook(self, text, basefile):
        soup = BeautifulSoup(text, "lxml")
        head = {}
        header_elements = soup.find("para")
        header_text = ''
        for el in header_elements.contents:
            if hasattr(el, 'name') and el.name == "informaltable":
                break
            else:
                header_text += el.string

        # Högst uppe på varje domslut står domstolsnamnet ("Högsta
        # domstolen") följt av referatnumret ("NJA 1987
        # s. 113"). Beroende på worddokumentet ser dock XML-strukturen
        # olika ut. Det vanliga är att informationen finns i en
        # pipeseparerad paragraf:

        parts = [x.strip() for x in header_text.split("|")]
        if len(parts) > 1:
            head['Domstol'] = parts[0]
            head['Referat'] = parts[1]
        else:
            # alternativ står de på första raden i en informaltable
            row = soup.find("informaltable").tgroup.tbody.row.find_all('entry')
            head['Domstol'] = row[0].get_text(strip=True)
            head['Referat'] = row[1].get_text(strip=True)

        # Hitta övriga enkla metadatafält i sidhuvudet
        for key in self.labels:
            node = soup.find(text=re.compile(key + ':'))
            if node:
                txt = node.find_parent('entry').find_next_sibling(
                    'entry').get_text(strip=True)
                if txt:
                    head[key] = txt

        # Hitta sammansatta metadata i sidhuvudet
        for key in ["Lagrum", "Rättsfall"]:
            node = soup.find(text=re.compile(key + ':'))
            if node:
                head[key] = []
                textchunk = node.find_parent(
                    'entry').find_next_sibling('entry').string
                for line in [util.normalize_space(x) for x in textchunk.split("\n\n")]:
                    if line:
                        head[key].append(line)

        body = []
        for p in soup.find(text=re.compile('REFERAT')).find_parent('tgroup').find_next_sibling(
                'tgroup').find('entry').get_text(strip=True).split("\n\n"):
            body.append(p)

        # Hitta sammansatta metadata i sidfoten
        head['Sökord'] = soup.find(text=re.compile('Sökord:')).find_parent(
            'entry').next_sibling.next_sibling.get_text(strip=True)

        if soup.find(text=re.compile('^\s*Litteratur:\s*$')):
            n = soup.find(text=re.compile('^\s*Litteratur:\s*$')).find_parent(
                'entry').next_sibling.next_sibling.get_text(strip=True)
            head['Litteratur'] = n
        return head, body

    # correct broken/missing metadata
    def sanitize_metadata(self, head, basefile):
        basefile_regex = re.compile('(?P<type>\w+)/(?P<year>\d+)-(?P<ordinal>\d+)')
        nja_regex = re.compile(
            "NJA ?(\d+) ?s\.? ?(\d+) *\( ?(?:NJA|) ?[ :]?(\d+) ?: ?(\d+)")
        date_regex = re.compile("(\d+)[^\d]+(\d+)[^\d]+(\d+)")
        referat_regex = re.compile(
            "(?P<type>[A-ZÅÄÖ]+)[^\d]*(?P<year>\d+)[^\d]+(?P<ordinal>\d+)")
        referat_templ = {'ADO': 'AD %(year)s nr %(ordinal)s',
                         'AD': '%(type)s %(year)s nr %(ordinal)s',
                         'MDO': 'MD %(year)s:%(ordinal)s',
                         'NJA': '%(type)s %(year)s s. %(ordinal)s',
                         None: '%(type)s %(year)s:%(ordinal)s'
                         }
        # 0. strip whitespace
        for k, v in head.items():
            if isinstance(v, str):
                head[k] = v.strip()
        
        # 1. Attempt to fix missing Referat
        if not head.get("Referat"):
            # For some courts (MDO, ADO) it's possible to reconstruct a missing
            # Referat from the basefile
            m = basefile_regex.match(basefile)
            if m and m.group("type") in ('ADO', 'MDO'):
                head["Referat"] = referat_templ[m.group("type")] % (m.groupdict())

        # 2. Correct known problems with Domstol not always being correctly specified
        if "Hovrättenför" in head["Domstol"] or "Hovrättenöver" in head["Domstol"]:
            head["Domstol"] = head["Domstol"].replace("Hovrätten", "Hovrätten ")
        try:
            # if this throws a KeyError, it's not canonically specified
            self.lookup_resource(head["Domstol"], cutoff=1)
        except KeyError:
            # lookup URI with fuzzy matching, then turn back to canonical label
            head["Domstol"] = self.lookup_label(str(self.lookup_resource(head["Domstol"], warn=False)))

        # 3. Convert head['Målnummer'] to a list. Occasionally more than one
        # Malnummer is provided (c.f. AD 1994 nr 107, AD
        # 2005-117, AD 2003-111) in a comma, semicolo or space
        # separated list. AD 2006-105 even separates with " och ".
        #
        # HOWEVER, "Ö 2475-12" must not be converted to ["Ö2475-12"], not ['Ö', '2475-12']
        if head.get("Målnummer"):
            if head["Målnummer"][:2] in ('Ö ', 'B ', 'T '):
                head["Målnummer"] = [head["Målnummer"].replace(" ", "")]
            else:
                res = []
                for v in re.split("och|,|;|\s", head['Målnummer']):
                    if v.strip():
                        res.append(v.strip())
                head['Målnummer'] = res

        # 4. Create a general term for Målnummer or Domsnummer to act
        # as a local identifier
        if head.get("Målnummer"):
            head["_localid"] = head["Målnummer"]
        elif head.get("Domsnummer"):
            # NB: localid needs to be a list
            head["_localid"] = [head["Domsnummer"]]
        else:
            raise errors.ParseError("Required key (Målnummer/Domsnummer) missing")

        # 5. For NJA, Canonicalize the identifier through a very
        # forgiving regex and split of the alternative identifier
        # as head['_nja_ordinal']
        #
        # "NJA 2008 s 567 (NJA 2008:86)"=>("NJA 2008 s 567", "NJA 2008:86")
        # "NJA 2011 s. 638(NJA2011:57)" => ("NJA 2011 s 638", "NJA 2001:57")
        # "NJA 2012 s. 16(2012:2)" => ("NJA 2012 s 16", "NJA 2012:2")
        if "NJA" in head["Referat"] and " not " not in head["Referat"]:
            m = nja_regex.match(head["Referat"])
            if m:
                head["Referat"] = "NJA %s s %s" % (m.group(1), m.group(2))
                head["_nja_ordinal"] = "NJA %s:%s" % (m.group(3), m.group(4))
            else:
                raise errors.ParseError("Unparseable NJA ref '%s'" % head["Referat"])

        # 6 Canonicalize referats: Fix crap like "AD 2010nr 67",
        # "AD2011 nr 17", "HFD_2012 ref.58", "RH 2012_121", "RH2010
        # :180", "MD:2012:5", "MIG2011:14", "-MÖD 2010:32" and many
        # MANY more
        if " not " not in head["Referat"]:  # notiser always have OK Referat
            m = referat_regex.search(head["Referat"])
            if m:
                if m.group("type") in referat_templ:
                    head["Referat"] = referat_templ[m.group("type")] % m.groupdict()
                else:
                    head["Referat"] = referat_templ[None] % m.groupdict()
            elif basefile.split("/")[0] in ('ADO', 'MDO'):
                # FIXME: The same logic as under 1, duplicated
                m = basefile_regex.match(basefile)
                head["Referat"] = referat_templ[m.group("type")] % (m.groupdict())
            else:
                raise errors.ParseError("Unparseable ref '%s'" % head["Referat"])

        # 7. Convert Sökord string to an actual list
        if head.get("Sökord"):
            try:
                res = self.sanitize_sokord(head["Sökord"], basefile)
            except KeywordContainsDescription as e:
                res = e.keywords
                rubrik = " / ".join(e.descriptions)
                # I don't really know if this turns out to be a good
                # idea, but let's try it
                if "Rubrik" in head and "_not_" in basefile:
                    self.log.debug("%s: Changing rubrik %s -> %s (is that better?)" % (basefile, head["Rubrik"], rubrik))
                    head["Rubrik"] = rubrik
            head["Sökord"] = res

        # 8. Convert Avgörandedatum to a sensible value
        head["Avgörandedatum"] = self.parse_swedish_date(head["Avgörandedatum"])

        # 9. Done!
        return head

    # Strings that might look like descriptions but actually are legit
    # keywords (albeit very wordy keywords)
    sokord_whitelist = ("Rättsprövning enligt lagen (2006:304) om rättsprövning av vissa regeringsbeslut",)
    def sanitize_sokord(self, sokordstring, basefile):
        def capitalize(s):
            # remove any non-word char start (like "- ", which
            # sometimes occur due to double-dashes)
            s = re.sub("^\W+", "", s)
            return util.ucfirst(s)

        def probable_description(s):
            # FIXME: try to determine if this is sentence-like
            # (eg. containing common verbs like "ansågs", containing
            # more than x words etc)
            return (s not in self.sokord_whitelist) and len(s) >= 50
        res = []
        descs = []
        if basefile.startswith("XXX/"):
            delimiter = ","
        else:
            delimiter = ";"
        for s in sokordstring.split(delimiter):
            s = util.normalize_space(s)
            if not s:
                continue

            # normalize the delimiter between the main keyword and
            # subkeyword for some common variations like "Allmän
            # handling, allmän handling eller inte", "Allmän handling?
            # (brev till biskop i pastoral angelägenhet)" or "Allmän
            # handling -övriga frågor?"
            if " - " not in s:
                s = re.sub("(Allmän handling|Allmän försäkring|Arbetsskadeförsäkring|Besvärsrätt|Byggnadsmål|Plan- och bygglagen|Förhandsbesked|Resning)[:,?]?\s+\(?(.*?)\)?$", r"\1 - \2", s)
            subres = []
            substrings = s.split(" - ") 
            for idx, subs in enumerate(substrings):
                # often, what should be keywords is more of
                # descriptions, never occurring more than once. Try to
                # identify these pseudo-descriptions (which sometimes
                # are pretty good as descriptions go)
                if not probable_description(subs):
                    subres.append(capitalize(subs))
                else:
                    if idx + 1 != len(substrings):
                        self.log.warning("%s: Found probable description %r in sökord, but not at last position" % (basefile, subs))
                    descs.append(capitalize(subs))
            res.append(tuple(subres))
        if descs:
            # the remainder is not a legit keyword term. However, it
            # might be a useful title. Communicate it back to the
            # caller (but if we have several, omit shorter substrings
            # of longer descs)
            descs.sort(key=len)
            descs = [desc for idx, desc in enumerate(descs) if (idx + 1) == len(descs) or desc not in descs[idx+1]]
            raise KeywordContainsDescription(res, descs)
        return res
    

    @cached_property
    def rattsfall_parser(self):
        return SwedishCitationParser(LegalRef(LegalRef.RATTSFALL, LegalRef.EURATTSFALL),
                                     self.minter,
                                     self.commondata)

    @cached_property
    def lagrum_parser(self):
        return SwedishCitationParser(LegalRef(LegalRef.LAGRUM, LegalRef.EULAGSTIFTNING),
                                       self.minter,
                                       self.commondata)

    @cached_property
    def litteratur_parser(self):
        return SwedishCitationParser(LegalRef(LegalRef.FORARBETEN),
                                       self.minter,
                                       self.commondata)

    # create nice RDF from the sanitized metadata
    def polish_metadata(self, head, basefile, infer_nodes=True):

        def ref_to_uri(ref):
            nodes = self.rattsfall_parser.parse_string(ref)
            assert isinstance(nodes[0], Link), "Can't make URI from '%s'" % ref
            return nodes[0].uri

        def split_nja(value):
            return [x[:-1] for x in value.split("(")]

        # 1. mint uris and create the two Describers we'll use
        graph = self.make_graph()
        refuri = ref_to_uri(head["Referat"])
        refdesc = Describer(graph, refuri)
        for malnummer in head['_localid']:
            bnodetmp = BNode()
            gtmp = Graph()
            gtmp.bind("rpubl", RPUBL)
            gtmp.bind("dcterms", DCTERMS)
            
            dtmp = Describer(gtmp, bnodetmp)
            dtmp.rdftype(RPUBL.VagledandeDomstolsavgorande)
            dtmp.value(RPUBL.malnummer, malnummer)
            dtmp.value(RPUBL.avgorandedatum, head['Avgörandedatum'])
            dtmp.rel(DCTERMS.publisher, self.lookup_resource(head["Domstol"]))
            resource = dtmp.graph.resource(bnodetmp)
            domuri = self.minter.space.coin_uri(resource)
            domdesc = Describer(graph, domuri)
            
        # 2. convert all strings in head to proper RDF (well, some is
        # converted to mixed-content lists and stored in self._bodymeta
        self._bodymeta = defaultdict(list)
        for label, value in head.items():
            if label == "Rubrik":
                value = util.normalize_space(value)
                refdesc.value(RPUBL.referatrubrik, value, lang="sv")
                domdesc.value(DCTERMS.title, value, lang="sv")

            elif label == "Domstol":
                with domdesc.rel(DCTERMS.publisher, self.lookup_resource(value)):
                    # NB: Here, we take the court name as provided in
                    # the source document as the value for the
                    # foaf:name triple. We could also look it up in
                    # self.commondata, but since we already have it...
                    domdesc.value(FOAF.name, value)
                
            elif label == "Målnummer":
                for v in value:
                    # FIXME: In these cases (multiple målnummer, which
                    # primarily occurs with AD), we should really
                    # create separate domdesc objects (there are two
                    # verdicts, just summarized in one document)
                    domdesc.value(RPUBL.malnummer, v)
            elif label == "Domsnummer":
                domdesc.value(RPUBL.domsnummer, value)
            elif label == "Diarienummer":
                domdesc.value(RPUBL.diarienummer, value)
            elif label == "Avdelning":
                domdesc.value(RPUBL.avdelning, value)
            elif label == "Referat":
                for pred, regex in {'rattsfallspublikation': r'([^ ]+)',
                                    'arsutgava': r'(\d{4})',
                                    'lopnummer': r'\d{4}(?:\:| nr | not )(\d+)',
                                    'sidnummer': r's.? ?(\d+)'}.items():
                    m = re.search(regex, value)
                    if m:
                        if pred == 'rattsfallspublikation':
                            uri = self.lookup_resource(m.group(1),
                                                       predicate=SKOS.altLabel,
                                                       cutoff=1)
                            refdesc.rel(RPUBL[pred], uri)
                        else:
                            refdesc.value(RPUBL[pred], m.group(1))
                refdesc.value(DCTERMS.identifier, value)

            elif label == "_nja_ordinal":
                refdesc.value(DCTERMS.bibliographicCitation,
                              value)
                m = re.search(r'\d{4}(?:\:| nr | not )(\d+)', value)
                if m:
                    refdesc.value(RPUBL.lopnummer, m.group(1))
            elif label == "Avgörandedatum":
                assert isinstance(value, date) # should have been done by sanitize metadata
                domdesc.value(RPUBL.avgorandedatum, value)


            # The following metadata (Lagrum, Rättsfall and
            # Litteratur) is handled slightly differently -- it's not
            # added to the RDF graph (doc.meta) that will later be
            # addede to the XHTML <head> section. Instead, it's saved
            # in a struct which will later be added to doc.body. This
            # is so that we can represent mixed content metadata
            # (links, linktexts and unlinked text)
            elif label == "Lagrum":
                for i in value:  # better be list not string
#                    if (re.search("\d+/\d+/(EU|EG|EEG)", i) or
#                        re.search("\((EU|EG|EEG)\) nr \d+/\d+", i) or
#                        " direktiv" in i or " förordning" in i):
#                        self.log.warning("%s(%s): Lagrum ref to EULaw: '%s'" %
#                                         (head.get("Referat"), head.get("Målnummer"), i))
                    self._bodymeta[label].append(self.lagrum_parser.parse_string(i,
                                                 predicate="rpubl:lagrum"))
            elif label == "Rättsfall":
                for i in value:
                    self._bodymeta[label].append(self.rattsfall_parser.parse_string(i,
                                                 predicate="rpubl:rattsfallshanvisning"))
            elif label == "Litteratur":
                if value:
                    for i in value.split(";"):
                        self._bodymeta[label].append(self.litteratur_parser.parse_string(i,
                                                     predicate="dcterms:relation"))
            elif label == "Sökord":
                for s in value:
                    self.add_keyword_to_metadata(domdesc, s)

        # 3. mint some owl:sameAs URIs -- but only if not using canonical URIs
        # (moved to lagen.nu.DV)

        # 4. Add some same-for-everyone properties
        refdesc.rel(DCTERMS.publisher, self.lookup_resource('Domstolsverket'))
        if 'not' in head['Referat']:
            refdesc.rdftype(RPUBL.Rattsfallsnotis)
        else:
            refdesc.rdftype(RPUBL.Rattsfallsreferat)
        domdesc.rdftype(RPUBL.VagledandeDomstolsavgorande)
        refdesc.rel(RPUBL.referatAvDomstolsavgorande, domuri)
        refdesc.value(PROV.wasGeneratedBy, self.qualified_class_name())
        self._canonical_uri = refuri
        return refdesc.graph.resource(refuri)

    def add_keyword_to_metadata(self, domdesc, keyword):
        # Canonical uris don't define a URI space for
        # keywords/concepts. Instead refer to bnodes
        # with rdfs:label set (cf. rdl/documentation/exempel/
        # documents/publ/Domslut/HD/2009/T_170-08.rdf).
        #
        # Subclasses that has an idea of how to create a URI for a
        # keyword/concept might override this. 
        assert isinstance(keyword, tuple), "Keyword %s should have been a tuple of sub-keywords (possible 1-tuple)"
        with domdesc.rel(DCTERMS.subject):
            # if subkeywords, create a label like "Allmän handling»Begärd handling saknades"
            domdesc.value(RDFS.label, "»".join(keyword), lang=self.lang)

    def postprocess_doc(self, doc):
        if self.config.mapfiletype == "nginx":
            path = urlparse(doc.uri).path
        else:
            idx = len(self.urispace_base) + len(self.urispace_segment) + 2
            path = doc.uri[idx:]

        def map_append_needed(mapped_path, filename):
            if mapped_path == path:
                # This means that a previously parsed, basefile
                # already maps to the same URI (eg because a referat
                # of multiple dom documents occur as several different
                # (identical) basefiles. If it's a different basefile
                # (and not just the same parsed twice), raise
                # DuplicateReferatDoc
                try:
                    # convert generated path to a basefile if possible
                    basefile = filename.split("/",3)[-1].split(".")[0]
                except:
                    basefile = filename
                if doc.basefile != basefile:
                    raise DuplicateReferatDoc(basefile, dummyfile=self.store.parsed_path(doc.basefile))
                return False


        # the result of readmapfile can be either False (the case in
        # question already appeared in the uri.map file(s), or None
        # (the case was not found anywhere -- we should
        # append it to the appropriate uri.map file)
        append_needed = self.readmapfile(map_append_needed)
        if append_needed is not False:
            if self.config.clientname:
                # in a distributed setting, use a
                # uri-<clientname>-<pid>.map, eg
                # "uri-sophie-4435.map", to avoid corruption of a
                # single file by multiple writer, or slowness due to
                # lock contention.
                mapfile = self.store.path("uri", "generated", ".%s.%s.map" % (self.config.clientname, os.getpid()))
            else:
                mapfile = self.store.path("uri", "generated", ".map")
            with codecs.open(mapfile, "a", encoding="utf-8") as fp:
                if self.config.mapfiletype == "nginx":
                    fp.write("%s\t/dv/generated/%s.html;\n" % (path,
                                                               doc.basefile))
                else:
                    fp.write("%s\t%s\n" % (path, doc.basefile))
            if hasattr(self, "_basefilemap"):
                delattr(self, "_basefilemap")

        # NB: This cannot be made to work 100% as there is not a 1:1
        # mapping between basefiles and URIs since multiple basefiles
        # might map to the same URI (those basefiles should be
        # equivalent though). Examples:
        # HDO/B883-81_1 -> https://lagen.nu/rf/nja/1982s350 -> HDO/B882-81_1
        # HFD/1112-14 -> https://lagen.nu/rf/hfd/2014:35 -> HFD/1113-14
        # 
        # However, we detect that above and throw a
        # DuplicateReferatDoc error for the second (or third, or
        # fourth...) basefile encountered.
        computed_basefile = self.basefile_from_uri(doc.uri)
        assert doc.basefile == computed_basefile, "%s -> %s -> %s" % (doc.basefile, doc.uri, computed_basefile)

        # remove empty Instans objects (these can happen when both a
        # separate heading, as well as a clue in a paragraph,
        # indicates a new court).
        roots = []
        for node in doc.body:
            if isinstance(node, Delmal):
                roots.append(node)
        if roots == []:
            roots.append(doc.body)

        for root in roots:
            for node in list(root):
                if isinstance(node, Instans) and len(node) == 0:
                    # print("Removing Instans %r" % node.court)
                    root.remove(node)

        # add information from _bodymeta to doc.body
        bodymeta = Div(**{'class': 'bodymeta',
                          'about': str(doc.meta.value(URIRef(doc.uri), RPUBL.referatAvDomstolsavgorande))})
        for k, v in sorted(self._bodymeta.items()):
            d = Div(**{'class': k})
            for i in v:
                d.append(P(i))
            bodymeta.append(d)
        doc.body.insert(0, bodymeta)
        
            

    def infer_identifier(self, basefile):
        p = self.store.distilled_path(basefile)
        if not os.path.exists(p):
            raise ValueError("No distilled file for basefile %s at %s" % (basefile, p))

        with self.store.open_distilled(basefile) as fp:
            g = Graph().parse(data=fp.read())
        uri = self.canonical_uri(basefile)
        return str(g.value(URIRef(uri), DCTERMS.identifier))
        
    def parse_body_parseconfigs(self):
        return ("default", "simple")

    # @staticmethod
    def get_parser(self, basefile, sanitized, parseconfig="default"):
        re_courtname = re.compile(
            "^(Högsta domstolen|Hovrätten (över|för)[A-ZÅÄÖa-zåäö ]+|([A-ZÅÄÖ][a-zåäö]+ )(tingsrätt|hovrätt))(|, mark- och miljödomstolen|, Mark- och miljööverdomstolen)$")

#         productions = {'karande': '..',
#                        'court': '..',
#                        'date': '..'}

        # at parse time, initialize matchers
        rx = (
            {'name': 'fr-överkl',
             're': '(?P<karanden>[\w\.\(\)\- ]+) överklagade (beslutet|domen) '
                   'till (?P<court>(Förvaltningsrätten|Länsrätten|Kammarrätten) i \w+(| län)'
                   '(|, migrationsdomstolen|, Migrationsöverdomstolen)|'
                   'Högsta förvaltningsdomstolen)( \((?P<date>\d+-\d+-\d+), '
                   '(?P<constitution>[\w\.\- ,]+)\)|$)',
             'method': 'match',
             'type': ('instans',),
             'court': ('REG', 'HFD', 'MIG')},

            {'name': 'fr-dom',
             're': '(?P<court>(Förvaltningsrätten|'
                   'Länsrätten|Kammarrätten) i \w+(| län)'
                   '(|, migrationsdomstolen|, Migrationsöverdomstolen)|'
                   'Högsta förvaltningsdomstolen) \((?P<date>\d+-\d+-\d+), '
                   '(?P<constitution>[\w\.\- ,]+)\)',
             'method': 'match',
             'type': ('dom',),
             'court': ('REG', 'HFD', 'MIG')},

            {'name': 'tr-dom',
             're': '(?P<court>TR:n|Tingsrätten|HovR:n|Hovrätten|Mark- och miljödomstolen) \((?P<constitution>[\w\.\- ,]+)\) (anförde|fastställde|stadfäste|meddelade) (följande i |i beslut i |i |)(dom|beslut) (d\.|d|den) (?P<date>\d+ \w+\.? \d+)',
             'method': 'match',
             'type': ('dom',),
             'court': ('HDO', 'HGO', 'HNN', 'HON', 'HSB', 'HSV', 'HVS')},
            {'name': 'hd-dom',
             're': 'Målet avgjordes efter huvudförhandling (av|i) (?P<court>HD) \((?P<constitution>[\w:\.\- ,]+)\),? som',
             'method': 'match',
             'type': ('dom',),
             'court': ('HDO',)},
            {'name': 'hd-dom2',
             're': '(?P<court>HD) \((?P<constitution>[\w:\.\- ,]+)\) meddelade den (?P<date>\d+ \w+ \d+) följande',
             'method': 'match',
             'type': ('dom',),
             'court': ('HDO',)},
            {'name': 'hd-fastst',
             're': '(?P<court>HD) \((?P<constitution>[\w:\.\- ,]+)\) (beslöt|fattade (slutligt|följande slutliga) beslut)',
             'method': 'match',
             'type': ('dom',),
             'court': ('HDO',)},

            {'name': 'mig-dom',
             're': '(?P<court>Kammarrätten i Stockholm, Migrationsöverdomstolen)  \((?P<date>\d+-\d+-\d+), (?P<constitution>[\w\.\- ,]+)\)',
             'method': 'match',
             'type': ('dom',),
             'court': ('MIG',)},
            {'name': 'miv-forstainstans',
             're': '(?P<court>Migrationsverket) avslog (ansökan|ansökningarna) den (?P<date>\d+ \w+ \d+) och beslutade att',
             'method': 'match',
             'type': ('dom',),
             'court': ('MIG',)},
            {'name': 'miv-forstainstans-2',
             're': '(?P<court>Migrationsverket) avslog den (?P<date>\d+ \w+ \d+) A:s ansökan och beslutade att',
             'method': 'match',
             'type': ('dom',),
             'court': ('MIG',)},
            {'name': 'mig-dom-alt',
             're': 'I sin dom avslog (?P<court>Förvaltningsrätten i Stockholm, migrationsdomstolen) \((?P<date>\d+- ?\d+-\d+), (?P<constitution>[\w\.\- ,]+)\)',
             'method': 'match',
             'type': ('dom',),
             'court': ('MIG',)},
            {'name': 'allm-åkl',
             're': 'Allmän åklagare yrkade (.*)vid (?P<court>(([A-ZÅÄÖ]'
                   '[a-zåäö]+ )+)(TR|tingsrätt))',
             'method': 'match',
             'type': ('instans',),
             'court': ('HDO', 'HGO', 'HNN', 'HON', 'HSB', 'HSV', 'HVS')},
            {'name': 'stämning',
             're': 'stämning å (?P<svarande>.*) vid (?P<court>(([A-ZÅÄÖ]'
                   '[a-zåäö]+ )+)(TR|tingsrätt))',
             'method': 'search',
             'type': ('instans',),
             'court': ('HDO', 'HGO', 'HNN', 'HON', 'HSB', 'HSV', 'HVS')},
            {'name': 'ansökan',
             're': 'ansökte vid (?P<court>(([A-ZÅÄÖ][a-zåäö]+ )+)'
                   '(TR|tingsrätt)) om ',
             'method': 'search',
             'type': ('instans',),
             'court': ('HDO', 'HGO', 'HNN', 'HON', 'HSB', 'HSV', 'HVS')},
            {'name': 'riksåkl',
             're': 'Riksåklagaren väckte i (?P<court>HD|HovR:n (över|för) '
                   '([A-ZÅÄÖ][a-zåäö]+ )+|[A-ZÅÄÖ][a-zåäö]+ HovR) åtal',
                   'method': 'match',
             'type': ('instans',),
             'court': ('HDO', 'HGO', 'HNN', 'HON', 'HSB', 'HSV', 'HVS')},
            {'name': 'tr-överkl',
             're': '(?P<karande>[\w\.\(\)\- ]+) (fullföljde talan|'
                   'överklagade) (|TR:ns dom.*)i (?P<court>HD|(HovR:n|hovrätten) '
                   '(över|för) (Skåne och Blekinge|Västra Sverige|Nedre '
                   'Norrland|Övre Norrland)|(Svea|Göta) (HovR|hovrätt))',
                   'method': 'match',
             'type': ('instans',),
             'court': ('HDO', 'HGO', 'HNN', 'HON', 'HSB', 'HSV', 'HVS')},
            {'name': 'fullfölj-överkl',
             're': '(?P<karanden>[\w\.\(\)\- ]+) fullföljde sin talan$',
             'method': 'match',
             'type': ('instans',)},
            {'name': 'myndighetsansökan',
             're': 'I (ansökan|en ansökan|besvär) hos (?P<court>\w+) '
                   '(om förhandsbesked|yrkade)',
             'method': 'match',
             'type': ('instans',),
             'court': ('REG', 'HFD')},
            {'name': 'myndighetsbeslut',
             're': '(?P<court>\w+) beslutade (därefter |)(den (?P<date>\d+ \w+ \d+)|'
                   '[\w ]+) att',
             'method': 'match',
             'type': ('instans',),
             'court': ('REG', 'HFD', 'MIG')},
            {'name': 'myndighetsbeslut2',
             're': '(?P<court>[\w ]+) (bedömde|vägrade) i (bistånds|)beslut'
                   ' (|den (?P<date>\d+ \w+ \d+))',
             'method': 'match',
             'type': ('instans',),
             'court': ('REG', 'HFD')},
            {'name': 'hd-revision',
             're': '(?P<karanden>[\w\.\(\)\- ]+) sökte revision och yrkade(,'
                   'i första hand,|, såsom hans talan fick förstås,|,|) att (?P<court>HD|)',
             'method': 'match',
             'type': ('instans',),
             'court': ('HDO',)},
            {'name': 'hd-revision2',
             're': '(?P<karanden>[\w\.\(\)\- ]+) sökte revision$',
             'method': 'match',
             'type': ('instans',),
             'court': 'HDO'},
            {'name': 'hd-revision3',
             're': '(?P<karanden>[\w\.\(\)\- ]+) sökte revision och framställde samma yrkanden',
             'method': 'match',
             'type': ('instans',),
             'court': 'HDO'},
            {'name': 'överklag-bifall',
             're': '(?P<karanden>[\w\.\(\)\- ]+) (anförde besvär|'
                   'överklagade) och yrkade bifall till (sin talan i '
                   '(?P<prevcourt>HovR:n|TR:n)|)',
             'method': 'match',
             'type': ('instans',),
             'court': ('HDO', 'HGO', 'HNN', 'HON', 'HSB', 'HSV', 'HVS')},
            {'name': 'överklag-2',
             're': '(?P<karanden>[\w\.\(\)\- ]+) överklagade '
                   '(för egen del |)och yrkade (i själva saken |)att '
                   '(?P<court>HD|HovR:n|kammarrätten|Regeringsrätten|)',
             'method': 'match',
             'type': ('instans',)},
            {'name': 'överklag-3',
             're': '(?P<karanden>[\w\.\(\)\- ]+) överklagade (?P<prevcourt>'
                   '\w+)s (beslut|omprövningsbeslut|dom)( i ersättningsfrågan|) (hos|till) '
                   '(?P<court>[\w\, ]+?)( och yrkade| och anförde|, som| \(Sverige\)|$)',
             'method': 'match',
             'type': ('instans',)},
            {'name': 'överklag-4',
             're': '(?!Även )(?P<karanden>(?!HD fastställer)[\w\.\(\)\- ]+) överklagade ((?P<prevcourt>\w+)s (beslut|dom)|beslutet|domen)( och|$)',
             'method': 'match',
             'type': ('instans',)},
            {'name': 'hd-ansokan',
             're': '(?P<karanden>[\w\.\(\)\- ]+) anhöll i ansökan som inkom '
                   'till (?P<court>HD) d \d+ \w+ \d+',
             'method': 'match',
             'type': ('instans',),
             'court': ('HDO',)},
            {'name': 'hd-skrivelse',
             're': '(?P<karanden>[\w\.\(\)\- ]+) anförde i en till '
                   '(?P<court>HD) den \d+ \w+ \d+ ställd',
             'method': 'match',
             'type': ('instans',),
             'court': ('HDO',)},
            {'name': 'överklag-5',
             're': '(?!Även )(?P<karanden>[\w\.\(\)\- ]+?) överklagade '
                   '(?P<prevcourt>\w+)s (dom|domar)',
             'method': 'match',
             'type': ('instans',)},
            {'name': 'överklag-6',
             're': '(?P<karanden>[\w\.\(\)\- ]+) överklagade domen till '
                   '(?P<court>\w+)($| och yrkade)',
             'method': 'match',
             'type': ('instans',)},
            {'name': 'myndighetsbeslut3',
             're': 'I sitt beslut den (?P<date>\d+ \w+ \d+) avslog '
                   '(?P<court>\w+)',
             'method': 'match',
             'type': ('instans',),
             'court': ('REG', 'HFD', 'MIG')},
            {'name': 'domskal',
             're': "(Skäl|Domskäl|HovR:ns domskäl|Hovrättens domskäl)(\. |$)",
             'method': 'match',
             'type': ('domskal',)},
            {'name': 'domskal-ref',
             're': "(Tingsrätten|TR[:\.]n|Hovrätten|HD|Högsta förvaltningsdomstolen) \([^)]*\) (meddelade|anförde|fastställde|yttrade)",
             'method': 'match',
             'type': ('domskal',)},
            {'name': 'domskal-dom-fr',  # a simplified copy of fr-överkl
             're': '(?P<court>(Förvaltningsrätten|'
                   'Länsrätten|Kammarrätten) i \w+(| län)'
                   '(|, migrationsdomstolen|, Migrationsöverdomstolen)|'
                   'Högsta förvaltningsdomstolen) \((?P<date>\d+-\d+-\d+), '
                   '(?P<constitution>[\w\.\- ,]+)\),? yttrade',
             'method': 'match',
             'type': ('domskal',)},
            {'name': 'domslut-standalone',
             're': '(Domslut|(?P<court>Hovrätten|HD|hd|Högsta förvaltningsdomstolen):?s avgörande)$',
             'method': 'match',
             'type': ('domslut',)},
            {'name': 'domslut-start',
             're': '(?P<court>[\w ]+(domstolen|rätten))s avgörande$',
             'method': 'match',
             'type': ('domslut',)}
        )
        court = basefile.split("/")[0]
        matchers = defaultdict(list)
        matchersname = defaultdict(list)
        for pat in rx:
            if 'court' not in pat or court in pat['court']:
                for t in pat['type']:
                    # print("Adding pattern %s to %s" %  (pat['name'], t))
                    matchers[t].append(
                        getattr(
                            re.compile(
                                pat['re'],
                                re.UNICODE),
                            pat['method']))
                    matchersname[t].append(pat['name'])

        def is_delmal(parser, chunk=None):
            # should handle "IV", "I (UM1001-08)" and "I." etc
            # but not "I DEFINITION" or "Inledning"...
            if chunk:
                strchunk = str(chunk)
            else:
                strchunk = str(parser.reader.peek()).strip()
            if len(strchunk) < 20:
                m = re.match("(I{1,3}|IV)\.? ?(|\(\w+\-\d+\))$", strchunk)
                if m:
                    res = {'id': m.group(1)}
                    if m.group(2):
                        res['malnr'] = m.group(2)[1:-1]
                    return res
            return {}

        def is_instans(parser, chunk=None):
            """Determines whether the current position starts a new instans part
            of the report.

            """
            chunk = parser.reader.peek()
            strchunk = str(chunk)
            res = analyze_instans(strchunk)
            # sometimes, HD domskäl is written in a way that mirrors
            # the referat of the lower instance (eg. "1. Optimum
            # ansökte vid Lunds tingsrätt om stämning mot..."). If the
            # instans progression goes from higher->lower court,
            # something is amiss.
            if (hasattr(parser, 'current_instans') and
                parser.current_instans.court == "Högsta domstolen" and
                isinstance(res.get('court'), str) and "tingsrätt" in res['court']):
                return False
            if res:
                # in some referats, two subsequent chunks both matches
                # analyze_instans, even though they refer to the _same_
                # instans. Check to see if that is the case
                if (hasattr(parser, 'current_instans') and
                    hasattr(parser.current_instans, 'court') and
                    parser.current_instans.court and
                    is_equivalent_court(res['court'],
                                        parser.current_instans.court)):
                    return {}
                else:
                    return res
            elif parser._state_stack == ['body']:
                # if we're at root level, *anything* starts a new instans
                return True
            else:
                return {}

        def is_equivalent_court(newcourt, oldcourt):
            # should handle a bunch of cases
            # >>> is_equivalent_court("Göta Hovrätt", "HovR:n")
            # True
            # >>> is_equivalent_court("HD", "Högsta domstolen")
            # True
            # >>> is_equivalent_court("Linköpings tingsrätt", "HovR:n")
            # False
            # >>> is_equivalent_court(True, "Högsta domstolen")
            # True
            # if newcourt is True:
            #     return newcourt
            newcourt = canonicalize_court(newcourt)
            oldcourt = canonicalize_court(oldcourt)
            if newcourt is True and str(oldcourt) in ('Högsta domstolen'):
                # typically an effect of both parties appealing to the
                # supreme court
                return True
            if newcourt == oldcourt:
                return True
            else:
                return False

        def canonicalize_court(courtname):
            if isinstance(courtname, bool):
                return courtname  # we have no idea which court this
                # is, only that it is A court
            else:
                return courtname.replace(
                    "HD", "Högsta domstolen").replace("HovR", "Hovrätt")

        def is_heading(parser):
            chunk = parser.reader.peek()
            strchunk = str(chunk)
            if not strchunk.strip():
                return False
            # a heading is reasonably short and does not end with a
            # period (or other sentence ending typography)
            return len(strchunk) < 140 and not (strchunk.endswith(".") or
                                                strchunk.endswith(":") or
                                                strchunk.startswith("”"))

        def is_betankande(parser):
            strchunk = str(parser.reader.peek())
            return strchunk in ("Målet avgjordes efter föredragning.",
                                "HD avgjorde målet efter föredragning.")
        
        def is_dom(parser):
            strchunk = str(parser.reader.peek())
            res = analyze_dom(strchunk)
            return res

        def is_domskal(parser):
            strchunk = str(parser.reader.peek())
            res = analyze_domskal(strchunk)
            return res

        def is_domslut(parser):
            strchunk = str(parser.reader.peek())
            return analyze_domslut(strchunk)

        def is_skiljaktig(parser):
            strchunk = str(parser.reader.peek())
            return re.match(
                "(Justitie|Kammarrätts)råde[nt] ([^\.]*) var (skiljaktig|av skiljaktig mening)", strchunk)

        def is_tillagg(parser):
            strchunk = str(parser.reader.peek())
            return re.match(
                "Justitieråde[nt] ([^\.]*) (tillade för egen del|gjorde för egen del ett tillägg)", strchunk)

        def is_endmeta(parser):
            strchunk = str(parser.reader.peek())
            return re.match("HD:s (beslut|dom|domar) meddela(de|d|t): den", strchunk)

        def is_paragraph(parser):
            return True

        # Turns out, this is really difficult if you consider
        # abbreviations.  This particular heuristic splits on periods
        # only (Sentences ending with ? or ! are rare in legal text)
        # and only if followed by a capital letter (ie next sentence)
        # or EOF. Does not handle things like "Mr. Smith" but that's
        # also rare in swedish text. However, needs to handle "Linder,
        # Erliksson, referent, och C. Bohlin", so another heuristic is
        # that the sentence before can't end in a single capital
        # letter.
        def split_sentences(text):
            text = util.normalize_space(text)
            text += " "
            return [x.strip() for x in re.split("(?<![A-ZÅÄÖ])\. (?=[A-ZÅÄÖ]|$)", text)]

        def analyze_instans(strchunk):
            res = {}
            # Case 1: Fixed headings indicating new instance
            if re_courtname.match(strchunk):
                res['court'] = strchunk
                res['complete'] = True
                return res
            else:
                # case 2: common wording patterns indicating new
                # instance
                # "H.T. sökte revision och yrkade att HD måtte fastställa" =>
                # <Instans name="HD"><str>H.T. sökte revision och yrkade att <PredicateSubject rel="HD" uri="http://lagen.nu/org/2008/hogsta-domstolen/">HD>/PredicateSubject>
                # <div class="instans" rel="dc:creator" href="..."

                # the needed sentence is usually 1st or 2nd
                # (occassionally 3rd), searching more yields risk of
                # false positives.
                sentences = split_sentences(strchunk)[:3]

                # In rare cases (HDO/T50-91_1) a chunk might be a
                # Domskäl, but the second sentence looks like the
                # start of an Instans. Since recognizers come in a
                # particular order, is_instans is run before
                # is_domskal, we must detect this false positive
                domskal_match = matchers['domskal'][matchersname['domskal'].index('domskal')]
                if domskal_match(sentences[0]):
                    return res
                for sentence in sentences:
                    for (r, rname) in zip(matchers['instans'], matchersname['instans']):
                        m = r(sentence)
                        if m:
                            # print("analyze_instans: Matcher '%s' succeeded on '%s'" % (rname, sentence))
                            mg = m.groupdict()
                            if 'court' in mg and mg['court']:
                                res['court'] = mg['court'].strip()
                            else:
                                res['court'] = True
                            # if 'prevcourt' in mg and mg['prevcourt']:
                            #    res['prevcourt'] = mg['prevcourt'].strip()
                            if 'date' in mg and mg['date']:
                                parse_swed = DV().parse_swedish_date
                                parse_iso = DV().parse_iso_date
                                try:
                                    res['date'] = parse_swed(mg['date'])
                                except ValueError:
                                    res['date'] = parse_iso(mg['date'])
                            return res
            return res

        def analyze_dom(strchunk):
            res = {}
            # special case for "referat" who are nothing but straight verdict documents.
            if strchunk.strip() == "SAKEN":
                return {'court': True}
            # probably only the 1st sentence is interesting
            for sentence in split_sentences(strchunk)[:1]:
                for (r, rname) in zip(matchers['dom'], matchersname['dom']):
                    m = r(sentence)
                    if m:
                        # print("analyze_dom: Matcher '%s' succeeded on '%s': %r" % (rname, sentence,m.groupdict()))
                        mg = m.groupdict()
                        if 'court' in mg and mg['court']:
                            res['court'] = mg['court'].strip()
                        if 'date' in mg and mg['date']:
                            parse_swed = DV().parse_swedish_date
                            parse_iso = DV().parse_iso_date
                            try:
                                res['date'] = parse_swed(mg['date'])
                            except ValueError:
                                try:
                                    res['date'] = parse_iso(mg['date'])
                                except ValueError:
                                    pass
                                    # or res['date'] = mg['date']??

                        # if 'constitution' in mg:
                        #    res['constitution'] = parse_constitution(mg['constitution'])
                        return res
            return res

        def analyze_domskal(strchunk):
            res = {}
            # only 1st sentence
            for sentence in split_sentences(strchunk)[:1]:
                for (r, rname) in zip(matchers['domskal'], matchersname['domskal']):
                    m = r(sentence)
                    if m:
                        # print("analyze_domskal: Matcher '%s' succeeded on '%s'" % (rname, sentence))
                        res['domskal'] = True
                        return res
            return res

        def analyze_domslut(strchunk):
            res = {}
            # only 1st sentence
            for sentence in split_sentences(strchunk)[:1]:
                for (r, rname) in zip(matchers['domslut'], matchersname['domslut']):
                    m = r(sentence)
                    if m:
                        # print("analyze_domslut: Matcher '%s' succeeded on '%s'" % (rname, sentence))
                        mg = m.groupdict()
                        if 'court' in mg and mg['court']:
                            res['court'] = mg['court'].strip()
                        else:
                            res['court'] = True
                        return res
            return res

        def parse_constitution(strchunk):
            res = []
            for thing in strchunk.split(", "):
                if thing in ("ordförande", "referent"):
                    res[-1]['position'] = thing
                elif thing.startswith("ordförande ") or thing.startswith("ordf "):
                    pos, name = thing.split(" ", 1)
                    if name.startswith("t f lagmannen"):
                        title, name = name[:13], name[14:]
                    elif name.startswith("hovrättsrådet"):
                        title, name = name[:13], name[14:]
                    else:
                        title = None
                    r = {'name': name,
                         'position': pos,
                         'title': title}
                    if 'title' not in r:
                        del r['title']
                    res.append(r)
                else:
                    name = thing
                    res.append({'name': name})
            # also filter nulls
            return res

        # FIXME: This and make_paragraph ought to be expressed as
        # generic functions in the ferenda.fsmparser module
        @newstate('body')
        def make_body(parser):
            return parser.make_children(Body())

        @newstate('delmal')
        def make_delmal(parser):
            attrs = is_delmal(parser, parser.reader.next())
            if hasattr(parser, 'current_instans'):
                delattr(parser, 'current_instans')
            d = Delmal(ordinal=attrs['id'], malnr=attrs.get('malnr'))
            return parser.make_children(d)

        @newstate('instans')
        def make_instans(parser):
            chunk = parser.reader.next()
            strchunk = str(chunk)
            idata = analyze_instans(strchunk)
            # idata may be {} if the special toplevel rule in is_instans applied
            if 'complete' in idata:
                i = Instans(court=strchunk)
                court = strchunk
            elif 'court' in idata and idata['court'] is not True:
                i = Instans([chunk], court=idata['court'])
                court = idata['court']
            else:
                i = Instans([chunk], court=parser.defaultcourt)
                court = parser.defaultcourt
                if court is None:
                    court = "" # we might need to calculate the courts len() below

            # FIXME: ugly hack, but is_instans needs access to this
            # object...
            parser.current_instans = i
            res = parser.make_children(i)

            # might need to adjust the court parameter based on better
            # information in the parse tree
            for child in res:
                if isinstance(child, Dom) and hasattr(child, 'court'):
                    # longer courtnames are better
                    if len(str(child.court)) > len(court):
                        i.court = child.court
            return res

        def make_heading(parser):
            # a heading is by definition a single line
            return Heading(parser.reader.next())

        @newstate('betankande')
        def make_betankande(parser):
            b = Betankande()
            b.append(parser.reader.next())
            return parser.make_children(b)

        @newstate('dom')
        def make_dom(parser):
            # fix date, constitution etc. Note peek() instead of read() --
            # this is so is_domskal can have a chance at the same data
            ddata = analyze_dom(str(parser.reader.peek()))
            d = Dom(avgorandedatum=ddata.get('date'),
                    court=ddata.get('court'),
                    malnr=ddata.get('caseid'))
            return parser.make_children(d)

        @newstate('domskal')
        def make_domskal(parser):
            d = Domskal()
            return parser.make_children(d)

        @newstate('domslut')
        def make_domslut(parser):
            d = Domslut()
            return parser.make_children(d)

        @newstate('skiljaktig')
        def make_skiljaktig(parser):
            s = Skiljaktig()
            s.append(parser.reader.next())
            return parser.make_children(s)

        @newstate('tillagg')
        def make_tillagg(parser):
            t = Tillagg()
            t.append(parser.reader.next())
            return parser.make_children(t)

        @newstate('endmeta')
        def make_endmeta(parser):
            m = Endmeta()
            m.append(parser.reader.next())
            return parser.make_children(m)

        def make_paragraph(parser):
            chunk = parser.reader.next()
            strchunk = str(chunk)
            if not strchunk.strip():  # filter out empty things
                return None
            if parser.has_ordered_paras and ordered(strchunk):
                # FIXME: Cut the ordinal from chunk somehow
                if isinstance(chunk, Paragraph):
                    chunks = list(chunk)
                    chunks[0] = re.sub("^\s*\d+\. ", "", chunks[0])
                    p = OrderedParagraph(chunks, ordinal=ordered(strchunk))
                else:
                    chunk = re.sub("^\s*\d+\. ", "", chunk)
                    p = OrderedParagraph([chunk], ordinal=ordered(strchunk))
            else:
                if isinstance(chunk, Paragraph):
                    p = chunk
                else:
                    p = Paragraph([chunk])
            return p

        def ordered(chunk):
            """Given a string that might be a ordered paragraph, return the
            ordinal if so, or None otherwise.x

            """
            # most ordered paras use "18. Blahonga". But when quoting
            # EU law, sometimes "18 Blahonga". Treat these the same.
            # NOTE: It should not match eg "24hPoker är en
            # bolagskonstruktion..." (HDO/B2760-09)
            m = re.match("(\d+)\.?\s", chunk)
            if m:
                return m.group(1)

        def transition_domskal(symbol, statestack):
            if 'betankande' in statestack:
                # Ugly hack: mangle the statestack so that *next time*
                # we encounter a is_domskal, we pop the statestack,
                # but for now we push to it.

                # FIXME: This made TestDV.test_parse_HDO_O2668_07 fail
                # since is_dom wasn't amongst the possible recognizers
                # when "HD (...) fattade slutligt beslut i enlighet
                # [...]" was up. I don't know if this logic is needed
                # anymore, but removing it does not cause test
                # failures.
                
                # statestack[statestack.index('betankande')] = "__done__"
                return make_domskal, "domskal"
            else:
                # here's where we pop the stack
                return False, None

        p = FSMParser()
        # "dom" should not really be a commonstate (it should
        # theoretically alwawys be followed by domskal or maybe
        # domslut) but in some cases, the domskal merges with the
        # start of dom in such a way that we can't transition into
        # domskal right away (eg HovR:s dom in HDO/B10-86_1 and prob
        # countless others)
        commonstates = (
            "body",
            "delmal",
            "instans",
            "dom",
            "domskal",
            "domslut",
            "betankande",
            "skiljaktig",
            "tillagg")


        if parseconfig == "simple":
            p.set_recognizers(is_paragraph)
            p.set_transitions({
                ("body", is_paragraph): (make_paragraph, None)
                })
            p.has_ordered_paras = False
        else:
            p.set_recognizers(is_delmal,
                              is_endmeta,
                              is_instans,
                              is_dom,
                              is_betankande,
                              is_domskal,
                              is_domslut,
                              is_skiljaktig,
                              is_tillagg,
                              is_heading,
                              is_paragraph)
            p.set_transitions({
                ("body", is_delmal): (make_delmal, "delmal"),
                ("body", is_instans): (make_instans, "instans"),
                ("body", is_endmeta): (make_endmeta, "endmeta"),
                ("delmal", is_instans): (make_instans, "instans"),
                ("delmal", is_delmal): (False, None),
                ("delmal", is_endmeta): (False, None),
                ("instans", is_betankande): (make_betankande, "betankande"),
                ("instans", is_domslut): (make_domslut, "domslut"),
                ("instans", is_dom): (make_dom, "dom"),
                ("instans", is_instans): (False, None),
                ("instans", is_skiljaktig): (make_skiljaktig, "skiljaktig"),
                ("instans", is_tillagg): (make_tillagg, "tillagg"),
                ("instans", is_delmal): (False, None),
                ("instans", is_endmeta): (False, None),
                # either (make_domskal, "domskal") or (False, None)
                ("betankande", is_domskal): transition_domskal,
                ("betankande", is_domslut): (make_domslut, "domslut"),
                ("betankande", is_dom): (False, None),
                ("__done__", is_domskal): (False, None),
                ("__done__", is_skiljaktig): (False, None),
                ("__done__", is_tillagg): (False, None),
                ("__done__", is_delmal): (False, None),
                ("__done__", is_endmeta): (False, None),
                ("__done__", is_domslut): (make_domslut, "domslut"),
                ("dom", is_domskal): (make_domskal, "domskal"),
                ("dom", is_domslut): (make_domslut, "domslut"),
                ("dom", is_instans): (False, None),
                ("dom", is_skiljaktig): (False, None),  # Skiljaktig mening is not considered
                # part of the dom, but rather an appendix
                ("dom", is_tillagg): (False, None),
                ("dom", is_endmeta): (False, None),
                ("dom", is_delmal): (False, None),
                ("domskal", is_delmal): (False, None),
                ("domskal", is_domslut): (False, None),
                ("domskal", is_instans): (False, None),
                ("domslut", is_delmal): (False, None),
                ("domslut", is_instans): (False, None),
                ("domslut", is_domskal): (False, None),
                ("domslut", is_skiljaktig): (False, None),
                ("domslut", is_tillagg): (False, None),
                ("domslut", is_endmeta): (False, None),
                ("domslut", is_dom): (False, None),
                ("skiljaktig", is_domslut): (False, None),
                ("skiljaktig", is_instans): (False, None),
                ("skiljaktig", is_skiljaktig): (False, None),
                ("skiljaktig", is_tillagg): (False, None),
                ("skiljaktig", is_delmal): (False, None),
                ("skiljaktig", is_endmeta): (False, None),
                ("tillagg", is_tillagg): (False, None),
                ("tillagg", is_delmal): (False, None),
                ("tillagg", is_endmeta): (False, None),
                ("endmeta", is_paragraph): (make_paragraph, None),
                (commonstates, is_heading): (make_heading, None),
                (commonstates, is_paragraph): (make_paragraph, None),
            })
            # only NJA and MD cases (distinguished by the first three
            # chars of basefile) can have ordered paragraphs
            p.has_ordered_paras = basefile[:3] in ('HDO', 'MDO')
        # parser configuration that is identical between the 'default'
        # and 'simple' parser
        p.initial_state = "body"
        p.initial_constructor = make_body
        p.debug = os.environ.get('FERENDA_FSMDEBUG', False)
        # In some cases it's difficult to determine court from document alone.
        p.defaultcourt = {'PMD': 'Patent- och marknadsöverdomstolen',
                          'MMD': 'Mark- och miljööverdomstolen'}.get(basefile.split("/")[0])
        # return p
        return p.parse

    # FIXME: Get this information from self.commondata and the slugs
    # file. However, that data does not contain lower-level
    # courts. For now, we use the slugs used internally at
    # Domstolsverket, but lower-case. Also, this list does not attempt
    # to bridge when a court changes name (eg. LST and FST are
    # distinct, even though they refer to the "same" court). In the
    # case of adminstrative decisions, this also includes slugs for
    # commmon administrative agencies.
    courtslugs = {
        "Skatterättsnämnden": "SRN",
        "Skatteverket": "SKV",
        "Migrationsverket": "MIV",
        "PTS": "PTS",
        "Attunda tingsrätt": "TAT",
        "Blekinge tingsrätt": "TBL",
        "Bollnäs TR": "TBOL",
        "Borås tingsrätt": "TBOR",
        "Eskilstuna tingsrätt": "TES",
        "Eslövs TR": "TESL",
        "Eksjö TR": "TEK",
        "Falu tingsrätt": "TFA",
        "Försäkringskassan": "FSK",
        "Förvaltningsrätten i Göteborg": "FGO",
        "Förvaltningsrätten i Göteborg, migrationsdomstolen": "MGO",
        "Förvaltningsrätten i Malmö": "FMA",
        "Förvaltningsrätten i Malmö, migrationsdomstolen": "MFM",
        "Förvaltningsrätten i Stockholm": "FST",
        "Förvaltningsrätten i Stockholm, migrationsdomstolen": "MFS",
        "Gotlands tingsrätt": "TGO",
        "Gävle tingsrätt": "TGA",
        "Göta hovrätt": "HGO",
        "Göteborgs TR": "TGO",
        "Göteborgs tingsrätt": "TGO",
        "Halmstads tingsrätt": "THA",
        "Helsingborgs tingsrätt": "THE",
        "Hudiksvalls tingsrätt": "THU",
        "Jönköpings tingsrätt": "TJO",
        "Kalmar tingsrätt": "TKA",
        "Kammarrätten i Sundsvall": "KSU",
        "Kristianstads tingsrätt": "TKR",
        "Linköpings tingsrätt": "TLI",
        "Ljusdals TR": "TLJ",
        "Luleå tingsrätt": "TLU",
        "Lunds tingsrätt": "TLU",
        "Lycksele tingsrätt": "TLY",
        "Länsrätten i Dalarnas län": "LDA",
        "Länsrätten i Göteborg": "LGO",
        "Länsrätten i Jämtlands län": "LJA",
        "Länsrätten i Kopparbergs län": "LKO",
        "Länsrätten i Kronobergs län": "LKR",
        "Länsrätten i Malmöhus län": "LMAL",
        "Länsrätten i Mariestad": "LMAR",
        "Länsrätten i Norbottens län": "LNO",
        "Länsrätten i Skaraborgs län": "LSK",
        "Länsrätten i Skåne län": "LSK",
        "Länsrätten i Stockholms län": "LST",
        "Länsrätten i Stockholms län, migrationsdomstolen": "MLS",
        "Länsrätten i Södermanlands län": "LSO",
        "Länsrätten i Uppsala län": "LUP",
        "Länsrätten i Vänersborg": "LVAN",
        "Länsrätten i Värmlands län": "LVAR",
        "Länsrätten i Västerbottens län": "LVAB",
        "Länsrätten i Västmanlands län": "LVAL",
        "Länsrätten i Älvsborgs län": "LAL",
        "Malmö TR": "TMA",
        "Malmö tingsrätt": "TMA",
        "Mariestads tingsrätt": "TMAR",
        "Mora tingsrätt": "TMO",
        "Nacka tingsrätt": "TNA",
        "Norrköpings tingsrätt": "TNO",
        "Nyköpings tingsrätt": "TNY",
        "Skaraborgs tingsrätt": "TSK",
        "Skövde TR": "TSK",
        "Solna tingsrätt": "TSO",
        "Stockholms TR": "TST",
        "Stockholms tingsrätt": "TST",
        "Sundsvalls tingsrätt": "TSU",
        "Svea hovrätt, Mark- och miljööverdomstolen": "MHS",
        "Södertälje tingsrätt": "TSE",
        "Södertörns tingsrätt": "TSN",
        "Södra Roslags TR": "TSR",
        "Uddevalla tingsrätt": "TUD",
        "Umeå tingsrätt": "TUM",
        "Uppsala tingsrätt": "TUP",
        "Varbergs tingsrätt": "TVAR",
        "Vänersborgs tingsrätt": "TVAN",
        "Värmlands tingsrätt": "TVARM",
        "Västmanlands tingsrätt": "TVAS",
        "Växjö tingsrätt": "TVA",
        "Ångermanlands tingsrätt": "TAN",
        "Örebro tingsrätt": "TOR",
        "Östersunds tingsrätt": "TOS",

        "Kammarrätten i Jönköping": "KJO",
        "Kammarrätten i Göteborg": "KGO",
        "Kammarrätten i Stockholm": "KST",
        "Göta HovR": "HGO",
        "HovR:n för Nedre Norrland": "HNN",
        "HovR:n för Västra Sverige": "HVS",
        "HovR:n för Övre Norrland": "HON",
        "HovR:n över Skåne och Blekinge": "HSB",
        "Hovrätten för Nedre Norrland": "HNN",
        "Hovrätten för Västra Sverige": "HVS",
        "Hovrätten för Västra Sverige": "HVS",
        "Hovrätten för Övre Norrland": "HON",
        "Hovrätten över Skåne och Blekinge": "HSB",
        "Hovrätten över Skåne och Blekinge": "HSB",
        "Svea HovR": "HSV",
        "Svea hovrätt": "HSV",

        # supreme courts generally use abbrevs established by
        # Vägledande rättsfall.
        "Kammarrätten i Stockholm, Migrationsöverdomstolen": "MIG",
        "Migrationsöverdomstolen": "MIG",
        "Högsta förvaltningsdomstolen": "HFD",
        "Regeringsrätten": "REGR",  # REG is "Regeringen"
        "Högsta domstolen": "HDO",
        "HD": "HDO",
        "arbetsdomstolen": "ADO",
        "Mark- och miljööverdomstolen": "MMD",
        "Patentbesvärsrätten": "PBR",
        "Patent- och marknadsöverdomstolen": "PMÖD",

        # for when the type of court, but not the specific court, is given
        "HovR:n": "HovR",
        "Hovrätten": "HovR",
        "Kammarrätten": "KamR",
        "Länsrätten": "LR",
        "TR:n": "TR",
        "Tingsrätten": "TR",
        "länsrätten": "LR",
        "miljödomstolen": "MID",
        "tingsrätten": "TR",
        "Länsstyrelsen": "LST",
        "Marknadsdomstolen": "MD",
        "Migrationsdomstolen": "MID",
        "fastighetsdomstolen": "FD",
        "förvaltningsrätten": "FR",
        "hovrätten": "HovR",
        "kammarrätten": "KamR",

        # additional courts/agencies
        "Alingsås TR": "TAL",
        "Alingsås tingsrätt": "TAL",
        "Arbetslöshetskassan": "ALK",
        "Arvika TR": "TAR",
        "Banverket": "BAN",
        "Bodens TR": "TBO",
        "Bollnäs tingsrätt": "TBO",
        "Borås TR": "TBO",
        "Byggnadsnämnden": "BYN",
        "Datainspektionen": "DI",
        "Eksjö tingsrätt": "TEK",
        "Energimyndigheten": "ENM",
        "Enköpings TR": "TEN",
        "Enköpings tingsrätt": "TEN",
        "Eskilstuna TR": "TES",
        "Falköpings TR": "TFA",
        "Falköpings tingsrätt": "TFA",
        "Falu TR": "TFA",
        "Fastighetsmäklarnämnden": "FMN",
        "Fastighetstaxeringsnämnden": "FTN",
        "Finansinspektionen": "FI",
        "Forskarskattenämnden": "FSN",
        "Förvaltningsrätten i Falun": "FFA",
        "Förvaltningsrätten i Härnösand": "FHA",
        "Förvaltningsrätten i Jönköping": "FJO",
        "Förvaltningsrätten i Karlstad": "FKA",
        "Förvaltningsrätten i Linköping": "FLI",
        "Förvaltningsrätten i Luleå": "FLU",
        "Förvaltningsrätten i Luleå, migrationsdomstolen": "FLUM",
        "Förvaltningsrätten i Skåne län": "FSK",
        "Förvaltningsrätten i Umeå": "FUM",
        "Förvaltningsrätten i Uppsala": "FUP",
        "Förvaltningsrätten i Växjö": "FVA",
        "Gotlands TR": "TGO",
        "Gällivare TR": "TGÄ",
        "Gällivare tingsrätt": "TGÄ",
        "Gävle TR": "TGÄ",
        "Hallsbergs TR": "THA",
        "Halmstads TR": "THA",
        "Handens TR": "THA",
        "Handens tingsrätt": "THA",
        "Haparanda TR": "THA",
        "Haparanda tingsrätt": "THA",
        "Hedemora TR": "THE",
        "Helsingborgs TR": "THE",
        "Huddinge TR": "THU",
        "Huddinge tingsrätt": "THU",
        "Hudiksvalls TR": "THU",
        "Härnösands TR": "THÄ",
        "Härnösands tingsrätt": "THÄ",
        "Hässleholms TR": "THÄ",
        "Hässleholms tingsrätt": "THÄ",
        "Invandrarverket": "INV",
        "Jakobsbergs TR": "TJA",
        "Jordbruksverket": "JBV",
        "Jämtbygdens TR": "TJÄ",
        "Jönköpings TR": "TJÖ",
        "Kalmar TR": "TKA",
        "Kammarkollegiet": "KK",
        "Karlshamns TR": "TKA",
        "Karlskoga TR": "TKA",
        "Karlskoga tingsrätt": "TKA",
        "Karlskrona TR": "TKA",
        "Karlskrona tingsrätt": "TKA",
        "Karlstads TR": "TKA",
        "Karlstads tingsrätt": "TKA",
        "Katrineholms TR": "TKA",
        "Katrineholms tingsrätt": "TKA",
        "Klippans TR": "TKL",
        "Klippans tingsrätt": "TKL",
        "Koncessionsnämnden för miljöskydd": "KFM",
        "Kriminalvården": "KRV",
        "Kristianstads TR": "TKR",
        "Kristinehamns TR": "TKR",
        "Kristinehamns tingsrätt": "TKR",
        "Kyrkogårdsnämnden": "KGN",
        "Kyrkogårdsstyrelsen": "KGS",
        "Köpings TR": "TKÖ",
        "Landskrona TR": "TLA",
        "Landskrona tingsrätt": "TLA",
        "Leksands TR": "TLE",
        "Lidköpings TR": "TLI",
        "Lidköpings tingsrätt": "TLI",
        "Lindesbergs TR": "TLI",
        "Linköpings TR": "TLI",
        "Ljungby TR": "TLJ",
        "Ljungby tingsrätt": "TLJ",
        "Ludvika TR": "TLU",
        "Ludvika tingsrätt": "TLU",
        "Luleå TR": "TLU",
        "Lunds TR": "TLU",
        "Lycksele TR": "TLY",
        "Läkemedelsverket": "LMV",
        "Länsrätten i Blekinge län": "LBL",
        "Länsrätten i Gotlands län": "LGO",
        "Länsrätten i Gävleborgs län": "LGÄ",
        "Länsrätten i Göteborg, migrationsdomstolen": "LGÖ",
        "Länsrätten i Hallands län": "LHA",
        "Länsrätten i Jönköpings län": "LJÖ",
        "Länsrätten i Kalmar län": "LKA",
        "Länsrätten i Kristianstads län": "LKR",
        "Länsrätten i Norrbottens län": "LNO",
        "Länsrätten i Skåne": "LSK",
        "Länsrätten i Skåne län, migrationsdomstolen": "LSK",
        "Länsrätten i Stockholm": "LST",
        "Länsrätten i Stockholm län": "LST",
        "Länsrätten i Stockholm, migrationsdomstolen": "LSTM",
        "Länsrätten i Västernorrlands län": "LVÄ",
        "Länsrätten i Örebro län": "LÖR",
        "Länsrätten i Östergötlands län": "LÖS",
        "Länsstyrelsen i Dalarnas län": "LSTD",
        "Länsstyrelsen i Stockholms län": "LSTS",
        "Mariestads TR": "TMA",
        "Mjölby TR": "TMJ",
        "Mora TR": "TMO",
        "Motala TR": "TMO",
        "Mölndals TR": "TMÖ",
        "Mölndals tingsrätt": "TMÖ",
        "Nacka TR": "TNA",
        "Nacka tingsrätt, mark- och miljödomstolen": "TNAM",
        "Nacka tingsrätt, miljödomstolen": "TNAM",
        "Norrköpings TR": "TNO",
        "Norrtälje tingsrätt": "TNO",
        "Nyköpings TR": "TNY",
        "Omsorgsnämnden i Trollhättans kommun": "OMS",
        "Oskarshamns TR": "TOS",
        "Oskarshamns tingsrätt": "TOS",
        "Piteå TR": "TPI",
        "Polismyndigheten": "POL",
        "RTV": "RTV",
        "Regeringen": "REG",
        "Revisorsnämnden": "REV",
        "Ronneby TR": "TRO",
        "Ronneby tingsrätt": "TRO",
        "Rättsskyddscentralen": "RSC",
        "Sala TR": "TSA",
        "Sandvikens TR": "TSA",
        "Simrishamns TR": "TSI",
        "Sjuhäradsbygdens TR": "TSJ",
        "Sjuhäradsbygdens tingsrätt": "TSJ",
        "Skattemyndigheten": "SKM",
        "Skattemyndigheten i Luleå": "SKML",
        "Skattverket": "SKV",
        "Skellefteå TR": "TSK",
        "Skellefteå tingsrätt": "TSK",
        "Skövde tingsrätt": "TSK",
        "Socialnämnden": "SON",
        "Socialstyrelsen": "SOS",
        "Sollefteå TR": "TSO",
        "Sollentuna TR": "TSO",
        "Sollentuna tingsrätt": "TSO",
        "Solna TR": "TSO",
        "Statens jordbruksverk": "SJV",
        "Stenungsunds TR": "TST",
        "Stenungsunds tingsrätt": "TST",
        "Stockholm tingsrätt": "TST",
        "Strömstads tingsrätt": "TST",
        "Sundsvalls TR": "TSU",
        "Sunne TR": "TSU",
        "Sunne tingsrätt": "TSU",
        "Svegs TR": "TSV",
        "Södertälje TR": "TSÖ",
        "Södra Roslags tingsrätt": "TSÖ",
        "Sölvesborgs TR": "TSÖ",
        "Tierps TR": "TTI",
        "Tierps tingsrätt": "TTI",
        "Trafiknämnden": "TRN",
        "Transportstyrelsen": "TS",
        "Trelleborgs TR": "TTR",
        "Trelleborgs tingsrätt": "TTR",
        "Trollhättans TR": "TTR",
        "Tullverket": "TV",
        "Uddevalla TR": "TUD",
        "Umeå TR": "TUM",
        "Umeå tingsrätt, mark- och miljödomstolen": "TUMM",
        "Ungdomsstyrelsen": "US",
        "Uppsala TR": "TUP",
        "Varbergs TR": "TVA",
        "Vattenöverdomstolen": "VÖD",
        "Vänersborgs TR": "TVÄ",
        "Vänersborgs tingsrätt, Miljödomstolen": "TVÄM",
        "Vänersborgs tingsrätt, mark- och miljödomstolen": "TVÄM",
        "Värnamo TR": "TVÄ",
        "Värnamo tingsrätt": "TVÄ",
        "Västerviks TR": "TVÄ",
        "Västerås TR": "TVÄ",
        "Västerås tingsrätt": "TVÄ",
        "Växjö TR": "TVÄ",
        "Växjö tingsrätt, mark- och miljödomstolen": "TVÄM",
        "Växjö tingsrätt, miljödomstolen": "TVÄM",
        "Ystads TR": "TYS",
        "Ystads tingsrätt": "TYS",
        "hovrätten för Västra Sverige": "HVS",
        "kammarrätten i Göteborg": "KGO",
        "länsrätten i Skåne län, migrationsdomstolen": "LSKM",
        "länsstyrelsen": "LST",
        "migrationsdomstolen": "MD",
        "regeringen": "REG",
        "skattemyndigheten": "SKM",
        "Ängelholms TR": "TÄN",
        "Åmåls TR": "TÅM",
        "Örebro TR": "TÖR",
        "Örnsköldsviks tingsrätt": "TÖR",
        "Östersunds TR": "TÖS"
    }

    def construct_id(self, node, state):
        if isinstance(node, Delmal):
            state = dict(state)
            node.uri = state['uri'] + "#" + node.ordinal
        elif isinstance(node, Instans):
            if node.court:
                state = dict(state)
                courtslug = self.courtslugs.get(node.court, "XXX")
                if courtslug == "XXX":
                    self.log.warning("%s No slug defined for court %s" % (state["basefile"], node.court))
                if "#" not in state['uri']:
                    state['uri'] += "#"
                else:
                    state['uri'] += "/"
                node.uri = state['uri'] + courtslug
            else:
                return state
        elif isinstance(node, OrderedParagraph):
            separator = "/" if "#" in state['uri'] else "#"
            node.uri = state['uri'] + separator + "P" + node.ordinal
            return state
        elif isinstance(node, (Body, Dom, Domskal)):
            return state
        else:
            return None
        state['uri'] = node.uri
        return state

    
    def visitor_functions(self, basefile):
        return ((self.construct_id, {'uri': self._canonical_uri,
                                     'basefile': basefile}),
                )

    def facets(self):
        # NOTE: it's important that RPUBL.rattsfallspublikation is the
        # first facet (toc_pagesets depend on it)
        def myselector(row, binding, resource_graph=None):
            return (util.uri_leaf(row['rpubl_rattsfallspublikation']),
                    row['rpubl_arsutgava'])

        # FIXME: This isn't used anymore -- when was it used and by which facet?
        def mykey(row, binding, resource_graph=None):
            if binding == "main":
                # we'd really like
                # rpubl:VagledandeDomstolsavgorande/rpubl:avgorandedatum,
                # but that requires modifying facet_query
                return row['update']
            else:
                return util.split_numalpha(row['dcterms_identifier'])

        return [Facet(RPUBL.rattsfallspublikation,
                      indexingtype=fulltextindex.Resource(),
                      use_for_toc=True,
                      use_for_feed=True,
                      selector=myselector,  # => ("ad","2001"), ("nja","1981")
                      key=Facet.resourcelabel,
                      identificator=Facet.defaultselector,
                      dimension_type='ref'),
                Facet(RPUBL.referatrubrik,
                      indexingtype=fulltextindex.Text(boost=4),
                      toplevel_only=True,
                      use_for_toc=False),
                Facet(DCTERMS.identifier,
                      use_for_toc=False),
                Facet(RPUBL.arsutgava,
                      indexingtype=fulltextindex.Label(),
                      use_for_toc=False,
                      selector=Facet.defaultselector,
                      key=Facet.defaultselector,
                      dimension_type='value'),
                Facet(RDF.type,
                      use_for_toc=False,
                      use_for_feed=True,
                      # dimension_label="main", # FIXME:
                      # dimension_label must be calculated as rdf_type
                      # or else the data from faceted_data() won't be
                      # usable by wsgi.stats
                      # key=  # FIXME add useful key method for sorting docs
                      identificator=lambda x, y, z: None),
                Facet(RPUBL.avgorandedatum,  # we need this data when
                                             # creating feeds, but not
                                             # to sort/group by
                      use_for_toc=False,
                      use_for_feed=False)
                ] + self.standardfacets

    def facet_query(self, context):
        query = super(DV, self).facet_query(context)
        # FIXME: This is really hacky, but the rpubl:avgorandedatum
        # that we need is not a property of the root resource, but
        # rather a linked resource. So we postprocess the query to get
        # at that linked resource
        return query.replace("?uri rpubl:avgorandedatum ?rpubl_avgorandedatum",
                             "?uri rpubl:referatAvDomstolsavgorande ?domuri . ?domuri rpubl:avgorandedatum ?rpubl_avgorandedatum")

    def _relate_fulltext_resources(self, body):
        res = []
        uris = set()
        for r in body.findall(".//*[@about]"):
            if r.get("class") == "bodymeta":
                continue
            if r.get("about") not in uris:
                uris.add(r.get("about"))
                res.append(r)
        return [body] + res

    _relate_fulltext_value_cache = {}
    def _relate_fulltext_value(self, facet, resource, desc):
        def rootlabel(desc):
            about = desc._subjects[-1]
            try:
                if "#" in about:
                    desc.about(URIRef(str(about).split("#", 1)[0]))
                return desc.getvalue(DCTERMS.identifier)
            finally:
                desc.about(about)

        if facet.dimension_label in ("label", "comment", "creator", "issued"):
            # "creator" and "issued" should be identical for the root
            # resource and all contained subresources. "label" and "comment" can
            # change slighly.
            resourceuri = resource.get("about")
            rooturi = resourceuri.split("#")[0]
            if "#" not in resourceuri:
                l = rootlabel(desc)
                desc.about(desc.getrel(RPUBL.referatAvDomstolsavgorande))
                self._relate_fulltext_value_cache[rooturi] = {
                    "creator": desc.getrel(DCTERMS.publisher),
                    "issued": desc.getvalue(RPUBL.avgorandedatum),
                    "label": l,
                    "comment": l,
                }
                desc.about(resourceuri)
            v = self._relate_fulltext_value_cache[rooturi][facet.dimension_label]
            
            if "#" in resourceuri and facet.dimension_label in ("label", "comment"):
                if "/P" not in resourceuri.split("#",1)[1]:
                    if desc.getvalues(DCTERMS.creator):
                        court = desc.getvalue(DCTERMS.creator)
                    else:
                        court = resource.get("about").split("#")[1]
                    self._relate_fulltext_value_cache[resourceuri] = {
                        "label": court,
                        "comment": "%s: %s" % (v, court)
                        }
                    v = self._relate_fulltext_value_cache[resourceuri].get(facet.dimension_label, None)
                else:
                    decisionuri, part = resourceuri.split("/P", 1)
                    v = "%s, punkt %s" % (self._relate_fulltext_value_cache[decisionuri][facet.dimension_label], part)
            return facet.dimension_label, v
        else:
            return super(DV, self)._relate_fulltext_value(facet, resource, desc)

    def tabs(self):
        return [("Vägledande rättsfall", self.dataset_uri())]
