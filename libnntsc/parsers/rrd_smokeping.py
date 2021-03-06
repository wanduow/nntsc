#
# This file is part of NNTSC.
#
# Copyright (C) 2013-2017 The University of Waikato, Hamilton, New Zealand.
#
# Authors: Shane Alcock
#          Brendon Jones
#
# All rights reserved.
#
# This code has been developed by the WAND Network Research Group at the
# University of Waikato. For further information please see
# http://www.wand.net.nz/
#
# NNTSC is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.
#
# NNTSC is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with NNTSC; if not, write to the Free Software Foundation, Inc.
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#
# Please report any bugs, questions or comments to contact@wand.net.nz
#

from libnntsc.parsers.common import NNTSCParser
from libnntsc.dberrorcodes import DB_DATA_ERROR
import libnntscclient.logger as logger

class RRDSmokepingParser(NNTSCParser):

    def __init__(self, db, influxdb=None):
        super(RRDSmokepingParser, self).__init__(db, influxdb)

        self.influxdb = influxdb
        self.streamtable = "streams_rrd_smokeping"
        self.datatable = "data_rrd_smokeping"
        self.colname = "rrd_smokeping"
        self.source = "rrd"
        self.module = "smokeping"

        self.streamcolumns = [
            {"name":"filename", "type":"varchar", "null":False},
            {"name":"source", "type":"varchar", "null":False},
            {"name":"host", "type":"varchar", "null":False},
            {"name":"family", "type":"varchar", "null":False},
            {"name":"minres", "type":"integer", "null":False, "default":"300"},
            {"name":"highrows", "type":"integer", "null":False,
                    "default":"1008"}
        ]

        self.uniquecolumns = ['filename', 'source', 'host', 'family']
        self.streamindexes = [
            {"name": "", "columns": ['source']},
            {"name": "", "columns": ['host']},
        ]

        self.datacolumns = [
            {"name":"loss", "type":"smallint", "null":True},
            {"name":"pingsent", "type": "smallint", "null": True},
            {"name":"median", "type":"double precision", "null": True},
            {"name":"pings", "type":"double precision[]", "null": True},
            {"name":"lossrate", "type": "float", "null": False},
        ]

        self.dataindexes = []

        self.matrix_cq = [
            ("median", "mean", "median_avg"),
            ("median", "stddev", "median_stddev"),
            ("median", "count", "median_count"),
            ("loss", "sum", "loss_sum"),
        ]

    def insert_stream(self, streamparams):
        if 'source' not in streamparams:
            logger.log("Missing 'source' parameter for Smokeping RRD")
            return DB_DATA_ERROR
        if 'host' not in streamparams:
            logger.log("Missing 'host' parameter for Smokeping RRD")
            return DB_DATA_ERROR
        if 'name' not in streamparams:
            logger.log("Missing 'name' parameter for Smokeping RRD")
            return DB_DATA_ERROR
        if 'family' not in streamparams:
            logger.log("Missing 'family' parameter for Smokeping RRD")
            return DB_DATA_ERROR

        streamparams['filename'] = streamparams.pop('file')

        return self.create_new_stream(streamparams, 0, not self.have_influx)


    def process_data(self, stream, ts, line):
        kwargs = {}

        if len(line) >= 1:
            if line[1] is None:
                kwargs['loss'] = None
            else:
                kwargs['loss'] = int(float(line[1]))

        if len(line) >= 2:
            if line[2] is None:
                kwargs['median'] = None
            else:
                kwargs['median'] = round(float(line[2]) * 1000.0, 6)

        kwargs['pings'] = []

        sent = 0
        for i in range(3, len(line)):
            sent += 1
            if line[i] is None:
                val = None
            else:
                val = round(float(line[i]) * 1000.0, 6)

            kwargs['pings'].append(val)

        kwargs['pingsent'] = sent
        if sent == 0 or kwargs['loss'] is None:
            kwargs['lossrate'] = None
        else:
            kwargs['lossrate'] = kwargs['loss'] / float(sent)

        if self.influxdb:
            casts = {"pings": str}
        else:
            casts = {"pings":"double precision[]"}
        self.insert_data(stream, ts, kwargs, casts)


# vim: set sw=4 tabstop=4 softtabstop=4 expandtab :
