# -*- coding: utf-8 -*-
from __future__ import (absolute_import, division,
                        print_function, unicode_literals)
from builtins import *

import os
import time
import logging
from collections import defaultdict, OrderedDict

from ferenda import DocumentRepository, DocumentStore
from ferenda import util, errors
from ferenda.decorators import updateentry

class CompositeStore(DocumentStore):
    """Custom store for CompositeRepository objects."""

    def __init__(self, datadir,
                 storage_policy="file",
                 compression=None,
                 docrepo_instances=None):
        self.datadir = datadir  # docrepo.datadir + docrepo.alias
        self.storage_policy = storage_policy
        if not docrepo_instances:
            docrepo_instances = OrderedDict()
        self.docrepo_instances = docrepo_instances
        self.basefiles = defaultdict(set)

    def list_basefiles_for(self, action, basedir=None, force=True):
        if not basedir:
            basedir = self.datadir
        # if action in ("parse", "news"): # NB: since symlinks from
        # <mainrepo>/entries to <subrepo>/entries is now created as
        # part of parse (in .copy_parsed), and possibly even as part
        # of download (see lagen.nu.myndfskr), we only need to query
        # subrepos prior to the parse step
        if action in ("parse"): 
            documents = set()
            for cls, inst in self.docrepo_instances.items():
                for basefile in inst.store.list_basefiles_for(action, force=force):
                    self.basefiles[cls].add(basefile)
                    if basefile not in documents:
                        documents.add(basefile)
                        yield basefile
        else:
            for basefile in super(CompositeStore,
                                  self).list_basefiles_for(action, basedir, force):
                yield basefile

    def remove(self, basefile):
        removed = 0
        for cls, inst in self.docrepo_instances.items():
            removed += inst.store.remove(basefile)
        removed += super(CompositeStore, self).remove(basefile)
        return removed

class CompositeRepository(DocumentRepository):
    """Acts as a proxy for a list of sub-repositories.

    Calls the download() method for each of the included
    subrepos. Parse calls each subrepos parse() method in order until
    one succeeds, unless config.failfast is True. In that case any
    errors from the first subrepo is re-raised.

    """

    subrepos = ()  # list of classes

    """List of respository classes to use."""
    documentstore_class = CompositeStore
    extrabases = ()
    """List of mixin classes to add to each subrepo class."""

    supress_subrepo_logging = True

    def get_instance(self, instanceclass):
        if instanceclass not in self._instances:
            if hasattr(self, '_config'):
                config = self.config
            else:
                # if we don't have a config object yet, the created
                # instance is just temporary -- don't save it
                config = None

            # FIXME: this instance will be using a default
            # ResourceLoader, eg if a subrepo is at foo/bar.py, only
            # foo/bar/res will we in that resourceloaders path. This
            # causes problems, primarily if our CompositeRepository is
            # subclassed to somewhere else, eg subclass/bar.py -- we
            # might want to use resources at subclass/res instead.

            # slightly magical: If our config object has a subsection
            # that matches the instanceclass alias, use that
            # subsection. Same if our config object's parent has a
            # subsection that matches the instanceclass alias.
            if hasattr(config, instanceclass.alias):
                config = getattr(config, instanceclass.alias)
            elif  hasattr(config._parent, instanceclass.alias):
                config = getattr(config._parent, instanceclass.alias)

            inst = instanceclass(config)
            if hasattr(self, '_config') and self.supress_subrepo_logging:
                # if the composite object has loglevel INFO, make the
                # subrepo have a slightly higher loglevel to avoid
                # creating almost-duplicate logging entries like:
                #
                # <time> subrepo1 INFO basefile: parse OK (2.42 sec)
                # <time> comprepo INFO basefile: parse OK (2.52 sec)
                #
                # Although not if the subrepo itself has subrepos
                #
                # NB: This causes problems when using a compositerepo
                # for downloading, since it's only the suprepos that
                # know and report on each individual basefile download
                # (unlike parse/relate/generate, the composite repo
                # never knows about each individual basefile being
                # downloaded and so can't provide a INFO logging of
                # it). We try to work around this in the download
                # method.
                if (self.log.getEffectiveLevel() == logging.INFO and
                    inst.log.getEffectiveLevel() == logging.INFO and
                    not isinstance(inst, CompositeRepository)):
                    customlevel = inst.log.getEffectiveLevel() + 1
                    logging.addLevelName(customlevel, "INFOEX")
                    inst.log.setLevel(customlevel)
            self._instances[instanceclass] = inst
        return self._instances[instanceclass]

    def __init__(self, config=None, **kwargs):
        self._instances = OrderedDict()
        # after this, self.config WILL be set (regardless of whether a
        # config object was provided or not
        super(CompositeRepository, self).__init__(config, **kwargs)

        newsubrepos = []
        for c in self.subrepos:  # populate self._instances
            if self.extrabases:
                bases = [x for x in self.extrabases if x not in c.__bases__]
                bases.append(c)
                c = type(c.__name__, tuple(bases), dict(c.__dict__))
                newsubrepos.append(c)
            if self.loadpath:
                c.loadpath = self.loadpath
            self.get_instance(c)
        if newsubrepos:
            self.subrepos = newsubrepos
        cls = self.documentstore_class

        self.store = cls(self.config.datadir + os.sep + self.alias,
                         storage_policy=self.storage_policy,
                         docrepo_instances=self._instances)
        if self.downloaded_suffix != ".html" and self.store.downloaded_suffixes == [".html"]:
            self.store.downloaded_suffixes = [self.downloaded_suffix]


    @classmethod
    def get_default_options(cls):
        # 1. Get options from superclass (NB: according to MRO...)
        opts = super(CompositeRepository, cls).get_default_options()
        # 2. Add extra options that ONLY exists in subrepos
        for c in cls.subrepos:
            for k, v in c.get_default_options().items():
                if k not in opts:
                    opts[k] = v
        # 3. add the extra 'failfast' option
        opts['failfast'] = False
        return opts

    # FIXME: we have no real need for this property getter override
    # (it's exactly the same as DocumentRepository.config) itself, but
    # since we want to override the setter, we need to use this to
    # define config.setter
    @property
    def config(self):
        return self._config

    @config.setter
    def config(self, config):
        # FIXME: This doesn't work (AttributeError: 'super' object has
        # no attribute 'config'), so we just copy the entire method
        # super(CompositeRepository, self).config = config
        self._config = config
        self.store = self.documentstore_class(
            config.datadir + os.sep + self.alias,
            storage_policy=self.storage_policy,
            docrepo_instances=self._instances)

    def download(self, basefile=None):
        for c in self.subrepos:
            inst = self.get_instance(c)
            # make sure that our store has access to our now
            # initialized subrepo objects
            if c not in self.store.docrepo_instances:
                self.store.docrepo_instances[c] = inst
            try:
                # temporarily re-set the logging level so that the
                # subrepos INFO messages get reported (see note in
                # get_instance).
                loglevel_workaround = False
                if (self.log.getEffectiveLevel() == logging.INFO and
                    inst.log.getEffectiveLevel() == logging.INFO + 1):
                    loglevel_workaround = True
                    inst.log.setLevel(self.log.getEffectiveLevel())
                ret = inst.download(basefile)
                if loglevel_workaround:
                    inst.log.setLevel(self.log.getEffectiveLevel() + 1)
            except Exception as e:  # be resilient
                loc = util.location_exception(e)
                self.log.error("download for %s failed: %s (%s)" % (c.alias, e, loc))
                ret = False
            if basefile and ret:
                # we got the doc we want, we're done!
                return

    # NOTE: this impl should NOT use the @managedparsing decorator --
    # but it can use @updateentry to catch warnings and errors thrown
    # by a subrepo
    @updateentry("parse")
    def parse(self, basefile):
        # first, check if we really need to parse. If any subrepo
        # returns that .store.needed(...., "parse") is false and we
        # have parsed file in the mainrepo, then we're done. This is
        # mainly to avoid the log message below (to be in line with
        # expected repo behaviour of not logging anything at severity
        # INFO if no real work was done), it does not noticably affect
        # performance
        force = (self.config.force is True or
                 self.config.parseforce is True)
        if not force:
            for c in self.subrepos:
                inst = self.get_instance(c)
                needed = inst.store.needed(basefile, "parse")
                if not needed and os.path.exists(self.store.parsed_path(basefile)):
                    self.log.debug("%s: Skipped" % basefile)
                    return True  # signals everything OK

        start = time.time()
        ret = False
        for inst in self.get_preferred_instances(basefile):
            try:
                ret = inst.parse(basefile)
            # Any error thrown (errors.ParseError or something
            # else) means we try next subrepo -- unless we want to
            # fail fast with a nice stacktrace during debugging.
            except Exception as e:
                if self.config.failfast:
                    raise
                else:
                    self.log.debug("%s: parse with %s failed: %s" %
                                   (basefile,
                                    inst.qualified_class_name(),
                                    str(e)))
                    ret = False
            if ret:
                break
            
        if ret:
            oldbasefile = basefile
            if ret is not True and ret != basefile:
                # this is a signal that parse discovered that the
                # basefile was adjusted. We should raise
                # DocumentRenamedError at the very end to get
                # updateentry do the right thing.
                basefile = ret
                # Also, touch the old parsed path so we don't
                # regenerate.
                with self.store.open_parsed(oldbasefile, "w"):
                    pass
                

            self.copy_parsed(basefile, inst)
            self.log.info("%(basefile)s parse OK (%(elapsed).3f sec)",
                          {'basefile': basefile,
                           'elapsed': time.time() - start})

            if basefile != oldbasefile:
                msg = "%s: In subrepo %s basefile turned out to really be %s" % (
                    oldbasefile, inst.qualified_class_name(), basefile)
                raise errors.DocumentRenamedError(True, msg, oldbasefile, basefile)
            return ret
        else:
            # subrepos should only contain those repos that actually
            # had a chance of parsing (basefile in
            # self.store.basefiles[c])
            subrepos_lbl = ", ".join([self.get_instance(x).qualified_class_name()
                                      for x in self.subrepos if basefile in self.store.basefiles[x]])
            if subrepos_lbl:
                raise errors.ParseError(
                    "No instance of %s was able to parse %s" %
                    (subrepos_lbl, basefile))
            else:
                raise errors.ParseError(
                    "No available instance (out of %s) had basefile %s" %
                    (len(self.subrepos), basefile))
                

    def get_preferred_instances(self, basefile):
        for c in self.subrepos:
            inst = self.get_instance(c)
            if (basefile in self.store.basefiles[c] or
                os.path.exists(inst.store.downloaded_path(basefile))):
                yield(inst)

    def copy_parsed(self, basefile, instance):
        # If the distilled and parsed links are recent, assume that
        # all external resources are OK as well
        
        if (not self.config.force and 
            util.outfile_is_newer([instance.store.distilled_path(basefile)],
                                  self.store.distilled_path(basefile)) and
            util.outfile_is_newer([instance.store.parsed_path(basefile)],
                                  self.store.parsed_path(basefile))):
            self.log.debug("%s: Attachments are (likely) up-to-date" % basefile)
            return

        util.link_or_copy(instance.store.documententry_path(basefile),
                          self.store.documententry_path(basefile))

        util.link_or_copy(instance.store.distilled_path(basefile),
                          self.store.distilled_path(basefile))

        util.link_or_copy(instance.store.parsed_path(basefile),
                          self.store.parsed_path(basefile))

        cnt = 0
        if instance.store.storage_policy == "dir":
            for attachment in instance.store.list_attachments(basefile, "parsed"):
                cnt += 1
                src = instance.store.parsed_path(basefile, attachment=attachment)
                target = self.store.parsed_path(basefile, attachment=attachment)
                util.link_or_copy(src, target)
            if cnt:
                self.log.debug("%s: Linked %s attachments from %s to %s" %
                               (basefile,
                                cnt,
                                os.path.dirname(instance.store.parsed_path(basefile)),
                                os.path.dirname(self.store.parsed_path(basefile))))
