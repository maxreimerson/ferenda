#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import unicode_literals
import sys, os
if sys.version_info < (2,7,0):
    import unittest2 as unittest
else:
    import unittest
if os.getcwd() not in sys.path: sys.path.insert(0,os.getcwd())

import rdflib

#SUT
from ferenda import Document

class TestDocument(unittest.TestCase):
    def test_init(self):
        d = Document()
        self.assertIsInstance(d.meta, rdflib.Graph)
        self.assertEqual(d.body, [])
        self.assertIsNone(d.uri)
        self.assertIsNone(d.lang)
        self.assertIsNone(d.basefile)
