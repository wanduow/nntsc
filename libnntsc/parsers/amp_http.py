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
import libnntscclient.logger as logger

class AmpHttpParser(NNTSCParser):
    def __init__(self, db, influxdb=None):
        super(AmpHttpParser, self).__init__(db, influxdb)

        self.streamtable = "streams_amp_http"
        self.datatable = "data_amp_http"
        self.colname = "amp_http"
        self.source = "amp"
        self.module = "http"

        self.streamcolumns = [
            {"name":"source", "type":"varchar", "null":False},
            {"name":"destination", "type":"varchar", "null":False},
            {"name":"max_connections", "type":"integer", "null":False},
            {"name":"max_connections_per_server", "type":"smallint", "null":False},
            {"name":"max_persistent_connections_per_server", "type":"smallint", "null":False},
            {"name":"pipelining_max_requests", "type":"smallint", "null":False},
            {"name":"persist", "type":"boolean", "null":False},
            {"name":"pipelining", "type":"boolean", "null":False},
            {"name":"caching", "type":"boolean", "null":False},
        ]

        self.uniquecolumns = ['source', 'destination', 'max_connections',
                'max_connections_per_server',
                "max_persistent_connections_per_server",
                "pipelining_max_requests",
                "persist", "pipelining", "caching"]
        self.streamindexes = [
            {"name": "", "columns": ['source']},
            {"name": "", "columns": ['destination']}
        ]

        self.datacolumns = [
            {"name":"server_count", "type":"integer", "null":True},
            {"name":"object_count", "type":"integer", "null":True},
            {"name":"duration", "type":"integer", "null":True},
            {"name":"bytes", "type":"bigint", "null":True},
        ]

        self.dataindexes = [
        ]

        self.matrix_cq = [
            ('"duration"', 'mean', '"duration_avg"'),
            ('"duration"', 'stddev', '"duration_stddev"'),
            ('"bytes"', 'max', '"bytes_max"'),
            ('"bytes"', 'mean', '"bytes_avg"'),
            ('"bytes"', 'stddev', '"bytes_stddev"')
        ]


    def _stream_key(self, stream_data):
        src = str(stream_data["source"])

        if 'url' in stream_data:
            dest = str(stream_data["url"])
        else:
            dest = str(stream_data['destination'])

        max_c = str(stream_data['max_connections'])
        max_cps = str(stream_data['max_connections_per_server'])
        max_pcps = str(stream_data['max_persistent_connections_per_server'])

        if 'pipelining_maxrequests' in stream_data:
            pipe_max = str(stream_data['pipelining_maxrequests'])
        else:
            pipe_max = str(stream_data['pipelining_max_requests'])

        pipe = stream_data['pipelining']

        if 'keep_alive' in stream_data:
            persist = stream_data['keep_alive']
        else:
            persist = stream_data['persist']
        caching = stream_data['caching']

        key = (src, dest, max_c, max_cps, max_pcps, pipe_max, persist, pipe, caching)

        return key

    def create_existing_stream(self, stream_data):
        """Extract the stream key from the stream data provided by NNTSC
    when the AMP module is first instantiated"""

        key = self._stream_key(stream_data)
        self.streams[key] = stream_data['stream_id']

    def _mangle_result(self, data):
        # Our columns are slightly different to the names that AMPsave uses,
        # so we'll have to mangle them to match what we're expecting
        key = self._stream_key(data)

        mangled = {}
        mangled['source'] = key[0]
        mangled['destination'] = key[1]
        mangled['max_connections'] = key[2]
        mangled['max_connections_per_server'] = key[3]
        mangled['max_persistent_connections_per_server'] = key[4]
        mangled['pipelining_max_requests'] = key[5]
        mangled['persist'] = key[6]
        mangled['pipelining'] = key[7]
        mangled['caching'] = key[8]

        mangled['server_count'] = data['server_count']
        mangled['object_count'] = data['object_count']

        # AMPSave reports duration in ms, we're going to store it as ms
        if data['duration'] is None:
            mangled['duration'] = None
        else:
            mangled['duration'] = int(data['duration'])
        mangled['bytes'] = data['bytes']

        return mangled, key

    def process_data(self, timestamp, data, source):
        data['source'] = source

        mangled, key = self._mangle_result(data)

        if key in self.streams:
            stream_id = self.streams[key]
        else:
            stream_id = self.create_new_stream(mangled, timestamp,
                    not self.have_influx)
            if stream_id < 0:
                logger.log("AMPModule: Cannot create stream for: ")
                logger.log("AMPModule: dns %s %s\n", source, \
                        mangled['destination'])
                return
            self.streams[key] = stream_id

        self.insert_data(stream_id, timestamp, mangled)
        self.db.update_timestamp(self.datatable, [stream_id], timestamp,
                self.have_influx)

# vim: set sw=4 tabstop=4 softtabstop=4 expandtab :

