# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import sys, os
from ferenda.compat import unittest
if os.getcwd() not in sys.path: sys.path.insert(0,os.getcwd())

from datetime import datetime
from tempfile import mkdtemp
import shutil

import whoosh.index
import whoosh.fields

from ferenda import FulltextIndex, DocumentRepository
from ferenda.fulltextindex import Identifier, Datetime, Text, Label, Keywords, Boolean, URI, Resource, Less, More, Between

#----------------------------------------------------------------
#
# Initial testdata and testrepo implementations used by our test cases
#

basic_dataset = [
    {'uri':'http://example.org/doc/1',
     'repo':'base',
     'basefile':'1',
     'title':'First example',
     'identifier':'Doc #1',
     'text':'This is the main text of the document (independent sections excluded)'},
    {'uri':'http://example.org/doc/1#s1',
     'repo':'base',
     'basefile':'1',
     'title':'First sec',
     'identifier':'Doc #1 (section 1)',
     'text':'This is an independent section, with extra section boost'},
    {'uri':'http://example.org/doc/1#s2',
     'repo':'base',
     'basefile':'1',
     'title':'Second sec',
     'identifier':'Doc #1 (section 2)',
     'text':'This is another independent section'},
    {'uri':'http://example.org/doc/1#s1',
     'repo':'base',
     'basefile':'1',
     'title':'First section',
     'identifier':'Doc #1 (section 1)',
     'text':'This is an (updated version of a) independent section, with extra section boost'},
    {'uri':'http://example.org/doc/2',
     'repo':'base',
     'basefile':'2',
     'title':'Second document',
     'identifier':'Doc #2',
     'text':'This is the second document (not the first)'}
    ]

custom_dataset = [
    {'repo':'repo1',
     'basefile':'1',
     'uri':'http://example.org/repo1/1',
     'title':'Title of first document in first repo',
     'identifier':'R1 D1',
     'issued':datetime(2013,2,14,14,6), # important to use real datetime object, not string representation
     'publisher': {'iri': 'http://example.org/publisher/e',
                   'label': 'Examples & son'},
     'category': ['green', 'standards'],
     'text': 'Long text here'},
    {'repo':'repo1',
     'basefile':'2',
     'uri':'http://example.org/repo1/2',
     'title':'Title of second document in first repo',
     'identifier':'R1 D2',
     'issued':datetime(2013,3,4,14,16),
     'publisher': {'iri': 'http://example.org/publisher/e',
                   'label': 'Examples & son'},
     'category': ['suggestions'],
     'text': 'Even longer text here'},
    {'repo':'repo2',
     'basefile':'1',
     'uri':'http://example.org/repo2/1',
     'title':'Title of first document in second repo',
     'identifier':'R2 D1',
     'secret': False,
     'references':'http://example.org/repo2/2',
     'category':['green', 'yellow'],
     'text': 'All documents must have texts'},
    {'repo':'repo2',
     'basefile':'2',
     'uri':'http://example.org/repo2/2',
     'title':'Title of second document in second repo',
     'identifier':'R2 D2',
     'secret': True,
     'references': None,
     'category':['yellow', 'red'],
     'text': 'Even this one'}
    ]

class DocRepo1(DocumentRepository):
    alias = "repo1"
    def get_indexed_properties(self):
        return {'issued':Datetime(),
                'publisher':Resource(),
                'abstract': Text(boost=2),
                'category':Keywords()}

class DocRepo2(DocumentRepository):
    alias = "repo2"
    def get_indexed_properties(self):
        return {'secret':Boolean(),   
                'references': URI(),
                'category': Keywords()}

#----------------------------------------------------------------
#
# The actual test -- note that these do not derive from
# unittest.TestCase. They are used as one of two superclasses to yield
# working TestCase classes, but this way allows us to define a test of
# the API, and then having it run once for each backend configuration.

class BasicIndex(object):

    repos = [DocumentRepository()]

    def test_create(self):
        # setUp calls FulltextIndex.connect, creating the index
        self.assertTrue(self.index.exists())

        # assert that the schema, using our types, looks OK
        want = {'uri':Identifier(),
                'repo':Label(),
                'basefile':Label(),
                'title':Text(boost=4),
                'identifier':Label(boost=16),
                'text':Text()}
        got = self.index.schema()
        self.assertEqual(want,got)

    def test_insert(self):
        self.index.update(**basic_dataset[0])
        self.index.update(**basic_dataset[1])
        self.index.commit()

        self.assertEqual(self.index.doccount(),2)
        self.index.update(**basic_dataset[2])
        self.index.update(**basic_dataset[3]) # updated version of basic_dataset[1]
        self.index.commit() 
        self.assertEqual(self.index.doccount(),3)

        
class BasicQuery(object):

    repos = [DocumentRepository()]

    def load(self, data):
        # print("loading...")
        for doc in data:
            self.index.update(**doc)
            self.index.commit()

    def test_basic(self):
        self.assertEqual(self.index.doccount(),0)
        self.load(basic_dataset)
        self.assertEqual(self.index.doccount(),4)

        res, pager = self.index.query("main")
        self.assertEqual(len(res),1)
        self.assertEqual(res[0]['identifier'], 'Doc #1')
        self.assertEqual(res[0]['uri'], 'http://example.org/doc/1')
        res, pager = self.index.query("document")
        self.assertEqual(len(res),2)
        # Doc #2 contains the term 'document' in title (which is a
        # boosted field), not just in text.
        self.assertEqual(res[0]['identifier'], 'Doc #2')
        res, pager = self.index.query("section")
        # can't get these results when using MockESBasicQuery with
        # CREATE_CANNED=True for some reason...
        if type(self) == ESBasicQuery:
            self.assertEqual(len(res),3)
            # NOTE: ES scores all three results equally (1.0), so it doesn't
            # neccesarily put section 1 in the top
            if isinstance(self, ESBase):
                self.assertEqual(res[0]['identifier'], 'Doc #1 (section 2)') 
            else:
                self.assertEqual(res[0]['identifier'], 'Doc #1 (section 1)')


    def test_fragmented(self):
        self.load([
            {'uri':'http://example.org/doc/3',
             'repo':'base',
             'basefile':'3',
             'title':'Other example',
             'identifier':'Doc #3',
             'text':"""Haystack needle haystack haystack haystack haystack
                       haystack haystack haystack haystack haystack haystack
                       haystack haystack needle haystack haystack."""}
            ])
        res, pager = self.index.query("needle")
        # this should return 1 hit (only 1 document)
        self.assertEqual(1, len(res))
        # that has a fragment connector (' ... ') in the middle
        self.assertIn(' ... ', "".join(str(x) for x in res[0]['text']))
        
    
class CustomIndex(object):

    repos = [DocRepo1(), DocRepo2()]
    
    def test_setup(self):
        # introspecting the schema (particularly if it's derived
        # directly from our definitions, not reverse-engineerded from
        # a Whoosh index on-disk) is useful for eg creating dynamic
        # search forms
        self.assertEqual({'uri':Identifier(),
                          'repo':Label(),
                          'basefile':Label(),
                          'title':Text(boost=4),
                          'identifier':Label(boost=16),
                          'text':Text(),
                          'issued':Datetime(),
                          'publisher':Resource(),
                          'abstract': Text(boost=2),
                          'category': Keywords(),
                          'secret': Boolean(),
                          'references': URI(),
                          'category': Keywords()}, self.index.schema())

    def test_insert(self):
        self.index.update(**custom_dataset[0]) # repo1
        self.index.update(**custom_dataset[2]) # repo2
        self.index.commit()
        self.assertEqual(self.index.doccount(),2)

        res, pager = self.index.query(uri="http://example.org/repo1/1")
        self.assertEqual(len(res), 1)
        self.assertEqual(custom_dataset[0],res[0])

        res, pager = self.index.query(uri="http://example.org/repo2/1")
        self.assertEqual(len(res), 1)
        self.assertEqual(custom_dataset[2],res[0])
        
    
class CustomQuery(object):        

    repos = [DocRepo1(), DocRepo2()]

    def load(self, data):
        for doc in data:
            self.index.update(**doc)
            self.index.commit()

    def test_boolean(self):
        self.load(custom_dataset)
        res, pager = self.index.query(secret=True)
        self.assertEqual(len(res),1)
        self.assertEqual(res[0]['identifier'], 'R2 D2')
        res, pager = self.index.query(secret=False)
        self.assertEqual(len(res),1)
        self.assertEqual(res[0]['identifier'], 'R2 D1')
    
    def test_keywords(self):
        self.load(custom_dataset)
        res, pager = self.index.query(category='green')
        self.assertEqual(len(res),2)
        identifiers = set([x['identifier'] for x in res])
        self.assertEqual(identifiers, set(['R1 D1','R2 D1']))
        
    def test_repo_limited_freetext(self):
        self.load(custom_dataset)
        res, pager = self.index.query('first', repo='repo1')
        self.assertEqual(len(res),2)
        self.assertEqual(res[0]['identifier'], 'R1 D1') # contains the term 'first' twice
        self.assertEqual(res[1]['identifier'], 'R1 D2') #          -""-             once

    def test_repo_dateinterval(self):
        self.load(custom_dataset)

        res, pager = self.index.query(issued=Less(datetime(2013,3,1)))
        self.assertEqual(len(res),1)
        self.assertEqual(res[0]['identifier'], 'R1 D1') 

        res, pager = self.index.query(issued=More(datetime(2013,3,1)))
        self.assertEqual(res[0]['identifier'], 'R1 D2') 

        res, pager = self.index.query(issued=Between(datetime(2013,2,1),datetime(2013,4,1)))
        self.assertEqual(len(res),2)
        identifiers = set([x['identifier'] for x in res])
        self.assertEqual(identifiers, set(['R1 D1','R1 D2']))

#----------------------------------------------------------------
#
# Additional base classes used together with above testcases to yield
# working testcase classes

class ESBase(unittest.TestCase):
    def setUp(self):
        self.maxDiff = None
        self.location = "http://localhost:9200/ferenda/"
        self.index = FulltextIndex.connect("ELASTICSEARCH", self.location, self.repos)

    def tearDown(self):
        self.index.destroy()


class WhooshBase(unittest.TestCase):
    def setUp(self):
        self.location = mkdtemp()
        self.index = FulltextIndex.connect("WHOOSH", self.location, self.repos)

    def tearDown(self):
        self.index.close()
        try:
            self.index.destroy()
        except WindowsError:
            # this happens on Win32 when doing the following sequence of events:
            #
            # i = FulltextIndex.connect("WHOOSH", ...)
            # i.update(...)
            # i.commit()
            # i.update(...)
            # i.commit()
            # i.destroy()
            #
            # Cannot solve this for now. FIXME:
            pass


#----------------------------------------------------------------
#
# The actual testcase classes -- they use multiple inheritance to gain
# both the backend-specific configurations (and the declaration as
# unittest.TestCase classes), and the actual tests. Generally, they
# can be empty (all the magic happens when the classes derive from two
# classes)

class WhooshBasicIndex(BasicIndex, WhooshBase): 
    def test_create(self):
        # First do the basic tests
        super(WhooshBasicIndex,self).test_create()

        # then do more low-level tests
        # 1 assert that some files have been created at the specified location
        self.assertNotEqual(os.listdir(self.location),[])
        # 2 assert that it's really a whoosh index
        self.assertTrue(whoosh.index.exists_in(self.location))

        # 3. assert that the actual schema with whoosh types is, in
        # fact, correct
        got = self.index.index.schema
        want = whoosh.fields.Schema(uri=whoosh.fields.ID(unique=True, stored=True),
                                    repo=whoosh.fields.ID(stored=True),
                                    basefile=whoosh.fields.ID(stored=True),
                                    title=whoosh.fields.TEXT(field_boost=4,stored=True),
                                    identifier=whoosh.fields.ID(field_boost=16,stored=True),
                                    text=whoosh.fields.TEXT(stored=True))
        self.assertEqual(sorted(want.names()), sorted(got.names()))
        for fld in got.names():
            self.assertEqual((fld,want[fld]),(fld,got[fld]))
            
        # finally, try to create again (opening an existing index
        # instead of creating)
        self.index = FulltextIndex.connect("WHOOSH", self.location)

       
class WhooshBasicQuery(BasicQuery, WhooshBase): pass

class ESBasicIndex(BasicIndex, ESBase): pass

class ESBasicQuery(BasicQuery, ESBase): pass

class WhooshCustomIndex(CustomIndex, WhooshBase): pass

class ESCustomIndex(CustomIndex, ESBase): pass

class WhooshCustomQuery(CustomQuery, WhooshBase): pass

class ESCustomQuery(CustomQuery, ESBase): pass
