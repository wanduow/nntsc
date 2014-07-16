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


from libnntsc.database import DBInsert
from libnntsc.dberrorcodes import *
from libnntsc.configurator import *
from libnntsc.parsers.rrd_smokeping import RRDSmokepingParser
from libnntsc.parsers.rrd_muninbytes import RRDMuninbytesParser
import libnntscclient.logger as logger
import sys, rrdtool, socket, time
from libnntsc.pikaqueue import initExportPublisher

RRD_RETRY = 0
RRD_CONTINUE = 1
RRD_HALT = 2

class RRDModule:
    def __init__(self, rrds, nntsc_conf, expqueue, exchange):

        dbconf = get_nntsc_db_config(nntsc_conf)
        if dbconf == {}:
            sys.exit(1)

    
        self.db = DBInsert(dbconf["name"], dbconf["user"], dbconf["pass"],
                dbconf["host"], cachetime=dbconf['cachetime'])
        self.db.connect_db(15)

        self.smokeparser = RRDSmokepingParser(self.db)
        self.muninparser = RRDMuninbytesParser(self.db)

        self.smokepings = {}
        self.muninbytes = {}
        self.rrds = {}
        for r in rrds:
            if r['modsubtype'] == 'smokeping':
                lastts = self.smokeparser.get_last_timestamp(r['stream_id'])
                r['lasttimestamp'] = lastts
                self.smokepings[r['stream_id']] = r
            elif r['modsubtype'] == 'muninbytes':
                lastts = self.muninparser.get_last_timestamp(r['stream_id'])
                r['lasttimestamp'] = lastts
                self.muninbytes[r['stream_id']] = r
            else:
                continue

            r['lastcommit'] = r['lasttimestamp']
            filename = str(r['filename'])
            if filename in self.rrds:
                self.rrds[filename].append(r)
            else:
                self.rrds[filename] = [r]

        self.exporter = initExportPublisher(nntsc_conf, expqueue, exchange)

        self.smokeparser.add_exporter(self.exporter)
        self.muninparser.add_exporter(self.exporter)

    def rejig_ts(self, endts, r):
        # Doing dumbass stuff that I shouldn't have to do to ensure
        # that the last line of output from fetch isn't full of NaNs.
        # First we try to make sure endts falls on a period boundary,
        # which you think would be enough, but even being on the
        # boundary is enough to make rrdfetch think it needs to give
        # you an extra period's worth of output, even if that output
        # is totally useless :(
        #
        # XXX Surely there must be a better way of dealing with this!

        if (endts % r['minres']) != 0:
            endts -= (endts % r['minres'])
            #endts -= 1

        startts = endts - (r['highrows'] * r['minres'])

        if (r["lasttimestamp"] > startts):
            startts = r["lasttimestamp"]

        # XXX Occasionally we manage to push our endts back past our last
        # timestamp, so we need to make sure we don't query for a broken
        # time period. This is a bit of a hax fix, but is better than nothing
        if endts < startts:
            endts = startts

        return startts, endts

    def read_from_rrd(self, r, fname):
        r['lastcommit'] = r['lasttimestamp']
        stream_id = r['stream_id']
        endts = rrdtool.last(str(fname))

        startts, endts = self.rejig_ts(endts, r)

        fetchres = rrdtool.fetch(fname, "AVERAGE", "-s",
                str(startts), "-e", str(endts))

        current = int(fetchres[0][0])
        last = int(fetchres[0][1])
        step = int(fetchres[0][2])

        data = fetchres[2]
        current += step

        update_needed = False
        datatable = None

        for line in data:

            if current == last:
                break

            code = DB_DATA_ERROR
            if r['modsubtype'] == "smokeping":
                code = self.smokeparser.process_data(r['stream_id'], current, 
                        line)
                if datatable is None:
                    datatable = self.smokeparser.get_data_table_name()

            if r['modsubtype'] == "muninbytes":
                code = self.muninparser.process_data(r['stream_id'], current, 
                        line)
                if datatable is None:
                    datatable = self.muninparser.get_data_table_name()

            if code == DB_NO_ERROR:
                if current > r['lasttimestamp']:
                    r['lasttimestamp'] = current
                    update_needed = True

            if code == DB_QUERY_TIMEOUT or code == DB_OPERATIONAL_ERROR:
                return code
                
            if code == DB_INTERRUPTED:
                logger.log("Interrupt in RRD module")
                return code

            if code != DB_NO_ERROR:
                logger.log("Error while inserting RRD data")

            current += step

        if not update_needed:
            return DB_NO_ERROR
        
        
        err = self.db.commit_data()
        if err != DB_NO_ERROR:
            return err

        if datatable is not None:
            code = self.db.update_timestamp(datatable, [r['stream_id']],
                r['lasttimestamp'])

        if code == DB_QUERY_TIMEOUT or code == DB_OPERATIONAL_ERROR:
            return code
        if code == DB_INTERRUPTED:
            logger.log("Interrupt in RRD module")
            return code

        if code != DB_NO_ERROR:
            logger.log("Error while updating last timestamp for RRD stream")
            return code

        return DB_NO_ERROR

    def rrdloop(self):
        for fname,rrds in self.rrds.items():
            for r in rrds:
                err = self.read_from_rrd(r, fname)

                if err == DB_QUERY_TIMEOUT or err == DB_OPERATIONAL_ERROR:
                    return RRD_RETRY
                if err == DB_INTERRUPTED:
                    return RRD_HALT
                # Ignore other DB errors as they represent bad data or 
                # database code. Try to carry on to the next RRD instead.
        return RRD_CONTINUE


    def run(self):
        logger.log("Starting RRD module")
        while True:
            result = self.rrdloop()
            
            if result == RRD_RETRY:
                self.revert_rrds()
                time.sleep(10)
                continue

            if result == RRD_HALT:
                break

            time.sleep(30)

        logger.log("Halting RRD module")

    def revert_rrds(self):
        logger.log("Reverting RRD timestamps to previous safe value")
        for fname,rrds in self.rrds.items():
            for r in rrds:
                if 'lastcommit' in r:
                    r['lasttimestamp'] = r['lastcommit']

def create_rrd_stream(db, rrdtype, params, index, existing, parser):

    if parser is None:
        return DB_NO_ERROR

    if "file" not in params:
        logger.log("Failed to create stream for RRD %d" % (index))
        logger.log("All RRDs must have a 'file' parameter")
        return DB_DATA_ERROR

    if params['file'] in existing:
        return DB_NO_ERROR

    info = rrdtool.info(params['file'])
    params['minres'] = info['step']
    params['highrows'] = info['rra[0].rows']
    logger.log("Creating stream for RRD-%s: %s" % (rrdtype, params['file']))

    code = parser.insert_stream(params)
    if code == DB_GENERIC_ERROR:
        logger.log("Database error while creating RRD stream")
    if code == DB_DATA_ERROR:
        logger.log("Invalid RRD stream description")
    if code == DB_INTERRUPTED:
        logger.log("RRD stream processing interrupted")
    if code == DB_DUPLICATE_KEY:
        # Note, we should not see this error as we should get
        # back the id of the duplicate stream instead
        logger.log("Duplicate key error while inserting RRD stream")
    if code == DB_CODING_ERROR:
        logger.log("Programming error while inserting RRD stream")
    if code == DB_QUERY_TIMEOUT:
        logger.log("Timeout while inserting RRD stream")

    return code

def insert_rrd_streams(db, conf):
    smoke = RRDSmokepingParser(db)
    munin = RRDMuninbytesParser(db)

    try:
        rrds = db.select_streams_by_module("rrd")
    except DBQueryException as e:
        logger.log("Error while fetching existing RRD streams from database")
        return

    files = set()
    for r in rrds:
        files.add(r['filename'])


    if conf == "":
        return

    try:
        f = open(conf, "r")
    except IOError, e:
        logger.log("WARNING: %s does not exist - no RRD streams will be added" % (conf))
        return

    logger.log("Reading RRD list from %s" % (conf))

    index = 1
    subtype = None
    parameters = {}

    for line in f:
        if line[0] == '#':
            continue
        if line == "\n" or line == "":
            continue

        x = line.strip().split("=")
        if len(x) != 2:
            continue

        if x[0] == "type":
            if parameters != {}:
                if subtype == "smokeping":
                    parser = smoke
                elif subtype == "muninbytes":
                    parser = munin
                else:
                    parser = None
    
                code = create_rrd_stream(db, subtype, parameters, index, 
                       files, parser)
                if code < 0:
                    return code
                    
            parameters = {}
            subtype = x[1]
            index += 1
        else:
            parameters[x[0]] = x[1]


    if parameters != {}:
        if subtype == "smokeping":
            parser = smoke
        elif subtype == "muninbytes":
            parser = munin
        else:
            parser = None
        code = create_rrd_stream(db, subtype, parameters, index, files, parser)
        if code < 0:
            return code

    f.close()
    return DB_NO_ERROR


def run_module(rrds, config, key, exchange):
    rrd = RRDModule(rrds, config, key, exchange)
    rrd.run()


def tables(db):

    smoke = RRDSmokepingParser(db)
    munin = RRDMuninbytesParser(db)

    err = smoke.register()
    if err != DB_NO_ERROR:
        return err

    return munin.register()


# vim: set sw=4 tabstop=4 softtabstop=4 expandtab :
