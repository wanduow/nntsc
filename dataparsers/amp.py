import sys

from libnntsc.database import Database
from libnntsc.configurator import *
import pika
from ampsave.importer import import_data_functions
from libnntsc.parsers import amp_icmp, amp_traceroute, amp_http2, amp_udpstream
import time
import logging

class AmpModule:
    def __init__(self, tests, nntsc_config, exp):

        logging.basicConfig()
        dbconf = get_nntsc_db_config(nntsc_config)
        if dbconf == {}:
            sys.exit(1)
        
        self.disable = get_nntsc_config_bool(nntsc_config, "amp", "disable")
        if self.disable == "NNTSCConfigError":
            print >> sys.stderr, "Invalid disable option for AMP"
            sys.exit(1)

        if self.disable == True:
            return
        
        self.disable = False    # just to be safe

        self.db = Database(dbconf["name"], dbconf["user"], dbconf["pass"],
                dbconf["host"])

        self.exporter = exp

        # the amp modules understand how to extract the test data from the blob
        self.amp_modules = import_data_functions()

        # set all the streams that we already know about for easy lookup of
        # their stream id when reporting data
        for i in tests:
            
            testtype = i["modsubtype"]

            if testtype == "icmp":
                key = amp_icmp.create_existing_stream(i)
            
        # Parse connection info
        username = get_nntsc_config(nntsc_config, "amp", "username")
        if username == "NNTSCConfigError":
            print >> sys.stderr, "Invalid username option for AMP" 
            sys.exit(1)
        password = get_nntsc_config(nntsc_config, "amp", "password")
        if password == "NNTSCConfigError":
            print >> sys.stderr, "Invalid password option for AMP" 
            sys.exit(1)
        self.host = get_nntsc_config(nntsc_config, "amp", "host")
        if self.host == "NNTSCConfigError":
            print >> sys.stderr, "Invalid host option for AMP" 
            sys.exit(1)
        self.port = get_nntsc_config(nntsc_config, "amp", "port")
        if self.port == "NNTSCConfigError":
            print >> sys.stderr, "Invalid port option for AMP" 
            sys.exit(1)
        self.ssl = get_nntsc_config_bool(nntsc_config, "amp", "ssl")
        if self.ssl == "NNTSCConfigError":
            print >> sys.stderr, "Invalid ssl option for AMP" 
            sys.exit(1)
        self.queue = get_nntsc_config(nntsc_config, "amp", "queue")
        if self.queue == "NNTSCConfigError":
            print >> sys.stderr, "Invalid queue option for AMP" 
            sys.exit(1)

        self.credentials = pika.PlainCredentials(username, password)
        self.connect_rabbit() 
        
    def connect_rabbit(self):
        # Connect to rabbitmq -- try again if the connection fails
        attempts = 1

        while True:
            try:
                self.connection = pika.BlockingConnection(
                        pika.ConnectionParameters(
                                host=self.host,
                                port=int(self.port),
                                ssl=self.ssl,
                                credentials=self.credentials)
                        )
                break
            except Exception:
                delay = attempts * 10.0
                if delay > 120:
                    delay = 120.0
                print >> sys.stderr, "Failed to connect to RabbitMQ (attempt %d), trying again in %.0f seconds" % (attempts, delay)
                time.sleep(delay)
                attempts += 1
                continue

        self.channel = self.connection.channel()
        self.channel.queue_declare(queue=self.queue, durable=True)
        # limit to only one outstanding message at a time
        self.channel.basic_qos(prefetch_count=1)
        self.channel.basic_consume(self.process_data, queue=self.queue)

        # TODO: Add some sort of callback in the event of the server going
        # away   

    def process_data(self, channel, method, properties, body):
        """ Process a single message from the queue.
            Depending on the test this message may include multiple results.
        """
        test = properties.headers["x-amp-test-type"]
        if test in self.amp_modules:
            data = self.amp_modules[test].get_data(body)
            source = properties.headers["x-amp-source-monitor"]
            
            if test == "icmp":
                amp_icmp.process_data(self.db, self.exporter, 
                        properties.timestamp, data, source)
        else:
            print >> sys.stderr, "unknown test: '%s'" % (
                    properties.headers["x-amp-test-type"])
        # TODO check if it all worked, don't ack if it fails
        self.db.commit_transaction()
        channel.basic_ack(delivery_tag = method.delivery_tag)

    def run(self):
        """ Run forever, calling the process_data callback for each message """
        print "Running amp modules: %s" % " ".join(self.amp_modules)
       
        if self.disable:
            return
             
        try:
            self.channel.start_consuming()
        except KeyboardInterrupt:
            self.channel.stop_consuming()
        print >> sys.stderr, "AMP: Closing connection to RabbitMQ"
        self.connection.close()

def run_module(tests, config, exp):
    amp = AmpModule(tests, config, exp)
    amp.run()

def tables(db):

    amp_icmp.register(db)
    amp_traceroute.register(db)
    amp_http2.register(db)
    amp_udpstream.register(db)

# vim: set sw=4 tabstop=4 softtabstop=4 expandtab :
