# This file is part of NNTSC
#
# Copyright (C) 2013 The University of Waikato, Hamilton, New Zealand
# Authors: Shane Alcock
#          Brendon Jones
#          Nathan Overall
#
# All rights reserved.
#
# This code has been developed by the WAND Network Research Group at the
# University of Waikato. For more information, please see
# http://www.wand.net.nz/
#
# This source code is proprietary to the University of Waikato and may not be
# redistributed, published or disclosed without prior permission from the
# University of Waikato and the WAND Network Research Group.
#
# Please report any bugs, questions or comments to contact@wand.net.nz
#
# $Id$


import sys

from libnntsc.database import DBInsert
from libnntsc.influx import InfluxInsertor
from libnntsc.configurator import *
from libnntsc.pikaqueue import PikaConsumer, initExportPublisher, \
        PikaNNTSCException, PIKA_CONSUMER_HALT, PIKA_CONSUMER_RETRY
import pika
from ampsave.importer import import_data_functions
from ampsave.exceptions import AmpTestVersionMismatch
from libnntsc.parsers.amp_icmp import AmpIcmpParser
from libnntsc.parsers.amp_traceroute import AmpTracerouteParser
from libnntsc.parsers.amp_dns import AmpDnsParser
from libnntsc.parsers.amp_http import AmpHttpParser
from libnntsc.parsers.amp_throughput import AmpThroughputParser
from libnntsc.parsers.amp_tcpping import AmpTcppingParser
from libnntsc.dberrorcodes import *
from google.protobuf.message import DecodeError
import time, signal
import logging

import libnntscclient.logger as logger

DEFAULT_COMMIT_FREQ=50

class AmpModule:
    def __init__(self, tests, nntsc_config, routekey, exchange, queueid):
        
        self.pending = []
        self.exporter = None
        self.pubthread = None

        logging.basicConfig()
        self.dbconf = get_nntsc_db_config(nntsc_config)
        if self.dbconf == {}:
            sys.exit(1)
        
        self.db = DBInsert(self.dbconf["name"], self.dbconf["user"], 
                self.dbconf["pass"], self.dbconf["host"])

        self.db.connect_db(15)

        self.influxconf = get_influx_config(nntsc_config)
        if self.influxconf == {}:
            sys.exit(1)

        if self.influxconf["useinflux"]:
            self.influxdb = InfluxInsertor(
                self.influxconf["name"], self.influxconf["user"], self.influxconf["pass"],
                self.influxconf["host"], self.influxconf["port"])
        else:
            self.influxdb = None

        # the amp modules understand how to extract the test data from the blob
        self.amp_modules = import_data_functions()

        self.collections = {}
        try:
            cols = self.db.list_collections()
        except DBQueryException as e:
            log(e)
            cols = []

        for c in cols:
            if c['module'] == "amp":
                self.collections[c['modsubtype']] = c['id']

        self.parsers = {
            "icmp":AmpIcmpParser(self.db, self.influxdb),
            "traceroute":AmpTracerouteParser(self.db),
            "throughput":AmpThroughputParser(self.db, self.influxdb),
            "dns":AmpDnsParser(self.db, self.influxdb),
            "http":AmpHttpParser(self.db, self.influxdb),
            "tcpping":AmpTcppingParser(self.db, self.influxdb)
        }

        # set all the streams that we already know about for easy lookup of
        # their stream id when reporting data
        for i in tests:

            testtype = i["modsubtype"]
            if testtype in self.parsers:
                key = self.parsers[testtype].create_existing_stream(i)

        self.initSource(nntsc_config)
        
        liveconf = get_nntsc_config_bool(nntsc_config, "liveexport", "enabled")
        if liveconf == "NNTSCConfigError":
            logger.log("Bad 'enabled' option for liveexport -- disabling")
            liveconf = False

        if liveconf == "NNTSCConfigMissing":
            liveconf = True

        if liveconf:
            self.exporter, self.pubthread = \
                    initExportPublisher(nntsc_config, routekey, exchange, \
                    queueid)

            for k, parser in self.parsers.iteritems():
                parser.add_exporter(self.exporter)

    def initSource(self, nntsc_config):
        # Parse connection info
        username = get_nntsc_config(nntsc_config, "amp", "username")
        if username == "NNTSCConfigMissing":
            username = "amp"
        password = get_nntsc_config(nntsc_config, "amp", "password")
        if password == "NNTSCConfigMissing":
            logger.log("Password not set for AMP RabbitMQ source, using empty string as default")
            password = ""
        host = get_nntsc_config(nntsc_config, "amp", "host")
        if host == "NNTSCConfigMissing":
            host = "localhost"
        port = get_nntsc_config(nntsc_config, "amp", "port")
        if port == "NNTSCConfigMissing":
            port = "5672"
        ssl = get_nntsc_config_bool(nntsc_config, "amp", "ssl")
        if ssl == "NNTSCConfigMissing":
            ssl = False
        queue = get_nntsc_config(nntsc_config, "amp", "queue")
        if queue == "NNTSCConfigMissing":
            queue = "amp-nntsc"
        
        self.commitfreq = get_nntsc_config(nntsc_config, "amp", "commitfreq")
        if self.commitfreq == "NNTSCConfigMissing":
            self.commitfreq = DEFAULT_COMMIT_FREQ
        else:
            self.commitfreq = int(self.commitfreq)

        if "NNTSCConfigError" in [username, password, host, port, ssl, queue]:
            logger.log("Failed to configure AMP source")
            sys.exit(1)

        logger.log("Connecting to RabbitMQ queue %s on host %s:%s (ssl=%s), username %s" % (queue, host, port, ssl, username))

        self.source = PikaConsumer('', queue, host, port, 
                ssl, username, password, True)


    def process_data(self, channel, method, properties, body):
        """ Process a single message from the queue.
            Depending on the test this message may include multiple results.
        """

        # push every new message onto the end of the list to be processed
        self.pending.append((channel, method, properties, body))

        # once there are enough messages ready to go, process them all
        if len(self.pending) < self.commitfreq:
            return

        # track how many messages were successfully written to the database
        processed = 0

        for channel, method, properties, body in self.pending:
            # ignore any messages that don't have user_id set
            if not hasattr(properties, "user_id"):
                continue

            test = properties.headers["x-amp-test-type"]

            # ignore any messages for tests we don't have a module for
            if test not in self.amp_modules:
                logger.log("unknown test: '%s'" % (
                        properties.headers["x-amp-test-type"]))
                logger.log("AMP -- Data error, acknowledging and moving on")
                continue

            # ignore any messages for tests we don't have a parser for
            if test not in self.parsers:
                continue

            try:
                data = self.amp_modules[test].get_data(body)
            except DecodeError as e:
                # TODO restore this error message once clients updated >= 0.5.0
                # we got something that wasn't a valid protocol buffer message
                #logger.log("Failed to decode result from %s for %s test: %s" %(
                #properties.user_id, test, e))

                # TODO remove these once all clients use amplet-client >= 0.5.0
                # temporarily try old data parsing functions
                try:
                    data = self.amp_modules[test].get_old_data(body)
                except AmpTestVersionMismatch as e:
                    logger.log("Old %s test (Version mismatch): %s" % (test, e))
                    data = None
                except AssertionError as e:
                    logger.log("Old %s test (assert failure): %s" % (test, e))
                    data = None
            except AmpTestVersionMismatch as e:
                logger.log("Ignoring AMP result for %s test (Version mismatch): %s" % (test, e))
                data = None
            except AssertionError as e:
                # A lot of ampsave functions assert fail if something goes
                # wrong, so we need to catch that and chuck the bogus data
                logger.log("Ignoring AMP result for %s test (ampsave assertion failure): %s" % (test, e))
                data = None

            # ignore any broken messages and carry on
            if data is None:
                continue

            try:
                # pass the message off to the test specific code
                self.parsers[test].process_data(properties.timestamp, data,
                        properties.user_id)
                processed += 1
            except DBQueryException as e:
                if e.code == DB_OPERATIONAL_ERROR:
                    # Disconnect while inserting data, need to reprocess the
                    # entire set of messages
                    logger.log("Database disconnect while processing AMP data")
                    channel.close()
                    return

                elif e.code == DB_DATA_ERROR:
                    # Data was bad so we couldn't insert into the database.
                    # Acknowledge the message so we can dump it from the queue
                    # and move on but don't try to export it to clients.
                    logger.log("AMP -- Data error, acknowledging and moving on")
                    continue

                elif e.code == DB_INTERRUPTED:
                    logger.log("Interrupt while processing AMP data")
                    channel.close()
                    return

                elif e.code == DB_GENERIC_ERROR:
                    logger.log("Database error while processing AMP data")
                    channel.close()
                    return

                elif e.code == DB_QUERY_TIMEOUT:
                    logger.log("Database timeout while processing AMP data")
                    channel.close()
                    return

                elif e.code == DB_CODING_ERROR:
                    logger.log("Bad database code encountered while processing AMP data")
                    channel.close()
                    return

                elif e.code == DB_DUPLICATE_KEY:
                    logger.log("Duplicate key error while processing AMP data")
                    channel.close()
                    return

                else:
                    logger.log("Unknown error code returned by database: %d" %
                            (code))
                    logger.log("Shutting down AMP module")
                    channel.close()
                    return

        # commit the data if anything was successfully processed
        if processed > 0:
            self.db.commit_data()

        # ack all data up to and including the most recent message
        channel.basic_ack(method.delivery_tag, True)

        # empty the list of pending data ready for more
        self.pending = []

    def run(self):
        """ Run forever, calling the process_data callback for each message """

        logger.log("Running amp modules: %s" % " ".join(self.amp_modules))


        try:
            self.source.configure([], self.process_data, self.commitfreq)
            self.source.run()
        except KeyboardInterrupt:
            self.source.halt_consumer()
        except:
            logger.log("AMP: Unknown exception during consumer loop")
            raise

        logger.log("AMP: Closed connection to RabbitMQ")

def run_module(tests, config, key, exchange, queueid):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    amp = AmpModule(tests, config, key, exchange, queueid)
    amp.run()

    if amp.pubthread:
        amp.pubthread.join()

def cqs(db, influxdb, retention_policy="default"):

    parser = AmpIcmpParser(db, influxdb)
    parser.build_cqs(retention_policy)

    parser = AmpTcppingParser(db, influxdb)
    parser.build_cqs(retention_policy)

    parser = AmpDnsParser(db, influxdb)
    parser.build_cqs(retention_policy)

    parser = AmpThroughputParser(db, influxdb)
    parser.build_cqs(retention_policy)

    parser = AmpHttpParser(db, influxdb)
    parser.build_cqs(retention_policy)

    
def tables(db):

    parser = AmpIcmpParser(db)
    parser.register()
        
    parser = AmpTracerouteParser(db)
    parser.register()

    parser = AmpDnsParser(db)
    parser.register()

    parser = AmpThroughputParser(db)
    parser.register()
    
    parser = AmpTcppingParser(db)
    parser.register()
    
    parser = AmpHttpParser(db)
    parser.register()

# vim: set sw=4 tabstop=4 softtabstop=4 expandtab :
