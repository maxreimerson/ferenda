# -*- coding: utf-8 -*-
from __future__ import (absolute_import, division,
                        print_function, unicode_literals)
from builtins import *

import os
import sys
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from zipfile import ZipFile

from ferenda.compat import unittest

#SUT
from ferenda import DocumentStore, DocumentEntry
from ferenda import util
from ferenda.errors import *

class Store(unittest.TestCase):
    def setUp(self):
        self.datadir = tempfile.mkdtemp()
        self.store = DocumentStore(self.datadir)

    def tearDown(self):
        shutil.rmtree(self.datadir)

    def p(self,path):
        path = self.datadir+"/"+path
        return path.replace('/', '\\') if os.sep == '\\' else path

    def test_open(self):
        wanted_filename = self.store.path("basefile", "maindir", ".suffix")
        with self.store.open("basefile", "maindir", ".suffix", "w") as fp:
            self.assertNotEqual(fp.name, wanted_filename)
            self.assertEqual(fp.realname, wanted_filename)
            fp.write("This is the data")
        self.assertEqual(util.readfile(wanted_filename),
                         "This is the data")
        mtime = os.stat(wanted_filename).st_mtime

        # make sure that the open method also can be used
        with self.store.open("basefile", "maindir", ".suffix") as fp:
            self.assertEqual("This is the data",
                             fp.read())

        # make sure writing identical content does not actually write
        # a new file
        time.sleep(.1) # just to get a different mtime
        with self.store.open("basefile", "maindir", ".suffix", "w") as fp:
            fp.write("This is the data")
        self.assertEqual(os.stat(wanted_filename).st_mtime,
                         mtime)

        # make sure normal
        fp = self.store.open("basefile", "maindir", ".suffix", "w")
        fp.write("This is the new data")
        fp.close()
        self.assertEqual(util.readfile(wanted_filename),
                         "This is the new data")
        

    def test_open_binary(self):
        wanted_filename = self.store.path("basefile", "maindir", ".suffix")
        # the smallest possible PNG image
        bindata = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
        with self.store.open("basefile", "maindir", ".suffix", "wb") as fp:
            fp.write(bindata)

        mimetype = util.runcmd("file -b --mime-type %s" % wanted_filename)[1]
        self.assertEqual("image/png", mimetype.strip())

        # make sure that the open method also can be used
        with self.store.open("basefile", "maindir", ".suffix", "rb") as fp:
            self.assertEqual(bindata, fp.read())

    
    def test_path(self):
        self.assertEqual(self.store.path("123","foo", ".bar"),
                         self.p("foo/123.bar"))
        self.assertEqual(self.store.path("123/a","foo", ".bar"),
                         self.p("foo/123/a.bar"))
        self.assertEqual(self.store.path("123:a","foo", ".bar"),
                         self.p("foo/123/%3Aa.bar"))
        realsep  = os.sep
        try:
            os.sep = "\\"
            self.assertEqual(self.store.path("123", "foo", ".bar"),
                             self.datadir.replace("/", os.sep) + "\\foo\\123.bar")
        finally:
            os.sep = realsep


    def test_path_version(self):
        eq = self.assertEqual
        eq(self.store.path("123","foo", ".bar", version="42"),
           self.p("archive/foo/123/.versions/42.bar"))
        eq(self.store.path("123/a","foo", ".bar", version="42"),
           self.p("archive/foo/123/a/.versions/42.bar"))
        eq(self.store.path("123:a","foo", ".bar", version="42"),
           self.p("archive/foo/123/%3Aa/.versions/42.bar"))
        eq(self.store.path("123:a","foo", ".bar", version="42:1"),
           self.p("archive/foo/123/%3Aa/.versions/42/%3A1.bar"))
        self.store.storage_policy = "dir"
        eq(self.store.path("123","foo", ".bar", version="42"),
           self.p("archive/foo/123/.versions/42/index.bar"))
        eq(self.store.path("123/a","foo", ".bar", version="42"),
           self.p("archive/foo/123/a/.versions/42/index.bar"))
        eq(self.store.path("123:a","foo", ".bar", version="42"),
           self.p("archive/foo/123/%3Aa/.versions/42/index.bar"))
        eq(self.store.path("123:a","foo", ".bar", version="42:1"),
           self.p("archive/foo/123/%3Aa/.versions/42/%3A1/index.bar"))
            

    def test_path_attachment(self):
        eq = self.assertEqual
        repo = self.store # to shorten lines < 80 chars
        repo.storage_policy = "dir" # attachments require this
        eq(repo.path("123","foo", None, attachment="external.foo"),
           self.p("foo/123/external.foo"))
        eq(repo.path("123/a","foo", None, attachment="external.foo"),
           self.p("foo/123/a/external.foo"))
        eq(repo.path("123:a","foo", None, attachment="external.foo"),
           self.p("foo/123/%3Aa/external.foo"))
        
        with self.assertRaises(AttachmentNameError):
            repo.path("123:a","foo", None,
                              attachment="invalid:attachment")

        with self.assertRaises(AttachmentNameError):
           repo.path("123:a","foo", None,
                             attachment="invalid/attachment"), 

        repo.storage_policy = "file"
        with self.assertRaises(AttachmentPolicyError):
           repo.path("123:a","foo", None,
                             attachment="external.foo"), 

    def test_path_version_attachment(self):
        eq = self.assertEqual
        self.store.storage_policy = "dir"
        eq(self.store.path("123","foo", None,
                                  version="42", attachment="external.foo"),
           self.p("archive/foo/123/.versions/42/external.foo"))
        eq(self.store.path("123/a","foo", None,
                                  version="42", attachment="external.foo"),
           self.p("archive/foo/123/a/.versions/42/external.foo"))

        eq(self.store.path("123:a","foo", None,
                                  version="42", attachment="external.foo"),
           self.p("archive/foo/123/%3Aa/.versions/42/external.foo"))
        
        
    def test_specific_path_methods(self):
        self.assertEqual(self.store.downloaded_path('123/a'),
                         self.p("downloaded/123/a.html"))
        self.assertEqual(self.store.downloaded_path('123/a', version="1"),
                         self.p("archive/downloaded/123/a/.versions/1.html"))
        self.assertEqual(self.store.parsed_path('123/a', version="1"),
                         self.p("archive/parsed/123/a/.versions/1.xhtml"))
        self.assertEqual(self.store.generated_path('123/a', version="1"),
                         self.p("archive/generated/123/a/.versions/1.html"))
        self.store.storage_policy = "dir"
        self.assertEqual(self.store.downloaded_path('123/a'),
                         self.p("downloaded/123/a/index.html"))
        self.assertEqual(self.store.downloaded_path('123/a', version="1"),
                         self.p("archive/downloaded/123/a/.versions/1/index.html"))
        self.assertEqual(self.store.parsed_path('123/a', version="1"),
                         self.p("archive/parsed/123/a/.versions/1/index.xhtml"))
        self.assertEqual(self.store.generated_path('123/a', version="1"),
                         self.p("archive/generated/123/a/.versions/1/index.html"))

           
    def test_basefile_to_pathfrag(self):
        self.assertEqual(self.store.basefile_to_pathfrag("123-a"), "123-a")
        self.assertEqual(self.store.basefile_to_pathfrag("123/a"), "123/a")
        self.assertEqual(self.store.basefile_to_pathfrag("123:a"), "123"+os.sep+"%3Aa")

    def test_pathfrag_to_basefile(self):
        self.assertEqual(self.store.pathfrag_to_basefile("123-a"), "123-a")
        self.assertEqual(self.store.pathfrag_to_basefile("123/a"), "123/a")
        self.assertEqual(self.store.pathfrag_to_basefile("123/%3Aa"), "123:a")

        try:
            # make sure the pathfrag method works as expected even when os.sep is not "/"
            realsep = os.sep
            os.sep = "\\"
            self.assertEqual(self.store.pathfrag_to_basefile("123\\a"), "123/a")
        finally:
            os.sep = realsep

    def test_list_basefiles_file(self):
        files = ["downloaded/123/a.html",
                 "downloaded/123/b.html",
                 "downloaded/124/a.html",
                 "downloaded/124/b.html"]
        basefiles = ["124/b", "124/a", "123/b", "123/a"]
        for f in files:
            util.writefile(self.p(f),"Nonempty")
        self.assertEqual(list(self.store.list_basefiles_for("parse")),
                         basefiles)

    def test_list_basefiles_parse_dir(self):
        files = ["downloaded/123/a/index.html",
                 "downloaded/123/b/index.html",
                 "downloaded/124/a/index.html",
                 "downloaded/124/b/index.html"]
        basefiles = ["124/b", "124/a", "123/b", "123/a"]

        self.store.storage_policy = "dir"
        for f in files:
            p = self.p(f)
            util.writefile(p,"nonempty")
        self.assertEqual(list(self.store.list_basefiles_for("parse")),
                         basefiles)

    def test_list_basefiles_generate_dir(self):
        files = ["parsed/123/a/index.xhtml",
                 "parsed/123/b/index.xhtml",
                 "parsed/124/a/index.xhtml",
                 "parsed/124/b/index.xhtml"]
        basefiles = ["124/b", "124/a", "123/b", "123/a"]

        self.store.storage_policy = "dir"
        for f in files:
            util.writefile(self.p(f),"nonempty")
        self.assertEqual(list(self.store.list_basefiles_for("generate")),
                         basefiles)

    def test_list_basefiles_postgenerate_file(self):
        files = ["generated/123/a.html",
                 "generated/123/b.html",
                 "generated/124/a.html",
                 "generated/124/b.html"]
        basefiles = ["124/b", "124/a", "123/b", "123/a"]
        for f in files:
            util.writefile(self.p(f),"nonempty")
        self.assertEqual(list(self.store.list_basefiles_for("_postgenerate")),
                         basefiles)

    def test_list_basefiles_invalid(self):
        with self.assertRaises(ValueError):
            list(self.store.list_basefiles_for("invalid_action"))

    def test_list_versions_file(self):
        files = ["archive/downloaded/123/a/.versions/1.html",
                 "archive/downloaded/123/a/.versions/2.html",
                 "archive/downloaded/123/a/.versions/2bis.html",
                 "archive/downloaded/123/a/.versions/10.html"]
        versions = ["1","2", "2bis", "10"]
        for f in files:
            util.writefile(self.p(f),"nonempty")
            # list_versions(action, basefile)
        self.assertEqual(list(self.store.list_versions("123/a","downloaded")),
                         versions)

    def test_list_versions_dir(self):
        files = ["archive/downloaded/123/a/.versions/1/index.html",
                 "archive/downloaded/123/a/.versions/2/index.html",
                 "archive/downloaded/123/a/.versions/2bis/index.html",
                 "archive/downloaded/123/a/.versions/10/index.html"]
        basefiles = ['123/a']
        versions = ["1","2", "2bis", "10"]
        for f in files:
            util.writefile(self.p(f),"nonempty")
        self.store.storage_policy = "dir"
        self.assertEqual(list(self.store.list_versions("123/a", "downloaded")),
                         versions)

    def test_list_complicated_versions(self):
        # the test here is that basefile + version might be ambigious
        # as to where to split unless we add the reserved .versions
        # directory
        a_files = ["archive/downloaded/123/.versions/a/27.html",
                 "archive/downloaded/123/.versions/a/27/b"]
        b_files = ["archive/downloaded/123/b/.versions/27.html",
                 "archive/downloaded/123/b/.versions/27/b.html"]
        a_versions = ["a/27", "a/27/b"]
        b_versions = ["27", "27/b"]
        for f in a_files + b_files:
            util.writefile(self.p(f),"nonempty")
        self.assertEqual(list(self.store.list_versions("123","downloaded")),
                         a_versions)
        self.assertEqual(list(self.store.list_versions("123/b","downloaded")),
                         b_versions)
        

    def test_list_attachments(self):
        self.store.storage_policy = "dir" # attachments require this
        files = ["downloaded/123/a/index.html",
                 "downloaded/123/a/attachment.html",
                 "downloaded/123/a/appendix.pdf",
                 "downloaded/123/a/other.txt"]
        basefiles = ['123/a']
        attachments = ['appendix.pdf', 'attachment.html', 'other.txt']
        for f in files:
            util.writefile(self.p(f),"nonempty")
            # list_attachments(action, basefile, version=None)
        self.assertEqual(list(self.store.list_attachments("123/a", "downloaded")),
                         attachments)

    def test_list_invalid_attachments(self):
        # test that files with an invalid suffix (in
        # store.invalid_suffixes) is not listed
        self.store.storage_policy = "dir" # attachments require this
        files = ["downloaded/123/a/index.html",
                 "downloaded/123/a/index.invalid",
                 "downloaded/123/a/other.invalid",
                 "downloaded/123/a/other.txt"]
        basefiles = ['123/a']
        attachments = ['other.txt']
        for f in files:
            util.writefile(self.p(f),"nonempty")
            # list_attachments(action, basefile, version=None)
        self.assertEqual(list(self.store.list_attachments("123/a", "downloaded")),
                         attachments)
        
    def test_list_attachments_version(self):
        self.store.storage_policy = "dir" # attachments require this
        files = ["archive/downloaded/123/a/.versions/1/index.html",
                 "archive/downloaded/123/a/.versions/1/attachment.txt",
                 "archive/downloaded/123/a/.versions/2/index.html",
                 "archive/downloaded/123/a/.versions/2/attachment.txt",
                 "archive/downloaded/123/a/.versions/2/other.txt"]
        basefiles = ['123/a']
        versions = ['1','2']
        attachments_1 = ['attachment.txt']
        attachments_2 = ['attachment.txt', 'other.txt']
        for f in files:
            util.writefile(self.p(f),"nonempty")

        self.assertEqual(list(self.store.list_attachments("123/a","downloaded",
                                                         "1")),
                         attachments_1)
        self.assertEqual(list(self.store.list_attachments("123/a","downloaded",
                                                         "2")),
                         attachments_2)


class Compression(unittest.TestCase):
    maxDiff = 2048
    compression = None
    expected_suffix = ""
    expected_mimetype = "text/plain"
    dummytext = """For applications that require data compression, the functions in this module allow compression and decompression, using the zlib library. The zlib library has its own home page at http://www.zlib.net. There are known incompatibilities between the Python module and versions of the zlib library earlier than 1.1.3; 1.1.3 has a security vulnerability, so we recommend using 1.1.4 or later.

zlib’s functions have many options and often need to be used in a particular order. This documentation doesn’t attempt to cover all of the permutations; consult the zlib manual at http://www.zlib.net/manual.html for authoritative information.

For reading and writing .gz files see the gzip module.
"""
    
    def p(self,path):
        path = self.datadir+"/"+path
        return path.replace('/', '\\') if os.sep == '\\' else path

    def setUp(self):
        self.datadir = tempfile.mkdtemp()
        self.store = DocumentStore(self.datadir, compression=self.compression)

    def test_intermediate_path(self):
        self.assertEqual(self.p("intermediate/123/a.xml" + self.expected_suffix),
                         self.store.intermediate_path('123/a'))

    def test_intermediate_path_selectsuffix(self):
        self.store.intermediate_suffixes = [".html", ".xhtml"]
        util.writefile(self.p("intermediate/123/a.html"), self.dummytext)
        self.assertEqual(self.p("intermediate/123/a.html") + self.expected_suffix,
                         self.store.intermediate_path('123/a'))

    def test_open_intermediate_path(self):
        self.store.intermediate_suffixes = [".html", ".xhtml"]
        with self.store.open_intermediate("123/a", mode="w", suffix=".xhtml") as fp:
            fp.write(self.dummytext)
        filename = self.p("intermediate/123/a.xhtml" + self.expected_suffix)
        self.assertTrue(os.path.exists(filename))
        mimetype = util.runcmd("file -b --mime-type %s" % filename)[1]
        self.assertIn(mimetype.strip(), self.expected_mimetype)
        with self.store.open_intermediate("123/a") as fp:
            # note, open_intermediate should open the file with the
            # the .xhtml suffix automatically
            self.assertEqual(self.dummytext, fp.read())

class GzipCompression(Compression):
    compression = "gz"
    expected_suffix = ".gz"
    expected_mimetype = ("application/x-gzip", "application/gzip")

class Bzip2Compression(Compression):
    compression = "bz2"
    expected_suffix = ".bz2"
    expected_mimetype = ("application/x-bzip2",)

@unittest.skipIf(sys.version_info < (3, 0, 0), "LZMAFile not available in py2")
class XzCompression(Compression):
    compression = "xz"
    expected_suffix = ".xz"
    expected_mimetype = ("application/x-xz",)
    


class Needed(unittest.TestCase):
    def setUp(self):
        self.datadir = tempfile.mkdtemp()
        self.store = DocumentStore(self.datadir)

    def tearDown(self):
        shutil.rmtree(self.datadir)

    def create_file(self, path, timestampoffset=0, content="dummy"):
        util.ensure_dir(path)
        with open(path, "w") as fp:
            fp.write(content)
        if timestampoffset:
            os.utime(path, (time.time(), time.time() + timestampoffset))
        
    def test_parse_not_needed(self):
        self.create_file(self.store.downloaded_path("a"))
        self.create_file(self.store.parsed_path("a"), 3600)
        self.assertFalse(self.store.needed("a", "parse"))

    def test_parse_needed(self):
        self.create_file(self.store.downloaded_path("a"))
        res = self.store.needed("a", "parse")
        self.assertTrue(res)
        self.assertIn("outfile doesn't exist", res.reason)

    def test_parse_needed_outdated(self):
        self.create_file(self.store.downloaded_path("a"))
        self.create_file(self.store.parsed_path("a"), -3600)
        res = self.store.needed("a", "parse")
        self.assertTrue(res)
        self.assertIn("is newer than outfile", res.reason)

    def create_entry(self, basefile, timestampoffset=0):
        # create a entry file with indexed_{ft,ts,dep} set to the
        # current time with optional offset. Also
        # .status['generated']['date'], to test needed(...,
        # 'transformlinks')
        de = DocumentEntry(self.store.documententry_path(basefile))
        delta = timedelta(seconds=timestampoffset)
        ts = datetime.now() + delta
        de.indexed_ts = ts
        de.indexed_ft = ts
        de.indexed_dep = ts
        de.updated = ts
        de.status['generate'] = {'date': ts}
        de.save()

    def test_relate_not_needed(self):
        self.create_entry("a")
        self.create_file(self.store.distilled_path("a"), -3600)
        self.create_file(self.store.parsed_path("a"), -3600)
        self.create_file(self.store.dependencies_path("a"), -3600)
        self.assertFalse(self.store.needed("a", "relate"))

    def test_relate_needed_ts(self):
        self.create_entry("a", -3600)
        self.create_file(self.store.distilled_path("a"))
        self.create_file(self.store.parsed_path("a"), -7200)
        self.create_file(self.store.dependencies_path("a"), -7200)
        res = self.store.needed("a", "relate")
        self.assertTrue(res)
        self.assertIn("is newer than indexed_ts in documententry", res.reason)

    def test_relate_needed_ft(self):
        self.create_entry("a", -3600)
        self.create_file(self.store.distilled_path("a"), -7200)
        self.create_file(self.store.parsed_path("a"))
        self.create_file(self.store.dependencies_path("a"), -7200)
        res = self.store.needed("a", "relate")
        self.assertTrue(res)
        self.assertIn("is newer than indexed_ft in documententry", res.reason)

    def test_relate_needed_dep(self):
        self.create_entry("a", -3600)
        self.create_file(self.store.distilled_path("a"), -7200)
        self.create_file(self.store.parsed_path("a"), -7200)
        self.create_file(self.store.dependencies_path("a"))
        res = self.store.needed("a", "relate")
        self.assertTrue(res)
        self.assertIn("is newer than indexed_dep in documententry", res.reason)
        
    def test_generate_not_needed(self):
        self.create_file(self.store.parsed_path("a"))
        self.create_file(self.store.generated_path("a"), 3600)
        self.assertFalse(self.store.needed("a", "generate"))

    def test_generate_not_needed_404(self):
        self.create_file(self.store.parsed_path("a"))
        self.create_file(self.store.generated_path("a") + ".404", 3600)
        self.assertFalse(self.store.needed("a", "generate"))

    def test_generate_needed(self):
        self.create_file(self.store.parsed_path("a"))
        res = self.store.needed("a", "generate")
        self.assertTrue(res)
        self.assertIn("outfile doesn't exist", res.reason)

    def test_generate_needed_outdated(self):
        self.create_file(self.store.parsed_path("a"))
        self.create_file(self.store.generated_path("a"), -3600)
        res = self.store.needed("a", "generate")
        self.assertTrue(res)
        self.assertIn("is newer than outfile", res.reason)

    def test_generate_needed_outdated_dep(self):
        self.create_file(self.store.parsed_path("a"), -3600)
        self.create_file(self.store.generated_path("a"), -1800)
        dependency_path = self.store.datadir + os.sep + "blahonga.txt"
        self.create_file(self.store.dependencies_path("a"), -3600, dependency_path)
        # create a arbitrary dependency file which is newer than outfile
        self.create_file(dependency_path)
        res = self.store.needed("a", "generate")
        self.assertTrue(res)
        self.assertIn("is newer than outfile", res.reason)

    def test_transformlinks_needed(self):
        self.create_file(self.store.generated_path("a"))
        self.create_entry("a")
        res = self.store.needed("a", "transformlinks")
        self.assertTrue(res)
        self.assertIn("has not been modified after generate", res.reason)

    def test_transformlinks_not_needed(self):
        self.create_entry("a", -3600)
        self.create_file(self.store.generated_path("a"))
        self.assertFalse(self.store.needed("a", "transformlinks"))


class ZipArchive(Store):

    def setUp(self):
        super(ZipArchive, self).setUp()
        self.datadir = tempfile.mkdtemp()
        self.store = DocumentStore(self.datadir)
        self.store.archiving_policy = "zip"

    def test_archive_path(self):
        self.assertEqual(self.store.archive_path("123/a"),
                         self.p("archive/123/a.zip"))

    def test_path_version(self):
        eq = self.assertEqual
        eq(self.p("archive/123.zip#foo/42.bar"),
           self.store.path("123","foo", ".bar", version="42"))
        eq(self.p("archive/123/a.zip#foo/42.bar"),
           self.store.path("123/a","foo", ".bar", version="42"))
        eq(self.p("archive/123/%3Aa.zip#foo/42.bar"),
           self.store.path("123:a","foo", ".bar", version="42"))
        eq(self.p("archive/123/%3Aa.zip#foo/42/%3A1.bar"),
           self.store.path("123:a","foo", ".bar", version="42:1"))
        self.store.storage_policy = "dir"
        eq(self.p("archive/123.zip#foo/42/index.bar"),
           self.store.path("123","foo", ".bar", version="42"))
        eq(self.p("archive/123/a.zip#foo/42/index.bar"),
           self.store.path("123/a","foo", ".bar", version="42"))
        eq(self.p("archive/123/%3Aa.zip#foo/42/index.bar"),
           self.store.path("123:a","foo", ".bar", version="42"))
        eq(self.p("archive/123/%3Aa.zip#foo/42/%3A1/index.bar"),
           self.store.path("123:a","foo", ".bar", version="42:1"))


    def test_path_version_attachment(self):
        eq = self.assertEqual
        self.store.storage_policy = "dir"
        eq(self.store.path("123","foo", None,
                                  version="42", attachment="external.foo"),
           self.p("archive/123.zip#foo/42/external.foo"))
        eq(self.store.path("123/a","foo", None,
                                  version="42", attachment="external.foo"),
           self.p("archive/123/a.zip#foo/42/external.foo"))

        eq(self.store.path("123:a","foo", None,
                                  version="42", attachment="external.foo"),
           self.p("archive/123/%3Aa.zip#foo/42/external.foo"))
        
        
    def test_specific_path_methods(self):
        self.assertEqual(self.store.downloaded_path('123/a', version="1"),
                         self.p("archive/123/a.zip#downloaded/1.html"))
        self.assertEqual(self.store.parsed_path('123/a', version="1"),
                         self.p("archive/123/a.zip#parsed/1.html"))
        self.assertEqual(self.store.generated_path('123/a', version="1"),
                         self.p("archive/123/a.zip#generated/1.html"))
        self.store.storage_policy = "dir"
        self.assertEqual(self.store.downloaded_path('123/a', version="1"),
                         self.p("archive/123/a.zip#downloaded/1/index.html"))
        self.assertEqual(self.store.parsed_path('123/a', version="1"),
                         self.p("archive/123/a.zip#parsed/1/index.xhtml"))
        self.assertEqual(self.store.generated_path('123/a', version="1"),
                         self.p("archive/123/a.zip#generated/1/index.html"))


    def _writezip(self, zip, files):
        util.ensure_dir(zip)
        with ZipFile(zip, "w") as zipfile:
            for f in files:
                with zipfile.open(f, "w") as fp:
                    fp.write(b"nonempty")
    
    def test_list_versions_file(self):
        self._writezip(self.store.archive_path("123/a"), 
                       ["downloaded/1.html",
                        "downloaded/2.html",
                        "downloaded/2bis.html",
                        "downloaded/10.html"])
        
        versions = ["1","2", "2bis", "10"]
        self.assertEqual(list(self.store.list_versions("123/a","downloaded")),
                         versions)

    def test_list_versions_dir(self):
        self._writezip(self.store.archive_path("123/a"), 
                       ["downloaded/1/index.html",
                        "downloaded/2/index.html",
                        "downloaded/2bis/index.html",
                        "downloaded/10/index.html"])
        versions = ["1","2", "2bis", "10"]
        self.store.storage_policy = "dir"
        self.assertEqual(list(self.store.list_versions("123/a", "downloaded")),
                         versions)

    def test_list_complicated_versions(self):
        # the test here is that basefile + version might be ambigious
        # as to where to split unless we add the reserved .versions
        # directory. This should not even be an issue with the zip archiving_policy, but...
        versions = ["a/27", "a/27/b"]
        b_versions = ["27", "27/b"]
        self._writezip(self.store.archive_path("123"),
                       ["downloaded/a/27.html",
                        "downloaded/a/27/b.html"])
        self._writezip(self.store.archive_path("123/b"),
                       ["downloaded/27.html",
                        "downloaded/27/b.html"])
        self.assertEqual(list(self.store.list_versions("123","downloaded")),
                         versions)
        self.assertEqual(list(self.store.list_versions("123/b","downloaded")),
                         b_versions)
        
        
    def test_list_attachments_version(self):
        self.store.storage_policy = "dir" # attachments require this
        self._writezip(self.store.archive_path("123/a"),
                       ["downloaded/1/index.html",
                        "downloaded/1/attachment.txt",
                        "downloaded/index.html",
                        "downloaded/attachment.txt",
                        "downloaded/other.txt"])
        self.assertEqual(list(self.store.list_attachments("123/a","downloaded", "1")),
                         ['attachment.txt'])
        self.assertEqual(list(self.store.list_attachments("123/a","downloaded", "2")),
                         ['attachment.txt', 'other.txt'])

    def test_write_versions(self):
        # this uses the DocumentStore API
        def write_files(files):
            for f in files:
                m = getattr(self.store, "open_" + f[1])
                with m(f[0], version=f[3], attachment=f[4]) as fp:
                    # path(basefile, maindir, suffix, version=None, attachment=None, storage_policy=None, archiving_policy=None):
                    fp.write("contents of %s (but in a zip file)" % self.store.path(f[0], f[1], f[2], f[3], f[4], archive_policy="file"))
 
        # this uses the raw zipfile API
        def read_files(files):
            for f in files:
                zip = self.store.archive_path(f[0])
                with ZipFile(zip) as zipfile:
                    if self.store.storage_policy == "dir":
                        subpath = "%s/%s%s" % (f[1], f[3], f[2])
                    else: 
                        subpath = "%s/%s/%s" % (f[1], f[3], f[4] or "index" + f[2])
                    with zipfile.open(subpath) as fp:
                        msg = "contents of %s (but in a zip file)" % self.store.path(f[0], f[1], f[2], f[3], f[4], archive_policy="file")
                        self.assertEqual(msg, fp.read())

        files = [
            # [0] = basefile, [1] = maindir, [2] = suffix, [3] = version, [4] = attachment
            ("123/a", "downloaded", ".html", "1", None),
            ("123/a", "downloaded", ".html", "2", None),
        ] 
        write_files(files)
        # assert that self.store.archive_path() is a zipfile with the contents we expect
        read_files(files)
        self.store.storage_policy = "dir"
        write_files([
            ("123/a", "downloaded", ".html", "1", None),
            ("123/a", "downloaded", ".html", "1", "attachment.txt"),
            ("123/a", "downloaded", ".html", "2", None),
            ("123/a", "downloaded", ".html", "2", "attachment.txt"),
            ("123/a", "downloaded", ".html", "2", "other.txt"),
        ])
        # assert that self.store.archive_path() is a zipfile with the contents we expect
        read_files(files)
 
    def test_read_versions(self):
        # do the same thing as test_write_versions but with the Documentstore API/raw zipfile API swapped
        # this uses the DocumentStore API

        def write_files(files):
            for f in files:
                zip = self.store.archive_path(f[0])
                with ZipFile(zip, "w") as zipfile:
                    if self.store.storage_policy == "dir":
                        subpath = "%s/%s%s" % (f[1], f[3], f[2])
                    else: 
                        subpath = "%s/%s/%s" % (f[1], f[3], f[4] or "index" + f[2])
                    with zipfile.open(subpath, "w") as fp:
                        msg = "contents of %s (but in a zip file)" % self.store.path(f[0], f[1], f[2], f[3], f[4], archive_policy="file")
                        fp.write(msg)
 
        def read_files(files):
            for f in files:
                m = getattr(self.store, "open_" + f[1])
                with m(f[0], version=f[3], attachment=f[4]) as fp:
                    msg = "contents of %s (but in a zip file)" % self.store.path(f[0], f[1], f[2], f[3], f[4], archive_policy="file")
                    self.assertEqual(msg, fp.read())



        files = [
            # [0] = basefile, [1] = maindir, [2] = suffix, [3] = version, [4] = attachment
            ("123/a", "downloaded", ".html", "1", None),
            ("123/a", "downloaded", ".html", "2", None),
        ] 
        write_files(files)
        # assert that self.store.archive_path() is a zipfile with the contents we expect
        read_files(files)
        self.store.storage_policy = "dir"
        write_files([
            ("123/a", "downloaded", ".html", "1", None),
            ("123/a", "downloaded", ".html", "1", "attachment.txt"),
            ("123/a", "downloaded", ".html", "2", None),
            ("123/a", "downloaded", ".html", "2", "attachment.txt"),
            ("123/a", "downloaded", ".html", "2", "other.txt"),
        ])
        # assert that self.store.archive_path() is a zipfile with the contents we expect
        read_files(files)

# import doctest
# from ferenda import documentstore
# def load_tests(loader,tests,ignore):
#     tests.addTests(doctest.DocTestSuite(documentstore))
#     return tests
